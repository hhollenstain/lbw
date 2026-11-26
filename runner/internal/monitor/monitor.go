// Package monitor keeps what the runner is doing right now, for the dashboard:
// the jobs it runs, the model calls it relays, and its recent log lines. It is
// memory only. Nothing here is written to disk or sent to LBW.
package monitor

import (
	"bytes"
	"encoding/json"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/hhollenstain/lbw/runner/internal/client"
)

// Limits keep the dashboard's memory small however long the runner runs.
const (
	recentJobs   = 20
	recentCalls  = 40
	jobEvents    = 200
	logLines     = 400
	goalPreview  = 400
	eventPreview = 400
)

// Event is one progress line of a job, as the dashboard shows it.
type Event struct {
	At      time.Time `json:"at"`
	Kind    string    `json:"kind"`
	Message string    `json:"message"`
}

// Job is one Claude Code job on this machine.
type Job struct {
	ID         string     `json:"id"`
	TaskID     string     `json:"task_id"`
	Project    string     `json:"project"`
	Repository string     `json:"repository"`
	Phase      string     `json:"phase"`
	Model      string     `json:"model"`
	Goal       string     `json:"goal"`
	Started    time.Time  `json:"started"`
	Ended      *time.Time `json:"ended,omitempty"`
	Status     string     `json:"status"`
	Error      string     `json:"error,omitempty"`
	Stopping   bool       `json:"stopping,omitempty"`
	Tokens     int64      `json:"tokens"`
	Events     []Event    `json:"events"`

	stop func()
}

// Call is one model call relayed to the owner's model server.
type Call struct {
	ID      string     `json:"id"`
	Path    string     `json:"path"`
	Model   string     `json:"model"`
	Started time.Time  `json:"started"`
	Ended   *time.Time `json:"ended,omitempty"`
	Status  int        `json:"status,omitempty"`
	Error   string     `json:"error,omitempty"`
}

// State is a copy of everything the dashboard shows.
type State struct {
	Started time.Time `json:"started"`
	Paused  bool      `json:"paused"`
	Jobs    []Job     `json:"jobs"`
	Calls   []Call    `json:"calls"`
	Log     []string  `json:"log"`
}

// Monitor is safe to use from the daemon, the relay, and the dashboard at once.
type Monitor struct {
	mu      sync.Mutex
	started time.Time
	paused  bool
	onPause []func()
	active  map[string]*Job
	order   []string
	recent  []Job
	calls   map[string]*Call
	done    []Call
	lines   []string
	partial []byte
}

// New starts an empty monitor.
func New() *Monitor {
	return &Monitor{started: time.Now(), active: map[string]*Job{}, calls: map[string]*Call{}}
}

// JobStarted records a leased job. stop cancels it.
func (m *Monitor) JobStarted(job *client.Job, stop func()) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	phase := job.Phase
	if phase == "" {
		phase = "build"
	}
	m.active[job.JobID] = &Job{
		ID:         job.JobID,
		TaskID:     job.TaskID,
		Project:    job.Project,
		Repository: job.Repository,
		Phase:      phase,
		Model:      job.Model,
		Goal:       clip(job.Goal, goalPreview),
		Started:    time.Now(),
		Status:     "running",
		Events:     []Event{},
		stop:       stop,
	}
	m.order = append(m.order, job.JobID)
}

// JobEvent adds a job's progress line, and counts its tokens.
func (m *Monitor) JobEvent(jobID string, event client.Event) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	job := m.active[jobID]
	if job == nil {
		return
	}
	if event.TotalTokens > 0 {
		job.Tokens += event.TotalTokens
	} else {
		job.Tokens += event.PromptTokens + event.CompletionTokens
	}
	text := strings.TrimSpace(event.Message)
	if text == "" {
		return
	}
	job.Events = append(job.Events, Event{At: time.Now(), Kind: event.Kind, Message: clip(text, eventPreview)})
	if len(job.Events) > jobEvents {
		job.Events = job.Events[len(job.Events)-jobEvents:]
	}
}

// JobFinished moves a job to the recent list.
func (m *Monitor) JobFinished(jobID string, result client.Result) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	job := m.active[jobID]
	if job == nil {
		return
	}
	delete(m.active, jobID)
	m.order = without(m.order, jobID)
	now := time.Now()
	job.Ended = &now
	job.Status = result.Status
	job.Error = clip(result.Error, eventPreview)
	job.stop = nil
	m.recent = append([]Job{*job}, m.recent...)
	if len(m.recent) > recentJobs {
		m.recent = m.recent[:recentJobs]
	}
}

// StopJob cancels a running job. It reports false when no such job runs.
func (m *Monitor) StopJob(jobID string) bool {
	if m == nil {
		return false
	}
	m.mu.Lock()
	job := m.active[jobID]
	var stop func()
	if job != nil && !job.Stopping && job.stop != nil {
		job.Stopping = true
		stop = job.stop
	}
	m.mu.Unlock()
	if stop == nil {
		return job != nil
	}
	stop()
	return true
}

// Stopped says whether the owner stopped this job from the dashboard.
func (m *Monitor) Stopped(jobID string) bool {
	if m == nil {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	job := m.active[jobID]
	return job != nil && job.Stopping
}

// CallStarted records a model call the relay took.
func (m *Monitor) CallStarted(call *client.ModelCall) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.calls[call.CallID] = &Call{ID: call.CallID, Path: call.Path, Model: modelOf(call.Body), Started: time.Now()}
}

// CallFinished moves a model call to the recent list.
func (m *Monitor) CallFinished(callID string, answer client.ModelAnswer) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	call := m.calls[callID]
	if call == nil {
		return
	}
	delete(m.calls, callID)
	now := time.Now()
	call.Ended = &now
	call.Status = answer.Status
	call.Error = clip(answer.Error, eventPreview)
	m.done = append([]Call{*call}, m.done...)
	if len(m.done) > recentCalls {
		m.done = m.done[:recentCalls]
	}
}

// Paused says whether the runner holds off taking new jobs and model calls.
func (m *Monitor) Paused() bool {
	if m == nil {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.paused
}

// SetPaused pauses or resumes taking new work. Work already running goes on.
func (m *Monitor) SetPaused(paused bool) {
	if m == nil {
		return
	}
	m.mu.Lock()
	changed := m.paused != paused
	m.paused = paused
	var wake []func()
	if changed && paused {
		wake = append(wake, m.onPause...)
	}
	m.mu.Unlock()
	for _, fn := range wake {
		fn()
	}
}

// OnPause calls fn whenever the runner is paused, so a waiting poll can end
// at once instead of taking one more job. The returned func unregisters it.
func (m *Monitor) OnPause(fn func()) func() {
	if m == nil {
		return func() {}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.onPause = append(m.onPause, fn)
	index := len(m.onPause) - 1
	return func() {
		m.mu.Lock()
		defer m.mu.Unlock()
		if index < len(m.onPause) {
			m.onPause[index] = func() {}
		}
	}
}

// Write keeps the runner's log lines, so the logger can write here too.
func (m *Monitor) Write(p []byte) (int, error) {
	if m == nil {
		return len(p), nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.partial = append(m.partial, p...)
	for {
		end := bytes.IndexByte(m.partial, '\n')
		if end < 0 {
			break
		}
		m.lines = append(m.lines, string(m.partial[:end]))
		m.partial = m.partial[end+1:]
	}
	if len(m.lines) > logLines {
		m.lines = m.lines[len(m.lines)-logLines:]
	}
	return len(p), nil
}

// Snapshot copies the state. Running jobs and calls come first, oldest first.
func (m *Monitor) Snapshot() State {
	m.mu.Lock()
	defer m.mu.Unlock()
	state := State{Started: m.started, Paused: m.paused, Jobs: []Job{}, Calls: []Call{}}
	for _, id := range m.order {
		job := *m.active[id]
		job.Events = append([]Event(nil), job.Events...)
		state.Jobs = append(state.Jobs, job)
	}
	state.Jobs = append(state.Jobs, m.recent...)
	for _, call := range m.calls {
		state.Calls = append(state.Calls, *call)
	}
	sortCalls(state.Calls)
	state.Calls = append(state.Calls, m.done...)
	state.Log = append([]string(nil), m.lines...)
	return state
}

func sortCalls(calls []Call) {
	for i := 1; i < len(calls); i++ {
		for j := i; j > 0 && calls[j].Started.Before(calls[j-1].Started); j-- {
			calls[j], calls[j-1] = calls[j-1], calls[j]
		}
	}
}

// modelOf reads the model name from a chat request. The prompt is not kept.
func modelOf(body string) string {
	if body == "" {
		return ""
	}
	var request struct {
		Model string `json:"model"`
	}
	if json.Unmarshal([]byte(body), &request) != nil {
		return ""
	}
	return request.Model
}

func without(ids []string, id string) []string {
	out := ids[:0]
	for _, item := range ids {
		if item != id {
			out = append(out, item)
		}
	}
	return out
}

// clip shortens text to about limit bytes without splitting a character.
func clip(text string, limit int) string {
	if len(text) <= limit {
		return text
	}
	cut := limit
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return strings.TrimSpace(text[:cut]) + "…"
}
