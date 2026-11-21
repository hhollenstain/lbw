// Package claude runs the person's own Claude Code and reads its stream-json output.
//
// The runner never reads or stores a Claude credential. Claude Code signs in
// through Anthropic's own flow and keeps its login where it always does. In a
// container, Claude Code reads ANTHROPIC_API_KEY from the environment instead.
package claude

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
)

// Program finds the claude program: the configured path, or the one on PATH.
func Program(configured string) (string, error) {
	if configured != "" {
		if _, err := os.Stat(configured); err != nil {
			return "", fmt.Errorf("claude_path %s: %w", configured, err)
		}
		return configured, nil
	}
	path, err := exec.LookPath("claude")
	if err != nil {
		return "", errors.New("Claude Code is not installed: see https://docs.claude.com/en/docs/claude-code")
	}
	return path, nil
}

var versionPattern = regexp.MustCompile(`\d+\.\d+\.\d+[\w.-]*`)

// Version is the installed Claude Code version, such as "2.1.3".
func Version(ctx context.Context, program string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, program, "--version").Output()
	if err != nil {
		return "", fmt.Errorf("claude --version: %w", err)
	}
	if found := versionPattern.FindString(string(out)); found != "" {
		return found, nil
	}
	return strings.TrimSpace(string(out)), nil
}

// Update runs `claude update` and returns what it printed.
func Update(ctx context.Context, program string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, program, "update").CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

// MaxSummary is the longest result LBW keeps: room for a plan.
const MaxSummary = 20000

// Options are the flags one job runs with.
type Options struct {
	Program        string
	Dir            string
	Prompt         string
	PermissionMode string
	AllowedTools   []string
	// DisallowedTools are refused even when another rule would allow them.
	DisallowedTools []string
	MaxTurns        int
	Model           string
}

// Args are the command-line arguments for a headless, streamed run.
func (o Options) Args() []string {
	args := []string{"-p", o.Prompt, "--output-format", "stream-json", "--verbose"}
	if o.PermissionMode != "" {
		args = append(args, "--permission-mode", o.PermissionMode)
	}
	if len(o.AllowedTools) > 0 {
		args = append(args, "--allowedTools", strings.Join(o.AllowedTools, ","))
	}
	if len(o.DisallowedTools) > 0 {
		args = append(args, "--disallowedTools", strings.Join(o.DisallowedTools, ","))
	}
	if o.MaxTurns > 0 {
		args = append(args, "--max-turns", fmt.Sprint(o.MaxTurns))
	}
	if o.Model != "" {
		args = append(args, "--model", o.Model)
	}
	return args
}

// Outcome is how a run ended.
type Outcome struct {
	OK      bool
	Summary string
	Error   string
}

// Run starts Claude Code, sends each progress event to emit, and waits.
// Cancelling ctx stops the run.
func Run(ctx context.Context, opts Options, emit func(client.Event)) Outcome {
	cmd := exec.CommandContext(ctx, opts.Program, opts.Args()...)
	cmd.Dir = opts.Dir
	cmd.Cancel = func() error {
		if err := cmd.Process.Signal(os.Interrupt); err != nil {
			return cmd.Process.Kill()
		}
		return nil
	}
	cmd.WaitDelay = 15 * time.Second
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return Outcome{Error: err.Error()}
	}
	var stderr strings.Builder
	cmd.Stderr = &limitWriter{w: &stderr, left: 8 << 10}
	if err := cmd.Start(); err != nil {
		return Outcome{Error: fmt.Sprintf("could not start claude: %v", err)}
	}
	parser := &Parser{}
	parser.Read(stdout, emit)
	waitErr := cmd.Wait()
	outcome := parser.Outcome()
	switch {
	case ctx.Err() != nil:
		return Outcome{Error: "the job was cancelled", Summary: outcome.Summary}
	case waitErr != nil && outcome.Error == "":
		outcome.OK = false
		outcome.Error = strings.TrimSpace(fmt.Sprintf("claude exited: %v %s", waitErr, lastLine(stderr.String())))
	case !parser.sawResult && outcome.Error == "":
		outcome.OK = false
		outcome.Error = "claude ended without a result " + lastLine(stderr.String())
	}
	return outcome
}

// Parser turns Claude Code's stream-json lines into LBW events.
type Parser struct {
	model     string
	sawResult bool
	outcome   Outcome
}

type line struct {
	Type              string                `json:"type"`
	Subtype           string                `json:"subtype"`
	Model             string                `json:"model"`
	ClaudeCodeVersion string                `json:"claude_code_version"`
	Message           *message              `json:"message"`
	Result            string                `json:"result"`
	IsError           bool                  `json:"is_error"`
	DurationMS        int64                 `json:"duration_ms"`
	NumTurns          int                   `json:"num_turns"`
	Usage             *usage                `json:"usage"`
	ModelUsage        map[string]modelUsage `json:"modelUsage"`
}

type message struct {
	Model   string    `json:"model"`
	Content []content `json:"content"`
}

type content struct {
	Type  string          `json:"type"`
	Text  string          `json:"text"`
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
}

type usage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
}

type modelUsage struct {
	InputTokens              int64 `json:"inputTokens"`
	OutputTokens             int64 `json:"outputTokens"`
	CacheReadInputTokens     int64 `json:"cacheReadInputTokens"`
	CacheCreationInputTokens int64 `json:"cacheCreationInputTokens"`
}

// Read consumes the stream until it ends.
func (p *Parser) Read(r io.Reader, emit func(client.Event)) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64<<10), 16<<20)
	for scanner.Scan() {
		p.Line(scanner.Bytes(), emit)
	}
}

// Line handles one stream-json line. Lines that are not JSON are ignored.
func (p *Parser) Line(raw []byte, emit func(client.Event)) {
	var item line
	if json.Unmarshal(raw, &item) != nil {
		return
	}
	switch item.Type {
	case "system":
		if item.Subtype != "init" {
			return
		}
		p.model = item.Model
		emit(client.Event{Kind: "step", Message: strings.TrimSpace(fmt.Sprintf("Claude Code %s started on %s", item.ClaudeCodeVersion, item.Model))})
	case "assistant":
		if item.Message == nil {
			return
		}
		if item.Message.Model != "" {
			p.model = item.Message.Model
		}
		for _, part := range item.Message.Content {
			switch part.Type {
			case "text":
				if text := clip(part.Text, 400); text != "" {
					emit(client.Event{Kind: "step", Message: text})
				}
			case "tool_use":
				emit(client.Event{Kind: "step", Name: part.Name, Message: describeTool(part.Name, part.Input)})
			}
		}
	case "result":
		p.sawResult = true
		p.outcome.OK = !item.IsError && (item.Subtype == "" || item.Subtype == "success")
		p.outcome.Summary = clip(item.Result, MaxSummary)
		if !p.outcome.OK {
			p.outcome.Error = "Claude Code stopped: " + strings.TrimPrefix(item.Subtype, "error_")
			if item.Subtype == "" {
				p.outcome.Error = "Claude Code reported an error: " + clip(item.Result, 300)
			}
		}
		for _, event := range p.usageEvents(item) {
			emit(event)
		}
	}
}

// Outcome is the result line's verdict, once the stream ends.
func (p *Parser) Outcome() Outcome { return p.outcome }

func (p *Parser) usageEvents(item line) []client.Event {
	var events []client.Event
	if len(item.ModelUsage) > 0 {
		names := make([]string, 0, len(item.ModelUsage))
		for name := range item.ModelUsage {
			names = append(names, name)
		}
		sort.Strings(names)
		for i, name := range names {
			used := item.ModelUsage[name]
			event := tokens(name, used.InputTokens, used.OutputTokens, used.CacheReadInputTokens, used.CacheCreationInputTokens)
			if i == 0 {
				event.ElapsedMS = item.DurationMS
			}
			events = append(events, event)
		}
		return events
	}
	if item.Usage != nil {
		event := tokens(p.model, item.Usage.InputTokens, item.Usage.OutputTokens, item.Usage.CacheReadInputTokens, item.Usage.CacheCreationInputTokens)
		event.ElapsedMS = item.DurationMS
		events = append(events, event)
	}
	return events
}

// tokens follows LBW's convention: prompt tokens include cached input, which is
// also counted on its own so the price of a cache read can differ.
func tokens(model string, input, output, cacheRead, cacheWrite int64) client.Event {
	prompt := input + cacheRead + cacheWrite
	return client.Event{
		Kind:             "llm",
		ModelName:        model,
		PromptTokens:     prompt,
		CompletionTokens: output,
		TotalTokens:      prompt + output,
		CacheReadTokens:  cacheRead,
		CacheWriteTokens: cacheWrite,
	}
}

func describeTool(name string, raw json.RawMessage) string {
	var input map[string]any
	_ = json.Unmarshal(raw, &input)
	for _, key := range []string{"file_path", "path", "pattern", "command", "description"} {
		if value, ok := input[key].(string); ok && value != "" {
			return clip(name+" "+value, 200)
		}
	}
	return name
}

func clip(text string, limit int) string {
	text = strings.TrimSpace(text)
	if len(text) <= limit {
		return text
	}
	return strings.TrimSpace(text[:limit]) + "…"
}

func lastLine(text string) string {
	lines := strings.Split(strings.TrimSpace(text), "\n")
	return clip(lines[len(lines)-1], 300)
}

type limitWriter struct {
	w    io.Writer
	left int
}

func (l *limitWriter) Write(p []byte) (int, error) {
	if l.left <= 0 {
		return len(p), nil
	}
	n := len(p)
	if n > l.left {
		p = p[:l.left]
	}
	l.left -= len(p)
	_, err := l.w.Write(p)
	return n, err
}
