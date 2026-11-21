package daemon

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/config"
	"github.com/hhollenstain/lbw/runner/internal/job"
)

// fakeLBW plays the server side: one job, then nothing.
type fakeLBW struct {
	mu       sync.Mutex
	job      *client.Job
	events   []client.Event
	result   *client.Result
	commands []string
	beats    int
	revoked  bool
}

func (f *fakeLBW) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Header.Get("Authorization") != "Bearer lbwr_test" || f.revoked {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	switch {
	case r.URL.Path == "/api/runner/heartbeat":
		f.beats++
		_ = json.NewEncoder(w).Encode(client.Beat{RunnerID: "r1", Name: "laptop", Commands: f.commands})
		f.commands = nil
	case r.URL.Path == "/api/runner/jobs/next":
		if f.job == nil {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		_ = json.NewEncoder(w).Encode(f.job)
		f.job = nil
	case strings.HasSuffix(r.URL.Path, "/events"):
		var body struct {
			Events []client.Event `json:"events"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.events = append(f.events, body.Events...)
		_, _ = io.WriteString(w, `{"cancel":false}`)
	case strings.HasSuffix(r.URL.Path, "/finish"):
		var result client.Result
		_ = json.NewDecoder(r.Body).Decode(&result)
		f.result = &result
		_, _ = io.WriteString(w, `{"ok":true}`)
	default:
		http.NotFound(w, r)
	}
}

func repo(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	run := func(args ...string) string {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=Ada", "GIT_AUTHOR_EMAIL=a@x", "GIT_COMMITTER_NAME=Ada", "GIT_COMMITTER_EMAIL=a@x")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v %s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	run("init", "-q", "-b", "main")
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", ".")
	run("commit", "-q", "-m", "start")
	return dir, run("rev-parse", "HEAD")
}

func TestTheDaemonRunsAJobAndReportsBack(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake claude is a shell script")
	}
	remote, base := repo(t)
	program, _ := filepath.Abs("../testdata/fake-claude.sh")
	lbw := &fakeLBW{
		job:      &client.Job{JobID: "job1", TaskID: "t1", Goal: "Add hello", BaseSHA: base, RemoteURL: remote},
		commands: []string{"update_claude"},
	}
	server := httptest.NewServer(lbw)
	defer server.Close()
	api, err := client.New(server.URL, "lbwr_test", "")
	if err != nil {
		t.Fatal(err)
	}
	d := &Daemon{
		API:     api,
		Jobs:    &job.Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: program},
		Version: "test",
		Timings: Timings{Heartbeat: 50 * time.Millisecond, Poll: 0, Flush: 20 * time.Millisecond, Retry: 10 * time.Millisecond},
		Log:     log.New(io.Discard, "", 0),
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go func() {
		for ctx.Err() == nil {
			lbw.mu.Lock()
			done := lbw.result != nil
			lbw.mu.Unlock()
			if done {
				cancel()
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	if err := d.Run(ctx); err != nil {
		t.Fatal(err)
	}
	lbw.mu.Lock()
	defer lbw.mu.Unlock()
	if lbw.result == nil || lbw.result.Status != "done" || !strings.Contains(lbw.result.Patch, "hello from claude") {
		t.Fatalf("result = %+v", lbw.result)
	}
	var sawUsage bool
	for _, event := range lbw.events {
		if event.Kind == "llm" && event.PromptTokens == 1300 && event.ModelName == "claude-sonnet-4-5" {
			sawUsage = true
		}
	}
	if !sawUsage {
		t.Fatalf("no usage event in %+v", lbw.events)
	}
	if lbw.beats < 1 {
		t.Fatal("no heartbeat")
	}
}

func TestADisconnectedRunnerStops(t *testing.T) {
	lbw := &fakeLBW{revoked: true}
	server := httptest.NewServer(lbw)
	defer server.Close()
	api, _ := client.New(server.URL, "lbwr_test", "")
	d := &Daemon{API: api, Jobs: &job.Runner{Config: &config.Config{}, Claude: "/nonexistent"}, Log: log.New(io.Discard, "", 0)}
	if err := d.Run(context.Background()); err != client.ErrUnauthorized {
		t.Fatalf("err = %v", err)
	}
}
