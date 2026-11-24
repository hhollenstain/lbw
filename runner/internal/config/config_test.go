package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestConfigRoundTripsWithSafeDefaults(t *testing.T) {
	home := t.TempDir()
	t.Setenv("LBW_RUNNER_HOME", home)
	t.Setenv("LBW_SERVER", "")
	t.Setenv("LBW_RUNNER_TOKEN", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PermissionMode != "acceptEdits" || len(cfg.AllowedTools) == 0 || cfg.WorkDir != filepath.Join(home, "work") {
		t.Fatalf("defaults = %+v", cfg)
	}
	cfg.Server = "https://lbw.example.com/"
	cfg.Repos = map[string]string{"o/r": "/src/r"}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(home, "config.json"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("config.json mode = %v %v", info, err)
	}
	again, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if again.Server != "https://lbw.example.com" || again.Repos["o/r"] != "/src/r" {
		t.Fatalf("loaded = %+v", again)
	}
}

func TestEnvironmentOverridesForContainers(t *testing.T) {
	t.Setenv("LBW_RUNNER_HOME", t.TempDir())
	t.Setenv("LBW_SERVER", "https://lbw.internal")
	t.Setenv("LBW_RUNNER_TOKEN", "lbwr_env")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Server != "https://lbw.internal" || cfg.Token() != "lbwr_env" {
		t.Fatalf("cfg = %+v token=%q", cfg, cfg.Token())
	}
}

func TestATokenFileIsPrivate(t *testing.T) {
	home := t.TempDir()
	t.Setenv("LBW_RUNNER_HOME", home)
	t.Setenv("LBW_RUNNER_TOKEN", "")
	cfg, _ := Load()
	cfg.TokenStore = "file"
	if err := os.WriteFile(cfg.tokenFile(), []byte("lbwr_file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if cfg.Token() != "lbwr_file" {
		t.Fatalf("token = %q", cfg.Token())
	}
	cfg.ForgetToken()
	if cfg.Token() != "" {
		t.Fatal("token was not forgotten")
	}
}

func TestTheSandboxIsOnAndCannotBeLeft(t *testing.T) {
	t.Setenv("LBW_RUNNER_SANDBOX", "")
	cfg := &Config{}
	if runtime.GOOS != "windows" && !cfg.SandboxOn() {
		t.Fatal("the sandbox is off by default")
	}
	var got map[string]map[string]any
	if err := json.Unmarshal([]byte(cfg.SandboxSettings()), &got); err != nil {
		t.Fatal(err)
	}
	box := got["sandbox"]
	if box["enabled"] != true || box["autoAllowBashIfSandboxed"] != true || box["allowUnsandboxedCommands"] != false || box["failIfUnavailable"] != true {
		t.Fatalf("sandbox = %v", box)
	}
	domains := box["network"].(map[string]any)["allowedDomains"].([]any)
	if len(domains) != len(DefaultSandboxDomains) || domains[0] != "pypi.org" {
		t.Fatalf("domains = %v", domains)
	}
	off := false
	cfg = &Config{Sandbox: &off, SandboxDomains: []string{"example.com"}}
	if cfg.SandboxOn() || !strings.Contains(cfg.SandboxSettings(), `"example.com"`) {
		t.Fatalf("config = %+v", cfg)
	}
}

func TestAContainerTurnsTheSandboxOff(t *testing.T) {
	on := true
	t.Setenv("LBW_RUNNER_SANDBOX", "off")
	if (&Config{Sandbox: &on}).SandboxOn() {
		t.Fatal("LBW_RUNNER_SANDBOX=off did not win")
	}
}
