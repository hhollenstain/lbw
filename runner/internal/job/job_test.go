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

func TestABuildPromptEndsWithLBWsPullRequestInstructions(t *testing.T) {
	text := Prompt(&client.Job{Goal: "Fix the login", Phase: "build", Instructions: "PR title: <title>"})
	if !strings.HasSuffix(strings.TrimSpace(text), "PR title: <title>") {
		t.Fatalf("prompt does not end with the instructions: %q", text)
	}
	if strings.Contains(text, "short summary") {
		t.Fatalf("prompt still asks for a short summary: %q", text)
	}
	if old := Prompt(&client.Job{Goal: "Fix the login"}); !strings.Contains(old, "short summary") {
		t.Fatalf("a job without instructions lost its summary line: %q", old)
	}
}

func TestAMergeClaudeMakesIsSentAsOneOrdinaryCommit(t *testing.T) {
	remote, base := Repo(t)
	t.Setenv("FAKE_CLAUDE_MERGE", "1")
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: fakeClaude(t)}
	job := &client.Job{JobID: "m", TaskID: "task1", Goal: "Resolve the merge conflicts", BaseSHA: base, RemoteURL: remote}
	got := r.Run(context.Background(), job, func(client.Event) {})
	if got.Status != "done" {
		t.Fatalf("result = %+v", got)
	}
	if n := strings.Count(got.Patch, "\nSubject: [PATCH"); n != 1 {
		t.Fatalf("patch has %d commits, want one:\n%s", n, got.Patch)
	}
	clone := t.TempDir()
	gitIn(t, "", "clone", "-q", remote, clone)
	gitIn(t, clone, "config", "user.name", "Ada")
	gitIn(t, clone, "config", "user.email", "ada@example.com")
	mbox := filepath.Join(t.TempDir(), "change.mbox")
	if err := os.WriteFile(mbox, []byte(got.Patch), 0o644); err != nil {
		t.Fatal(err)
	}
	gitIn(t, clone, "am", "--3way", mbox)
	for _, name := range []string{"side.txt", "hello.txt"} {
		if _, err := os.Stat(filepath.Join(clone, name)); err != nil {
			t.Fatalf("%s did not arrive: %v", name, err)
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

func TestASpecJobAnswersWithTheSpecAndSendsNoChange(t *testing.T) {
	remote, base := Repo(t)
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: fakeClaude(t)}
	job := &client.Job{JobID: "s", Goal: "Add a hello file", BaseSHA: base, RemoteURL: remote, Phase: "spec", Instructions: "Write the spec."}
	got := r.Run(context.Background(), job, func(client.Event) {})
	if got.Status != "done" || got.Patch != "" || got.Summary != "Added hello.txt." {
		t.Fatalf("result = %+v", got)
	}
	text := Prompt(job)
	for _, want := range []string{"writing the spec", "Do not change any files", "Write the spec."} {
		if !strings.Contains(text, want) {
			t.Fatalf("prompt is missing %q:\n%s", want, text)
		}
	}
}

func TestABuildFollowsTheAcceptedSpecAndPlan(t *testing.T) {
	text := Prompt(&client.Job{Goal: "Add a hello file", SpecPath: "specs/hello.md", Spec: "## Acceptance criteria\n- hello.txt exists", Plan: "1. Write hello.txt"})
	for _, want := range []string{"specs/hello.md", "hello.txt exists", "accepted this plan", "1. Write hello.txt"} {
		if !strings.Contains(text, want) {
			t.Fatalf("prompt is missing %q:\n%s", want, text)
		}
	}
}

func TestWorkSentBackIsAppliedFirstAndSentAgain(t *testing.T) {
	remote, base := Repo(t)
	earlier := t.TempDir()
	gitIn(t, earlier, "clone", "-q", remote, ".")
	if err := os.WriteFile(filepath.Join(earlier, "first.txt"), []byte("first\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitIn(t, earlier, "add", "first.txt")
	gitIn(t, earlier, "commit", "-q", "-m", "First try")
	start := gitIn(t, earlier, "format-patch", "--stdout", base+"..HEAD")
	r := &Runner{Config: &config.Config{WorkDir: t.TempDir()}, Claude: fakeClaude(t)}
	job := &client.Job{JobID: "b", TaskID: "t", Goal: "Add files", BaseSHA: base, RemoteURL: remote, StartPatch: start + "\n", ReviewNotes: "Also add hello"}
	got := r.Run(context.Background(), job, func(client.Event) {})
	if got.Status != "done" {
		t.Fatalf("result = %+v", got)
	}
	for _, want := range []string{"Subject: [PATCH 1/2] First try", "+first", "+hello from claude"} {
		if !strings.Contains(got.Patch, want) {
			t.Fatalf("patch is missing %q:\n%s", want, got.Patch)
		}
	}
}

func TestABuildMayRunTheProjectsOwnCommands(t *testing.T) {
	base := []string{"Read"}
	job := &client.Job{Commands: []string{"uv run pytest -q", "make (bad)", "", "npm test\nrm -rf /"}}
	got := strings.Join(AllowedTools(base, job), " | ")
	if got != "Read | Bash(uv run pytest -q) | Bash(uv run pytest -q:*)" {
		t.Fatalf("allowed = %s", got)
	}
	job.Phase = "spec"
	if got := strings.Join(AllowedTools(base, job), ","); got != "Read,Glob,Grep" {
		t.Fatalf("a spec job may use: %v", got)
	}
	if len(base) != 1 {
		t.Fatalf("the runner's own list changed: %v", base)
	}
}

func TestASpecJobCannotWriteOrRunCommands(t *testing.T) {
	for _, tool := range []string{"Write", "Edit", "Bash", "Task", "Agent"} {
		found := false
		for _, off := range readOnlyTools {
			found = found || off == tool
		}
		if !found {
			t.Fatalf("%s is not turned off for a spec job", tool)
		}
	}
}
