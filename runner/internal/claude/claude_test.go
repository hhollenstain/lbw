package claude

import (
	"context"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/hhollenstain/lbw/runner/internal/client"
)

func fake(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the fake claude is a shell script")
	}
	path, err := filepath.Abs("../testdata/fake-claude.sh")
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func TestParserCountsTokensTheWayLBWDoes(t *testing.T) {
	var events []client.Event
	p := &Parser{}
	p.Read(strings.NewReader(strings.Join([]string{
		`{"type":"system","subtype":"init","model":"claude-opus-4-1","claude_code_version":"2.1.0"}`,
		`{"type":"result","subtype":"success","result":"done","duration_ms":10,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":20}}`,
	}, "\n")), func(e client.Event) { events = append(events, e) })
	if len(events) != 2 {
		t.Fatalf("events = %+v", events)
	}
	usage := events[1]
	if usage.Kind != "llm" || usage.ModelName != "claude-opus-4-1" {
		t.Fatalf("usage event = %+v", usage)
	}
	if usage.PromptTokens != 130 || usage.CompletionTokens != 5 || usage.TotalTokens != 135 {
		t.Fatalf("tokens = %+v", usage)
	}
	if usage.CacheReadTokens != 100 || usage.CacheWriteTokens != 20 || usage.ElapsedMS != 10 {
		t.Fatalf("cache = %+v", usage)
	}
	if got := p.Outcome(); !got.OK || got.Summary != "done" {
		t.Fatalf("outcome = %+v", got)
	}
}

func TestAnErrorResultFailsTheRun(t *testing.T) {
	p := &Parser{}
	p.Line([]byte(`{"type":"result","subtype":"error_max_turns","is_error":true}`), func(client.Event) {})
	if got := p.Outcome(); got.OK || !strings.Contains(got.Error, "max_turns") {
		t.Fatalf("outcome = %+v", got)
	}
}

func TestRunStreamsEventsFromClaudeCode(t *testing.T) {
	program := fake(t)
	dir := t.TempDir()
	var events []client.Event
	got := Run(context.Background(), Options{Program: program, Dir: dir, Prompt: "add a file"}, func(e client.Event) {
		events = append(events, e)
	})
	if !got.OK || got.Summary != "Added hello.txt." {
		t.Fatalf("outcome = %+v", got)
	}
	var kinds []string
	for _, e := range events {
		kinds = append(kinds, e.Kind+":"+e.Message)
	}
	joined := strings.Join(kinds, "|")
	for _, want := range []string{"Claude Code 2.1.0 started on claude-sonnet-4-5", "Write hello.txt", "llm:"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in %s", want, joined)
		}
	}
	if v, err := Version(context.Background(), program); err != nil || v != "2.1.0" {
		t.Fatalf("version = %q %v", v, err)
	}
}

func TestArgsAreHeadlessAndStreamed(t *testing.T) {
	args := strings.Join(Options{Prompt: "x", PermissionMode: "acceptEdits", AllowedTools: []string{"Read", "Edit"}, MaxTurns: 3}.Args(), " ")
	want := "-p x --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools Read,Edit --max-turns 3"
	if args != want {
		t.Fatalf("args = %s", args)
	}
}
