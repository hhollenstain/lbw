// Package update finds, checks, and installs new LBW Runner releases.
//
// Releases are published to GitHub under tags named runner-vX.Y.Z. Each one
// carries a binary per platform, SHA256SUMS, and SHA256SUMS.sig: an Ed25519
// signature over SHA256SUMS made with LBW's release key. The runner holds the
// public half (PublicKey). A download is installed only when the signature
// verifies and the binary matches its listed sum, so a compromised download
// host cannot hand the runner a binary of its choosing.
//
// The runner never installs on its own. It tells the person an update is
// available, and installs when they ask from the app, the tray, or the CLI.
package update

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// TagPrefix marks the repository's runner releases among its other tags.
const TagPrefix = "runner-v"

// DefaultAPI is the GitHub releases list of the repository that publishes the runner.
const DefaultAPI = "https://api.github.com/repos/hhollenstain/lbw/releases?per_page=30"

// MaxBinary bounds a download. A runner binary is well under it.
const MaxBinary = 200 << 20

// Release is a published runner release.
type Release struct {
	Tag     string `json:"tag"`
	Version string `json:"version"`
	URL     string `json:"url"`
	assets  map[string]string
}

// Checker asks GitHub for the newest runner release. It keeps the last answer
// and its ETag, so an unchanged list costs an empty 304, which GitHub does
// not count against its rate limit.
type Checker struct {
	API  string
	HTTP *http.Client

	mu     sync.Mutex
	etag   string
	latest *Release
}

// NewChecker checks the published releases.
func NewChecker() *Checker {
	return &Checker{API: DefaultAPI, HTTP: &http.Client{Timeout: 30 * time.Second}}
}

// Latest is the newest runner release, or nil when none is published.
func (c *Checker) Latest(ctx context.Context) (*Release, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.API, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "lbw-runner")
	c.mu.Lock()
	if c.etag != "" {
		req.Header.Set("If-None-Match", c.etag)
	}
	c.mu.Unlock()
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("could not reach GitHub: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.latest, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GitHub answered %d while listing releases", resp.StatusCode)
	}
	var listed []struct {
		Tag        string `json:"tag_name"`
		URL        string `json:"html_url"`
		Draft      bool   `json:"draft"`
		Prerelease bool   `json:"prerelease"`
		Assets     []struct {
			Name string `json:"name"`
			URL  string `json:"browser_download_url"`
		} `json:"assets"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&listed); err != nil {
		return nil, fmt.Errorf("reading GitHub's release list: %w", err)
	}
	var best *Release
	for _, item := range listed {
		version, ok := Parse(item.Tag)
		if !ok || item.Draft || item.Prerelease || !strings.HasPrefix(item.Tag, TagPrefix) {
			continue
		}
		if best != nil && !Newer(version, best.Version) {
			continue
		}
		assets := map[string]string{}
		for _, asset := range item.Assets {
			assets[asset.Name] = asset.URL
		}
		best = &Release{Tag: item.Tag, Version: version, URL: item.URL, assets: assets}
	}
	c.mu.Lock()
	c.etag, c.latest = resp.Header.Get("ETag"), best
	c.mu.Unlock()
	return best, nil
}

// Parse reads X.Y.Z from runner-vX.Y.Z, vX.Y.Z, or X.Y.Z. A development
// build, such as abc1234-dirty or runner-v1.2.0-3-gabc1234, does not parse.
func Parse(tag string) (string, bool) {
	version := strings.TrimPrefix(strings.TrimPrefix(tag, TagPrefix), "v")
	parts := strings.Split(version, ".")
	if len(parts) != 3 {
		return "", false
	}
	for _, part := range parts {
		if _, err := strconv.Atoi(part); err != nil || part == "" {
			return "", false
		}
	}
	return version, true
}

// Newer says whether version a is later than b. Both are X.Y.Z.
func Newer(a, b string) bool {
	left, right := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < 3; i++ {
		x, _ := strconv.Atoi(left[i])
		y, _ := strconv.Atoi(right[i])
		if x != y {
			return x > y
		}
	}
	return false
}

// AssetName is this platform's binary in a release.
func AssetName(goos, goarch string) string {
	name := "lbw-runner-" + goos + "-" + goarch
	if goos == "windows" {
		name += ".exe"
	}
	return name
}

// Fetch downloads this platform's binary from rel into dir, and returns its
// path only once SHA256SUMS is signed by key and the binary matches its sum.
func Fetch(ctx context.Context, client *http.Client, rel *Release, key ed25519.PublicKey, dir string) (string, error) {
	if len(key) != ed25519.PublicKeySize {
		return "", errors.New("this build has no release key, so it cannot check an update")
	}
	name := AssetName(runtime.GOOS, runtime.GOARCH)
	for _, needed := range []string{name, "SHA256SUMS", "SHA256SUMS.sig"} {
		if rel.assets[needed] == "" {
			return "", fmt.Errorf("%s has no %s", rel.Tag, needed)
		}
	}
	sums, err := get(ctx, client, rel.assets["SHA256SUMS"], 1<<20)
	if err != nil {
		return "", err
	}
	signature, err := get(ctx, client, rel.assets["SHA256SUMS.sig"], 4<<10)
	if err != nil {
		return "", err
	}
	if err := Verify(key, sums, signature); err != nil {
		return "", fmt.Errorf("%s: %w", rel.Tag, err)
	}
	want, err := listedSum(sums, name)
	if err != nil {
		return "", fmt.Errorf("%s: %w", rel.Tag, err)
	}
	binary, err := get(ctx, client, rel.assets[name], MaxBinary)
	if err != nil {
		return "", err
	}
	got := sha256.Sum256(binary)
	if hex.EncodeToString(got[:]) != want {
		return "", fmt.Errorf("%s: the downloaded %s does not match its signed sum", rel.Tag, name)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	path := filepath.Join(dir, name+"."+rel.Version)
	if err := os.WriteFile(path, binary, 0o755); err != nil {
		return "", err
	}
	return path, nil
}

// Verify checks that signature is the release key's Ed25519 signature of
// sums. The signature file holds it base64 encoded.
func Verify(key ed25519.PublicKey, sums, signature []byte) error {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(signature)))
	if err != nil || len(raw) != ed25519.SignatureSize {
		return errors.New("the release signature is not readable")
	}
	if !ed25519.Verify(key, sums, raw) {
		return errors.New("the release is not signed by LBW's release key")
	}
	return nil
}

func listedSum(sums []byte, name string) (string, error) {
	scanner := bufio.NewScanner(bytes.NewReader(sums))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name {
			return strings.ToLower(fields[0]), nil
		}
	}
	return "", fmt.Errorf("SHA256SUMS does not list %s", name)
}

func get(ctx context.Context, client *http.Client, url string, limit int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "lbw-runner")
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("downloading the update: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("downloading the update: %d from %s", resp.StatusCode, req.URL.Host)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("the download is larger than a runner release")
	}
	return data, nil
}

// Replace puts the verified binary at target. The running program keeps its
// old file open, so it carries on until it restarts. On Windows, where a
// running program cannot be overwritten, the old one is moved aside first.
func Replace(downloaded, target string) error {
	if runtime.GOOS == "windows" {
		old := target + ".old"
		_ = os.Remove(old)
		if err := os.Rename(target, old); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if err := os.Rename(downloaded, target); err != nil {
		return fmt.Errorf("could not put the update in place: %w", err)
	}
	return os.Chmod(target, 0o755)
}
