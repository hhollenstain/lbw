// Package service installs the runner as a per-user background service:
// a LaunchAgent on macOS, a systemd user unit on Linux. It runs as the
// person, so Claude Code finds the same login it uses in their terminal.
package service

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const label = "dev.lookbothways.lbw-runner"

// Unit is a service file and the commands that start it.
type Unit struct {
	Path     string
	Contents string
	Start    []string
	Stop     []string
}

// For builds the service file for this platform. program is the runner binary.
func For(program, home, logDir string) (*Unit, error) {
	path := os.Getenv("PATH")
	switch runtime.GOOS {
	case "darwin":
		plist := filepath.Join(home, "Library", "LaunchAgents", label+".plist")
		return &Unit{
			Path:     plist,
			Contents: launchd(program, path, logDir),
			Start:    []string{"launchctl", "bootstrap", fmt.Sprintf("gui/%d", os.Getuid()), plist},
			Stop:     []string{"launchctl", "bootout", fmt.Sprintf("gui/%d/%s", os.Getuid(), label)},
		}, nil
	case "linux":
		unit := filepath.Join(home, ".config", "systemd", "user", "lbw-runner.service")
		return &Unit{
			Path:     unit,
			Contents: systemd(program, path),
			Start:    []string{"systemctl", "--user", "enable", "--now", "lbw-runner.service"},
			Stop:     []string{"systemctl", "--user", "disable", "--now", "lbw-runner.service"},
		}, nil
	}
	return nil, fmt.Errorf("installing a service is not supported on %s: run `lbw-runner run` instead", runtime.GOOS)
}

// Write saves the service file, readable only by the person.
func (u *Unit) Write() error {
	if err := os.MkdirAll(filepath.Dir(u.Path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(u.Path, []byte(u.Contents), 0o600)
}

func launchd(program, path, logDir string) string {
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>%s</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
`, label, xmlEscape(program), xmlEscape(path), xmlEscape(filepath.Join(logDir, "runner.log")), xmlEscape(filepath.Join(logDir, "runner.log")))
}

func systemd(program, path string) string {
	return fmt.Sprintf(`[Unit]
Description=LBW runner
After=network-online.target

[Service]
ExecStart=%s run
Environment=PATH=%s
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
`, program, path)
}

func xmlEscape(text string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(text)
}
