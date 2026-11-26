package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/app"
	"github.com/hhollenstain/lbw/runner/internal/config"
	"github.com/hhollenstain/lbw/runner/internal/dashboard"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
	"github.com/hhollenstain/lbw/runner/internal/update"
)

// checkEvery is how often a runner looks for a newer release. The first look
// waits a random part of an hour, so the runners in one office do not all ask
// GitHub at once when they start in the morning.
const checkEvery = 24 * time.Hour

// runnerUpdates finds newer runner releases and installs one when the person
// asks. It never installs on its own.
type runnerUpdates struct {
	checker *update.Checker
	work    *monitor.Monitor
	dash    *dashboard.Server
	log     *log.Logger

	mu     sync.Mutex
	latest *update.Release
}

func newRunnerUpdates(cfg *config.Config, work *monitor.Monitor, dash *dashboard.Server, logger *log.Logger) *runnerUpdates {
	checker := update.NewChecker()
	if cfg.ReleasesURL != "" {
		checker.API = cfg.ReleasesURL
	}
	return &runnerUpdates{checker: checker, work: work, dash: dash, log: logger}
}

// watch checks once a day. A build from source does not check.
func (u *runnerUpdates) watch(ctx context.Context) {
	if _, ok := update.Parse(version); !ok {
		u.dash.Update(func(status *dashboard.Status) { status.Runner.Dev = true })
		return
	}
	wait := time.Minute + rand.N(time.Hour)
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		u.check(ctx)
		wait = checkEvery
	}
}

// check asks for the newest release and shows whether it is newer than this one.
func (u *runnerUpdates) check(ctx context.Context) {
	current, ok := update.Parse(version)
	if !ok {
		u.dash.Update(func(status *dashboard.Status) { status.Runner.Dev = true })
		return
	}
	rel, err := u.checker.Latest(ctx)
	now := time.Now()
	available := err == nil && rel != nil && update.Newer(rel.Version, current)
	u.mu.Lock()
	announce := available && (u.latest == nil || u.latest.Version != rel.Version)
	if err == nil {
		u.latest = rel
	}
	u.mu.Unlock()
	if announce {
		u.log.Printf("LBW Runner %s is available (this is %s): install it from the dashboard or with `lbw-runner update`", rel.Version, current)
	}
	u.dash.Update(func(status *dashboard.Status) {
		status.Runner.Checked = &now
		if err != nil {
			status.Runner.Error = err.Error()
			return
		}
		status.Runner.Error = ""
		status.Runner.Available = available
		status.Runner.Latest, status.Runner.URL = "", ""
		if rel != nil {
			status.Runner.Latest, status.Runner.URL = rel.Version, rel.URL
		}
	})
}

// install puts the newer release in place of this program. It refuses while
// work runs, because restarting would cut that work off.
func (u *runnerUpdates) install(ctx context.Context) error {
	state := u.work.Snapshot()
	for _, job := range state.Jobs {
		if job.Status == "running" {
			return errors.New("a job is running here: let it finish or stop it, then install the update")
		}
	}
	for _, call := range state.Calls {
		if call.Ended == nil {
			return errors.New("a local model call is in flight: try again in a moment")
		}
	}
	u.check(ctx)
	u.mu.Lock()
	rel := u.latest
	u.mu.Unlock()
	current, _ := update.Parse(version)
	if rel == nil || current == "" || !update.Newer(rel.Version, current) {
		return errors.New("no newer runner is published")
	}
	target, err := executable()
	if err != nil {
		return err
	}
	if err := fetchAndReplace(ctx, rel, target); err != nil {
		return err
	}
	u.log.Printf("installed LBW Runner %s", rel.Version)
	return nil
}

func fetchAndReplace(ctx context.Context, rel *update.Release, target string) error {
	client := &http.Client{Timeout: 10 * time.Minute}
	// Downloaded beside the program, so putting it in place is a rename on one disk.
	downloaded, err := update.Fetch(ctx, client, rel, update.PublicKey, filepath.Dir(target))
	if err != nil {
		return err
	}
	if err := update.Replace(downloaded, target); err != nil {
		_ = os.Remove(downloaded)
		return err
	}
	return nil
}

// executable is this program's real path, through any symlink.
func executable() (string, error) {
	path, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(path)
}

// updateRunner is `lbw-runner update`: check for a newer release, and install it.
func updateRunner(ctx context.Context, args []string, out io.Writer) error {
	flags := flag.NewFlagSet("update", flag.ContinueOnError)
	checkOnly := flags.Bool("check", false, "only say whether a newer runner is published")
	if err := flags.Parse(args); err != nil {
		return err
	}
	current, ok := update.Parse(version)
	if !ok {
		return fmt.Errorf("this is a build from source (%s), which does not update itself: build it again, or install a release from https://github.com/hhollenstain/lbw/releases", version)
	}
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	checker := update.NewChecker()
	if cfg.ReleasesURL != "" {
		checker.API = cfg.ReleasesURL
	}
	rel, err := checker.Latest(ctx)
	if err != nil {
		return err
	}
	if rel == nil || !update.Newer(rel.Version, current) {
		fmt.Fprintf(out, "LBW Runner %s is up to date.\n", current)
		return nil
	}
	fmt.Fprintf(out, "LBW Runner %s is available (this is %s): %s\n", rel.Version, current, rel.URL)
	if *checkOnly {
		return nil
	}
	// A running runner installs into its own program and restarts into it.
	if port := cfg.Dashboard(); port > 0 {
		key := readKey(cfg.HomeDir())
		if up, _ := app.Running(ctx, port, key, dashboard.Proof); up {
			if err := dashboardPost(ctx, port, key, "/api/update"); err != nil {
				return err
			}
			fmt.Fprintf(out, "Installed %s. The runner and its tray icon restart on it.\n", rel.Version)
			return nil
		}
	}
	target, err := executable()
	if err != nil {
		return err
	}
	if home, err := os.UserHomeDir(); err == nil {
		if layout, err := app.For(home, version); err == nil {
			if _, err := os.Stat(layout.Program); err == nil {
				target = layout.Program
			}
		}
	}
	if err := fetchAndReplace(ctx, rel, target); err != nil {
		return err
	}
	fmt.Fprintf(out, "Installed %s at %s. Start it with `lbw-runner open`.\n", rel.Version, target)
	return nil
}

// dashboardPost asks the running runner to do something, as its page does.
func dashboardPost(ctx context.Context, port int, key, path string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, dashboard.Address(port)+path, strings.NewReader("{}"))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: "lbw_runner", Value: key})
	resp, err := (&http.Client{Timeout: 11 * time.Minute, Transport: &http.Transport{Proxy: nil}}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var problem struct {
			Error string `json:"error"`
		}
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		_ = json.Unmarshal(data, &problem)
		if problem.Error == "" {
			problem.Error = fmt.Sprintf("the runner answered %d", resp.StatusCode)
		}
		return errors.New(problem.Error)
	}
	return nil
}

// updateLine is this runner's version, and whether a newer one is published.
func updateLine(ctx context.Context, cfg *config.Config) string {
	current, ok := update.Parse(version)
	if !ok {
		return version + " (built from source: does not update itself)"
	}
	checker := update.NewChecker()
	if cfg.ReleasesURL != "" {
		checker.API = cfg.ReleasesURL
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	rel, err := checker.Latest(ctx)
	switch {
	case err != nil:
		return current + " (could not check for updates: " + err.Error() + ")"
	case rel != nil && update.Newer(rel.Version, current):
		return current + " (" + rel.Version + " is available: lbw-runner update)"
	}
	return current + " (up to date)"
}
