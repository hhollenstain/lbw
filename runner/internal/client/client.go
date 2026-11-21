// Package client talks to an LBW server. Every request is outbound HTTPS from
// the runner, so the runner needs no open port.
package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// ErrUnauthorized means the runner's token was revoked or never issued.
var ErrUnauthorized = errors.New("this runner is not linked: run `lbw-runner login`")

// ErrPending means the person has not entered the link code yet.
var ErrPending = errors.New("waiting for approval")

// Client is one LBW server and this runner's token.
type Client struct {
	Server string
	Token  string
	HTTP   *http.Client
}

// New builds a client. caFile adds a certificate authority, such as a local dev CA.
func New(server, token, caFile string) (*Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("ca_file: %w", err)
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("ca_file %s has no certificates", caFile)
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	return &Client{
		Server: strings.TrimRight(server, "/"),
		Token:  token,
		HTTP:   &http.Client{Transport: transport, Timeout: 60 * time.Second},
	}, nil
}

// Link is what the server returns when linking starts.
type Link struct {
	DeviceCode      string `json:"device_code"`
	UserCode        string `json:"user_code"`
	VerificationURL string `json:"verification_url"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
}

// Claimed is the runner's own token, returned once.
type Claimed struct {
	Token    string `json:"token"`
	RunnerID string `json:"runner_id"`
}

// Facts are what the runner reports at each heartbeat.
type Facts struct {
	Version       string `json:"version"`
	ClaudeVersion string `json:"claude_version"`
	ClaudeReady   bool   `json:"claude_ready"`
	Platform      string `json:"platform"`
}

// Beat is the server's answer to a heartbeat.
type Beat struct {
	RunnerID string   `json:"runner_id"`
	Name     string   `json:"name"`
	Commands []string `json:"commands"`
}

// Job is one leased job.
type Job struct {
	JobID       string `json:"job_id"`
	TaskID      string `json:"task_id"`
	Goal        string `json:"goal"`
	ReviewNotes string `json:"review_notes"`
	BaseSHA     string `json:"base_sha"`
	BaseBranch  string `json:"base_branch"`
	RemoteURL   string `json:"remote_url"`
	Repository  string `json:"repository"`
	Project     string `json:"project"`
	Model       string `json:"model"`
	MaxSeconds  int    `json:"max_seconds"`
}

// Event is one line of progress shown on the job in LBW.
type Event struct {
	Kind             string `json:"kind"`
	Message          string `json:"message,omitempty"`
	Name             string `json:"name,omitempty"`
	ModelName        string `json:"model_name,omitempty"`
	PromptTokens     int64  `json:"prompt_tokens,omitempty"`
	CompletionTokens int64  `json:"completion_tokens,omitempty"`
	TotalTokens      int64  `json:"total_tokens,omitempty"`
	CacheReadTokens  int64  `json:"cache_read_tokens,omitempty"`
	CacheWriteTokens int64  `json:"cache_write_tokens,omitempty"`
	ElapsedMS        int64  `json:"elapsed_ms,omitempty"`
}

// Result is what the runner sends when a job ends.
type Result struct {
	Status  string `json:"status"`
	Summary string `json:"summary"`
	Error   string `json:"error"`
	Patch   string `json:"patch"`
}

// StartLink begins linking. The person enters UserCode on their profile.
func (c *Client) StartLink(ctx context.Context, name, platform string) (*Link, error) {
	var link Link
	_, err := c.do(ctx, http.MethodPost, "/api/runner/link", map[string]string{"name": name, "platform": platform}, &link, false)
	return &link, err
}

// ClaimToken returns ErrPending until the person approves the code.
func (c *Client) ClaimToken(ctx context.Context, deviceCode string) (*Claimed, error) {
	var claimed Claimed
	status, err := c.do(ctx, http.MethodPost, "/api/runner/link/token", map[string]string{"device_code": deviceCode}, &claimed, false)
	if status == http.StatusPreconditionRequired {
		return nil, ErrPending
	}
	return &claimed, err
}

// Heartbeat reports health and returns maintenance commands.
func (c *Client) Heartbeat(ctx context.Context, facts Facts) (*Beat, error) {
	var beat Beat
	_, err := c.do(ctx, http.MethodPost, "/api/runner/heartbeat", facts, &beat, true)
	return &beat, err
}

// NextJob waits up to wait for a job. It returns nil when none is waiting.
func (c *Client) NextJob(ctx context.Context, wait time.Duration) (*Job, error) {
	var job Job
	path := fmt.Sprintf("/api/runner/jobs/next?wait=%d", int(wait.Seconds()))
	status, err := c.do(ctx, http.MethodGet, path, nil, &job, true)
	if err != nil {
		return nil, err
	}
	if status == http.StatusNoContent {
		return nil, nil
	}
	return &job, nil
}

// SendEvents also keeps the job's lease alive. It reports whether LBW cancelled the job.
func (c *Client) SendEvents(ctx context.Context, jobID string, events []Event) (bool, error) {
	if events == nil {
		events = []Event{}
	}
	var answer struct {
		Cancel bool `json:"cancel"`
	}
	_, err := c.do(ctx, http.MethodPost, "/api/runner/jobs/"+jobID+"/events", map[string]any{"events": events}, &answer, true)
	return answer.Cancel, err
}

// Finish hands the result back to the waiting job.
func (c *Client) Finish(ctx context.Context, jobID string, result Result) error {
	_, err := c.do(ctx, http.MethodPost, "/api/runner/jobs/"+jobID+"/finish", result, nil, true)
	return err
}

func (c *Client) do(ctx context.Context, method, path string, body, out any, auth bool) (int, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Server+path, reader)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if auth {
		if c.Token == "" {
			return 0, ErrUnauthorized
		}
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch {
	case resp.StatusCode == http.StatusNoContent:
		return resp.StatusCode, nil
	case resp.StatusCode == http.StatusUnauthorized:
		return resp.StatusCode, ErrUnauthorized
	case resp.StatusCode == http.StatusPreconditionRequired:
		return resp.StatusCode, nil
	case resp.StatusCode >= 400:
		var problem struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(data, &problem)
		if problem.Error == "" {
			problem.Error = strings.TrimSpace(string(data))
		}
		return resp.StatusCode, fmt.Errorf("%s %s: %d %s", method, path, resp.StatusCode, problem.Error)
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return resp.StatusCode, fmt.Errorf("%s %s: %w", method, path, err)
		}
	}
	return resp.StatusCode, nil
}
