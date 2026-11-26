//go:build darwin && !cgo

package tray

import (
	"context"
	"errors"
	"time"
)

// Options are what the tray needs from the runner.
type Options struct {
	Port          int
	Key           func() string
	OpenDashboard func()
	Browse        func(url string)
	StartRunner   func() error
	Every         time.Duration
	Program       string
	Restart       func()
}

// Supported says this build can show a tray icon. A macOS build without cgo
// cannot: the menu bar is drawn through AppKit.
const Supported = false

// Run reports that this build has no menu bar icon.
func Run(context.Context, Options) error {
	return errors.New("this macOS build has no menu bar icon: build it with CGO_ENABLED=1")
}
