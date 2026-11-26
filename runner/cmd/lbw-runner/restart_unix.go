//go:build !windows

package main

import (
	"os"
	"syscall"
)

// restartSelf runs the program at path in place of this process, with the
// same arguments. The process id stays, so launchd or systemd keeps
// tracking it, and the dashboard's port is free again as the old one goes.
func restartSelf(path, _ string) error {
	return syscall.Exec(path, os.Args, os.Environ())
}
