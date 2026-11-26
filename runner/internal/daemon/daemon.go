// Package daemon is the runner's main loop: stay in touch with LBW, take one
// job at a time, stream its progress, and do maintenance LBW asks for.
package daemon

import (
	"context"
	"errors"
	"fmt"
	"log"
	"runtime"
	"sync"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/claude"
	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/job"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
	"github.com/hhollenstain/lbw/runner/internal/relay"
)

// Timings are the loop's intervals. Tests shorten them.
type Timings struct {
	Heartbeat time.Duration
	Poll      time.Duration
	Flush     time.Duration
	Retry     time.Duration
}

// DefaultTimings keep a job's lease (two minutes on the server) well alive.
var DefaultTimings = Timings{
	Heartbeat: 30 * time.Second,
	Poll:      25 * time.Second,
	Flush:     3 * time.Second,
	Retry:     10 * time.Second,
}

// Daemon runs until its context ends or the runner is disconnected.
type Daemon struct {
	API *client.Client
	// Jobs runs Claude Code jobs. Nil when Claude Code is not installed: the
	// runner then only relays model calls.
	Jobs *job.Runner
	// Relay passes model calls to the owner's own model server. Nil when off.
	Relay   *relay.Relay
	Version string
	Timings Timings
	Log     *log.Logger
	// Monitor shows this runner's work on its dashboard, and can pause it or
	// stop a job. Nil runs without a dashboard.
	Monitor *monitor.Monitor
	// OnBeat hears each heartbeat: LBW's answer, or why there was none.
	OnBeat func(beat *client.Beat, err error)

	mu    sync.Mutex
	facts client.Facts
}

// Facts reports this machine's runner and Claude Code versions.
func (d *Daemon) Facts(ctx context.Context) client.Facts {
	facts := client.Facts{
		Version:  d.Version,
		Platform: runtime.GOOS + "/" + runtime.GOARCH,
		LLMRelay: d.Relay != nil,
	}
	if d.Jobs != nil {
		if version, err := claude.Version(ctx, d.Jobs.Claude); err == nil {
			facts.ClaudeVersion = version
			facts.ClaudeReady = true
		}
	}
	d.mu.Lock()
	d.facts = facts
	d.mu.Unlock()
	return facts
}

// Run loops until ctx ends. It returns client.ErrUnauthorized when LBW
// disconnected this runner, so the service stops instead of retrying forever.
func (d *Daemon) Run(ctx context.Context) error {
	if d.Timings == (Timings{}) {
		d.Timings = DefaultTimings
	}
	d.Facts(ctx)
	if err := d.beat(ctx); err != nil {
		return err
	}
	beats := make(chan error, 1)
	go d.heartbeats(ctx, beats)
	if d.Relay != nil {
		go func() {
			if err := d.Relay.Run(ctx); err != nil {
				select {
				case beats <- err:
				default:
				}
			}
		}()
	}
	if d.Jobs == nil {
		select {
		case err := <-beats:
			return err
		case <-ctx.Done():
			return nil
		}
	}
	for ctx.Err() == nil {
		select {
		case err := <-beats:
			return err
		default:
		}
		if d.Monitor.Paused() {
			sleep(ctx, pausedWait)
			continue
		}
		next, err := d.nextJob(ctx)
		if errors.Is(err, client.ErrUnauthorized) {
			return err
		}
		if errors.Is(err, errPaused) {
			continue
		}
		if err != nil {
			if ctx.Err() == nil {
				d.Log.Printf("could not reach LBW: %v", err)
				sleep(ctx, d.Timings.Retry)
			}
			continue
		}
		if next == nil {
			continue
		}
		d.work(ctx, next)
	}
	return nil
}

// pausedWait is how often a paused runner checks whether it was resumed.
const pausedWait = time.Second

var errPaused = errors.New("paused")

// nextJob waits for a job, and stops waiting as soon as the owner pauses the
// runner, so it does not take one more job after the pause.
func (d *Daemon) nextJob(ctx context.Context) (*client.Job, error) {
	poll, cancel := context.WithCancel(ctx)
	defer cancel()
	forget := d.Monitor.OnPause(cancel)
	defer forget()
	next, err := d.API.NextJob(poll, d.Timings.Poll)
	if poll.Err() != nil && ctx.Err() == nil {
		// A job that arrived as the pause landed is already leased to this
		// runner, so it runs rather than waiting out its lease.
		if next != nil {
			return next, nil
		}
		return nil, errPaused
	}
	return next, err
}

func (d *Daemon) heartbeats(ctx context.Context, failed chan<- error) {
	ticker := time.NewTicker(d.Timings.Heartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := d.beat(ctx); errors.Is(err, client.ErrUnauthorized) {
				failed <- err
				return
			}
		}
	}
}

func (d *Daemon) beat(ctx context.Context) error {
	d.mu.Lock()
	facts := d.facts
	d.mu.Unlock()
	answer, err := d.API.Heartbeat(ctx, facts)
	if d.OnBeat != nil {
		d.OnBeat(answer, err)
	}
	if err != nil {
		if !errors.Is(err, client.ErrUnauthorized) {
			d.Log.Printf("heartbeat failed: %v", err)
			return nil
		}
		return err
	}
	for _, command := range answer.Commands {
		d.maintain(ctx, command)
	}
	return nil
}

func (d *Daemon) maintain(ctx context.Context, command string) {
	switch command {
	case "update_claude":
		if d.Jobs == nil {
			d.Log.Printf("Claude Code is not installed, so it cannot be updated")
			return
		}
		d.Log.Printf("updating Claude Code")
		out, err := claude.Update(ctx, d.Jobs.Claude)
		if err != nil {
			d.Log.Printf("claude update failed: %v %s", err, out)
		} else {
			d.Log.Printf("claude update: %s", out)
		}
		_, _ = d.API.Heartbeat(ctx, d.Facts(ctx))
	default:
		d.Log.Printf("ignoring unknown command %q", command)
	}
}

// work runs one job. Events are sent in batches, and an empty batch still
// goes out on each flush so LBW knows the runner is alive.
func (d *Daemon) work(parent context.Context, next *client.Job) {
	d.Log.Printf("job %s: %s", next.JobID, firstLine(next.Goal))
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	d.Monitor.JobStarted(next, cancel)
	var mu sync.Mutex
	var pending []client.Event
	emit := func(event client.Event) {
		d.Monitor.JobEvent(next.JobID, event)
		mu.Lock()
		pending = append(pending, event)
		mu.Unlock()
	}
	flush := func(send context.Context) {
		mu.Lock()
		batch := pending
		pending = nil
		mu.Unlock()
		stop, err := d.API.SendEvents(send, next.JobID, batch)
		if err != nil {
			d.Log.Printf("job %s: could not send progress: %v", next.JobID, err)
			mu.Lock()
			pending = append(batch, pending...)
			mu.Unlock()
			return
		}
		if stop {
			d.Log.Printf("job %s: LBW cancelled it", next.JobID)
			cancel()
		}
	}
	done := make(chan client.Result, 1)
	go func() { done <- d.Jobs.Run(ctx, next, emit) }()
	ticker := time.NewTicker(d.Timings.Flush)
	defer ticker.Stop()
	for {
		select {
		case result := <-done:
			if d.Monitor.Stopped(next.JobID) {
				result = client.Result{Status: "failed", Summary: result.Summary, Error: StoppedByOwner}
				emit(client.Event{Kind: "step", Message: StoppedByOwner})
			}
			d.Monitor.JobFinished(next.JobID, result)
			flush(parent)
			if err := d.API.Finish(parent, next.JobID, result); err != nil {
				d.Log.Printf("job %s: could not report the result: %v", next.JobID, err)
			}
			d.Log.Printf("job %s: %s %s", next.JobID, result.Status, result.Error)
			return
		case <-ticker.C:
			flush(ctx)
		}
	}
}

// StoppedByOwner is the error a job reports when its owner stopped it on this machine.
const StoppedByOwner = "Stopped on the runner by its owner"

func firstLine(text string) string {
	for i, r := range text {
		if r == '\n' {
			return text[:i]
		}
	}
	if len(text) > 80 {
		return fmt.Sprintf("%s…", text[:80])
	}
	return text
}

func sleep(ctx context.Context, wait time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(wait):
	}
}
