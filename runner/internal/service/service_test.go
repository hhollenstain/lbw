package service

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestTheServiceRunsTheRunnerAsThePerson(t *testing.T) {
	unit, err := For("/usr/local/bin/lbw-runner", "/home/ada", "/home/ada/.config/lbw-runner")
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		if err == nil {
			t.Fatal("expected an unsupported-platform error")
		}
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(unit.Contents, "/usr/local/bin/lbw-runner") || !strings.HasPrefix(unit.Path, "/home/ada") {
		t.Fatalf("unit = %+v", unit)
	}
}

func TestTheTrayStartsInTheDesktopSessionAndStaysQuitWhenQuit(t *testing.T) {
	unit, err := TrayFor("/Users/ada/Applications/LBW Runner.app/Contents/MacOS/lbw-runner", "/Users/ada", "/tmp/lbw")
	if runtime.GOOS != "darwin" {
		if err == nil {
			t.Fatal("only macOS has a tray login item")
		}
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"<string>tray</string>", "<string>Aqua</string>", "<key>SuccessfulExit</key><false/>", "dev.lookbothways.lbw-runner.tray"} {
		if !strings.Contains(unit.Contents, want) {
			t.Fatalf("missing %q in %s", want, unit.Contents)
		}
	}
	if !strings.HasSuffix(unit.Path, "Library/LaunchAgents/dev.lookbothways.lbw-runner.tray.plist") {
		t.Fatalf("path = %s", unit.Path)
	}
}

func TestLoadingRetriesWhileLaunchdCatchesUp(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script")
	}
	dir := t.TempDir()
	tries := filepath.Join(dir, "tries")
	// Fails twice, as a bootstrap does while launchd finishes a bootout.
	script := filepath.Join(dir, "start.sh")
	_ = os.WriteFile(script, []byte("#!/bin/sh\necho x >> "+tries+"\n[ $(wc -l < "+tries+") -ge 3 ]\n"), 0o755)
	unit := &Unit{Start: []string{script}, Kick: []string{"/usr/bin/false"}}
	if err := unit.load(5, time.Millisecond); err != nil {
		t.Fatalf("load: %v", err)
	}
	if data, _ := os.ReadFile(tries); strings.Count(string(data), "x") != 3 {
		t.Fatalf("tries = %q", data)
	}
	never := &Unit{Start: []string{"/bin/sh", "-c", "echo Bootstrap failed: 5; exit 5"}, Kick: []string{"/usr/bin/false"}}
	if err := never.load(2, time.Millisecond); err == nil || !strings.Contains(err.Error(), "Bootstrap failed: 5") {
		t.Fatalf("err = %v", err)
	}
	loaded := &Unit{Start: []string{"/usr/bin/false"}, Kick: []string{"/usr/bin/true"}}
	if err := loaded.load(2, time.Millisecond); err != nil {
		t.Fatalf("a loaded service was not started: %v", err)
	}
}
