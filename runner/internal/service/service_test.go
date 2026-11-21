package service

import (
	"runtime"
	"strings"
	"testing"
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
