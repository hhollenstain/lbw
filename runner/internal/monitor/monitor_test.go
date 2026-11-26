package monitor

import (
	"fmt"
	"strings"
	"testing"

	"github.com/hhollenstain/lbw/runner/internal/client"
)

func TestAJobMovesFromRunningToRecentWithItsProgress(t *testing.T) {
	m := New()
	stopped := false
	m.JobStarted(&client.Job{JobID: "j1", TaskID: "t1", Repository: "acme/demo", Goal: "Add a page"}, func() { stopped = true })
	m.JobEvent("j1", client.Event{Kind: "step", Message: "Working in a fresh worktree"})
	m.JobEvent("j1", client.Event{Kind: "tokens", TotalTokens: 1200})
	m.JobEvent("j1", client.Event{Kind: "tokens", PromptTokens: 100, CompletionTokens: 50})

	state := m.Snapshot()
	if len(state.Jobs) != 1 || state.Jobs[0].Status != "running" || state.Jobs[0].Phase != "build" {
		t.Fatalf("jobs = %+v", state.Jobs)
	}
	if state.Jobs[0].Tokens != 1350 || len(state.Jobs[0].Events) != 1 {
		t.Fatalf("job = %+v", state.Jobs[0])
	}

	if !m.StopJob("j1") || !stopped || !m.Stopped("j1") {
		t.Fatalf("stop did not cancel the job")
	}
	m.JobFinished("j1", client.Result{Status: "failed", Error: "stopped"})
	state = m.Snapshot()
	if state.Jobs[0].Status != "failed" || state.Jobs[0].Ended == nil || m.StopJob("j1") {
		t.Fatalf("finished job = %+v", state.Jobs[0])
	}
}

func TestProgressAndHistoryStayBounded(t *testing.T) {
	m := New()
	for i := 0; i < recentJobs+5; i++ {
		id := fmt.Sprintf("j%d", i)
		m.JobStarted(&client.Job{JobID: id}, nil)
		for e := 0; e < jobEvents+10; e++ {
			m.JobEvent(id, client.Event{Message: "line"})
		}
		if i == 0 {
			if got := len(m.Snapshot().Jobs[0].Events); got != jobEvents {
				t.Fatalf("events = %d", got)
			}
		}
		m.JobFinished(id, client.Result{Status: "done"})
	}
	if got := len(m.Snapshot().Jobs); got != recentJobs {
		t.Fatalf("recent jobs = %d", got)
	}
	m.JobStarted(&client.Job{JobID: "long", Goal: strings.Repeat("é", goalPreview)}, nil)
	goal := m.Snapshot().Jobs[0].Goal
	if !strings.HasSuffix(goal, "…") || !strings.HasPrefix(goal, "é") || strings.ContainsRune(goal, '�') {
		t.Fatalf("goal was not clipped on a character: %q", goal[len(goal)-8:])
	}
}

func TestModelCallsKeepTheModelNameButNotThePrompt(t *testing.T) {
	m := New()
	m.CallStarted(&client.ModelCall{CallID: "c1", Path: "/v1/chat/completions", Body: `{"model":"qwen2.5-coder:7b","messages":[{"content":"secret plan"}]}`})
	state := m.Snapshot()
	if len(state.Calls) != 1 || state.Calls[0].Model != "qwen2.5-coder:7b" || state.Calls[0].Ended != nil {
		t.Fatalf("calls = %+v", state.Calls)
	}
	if strings.Contains(fmt.Sprintf("%+v", state), "secret plan") {
		t.Fatal("the prompt was kept")
	}
	m.CallFinished("c1", client.ModelAnswer{Status: 200})
	if call := m.Snapshot().Calls[0]; call.Status != 200 || call.Ended == nil {
		t.Fatalf("call = %+v", call)
	}
}

func TestPausingWakesAWaitingPoll(t *testing.T) {
	m := New()
	woke := 0
	forget := m.OnPause(func() { woke++ })
	m.SetPaused(true)
	m.SetPaused(true)
	m.SetPaused(false)
	if woke != 1 || m.Paused() {
		t.Fatalf("woke = %d paused = %v", woke, m.Paused())
	}
	forget()
	m.SetPaused(true)
	if woke != 1 {
		t.Fatalf("a forgotten hook ran")
	}
}

func TestTheLogKeepsWholeLines(t *testing.T) {
	m := New()
	_, _ = m.Write([]byte("first\nsec"))
	_, _ = m.Write([]byte("ond\n"))
	if got := m.Snapshot().Log; len(got) != 2 || got[1] != "second" {
		t.Fatalf("log = %q", got)
	}
	var nilMonitor *Monitor
	nilMonitor.JobStarted(&client.Job{JobID: "x"}, nil)
	if nilMonitor.Paused() {
		t.Fatal("a nil monitor is never paused")
	}
}
