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
	API     *client.Client
	Jobs    *job.Runner
	Version string
	Timings Timings
	Log     *log.Logger

	mu    sync.Mutex
	facts client.Facts
}

// Facts reports this machine's runner and Claude Code versions.
func (d *Daemon) Facts(ctx context.Context) client.Facts {
	facts := client.Facts{Version: d.Version, Platform: runtime.GOOS + "/" + runtime.GOARCH}
	if version, err := claude.Version(ctx, d.Jobs.Claude); err == nil {
		facts.ClaudeVersion = version
		facts.ClaudeReady = true
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
	for ctx.Err() == nil {
		select {
		case err := <-beats:
			return err
		default:
		}
		next, err := d.API.NextJob(ctx, d.Timings.Poll)
		if errors.Is(err, client.ErrUnauthorized) {
			return err
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
	var mu sync.Mutex
	var pending []client.Event
	emit := func(event client.Event) {
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
