package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestTheCommandListsItsCommands(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := dispatch(context.Background(), nil, &out, &errOut); code != 2 || !strings.Contains(errOut.String(), "login --server") {
		t.Fatalf("code=%d err=%s", code, errOut.String())
	}
	out.Reset()
	if code := dispatch(context.Background(), []string{"version"}, &out, &errOut); code != 0 || strings.TrimSpace(out.String()) != "dev" {
		t.Fatalf("version = %q", out.String())
	}
	if code := dispatch(context.Background(), []string{"nope"}, &out, &errOut); code != 2 {
		t.Fatalf("code = %d", code)
	}
}

func TestLoginNeedsAServer(t *testing.T) {
	t.Setenv("LBW_RUNNER_HOME", t.TempDir())
	t.Setenv("LBW_SERVER", "")
	var out, errOut bytes.Buffer
	if code := dispatch(context.Background(), []string{"login"}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "--server") {
		t.Fatalf("code=%d err=%s", code, errOut.String())
	}
}
