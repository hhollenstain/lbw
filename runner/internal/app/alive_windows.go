//go:build windows

package app

import "os"

// Alive says whether a process with this id is running. On Windows, finding
// a process opens it, which fails once it has exited.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	_ = process.Release()
	return true
}
