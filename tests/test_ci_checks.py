"""A pull request's CI checks are tracked, and failed ones can be investigated and fixed."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.runtime import ci_checks
from agent_lbw.runtime.ci_checks import failure_context, pull_checks, pull_parts
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.local_runner import _branch_for
from agent_lbw.runtime.store.github import GitHubAppError
from agent_lbw.runtime.store.recording import stored_job
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.web_ui import create_app

PR = "https://github.com/acme/demo/pull/7"
LOG = "\n".join(f"2026-11-24T01:00:{i % 60:02d}.0000000Z line {i}" for i in range(400))
LOG += "\n\x1b[31mFAILED tests/test_app.py::test_home - assert 500 == 200\x1b[0m"
LOG += "\nPost job cleanup.\n" + "\n".join(f"cleanup {i}" for i in range(300))


def fake_github(method: str, path: str, *, token: str) -> object:
    assert token == "tok"
    if path == "/repos/acme/demo/pulls/7":
        return {"head": {"sha": "abc123def4567890"}}
    if path.startswith("/repos/acme/demo/commits/abc123def4567890/check-runs"):
        return {
            "check_runs": [
                {
                    "id": 11,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/acme/demo/actions/runs/1/job/11",
                    "output": {"title": "1 failed", "summary": "pytest exited 1"},
                    "app": {"slug": "github-actions"},
                },
                {
                    "id": 12,
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "github-actions"},
                },
            ]
        }
    if path == "/repos/acme/demo/commits/abc123def4567890/status":
        return {
            "statuses": [
                {
                    "id": 21,
                    "context": "ci/deploy-preview",
                    "state": "error",
                    "description": "Preview build broke",
                    "target_url": "https://ci.example.com/7",
                }
            ]
        }
    if path.startswith("/repos/acme/demo/check-runs/11/annotations"):
        return [{"path": "app/main.py", "start_line": 42, "message": "KeyError: 'user'"}]
    raise GitHubAppError(f"unexpected {path}")


def fake_log(path: str, *, token: str) -> str:
    assert path == "/repos/acme/demo/actions/jobs/11/logs"
    return LOG


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ci_checks._cache.clear()


def test_checks_are_read_failed_first() -> None:
    assert pull_parts(PR) == ("acme", "demo", 7)
    assert pull_parts("https://gitlab.com/a/b/merge_requests/1") is None
    found = pull_checks(PR, "tok", api=fake_github)
    assert found["sha"] == "abc123def4567890"
    assert found["state"] == "failing"
    assert [check["name"] for check in found["checks"]] == ["ci/deploy-preview", "tests", "lint"]
    assert found["failed"] == ["ci/deploy-preview", "tests"]


def test_what_a_failed_check_printed_is_gathered() -> None:
    text = failure_context(PR, "tok", ["tests"], api=fake_github, download=fake_log)
    assert "## tests (failure)" in text
    assert "app/main.py:42: KeyError: 'user'" in text
    assert "FAILED tests/test_app.py::test_home" in text
    assert "\x1b[" not in text and "2026-11-24T01:00" not in text
    assert "[LBW shortened this log to its last lines]" in text
    assert "line 100\n" not in text
    assert "ci/deploy-preview" not in text
    assert "cleanup 299" not in text


def _done(task: Task) -> CycleResult:
    return CycleResult(
        status="success",
        goal=task.goal,
        summary="Added the page.",
        tests_passed=True,
        committed=True,
        published=True,
        commit_sha="abc123def4567890",
        reason="",
        pr_url=PR,
        branch="lbw/add-the-page",
    )


def test_a_person_investigates_failed_checks_and_the_fix_waits_for_approval(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    seen: dict = {}

    def runner(task: Task) -> CycleResult:
        if not task.ci_pr_url:
            return _done(task)
        seen.update(context=task.ci_context, goal=task.goal)
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary="Fixed the KeyError.",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="waiting for approval",
            stage="approval",
        )

    monkeypatch.setattr(ci_checks, "github_api", fake_github)
    monkeypatch.setattr(ci_checks, "github_download", fake_log)
    monkeypatch.setattr("agent_lbw.web_ui._ui_github_token", lambda ui: "tok")
    monkeypatch.setattr("agent_lbw.web_ui.lookup_pull_state", lambda *a, **k: "open")
    monkeypatch.setattr(
        "agent_lbw.runtime.store.github.current_token", lambda root, database_url="": "tok"
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    init_git_repo(tmp_path)
    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        client.get("/api/directory")
        created = client.post(
            "/api/tasks", json={"workspace": str(tmp_path), "goal": "Add the page"}
        )
        first = created.json()["id"]
        for _ in range(100):
            if client.get(f"/api/tasks/{first}").json()["status"] == "success":
                break
            time.sleep(0.05)

        checks = client.get(f"/api/tasks/{first}/checks")
        assert checks.status_code == 200, checks.text
        assert checks.json()["failed"] == ["ci/deploy-preview", "tests"]

        none = client.post(f"/api/tasks/{first}/checks/investigate", json={"checks": ["lint"]})
        assert none.status_code == 409
        started = client.post(f"/api/tasks/{first}/checks/investigate", json={"checks": ["tests"]})
        assert started.status_code == 201, started.text
        run = started.json()
        assert (run["attempt_of"], run["attempt"]) == (first, 2)
        assert (run["triage_gate"], run["approval_gate"]) == ("auto", "ask")
        assert (run["ci_pr_url"], run["ci_checks"]) == (PR, ["tests"])
        assert run["goal"].splitlines()[0] == "Fix failing CI checks on PR #7: tests"
        assert PR in run["goal"]
        for _ in range(100):
            body = client.get(f"/api/tasks/{run['id']}").json()
            if body["status"] == "waiting":
                break
            time.sleep(0.05)
        assert (body["status"], body["stage"]) == ("waiting", "approval")
        assert "FAILED tests/test_app.py::test_home" in seen["context"]
        assert "FAILED" not in seen["goal"]
    finally:
        manager.shutdown(wait=True)
    stored = stored_job(tmp_path, "", run["id"])
    assert stored["ci"] == {"pr_url": PR, "checks": ["tests"]}
    assert "FAILED tests/test_app.py" not in str(stored)


def test_a_merge_conflict_shows_in_the_checks_and_starts_a_merge_run(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    pull = {"head": {"sha": "abc123def4567890"}, "base": {"ref": "main"}, "mergeable": True}

    def github(method: str, path: str, *, token: str) -> object:
        if path == "/repos/acme/demo/pulls/7":
            return pull
        return fake_github(method, path, token=token)

    seen: list[tuple[str, list[str], str]] = []

    def runner(task: Task) -> CycleResult:
        if not task.merge_base:
            return _done(task)
        seen.append((task.merge_base, task.ci_checks, task.ci_context))
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary="Merged main and resolved app.py.",
            tests_passed=None,
            committed=True,
            published=False,
            commit_sha=None,
            reason="waiting for approval",
            stage="approval",
        )

    monkeypatch.setattr(ci_checks, "github_api", github)
    monkeypatch.setattr(ci_checks, "github_download", fake_log)
    monkeypatch.setattr("agent_lbw.web_ui._ui_github_token", lambda ui: "tok")
    monkeypatch.setattr("agent_lbw.web_ui.lookup_pull_state", lambda *a, **k: "open")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    init_git_repo(tmp_path)
    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        client.get("/api/directory")
        first = client.post(
            "/api/tasks", json={"workspace": str(tmp_path), "goal": "Add the page"}
        ).json()["id"]
        for _ in range(100):
            if client.get(f"/api/tasks/{first}").json()["status"] == "success":
                break
            time.sleep(0.05)
        clean = client.get(f"/api/tasks/{first}/checks").json()
        assert clean["merge"] == {"state": "clean", "base": "main"}
        refused = client.post(f"/api/tasks/{first}/checks/merge")
        assert refused.status_code == 409
        assert refused.json() == {"error": "GitHub reports no merge conflict on this pull request"}

        pull.update(mergeable=False, mergeable_state="dirty")
        conflict = client.get(f"/api/tasks/{first}/checks?refresh=true").json()
        assert conflict["merge"] == {"state": "conflict", "base": "main"}
        started = client.post(f"/api/tasks/{first}/checks/merge")
        assert started.status_code == 201, started.text
        run = started.json()
        assert (run["attempt_of"], run["attempt"]) == (first, 2)
        assert (run["merge_base"], run["ci_pr_url"]) == ("main", PR)
        assert (run["triage_gate"], run["approval_gate"]) == ("auto", "ask")
        assert run["goal"].startswith(f"Merge main into the branch of {PR}")
        for _ in range(100):
            body = client.get(f"/api/tasks/{run['id']}").json()
            if body["status"] == "waiting":
                break
            time.sleep(0.05)
        assert body["stage"] == "approval"
        assert seen == [("main", [], "")]
    finally:
        manager.shutdown(wait=True)
    stored = stored_job(tmp_path, "", run["id"])
    assert stored["ci"] == {"pr_url": PR, "checks": [], "merge_base": "main"}


def test_a_change_without_a_pull_request_has_no_checks(settings: Settings, tmp_path: Path) -> None:
    def runner(task: Task) -> CycleResult:
        return CycleResult(
            status="failed",
            goal=task.goal,
            summary="",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="no",
        )

    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        client.get("/api/directory")
        task_id = client.post("/api/tasks", json={"workspace": str(tmp_path), "goal": "x"}).json()[
            "id"
        ]
        assert client.get(f"/api/tasks/{task_id}/checks").status_code == 404
        assert client.post(f"/api/tasks/{task_id}/checks/investigate").status_code == 404
    finally:
        manager.shutdown(wait=True)


def test_a_runner_fix_for_an_open_pull_request_lands_on_its_branch() -> None:
    task = Task(
        id="abcdef1234",
        workspace="/w",
        goal="g",
        auto_commit=True,
        create_pr=True,
        model_name="opus",
        model_base_url="",
        model_api_key="",
        resume_branch="lbw/add-the-page",
        ci_pr_url=PR,
    )
    assert _branch_for(task) == "lbw/add-the-page"
    task.ci_pr_url = ""
    assert _branch_for(task) == "lbw/runner-abcdef12"
    task.resume_branch = "main"
    task.change_base = "abc"
    assert _branch_for(task) == "lbw/runner-abcdef12"


def test_an_approved_fix_comments_on_the_pull_request_and_keeps_its_text(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_lbw.runtime import improve
    from agent_lbw.runtime.project import load_project
    from agent_lbw.sandbox import Workspace
    from agent_lbw.tools.base import ToolResult

    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "lbw/add-the-page"], cwd=tmp_path, check=True)
    calls: list[tuple] = []
    monkeypatch.setattr(improve, "existing_pull_request", lambda workspace, token=None: PR)
    monkeypatch.setattr(improve, "push_changes", lambda *a, **k: ToolResult(True, "pushed"))
    monkeypatch.setattr(improve, "commits_ahead_of_base", lambda *a, **k: 1)
    monkeypatch.setattr(
        improve, "fold_unpublished_commits", lambda *a, **k: ToolResult(True, "1 commit(s)")
    )
    monkeypatch.setattr(
        improve,
        "update_pull_request",
        lambda workspace, title, body, token=None: (
            calls.append(("edit", title)) or ToolResult(True, "")
        ),
    )
    monkeypatch.setattr(
        improve,
        "comment_on_pull_request",
        lambda workspace, body, token=None: calls.append(("comment", body)) or ToolResult(True, ""),
    )
    workspace = Workspace(tmp_path)
    for keep in (True, False):
        published, url, failed, _ = improve._publish_pull_request(
            workspace,
            load_project(tmp_path),
            "Fix failing CI checks on PR #7: tests",
            "Fixed the KeyError in app/main.py.",
            tests_passed=True,
            sha="abc123def4567890",
            committed=True,
            screenshot_files=[],
            token="tok",
            keep_text=keep,
        )
        assert (published, url, failed) == (True, PR, None)
    assert calls[0][0] == "comment"
    assert "LBW pushed abc123de" in calls[0][1] and "Fixed the KeyError" in calls[0][1]
    assert calls[1][0] == "edit"
