// Package app installs LBW Runner as an app a person opens like any other:
// LBW Runner.app in ~/Applications on macOS, a desktop entry on Linux, and a
// Start menu entry on Windows. The app holds its own copy of the runner, the
// runner starts at login, and opening the app shows the runner's dashboard.
package app

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Name is how the app is listed.
const Name = "LBW Runner"

// Layout is where the app's files go on this machine.
type Layout struct {
	// Program is the runner binary inside the app. The login service runs it.
	Program string
	// Shown is what the person opens: the .app, the desktop entry, or the Start menu entry.
	Shown string
	// Files are written by Install and removed by Uninstall, with their contents.
	Files []File
	// Remove are folders Uninstall deletes, such as the .app bundle.
	Remove []string
}

// File is one file of the app.
type File struct {
	Path string
	Data []byte
	Mode os.FileMode
}

// For lays out the app for this platform. home is the person's home folder.
func For(home, version string) (*Layout, error) {
	switch runtime.GOOS {
	case "darwin":
		bundle := filepath.Join(home, "Applications", Name+".app")
		contents := filepath.Join(bundle, "Contents")
		program := filepath.Join(contents, "MacOS", "lbw-runner")
		return &Layout{
			Program: program,
			Shown:   bundle,
			Files: []File{
				{filepath.Join(contents, "Info.plist"), []byte(infoPlist(version)), 0o644},
				{filepath.Join(contents, "MacOS", Name), []byte(launcher), 0o755},
				{filepath.Join(contents, "Resources", "AppIcon.icns"), ICNS(), 0o644},
			},
			Remove: []string{bundle},
		}, nil
	case "linux":
		data := os.Getenv("XDG_DATA_HOME")
		if data == "" {
			data = filepath.Join(home, ".local", "share")
		}
		folder := filepath.Join(data, "lbw-runner")
		program := filepath.Join(folder, "lbw-runner")
		entry := filepath.Join(data, "applications", "lbw-runner.desktop")
		return &Layout{
			Program: program,
			Shown:   entry,
			Files: []File{
				{entry, []byte(desktopEntry(program)), 0o644},
				// The tray icon starts with the desktop session, where it can be drawn.
				{filepath.Join(autostartDir(home), "lbw-runner-tray.desktop"), []byte(trayAutostart(program)), 0o644},
				{filepath.Join(data, "icons", "hicolor", "256x256", "apps", "lbw-runner.png"), IconPNG(256), 0o644},
			},
			Remove: []string{folder},
		}, nil
	case "windows":
		local := os.Getenv("LOCALAPPDATA")
		roaming := os.Getenv("APPDATA")
		if local == "" || roaming == "" {
			return nil, errors.New("LOCALAPPDATA and APPDATA are not set")
		}
		folder := filepath.Join(local, Name)
		program := filepath.Join(folder, "lbw-runner.exe")
		programs := filepath.Join(roaming, "Microsoft", "Windows", "Start Menu", "Programs")
		entry := filepath.Join(programs, Name+".cmd")
		return &Layout{
			Program: program,
			Shown:   entry,
			Files: []File{
				{entry, []byte(windowsLauncher(program, "open")), 0o644},
				// Windows has no per-person service here, so the runner starts from the Startup folder.
				{filepath.Join(programs, "Startup", Name+".cmd"), []byte(windowsLauncher(program, "open --background")), 0o644},
				{filepath.Join(folder, "lbw-runner.png"), IconPNG(256), 0o644},
			},
			Remove: []string{folder},
		}, nil
	}
	return nil, fmt.Errorf("LBW Runner cannot be installed as an app on %s", runtime.GOOS)
}

// Install copies the runner from source into the app and writes the app's files.
func (l *Layout) Install(source string) error {
	if err := copyProgram(source, l.Program); err != nil {
		return fmt.Errorf("could not copy the runner into the app: %w", err)
	}
	for _, file := range l.Files {
		if err := os.MkdirAll(filepath.Dir(file.Path), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(file.Path, file.Data, file.Mode); err != nil {
			return err
		}
	}
	if runtime.GOOS == "darwin" {
		// Spotlight and Launchpad list the app at once instead of on their next scan.
		lsregister := "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
		_ = exec.Command(lsregister, "-f", l.Shown).Run()
	}
	return nil
}

// Uninstall removes the app's files. Settings and the token in the runner folder stay.
func (l *Layout) Uninstall() []string {
	var removed []string
	for _, file := range l.Files {
		if os.Remove(file.Path) == nil {
			removed = append(removed, file.Path)
		}
	}
	for _, folder := range l.Remove {
		if _, err := os.Stat(folder); err == nil && os.RemoveAll(folder) == nil {
			removed = append(removed, folder)
		}
	}
	return removed
}

// copyProgram copies the binary next to its target and renames it in place,
// so a runner already running from the target keeps its old file.
func copyProgram(source, target string) error {
	if same(source, target) {
		return nil
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	temp := target + ".new"
	out, err := os.OpenFile(temp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(temp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(temp)
		return err
	}
	return os.Rename(temp, target)
}

func same(a, b string) bool {
	left, err1 := filepath.EvalSymlinks(a)
	right, err2 := filepath.EvalSymlinks(b)
	return err1 == nil && err2 == nil && left == right
}

// launcher is the app's executable on macOS. Opening the app shows the dashboard.
const launcher = `#!/bin/sh
exec "$(dirname "$0")/lbw-runner" open
`

func infoPlist(version string) string {
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>%[1]s</string>
  <key>CFBundleDisplayName</key><string>%[1]s</string>
  <key>CFBundleIdentifier</key><string>dev.lookbothways.lbw-runner.app</string>
  <key>CFBundleExecutable</key><string>%[1]s</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>%[2]s</string>
  <key>CFBundleVersion</key><string>%[2]s</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
`, Name, xmlText(version))
}

func desktopEntry(program string) string {
	return fmt.Sprintf(`[Desktop Entry]
Type=Application
Name=%s
Comment=Run your LBW jobs and local models on this machine
Exec="%s" open
Icon=lbw-runner
Terminal=false
Categories=Development;
`, Name, program)
}

func autostartDir(home string) string {
	if dir := os.Getenv("XDG_CONFIG_HOME"); dir != "" {
		return filepath.Join(dir, "autostart")
	}
	return filepath.Join(home, ".config", "autostart")
}

func trayAutostart(program string) string {
	return fmt.Sprintf(`[Desktop Entry]
Type=Application
Name=%s tray icon
Exec="%s" tray
Icon=lbw-runner
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
`, Name, program)
}

func windowsLauncher(program, args string) string {
	return fmt.Sprintf("@echo off\r\nstart \"\" \"%s\" %s\r\n", program, args)
}

func xmlText(text string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(text)
}

// Running checks that the runner answers on port and holds key: its health
// check signs a fresh nonce with the key, so another program on the port
// cannot pass for it and is never sent the key.
func Running(ctx context.Context, port int, key string, proof func(key, nonce string) string) (bool, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return false, err
	}
	nonce := hex.EncodeToString(buf)
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	url := fmt.Sprintf("http://127.0.0.1:%d/healthz?nonce=%s", port, nonce)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false, err
	}
	resp, err := (&http.Client{Transport: &http.Transport{Proxy: nil}}).Do(req)
	if err != nil {
		return false, nil
	}
	defer resp.Body.Close()
	var body struct {
		App   string `json:"app"`
		Proof string `json:"proof"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&body) != nil || body.App != "lbw-runner" {
		return false, fmt.Errorf("another program is using port %d: set dashboard_port in config.json", port)
	}
	if key == "" || body.Proof != proof(key, nonce) {
		return false, fmt.Errorf("a different runner is using port %d: stop it, or set dashboard_port in config.json", port)
	}
	return true, nil
}

// StartDetached starts program with args in the background, writing to
// logFile. It keeps running after the command that started it exits.
func StartDetached(program, logFile string, args ...string) error {
	if err := os.MkdirAll(filepath.Dir(logFile), 0o700); err != nil {
		return err
	}
	out, err := os.OpenFile(logFile, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer out.Close()
	cmd := exec.Command(program, args...)
	cmd.Stdout = out
	cmd.Stderr = out
	detach(cmd)
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

// Browse opens url in its own app window when a Chromium browser is
// installed, and in the default browser otherwise.
func Browse(url string) error {
	for _, command := range appWindows(url) {
		if err := exec.Command(command[0], command[1:]...).Start(); err == nil {
			return nil
		}
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	return cmd.Start()
}

// appWindows are the commands that open url as an app window, best first.
func appWindows(url string) [][]string {
	flag := "--app=" + url
	var found [][]string
	switch runtime.GOOS {
	case "darwin":
		home, _ := os.UserHomeDir()
		for _, browser := range []string{"Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"} {
			for _, root := range []string{"/Applications", filepath.Join(home, "Applications")} {
				if _, err := os.Stat(filepath.Join(root, browser+".app")); err == nil {
					found = append(found, []string{"open", "-na", browser, "--args", flag})
				}
			}
		}
	case "windows":
		found = append(found, []string{"cmd", "/c", "start", "", "msedge", flag})
	default:
		for _, browser := range []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"} {
			if path, err := exec.LookPath(browser); err == nil {
				found = append(found, []string{path, flag})
			}
		}
	}
	return found
}
