package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hhollenstain/lbw/runner/internal/update"
)

func TestAKeySignsAndTheRunnerChecksIt(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "signing.key")
	var out bytes.Buffer
	if err := run([]string{"keygen", keyFile}, &out); err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(keyFile); info.Mode().Perm() != 0o600 {
		t.Fatalf("the private key is readable by others: %v", info.Mode())
	}
	if err := run([]string{"keygen", keyFile}, &out); err == nil {
		t.Fatal("keygen overwrote an existing key")
	}
	public, _ := base64.StdEncoding.DecodeString(strings.TrimSpace(out.String()))
	seed, _ := os.ReadFile(keyFile)
	t.Setenv("RUNNER_SIGNING_KEY", string(seed))

	sums := filepath.Join(dir, "SHA256SUMS")
	_ = os.WriteFile(sums, []byte("abc  lbw-runner-linux-amd64\n"), 0o644)
	if err := run([]string{"sign", sums}, &out); err != nil {
		t.Fatal(err)
	}
	trusted := update.PublicKey
	defer func() { update.PublicKey = trusted }()
	update.PublicKey = ed25519.PublicKey(public)
	if err := run([]string{"verify", sums}, &out); err != nil {
		t.Fatalf("verify: %v", err)
	}
	_ = os.WriteFile(sums, []byte("abc  lbw-runner-linux-amd64\nevil  extra\n"), 0o644)
	if err := run([]string{"verify", sums}, &out); err == nil {
		t.Fatal("a changed SHA256SUMS verified")
	}
	t.Setenv("RUNNER_SIGNING_KEY", "nope")
	if err := run([]string{"sign", sums}, &out); err == nil {
		t.Fatal("signed without a key")
	}
}
