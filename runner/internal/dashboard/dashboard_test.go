package dashboard

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
)

const key = "0123456789abcdef0123456789abcdef"

func newServer() (*Server, http.Handler) {
	s := &Server{Monitor: monitor.New(), Port: 7317, Key: key, Log: log.New(io.Discard, "", 0)}
	s.Update(func(status *Status) { status.Connection = "online" })
	return s, s.Handler()
}

func request(t *testing.T, h http.Handler, method, target, body string, edit func(*http.Request)) *httptest.ResponseRecorder {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	req.Host = "127.0.0.1:7317"
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if edit != nil {
		edit(req)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func signed(req *http.Request) {
	req.AddCookie(&http.Cookie{Name: cookieName, Value: key})
}

func TestTheKeyOpensThePageOnceAndStaysOutOfTheAddress(t *testing.T) {
	_, h := newServer()
	if rec := request(t, h, "GET", "/", "", nil); rec.Code != http.StatusUnauthorized || strings.Contains(rec.Body.String(), "Running now") {
		t.Fatalf("no key: %d", rec.Code)
	}
	if rec := request(t, h, "GET", "/?key=wrong", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("wrong key: %d", rec.Code)
	}
	rec := request(t, h, "GET", "/?key="+key, "", nil)
	if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/" {
		t.Fatalf("key: %d %s", rec.Code, rec.Header().Get("Location"))
	}
	cookie := rec.Result().Cookies()[0]
	if !cookie.HttpOnly || cookie.SameSite != http.SameSiteStrictMode {
		t.Fatalf("cookie = %+v", cookie)
	}
	page := request(t, h, "GET", "/", "", signed)
	if page.Code != http.StatusOK || !strings.Contains(page.Body.String(), "Running now") {
		t.Fatalf("page: %d", page.Code)
	}
	if page.Header().Get("X-Frame-Options") != "DENY" {
		t.Fatal("the page can be framed")
	}
}

func TestOnlyThisMachineAndThisPageReachTheAPI(t *testing.T) {
	_, h := newServer()
	rebound := request(t, h, "GET", "/api/state", "", func(req *http.Request) {
		signed(req)
		req.Host = "attacker.example:7317"
	})
	if rebound.Code != http.StatusMisdirectedRequest {
		t.Fatalf("rebound host: %d", rebound.Code)
	}
	if rec := request(t, h, "GET", "/api/state", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("no cookie: %d", rec.Code)
	}
	crossSite := request(t, h, "POST", "/api/pause", `{"paused":true}`, func(req *http.Request) {
		signed(req)
		req.Header.Set("Origin", "https://attacker.example")
	})
	if crossSite.Code != http.StatusForbidden {
		t.Fatalf("cross-site post: %d", crossSite.Code)
	}
	form := request(t, h, "POST", "/api/pause", "", func(req *http.Request) {
		signed(req)
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	})
	if form.Code != http.StatusUnsupportedMediaType {
		t.Fatalf("form post: %d", form.Code)
	}
}

func TestThePagePausesAndStopsWork(t *testing.T) {
	s, h := newServer()
	cancelled := false
	s.Monitor.JobStarted(&client.Job{JobID: "j1", Goal: "Add a page"}, func() { cancelled = true })

	paused := request(t, h, "POST", "/api/pause", `{"paused":true}`, func(req *http.Request) {
		signed(req)
		req.Header.Set("Origin", "http://127.0.0.1:7317")
	})
	var state struct {
		Status Status        `json:"status"`
		Work   monitor.State `json:"work"`
	}
	if err := json.Unmarshal(paused.Body.Bytes(), &state); err != nil || !state.Work.Paused || state.Status.Connection != "online" {
		t.Fatalf("pause: %d %s", paused.Code, paused.Body.String())
	}
	if rec := request(t, h, "POST", "/api/jobs/nope/stop", `{}`, signed); rec.Code != http.StatusNotFound {
		t.Fatalf("stop unknown: %d", rec.Code)
	}
	if rec := request(t, h, "POST", "/api/jobs/j1/stop", `{}`, signed); rec.Code != http.StatusOK || !cancelled {
		t.Fatalf("stop: %d cancelled=%v", rec.Code, cancelled)
	}
}

func TestLinkingIsOfferedOnlyWhenNotLinked(t *testing.T) {
	s, h := newServer()
	var asked []string
	s.Actions.Link = func(_ context.Context, server, name string) error {
		asked = append(asked, server, name)
		s.Update(func(status *Status) { status.Link = &Link{Code: "ABCD-EFGH", URL: server + "/link"} })
		return nil
	}
	if rec := request(t, h, "POST", "/api/link", `{"server":"https://lbw.example"}`, signed); rec.Code != http.StatusConflict {
		t.Fatalf("linked runner: %d", rec.Code)
	}
	s.Update(func(status *Status) { status.Connection = "not_linked" })
	if rec := request(t, h, "POST", "/api/link", `{"server":""}`, signed); rec.Code != http.StatusBadRequest {
		t.Fatalf("no server: %d", rec.Code)
	}
	rec := request(t, h, "POST", "/api/link", `{"server":"https://lbw.example","name":"laptop"}`, signed)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "ABCD-EFGH") {
		t.Fatalf("link: %d %s", rec.Code, rec.Body.String())
	}
	if strings.Join(asked, " ") != "https://lbw.example laptop" {
		t.Fatalf("asked = %q", asked)
	}
}

func TestTheHealthCheckProvesTheKeyWithoutSendingIt(t *testing.T) {
	_, h := newServer()
	rec := request(t, h, "GET", "/healthz?nonce=n1", "", nil)
	if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), key) {
		t.Fatalf("health: %d %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), Proof(key, "n1")) || Proof(key, "n1") == Proof(key, "n2") {
		t.Fatal("the proof does not depend on the nonce")
	}
}

func TestTheKeyIsMadeOnceAndOnlyTheOwnerCanReadIt(t *testing.T) {
	home := t.TempDir()
	first, err := LoadKey(home)
	if err != nil || len(first) != 64 {
		t.Fatalf("key %q %v", first, err)
	}
	again, _ := LoadKey(home)
	if again != first {
		t.Fatal("the key changed")
	}
	info, err := os.Stat(filepath.Join(home, "dashboard.key"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v %v", info.Mode(), err)
	}
}

func TestThePageShowsTheInboxAndStopsJobsInLBW(t *testing.T) {
	s, h := newServer()
	refreshed := 0
	var stopped []string
	s.Actions.RefreshInbox = func() { refreshed++ }
	s.Actions.StopTask = func(_ context.Context, id string) (*client.Inbox, error) {
		if id == "done" {
			return nil, errors.New("only a queued or running job can be stopped")
		}
		stopped = append(stopped, id)
		return &client.Inbox{Waiting: []client.InboxItem{}, Running: []client.InboxItem{}}, nil
	}
	if s.Watched() {
		t.Fatal("no one has opened the page yet")
	}
	s.SetInbox(&client.Inbox{
		Waiting: []client.InboxItem{{ID: "t1", Title: "Add a page", WaitingFor: "Review the plan", Link: "/?change=t1"}},
		Running: []client.InboxItem{{ID: "t2", Title: "Write the docs", Status: "running"}},
	}, nil)

	rec := request(t, h, "GET", "/api/state", "", signed)
	var state struct {
		Inbox InboxState `json:"inbox"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &state); err != nil || len(state.Inbox.Waiting) != 1 || state.Inbox.Checked == nil {
		t.Fatalf("state: %s", rec.Body.String())
	}
	if refreshed != 1 || !s.Watched() {
		t.Fatalf("opening the page did not read the inbox: %d", refreshed)
	}
	request(t, h, "GET", "/api/state", "", signed)
	if refreshed != 1 {
		t.Fatal("every refresh read the inbox again")
	}

	if rec := request(t, h, "POST", "/api/tasks/done/stop", `{}`, signed); rec.Code != http.StatusBadGateway || !strings.Contains(rec.Body.String(), "only a queued") {
		t.Fatalf("stop done: %d %s", rec.Code, rec.Body.String())
	}
	rec = request(t, h, "POST", "/api/tasks/t2/stop", `{}`, signed)
	if rec.Code != http.StatusOK || strings.Join(stopped, ",") != "t2" || strings.Contains(rec.Body.String(), "Write the docs") {
		t.Fatalf("stop: %d %s", rec.Code, rec.Body.String())
	}

	s.SetInbox(nil, errors.New("LBW is unreachable"))
	rec = request(t, h, "GET", "/api/state", "", signed)
	if !strings.Contains(rec.Body.String(), "LBW is unreachable") {
		t.Fatalf("the read error is not shown: %s", rec.Body.String())
	}
}

func TestTheTrayReadsTheInboxSlowerThanAnOpenPage(t *testing.T) {
	s, h := newServer()
	refreshed := 0
	s.Actions.RefreshInbox = func() { refreshed++ }
	if s.InboxEvery() != 0 {
		t.Fatal("nobody watches, yet the inbox is read")
	}
	request(t, h, "GET", "/api/state?from=tray", "", signed)
	if s.InboxEvery() != TrayInboxEvery || s.Watched() || refreshed != 1 {
		t.Fatalf("tray only: every=%v watched=%v refreshed=%d", s.InboxEvery(), s.Watched(), refreshed)
	}
	request(t, h, "GET", "/api/state?from=tray", "", signed)
	if refreshed != 1 {
		t.Fatal("each tray poll read the inbox")
	}
	request(t, h, "GET", "/api/state", "", signed)
	if s.InboxEvery() != PageInboxEvery || refreshed != 2 {
		t.Fatalf("page open: every=%v refreshed=%d", s.InboxEvery(), refreshed)
	}
}

func TestAnUpdateInstallsOnlyWhenAskedThenRestarts(t *testing.T) {
	s, h := newServer()
	restarted := make(chan struct{}, 1)
	blocked := true
	s.Actions.InstallUpdate = func(context.Context) error {
		if blocked {
			return errors.New("a job is running here: let it finish or stop it, then install the update")
		}
		return nil
	}
	s.Actions.Restart = func() { restarted <- struct{}{} }
	checked := 0
	s.Actions.CheckUpdate = func(context.Context) {
		checked++
		s.Update(func(status *Status) { status.Runner = RunnerUpdate{Latest: "0.3.0", Available: true} })
	}
	if rec := request(t, h, "POST", "/api/update/check", `{}`, signed); rec.Code != http.StatusOK || checked != 1 || !strings.Contains(rec.Body.String(), `"available":true`) {
		t.Fatalf("check: %d %s", rec.Code, rec.Body.String())
	}
	rec := request(t, h, "POST", "/api/update", `{}`, signed)
	if rec.Code != http.StatusConflict || !strings.Contains(rec.Body.String(), "a job is running") || s.Status().Runner.Installing {
		t.Fatalf("busy: %d %s", rec.Code, rec.Body.String())
	}
	blocked = false
	if rec := request(t, h, "POST", "/api/update", `{}`, signed); rec.Code != http.StatusOK {
		t.Fatalf("install: %d %s", rec.Code, rec.Body.String())
	}
	select {
	case <-restarted:
	case <-time.After(3 * time.Second):
		t.Fatal("the runner did not restart into the update")
	}
	if rec := request(t, h, "POST", "/api/update", `{}`, nil); rec.Code != http.StatusUnauthorized {
		t.Fatal("an update installed without the key")
	}
}
