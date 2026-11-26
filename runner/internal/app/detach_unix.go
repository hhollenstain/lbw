//go:build !windows

package app

import (
	"os/exec"
	"syscall"
)

// detach starts the runner in its own session, so it outlives the app's launcher.
func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}
