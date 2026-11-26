// Package tray is the runner's icon in the menu bar (macOS) or the system tray
// (Windows, Linux). It runs as its own process, `lbw-runner tray`, and reads
// the runner's dashboard on 127.0.0.1 with the dashboard key, so a problem in
// the tray never touches a running job.
//
// This file turns the dashboard's state into what the menu shows. It does not
// import the tray library, so it is tested on every platform.
package tray

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/dashboard"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
)

// Slots is how many waiting and running jobs the menu lists by name.
const Slots = 5

// Snapshot is the dashboard's state, or that the runner did not answer.
type Snapshot struct {
	Reachable bool
	Status    dashboard.Status     `json:"status"`
	Work      monitor.State        `json:"work"`
	Inbox     dashboard.InboxState `json:"inbox"`
}

// Line is one menu entry. Link is what a click opens; empty means it only informs.
type Line struct {
	Text string
	Link string
}

// View is the whole menu, and the icon's state.
type View struct {
	Status        string
	Tooltip       string
	Count         string
	Attention     bool
	WaitingHeader string
	Waiting       []Line
	RunningHeader string
	Running       []Line
	Calls         string
	Linked        bool
	Reachable     bool
	Paused        bool
	// Update is the menu entry for a newer runner, or empty when there is none.
	Update    string
	CanUpdate bool
}

// Build lays out the menu from a snapshot.
func Build(snap Snapshot, now time.Time) View {
	if !snap.Reachable {
		return View{
			Status:  "Runner is not running",
			Tooltip: "LBW Runner is not running",
		}
	}
	status := snap.Status
	view := View{Reachable: true, Paused: snap.Work.Paused}
	switch status.Connection {
	case "online":
		view.Linked = true
		view.Status = "Online"
		if snap.Work.Paused {
			view.Status = "Paused: no new work"
		}
	case "connecting":
		view.Linked = true
		view.Status = "Connecting to LBW…"
	case "disconnected":
		view.Status = "Disconnected: link it again"
	default:
		view.Status = "Not linked: open the dashboard to link"
	}
	if status.Name != "" && view.Linked {
		view.Status = "● " + view.Status + " · " + status.Name
	}

	waiting := snap.Inbox.Waiting
	view.Attention = len(waiting) > 0
	switch {
	case !view.Linked:
	case len(waiting) == 0:
		view.WaitingHeader = "Nothing is waiting for you"
	default:
		view.WaitingHeader = fmt.Sprintf("Waiting for you: %d", len(waiting))
		view.Count = strconv.Itoa(len(waiting))
	}
	for _, item := range waiting {
		if len(view.Waiting) == Slots {
			break
		}
		view.Waiting = append(view.Waiting, Line{
			Text: clip(firstNonEmpty(item.WaitingFor, "Your turn")+": "+item.Title, 60),
			Link: lbwLink(status.Server, item.Link),
		})
	}

	here := map[string]bool{}
	for _, job := range snap.Work.Jobs {
		if job.Status != "running" {
			continue
		}
		here[job.TaskID] = true
		if len(view.Running) < Slots {
			view.Running = append(view.Running, Line{
				Text: clip(firstLine(job.Goal), 44) + " · " + job.Phase + " · " + elapsed(job.Started, now),
				Link: changeLink(status.Server, job.TaskID, firstNonEmpty(job.Repository, job.Project)),
			})
		}
	}
	elsewhere := 0
	for _, item := range snap.Inbox.Running {
		if here[item.ID] {
			continue
		}
		elsewhere++
		if len(view.Running) < Slots {
			view.Running = append(view.Running, Line{
				Text: clip(item.Title, 44) + " · in LBW",
				Link: lbwLink(status.Server, item.Link),
			})
		}
	}
	switch mine := len(here) + elsewhere; {
	case !view.Linked:
	case mine == 0:
		view.RunningHeader = "Nothing of yours is running"
	default:
		view.RunningHeader = fmt.Sprintf("Running: %d here · %d in LBW", len(here), elsewhere)
	}

	inFlight, recent := 0, 0
	for _, call := range snap.Work.Calls {
		if call.Ended == nil {
			inFlight++
		} else {
			recent++
		}
	}
	if status.Relay && view.Linked {
		view.Calls = fmt.Sprintf("Local model calls: %d now · %d recent", inFlight, recent)
	}

	switch update := status.Runner; {
	case update.Installing:
		view.Update = "Installing the update…"
	case update.Available && update.Latest != "":
		view.Update = "Install update " + update.Latest
		view.CanUpdate = true
	}

	tip := []string{"LBW Runner", strings.TrimPrefix(view.Status, "● ")}
	if len(waiting) > 0 {
		tip = append(tip, fmt.Sprintf("%d waiting for you", len(waiting)))
	}
	if n := len(here) + elsewhere; n > 0 {
		tip = append(tip, fmt.Sprintf("%d running", n))
	}
	view.Tooltip = strings.Join(tip, " · ")
	return view
}

// Fetch reads the dashboard's state as the tray. A runner that does not
// answer is a snapshot with Reachable false, not an error.
func Fetch(ctx context.Context, port int, key string) Snapshot {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, dashboard.Address(port)+"/api/state?from=tray", nil)
	if err != nil {
		return Snapshot{}
	}
	req.AddCookie(&http.Cookie{Name: "lbw_runner", Value: key})
	resp, err := localClient.Do(req)
	if err != nil {
		return Snapshot{}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Snapshot{}
	}
	var snap Snapshot
	if json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&snap) != nil {
		return Snapshot{}
	}
	snap.Reachable = true
	return snap
}

// SetPaused pauses or resumes new work through the dashboard.
func SetPaused(ctx context.Context, port int, key string, paused bool) error {
	return post(ctx, port, key, "/api/pause", fmt.Sprintf(`{"paused":%t}`, paused))
}

// InstallUpdate asks the runner to install the newer release and restart.
func InstallUpdate(ctx context.Context, port int, key string) error {
	return post(ctx, port, key, "/api/update", "{}")
}

func post(ctx context.Context, port int, key, path, payload string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, dashboard.Address(port)+path, strings.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: "lbw_runner", Value: key})
	resp, err := localClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("the runner answered %d", resp.StatusCode)
	}
	return nil
}

// Installing an update downloads it, so the client waits long enough for that.
var localClient = &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{Proxy: nil}}

// lbwLink makes an inbox item's path into an address on the LBW server.
func lbwLink(server, path string) string {
	if server == "" || !strings.HasPrefix(path, "/") {
		return ""
	}
	return strings.TrimRight(server, "/") + path
}

// changeLink opens a job's change in LBW, as the dashboard's "Open in LBW" does.
func changeLink(server, taskID, project string) string {
	if server == "" || taskID == "" {
		return ""
	}
	params := url.Values{"section": {"changes"}, "change": {taskID}}
	if project != "" {
		params.Set("project", project)
	}
	return strings.TrimRight(server, "/") + "/?" + params.Encode()
}

func elapsed(started time.Time, now time.Time) string {
	d := now.Sub(started).Round(time.Second)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	return fmt.Sprintf("%dh %dm", int(d.Hours()), int(d.Minutes())%60)
}

func firstLine(text string) string {
	for _, line := range strings.Split(text, "\n") {
		if strings.TrimSpace(line) != "" {
			return strings.TrimSpace(line)
		}
	}
	return "A job"
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

// clip shortens text for a menu, by characters rather than bytes.
func clip(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return strings.TrimSpace(string(runes[:limit-1])) + "…"
}
