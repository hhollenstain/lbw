// Package relay passes LBW's model calls to the owner's own model server.
//
// When the runner's owner picks a local model, such as one served by Ollama,
// LBW runs the job and sends each model call here. The runner forwards it to
// the address the owner set on their LBW profile and sends the answer back.
// LBW only hands this runner calls made for its owner. The runner relays two
// calls only (list models, chat completions), and only to this machine or the
// owner's private network unless the owner lists a host in config.json. The
// address comes from LBW, so the runner checks the address it actually dials:
// a host name that resolves to a public address is refused too.
package relay

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
)

// MaxBody caps a model call's request and answer, matching LBW's limit.
const MaxBody = 16 << 20

// Parallel is how many model calls the runner relays at once.
const Parallel = 4

var routes = map[string]string{
	"/v1/models":           http.MethodGet,
	"/v1/chat/completions": http.MethodPost,
}

// cgnat is 100.64.0.0/10, the range Tailscale and carrier NAT hand out.
var cgnat = &net.IPNet{IP: net.IPv4(100, 64, 0, 0), Mask: net.CIDRMask(10, 32)}

// Relay polls LBW for model calls and forwards each one.
type Relay struct {
	API *client.Client
	// HTTP calls the model server. Its timeout bounds one model call.
	HTTP *http.Client
	// Hosts are extra host names the owner allows, such as a server on the
	// public internet.
	Hosts []string
	// PrivateNetwork lets the relay reach private addresses (10/8, 172.16/12,
	// 192.168/16, fc00::/7, 100.64/10) as well as this machine.
	PrivateNetwork bool
	Poll           time.Duration
	Retry          time.Duration
	Log            *log.Logger
	// Monitor shows each call on the dashboard and pauses taking new ones.
	// Nil runs without it.
	Monitor *monitor.Monitor
}

// New builds a relay with a ten-minute limit per model call.
func New(api *client.Client, hosts []string, logger *log.Logger) *Relay {
	r := &Relay{
		API:            api,
		Hosts:          hosts,
		PrivateNetwork: true,
		Poll:           25 * time.Second,
		Retry:          10 * time.Second,
		Log:            logger,
	}
	r.HTTP = &http.Client{
		Timeout: 10 * time.Minute,
		// No proxy: the address dialed must be the one checked.
		Transport: &http.Transport{Proxy: nil, DialContext: r.dial, ForceAttemptHTTP2: true},
		// A redirect could lead anywhere, so it is answered as it is.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	return r
}

// Run relays calls until ctx ends or LBW disconnects the runner.
func (r *Relay) Run(ctx context.Context) error {
	slots := make(chan struct{}, Parallel)
	for ctx.Err() == nil {
		if r.Monitor.Paused() {
			wait(ctx, time.Second)
			continue
		}
		call, err := r.API.NextModelCall(ctx, r.Poll)
		if errors.Is(err, client.ErrUnauthorized) {
			return err
		}
		if err != nil {
			if ctx.Err() == nil {
				r.Log.Printf("model relay could not reach LBW: %v", err)
				wait(ctx, r.Retry)
			}
			continue
		}
		if call == nil {
			continue
		}
		if err := r.API.AckModelCall(ctx, call.CallID); err != nil {
			r.Log.Printf("model call %s: not relayed: %v", call.CallID, err)
			continue
		}
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			return nil
		}
		r.Monitor.CallStarted(call)
		go func(call *client.ModelCall) {
			defer func() { <-slots }()
			answer := r.Forward(ctx, call)
			r.Monitor.CallFinished(call.CallID, answer)
			if err := r.API.AnswerModelCall(ctx, call.CallID, answer); err != nil {
				r.Log.Printf("model call %s: could not answer LBW: %v", call.CallID, err)
			}
		}(call)
	}
	return nil
}

// Forward sends one call to the owner's server. It never returns an error:
// a refusal or failure goes back to LBW in the answer.
func (r *Relay) Forward(ctx context.Context, call *client.ModelCall) client.ModelAnswer {
	target, err := r.Target(call)
	if err != nil {
		return client.ModelAnswer{Error: err.Error()}
	}
	var body io.Reader
	if call.Body != "" {
		body = strings.NewReader(call.Body)
	}
	req, err := http.NewRequestWithContext(ctx, call.Method, target, body)
	if err != nil {
		return client.ModelAnswer{Error: err.Error()}
	}
	req.Header.Set("Accept", "application/json")
	if call.Body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := r.HTTP.Do(req)
	if err != nil {
		return client.ModelAnswer{Error: "your model server did not answer: " + err.Error()}
	}
	defer resp.Body.Close()
	var out bytes.Buffer
	n, err := io.Copy(&out, io.LimitReader(resp.Body, MaxBody+1))
	if err != nil {
		return client.ModelAnswer{Error: "reading your model server's answer: " + err.Error()}
	}
	if n > MaxBody {
		return client.ModelAnswer{Error: "your model server's answer is larger than LBW accepts"}
	}
	return client.ModelAnswer{Status: resp.StatusCode, Body: out.String()}
}

// Target checks the call and returns the URL to send it to.
func (r *Relay) Target(call *client.ModelCall) (string, error) {
	if method, ok := routes[call.Path]; !ok || method != call.Method {
		return "", fmt.Errorf("the runner does not relay %s %s", call.Method, call.Path)
	}
	if len(call.Body) > MaxBody {
		return "", errors.New("the model call is larger than the runner relays")
	}
	base, err := url.Parse(strings.TrimRight(call.BaseURL, "/"))
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return "", fmt.Errorf("the model server address %q is not an http or https address", call.BaseURL)
	}
	if base.User != nil || base.RawQuery != "" || base.Fragment != "" || (base.Path != "" && base.Path != "/") {
		return "", fmt.Errorf("the model server address %q takes only a host and port", call.BaseURL)
	}
	if host := base.Hostname(); !r.listed(host) {
		if ip := net.ParseIP(host); ip != nil && !r.reachable(ip) {
			return "", r.refusal(host)
		}
		if net.ParseIP(host) == nil && !r.PrivateNetwork && !strings.EqualFold(host, "localhost") {
			return "", r.refusal(host)
		}
	}
	return base.Scheme + "://" + base.Host + call.Path, nil
}

// listed reports whether the owner named this host in llm_hosts.
func (r *Relay) listed(host string) bool {
	for _, extra := range r.Hosts {
		if strings.EqualFold(strings.TrimSpace(extra), host) {
			return true
		}
	}
	return false
}

// reachable reports whether the relay may dial this address without llm_hosts.
// Link-local addresses, such as a cloud metadata service, are never reachable.
func (r *Relay) reachable(ip net.IP) bool {
	if ip.IsLoopback() {
		return true
	}
	return r.PrivateNetwork && (ip.IsPrivate() || cgnat.Contains(ip))
}

func (r *Relay) refusal(host string) error {
	if r.PrivateNetwork {
		return fmt.Errorf("the runner relays only to this machine and your private network: add %q to llm_hosts in config.json to allow it", host)
	}
	return fmt.Errorf("this runner relays only to this machine: add %q to llm_hosts in config.json to allow it", host)
}

// dial resolves the host and connects only to an address the relay may reach,
// so a host name cannot lead the relay to a public address.
func (r *Relay) dial(ctx context.Context, network, addr string) (net.Conn, error) {
	var dialer net.Dialer
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}
	if r.listed(host) {
		return dialer.DialContext(ctx, network, addr)
	}
	ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, err
	}
	for _, ip := range ips {
		if r.reachable(ip.IP) {
			return dialer.DialContext(ctx, network, net.JoinHostPort(ip.IP.String(), port))
		}
	}
	return nil, r.refusal(host)
}

func wait(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}
