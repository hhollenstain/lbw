//go:build windows

package main

import (
	"os"

	"github.com/hhollenstain/lbw/runner/internal/app"
)

// restartSelf starts the program at path again and ends this process.
// Windows cannot replace a running process in place.
func restartSelf(path, logFile string) error {
	if err := app.StartDetached(path, logFile, os.Args[1:]...); err != nil {
		return err
	}
	os.Exit(0)
	return nil
}
