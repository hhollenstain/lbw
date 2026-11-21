package job

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/hhollenstain/lbw/runner/internal/client"
	"github.com/hhollenstain/lbw/runner/internal/config"
)

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=Ada", "GIT_AUTHOR_EMAIL=ada@example.com", "GIT_COMMITTER_NAME=Ada", "GIT_COMMITTER_EMAIL=ada@example.com")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// Repo makes a repository with one commit and returns its path and that commit.
func Repo(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	gitIn(t, dir, "init", "-q", "-b", "main")
	gitIn(t, dir, "config", "user.name", "Ada")
	gitIn(t, dir, "config", "user.email", "ada@example.com")
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("# demo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitIn(t, dir, "add", ".")
	gitIn(t, dir, "commit", "-q", "-m", "start")
	return dir, gitIn(t, dir, "rev-parse", "HEAD")
}

func fakeClaude(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the fake claude is a shell script")
	}
	path, err := filepath.Abs("../testdata/fake-claude.sh")
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func TestAJobCommitsClaudesChangeAndSendsAPatch(t *testing.T) {
	remote, base := Repo(t)
	cfg := &config.Config{WorkDir: t.TempDir(), PermissionMode: "acceptEdits"}
	r := &Runner{Config: cfg, Claude: fakeClaude(t)}
	job := &client.Job{JobID: "job1", TaskID: "task1", Goal: "Add a hello file\nwith detail", BaseSHA: base, RemoteURL: remote, Repository: "o/demo"}
	var events []client.Event
	got := r.Run(context.Background(), job, func(e client.Event) { events = append(events, e) })
	if got.Status != "done" {
		t.Fatalf("result = %+v", got)
	}
	for _, want := range []string{"Subject: [PATCH] Add a hello file", "+hello from claude", "LBW-Job: task1"} {
		if !strings.Contains(got.Patch, want) {
			t.Fatalf("patch is missing %q:\n%s", want, got.Patch)
		}
	}
	if got.Summary != "Added hello.txt." {
		t.Fatalf("summary = %q", got.Summary)
	}
	entries, _ := os.ReadDir(filepath.Join(cfg.WorkDir, "jobs"))
	if len(entries) != 0 {
		t.Fatalf("the worktree was left behind: %v", entries)
	}
	if gitIn(t, remote, "rev-parse", "HEAD") != base {
		t.Fatal("the runner changed the source repository")
	}
}

func TestAMappedCheckoutIsUsedInsteadOfACloneAndLeftAlone(t *testing.T) {
	local, base := Repo(t)
	cfg := &config.Config{WorkDir: t.TempDir(), Repos: map[string]string{"o/demo": local}}
	r := &Runner{Config: cfg, Claude: fakeClaude(t)}
	got := r.Run(context.Background(), &client.Job{JobID: "j", TaskID: "t", Goal: "g", BaseSHA: base, Repository: "o/demo"}, func(client.Event) {})
	if got.Status != "done" {
		t.Fatalf("result = %+v", got)
	}
	if _, err := os.Stat(filepath.Join(cfg.WorkDir, "repos")); !os.IsNotExist(err) {
		t.Fatal("a mapped checkout should not be cloned")
	}
	if status := gitIn(t, local, "status", "--porcelain"); status != "" {
		t.Fatalf("the person's checkout changed: %s", status)
	}
}

func TestAMissingCommitOrCheckoutIsExplained(t *testing.T) {
	cfg := &config.Config{WorkDir: t.TempDir()}
	r := &Runner{Config: cfg, Claude: fakeClaude(t)}
	got := r.Run(context.Background(), &client.Job{JobID: "j", BaseSHA: "abc", Project: "demo"}, func(client.Event) {})
	if got.Status != "failed" || !strings.Contains(got.Error, "add it under repos") {
		t.Fatalf("result = %+v", got)
	}
	remote, _ := Repo(t)
	missing := strings.Repeat("a", 40)
	got = r.Run(context.Background(), &client.Job{JobID: "k", BaseSHA: missing, RemoteURL: remote}, func(client.Event) {})
	if got.Status != "failed" || !strings.Contains(got.Error, "push it first") {
		t.Fatalf("result = %+v", got)
	}
}

func TestAClaudeFailureFailsTheJob(t *testing.T) {
	t.Setenv("FAKE_CLAUDE_FAIL", "1")
	remote, base := Repo(t)
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: fakeClaude(t)}
	got := r.Run(context.Background(), &client.Job{JobID: "j", Goal: "g", BaseSHA: base, RemoteURL: remote}, func(client.Event) {})
	if got.Status != "failed" || got.Patch != "" || !strings.Contains(got.Error, "max_turns") {
		t.Fatalf("result = %+v", got)
	}
}

func TestThePromptCarriesTheGoalAndReviewNotes(t *testing.T) {
	text := Prompt(&client.Job{Goal: "Fix the login", ReviewNotes: "Keep the old route"})
	for _, want := range []string{"Fix the login", "Keep the old route", "Do not push"} {
		if !strings.Contains(text, want) {
			t.Fatalf("prompt is missing %q", want)
		}
	}
}

func TestModelPrefersTheJobOverTheRunnerSetting(t *testing.T) {
	if got := model(&client.Job{Model: "opus"}, "sonnet"); got != "opus" {
		t.Fatalf("job model: got %q", got)
	}
	if got := model(&client.Job{}, "sonnet"); got != "sonnet" {
		t.Fatalf("runner fallback: got %q", got)
	}
}

func TestAPlanReadsTheCodeAndChangesNothing(t *testing.T) {
	remote, base := Repo(t)
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir(), PermissionMode: "acceptEdits", AllowedTools: []string{"Edit", "Write"}}, Claude: fakeClaude(t)}
	got := r.Run(context.Background(), &client.Job{JobID: "p", TaskID: "t", Phase: "plan", Goal: "Add a hello file", BaseSHA: base, RemoteURL: remote}, func(client.Event) {})
	if got.Status != "done" || got.Patch != "" || !strings.Contains(got.Summary, "## Plan") {
		t.Fatalf("result = %+v", got)
	}
}

func TestAPlanThatEditsFilesFails(t *testing.T) {
	t.Setenv("FAKE_CLAUDE_PLAN_WRITES", "1")
	remote, base := Repo(t)
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: fakeClaude(t)}
	got := r.Run(context.Background(), &client.Job{JobID: "p", Phase: "plan", Goal: "g", BaseSHA: base, RemoteURL: remote}, func(client.Event) {})
	if got.Status != "failed" || !strings.Contains(got.Error, "must not edit") {
		t.Fatalf("result = %+v", got)
	}
}

func TestPlanToolsAreReadOnly(t *testing.T) {
	args := strings.Join(claudeArgsForPlan(), " ")
	for _, want := range []string{"--allowedTools Read,Glob,Grep", "--disallowedTools Edit,Write,MultiEdit,NotebookEdit", "--permission-mode default"} {
		if !strings.Contains(args, want) {
			t.Fatalf("plan args are missing %q: %s", want, args)
		}
	}
}

func TestABuildFollowsThePlanAndContinuesFromEarlierCommits(t *testing.T) {
	remote, base := Repo(t)
	cfg := &config.Config{WorkDir: t.TempDir()}
	r := &Runner{Config: cfg, Claude: fakeClaude(t)}
	first := r.Run(context.Background(), &client.Job{JobID: "a", TaskID: "t", Goal: "Add a hello file", BaseSHA: base, RemoteURL: remote}, func(client.Event) {})
	if first.Status != "done" {
		t.Fatalf("first = %+v", first)
	}
	again := r.Run(context.Background(), &client.Job{JobID: "b", TaskID: "t", Goal: "Add a hello file", BaseSHA: base, RemoteURL: remote, PriorPatch: first.Patch, ReviewNotes: "Say it twice"}, func(client.Event) {})
	if again.Status != "done" {
		t.Fatalf("again = %+v", again)
	}
	if strings.Count(again.Patch, "Subject: [PATCH") != 2 {
		t.Fatalf("the sent-back build should carry both commits:\n%s", again.Patch)
	}
	text := Prompt(&client.Job{Goal: "g", Plan: "1. Do the thing"})
	if !strings.Contains(text, "Follow this plan:\n1. Do the thing") {
		t.Fatalf("prompt = %s", text)
	}
}

func claudeArgsForPlan() []string {
	return planOptions("claude", "/tmp/x", &client.Job{Goal: "g"}, &config.Config{}).Args()
}
