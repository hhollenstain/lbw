package client

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestLinkingWaitsForApprovalThenReturnsTheToken(t *testing.T) {
	approved := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/runner/link":
			_, _ = w.Write([]byte(`{"device_code":"d","user_code":"ABCD-EFGH","interval":1,"expires_in":600}`))
		case "/api/runner/link/token":
			if !approved {
				w.WriteHeader(http.StatusPreconditionRequired)
				return
			}
			_, _ = w.Write([]byte(`{"token":"lbwr_x","runner_id":"r1"}`))
		}
	}))
	defer server.Close()
	api, err := New(server.URL+"/", "", "")
	if err != nil {
		t.Fatal(err)
	}
	link, err := api.StartLink(context.Background(), "laptop", "darwin/arm64")
	if err != nil || link.UserCode != "ABCD-EFGH" {
		t.Fatalf("link = %+v %v", link, err)
	}
	if _, err := api.ClaimToken(context.Background(), link.DeviceCode); !errors.Is(err, ErrPending) {
		t.Fatalf("err = %v", err)
	}
	approved = true
	claimed, err := api.ClaimToken(context.Background(), link.DeviceCode)
	if err != nil || claimed.Token != "lbwr_x" {
		t.Fatalf("claimed = %+v %v", claimed, err)
	}
}

func TestAnErrorBodyIsReported(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"error":"this job is no longer running"}`))
	}))
	defer server.Close()
	api, _ := New(server.URL, "lbwr_x", "")
	err := api.Finish(context.Background(), "j", Result{Status: "done"})
	if err == nil || err.Error() != "POST /api/runner/jobs/j/finish: 409 this job is no longer running" {
		t.Fatalf("err = %v", err)
	}
	if _, err := New(server.URL, "", "/does/not/exist.pem"); err == nil {
		t.Fatal("a missing CA file should be refused")
	}
}
