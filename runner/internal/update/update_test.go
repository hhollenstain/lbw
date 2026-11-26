package update

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
)

// fakeGitHub serves a releases list and the assets of one signed release.
type fakeGitHub struct {
	server   *httptest.Server
	key      ed25519.PrivateKey
	binary   []byte
	sums     []byte
	sig      []byte
	listings atomic.Int32
}

func newFakeGitHub(t *testing.T) *fakeGitHub {
	t.Helper()
	_, private, _ := ed25519.GenerateKey(rand.Reader)
	f := &fakeGitHub{key: private, binary: []byte("#!/bin/sh\necho new runner\n")}
	name := AssetName(runtime.GOOS, runtime.GOARCH)
	sum := sha256.Sum256(f.binary)
	f.sums = []byte(fmt.Sprintf("%s  %s\n%s  lbw-runner-other\n", hex.EncodeToString(sum[:]), name, strings.Repeat("0", 64)))
	f.sig = []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(private, f.sums)))
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/releases":
			f.listings.Add(1)
			if r.Header.Get("If-None-Match") == `"list1"` {
				w.WriteHeader(http.StatusNotModified)
				return
			}
			asset := func(file string) map[string]string {
				return map[string]string{"name": file, "browser_download_url": f.server.URL + "/download/" + file}
			}
			w.Header().Set("ETag", `"list1"`)
			_ = json.NewEncoder(w).Encode([]map[string]any{
				{"tag_name": "v9.9.9", "html_url": "x"},
				{"tag_name": "runner-v0.4.0-rc1", "prerelease": true},
				{"tag_name": "runner-v0.5.0", "draft": true},
				{"tag_name": "runner-v0.3.0", "html_url": f.server.URL + "/r/0.3.0", "assets": []any{asset(name), asset("SHA256SUMS"), asset("SHA256SUMS.sig")}},
				{"tag_name": "runner-v0.2.1", "html_url": "old"},
			})
		case "/download/" + name:
			_, _ = w.Write(f.binary)
		case "/download/SHA256SUMS":
			_, _ = w.Write(f.sums)
		case "/download/SHA256SUMS.sig":
			_, _ = w.Write(f.sig)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeGitHub) public() ed25519.PublicKey { return f.key.Public().(ed25519.PublicKey) }

func TestVersionsAreRunnerTagsOnly(t *testing.T) {
	for tag, want := range map[string]string{"runner-v1.2.3": "1.2.3", "v0.10.0": "0.10.0", "2.0.1": "2.0.1"} {
		if got, ok := Parse(tag); !ok || got != want {
			t.Fatalf("Parse(%q) = %q %v", tag, got, ok)
		}
	}
	for _, tag := range []string{"dev", "ee6cbc4-dirty", "runner-v1.2.0-3-gabc1234", "1.2", "1.x.0"} {
		if _, ok := Parse(tag); ok {
			t.Fatalf("Parse(%q) took a build from source for a release", tag)
		}
	}
	if !Newer("0.10.0", "0.9.9") || Newer("0.3.0", "0.3.0") || Newer("0.2.9", "0.3.0") {
		t.Fatal("versions compare as text")
	}
}

func TestTheNewestPublishedRunnerReleaseIsFoundAndAskedForCheaply(t *testing.T) {
	f := newFakeGitHub(t)
	checker := &Checker{API: f.server.URL + "/releases", HTTP: f.server.Client()}
	rel, err := checker.Latest(context.Background())
	if err != nil || rel == nil || rel.Version != "0.3.0" || rel.Tag != "runner-v0.3.0" {
		t.Fatalf("latest = %+v %v", rel, err)
	}
	again, err := checker.Latest(context.Background())
	if err != nil || again != rel || f.listings.Load() != 2 {
		t.Fatalf("the unchanged list was not reused: %+v %v", again, err)
	}
}

func TestOnlyASignedMatchingBinaryIsInstalled(t *testing.T) {
	f := newFakeGitHub(t)
	checker := &Checker{API: f.server.URL + "/releases", HTTP: f.server.Client()}
	rel, _ := checker.Latest(context.Background())
	dir := t.TempDir()

	path, err := Fetch(context.Background(), f.server.Client(), rel, f.public(), dir)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(dir, "lbw-runner")
	if err := os.WriteFile(target, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Replace(path, target); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(target); string(data) != string(f.binary) {
		t.Fatalf("target = %q", data)
	}

	_, otherKey, _ := ed25519.GenerateKey(rand.Reader)
	if _, err := Fetch(context.Background(), f.server.Client(), rel, otherKey.Public().(ed25519.PublicKey), dir); err == nil || !strings.Contains(err.Error(), "not signed by LBW's release key") {
		t.Fatalf("another key's release was accepted: %v", err)
	}
	if _, err := Fetch(context.Background(), f.server.Client(), rel, nil, dir); err == nil {
		t.Fatal("a build with no release key installed an update")
	}
	f.binary = []byte("swapped after signing")
	if _, err := Fetch(context.Background(), f.server.Client(), rel, f.public(), dir); err == nil || !strings.Contains(err.Error(), "does not match its signed sum") {
		t.Fatalf("a swapped binary was accepted: %v", err)
	}
	delete(rel.assets, "SHA256SUMS.sig")
	if _, err := Fetch(context.Background(), f.server.Client(), rel, f.public(), dir); err == nil || !strings.Contains(err.Error(), "has no SHA256SUMS.sig") {
		t.Fatalf("an unsigned release was accepted: %v", err)
	}
}

func TestTheBuiltInKeyIsAnEd25519Key(t *testing.T) {
	if len(PublicKey) != ed25519.PublicKeySize {
		t.Fatalf("the release key is %d bytes", len(PublicKey))
	}
}
