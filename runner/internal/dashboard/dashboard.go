// Package dashboard serves the runner's own page on this machine: what it runs
// now, the model calls it relays, and its recent log, with controls to pause
// new work, stop a job, update Claude Code, and link the machine.
//
// It listens on 127.0.0.1 only. A random key in the runner folder, readable
// only by its owner, opens it: `lbw-runner open` passes the key once in the
// address, and the page keeps it as a SameSite=Strict cookie. Requests must
// name this host, so a web page cannot reach it through DNS rebinding, and a
// change must come from this page.
package dashboard

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
)

const cookieName = "lbw_runner"

//go:embed index.html
var page []byte

// Link is a link the person is approving on their LBW profile.
type Link struct {
	Code    string    `json:"code"`
	URL     string    `json:"url"`
	Expires time.Time `json:"expires"`
	Error   string    `json:"error,omitempty"`
}

// Status is the runner itself: whether it is linked, to what, and with which tools.
type Status struct {
	Version  string `json:"version"`
	Platform string `json:"platform"`
	// Connection is "online", "connecting", "not_linked", or "disconnected".
	Connection    string `json:"connection"`
	Server        string `json:"server"`
	Name          string `json:"name"`
	ClaudeVersion string `json:"claude_version"`
	ClaudeError   string `json:"claude_error,omitempty"`
	Relay         bool   `json:"relay"`
	Sandbox       bool   `json:"sandbox"`
	Link          *Link  `json:"link,omitempty"`
	Updating      bool   `json:"updating"`
	UpdateOutput  string `json:"update_output,omitempty"`
	// Runner is this runner's own update: what is published, and how installing went.
	Runner RunnerUpdate `json:"runner_update"`
}

// Actions are what the page can ask the runner to do.
type Actions struct {
	// Link starts linking this machine. It returns once the code is shown in
	// Status.Link, and finishes in the background.
	Link func(ctx context.Context, server, name string) error
	// UpdateClaude runs `claude update`.
	UpdateClaude func(ctx context.Context) (string, error)
	// StopTask stops one of the owner's jobs in LBW and returns the inbox after it.
	StopTask func(ctx context.Context, taskID string) (*client.Inbox, error)
	// RefreshInbox asks for the inbox to be read again now.
	RefreshInbox func()
	// CheckUpdate looks for a newer runner release now.
	CheckUpdate func(ctx context.Context)
	// InstallUpdate downloads and checks the newer release and puts it in
	// place. Restart then starts it; it is called once the answer is sent.
	InstallUpdate func(ctx context.Context) error
	Restart       func()
}

// RunnerUpdate is whether a newer runner is published, and installing it.
type RunnerUpdate struct {
	Latest     string     `json:"latest,omitempty"`
	Available  bool       `json:"available"`
	URL        string     `json:"url,omitempty"`
	Checked    *time.Time `json:"checked,omitempty"`
	Error      string     `json:"error,omitempty"`
	Installing bool       `json:"installing"`
	// Dev is a build from source, which does not update itself.
	Dev bool `json:"dev"`
}

// InboxState is the owner's inbox as the page shows it.
type InboxState struct {
	Waiting []client.InboxItem `json:"waiting"`
	Running []client.InboxItem `json:"running"`
	Checked *time.Time         `json:"checked,omitempty"`
	Error   string             `json:"error,omitempty"`
}

// viewWindow is how long after the page or the tray last asked for its state
// the inbox keeps being read. A runner nobody looks at reads nothing.
const viewWindow = 30 * time.Second

// Inbox reads while someone looks: every 10 seconds with the page open, and
// once a minute when only the tray icon watches, so a tray on every desk
// stays light on LBW.
const (
	PageInboxEvery = 10 * time.Second
	TrayInboxEvery = time.Minute
)

// Server is the dashboard.
type Server struct {
	Monitor *monitor.Monitor
	Actions Actions
	Port    int
	Key     string
	Log     *log.Logger

	mu     sync.Mutex
	status Status
	inbox  InboxState
	viewed time.Time
	tray   time.Time
}

// SetInbox keeps the inbox the runner read, or why it could not read it.
// A nil inbox with no error clears it, as when the machine is unlinked.
func (s *Server) SetInbox(inbox *client.Inbox, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		s.inbox.Error = err.Error()
		return
	}
	if inbox == nil {
		s.inbox = InboxState{}
		return
	}
	now := time.Now()
	s.inbox = InboxState{Waiting: inbox.Waiting, Running: inbox.Running, Checked: &now}
}

// Watched says whether someone has the page open, so the inbox is worth reading.
func (s *Server) Watched() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return time.Since(s.viewed) < viewWindow
}

// InboxEvery is how often the inbox is worth reading now: every 10 seconds
// with the page open, every minute with only the tray, and never otherwise.
func (s *Server) InboxEvery() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case time.Since(s.viewed) < viewWindow:
		return PageInboxEvery
	case time.Since(s.tray) < viewWindow:
		return TrayInboxEvery
	}
	return 0
}

// Update changes the runner's status under the lock.
func (s *Server) Update(change func(*Status)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	change(&s.status)
}

// Status copies the runner's status.
func (s *Server) Status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := s.status
	if status.Link != nil {
		link := *status.Link
		status.Link = &link
	}
	return status
}

// Address is the page's address on this machine.
func Address(port int) string {
	return "http://127.0.0.1:" + strconv.Itoa(port)
}

// Serve listens until ctx ends.
func (s *Server) Serve(ctx context.Context) error {
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(s.Port))
	listener, err := net.Listen("tcp", address)
	// A runner restarting into an update may start while the old one still
	// holds the port, so wait a few seconds for it.
	for tries := 0; err != nil && tries < 20 && ctx.Err() == nil; tries++ {
		time.Sleep(500 * time.Millisecond)
		listener, err = net.Listen("tcp", address)
	}
	if err != nil {
		return fmt.Errorf("the dashboard could not listen on 127.0.0.1:%d: %w", s.Port, err)
	}
	server := &http.Server{Handler: s.Handler(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Handler routes the page and its API.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /{$}", s.index)
	mux.HandleFunc("GET /api/state", s.signedIn(s.state))
	mux.HandleFunc("POST /api/pause", s.signedIn(s.pause))
	mux.HandleFunc("POST /api/jobs/{id}/stop", s.signedIn(s.stop))
	mux.HandleFunc("POST /api/tasks/{id}/stop", s.signedIn(s.stopTask))
	mux.HandleFunc("POST /api/update-claude", s.signedIn(s.updateClaude))
	mux.HandleFunc("POST /api/update/check", s.signedIn(s.checkUpdate))
	mux.HandleFunc("POST /api/update", s.signedIn(s.installUpdate))
	mux.HandleFunc("POST /api/link", s.signedIn(s.link))
	return s.local(mux)
}

// local refuses a request that does not name this machine, which is how a
// rebound DNS name would reach it.
func (s *Server) local(next http.Handler) http.Handler {
	port := strconv.Itoa(s.Port)
	allowed := map[string]bool{"127.0.0.1:" + port: true, "localhost:" + port: true}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !allowed[r.Host] {
			http.Error(w, "this page only answers on 127.0.0.1", http.StatusMisdirectedRequest)
			return
		}
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

// signedIn needs the key's cookie. A change must also come from this page.
func (s *Server) signedIn(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie(cookieName)
		if err != nil || !s.keyMatches(cookie.Value) {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "open the dashboard from LBW Runner"})
			return
		}
		if r.Method != http.MethodGet {
			origin := r.Header.Get("Origin")
			if origin != "" && origin != "http://"+r.Host {
				writeJSON(w, http.StatusForbidden, map[string]string{"error": "that request did not come from this page"})
				return
			}
			if !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
				writeJSON(w, http.StatusUnsupportedMediaType, map[string]string{"error": "send JSON"})
				return
			}
		}
		next(w, r)
	}
}

func (s *Server) keyMatches(value string) bool {
	return s.Key != "" && subtle.ConstantTimeCompare([]byte(value), []byte(s.Key)) == 1
}

// health proves this is the runner without giving the key away: it answers
// a nonce with an HMAC only the key's holder can check.
func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"app": "lbw-runner", "proof": Proof(s.Key, r.URL.Query().Get("nonce"))})
}

// Proof is the health check's answer to nonce.
func Proof(key, nonce string) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(nonce))
	return hex.EncodeToString(mac.Sum(nil))
}

func (s *Server) index(w http.ResponseWriter, r *http.Request) {
	if key := r.URL.Query().Get("key"); key != "" {
		if !s.keyMatches(key) {
			http.Error(w, "That key does not open this runner. Open LBW Runner again.", http.StatusUnauthorized)
			return
		}
		http.SetCookie(w, &http.Cookie{
			Name: cookieName, Value: s.Key, Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode,
		})
		// Take the key out of the address bar and the history.
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	cookie, err := r.Cookie(cookieName)
	if err != nil || !s.keyMatches(cookie.Value) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `<!doctype html><meta charset="utf-8"><title>LBW Runner</title><body style="font:15px system-ui;margin:3rem;color:#10243f">`+
			`<h1 style="font-size:1.3rem">Open LBW Runner</h1><p>Open the LBW Runner app, or run <code>lbw-runner open</code>, to see this page.</p>`)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
	_, _ = w.Write(page)
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	var opened bool
	if r.URL.Query().Get("from") == "tray" {
		// The tray polls often for its own counts, but reads the inbox slowly.
		opened = time.Since(s.tray) >= viewWindow && time.Since(s.viewed) >= viewWindow
		s.tray = time.Now()
	} else {
		opened = time.Since(s.viewed) >= viewWindow
		s.viewed = time.Now()
	}
	inbox := s.inbox
	s.mu.Unlock()
	if opened && s.Actions.RefreshInbox != nil {
		// The page was just opened: read the inbox now rather than at the next tick.
		s.Actions.RefreshInbox()
	}
	if inbox.Waiting == nil {
		inbox.Waiting = []client.InboxItem{}
	}
	if inbox.Running == nil {
		inbox.Running = []client.InboxItem{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": s.Status(), "work": s.Monitor.Snapshot(), "inbox": inbox})
}

func (s *Server) pause(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Paused bool `json:"paused"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "send {\"paused\": true} or false"})
		return
	}
	s.Monitor.SetPaused(body.Paused)
	if body.Paused {
		s.Log.Printf("paused from the dashboard: no new jobs or model calls until resumed")
	} else {
		s.Log.Printf("resumed from the dashboard")
	}
	s.state(w, r)
}

func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.Monitor.StopJob(id) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "that job is not running"})
		return
	}
	s.Log.Printf("job %s: stopped from the dashboard", id)
	s.state(w, r)
}

// stopTask stops a job in LBW: one that LBW runs, or a runner job elsewhere.
func (s *Server) stopTask(w http.ResponseWriter, r *http.Request) {
	if s.Actions.StopTask == nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "link this machine first"})
		return
	}
	id := r.PathValue("id")
	inbox, err := s.Actions.StopTask(r.Context(), id)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	s.SetInbox(inbox, nil)
	s.Log.Printf("task %s: stopped in LBW from the dashboard", id)
	s.state(w, r)
}

func (s *Server) checkUpdate(w http.ResponseWriter, r *http.Request) {
	if s.Actions.CheckUpdate == nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "this runner does not check for updates"})
		return
	}
	s.Actions.CheckUpdate(r.Context())
	s.state(w, r)
}

// installUpdate puts the newer runner in place, answers, then restarts into it.
func (s *Server) installUpdate(w http.ResponseWriter, r *http.Request) {
	if s.Actions.InstallUpdate == nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "this runner does not update itself"})
		return
	}
	busy := false
	s.Update(func(status *Status) {
		busy = status.Runner.Installing
		status.Runner.Installing = true
		status.Runner.Error = ""
	})
	if busy {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "the update is already installing"})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	if err := s.Actions.InstallUpdate(ctx); err != nil {
		s.Update(func(status *Status) {
			status.Runner.Installing = false
			status.Runner.Error = err.Error()
		})
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	s.Log.Printf("installed the runner update from the dashboard: restarting")
	s.state(w, r)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
	if s.Actions.Restart != nil {
		// Restart after the answer is on its way, so the page hears it.
		go func() {
			time.Sleep(500 * time.Millisecond)
			s.Actions.Restart()
		}()
	}
}

func (s *Server) updateClaude(w http.ResponseWriter, r *http.Request) {
	if s.Actions.UpdateClaude == nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "Claude Code is not installed on this machine"})
		return
	}
	busy := false
	s.Update(func(status *Status) {
		busy = status.Updating
		status.Updating = true
	})
	if busy {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "Claude Code is already updating"})
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		out, err := s.Actions.UpdateClaude(ctx)
		if err != nil {
			out = strings.TrimSpace(out + "\n" + err.Error())
		}
		s.Update(func(status *Status) {
			status.Updating = false
			status.UpdateOutput = strings.TrimSpace(out)
		})
	}()
	s.state(w, r)
}

func (s *Server) link(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Server string `json:"server"`
		Name   string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || strings.TrimSpace(body.Server) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "enter your LBW address"})
		return
	}
	if s.Status().Connection == "online" {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "this machine is already linked"})
		return
	}
	if err := s.Actions.Link(r.Context(), strings.TrimSpace(body.Server), strings.TrimSpace(body.Name)); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	s.state(w, r)
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// LoadKey reads the dashboard key from the runner folder, making one the
// first time. Only the owner can read the file.
func LoadKey(home string) (string, error) {
	path := filepath.Join(home, "dashboard.key")
	if data, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(data))) >= 32 {
		return strings.TrimSpace(string(data)), nil
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	key := hex.EncodeToString(buf)
	if err := os.MkdirAll(home, 0o700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(key+"\n"), 0o600); err != nil {
		return "", err
	}
	return key, nil
}
