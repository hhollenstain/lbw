"""Fix merge conflicts: merge the base branch into an open pull request without rewriting it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_lbw.config import Settings
from agent_lbw.progress import bind_progress, reset_progress
from agent_lbw.runtime import merge_fix
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.merge_fix import Conflict, fix_merge_conflict, resolve_with_agent
from agent_lbw.runtime.tasks import Task

_ENV = {
    "GIT_AUTHOR_NAME": "Ada",
    "GIT_AUTHOR_EMAIL": "ada@example.com",
    "GIT_COMMITTER_NAME": "Ada",
    "GIT_COMMITTER_EMAIL": "ada@example.com",
}
BRANCH = "lbw/runner-abc"


def _git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(_ENV)
    done = subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(root: Path, name: str, text: str, message: str) -> str:
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _pull_request(tmp_path: Path, *, base_edit: tuple[str, str]) -> tuple[Path, Path]:
    """A checkout on a pushed PR branch, and a main on GitHub that moved since."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    _commit(work, "app.py", "greeting = 'hi'\n", "start")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", BRANCH)
    _commit(work, "app.py", "greeting = 'hello'\n", "Say hello")
    _git(work, "push", "-q", "origin", BRANCH)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _commit(other, base_edit[0], base_edit[1], "Change main")
    _git(other, "push", "-q", "origin", "main")
    return work, origin


def _task(work: Path, **fields) -> Task:
    values = {
        "id": "merge-run",
        "workspace": str(work),
        "goal": "Merge main into the pull request",
        "auto_commit": True,
        "create_pr": True,
        "model_name": "scripted",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "ollama",
        "merge_base": "main",
        "branch": BRANCH,
        "approval_gate": "ask",
        "committer_name": "Ada",
        "committer_email": "ada@example.com",
    }
    values.update(fields)
    return Task(**values)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def publish(root, settings, goal, summary, **kwargs):
        calls.append({"head": _git(Path(root), "rev-parse", "HEAD"), **kwargs})
        return CycleResult(
            status="success",
            goal=goal,
            summary=summary,
            tests_passed=None,
            committed=True,
            published=True,
            commit_sha=None,
            reason=kwargs.get("reason", ""),
            stage="done",
        )

    monkeypatch.setattr(merge_fix, "publish_runner_change", publish)
    return calls


def _run(task: Task, settings: Settings, resolve) -> CycleResult:
    progress = bind_progress(task.events)
    try:
        return fix_merge_conflict(task, settings=settings, store_root=None, resolve=resolve)
    finally:
        reset_progress(progress)


def _never(conflict: Conflict) -> tuple[str, str]:
    raise AssertionError("a clean merge needs no model")


def _parents(work: Path, ref: str = "HEAD") -> list[str]:
    return _git(work, "rev-list", "--parents", "-n", "1", ref).split()[1:]


def test_a_clean_merge_is_pushed_at_once(
    settings: Settings, tmp_path: Path, published: list[dict]
) -> None:
    work, _ = _pull_request(tmp_path, base_edit=("README.md", "# demo\n"))
    head = _git(work, "rev-parse", "HEAD")

    result = _run(_task(work), settings, _never)

    assert result.status == "success"
    main = _git(work, "rev-parse", "refs/remotes/origin/main")
    assert _parents(work) == [head, main]
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    assert (work / "README.md").is_file()
    assert published[0]["keep_pull_request"] is True
    assert published[0]["pr_base"] == "main"
    assert published[0]["reason"] == "merged with no conflicts"


def test_resolved_conflicts_wait_for_approval_then_push(
    settings: Settings, tmp_path: Path, published: list[dict]
) -> None:
    work, _ = _pull_request(tmp_path, base_edit=("app.py", "greeting = 'hey'\n"))
    head = _git(work, "rev-parse", "HEAD")
    seen: list[Conflict] = []

    def resolve(conflict: Conflict) -> tuple[str, str]:
        seen.append(conflict)
        snapshot = _git(work, "show", f"{conflict.snapshot}:app.py")
        assert "<<<<<<<" in snapshot
        assert _parents(work, conflict.snapshot) == [head]
        (work / "app.py").write_text("greeting = 'hello, hey'\n", encoding="utf-8")
        _git(work, "add", "app.py")
        return _git(work, "write-tree"), ""

    task = _task(work)
    result = _run(task, settings, resolve)

    assert (result.status, result.stage) == ("waiting", "approval")
    assert seen[0].files == ["app.py"]
    merge = _git(work, "rev-parse", "HEAD")
    assert _parents(work) == [head, _git(work, "rev-parse", "refs/remotes/origin/main")]
    assert (work / "app.py").read_text(encoding="utf-8") == "greeting = 'hello, hey'\n"
    assert not (work / ".git" / "MERGE_HEAD").exists()
    assert any(event.get("approved_sha") == merge for event in task.events)
    assert any(event.get("kind") == "file" for event in task.events)
    assert published == []

    # Something moves the branch before approval. Approving still pushes the merge.
    _git(work, "checkout", "-q", "-f", "-B", BRANCH, head)
    task.decision = "approve"
    done = _run(task, settings, _never)
    assert done.status == "success"
    assert published[0]["head"] == merge


def test_markers_left_in_a_file_fail_and_leave_the_branch_alone(
    settings: Settings, tmp_path: Path, published: list[dict]
) -> None:
    work, _ = _pull_request(tmp_path, base_edit=("app.py", "greeting = 'hey'\n"))
    head = _git(work, "rev-parse", "HEAD")

    def keep_markers(conflict: Conflict) -> tuple[str, str]:
        return _git(work, "rev-parse", f"{conflict.snapshot}^{{tree}}"), ""

    result = _run(_task(work), settings, keep_markers)

    assert result.status == "failed"
    assert result.reason == "conflict markers are still in app.py"
    assert _git(work, "rev-parse", "HEAD") == head
    assert not (work / ".git" / "MERGE_HEAD").exists()
    assert published == []


def test_the_coding_agent_edits_only_the_conflicted_files(
    settings: Settings, tmp_path: Path, published: list[dict]
) -> None:
    work, _ = _pull_request(tmp_path, base_edit=("app.py", "greeting = 'hey'\n"))
    told: list[str] = []

    def agent(notes: str) -> None:
        told.append(notes)
        (work / "app.py").write_text("greeting = 'hello'\n", encoding="utf-8")
        (work / "history.json").write_text("[]\n", encoding="utf-8")

    result = _run(_task(work), settings, lambda conflict: resolve_with_agent(conflict, agent))

    assert result.status == "waiting"
    assert "- app.py" in told[0]
    tracked = _git(work, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "history.json" not in tracked
    assert _git(work, "show", "HEAD:app.py") == "greeting = 'hello'"


def test_a_branch_behind_github_catches_up_and_a_diverged_one_stops(
    settings: Settings, tmp_path: Path, published: list[dict]
) -> None:
    work, origin = _pull_request(tmp_path, base_edit=("README.md", "# demo\n"))
    pusher = tmp_path / "pusher"
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(pusher))
    pushed = _commit(pusher, "notes.txt", "more\n", "More work on GitHub")
    _git(pusher, "push", "-q", "origin", BRANCH)

    caught_up = _run(_task(work), settings, _never)
    assert caught_up.status == "success"
    assert _parents(work)[0] == pushed

    _commit(pusher, "notes.txt", "remote only\n", "Remote only")
    _git(pusher, "push", "-q", "origin", BRANCH)
    _commit(work, "local.txt", "local only\n", "Local only")
    diverged = _run(_task(work), settings, _never)
    assert diverged.status == "failed"
    assert "have different commits" in diverged.reason
