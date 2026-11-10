from __future__ import annotations

import subprocess
from pathlib import Path

from tests.support import init_git_repo

from agent_lbw.sandbox import Workspace
from agent_lbw.tools.git import (
    CO_AUTHORED_BY,
    agent_commit,
    branch_named_in_goal,
    commit_changes,
    commits_ahead_of_base,
    create_pull_request,
    current_branch,
    current_sha,
    default_base_branch,
    ensure_pr_branch,
    existing_pull_request,
    extract_pr_url,
    fold_unpublished_commits,
    goal_keeps_current_branch,
    has_changes,
    intended_pr_base,
    is_runtime_artifact,
    is_tracked,
    list_branches,
    merge_pull_request,
    move_commits_off_base,
    parse_github_remote,
    prepare_clean_base,
    pull_request_state,
    push_changes,
    resume_workspace,
    run_git,
    update_pull_request,
    upstream_state,
    with_lbw_coauthor,
)


def test_extract_pr_url_from_gh_output() -> None:
    assert extract_pr_url(None) is None
    assert extract_pr_url("no link here") is None
    assert (
        extract_pr_url("Creating pull request\nhttps://github.com/acme/repo/pull/12\n")
        == "https://github.com/acme/repo/pull/12"
    )
    assert extract_pr_url("https://example.test/pull/1") == "https://example.test/pull/1"
    assert (
        extract_pr_url("opened https://gitlab.com/acme/app/-/merge_requests/7.")
        == "https://gitlab.com/acme/app/-/merge_requests/7"
    )


def test_parse_github_remote_from_common_urls() -> None:
    assert parse_github_remote("git@github.com:acme/repo.git") == ("acme", "repo")
    assert parse_github_remote("https://github.com/acme/repo.git") == ("acme", "repo")
    assert parse_github_remote("https://github.com/acme/repo") == ("acme", "repo")
    assert parse_github_remote("ssh://git@github.com/acme/repo.git") == ("acme", "repo")
    assert parse_github_remote("https://gitlab.com/acme/repo.git") is None


def test_commit_changes_never_adds_lbw_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    shot = tmp_path / ".lbw" / "ui-screenshots" / "ui-review_goal.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text("name: demo\n", encoding="utf-8")
    assert not is_tracked(workspace, shot)
    result = commit_changes(workspace, "update app", extra_paths=[shot, config])
    assert result.ok
    assert not is_tracked(workspace, shot)
    assert not is_tracked(workspace, config)
    listed = run_git(workspace, ["ls-files", "--", ".lbw"])
    assert listed.stdout.strip() == ""


def test_with_lbw_coauthor_adds_github_trailer() -> None:
    assert with_lbw_coauthor("") == ""
    assert with_lbw_coauthor("  fix the thing  ") == f"fix the thing\n\n{CO_AUTHORED_BY}"
    already = f"fix the thing\n\n{CO_AUTHORED_BY}"
    assert with_lbw_coauthor(already) == already


def test_commits_ahead_of_base_counts_feature_commits(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    assert commits_ahead_of_base(workspace) == 0
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text("two\n", encoding="utf-8")
    committed = commit_changes(workspace, "two")
    assert committed.ok
    assert commits_ahead_of_base(workspace) == 1


def test_existing_pull_request_reads_gh_json(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args and args[0] == "gh" and "view" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"url":"https://github.com/acme/repo/pull/4"}\n',
                stderr="",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    assert existing_pull_request(workspace) == "https://github.com/acme/repo/pull/4"


def test_pull_request_state_reads_gh_json(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        assert args[:4] == ["gh", "pr", "view", "https://github.com/acme/repo/pull/4"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='{"state":"MERGED"}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    assert pull_request_state("https://github.com/acme/repo/pull/4") == "merged"


def test_update_pull_request_edits_the_open_pr(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    seen: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="https://github.com/acme/repo/pull/4\n",
            stderr="",
        )

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    result = update_pull_request(workspace, "Keep the branch", "Added more context")
    assert result.ok
    assert seen["args"][:3] == ["gh", "pr", "edit"]
    assert "Keep the branch" in seen["args"]
    assert any("Added more context" in part for part in seen["args"])


def test_existing_pull_request_missing_gh_is_none(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)

    def fake_run(args, **kwargs):
        if args and args[0] == "gh":
            raise FileNotFoundError("gh")
        raise AssertionError(args)

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    assert existing_pull_request(Workspace(tmp_path)) is None


def test_create_pull_request_treats_already_exists_as_success(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args and args[0] == "gh":
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr=(
                    'a pull request for branch "lbw/feature" already exists:\n'
                    "https://github.com/acme/repo/pull/9\n"
                ),
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    result = create_pull_request(workspace, "Add feature", "details", base="main")
    assert result.ok
    assert extract_pr_url(result.output) == "https://github.com/acme/repo/pull/9"


def test_create_pull_request_does_not_pass_unknown_coauthor_flag(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    captured: dict[str, list[str]] = {}
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args and args[0] == "gh":
            captured["args"] = list(args)
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/acme/repo/pull/1\n",
                stderr="",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    result = create_pull_request(workspace, "Add feature", "details", base="main")
    assert result.ok
    args = captured["args"]
    assert args[:3] == ["gh", "pr", "create"]
    assert args[args.index("--head") + 1] == "lbw/feature"
    assert "--add-co-author" not in args
    body = args[args.index("--body") + 1]
    assert CO_AUTHORED_BY in body
    assert "--base" in args
    assert args[args.index("--base") + 1] == "main"


def test_run_log_paths_are_runtime_artifacts() -> None:
    assert is_runtime_artifact(".lbw/runs/cycle.json")
    assert is_runtime_artifact(".lbw/runs")
    assert is_runtime_artifact(".lbw/servers.json")
    assert is_runtime_artifact(".lbw/workspaces.json")
    assert is_runtime_artifact(".lbw/config.yaml")
    assert is_runtime_artifact(".lbw/ui-screenshots/ui-review.png")
    assert is_runtime_artifact(".lbw/.gitignore")
    assert is_runtime_artifact("history.json")
    assert not is_runtime_artifact("src/agent_lbw/cli.py")
    assert not is_runtime_artifact("lbw/runs/cycle.json")


def test_tool_caches_are_runtime_artifacts() -> None:
    assert is_runtime_artifact("__pycache__/app.cpython-312.pyc")
    assert is_runtime_artifact("src/pkg/__pycache__/mod.cpython-312.pyc")
    assert is_runtime_artifact(".pytest_cache/v/cache/nodeids")
    assert is_runtime_artifact(".ruff_cache/CACHEDIR.TAG")
    assert is_runtime_artifact("build/mod.pyc")
    assert not is_runtime_artifact("src/pkg/mod.py")
    assert not is_runtime_artifact("docs/pycache_notes.md")


def test_bytecode_left_by_the_test_command_is_not_project_work(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00")
    assert not has_changes(workspace)
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert has_changes(workspace)
    result = commit_changes(workspace, "change app")
    assert result.ok
    tracked = run_git(workspace, ["ls-files"]).stdout.split()
    assert "app.py" in tracked
    assert not any(path.startswith("__pycache__/") for path in tracked)


def test_commit_happy_path(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / "readme.txt").write_text("hello world\n", encoding="utf-8")
    assert has_changes(workspace)
    result = commit_changes(workspace, "update readme")
    assert result.ok
    assert "committed" in result.output
    assert not has_changes(workspace)
    log = run_git(workspace, ["log", "-1", "--format=%B"]).stdout
    assert "update readme" in log
    assert CO_AUTHORED_BY in log


def test_commit_rejects_env_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    result = commit_changes(workspace, "add env")
    assert not result.ok
    assert "secrets" in result.output


def test_commit_skips_run_logs(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "cycle.json").write_text('{"status": "success"}\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert has_changes(workspace)
    result = commit_changes(workspace, "update app")
    assert result.ok
    tracked = run_git(workspace, ["ls-files", ".lbw/runs"])
    assert tracked.stdout.strip() == ""
    assert (runs / "cycle.json").exists()


def test_commit_skips_history_json(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / "history.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert has_changes(workspace)
    result = commit_changes(workspace, "update app")
    assert result.ok
    tracked = run_git(workspace, ["ls-files", "history.json"])
    assert tracked.stdout.strip() == ""
    assert (tmp_path / "history.json").exists()


def test_run_logs_alone_are_not_changes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "cycle.json").write_text("{}\n", encoding="utf-8")
    assert not has_changes(workspace)
    result = commit_changes(workspace, "should not commit logs")
    assert not result.ok


def test_agent_commit_refuses_protected_branch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    result = agent_commit(workspace, "update app")
    assert result.ok is False
    assert "refusing to commit" in result.output
    assert has_changes(workspace)
    assert current_branch(workspace) in {"main", "master"}


def test_agent_commit_leaves_the_tree_for_one_cycle_commit(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    result = agent_commit(workspace, "update app")
    assert result.ok is False
    assert "one commit after review" in result.output
    assert has_changes(workspace)
    assert current_branch(workspace) == "lbw/feature"


def test_cycle_commit_helper_still_works_on_protected_branch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    result = commit_changes(workspace, "update app")
    assert result.ok
    assert current_branch(workspace) in {"main", "master"}


def test_push_refuses_protected_branches(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    for name in ("main", "master"):
        result = push_changes(workspace, "origin", name)
        assert result.ok is False
        assert "refusing" in result.output


def test_ensure_pr_branch_moves_commits_off_main(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    protected = current_branch(workspace)
    sha_before = current_sha(workspace)
    (tmp_path / "app.py").write_text("two\n", encoding="utf-8")
    committed = commit_changes(workspace, "change app")
    assert committed.ok
    after = current_sha(workspace)
    result = ensure_pr_branch(workspace, "Improve the adder", sha_before)
    assert result.ok
    assert result.output.startswith("lbw/")
    assert current_branch(workspace) == result.output
    assert current_sha(workspace) == after
    assert run_git(workspace, ["rev-parse", protected or "HEAD"]).stdout.strip() == sha_before


def test_ensure_pr_branch_leaves_the_branch_the_run_started_from(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "fix/format"])
    sha_before = current_sha(workspace)
    (tmp_path / "app.py").write_text("two\n", encoding="utf-8")
    committed = commit_changes(workspace, "format the file")
    assert committed.ok
    after = current_sha(workspace)
    result = ensure_pr_branch(workspace, "Format the sources", sha_before, base="fix/format")
    assert result.ok
    assert result.output.startswith("lbw/")
    assert current_branch(workspace) == result.output
    assert current_sha(workspace) == after
    assert run_git(workspace, ["rev-parse", "fix/format"]).stdout.strip() == sha_before
    assert intended_pr_base(workspace, "fix/format", None) == "fix/format"


def test_move_commits_off_base_uses_the_remote_when_the_run_sha_is_missing(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "fix/format"])
    sha_before = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/fix/format", sha_before])
    (tmp_path / "app.py").write_text("two\n", encoding="utf-8")
    assert commit_changes(workspace, "format the file").ok
    after = current_sha(workspace)
    moved = move_commits_off_base(workspace, "Format the sources", "fix/format", None)
    assert moved.ok
    assert current_branch(workspace) == moved.output
    assert current_sha(workspace) == after
    assert run_git(workspace, ["rev-parse", "fix/format"]).stdout.strip() == sha_before


def test_move_commits_off_base_reports_when_the_remote_already_has_them(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "fix/format"])
    (tmp_path / "app.py").write_text("two\n", encoding="utf-8")
    assert commit_changes(workspace, "format the file").ok
    after = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/fix/format", after])
    moved = move_commits_off_base(workspace, "Format the sources", "fix/format", None)
    assert moved.ok is False
    assert "already on fix/format" in moved.output


def test_default_base_branch_prefers_origin_main_over_the_working_branch(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    initial = current_sha(workspace) or ""
    run_git(workspace, ["checkout", "-b", "fix/format"])
    local = current_branch(workspace)
    if local not in {"main", "master"}:
        run_git(workspace, ["branch", "-D", "main"])
        run_git(workspace, ["branch", "-D", "master"])
    run_git(workspace, ["update-ref", "refs/remotes/origin/main", initial])
    assert default_base_branch(workspace) == "main"
    assert default_base_branch(workspace, "fix/format", avoid="fix/format") == "main"


def _commit_file(workspace: Workspace, root: Path, text: str, message: str) -> None:
    (root / "app.py").write_text(text, encoding="utf-8")
    committed = commit_changes(workspace, message)
    assert committed.ok


def test_fold_unpublished_commits_keeps_one_commit_for_a_long_run(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    base = current_branch(workspace) or "main"
    run_git(workspace, ["checkout", "-b", "lbw/docs"])
    for number in range(1, 6):
        _commit_file(workspace, tmp_path, f"step {number}\n", f"step {number}")
    assert commits_ahead_of_base(workspace, base) == 5
    folded = fold_unpublished_commits(workspace, base, "Write up AGENTS.md")
    assert folded.ok
    assert folded.output.startswith("folded 5 commits")
    assert commits_ahead_of_base(workspace, base) == 1
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "step 5\n"


def test_fold_unpublished_commits_leaves_a_branch_already_on_the_remote(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    base = current_branch(workspace) or "main"
    base_sha = current_sha(workspace) or ""
    run_git(workspace, ["checkout", "-b", "lbw/docs"])
    for number in range(1, 5):
        _commit_file(workspace, tmp_path, f"step {number}\n", f"step {number}")
    tip = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/lbw/docs", tip])
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{base}", base_sha])
    folded = fold_unpublished_commits(workspace, base, "Write up AGENTS.md")
    assert folded.ok
    assert "already on the remote" in folded.output
    assert commits_ahead_of_base(workspace, base) == 4
    assert current_sha(workspace) == tip


def test_create_pull_request_refuses_a_branch_into_itself(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "fix/format"])
    result = create_pull_request(workspace, "Format the sources", "details", base="fix/format")
    assert result.ok is False
    assert "same branch" in result.output


def test_upstream_state_without_remote_is_not_pushed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    state = upstream_state(Workspace(tmp_path))
    assert state.pushed is False
    assert state.tracking is None
    assert "not pushed" in state.detail


def test_upstream_state_matches_tracking_ref(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    branch = current_branch(workspace)
    sha = current_sha(workspace)
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{branch}", sha or ""])
    run_git(workspace, ["branch", f"--set-upstream-to=origin/{branch}"])
    state = upstream_state(workspace)
    assert state.pushed is True
    assert state.ahead == 0
    assert "pushed" in state.detail


def test_upstream_state_detects_unpushed_local_commits(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    branch = current_branch(workspace)
    sha = current_sha(workspace)
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{branch}", sha or ""])
    run_git(workspace, ["branch", f"--set-upstream-to=origin/{branch}"])
    (tmp_path / "app.py").write_text("print('next')\n", encoding="utf-8")
    commit_changes(workspace, "local only")
    state = upstream_state(workspace)
    assert state.pushed is False
    assert state.ahead == 1
    assert "not pushed" in state.detail


def test_resume_workspace_checks_out_feature_branch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    start = current_branch(workspace)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text("print('next')\n", encoding="utf-8")
    run_git(workspace, ["add", "app.py"])
    run_git(workspace, ["commit", "-m", "wip"])
    run_git(workspace, ["checkout", start or "master"])
    assert current_branch(workspace) != "lbw/feature"
    result = resume_workspace(workspace, branch="lbw/feature")
    assert result.ok
    assert current_branch(workspace) == "lbw/feature"


def test_branch_named_in_goal_reads_an_explicit_branch() -> None:
    assert branch_named_in_goal("Fix the start button") is None
    assert branch_named_in_goal("Work on the branch and ship it") is None
    assert branch_named_in_goal("Start on branch feature/login") == "feature/login"
    assert branch_named_in_goal("base branch: release/1.2") == "release/1.2"
    assert goal_keeps_current_branch("from the current branch create a PR")
    assert not goal_keeps_current_branch("Start on branch feature/login")


def test_prepare_clean_base_uses_origin_head_and_stashes_edits(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('base')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    head = current_branch(workspace) or "master"
    sha = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{head}", sha])
    run_git(workspace, ["checkout", "-b", "feature/wip"])
    (tmp_path / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("untracked\n", encoding="utf-8")
    result = prepare_clean_base(workspace, head)
    assert result.ok
    assert current_branch(workspace) == head
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('base')\n"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "untracked\n"
    stashes = run_git(workspace, ["stash", "list"])
    assert "lbw: stashed before a clean start" in stashes.stdout
    default, names = list_branches(workspace)
    assert default == head
    assert head in names
    assert "feature/wip" in names


def test_prepare_clean_base_checks_out_a_named_branch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('base')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    head = current_branch(workspace) or "master"
    run_git(workspace, ["checkout", "-b", "feature/login"])
    (tmp_path / "app.py").write_text("print('feature')\n", encoding="utf-8")
    run_git(workspace, ["add", "app.py"])
    run_git(workspace, ["commit", "-m", "feature"])
    feature = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/feature/login", feature])
    run_git(workspace, ["checkout", head])
    (tmp_path / "app.py").write_text("print('local main')\n", encoding="utf-8")
    result = prepare_clean_base(workspace, "feature/login")
    assert result.ok
    assert current_branch(workspace) == "feature/login"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('feature')\n"


def test_a_github_commit_uses_the_connected_person(tmp_path: Path) -> None:
    from agent_lbw.runtime.account import load_account, update_account
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.github_sync import link_github_login
    from agent_lbw.tools.git import CONNECT_GITHUB

    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    run_git(workspace, ["remote", "add", "origin", "git@github.com:acme/repo.git"])
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    account = load_account(tmp_path)
    refused = commit_changes(workspace, "update app", store_root=tmp_path)
    assert not refused.ok
    assert refused.output == CONNECT_GITHUB

    update_account(tmp_path, user_name="Ada Lovelace", user_email="ada@example.com")
    with database(tmp_path) as db:
        link_github_login(db, user_id=account["current_user_id"], github_login="ada")
    committed = commit_changes(workspace, "update app", store_root=tmp_path)
    assert committed.ok
    logged = run_git(workspace, ["log", "-1", "--format=%an%n%ae%n%cn%n%ce"])
    assert logged.stdout.splitlines() == [
        "Ada Lovelace",
        "ada@example.com",
        "Ada Lovelace",
        "ada@example.com",
    ]


def test_merge_pull_request_calls_gh_squash(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    seen: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, stdout="merged\n", stderr="")

    monkeypatch.setattr("agent_lbw.tools.git.subprocess.run", fake_run)
    result = merge_pull_request(workspace, "https://github.com/acme/repo/pull/4")
    assert result.ok
    assert seen["args"][:3] == ["gh", "pr", "merge"]
    assert "https://github.com/acme/repo/pull/4" in seen["args"]
    assert "--squash" in seen["args"]
    assert "--delete-branch" in seen["args"]
