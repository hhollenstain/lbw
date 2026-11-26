// Command lbw-runner links this machine to a person's LBW account and runs
// their jobs here with their own Claude Code.
//
//	lbw-runner login --server https://lbw.example.com
//	lbw-runner run
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/app"
	"github.com/hhollenstain/lbw/runner/internal/claude"
	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/config"
	"github.com/hhollenstain/lbw/runner/internal/daemon"
	"github.com/hhollenstain/lbw/runner/internal/dashboard"
	"github.com/hhollenstain/lbw/runner/internal/job"
	"github.com/hhollenstain/lbw/runner/internal/monitor"
	"github.com/hhollenstain/lbw/runner/internal/relay"
	"github.com/hhollenstain/lbw/runner/internal/service"
	"github.com/hhollenstain/lbw/runner/internal/tray"
)

var version = "dev"

const usage = `lbw-runner runs your LBW jobs on this machine with your own Claude Code.

Commands:
  install-app                                       add the LBW Runner app and start it at login
  open [--background]                               start the runner if needed and show its dashboard
  tray                                              show the runner's icon in the menu bar or system tray
  update [--check]                                  install a newer signed release, or only check for one
  login --server URL [--name NAME] [--ca-file PEM]   link this machine to your LBW account
  run                                               wait for jobs and run them, with the dashboard
  status                                            show the link and the Claude Code version
  doctor                                            check git, Claude Code, and the server
  update-claude                                     run claude update
  install-service                                   start at login (launchd or systemd --user)
  uninstall-app                                     remove the app and stop starting at login
  logout                                            forget this machine's token
  version                                           print the version
`

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	code := dispatch(ctx, os.Args[1:], os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

func dispatch(ctx context.Context, args []string, out, errOut io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(errOut, usage)
		return 2
	}
	var err error
	switch args[0] {
	case "login":
		err = login(ctx, args[1:], out)
	case "run":
		err = run(ctx, errOut)
	case "status":
		err = status(ctx, out)
	case "doctor":
		err = doctor(ctx, out)
	case "update-claude":
		err = updateClaude(ctx, out)
	case "install-service":
		err = installService(out)
	case "install-app":
		err = installApp(ctx, out)
	case "uninstall-app":
		err = uninstallApp(out)
	case "open":
		err = openApp(ctx, args[1:], out)
	case "tray":
		err = runTray(ctx)
	case "update":
		err = updateRunner(ctx, args[1:], out)
	case "logout":
		err = logout(out)
	case "version", "--version", "-v":
		fmt.Fprintln(out, version)
	case "help", "--help", "-h":
		fmt.Fprint(out, usage)
	default:
		fmt.Fprintf(errOut, "unknown command %q\n\n%s", args[0], usage)
		return 2
	}
	if err != nil {
		fmt.Fprintln(errOut, "lbw-runner:", err)
		return 1
	}
	return 0
}

func login(ctx context.Context, args []string, out io.Writer) error {
	flags := flag.NewFlagSet("login", flag.ContinueOnError)
	server := flags.String("server", "", "LBW address, such as https://www-local.lookbothways.dev")
	host, _ := os.Hostname()
	name := flags.String("name", host, "how this machine is listed on your profile")
	caFile := flags.String("ca-file", "", "extra certificate authority (PEM) to trust")
	if err := flags.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if *caFile != "" {
		abs, err := filepath.Abs(*caFile)
		if err != nil {
			return err
		}
		cfg.CAFile = abs
	}
	link, err := startLink(ctx, cfg, *server, *name)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Open %s\nand enter this code under Integrations → Your machines:\n\n    %s\n\nWaiting for approval…\n", link.VerificationURL, link.UserCode)
	if err := link.Wait(ctx); err != nil {
		return err
	}
	fmt.Fprintf(out, "Linked %s. The token is in your %s.\nStart it with `lbw-runner install-app`, or `lbw-runner run`.\n", cfg.Name, storeName(cfg.TokenStore))
	return nil
}

// pendingLink is a link code waiting for the person to approve it on their profile.
type pendingLink struct {
	*client.Link
	cfg  *config.Config
	api  *client.Client
	name string
}

// startLink asks LBW for a link code. server may be empty to keep the saved one.
func startLink(ctx context.Context, cfg *config.Config, server, name string) (*pendingLink, error) {
	if server = strings.TrimRight(strings.TrimSpace(server), "/"); server != "" {
		if !strings.HasPrefix(server, "https://") && !strings.HasPrefix(server, "http://") {
			return nil, errors.New("the LBW address starts with https://")
		}
		cfg.Server = server
	}
	if cfg.Server == "" {
		return nil, errors.New("pass --server with your LBW address")
	}
	if strings.TrimSpace(name) == "" {
		name, _ = os.Hostname()
	}
	api, err := client.New(cfg.Server, "", cfg.CAFile)
	if err != nil {
		return nil, err
	}
	link, err := api.StartLink(ctx, name, runtime.GOOS+"/"+runtime.GOARCH)
	if err != nil {
		return nil, err
	}
	return &pendingLink{Link: link, cfg: cfg, api: api, name: name}, nil
}

// Wait polls until the person approves the code, then keeps the token.
func (p *pendingLink) Wait(ctx context.Context) error {
	interval := time.Duration(max(p.Interval, 1)) * time.Second
	deadline := time.Now().Add(time.Duration(p.ExpiresIn) * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
		claimed, err := p.api.ClaimToken(ctx, p.DeviceCode)
		if errors.Is(err, client.ErrPending) {
			continue
		}
		if err != nil {
			return err
		}
		p.cfg.RunnerID = claimed.RunnerID
		p.cfg.Name = p.name
		if err := p.cfg.SaveToken(claimed.Token); err != nil {
			return fmt.Errorf("could not keep the token: %w", err)
		}
		return p.cfg.Save()
	}
	return errors.New("the code expired: link again")
}

func setup() (*config.Config, *client.Client, *job.Runner, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, nil, nil, err
	}
	if cfg.Server == "" {
		return nil, nil, nil, errors.New("not linked: run `lbw-runner login --server URL`")
	}
	api, err := client.New(cfg.Server, cfg.Token(), cfg.CAFile)
	if err != nil {
		return nil, nil, nil, err
	}
	program, err := claude.Program(cfg.ClaudePath)
	if err != nil {
		return cfg, api, nil, err
	}
	return cfg, api, &job.Runner{Config: cfg, Claude: program}, nil
}

func run(ctx context.Context, errOut io.Writer) error {
	work := monitor.New()
	logger := log.New(io.MultiWriter(errOut, work), "", log.LstdFlags)
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	linked := make(chan struct{}, 1)
	lbw := &linkedLBW{refresh: make(chan struct{}, 1)}
	dash, err := startDashboard(ctx, cfg, work, logger, linked, lbw)
	if err != nil {
		return err
	}
	for ctx.Err() == nil {
		if cfg, err = config.Load(); err != nil {
			return err
		}
		if cfg.Server == "" || cfg.Token() == "" {
			if dash == nil {
				return errors.New("not linked: run `lbw-runner login --server URL`")
			}
			dash.Update(func(status *dashboard.Status) {
				if status.Connection != "disconnected" {
					status.Connection = "not_linked"
				}
			})
			logger.Printf("not linked: link this machine from the dashboard (`lbw-runner open`) or with `lbw-runner login`")
			if !waitForLink(ctx, linked, cfg.Token()) {
				return nil
			}
			continue
		}
		err = serve(ctx, cfg, work, dash, logger, lbw)
		if !errors.Is(err, client.ErrUnauthorized) {
			return err
		}
		logger.Printf("%v", err)
		if dash == nil {
			// Exit cleanly so launchd or systemd does not restart a disconnected runner.
			return nil
		}
		dash.Update(func(status *dashboard.Status) { status.Connection = "disconnected" })
		if !waitForLink(ctx, linked, cfg.Token()) {
			return nil
		}
	}
	return nil
}

// serve runs the daemon for one link, until ctx ends or LBW disconnects it.
func serve(ctx context.Context, cfg *config.Config, work *monitor.Monitor, dash *dashboard.Server, logger *log.Logger, lbw *linkedLBW) error {
	api, err := client.New(cfg.Server, cfg.Token(), cfg.CAFile)
	if err != nil {
		return err
	}
	if dash != nil {
		linkCtx, unlink := context.WithCancel(ctx)
		lbw.set(api)
		defer func() {
			unlink()
			lbw.set(nil)
			dash.SetInbox(nil, nil)
		}()
		go watchInbox(linkCtx, api, dash, lbw.refresh)
	}
	var jobs *job.Runner
	program, claudeErr := claude.Program(cfg.ClaudePath)
	if claudeErr == nil {
		jobs = &job.Runner{Config: cfg, Claude: program}
	} else if cfg.NoLLMRelay {
		return claudeErr
	} else {
		// Without Claude Code the runner can still relay model calls to a local server.
		logger.Printf("%v: relaying model calls only", claudeErr)
	}
	d := &daemon.Daemon{API: api, Jobs: jobs, Version: version, Log: logger, Monitor: work}
	if !cfg.NoLLMRelay {
		d.Relay = relay.New(api, cfg.LLMHosts, logger)
		d.Relay.PrivateNetwork = cfg.LLMPrivateNetwork == nil || *cfg.LLMPrivateNetwork
		d.Relay.Monitor = work
	}
	if dash != nil {
		facts := d.Facts(ctx)
		dash.Update(func(status *dashboard.Status) {
			status.Server, status.Name = cfg.Server, cfg.Name
			status.Connection = "connecting"
			status.Link = nil
			status.Relay = d.Relay != nil
			status.ClaudeVersion = facts.ClaudeVersion
			status.ClaudeError = ""
			if claudeErr != nil {
				status.ClaudeError = "Not installed: this runner only relays model calls"
			}
		})
		d.OnBeat = func(beat *client.Beat, err error) {
			dash.Update(func(status *dashboard.Status) {
				if err == nil {
					status.Connection = "online"
					// LBW's name for this machine, also when it was linked by LBW_RUNNER_TOKEN.
					if beat != nil && beat.Name != "" {
						status.Name = beat.Name
					}
				} else if !errors.Is(err, client.ErrUnauthorized) {
					status.Connection = "connecting"
				}
			})
		}
	}
	logger.Printf("lbw-runner %s connected to %s", version, api.Server)
	return d.Run(ctx)
}

// startDashboard serves the runner's page unless config.json turns it off.
func startDashboard(ctx context.Context, cfg *config.Config, work *monitor.Monitor, logger *log.Logger, linked chan<- struct{}, lbw *linkedLBW) (*dashboard.Server, error) {
	port := cfg.Dashboard()
	if port == 0 {
		return nil, nil
	}
	key, err := dashboard.LoadKey(cfg.HomeDir())
	if err != nil {
		return nil, fmt.Errorf("could not make the dashboard key: %w", err)
	}
	dash := &dashboard.Server{Monitor: work, Port: port, Key: key, Log: logger}
	dash.Update(func(status *dashboard.Status) {
		status.Version = version
		status.Platform = runtime.GOOS + "/" + runtime.GOARCH
		status.Connection = "connecting"
		status.Server, status.Name = cfg.Server, cfg.Name
		status.Relay = !cfg.NoLLMRelay
		status.Sandbox = cfg.SandboxOn()
	})
	dash.Actions = dashboard.Actions{
		Link: func(request context.Context, server, name string) error {
			return linkFromDashboard(ctx, request, dash, server, name, linked, logger)
		},
		UpdateClaude: claudeUpdate,
		StopTask: func(request context.Context, taskID string) (*client.Inbox, error) {
			api := lbw.get()
			if api == nil {
				return nil, errors.New("this machine is not linked")
			}
			return api.StopTask(request, taskID)
		},
		RefreshInbox: lbw.wake,
	}
	updates := newRunnerUpdates(cfg, work, dash, logger)
	dash.Actions.CheckUpdate = updates.check
	dash.Actions.InstallUpdate = updates.install
	dash.Actions.Restart = func() {
		path, err := executable()
		if err == nil {
			err = restartSelf(path, filepath.Join(cfg.HomeDir(), "runner.log"))
		}
		if err != nil {
			logger.Printf("could not restart into the update: %v. Quit and open LBW Runner again.", err)
		}
	}
	if cfg.NoUpdateCheck {
		logger.Printf("not checking for runner updates (no_update_check)")
	} else {
		go updates.watch(ctx)
	}
	go func() {
		// Show the Claude Code version before the machine is linked, too.
		program, err := claude.Program(cfg.ClaudePath)
		if err == nil {
			version, verr := claude.Version(ctx, program)
			dash.Update(func(status *dashboard.Status) {
				if status.ClaudeVersion == "" && verr == nil {
					status.ClaudeVersion = version
				}
			})
		}
	}()
	go func() {
		if err := dash.Serve(ctx); err != nil {
			logger.Printf("%v", err)
		}
	}()
	logger.Printf("dashboard on %s: open it with `lbw-runner open`", dashboard.Address(port))
	return dash, nil
}

// linkedLBW is the client for the current link, for the dashboard's actions.
type linkedLBW struct {
	mu      sync.Mutex
	api     *client.Client
	refresh chan struct{}
}

func (l *linkedLBW) set(api *client.Client) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.api = api
}

func (l *linkedLBW) get() *client.Client {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.api
}

// wake reads the inbox again now, as when the dashboard is opened.
func (l *linkedLBW) wake() {
	select {
	case l.refresh <- struct{}{}:
	default:
	}
}

// watchInbox reads the owner's inbox while someone looks at the dashboard or
// the tray. A runner nobody looks at does not ask, so thousands of idle
// runners add no load, and an unchanged inbox is an empty 304.
func watchInbox(ctx context.Context, api *client.Client, dash *dashboard.Server, refresh <-chan struct{}) {
	ticker := time.NewTicker(dashboard.PageInboxEvery)
	defer ticker.Stop()
	var last time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-refresh:
		case <-ticker.C:
			every := dash.InboxEvery()
			if every == 0 || time.Since(last) < every-time.Second {
				continue
			}
		}
		last = time.Now()
		inbox, err := api.Inbox(ctx)
		if ctx.Err() != nil {
			return
		}
		dash.SetInbox(inbox, err)
	}
}

// linkFromDashboard shows a link code on the dashboard and waits for approval
// in the background. The runner starts taking jobs once it is approved.
func linkFromDashboard(ctx, request context.Context, dash *dashboard.Server, server, name string, linked chan<- struct{}, logger *log.Logger) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	pending, err := startLink(request, cfg, server, name)
	if err != nil {
		return err
	}
	dash.Update(func(status *dashboard.Status) {
		status.Server = cfg.Server
		status.Link = &dashboard.Link{
			Code:    pending.UserCode,
			URL:     pending.VerificationURL,
			Expires: time.Now().Add(time.Duration(pending.ExpiresIn) * time.Second),
		}
	})
	go func() {
		if err := pending.Wait(ctx); err != nil {
			logger.Printf("linking failed: %v", err)
			dash.Update(func(status *dashboard.Status) {
				status.Link = &dashboard.Link{Error: err.Error()}
			})
			return
		}
		logger.Printf("linked %s to %s", cfg.Name, cfg.Server)
		dash.Update(func(status *dashboard.Status) {
			status.Link = nil
			status.Connection = "connecting"
			status.Server, status.Name = cfg.Server, cfg.Name
		})
		select {
		case linked <- struct{}{}:
		default:
		}
	}()
	return nil
}

// waitForLink waits for a link from the dashboard, or a new token saved by
// `lbw-runner login` in a terminal.
func waitForLink(ctx context.Context, linked <-chan struct{}, token string) bool {
	for {
		select {
		case <-ctx.Done():
			return false
		case <-linked:
			return true
		case <-time.After(5 * time.Second):
			if cfg, err := config.Load(); err == nil && cfg.Server != "" && cfg.Token() != "" && cfg.Token() != token {
				return true
			}
		}
	}
}

// claudeUpdate runs `claude update` with the configured program.
func claudeUpdate(ctx context.Context) (string, error) {
	cfg, err := config.Load()
	if err != nil {
		return "", err
	}
	program, err := claude.Program(cfg.ClaudePath)
	if err != nil {
		return "", err
	}
	return claude.Update(ctx, program)
}

func status(ctx context.Context, out io.Writer) error {
	cfg, api, jobs, err := setup()
	if cfg != nil {
		fmt.Fprintf(out, "server:     %s\nname:       %s\ntoken:      %s\nwork dir:   %s\n", cfg.Server, cfg.Name, present(cfg.Token() != "", storeName(cfg.TokenStore)), cfg.WorkDir)
		if port := cfg.Dashboard(); port > 0 {
			fmt.Fprintf(out, "dashboard:  %s (lbw-runner open)\n", dashboard.Address(port))
		}
		fmt.Fprintf(out, "runner:     %s\n", updateLine(ctx, cfg))
	}
	if err != nil {
		return err
	}
	d := &daemon.Daemon{API: api, Jobs: jobs, Version: version}
	facts := d.Facts(ctx)
	fmt.Fprintf(out, "claude:     %s %s\n", jobs.Claude, facts.ClaudeVersion)
	beat, err := api.Heartbeat(ctx, facts)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "linked as:  %s (%s)\n", beat.Name, beat.RunnerID)
	return nil
}

func doctor(ctx context.Context, out io.Writer) error {
	problems := 0
	check := func(name string, err error, detail string) {
		if err != nil {
			problems++
			fmt.Fprintf(out, "✗ %s: %v\n", name, err)
			return
		}
		fmt.Fprintf(out, "✓ %s %s\n", name, detail)
	}
	gitPath, err := exec.LookPath("git")
	check("git", err, gitPath)
	cfg, api, jobs, err := setup()
	if jobs != nil {
		ver, verr := claude.Version(ctx, jobs.Claude)
		check("Claude Code", verr, ver)
		if os.Getenv("ANTHROPIC_API_KEY") != "" {
			fmt.Fprintln(out, "  Claude Code will bill the ANTHROPIC_API_KEY in this environment")
		}
	} else {
		check("Claude Code", err, "")
	}
	if cfg != nil && cfg.SandboxOn() {
		check("sandbox", sandboxReady(), "on: shell commands run in Claude Code's sandbox")
	} else if cfg != nil {
		fmt.Fprintln(out, "  sandbox off: Claude Code may run only allowed_tools and the project's commands")
	}
	if api != nil {
		_, herr := api.Heartbeat(ctx, client.Facts{Version: version, Platform: runtime.GOOS + "/" + runtime.GOARCH})
		check("LBW "+cfg.Server, herr, "reachable, runner linked")
	}
	if problems > 0 {
		return fmt.Errorf("%d problem(s)", problems)
	}
	return nil
}

// sandboxReady checks what Claude Code's sandbox needs on this platform.
func sandboxReady() error {
	if runtime.GOOS != "linux" {
		return nil
	}
	for _, name := range []string{"bwrap", "socat"} {
		if _, err := exec.LookPath(name); err != nil {
			return fmt.Errorf("%s is not installed: install bubblewrap and socat, or set \"sandbox\": false", name)
		}
	}
	return nil
}

func updateClaude(ctx context.Context, out io.Writer) error {
	text, err := claudeUpdate(ctx)
	fmt.Fprintln(out, text)
	return err
}

func installService(out io.Writer) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	program, err := os.Executable()
	if err != nil {
		return err
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	unit, err := service.For(program, home, cfg.HomeDir())
	if err != nil {
		return err
	}
	if err := unit.Write(); err != nil {
		return err
	}
	fmt.Fprintf(out, "Wrote %s\nStart it now with:\n    %s\nStop it with:\n    %s\n", unit.Path, strings.Join(unit.Start, " "), strings.Join(unit.Stop, " "))
	return nil
}

func logout(out io.Writer) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	cfg.ForgetToken()
	if err := cfg.Save(); err != nil {
		return err
	}
	fmt.Fprintln(out, "Forgot this machine's token. Disconnect it on your profile too, so LBW stops sending it jobs.")
	return nil
}

// installApp adds LBW Runner where this platform lists apps, with its own copy
// of the runner, starts it at login, and opens its dashboard.
func installApp(ctx context.Context, out io.Writer) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.Dashboard() == 0 {
		return errors.New("the dashboard is off in config.json (no_dashboard), so the app has nothing to show")
	}
	source, err := os.Executable()
	if err != nil {
		return err
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	layout, err := app.For(home, version)
	if err != nil {
		return err
	}
	if err := layout.Install(source); err != nil {
		return err
	}
	fmt.Fprintf(out, "Added %s\n", layout.Shown)
	if unit, err := service.For(layout.Program, home, cfg.HomeDir()); err == nil {
		if err := unit.Write(); err != nil {
			return err
		}
		// Reload it, so a service written for an older copy runs this one.
		_ = exec.Command(unit.Stop[0], unit.Stop[1:]...).Run()
		if err := unit.Load(); err != nil {
			return fmt.Errorf("could not start the runner at login: %w", err)
		}
		fmt.Fprintf(out, "The runner starts at login (%s)\n", unit.Path)
	} else if runtime.GOOS != "windows" {
		return err
	}
	if !tray.Supported {
		fmt.Fprintln(out, "This build has no menu bar icon: build it with CGO_ENABLED=1 to get one.")
	} else if unit, err := service.TrayFor(layout.Program, home, cfg.HomeDir()); err == nil {
		if err := unit.Write(); err != nil {
			return err
		}
		stopTray(cfg)
		_ = exec.Command(unit.Stop[0], unit.Stop[1:]...).Run()
		if err := unit.Load(); err != nil {
			return fmt.Errorf("could not show the menu bar icon: %w", err)
		}
		fmt.Fprintln(out, "The LBW Runner icon is in your menu bar, and it starts at login")
	}
	fmt.Fprintln(out, "Opening the dashboard. Open LBW Runner any time to see it again.")
	return openApp(ctx, nil, out)
}

// uninstallApp stops the runner starting at login and removes the app.
// The settings and token stay: `lbw-runner logout` forgets the token.
func uninstallApp(out io.Writer) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	layout, err := app.For(home, version)
	if err != nil {
		return err
	}
	for _, unitFor := range []func(string, string, string) (*service.Unit, error){service.For, service.TrayFor} {
		if unit, err := unitFor(layout.Program, home, cfg.HomeDir()); err == nil {
			_ = exec.Command(unit.Stop[0], unit.Stop[1:]...).Run()
			if os.Remove(unit.Path) == nil {
				fmt.Fprintf(out, "Removed %s\n", unit.Path)
			}
		}
	}
	stopTray(cfg)
	removed := layout.Uninstall()
	for _, path := range removed {
		fmt.Fprintf(out, "Removed %s\n", path)
	}
	if len(removed) == 0 {
		fmt.Fprintln(out, "LBW Runner was not installed as an app.")
	}
	fmt.Fprintln(out, "Your settings and token stay. `lbw-runner logout` forgets the token.")
	return nil
}

// openApp starts the runner if it is not running, and shows its dashboard.
func openApp(ctx context.Context, args []string, out io.Writer) error {
	flags := flag.NewFlagSet("open", flag.ContinueOnError)
	background := flags.Bool("background", false, "start the runner without opening the dashboard")
	if err := flags.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	port := cfg.Dashboard()
	if port == 0 {
		return errors.New("the dashboard is off in config.json (no_dashboard)")
	}
	up, err := app.Running(ctx, port, readKey(cfg.HomeDir()), dashboard.Proof)
	if err != nil {
		return err
	}
	if !up {
		if err := startRunner(cfg); err != nil {
			return err
		}
		deadline := time.Now().Add(20 * time.Second)
		for !up && time.Now().Before(deadline) {
			time.Sleep(300 * time.Millisecond)
			if up, err = app.Running(ctx, port, readKey(cfg.HomeDir()), dashboard.Proof); err != nil {
				return err
			}
		}
		if !up {
			return fmt.Errorf("the runner did not start: see %s", filepath.Join(cfg.HomeDir(), "runner.log"))
		}
	}
	ensureTray(cfg)
	if *background {
		return nil
	}
	address := dashboard.Address(port) + "/?key=" + readKey(cfg.HomeDir())
	if err := app.Browse(address); err != nil {
		return fmt.Errorf("could not open a browser: open %s", dashboard.Address(port))
	}
	fmt.Fprintf(out, "Dashboard: %s\n", dashboard.Address(port))
	return nil
}

// startRunner starts the login service when it is installed, and otherwise
// starts `lbw-runner run` in the background.
func startRunner(cfg *config.Config) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	program, err := os.Executable()
	if err != nil {
		return err
	}
	if unit, err := service.For(program, home, cfg.HomeDir()); err == nil {
		if _, statErr := os.Stat(unit.Path); statErr == nil {
			if exec.Command(unit.Kick[0], unit.Kick[1:]...).Run() == nil || unit.Load() == nil {
				return nil
			}
		}
	}
	return app.StartDetached(program, filepath.Join(cfg.HomeDir(), "runner.log"), "run")
}

// runTray shows the runner's icon. Only one runs per person: a second one exits.
func runTray(ctx context.Context) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	port := cfg.Dashboard()
	if port == 0 {
		return errors.New("the dashboard is off in config.json (no_dashboard), so the tray has nothing to show")
	}
	pidFile := trayPidFile(cfg)
	if pid := readPid(pidFile); pid != os.Getpid() && app.Alive(pid) {
		return nil
	}
	if err := os.MkdirAll(cfg.HomeDir(), 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o600); err != nil {
		return err
	}
	defer func() {
		if readPid(pidFile) == os.Getpid() {
			_ = os.Remove(pidFile)
		}
	}()
	program, err := executable()
	if err != nil {
		return err
	}
	return tray.Run(ctx, tray.Options{
		Port: port,
		Key:  func() string { return readKey(cfg.HomeDir()) },
		OpenDashboard: func() {
			_ = openApp(context.Background(), nil, io.Discard)
		},
		Browse:      func(link string) { _ = app.Browse(link) },
		StartRunner: func() error { return startRunner(cfg) },
		Program:     program,
		Restart: func() {
			_ = os.Remove(pidFile)
			_ = restartSelf(program, filepath.Join(cfg.HomeDir(), "tray.log"))
		},
	})
}

// ensureTray shows the tray icon when it is not showing, from the app's own
// copy of the runner when the app is installed, so macOS keeps it out of the Dock.
func ensureTray(cfg *config.Config) {
	if !tray.Supported || app.Alive(readPid(trayPidFile(cfg))) {
		return
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return
	}
	program, err := os.Executable()
	if err != nil {
		return
	}
	if layout, err := app.For(home, version); err == nil {
		if _, err := os.Stat(layout.Program); err == nil {
			program = layout.Program
		}
	}
	if unit, err := service.TrayFor(program, home, cfg.HomeDir()); err == nil {
		if _, err := os.Stat(unit.Path); err == nil {
			if exec.Command(unit.Kick[0], unit.Kick[1:]...).Run() == nil || unit.Load() == nil {
				return
			}
		}
	}
	_ = app.StartDetached(program, filepath.Join(cfg.HomeDir(), "tray.log"), "tray")
}

// stopTray ends a tray started outside launchd, as when the app is removed.
func stopTray(cfg *config.Config) {
	if pid := readPid(trayPidFile(cfg)); pid != os.Getpid() && app.Alive(pid) {
		if process, err := os.FindProcess(pid); err == nil {
			_ = process.Signal(os.Interrupt)
		}
	}
}

func trayPidFile(cfg *config.Config) string {
	return filepath.Join(cfg.HomeDir(), "tray.pid")
}

func readPid(path string) int {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	pid, _ := strconv.Atoi(strings.TrimSpace(string(data)))
	return pid
}

func readKey(home string) string {
	data, err := os.ReadFile(filepath.Join(home, "dashboard.key"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func storeName(store string) string {
	switch store {
	case "keychain":
		return "macOS Keychain"
	case "secret-service":
		return "Secret Service keyring"
	case "file":
		return "token file (readable only by you)"
	}
	return "environment"
}

func present(ok bool, where string) string {
	if ok {
		return "saved in " + where
	}
	return "missing"
}
