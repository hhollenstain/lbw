package tray

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/dashboard"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
)

func online() Snapshot {
	now := time.Now()
	ended := now.Add(-time.Minute)
	return Snapshot{
		Reachable: true,
		Status:    dashboard.Status{Connection: "online", Name: "henry-mbp", Server: "https://lbw.example", Relay: true},
		Work: monitor.State{
			Jobs: []monitor.Job{
				{ID: "j1", TaskID: "t1", Repository: "acme/web", Phase: "build", Goal: "Add a dark mode toggle\nKeep the colors", Started: now.Add(-4 * time.Minute), Status: "running"},
				{ID: "j0", TaskID: "t0", Phase: "plan", Goal: "Old job", Started: now.Add(-time.Hour), Status: "done"},
			},
			Calls: []monitor.Call{{ID: "c1", Started: now}, {ID: "c2", Started: ended, Ended: &ended, Status: 200}},
		},
		Inbox: dashboard.InboxState{
			Waiting: []client.InboxItem{
				{ID: "t7", Title: "Add rate limiting to the public API", WaitingFor: "Review the plan", Link: "/?change=t7"},
				{ID: "t9", Title: "Fix the flaky test", WaitingFor: "Approve the change", Link: "/?change=t9"},
			},
			Running: []client.InboxItem{
				{ID: "t1", Title: "Add a dark mode toggle", Status: "running", Link: "/?change=t1"},
				{ID: "t11", Title: "Upgrade the date library", Status: "running", Link: "/?change=t11"},
			},
		},
	}
}

func TestTheMenuShowsWhatWaitsAndWhatRuns(t *testing.T) {
	view := Build(online(), time.Now())
	if view.Status != "● Online · henry-mbp" || !view.Linked || !view.Reachable {
		t.Fatalf("status = %q", view.Status)
	}
	if view.WaitingHeader != "Waiting for you: 2" || view.Count != "2" || !view.Attention {
		t.Fatalf("waiting = %q count %q", view.WaitingHeader, view.Count)
	}
	if view.Waiting[0].Text != "Review the plan: Add rate limiting to the public API" || view.Waiting[0].Link != "https://lbw.example/?change=t7" {
		t.Fatalf("first waiting = %+v", view.Waiting[0])
	}
	// The job on this machine is listed once, with its step and time.
	if view.RunningHeader != "Running: 1 here · 1 in LBW" || len(view.Running) != 2 {
		t.Fatalf("running = %q %+v", view.RunningHeader, view.Running)
	}
	if view.Running[0].Text != "Add a dark mode toggle · build · 4m" || !strings.Contains(view.Running[0].Link, "change=t1") || !strings.Contains(view.Running[0].Link, "project=acme%2Fweb") {
		t.Fatalf("local job = %+v", view.Running[0])
	}
	if view.Running[1].Text != "Upgrade the date library · in LBW" {
		t.Fatalf("LBW job = %+v", view.Running[1])
	}
	if view.Calls != "Local model calls: 1 now · 1 recent" {
		t.Fatalf("calls = %q", view.Calls)
	}
	if view.Tooltip != "LBW Runner · Online · henry-mbp · 2 waiting for you · 2 running" {
		t.Fatalf("tooltip = %q", view.Tooltip)
	}
}

func TestTheMenuSaysWhenTheRunnerIsStoppedPausedOrUnlinked(t *testing.T) {
	if view := Build(Snapshot{}, time.Now()); view.Reachable || view.Status != "Runner is not running" || view.Attention {
		t.Fatalf("stopped = %+v", view)
	}
	paused := online()
	paused.Work.Paused = true
	paused.Inbox = dashboard.InboxState{}
	paused.Work.Jobs = nil
	view := Build(paused, time.Now())
	if view.Status != "● Paused: no new work · henry-mbp" || !view.Paused || view.Count != "" || view.Attention {
		t.Fatalf("paused = %+v", view)
	}
	if view.WaitingHeader != "Nothing is waiting for you" || view.RunningHeader != "Nothing of yours is running" {
		t.Fatalf("empty = %q %q", view.WaitingHeader, view.RunningHeader)
	}
	unlinked := Snapshot{Reachable: true, Status: dashboard.Status{Connection: "not_linked"}}
	view = Build(unlinked, time.Now())
	if view.Linked || view.WaitingHeader != "" || view.Calls != "" || !strings.HasPrefix(view.Status, "Not linked") {
		t.Fatalf("unlinked = %+v", view)
	}
}

func TestTheMenuListsAtMostFiveOfEach(t *testing.T) {
	snap := online()
	for i := 0; i < 8; i++ {
		snap.Inbox.Waiting = append(snap.Inbox.Waiting, client.InboxItem{ID: "w" + strconv.Itoa(i), Title: strings.Repeat("long title ", 20), Link: "/"})
	}
	view := Build(snap, time.Now())
	if len(view.Waiting) != Slots || view.WaitingHeader != "Waiting for you: 10" {
		t.Fatalf("waiting = %d %q", len(view.Waiting), view.WaitingHeader)
	}
	if text := view.Waiting[4].Text; len([]rune(text)) > 60 || !strings.HasSuffix(text, "…") {
		t.Fatalf("not clipped: %q", text)
	}
}

func TestTheTrayReadsTheDashboardWithTheKey(t *testing.T) {
	var paused []bool
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie("lbw_runner")
		if err != nil || cookie.Value != "k" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/state":
			if r.URL.Query().Get("from") != "tray" {
				t.Errorf("the tray did not say it was the tray")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"status": map[string]any{"connection": "online", "name": "box"}})
		case "/api/pause":
			var body struct {
				Paused bool `json:"paused"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			paused = append(paused, body.Paused)
		}
	}))
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server.Listener = listener
	server.Start()
	defer server.Close()
	_, portText, _ := net.SplitHostPort(listener.Addr().String())
	port, _ := strconv.Atoi(portText)

	if snap := Fetch(context.Background(), port, "k"); !snap.Reachable || snap.Status.Name != "box" {
		t.Fatalf("snap = %+v", snap)
	}
	if snap := Fetch(context.Background(), port, "wrong"); snap.Reachable {
		t.Fatal("a wrong key read the state")
	}
	if err := SetPaused(context.Background(), port, "k", true); err != nil || len(paused) != 1 || !paused[0] {
		t.Fatalf("pause: %v %v", err, paused)
	}
}

func TestTheMenuOffersAPublishedUpdate(t *testing.T) {
	snap := online()
	if view := Build(snap, time.Now()); view.Update != "" {
		t.Fatalf("no update yet, but the menu says %q", view.Update)
	}
	snap.Status.Runner = dashboard.RunnerUpdate{Latest: "0.3.0", Available: true}
	if view := Build(snap, time.Now()); view.Update != "Install update 0.3.0" || !view.CanUpdate {
		t.Fatalf("update = %q", view.Update)
	}
	snap.Status.Runner.Installing = true
	if view := Build(snap, time.Now()); view.Update != "Installing the update…" || view.CanUpdate {
		t.Fatalf("installing = %q", view.Update)
	}
}
