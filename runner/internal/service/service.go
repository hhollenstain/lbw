// Package service installs the runner as a per-user background service:
// a LaunchAgent on macOS, a systemd user unit on Linux. It runs as the
// person, so Claude Code finds the same login it uses in their terminal.
package service

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const label = "dev.lookbothways.lbw-runner"

// Unit is a service file and the commands that start it.
type Unit struct {
	Path     string
	Contents string
	Start    []string
	Stop     []string
	// Kick starts the service when it is installed and loaded but not running.
	Kick []string
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
			Kick:     []string{"launchctl", "kickstart", fmt.Sprintf("gui/%d/%s", os.Getuid(), label)},
		}, nil
	case "linux":
		unit := filepath.Join(home, ".config", "systemd", "user", "lbw-runner.service")
		return &Unit{
			Path:     unit,
			Contents: systemd(program, path),
			Start:    []string{"systemctl", "--user", "enable", "--now", "lbw-runner.service"},
			Stop:     []string{"systemctl", "--user", "disable", "--now", "lbw-runner.service"},
			Kick:     []string{"systemctl", "--user", "start", "lbw-runner.service"},
		}, nil
	}
	return nil, fmt.Errorf("installing a service is not supported on %s: run `lbw-runner run` instead", runtime.GOOS)
}

// TrayFor is the login item that shows the tray icon. Only macOS has one:
// Linux starts it from the desktop's autostart folder, and Windows from the
// Startup folder, both written with the app.
func TrayFor(program, home, logDir string) (*Unit, error) {
	if runtime.GOOS != "darwin" {
		return nil, fmt.Errorf("the tray icon starts with the desktop on %s", runtime.GOOS)
	}
	plist := filepath.Join(home, "Library", "LaunchAgents", trayLabel+".plist")
	domain := fmt.Sprintf("gui/%d", os.Getuid())
	return &Unit{
		Path:     plist,
		Contents: trayLaunchd(program, logDir),
		Start:    []string{"launchctl", "bootstrap", domain, plist},
		Stop:     []string{"launchctl", "bootout", domain + "/" + trayLabel},
		Kick:     []string{"launchctl", "kickstart", domain + "/" + trayLabel},
	}, nil
}

const trayLabel = label + ".tray"

// trayLaunchd starts the tray in the person's desktop session. Quitting it
// exits cleanly, so launchd leaves it closed until the next login.
func trayLaunchd(program, logDir string) string {
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>tray</string></array>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
`, trayLabel, xmlEscape(program), xmlEscape(filepath.Join(logDir, "tray.log")), xmlEscape(filepath.Join(logDir, "tray.log")))
}

// Load starts the service, retrying for a few seconds. launchd finishes a
// bootout, and registers a plist just written, a moment after the command
// returns, and a bootstrap sent in that moment fails with "Bootstrap failed:
// 5". A service that turns out to be loaded already is started with Kick.
func (u *Unit) Load() error {
	return u.load(10, 500*time.Millisecond)
}

func (u *Unit) load(tries int, wait time.Duration) error {
	var out []byte
	var err error
	for try := 0; try < tries; try++ {
		if try > 0 {
			time.Sleep(wait)
		}
		out, err = exec.Command(u.Start[0], u.Start[1:]...).CombinedOutput()
		if err == nil {
			return nil
		}
	}
	if len(u.Kick) > 0 && exec.Command(u.Kick[0], u.Kick[1:]...).Run() == nil {
		return nil
	}
	return fmt.Errorf("%v %s", err, strings.TrimSpace(string(out)))
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
