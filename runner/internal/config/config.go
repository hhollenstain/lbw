// Package config keeps the runner's settings and its token.
//
// Settings live in config.json under the runner's home folder. The token is kept
// in the operating system's secret store when one is available (the macOS
// Keychain, or the Secret Service on Linux) and otherwise in a file only the
// user can read. LBW_RUNNER_TOKEN and LBW_SERVER override both, for containers.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

const service = "lbw-runner"

// Config is what `lbw-runner login` writes and `lbw-runner run` reads.
type Config struct {
	Server   string `json:"server"`
	RunnerID string `json:"runner_id,omitempty"`
	Name     string `json:"name,omitempty"`
	// CAFile trusts an extra certificate authority, such as a local development CA.
	CAFile string `json:"ca_file,omitempty"`
	// WorkDir holds cloned repositories and one git worktree per job.
	WorkDir string `json:"work_dir,omitempty"`
	// Repos maps "owner/repo" or a project name to a checkout already on this machine.
	Repos map[string]string `json:"repos,omitempty"`
	// ClaudePath is the claude program. Empty means the one on PATH.
	ClaudePath string `json:"claude_path,omitempty"`
	// PermissionMode is passed to claude --permission-mode.
	PermissionMode string `json:"permission_mode,omitempty"`
	// AllowedTools is passed to claude --allowedTools.
	AllowedTools []string `json:"allowed_tools,omitempty"`
	// Sandbox runs Claude Code's shell commands in its sandbox and approves any
	// command there without asking. Writes stay in the job's worktree and the
	// network reaches only SandboxDomains. Unset means on, except on Windows,
	// where Claude Code has no sandbox.
	Sandbox *bool `json:"sandbox,omitempty"`
	// SandboxDomains are the hosts sandboxed commands may reach. Unset means
	// DefaultSandboxDomains, the common package registries.
	SandboxDomains []string `json:"sandbox_domains,omitempty"`
	// MaxTurns limits one job's agent turns. Zero means Claude Code's default.
	MaxTurns int `json:"max_turns,omitempty"`
	// Model asks Claude Code for a model when the job does not name one. Empty
	// means the account's default.
	Model string `json:"model,omitempty"`
	// NoLLMRelay stops this runner from relaying model calls to the owner's
	// model server, such as Ollama.
	NoLLMRelay bool `json:"no_llm_relay,omitempty"`
	// LLMHosts are host names the runner may relay model calls to besides
	// this machine and the private network, such as a server on the internet.
	LLMHosts []string `json:"llm_hosts,omitempty"`
	// LLMPrivateNetwork, when false, stops the relay from reaching private
	// network addresses: only this machine and LLMHosts. Unset means true.
	LLMPrivateNetwork *bool `json:"llm_private_network,omitempty"`
	// TokenStore is "keychain", "secret-service", or "file". Set at login.
	TokenStore string `json:"token_store,omitempty"`
	// DashboardPort is where the runner's own page listens on 127.0.0.1.
	// Zero means 7317.
	DashboardPort int `json:"dashboard_port,omitempty"`
	// NoDashboard turns the runner's own page off, such as in a container.
	NoDashboard bool `json:"no_dashboard,omitempty"`
	// ReleasesURL is where the runner looks for new releases: a GitHub releases
	// list, or a mirror of one. Empty means LBW's own releases. A release is
	// installed only when it is signed by LBW's release key, wherever it came from.
	ReleasesURL string `json:"releases_url,omitempty"`
	// NoUpdateCheck stops the daily check for a newer runner.
	NoUpdateCheck bool `json:"no_update_check,omitempty"`

	home string
}

// DefaultAllowedTools lets Claude Code read and edit the worktree and run git
// read commands. Add a test command, such as "Bash(make test:*)", in config.json.
var DefaultAllowedTools = []string{
	"Read", "Edit", "Write", "Glob", "Grep", "MultiEdit",
	"Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
}

// DefaultSandboxDomains let a sandboxed build install packages and fetch modules.
var DefaultSandboxDomains = []string{
	"pypi.org", "files.pythonhosted.org",
	"registry.npmjs.org", "registry.yarnpkg.com",
	"proxy.golang.org", "sum.golang.org",
	"crates.io", "static.crates.io", "index.crates.io",
	"rubygems.org",
	"github.com", "codeload.github.com", "objects.githubusercontent.com",
}

// SandboxOn says whether jobs run shell commands in Claude Code's sandbox.
// LBW_RUNNER_SANDBOX=off or on overrides config.json, for containers, where the
// container is the boundary.
func (c *Config) SandboxOn() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("LBW_RUNNER_SANDBOX"))) {
	case "off", "false", "0":
		return false
	case "on", "true", "1":
		return true
	}
	if c.Sandbox != nil {
		return *c.Sandbox
	}
	return runtime.GOOS != "windows"
}

// SandboxSettings is the --settings value that turns the sandbox on for one
// run: any shell command is approved inside it, a command can never leave it,
// and Claude Code stops instead of running unsandboxed when it cannot start.
func (c *Config) SandboxSettings() string {
	domains := c.SandboxDomains
	if len(domains) == 0 {
		domains = DefaultSandboxDomains
	}
	data, _ := json.Marshal(map[string]any{
		"sandbox": map[string]any{
			"enabled":                  true,
			"autoAllowBashIfSandboxed": true,
			"allowUnsandboxedCommands": false,
			"failIfUnavailable":        true,
			"network":                  map[string]any{"allowedDomains": domains},
		},
	})
	return string(data)
}

// Home is the runner's folder: LBW_RUNNER_HOME, or lbw-runner in the user config folder.
func Home() (string, error) {
	if dir := strings.TrimSpace(os.Getenv("LBW_RUNNER_HOME")); dir != "" {
		return dir, nil
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, service), nil
}

// Load reads config.json. A missing file is an empty config, not an error.
func Load() (*Config, error) {
	home, err := Home()
	if err != nil {
		return nil, err
	}
	cfg := &Config{home: home}
	data, err := os.ReadFile(filepath.Join(home, "config.json"))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if len(data) > 0 {
		if err := json.Unmarshal(data, cfg); err != nil {
			return nil, fmt.Errorf("config.json: %w", err)
		}
	}
	if env := strings.TrimSpace(os.Getenv("LBW_SERVER")); env != "" {
		cfg.Server = env
	}
	cfg.Server = strings.TrimRight(cfg.Server, "/")
	if cfg.WorkDir == "" {
		cfg.WorkDir = filepath.Join(home, "work")
	}
	if cfg.PermissionMode == "" {
		cfg.PermissionMode = "acceptEdits"
	}
	if len(cfg.AllowedTools) == 0 {
		cfg.AllowedTools = append([]string(nil), DefaultAllowedTools...)
	}
	return cfg, nil
}

// DefaultDashboardPort is where the dashboard listens unless dashboard_port says otherwise.
const DefaultDashboardPort = 7317

// Dashboard is the dashboard's port, or 0 when it is off. LBW_RUNNER_DASHBOARD=off
// turns it off, for containers.
func (c *Config) Dashboard() int {
	if c.NoDashboard || strings.EqualFold(strings.TrimSpace(os.Getenv("LBW_RUNNER_DASHBOARD")), "off") {
		return 0
	}
	if c.DashboardPort > 0 {
		return c.DashboardPort
	}
	return DefaultDashboardPort
}

// HomeDir is where this config was loaded from.
func (c *Config) HomeDir() string { return c.home }

// Save writes config.json, readable only by the user.
func (c *Config) Save() error {
	if err := os.MkdirAll(c.home, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(c.home, "config.json"), append(data, '\n'), 0o600)
}

// Token returns the runner token, or "" when the runner is not linked.
func (c *Config) Token() string {
	if env := strings.TrimSpace(os.Getenv("LBW_RUNNER_TOKEN")); env != "" {
		return env
	}
	switch c.TokenStore {
	case "keychain":
		out, err := exec.Command("security", "find-generic-password", "-s", service, "-a", c.Server, "-w").Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	case "secret-service":
		out, err := exec.Command("secret-tool", "lookup", "service", service, "account", c.Server).Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	}
	data, err := os.ReadFile(c.tokenFile())
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// SaveToken keeps the token in the best store this machine has.
func (c *Config) SaveToken(token string) error {
	if runtime.GOOS == "darwin" && have("security") {
		err := exec.Command("security", "add-generic-password", "-U", "-s", service, "-a", c.Server, "-w", token).Run()
		if err == nil {
			c.TokenStore = "keychain"
			return nil
		}
	}
	if runtime.GOOS == "linux" && have("secret-tool") {
		cmd := exec.Command("secret-tool", "store", "--label=LBW runner", "service", service, "account", c.Server)
		cmd.Stdin = strings.NewReader(token)
		if cmd.Run() == nil {
			c.TokenStore = "secret-service"
			return nil
		}
	}
	if err := os.MkdirAll(c.home, 0o700); err != nil {
		return err
	}
	c.TokenStore = "file"
	return os.WriteFile(c.tokenFile(), []byte(token+"\n"), 0o600)
}

// ForgetToken removes the token from wherever it was kept.
func (c *Config) ForgetToken() {
	switch c.TokenStore {
	case "keychain":
		_ = exec.Command("security", "delete-generic-password", "-s", service, "-a", c.Server).Run()
	case "secret-service":
		_ = exec.Command("secret-tool", "clear", "service", service, "account", c.Server).Run()
	}
	_ = os.Remove(c.tokenFile())
	c.TokenStore = ""
	c.RunnerID = ""
}

func (c *Config) tokenFile() string { return filepath.Join(c.home, "token") }

func have(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}
