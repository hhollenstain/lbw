// Package job runs one LBW job in its own git worktree on this machine.
//
// The worktree starts at the exact commit LBW chose. Claude Code works there.
// Whatever it changed is committed locally and sent back to LBW as a patch
// (git format-patch). The runner never pushes: LBW applies the commits,
// runs the project's tests, and publishes with the organization's GitHub App.
package job

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/hhollenstain/lbw/runner/internal/claude"
	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/config"
)

// MaxPatchBytes matches the largest change LBW accepts.
const MaxPatchBytes = 5 << 20

// DocumentLimit is the longest spec or plan the runner sends back. LBW keeps 40,000 characters.
const DocumentLimit = 40000

// PullRequestLimit is the longest build reply the runner sends back when LBW
// asks it to end with the pull request's title and description.
const PullRequestLimit = 12000

// readOnlyTools are the tools a spec or plan job turns off: every tool that can
// write, including the shell and subagents. It reads with Read, Glob, and Grep.
var readOnlyTools = []string{"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "Task", "Agent"}

// Runner runs jobs with one config. Claude is the program; tests swap it.
type Runner struct {
	Config *config.Config
	Claude string
}

// Run does one job and returns what to send back to LBW.
func (r *Runner) Run(ctx context.Context, job *client.Job, emit func(client.Event)) client.Result {
	if job.MaxSeconds > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(job.MaxSeconds)*time.Second)
		defer cancel()
	}
	repo, err := r.repository(ctx, job, emit)
	if err != nil {
		return failed(err)
	}
	tree := filepath.Join(r.Config.WorkDir, "jobs", safe(job.JobID))
	_ = os.MkdirAll(filepath.Dir(tree), 0o700)
	if _, err := git(ctx, repo, "worktree", "add", "--detach", tree, job.BaseSHA); err != nil {
		return failed(fmt.Errorf("could not make a worktree at %s: %w", short(job.BaseSHA), err))
	}
	defer func() {
		_, _ = git(context.Background(), repo, "worktree", "remove", "--force", tree)
		_, _ = git(context.Background(), repo, "worktree", "prune")
	}()
	emit(client.Event{Kind: "step", Message: fmt.Sprintf("Working in a fresh worktree at %s", short(job.BaseSHA))})
	start := job.BaseSHA
	if job.StartPatch != "" && !ReadOnly(job) {
		if err := applyStart(ctx, r.Config.WorkDir, tree, job); err != nil {
			return failed(err)
		}
		if head, err := git(ctx, tree, "rev-parse", "HEAD"); err == nil {
			start = strings.TrimSpace(head)
		}
		emit(client.Event{Kind: "step", Message: "Continuing from the work that was sent back"})
	}
	opts := claude.Options{
		Program:        r.Claude,
		Dir:            tree,
		Prompt:         Prompt(job),
		PermissionMode: r.Config.PermissionMode,
		AllowedTools:   AllowedTools(r.Config.AllowedTools, job),
		MaxTurns:       r.Config.MaxTurns,
		Model:          model(job, r.Config.Model),
	}
	if r.Config.SandboxOn() && !ReadOnly(job) {
		opts.Settings = r.Config.SandboxSettings()
	}
	if ReadOnly(job) {
		opts.DisallowedTools = readOnlyTools
		opts.ResultLimit = DocumentLimit
	} else if strings.TrimSpace(job.Instructions) != "" {
		opts.ResultLimit = PullRequestLimit
	}
	outcome := claude.Run(ctx, opts, emit)
	if !outcome.OK {
		return client.Result{Status: "failed", Summary: outcome.Summary, Error: outcome.Error}
	}
	if ReadOnly(job) {
		if strings.TrimSpace(outcome.Summary) == "" {
			return client.Result{Status: "failed", Error: "Claude Code did not write the " + job.Phase}
		}
		return client.Result{Status: "done", Summary: outcome.Summary}
	}
	patch, err := collect(ctx, tree, job, start)
	if err != nil {
		return client.Result{Status: "failed", Summary: outcome.Summary, Error: err.Error()}
	}
	return client.Result{Status: "done", Summary: outcome.Summary, Patch: patch}
}

// AllowedTools is the runner's allowlist plus, for a build, the project's own
// commands. A command with a newline or parentheses is left out, because it
// cannot be written as one Claude Code permission rule.
func AllowedTools(base []string, job *client.Job) []string {
	if ReadOnly(job) {
		return []string{"Read", "Glob", "Grep"}
	}
	tools := append([]string(nil), base...)
	for _, command := range job.Commands {
		command = strings.TrimSpace(command)
		if command == "" || len(command) > 300 || strings.ContainsAny(command, "()\n\r") {
			continue
		}
		tools = append(tools, "Bash("+command+")", "Bash("+command+":*)")
	}
	return tools
}

// ReadOnly says whether the job only reads the repository and answers with a
// spec or a plan for a person to accept.
func ReadOnly(job *client.Job) bool {
	return job.Phase == "spec" || job.Phase == "plan"
}

// Prompt is the instruction Claude Code receives. The repository's own
// CLAUDE.md and AGENTS.md still apply, because Claude Code reads them itself.
func Prompt(job *client.Job) string {
	var b strings.Builder
	if ReadOnly(job) {
		b.WriteString("You are writing the " + job.Phase + " for a job from LBW in a git worktree of this repository.\n")
		b.WriteString("Read the repository as much as you need. Do not change any files. ")
		b.WriteString("A person reads your final reply and accepts it before any work starts, ")
		b.WriteString("so your final reply is only the " + job.Phase + ", with nothing before or after it.\n\n")
		b.WriteString(strings.TrimSpace(job.Instructions))
		b.WriteString("\n")
		return b.String()
	}
	b.WriteString("You are working on a job from LBW in a git worktree of this repository.\n\n")
	b.WriteString("Goal:\n")
	b.WriteString(strings.TrimSpace(job.Goal))
	b.WriteString("\n")
	if spec := strings.TrimSpace(job.Spec); spec != "" {
		b.WriteString("\nA person accepted this spec")
		if job.SpecPath != "" {
			b.WriteString(" (" + job.SpecPath + ")")
		}
		b.WriteString(". It is the source of truth for the change. Build what it requires and prove each acceptance criterion. ")
		b.WriteString("Do not write the spec file: LBW commits it with your change.\n")
		b.WriteString(spec)
		b.WriteString("\n")
	}
	if plan := strings.TrimSpace(job.Plan); plan != "" {
		b.WriteString("\nA person accepted this plan. Follow it:\n")
		b.WriteString(plan)
		b.WriteString("\n")
	}
	if job.StartPatch != "" {
		b.WriteString("\nThe worktree already has the earlier work on this goal, committed. Continue from it.\n")
	}
	if notes := strings.TrimSpace(job.ReviewNotes); notes != "" {
		b.WriteString("\nA reviewer asked for these changes:\n")
		b.WriteString(notes)
		b.WriteString("\n")
	}
	b.WriteString("\nMake the change, keep it focused on the goal, and run the project's tests if you can. ")
	b.WriteString("Do not push, open pull requests, or change git remotes: LBW reviews and publishes the work. ")
	if finish := strings.TrimSpace(job.Instructions); finish != "" {
		b.WriteString("\n")
		b.WriteString(finish)
		b.WriteString("\n")
		return b.String()
	}
	b.WriteString("End with a short summary of what you changed and why.\n")
	return b.String()
}

func (r *Runner) repository(ctx context.Context, job *client.Job, emit func(client.Event)) (string, error) {
	if job.BaseSHA == "" {
		return "", errors.New("LBW did not say which commit to start from")
	}
	for _, key := range []string{job.Repository, job.Project} {
		if path, ok := r.Config.Repos[key]; ok && key != "" {
			if !hasCommit(ctx, path, job.BaseSHA) {
				_, _ = git(ctx, path, "fetch", "--quiet", "origin")
			}
			if !hasCommit(ctx, path, job.BaseSHA) {
				return "", fmt.Errorf("%s does not have commit %s: fetch or push it first", path, short(job.BaseSHA))
			}
			return path, nil
		}
	}
	if job.RemoteURL == "" {
		return "", fmt.Errorf("no checkout is set for %q: add it under repos in %s", firstNonEmpty(job.Repository, job.Project), filepath.Join(r.Config.HomeDir(), "config.json"))
	}
	cache := filepath.Join(r.Config.WorkDir, "repos", cacheName(job))
	if _, err := os.Stat(filepath.Join(cache, ".git")); err != nil {
		emit(client.Event{Kind: "step", Message: "Cloning " + job.RemoteURL})
		_ = os.MkdirAll(filepath.Dir(cache), 0o700)
		if _, err := git(ctx, "", "clone", "--quiet", "--no-checkout", job.RemoteURL, cache); err != nil {
			return "", fmt.Errorf("could not clone %s with this machine's git credentials: %w", job.RemoteURL, err)
		}
	} else if !hasCommit(ctx, cache, job.BaseSHA) {
		if _, err := git(ctx, cache, "fetch", "--quiet", "origin"); err != nil {
			return "", fmt.Errorf("could not fetch %s: %w", job.RemoteURL, err)
		}
	}
	if !hasCommit(ctx, cache, job.BaseSHA) {
		return "", fmt.Errorf("commit %s is not on %s: push it first", short(job.BaseSHA), job.RemoteURL)
	}
	return cache, nil
}

// applyStart puts the work a person sent back onto the fresh worktree, so the
// build continues from it. collect then sends it back with the new commits.
func applyStart(ctx context.Context, workDir, tree string, job *client.Job) error {
	if len(job.StartPatch) > MaxPatchBytes {
		return fmt.Errorf("the earlier work is %d bytes, more than LBW accepts", len(job.StartPatch))
	}
	file, err := os.CreateTemp(workDir, "start-*.mbox")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	_, err = file.WriteString(job.StartPatch)
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if _, err := gitAuthor(ctx, tree, "am", "--3way", "--keep-cr", file.Name()); err != nil {
		_, _ = git(context.Background(), tree, "am", "--abort")
		return fmt.Errorf("the earlier work did not apply: %w", err)
	}
	return nil
}

// collect commits whatever Claude Code left uncommitted and returns every
// commit since the base as one mailbox patch. A patch cannot carry a merge:
// format-patch would send the merged branch's own commits instead, and they
// do not apply. So when Claude Code merged or rebased past `start`, the commit
// LBW sent, everything after it goes as one commit holding the final files.
func collect(ctx context.Context, tree string, job *client.Job, start string) (string, error) {
	if _, err := git(ctx, tree, "add", "-A"); err != nil {
		return "", err
	}
	if _, err := git(ctx, tree, "diff", "--cached", "--quiet"); err != nil {
		message := commitMessage(job)
		if _, err := gitAuthor(ctx, tree, "commit", "--quiet", "--no-verify", "-m", message); err != nil {
			return "", fmt.Errorf("could not commit the change: %w", err)
		}
	}
	head := "HEAD"
	if !linear(ctx, tree, start) {
		message := commitMessage(job)
		flat, err := gitAuthor(ctx, tree, "commit-tree", "HEAD^{tree}", "-p", start, "-m", message)
		if err != nil {
			return "", fmt.Errorf("could not flatten the change: %w", err)
		}
		fields := strings.Fields(flat)
		if len(fields) == 0 {
			return "", errors.New("could not flatten the change: git wrote no commit")
		}
		head = fields[len(fields)-1]
	}
	patch, err := git(ctx, tree, "format-patch", "--binary", "--stdout", job.BaseSHA+".."+head)
	if err != nil {
		return "", err
	}
	if len(patch) > MaxPatchBytes {
		return "", fmt.Errorf("the change is %d bytes, more than LBW accepts", len(patch))
	}
	return patch, nil
}

func commitMessage(job *client.Job) string {
	title := strings.TrimSpace(strings.SplitN(strings.TrimSpace(job.Goal), "\n", 2)[0])
	if title == "" {
		title = "Change from LBW"
	}
	if len(title) > 72 {
		title = strings.TrimSpace(title[:72])
	}
	return title + "\n\nLBW-Job: " + job.TaskID + "\n"
}

func git(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return string(out), fmt.Errorf("git %s: %w: %s", args[0], err, strings.TrimSpace(string(out)))
	}
	return string(out), nil
}

// gitAuthor commits as the person's own git identity, with a fallback when
// this machine has none set.
// linear says whether HEAD is `start` plus ordinary commits on top of it:
// no merge, and `start` still in its history.
func linear(ctx context.Context, dir, start string) bool {
	if _, err := git(ctx, dir, "merge-base", "--is-ancestor", start, "HEAD"); err != nil {
		return false
	}
	merges, err := git(ctx, dir, "rev-list", "--merges", start+"..HEAD")
	return err == nil && strings.TrimSpace(merges) == ""
}

func gitAuthor(ctx context.Context, dir string, args ...string) (string, error) {
	if name, _ := git(ctx, dir, "config", "user.name"); strings.TrimSpace(name) == "" {
		args = append([]string{"-c", "user.name=LBW runner", "-c", "user.email=runner@lbw.invalid"}, args...)
	}
	return git(ctx, dir, args...)
}

func hasCommit(ctx context.Context, dir, sha string) bool {
	_, err := git(ctx, dir, "cat-file", "-e", sha+"^{commit}")
	return err == nil
}

var unsafe = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

func safe(name string) string { return unsafe.ReplaceAllString(name, "_") }

func cacheName(job *client.Job) string {
	sum := sha256.Sum256([]byte(job.RemoteURL))
	label := safe(firstNonEmpty(job.Repository, job.Project, "repo"))
	return label + "-" + hex.EncodeToString(sum[:4])
}

func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func failed(err error) client.Result {
	return client.Result{Status: "failed", Error: err.Error()}
}

// model is the Claude Code model for a job: the one the organization chose for
// it in LBW, or the runner's own setting when LBW did not name one.
func model(job *client.Job, fallback string) string {
	if job.Model != "" {
		return job.Model
	}
	return fallback
}
