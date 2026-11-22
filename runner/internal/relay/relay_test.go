package relay

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/client"
)

func quiet() *log.Logger { return log.New(io.Discard, "", 0) }

func TestTargetRelaysOnlyModelCallsToThisMachineAndThePrivateNetwork(t *testing.T) {
	r := New(nil, []string{"models.example.com"}, quiet())
	ok := []client.ModelCall{
		{Method: "GET", Path: "/v1/models", BaseURL: "http://127.0.0.1:11434"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://localhost:1234/"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://[::1]:8000"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://192.168.86.122:11434"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://10.0.0.5:11434"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://100.101.102.103:11434"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://gpu-box.local:11434"},
		{Method: "POST", Path: "/v1/chat/completions", BaseURL: "http://models.example.com"},
	}
	for _, call := range ok {
		if _, err := r.Target(&call); err != nil {
			t.Errorf("%+v: %v", call, err)
		}
	}
	got, _ := r.Target(&ok[0])
	if got != "http://127.0.0.1:11434/v1/models" {
		t.Fatalf("target = %q", got)
	}
	refused := []client.ModelCall{
		{Method: "POST", Path: "/api/pull", BaseURL: "http://127.0.0.1:11434"},
		{Method: "GET", Path: "/v1/chat/completions", BaseURL: "http://127.0.0.1:11434"},
		{Method: "GET", Path: "/v1/models", BaseURL: "http://93.184.216.34"},
		{Method: "GET", Path: "/v1/models", BaseURL: "http://169.254.169.254"},
		{Method: "GET", Path: "/v1/models", BaseURL: "http://me:pw@127.0.0.1:11434"},
		{Method: "GET", Path: "/v1/models", BaseURL: "http://127.0.0.1:11434/admin"},
		{Method: "GET", Path: "/v1/models", BaseURL: "file:///etc/passwd"},
	}
	for _, call := range refused {
		if _, err := r.Target(&call); err == nil {
			t.Errorf("%+v was allowed", call)
		}
	}
}

func TestTargetWithoutThePrivateNetworkRelaysOnlyToThisMachine(t *testing.T) {
	r := New(nil, []string{"gpu-box.lan"}, quiet())
	r.PrivateNetwork = false
	for _, base := range []string{"http://127.0.0.1:11434", "http://localhost:11434", "http://gpu-box.lan:11434"} {
		if _, err := r.Target(&client.ModelCall{Method: "GET", Path: "/v1/models", BaseURL: base}); err != nil {
			t.Errorf("%s: %v", base, err)
		}
	}
	for _, base := range []string{"http://192.168.86.122:11434", "http://other-box.local:11434"} {
		if _, err := r.Target(&client.ModelCall{Method: "GET", Path: "/v1/models", BaseURL: base}); err == nil {
			t.Errorf("%s was allowed", base)
		}
	}
}

func TestDialRefusesAPublicAddressItWasNotGiven(t *testing.T) {
	r := New(nil, nil, quiet())
	if _, err := r.dial(context.Background(), "tcp", "93.184.216.34:80"); err == nil || !strings.Contains(err.Error(), "llm_hosts") {
		t.Fatalf("err = %v", err)
	}
	if _, err := r.dial(context.Background(), "tcp", "169.254.169.254:80"); err == nil {
		t.Fatal("a link-local address was dialed")
	}
}

func TestForwardReturnsTheServersAnswerAndDoesNotFollowRedirects(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		switch req.URL.Path {
		case "/v1/chat/completions":
			body, _ := io.ReadAll(req.Body)
			if !strings.Contains(string(body), `"qwen"`) {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			_, _ = io.WriteString(w, `{"choices":[]}`)
		case "/v1/models":
			http.Redirect(w, req, "http://example.com/steal", http.StatusFound)
		}
	}))
	defer server.Close()
	r := New(nil, nil, quiet())
	answer := r.Forward(context.Background(), &client.ModelCall{
		Method: "POST", Path: "/v1/chat/completions", BaseURL: server.URL, Body: `{"model":"qwen"}`,
	})
	if answer.Status != 200 || answer.Body != `{"choices":[]}` || answer.Error != "" {
		t.Fatalf("answer = %+v", answer)
	}
	moved := r.Forward(context.Background(), &client.ModelCall{Method: "GET", Path: "/v1/models", BaseURL: server.URL})
	if moved.Status != http.StatusFound {
		t.Fatalf("redirect was followed: %+v", moved)
	}
	down := r.Forward(context.Background(), &client.ModelCall{Method: "GET", Path: "/v1/models", BaseURL: "http://127.0.0.1:1"})
	if down.Error == "" {
		t.Fatalf("a closed port should fail: %+v", down)
	}
}

func TestRunTakesACallAndAnswersIt(t *testing.T) {
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"data":[{"id":"llama3.2"}]}`)
	}))
	defer model.Close()
	var mu sync.Mutex
	handed := false
	answers := make(chan client.ModelAnswer, 1)
	lbw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer lbwr_test" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch {
		case req.URL.Path == "/api/runner/llm/next":
			mu.Lock()
			defer mu.Unlock()
			if handed {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			handed = true
			_ = json.NewEncoder(w).Encode(client.ModelCall{CallID: "llm_1", Method: "GET", Path: "/v1/models", BaseURL: model.URL})
		case req.URL.Path == "/api/runner/llm/llm_1/ack":
			_, _ = io.WriteString(w, `{"ok":true}`)
		case req.URL.Path == "/api/runner/llm/llm_1":
			var answer client.ModelAnswer
			_ = json.NewDecoder(req.Body).Decode(&answer)
			answers <- answer
			_, _ = io.WriteString(w, `{"ok":true}`)
		default:
			http.NotFound(w, req)
		}
	}))
	defer lbw.Close()
	api, err := client.New(lbw.URL, "lbwr_test", "")
	if err != nil {
		t.Fatal(err)
	}
	r := New(api, nil, quiet())
	r.Poll = 0
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Run(ctx) }()
	select {
	case answer := <-answers:
		if answer.Status != 200 || !strings.Contains(answer.Body, "llama3.2") {
			t.Fatalf("answer = %+v", answer)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the relay never answered")
	}
}

func TestRunStopsWhenTheRunnerIsDisconnected(t *testing.T) {
	lbw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer lbw.Close()
	api, _ := client.New(lbw.URL, "lbwr_gone", "")
	if err := New(api, nil, quiet()).Run(context.Background()); err != client.ErrUnauthorized {
		t.Fatalf("err = %v", err)
	}
}

func TestACallLBWOfferedElsewhereIsNotRelayed(t *testing.T) {
	var mu sync.Mutex
	handed, forwarded, answered := false, false, false
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		forwarded = true
		mu.Unlock()
		_, _ = io.WriteString(w, `{}`)
	}))
	defer model.Close()
	lbw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch req.URL.Path {
		case "/api/runner/llm/next":
			if handed {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			handed = true
			_ = json.NewEncoder(w).Encode(client.ModelCall{CallID: "llm_2", Method: "GET", Path: "/v1/models", BaseURL: model.URL})
		case "/api/runner/llm/llm_2/ack":
			w.WriteHeader(http.StatusConflict)
			_, _ = io.WriteString(w, `{"error":"LBW is no longer waiting for this call"}`)
		case "/api/runner/llm/llm_2":
			answered = true
		}
	}))
	defer lbw.Close()
	api, _ := client.New(lbw.URL, "lbwr_test", "")
	r := New(api, nil, quiet())
	r.Poll = 0
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	_ = r.Run(ctx)
	mu.Lock()
	defer mu.Unlock()
	if !handed || forwarded || answered {
		t.Fatalf("handed=%v forwarded=%v answered=%v", handed, forwarded, answered)
	}
}
