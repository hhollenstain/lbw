package app

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"image/png"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestTheAppHoldsItsOwnRunnerAndRemovesCleanly(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("app layout is checked on macOS and Linux")
	}
	home := t.TempDir()
	t.Setenv("XDG_DATA_HOME", "")
	source := filepath.Join(t.TempDir(), "lbw-runner")
	if err := os.WriteFile(source, []byte("#!/bin/sh\necho runner\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	layout, err := For(home, "1.2.3")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(layout.Program, home) || !strings.HasPrefix(layout.Shown, home) {
		t.Fatalf("layout outside home: %+v", layout)
	}
	if err := layout.Install(source); err != nil {
		t.Fatal(err)
	}
	copied, err := os.ReadFile(layout.Program)
	if err != nil || !strings.Contains(string(copied), "echo runner") {
		t.Fatalf("program not copied: %v", err)
	}
	if err := layout.Install(layout.Program); err != nil {
		t.Fatalf("installing again from the app's own copy: %v", err)
	}
	for _, file := range layout.Files {
		if _, err := os.Stat(file.Path); err != nil {
			t.Fatalf("missing %s", file.Path)
		}
	}
	if runtime.GOOS == "darwin" {
		plist, _ := os.ReadFile(filepath.Join(layout.Shown, "Contents", "Info.plist"))
		if !strings.Contains(string(plist), "<string>1.2.3</string>") || !strings.Contains(string(plist), "AppIcon") {
			t.Fatalf("Info.plist = %s", plist)
		}
		info, _ := os.Stat(filepath.Join(layout.Shown, "Contents", "MacOS", Name))
		if info.Mode().Perm()&0o100 == 0 {
			t.Fatal("the app's launcher is not executable")
		}
	} else {
		entry, _ := os.ReadFile(layout.Shown)
		if !strings.Contains(string(entry), `Exec="`+layout.Program+`" open`) {
			t.Fatalf("desktop entry = %s", entry)
		}
	}
	removed := layout.Uninstall()
	if len(removed) == 0 {
		t.Fatal("nothing was removed")
	}
	if _, err := os.Stat(layout.Program); !os.IsNotExist(err) {
		t.Fatalf("the program is still there: %v", err)
	}
}

func TestTheIconIsAValidPNGAndICNS(t *testing.T) {
	img, err := png.Decode(bytes.NewReader(IconPNG(64)))
	if err != nil || img.Bounds().Dx() != 64 {
		t.Fatalf("png: %v", err)
	}
	// The corner is outside the tile, and the centre is the green dot.
	if _, _, _, a := img.At(0, 0).RGBA(); a != 0 {
		t.Fatal("the corner is not transparent")
	}
	if r, g, _, _ := img.At(32, 32).RGBA(); g <= r {
		t.Fatal("the centre is not green")
	}
	icns := ICNS()
	if string(icns[:4]) != "icns" || int(binary.BigEndian.Uint32(icns[4:8])) != len(icns) {
		t.Fatal("bad icns header")
	}
	if string(icns[8:12]) != "ic09" {
		t.Fatalf("first entry %q", icns[8:12])
	}
}

func TestOpeningChecksItIsReallyThisRunner(t *testing.T) {
	proof := func(key, nonce string) string { return key + ":" + nonce }
	serve := func(answer func(nonce string) map[string]string) int {
		server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(answer(r.URL.Query().Get("nonce")))
		}))
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		server.Listener = listener
		server.Start()
		t.Cleanup(server.Close)
		_, port, _ := net.SplitHostPort(listener.Addr().String())
		number, _ := strconv.Atoi(port)
		return number
	}
	runner := serve(func(nonce string) map[string]string {
		return map[string]string{"app": "lbw-runner", "proof": proof("k", nonce)}
	})
	if up, err := Running(context.Background(), runner, "k", proof); !up || err != nil {
		t.Fatalf("runner: %v %v", up, err)
	}
	if up, err := Running(context.Background(), runner, "other", proof); up || err == nil {
		t.Fatal("a runner with another key passed")
	}
	stranger := serve(func(string) map[string]string { return map[string]string{"hello": "world"} })
	if up, err := Running(context.Background(), stranger, "k", proof); up || err == nil {
		t.Fatal("another program passed for the runner")
	}
	listener, _ := net.Listen("tcp", "127.0.0.1:0")
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	listener.Close()
	closed, _ := strconv.Atoi(port)
	if up, err := Running(context.Background(), closed, "k", proof); up || err != nil {
		t.Fatalf("nothing listening: %v %v", up, err)
	}
}

func TestTheTrayIconHasAMenuBarAndAColorForm(t *testing.T) {
	template, err := png.Decode(bytes.NewReader(TrayIcon(36, true, false)))
	if err != nil {
		t.Fatal(err)
	}
	// A template icon is black and transparent only, so macOS can tint it.
	for y := 0; y < 36; y++ {
		for x := 0; x < 36; x++ {
			if r, g, b, a := template.At(x, y).RGBA(); a > 0 && (r|g|b) != 0 {
				t.Fatalf("template pixel %d,%d has color", x, y)
			}
		}
	}
	// The centre is a ring at rest and a filled dot when something waits.
	if _, _, _, a := template.At(18, 18).RGBA(); a != 0 {
		t.Fatal("the resting dot is not a ring")
	}
	busy, _ := png.Decode(bytes.NewReader(TrayIcon(36, true, true)))
	if _, _, _, a := busy.At(18, 18).RGBA(); a == 0 {
		t.Fatal("the waiting dot is not filled")
	}
	ico := ICO(TrayIcon(32, false, true), 32)
	if binary.LittleEndian.Uint16(ico[2:4]) != 1 || binary.LittleEndian.Uint16(ico[4:6]) != 1 || ico[6] != 32 {
		t.Fatalf("bad ico header % x", ico[:8])
	}
	if !bytes.Equal(ico[22:30], []byte("\x89PNG\r\n\x1a\n")) {
		t.Fatal("the ico does not hold the png")
	}
}

func TestAliveTellsARunningProcessFromAGoneOne(t *testing.T) {
	if !Alive(os.Getpid()) {
		t.Fatal("this process is not alive")
	}
	if Alive(0) || Alive(-1) {
		t.Fatal("a missing pid is alive")
	}
}
