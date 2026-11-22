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
	"strings"
	"syscall"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/claude"
	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/config"
	"github.com/hhollenstain/lbw/runner/internal/daemon"
	"github.com/hhollenstain/lbw/runner/internal/job"
	"github.com/hhollenstain/lbw/runner/internal/relay"
	"github.com/hhollenstain/lbw/runner/internal/service"
)

var version = "dev"

const usage = `lbw-runner runs your LBW jobs on this machine with your own Claude Code.

Commands:
  login --server URL [--name NAME] [--ca-file PEM]   link this machine to your LBW account
  run                                               wait for jobs and run them
  status                                            show the link and the Claude Code version
  doctor                                            check git, Claude Code, and the server
  update-claude                                     run claude update
  install-service                                   start at login (launchd or systemd --user)
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
	if *server != "" {
		cfg.Server = strings.TrimRight(*server, "/")
	}
	if *caFile != "" {
		abs, err := filepath.Abs(*caFile)
		if err != nil {
			return err
		}
		cfg.CAFile = abs
	}
	if cfg.Server == "" {
		return errors.New("pass --server with your LBW address")
	}
	api, err := client.New(cfg.Server, "", cfg.CAFile)
	if err != nil {
		return err
	}
	link, err := api.StartLink(ctx, *name, runtime.GOOS+"/"+runtime.GOARCH)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Open %s\nand enter this code under Integrations → Your machines:\n\n    %s\n\nWaiting for approval…\n", link.VerificationURL, link.UserCode)
	interval := time.Duration(max(link.Interval, 1)) * time.Second
	deadline := time.Now().Add(time.Duration(link.ExpiresIn) * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
		claimed, err := api.ClaimToken(ctx, link.DeviceCode)
		if errors.Is(err, client.ErrPending) {
			continue
		}
		if err != nil {
			return err
		}
		cfg.RunnerID = claimed.RunnerID
		cfg.Name = *name
		if err := cfg.SaveToken(claimed.Token); err != nil {
			return fmt.Errorf("could not keep the token: %w", err)
		}
		if err := cfg.Save(); err != nil {
			return err
		}
		fmt.Fprintf(out, "Linked %s. The token is in your %s.\nStart it with `lbw-runner run`, or `lbw-runner install-service` to start at login.\n", *name, storeName(cfg.TokenStore))
		return nil
	}
	return errors.New("the code expired: run login again")
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
	cfg, api, jobs, err := setup()
	logger := log.New(errOut, "", log.LstdFlags)
	if err != nil {
		// Without Claude Code the runner can still relay model calls to a local server.
		if jobs != nil || api == nil || cfg.NoLLMRelay {
			return err
		}
		logger.Printf("%v: relaying model calls only", err)
	}
	d := &daemon.Daemon{API: api, Jobs: jobs, Version: version, Log: logger}
	if !cfg.NoLLMRelay {
		d.Relay = relay.New(api, cfg.LLMHosts, logger)
		d.Relay.PrivateNetwork = cfg.LLMPrivateNetwork == nil || *cfg.LLMPrivateNetwork
	}
	logger.Printf("lbw-runner %s connected to %s", version, api.Server)
	err = d.Run(ctx)
	if errors.Is(err, client.ErrUnauthorized) {
		// Exit cleanly so launchd or systemd does not restart a disconnected runner.
		logger.Printf("%v", err)
		return nil
	}
	return err
}

func status(ctx context.Context, out io.Writer) error {
	cfg, api, jobs, err := setup()
	if cfg != nil {
		fmt.Fprintf(out, "server:     %s\nname:       %s\ntoken:      %s\nwork dir:   %s\n", cfg.Server, cfg.Name, present(cfg.Token() != "", storeName(cfg.TokenStore)), cfg.WorkDir)
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
	if api != nil {
		_, herr := api.Heartbeat(ctx, client.Facts{Version: version, Platform: runtime.GOOS + "/" + runtime.GOARCH})
		check("LBW "+cfg.Server, herr, "reachable, runner linked")
	}
	if problems > 0 {
		return fmt.Errorf("%d problem(s)", problems)
	}
	return nil
}

func updateClaude(ctx context.Context, out io.Writer) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	program, err := claude.Program(cfg.ClaudePath)
	if err != nil {
		return err
	}
	text, err := claude.Update(ctx, program)
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
