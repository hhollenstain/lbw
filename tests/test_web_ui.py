from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.llm.client import model_ids_from_payload, normalize_model_base_url
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.servers import remember_server
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.runtime.workspaces import remember_workspace
from agent_lbw.web_ui import (
    _PR_STATE_CACHE,
    _PR_STATE_LOCK,
    create_app,
)


def _ok_result(goal: str | None = "do the thing") -> CycleResult:
    return CycleResult(
        status="success",
        goal=goal,
        summary="done",
        tests_passed=True,
        committed=False,
        published=False,
        commit_sha=None,
        reason="completed",
    )


def test_task_context_window_is_pinned(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert b'id="context-window"' in home.content
        rejected = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "shrink", "context_window": 128},
        )
        assert rejected.status_code == 400
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "shrink", "context_window": 4096},
        )
        assert created.status_code == 201
        assert created.json()["context_window"] == 4096
    finally:
        manager.shutdown(wait=False)


def test_submit_rejects_missing_workspace(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result())
    try:
        try:
            manager.submit(tmp_path / "missing", "goal")
        except ValueError as exc:
            assert "not a directory" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        manager.shutdown(wait=False)


def test_queue_runs_one_at_a_time(settings: Settings, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    running_seen = []

    def runner(task: Task) -> CycleResult:
        running_seen.append(task.id)
        started.set()
        assert release.wait(timeout=2)
        return _ok_result(task.goal)

    manager = TaskManager(settings, max_concurrent=1, runner=runner)
    try:
        first = manager.submit(tmp_path, "one")
        second = manager.submit(tmp_path, "two")
        assert started.wait(timeout=2)
        assert manager.get(first.id).status == "running"
        assert manager.get(second.id).status == "queued"
        release.set()
        manager.shutdown(wait=True, cancel_futures=False)
        assert manager.get(first.id).status == "success"
        assert manager.get(second.id).status == "success"
        assert running_seen == [first.id, second.id]
    finally:
        release.set()
        manager.shutdown(wait=False)


def test_rerun_on_main_starts_clean_instead_of_resuming_the_dirty_tree(
    settings: Settings, tmp_path: Path
) -> None:
    seen: list[Task] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task)
        return CycleResult(
            status="failed",
            goal=task.goal,
            summary="no",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="tests failed; commit skipped",
        )

    manager = TaskManager(settings, runner=runner)
    try:
        first = manager.submit(tmp_path, "Fix the player")
        deadline = time.time() + 3
        while time.time() < deadline and first.status != "failed":
            time.sleep(0.05)
        first.branch = "main"
        first.sha_before = "abc123"
        again = manager.rerun(first.id)
        assert again is not None
        assert again.resume_branch is None
        assert again.resume_sha is None
        assert again.base_branch == "main"
        first.branch = "lbw/fix-the-player"
        feature = manager.rerun(first.id)
        assert feature is not None
        assert feature.resume_branch == "lbw/fix-the-player"
    finally:
        manager.shutdown(wait=False)


def test_failed_goal_stays_failed_when_nothing_else_is_queued(
    settings: Settings, tmp_path: Path
) -> None:
    goals: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        goals.append(task.goal)
        return CycleResult(
            status="failed",
            goal=task.goal,
            summary="missed it",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="goal not met; PR skipped: only error handling",
        )

    manager = TaskManager(settings, runner=runner)
    try:
        manager.submit(tmp_path, 'ERROR: Node not found: "Player"')
        deadline = time.time() + 3
        while time.time() < deadline:
            if any(task.status == "failed" for task in manager.list()):
                break
            time.sleep(0.05)
        time.sleep(0.2)
        assert goals == ['ERROR: Node not found: "Player"']
        tasks = manager.list()
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
    finally:
        manager.shutdown(wait=False)


def test_continuous_queues_next_until_stopped(settings: Settings, tmp_path: Path) -> None:
    holder: list[TaskManager] = []
    runs: list[str] = []

    def runner(task: Task) -> CycleResult:
        runs.append(task.id)
        if len(runs) >= 2:
            holder[0].stop_continuous(tmp_path)
        return CycleResult(
            status="success",
            goal=task.goal or "picked",
            summary="done",
            tests_passed=True,
            committed=True,
            published=True,
            commit_sha="abc",
            reason="completed",
            pr_url="https://example.test/pull/1",
            merged=True,
        )

    manager = TaskManager(settings, max_concurrent=1, runner=runner)
    holder.append(manager)
    try:
        first = manager.submit(tmp_path, None, continuous=True)
        assert first.continuous is True
        assert first.create_pr is True
        deadline = time.time() + 3
        while time.time() < deadline and len(runs) < 2:
            time.sleep(0.05)
        assert len(runs) == 2
        manager.stop_continuous(tmp_path)
        manager.shutdown(wait=True, cancel_futures=False)
        assert manager.is_continuous_active(tmp_path) is False
        listed = manager.list()
        assert all(item.continuous for item in listed)
        assert any(item.merged for item in listed)
    finally:
        manager.shutdown(wait=False)


def test_live_task_logs_follow_the_running_task(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.web_ui import _live_task_logs

    gate = threading.Event()

    def runner(task: Task) -> CycleResult:
        task.logs.append("model hello from the server")
        gate.wait(timeout=2)
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        queued = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "watch the console"},
        )
        assert queued.status_code == 201
        deadline = time.time() + 2
        lines: list[str] = []
        while time.time() < deadline:
            _task_id, lines = _live_task_logs(manager)
            if any("hello from the server" in line for line in lines):
                break
            time.sleep(0.05)
        assert any("hello from the server" in line for line in lines)
        assert any(getattr(route, "path", "") == "/api/events/stream" for route in app.routes)
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_serve_returns_after_keyboard_interrupt(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.web_ui import serve

    captured: dict[str, int | None] = {}

    class FakeServer:
        def __init__(self, config: object) -> None:
            captured["timeout"] = getattr(config, "timeout_graceful_shutdown", None)

        def run(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr("agent_lbw.web_ui.uvicorn.Server", FakeServer)
    serve(tmp_path, settings, host="127.0.0.1", port=8765)
    assert captured["timeout"] == 1


def test_console_event_payload_splits_agent_and_model() -> None:
    from agent_lbw.web_ui import _console_event_payload

    payload = _console_event_payload(
        {
            "kind": "llm",
            "purpose": "agent",
            "ok": True,
            "at": "2026-09-29T01:00:00Z",
            "messages": [
                {"role": "system", "content": "You are lbw."},
                {"role": "user", "content": "fix the live console colors"},
                {"role": "assistant", "content": "I will look at index.html"},
            ],
            "response": "Colored agent and model lines in the live console.",
        }
    )
    assert payload["agent"] == "fix the live console colors"
    assert payload["model"] == "Colored agent and model lines in the live console."
    assert payload["kind"] == "llm"


def test_web_ui_queues_and_lists_tasks(settings: Settings, tmp_path: Path) -> None:
    gate = threading.Event()

    def runner(task: Task) -> CycleResult:
        gate.wait(timeout=2)
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert home.content.strip().startswith(b"<!DOCTYPE html>")
        assert b"</html>" in home.content
        assert b"lbw" in home.content
        assert b'id="app-logo"' in home.content
        assert b"<title>LBW</title>" in home.content
        assert b"<title>" in home.content
        assert b"<header>" not in home.content
        assert b'id="toggle-sidebar"' in home.content
        assert b'id="toasts"' in home.content
        assert b'id="notify-toggle"' in home.content
        assert b'id="notify-history"' in home.content
        assert b"notice-success" in home.content
        assert b"notice-error" in home.content
        assert b"notice-warning" in home.content
        assert b"notice-skipped" in home.content
        assert b"function notify(" in home.content
        assert b'class="pr-link' in home.content
        assert b"function prLinkHtml(" in home.content
        assert b"function rerunFailedTask(" in home.content
        assert b"data-rerun-id" in home.content
        assert b'id="rerun-goal-picker"' in home.content
        assert b'id="editor-scheme"' in home.content
        assert b'<option value="cursor">Cursor</option>' in home.content
        assert b"file-open-link" in home.content
        assert b"function editorFileUrl(" in home.content
        assert b'id="open-live-console"' not in home.content
        assert b'id="live-console"' not in home.content
        assert b"function appendConsoleEntry(" not in home.content
        assert b"data-history-update" in home.content
        assert b"card-actions" in home.content
        assert b"pr-state" in home.content
        assert b"pr-merged" in home.content
        assert b"function runTextOverflows(" in home.content
        assert b"data-expand-run" in home.content
        assert b"-webkit-line-clamp: 3" in home.content
        assert b"/api/pulls/status" in home.content
        assert b"/rerun" in home.content
        assert b"/rework" in home.content
        assert b"function activityLogHtml(" in home.content
        assert b"function oneJobPerPull(" in home.content
        assert b"function reworkTask(" in home.content
        assert b"GITHUB_MARK" in home.content
        assert b'event.kind === "pr"' in home.content
        assert b'event.kind === "ui"' in home.content
        assert b"function renderFileChanges(" in home.content
        assert b"function renderChangeBranch(" in home.content
        assert b"function fileReviewDiff(" in home.content
        assert b"file-change-tree" in home.content
        assert b"file-change-view" in home.content
        assert b"data-change-path" in home.content
        assert b'data-task-pane="changes"' in home.content
        assert b'data-task-pane="screenshots"' in home.content
        assert b'<link rel="stylesheet" href="/static/themes.css">' in home.content
        assert b'<link rel="stylesheet" href="/static/layout.css">' in home.content
        assert b"<style>" in home.content
        assert home.content.index(b"<style>") < home.content.index(b"* { box-sizing")
        assert b'id="run-detail"' in home.content
        assert b'id="run-list"' in home.content
        assert b'id="run-search"' in home.content
        assert b'id="refresh-pr-status"' in home.content
        assert b'data-status-filter="running"' in home.content
        assert b'data-status-filter="success"' in home.content
        assert b'data-status-filter="skipped"' in home.content
        assert b'data-status-filter="failed"' in home.content
        assert b"function workspaceRuns(" in home.content
        assert b"function ensureRunSelection(" in home.content
        assert b"/api/tasks?workspace=" in home.content
        assert home.content.index(b'id="run-detail"') < home.content.index(b'id="run-list"')
        assert b"data-main-pane=" not in home.content
        assert b'id="past-runs"' not in home.content
        assert b'id="running-chicken"' not in home.content
        assert b"function runningChickenHtml(" not in home.content
        assert b"function syncRunningChicken(" not in home.content
        assert b'id="task-tokens"' in home.content
        assert b"data-task-tokens" in home.content
        assert b"function syncTaskTokens(" in home.content
        assert b"function tokenUsageFromItem(" in home.content
        assert b"function parseTokenValue(" in home.content
        assert b"function goalText(" in home.content
        assert b"function goalHeadline(" in home.content
        assert b'return item.goal.split("\\n")[0];' not in home.content
        assert b"function taskTokensHover(" in home.content
        assert b"function renderTokenBar(" in home.content
        assert b"function rememberContextWindow(" in home.content
        assert b"percentLabel + " in home.content
        assert b'winFmt + " tokens"' in home.content
        assert b'id="model-hint"' in home.content
        assert b"function loadGrantedModels(" in home.content
        assert b"/api/org/models?workspace=" in home.content
        assert b'id="llm-server-toggle"' not in home.content
        assert b'id="base-url"' not in home.content
        assert b'id="load-models"' not in home.content
        assert b".task-pane {\n      display: none;" not in home.content
        assert b"function taskPaneFromTab(" in home.content
        assert b"/api/ui-screenshot" in home.content
        assert b'id="file-changes"' in home.content
        assert b"function focusTimelineStage(" in home.content
        assert b'data-stage="' in home.content
        assert b"pinTimelineStage" in home.content
        assert b"function renderProgressStageBar(" in home.content
        assert b">Log</button>" in home.content
        assert b"progressHtml + logHtml" not in home.content
        assert b"${taskProgress}" not in home.content
        assert b"Before tests" in home.content
        assert b'event.kind === "lint"' in home.content
        assert b"Lint failed" in home.content
        assert b'id: "lint"' in home.content
        assert b"label || name" not in home.content
        assert b"\n  10|" not in home.content

        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Improve the UI", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]

        listed = client.get("/api/tasks")
        assert listed.status_code == 200
        body = listed.json()
        assert len(body) == 1
        assert body[0]["id"] == task_id
        assert body[0]["goal"] == "Improve the UI"
        assert body[0]["model"] == settings.model_name
        assert body[0]["base_url"] == settings.model_base_url
        assert body[0]["status"] in {"queued", "running"}
        assert "tokens_used" in body[0]
        assert "context_window" in body[0]
        if body[0]["status"] == "running":
            assert body[0]["started_at"]
            assert body[0]["finished_at"] is None
        live = manager.get(task_id)
        assert live is not None
        live.events.append(
            {
                "kind": "llm",
                "ok": True,
                "prompt_tokens": 1200,
                "completion_tokens": 80,
                "total_tokens": 1280,
            }
        )
        live.context_window = 32768
        usage = client.get(f"/api/tasks/{task_id}").json()
        assert usage["tokens_used"] == 1200
        assert usage["tokens_total"] == 1280
        assert usage["context_window"] == 32768

        missing = client.get("/api/tasks/nope")
        assert missing.status_code == 404
        assert missing.json()["error"] == "task not found"

        bad = client.post("/api/tasks", json={"workspace": str(tmp_path / "nope")})
        assert bad.status_code == 400
        assert "error" in bad.json()
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_web_ui_paginates_tasks_and_serves_favicon(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        for index in range(3):
            created = client.post(
                "/api/tasks",
                json={
                    "workspace": str(tmp_path),
                    "goal": f"Task {index}",
                    "auto_commit": False,
                },
            )
            assert created.status_code == 201
        listed = client.get("/api/tasks")
        assert listed.status_code == 200
        assert isinstance(listed.json(), list)
        assert len(listed.json()) == 3
        paged = client.get("/api/tasks", params={"page": 1, "page_size": 2})
        assert paged.status_code == 200
        body = paged.json()
        assert body["total"] == 3
        assert body["total_pages"] == 2
        assert body["page"] == 1
        assert len(body["items"]) == 2
        page_two = client.get("/api/tasks", params={"page": 2, "page_size": 2}).json()
        assert page_two["page"] == 2
        assert len(page_two["items"]) == 1
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200
        assert b"<svg" in icon.content
        assert b"LBW" in icon.content
        assert b"#c4e38a" in icon.content
        assert b"#11140f" in icon.content
        themes = client.get("/static/themes.css")
        assert themes.status_code == 200
        assert b"--bg: #ffffff" in themes.content
        assert b"--accent:" in themes.content
        assert b"--navy:" in themes.content
        assert b'[data-theme="dark"]' in themes.content
        layout = client.get("/static/layout.css")
        assert layout.status_code == 200
        assert b"box-sizing: border-box" in layout.content
    finally:
        manager.shutdown(wait=False)


def test_web_ui_lists_tasks_for_one_workspace(settings: Settings, tmp_path: Path) -> None:
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=home)
    client = TestClient(app)
    try:
        for workspace, goal in ((home, "Home task"), (other, "Other task")):
            created = client.post(
                "/api/tasks",
                json={"workspace": str(workspace), "goal": goal, "auto_commit": False},
            )
            assert created.status_code == 201
        home_tasks = client.get("/api/tasks", params={"workspace": str(home)}).json()
        other_tasks = client.get("/api/tasks", params={"workspace": str(other)}).json()
        assert [item["goal"] for item in home_tasks] == ["Home task"]
        assert [item["goal"] for item in other_tasks] == ["Other task"]
        assert len(client.get("/api/tasks").json()) == 2
    finally:
        manager.shutdown(wait=False)


def test_finished_task_records_start_and_finish_times(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Time this", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"} and match["finished_at"]:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["started_at"]
        assert body["finished_at"]
        assert body["finished_at"] >= body["started_at"]
        assert body["pr_url"] is None
    finally:
        manager.shutdown(wait=False)


def test_finished_task_includes_pr_url(settings: Settings, tmp_path: Path) -> None:
    pr = "https://github.com/acme/repo/pull/12"

    def runner(task: Task) -> CycleResult:
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="opened a pull request",
            tests_passed=True,
            committed=True,
            published=True,
            commit_sha="abc123",
            reason="completed",
            pr_url=pr,
            events=[{"kind": "pr", "url": pr, "message": pr}],
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship it", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["published"] is True
        assert body["pr_url"] == pr
    finally:
        manager.shutdown(wait=False)


def test_submit_uses_requested_model(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    try:
        task = manager.submit(
            tmp_path,
            "goal",
            model_name="other-coder",
            model_base_url="10.0.0.8:8000",
        )
        assert task.model_name == "other-coder"
        assert task.model_base_url == "http://10.0.0.8:8000/v1"
        fallback = manager.submit(tmp_path, "goal")
        assert fallback.model_name == settings.model_name
        assert fallback.model_base_url == settings.model_base_url
    finally:
        manager.shutdown(wait=False)


def test_web_ui_lists_models_and_queues_with_selection(settings: Settings, tmp_path: Path) -> None:
    gate = threading.Event()

    def runner(task: Task) -> CycleResult:
        gate.wait(timeout=2)
        return _ok_result(task.goal)

    manager = TaskManager(
        settings,
        runner=runner,
        models_fn=lambda: ["alpha-coder", "beta-coder"],
    )
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b'id="model"' in home.content
        assert b'id="base-branch"' in home.content
        assert b"function loadBranches(" in home.content
        assert b'id="server-history"' not in home.content
        assert b"Load models" not in home.content

        models = client.get("/api/models")
        assert models.status_code == 200
        body = models.json()
        assert body["default"] == settings.model_name
        assert body["models"][0] == settings.model_name
        assert "alpha-coder" in body["models"]
        assert "beta-coder" in body["models"]
        assert "context_window" in body

        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Use the other model",
                "model": "beta-coder",
                "auto_commit": False,
            },
        )
        assert created.status_code == 201
        assert created.json()["model"] == "beta-coder"
        assert created.json()["base_url"] == settings.model_base_url
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_web_ui_lists_models_from_requested_host(settings: Settings, tmp_path: Path) -> None:
    gate = threading.Event()

    def runner(task: Task) -> CycleResult:
        gate.wait(timeout=2)
        return _ok_result(task.goal)

    def models_fn(url: str, key: str) -> list[str]:
        if "10.0.0.8" in url:
            return ["remote-coder"]
        return ["alpha-coder", "beta-coder"]

    manager = TaskManager(settings, runner=runner, models_fn=models_fn)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        remote = client.get("/api/models", params={"base_url": "10.0.0.8:8000"})
        assert remote.status_code == 200
        body = remote.json()
        assert body["base_url"] == "http://10.0.0.8:8000/v1"
        assert body["models"] == ["remote-coder"]
        assert settings.model_name not in body["models"]

        posted = client.post(
            "/api/models",
            json={"base_url": "10.0.0.8:8000", "model": "remote-coder"},
        )
        assert posted.status_code == 200
        assert posted.json()["models"] == ["remote-coder"]
        assert posted.json()["base_url"] == "http://10.0.0.8:8000/v1"
        assert posted.json()["servers"][0]["url"] == "http://10.0.0.8:8000/v1"
        assert posted.json()["last_model"] == "remote-coder"

        listed = client.get("/api/servers")
        assert listed.status_code == 200
        assert listed.json()["servers"][0]["url"] == "http://10.0.0.8:8000/v1"
        assert listed.json()["last_base_url"] == "http://10.0.0.8:8000/v1"
        assert listed.json()["last_model"] == "remote-coder"
        saved = json.loads((tmp_path / ".lbw" / "servers.json").read_text(encoding="utf-8"))
        assert saved["servers"][0]["url"] == "http://10.0.0.8:8000/v1"
        assert saved["last_model"] == "remote-coder"

        home = client.get("/")
        assert b"http://10.0.0.8:8000/v1" not in home.content
        assert b'id="base-url"' not in home.content

        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Use the remote host",
                "model": "remote-coder",
                "base_url": "10.0.0.8:8000",
                "auto_commit": False,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["model"] == "remote-coder"
        assert payload["base_url"] == "http://10.0.0.8:8000/v1"
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_list_models_skips_local_default_for_other_host(settings: Settings) -> None:
    manager = TaskManager(
        settings,
        runner=lambda task: _ok_result(task.goal),
        models_fn=lambda url, key: ["remote-a"],
    )
    try:
        remote = manager.list_models(base_url="http://10.0.0.5:11434")
        assert remote == ["remote-a"]
        local = manager.list_models()
        assert local[0] == settings.model_name
        assert "remote-a" in local
    finally:
        manager.shutdown(wait=False)


def test_history_uses_the_stored_job_when_a_database_is_configured(
    settings: Settings, tmp_path: Path
) -> None:
    from agent_lbw.runtime.store.recording import record_task, sync_task

    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "disk-only.json").write_text(
        json.dumps(
            {"id": "disk-only", "status": "success", "goal": "Only on disk", "summary": "disk"}
        ),
        encoding="utf-8",
    )
    job = type("Job", (), {})()
    job.id = "job-kept"
    job.workspace = str(tmp_path)
    job.goal = "Stored goal"
    job.status = "success"
    job.branch = ""
    job.pr_url = ""
    job.requester_id = ""
    job.summary = "stored summary"
    job.reason = ""
    job.plan = "Do the work"
    job.events = []
    job.evidence = []
    job.criteria = []
    job.decision = ""
    job.tests_passed = True
    job.committed = False
    job.published = False
    job.stage = ""
    job.model_name = ""
    job.model_base_url = ""
    record_task(tmp_path, "", job)
    sync_task(tmp_path, "", job)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        items = client.get("/api/history").json()["items"]
        assert [item["id"] for item in items] == ["job-kept"]
        assert items[0]["goal"] == "Stored goal"
        assert items[0]["plan"] == "Do the work"
    finally:
        manager.shutdown(wait=False)


def test_history_endpoint_reads_run_logs(settings: Settings, tmp_path: Path) -> None:
    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "20260919T180000Z.json").write_text(
        '{"status": "success", "goal": "Past goal", "summary": "done"}\n',
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        history = client.get("/api/history")
        assert history.status_code == 200
        body = history.json()
        # Paginated response format
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == "20260919T180000Z"
        assert body["items"][0]["created_at"] == "2026-09-19T18:00:00Z"
        assert body["items"][0]["goal"] == "Past goal"
        assert "tokens_used" in body["items"][0]
        assert "context_window" in body["items"][0]
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] == 1
        assert body["total_pages"] == 1
    finally:
        manager.shutdown(wait=False)


def test_history_lists_a_run_log_and_its_stored_job_once(
    settings: Settings, tmp_path: Path
) -> None:
    created = "2026-11-04T20:01:47Z"
    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "20261104T200512Z.json").write_text(
        json.dumps(
            {
                "id": "20261104T200512Z",
                "status": "success",
                "goal": "Update the docs",
                "summary": "docs now match the app",
                "created_at": created,
                "pr_url": "https://github.com/acme/repo/pull/4",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {
                    "id": "task-docs",
                    "status": "success",
                    "goal": "Update the docs",
                    "summary": "docs now match the app",
                    "created_at": created,
                    "pr_url": "https://github.com/acme/repo/pull/4",
                },
                {
                    "id": "task-earlier",
                    "status": "success",
                    "goal": "Update the docs",
                    "summary": "first pass",
                    "created_at": "2026-11-03T15:00:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        items = client.get("/api/history", params={"page_size": 100}).json()["items"]
        assert [item["id"] for item in items] == ["task-docs", "task-earlier"]
    finally:
        manager.shutdown(wait=False)


def test_history_fills_token_window_from_current_model(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.web_ui.effective_context_window",
        lambda *args, **kwargs: 32768,
    )
    runs = tmp_path / ".lbw" / "runs"
    runs.mkdir(parents=True)
    (runs / "20260919T180000Z.json").write_text(
        json.dumps(
            {
                "status": "success",
                "goal": "Past goal",
                "events": [
                    {"kind": "llm", "ok": True, "prompt_tokens": 6768, "total_tokens": 7000},
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        item = client.get("/api/history").json()["items"][0]
        assert item["tokens_used"] == 6768
        assert item["context_window"] == 32768
    finally:
        manager.shutdown(wait=False)


def test_history_search_matches_pr_url(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "goal": "Ship it",
                    "published": True,
                    "pr_url": "https://github.com/acme/repo/pull/12",
                    "created_at": "2026-09-20T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        found = client.get("/api/history", params={"search": "pull/12"})
        assert found.status_code == 200
        items = found.json()["items"]
        assert len(items) == 1
        assert items[0]["pr_url"] == "https://github.com/acme/repo/pull/12"
        missed = client.get("/api/history", params={"search": "no-such-pr"})
        assert missed.json()["items"] == []
    finally:
        manager.shutdown(wait=False)


def test_task_and_history_polls_omit_llm_transcripts(settings: Settings, tmp_path: Path) -> None:
    blob = ("x" * 800) + "SECRET_TRANSCRIPT"
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "goal": "Past run",
                    "events": [
                        {
                            "kind": "llm",
                            "ok": True,
                            "messages": [{"role": "system", "content": blob}],
                            "response": blob,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    gate = threading.Event()

    def runner(task: Task) -> CycleResult:
        task.events.append(
            {
                "kind": "llm",
                "ok": True,
                "messages": [{"role": "user", "content": blob}],
                "response": blob,
                "prompt_tokens": 9,
            }
        )
        gate.wait(timeout=2)
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert b"refreshQueued" in home.content
        assert b"async function refreshNow(" in home.content
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "stay responsive"},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match.get("events"):
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        dumped = json.dumps(body)
        assert "SECRET_TRANSCRIPT" not in dumped
        assert "messages" not in body["events"][0]
        history = client.get("/api/history").json()
        assert "SECRET_TRANSCRIPT" not in json.dumps(history)
        for item in history["items"]:
            for event in item.get("events") or []:
                assert "messages" not in event
    finally:
        gate.set()
        manager.shutdown(wait=False)


def test_history_load_reuses_unchanged_files(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.web_ui import UiState

    (tmp_path / "history.json").write_text(
        json.dumps([{"status": "success", "goal": "cached"}]),
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    try:
        ui = UiState(
            manager,
            default_workspace=tmp_path,
            default_goal=None,
            default_create_pr=False,
        )
        first = ui.load_history(tmp_path)
        assert first[0]["goal"] == "cached"

        def boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("history should be cached")

        monkeypatch.setattr("agent_lbw.web_ui.json.loads", boom)
        monkeypatch.setattr("agent_lbw.web_ui.json.load", boom)
        second = ui.load_history(tmp_path)
        assert second is first
    finally:
        manager.shutdown(wait=False)


def test_history_poll_skips_the_body_when_the_token_matches(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "history.json").write_text(
        json.dumps([{"status": "success", "goal": "cached", "created_at": "2026-10-04T00:00:00Z"}]),
        encoding="utf-8",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        first = client.get("/api/history", params={"page_size": 100})
        assert first.status_code == 200
        body = first.json()
        assert body["unchanged"] is False
        assert body["items"][0]["goal"] == "cached"
        token = body["token"]
        assert token

        def boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("unchanged history should not be parsed again")

        monkeypatch.setattr("agent_lbw.web_ui.UiState.load_history", boom)
        second = client.get("/api/history", params={"page_size": 100, "token": token})
        assert second.status_code == 200
        skipped = second.json()
        assert skipped["unchanged"] is True
        assert skipped["items"] == []
        assert skipped["token"] == token
    finally:
        manager.shutdown(wait=False)


def test_remember_server_dedupes_and_keeps_newest_first(tmp_path: Path) -> None:
    first = remember_server(tmp_path, "10.0.0.8:8000", default="http://127.0.0.1:11434/v1")
    assert first[0]["url"] == "http://10.0.0.8:8000/v1"
    assert "http://127.0.0.1:11434/v1" in [s["url"] for s in first]
    again = remember_server(tmp_path, "http://10.0.0.8:8000/v1")
    assert again[0]["url"] == "http://10.0.0.8:8000/v1"
    assert again.count({"url": "http://10.0.0.8:8000/v1", "alias": None}) == 1
    newer = remember_server(tmp_path, "http://10.0.0.9:11434/v1")
    assert newer[0]["url"] == "http://10.0.0.9:11434/v1"
    assert newer[1]["url"] == "http://10.0.0.8:8000/v1"
    gitignore = (tmp_path / ".lbw" / ".gitignore").read_text(encoding="utf-8")
    assert "*" in gitignore
    assert ".lbw/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    remember_server(tmp_path, "http://10.0.0.8:8000/v1", model="remote-coder")
    remember_server(tmp_path, "http://10.0.0.8:8000/v1")
    saved = json.loads((tmp_path / ".lbw" / "servers.json").read_text(encoding="utf-8"))
    assert saved["last_base_url"] == "http://10.0.0.8:8000/v1"
    assert saved["last_model"] == "remote-coder"


def test_model_ids_from_openai_payload() -> None:
    names = model_ids_from_payload(
        {"data": [{"id": "qwen2.5-coder:14b"}, {"id": "llama3.1"}, {"id": "llama3.1"}]}
    )
    assert names == ["qwen2.5-coder:14b", "llama3.1"]


def test_normalize_model_base_url() -> None:
    assert normalize_model_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert normalize_model_base_url("http://10.0.0.5:11434") == "http://10.0.0.5:11434/v1"
    assert normalize_model_base_url("10.0.0.5:8000") == "http://10.0.0.5:8000/v1"
    assert normalize_model_base_url("https://llm.example.com/v1/") == "https://llm.example.com/v1"
    try:
        normalize_model_base_url("  ")
    except ValueError as exc:
        assert "required" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_web_ui_workspace_picker_browse_select_create(settings: Settings, tmp_path: Path) -> None:
    other = tmp_path / "other-app"
    other.mkdir()
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b'id="open-workspace-picker"' not in home.content
        assert b'aria-label="Add project"' not in home.content
        assert b'aria-label="Archive project"' not in home.content
        assert b'id="archive-project"' not in home.content
        assert b'id="workspace-picker"' in home.content
        assert b'id="project-select"' in home.content
        assert b'id="project-search"' in home.content
        assert b"function writePageUrl(" in home.content
        assert b'id="archived-workspaces"' in home.content
        assert b'id="workspace-tabs"' not in home.content
        assert b'id="guidelines"' in home.content
        assert b'id="open-settings"' in home.content
        sidebar = home.content.split(b'id="sidebar"', 1)[1].split(b"</aside>", 1)[0]
        assert b'id="settings-dock"' in sidebar
        assert b'id="open-profile"' in sidebar
        assert b'id="theme-toggle"' in sidebar
        assert b'id="open-workspace-page"' not in home.content
        assert b'id="settings-panel"' not in home.content
        assert b"main.collapsed button.rail-item" in home.content
        assert b'id="workspace-summary"' in home.content
        assert b'class="icon-github"' in home.content
        assert b'class="icon-folder"' in home.content
        assert b'id="github-issues"' in home.content
        assert b'id="github-goals-section"' in home.content
        assert b'id="github-owner"' not in home.content
        assert b'id="github-repo"' not in home.content
        assert b'id="load-goals"' not in home.content
        personal = home.content.split(b'id="personal-view"', 1)[1].split(
            b'id="organization-view"', 1
        )[0]
        assert b'id="editor-scheme"' in personal
        assert b'id="save-guidelines"' in home.content
        assert b'id="project-skills-root"' in home.content
        assert home.content.count(b'id="project-skills-root"') == 1
        assert b'data-rail="skills"' in home.content
        assert b'id="skills-panel"' in home.content
        script = client.get("/static/app/app.js").content
        assert b'id:"skills-search"' in script
        assert b'"data-skill-repo"' in script
        assert b'id:"workspace-skills"' in script
        assert b'data-rail="rules"' in home.content
        assert b'id="rules-view"' in home.content
        assert b'id="skills-popup"' not in home.content
        assert b'id="guidelines-popup"' not in home.content
        assert b'id:"clone-skill-source"' in script
        assert b'id:"skill-info-pop"' in script
        assert b"skill-markdown" in home.content
        assert b'id="workspace" name="workspace" type="hidden"' in home.content
        assert b"Clone a repository" in home.content

        listed = client.get("/api/workspaces")
        assert listed.status_code == 200
        body = listed.json()
        assert body["current"] == ""
        assert body["workspaces"] == []
        assert body["home"]

        browse = client.get("/api/workspaces/browse", params={"path": str(tmp_path)})
        assert browse.status_code == 200
        listing = browse.json()
        assert listing["path"] == str(tmp_path.resolve())
        names = [item["name"] for item in listing["entries"]]
        assert "other-app" in names

        selected = client.post("/api/workspaces/select", json={"path": str(other)})
        assert selected.status_code == 200
        assert selected.json()["current"] == str(other.resolve())
        saved = json.loads((tmp_path / ".lbw" / "workspaces.json").read_text(encoding="utf-8"))
        assert saved["last"] == str(other.resolve())

        created = client.post(
            "/api/workspaces/create",
            json={"path": str(tmp_path / "fresh-app")},
        )
        assert created.status_code == 200
        fresh = tmp_path / "fresh-app"
        assert created.json()["current"] == str(fresh.resolve())
        assert (fresh / ".lbw" / "config.yaml").exists()
        assert (fresh / ".git").exists()

        missing = client.post("/api/workspaces/select", json={"path": str(tmp_path / "nope")})
        assert missing.status_code == 400
        empty_clone = client.post("/api/workspaces/clone", json={"url": "  "})
        assert empty_clone.status_code == 400
    finally:
        manager.shutdown(wait=False)


def test_web_ui_workspace_guidelines_and_tabs(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        listed = client.get("/api/workspaces")
        body = listed.json()
        assert body["current"] == str(tmp_path.resolve())
        assert "You are LBW" in body["default_guidelines"]
        assert body["custom_guidelines"] is False
        saved = client.put(
            "/api/workspaces/guidelines",
            json={
                "path": str(tmp_path),
                "guidelines": "Prefer pytest. Never skip tests.",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["custom_guidelines"] is True
        assert "Prefer pytest" in saved.json()["guidelines"]
        created = client.post(
            "/api/workspaces/create",
            json={
                "path": str(tmp_path / "fresh-rules"),
                "guidelines": "Use Go modules.",
            },
        )
        assert created.status_code == 200
        fresh = tmp_path / "fresh-rules"
        payload = created.json()
        assert payload["current"] == str(fresh.resolve())
        assert payload["custom_guidelines"] is True
        assert "Use Go modules" in payload["guidelines"]
        paths = [item["path"] for item in payload["workspaces"]]
        assert str(tmp_path.resolve()) in paths
        assert str(fresh.resolve()) in paths
        switched = client.post("/api/workspaces/select", json={"path": str(tmp_path)})
        assert switched.status_code == 200
        assert switched.json()["current"] == str(tmp_path.resolve())
        switched_paths = [item["path"] for item in switched.json()["workspaces"]]
        assert str(tmp_path.resolve()) in switched_paths
        assert str(fresh.resolve()) in switched_paths
        assert switched.json()["custom_guidelines"] is True
        reset = client.put(
            "/api/workspaces/guidelines",
            json={"path": str(tmp_path), "guidelines": ""},
        )
        assert reset.status_code == 200
        assert reset.json()["custom_guidelines"] is False
    finally:
        manager.shutdown(wait=False)


def test_the_checkout_volume_is_not_a_project_for_each_organization(
    settings: Settings, tmp_path: Path
) -> None:
    shelf = tmp_path / "workspaces"
    shelf.mkdir()
    project = shelf / "demo"
    project.mkdir()
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    book = shelf / ".lbw" / "schedule-files" / "abc"
    book.mkdir(parents=True)
    remember_workspace(shelf, shelf)
    remember_workspace(shelf, project)
    remember_workspace(shelf, book)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=shelf)
    client = TestClient(app)
    try:
        listed = client.get("/api/workspaces")
        assert listed.status_code == 200
        assert listed.json()["workspaces"] == []
        assert listed.json()["current"] == ""
        from agent_lbw.runtime.account import load_account
        from agent_lbw.runtime.db import database
        from agent_lbw.runtime.store.projects import ensure_project, grant_project

        account = load_account(shelf)
        with database(shelf) as db:
            saved = ensure_project(
                db,
                account["org"]["id"],
                name="demo",
                kind="github",
                locator="acme/demo",
            )
            grant_project(
                db,
                saved["id"],
                subject_type="identity",
                subject_id=account["current_user_id"],
                access="use",
            )
        listed = client.get("/api/workspaces")
        paths = [item["path"] for item in listed.json()["workspaces"]]
        assert paths == ["acme/demo"]
        assert listed.json()["current"] == "acme/demo"
        assert str(project.resolve()) not in paths
        home = client.get("/")
        assert b'id="project-empty" hidden' in home.content
        assert b">acme/demo<" in home.content
    finally:
        manager.shutdown(wait=False)


def test_web_ui_lists_a_project_row_not_a_remembered_folder(
    settings: Settings, tmp_path: Path
) -> None:
    remembered = tmp_path / "picked"
    remembered.mkdir()
    kept = tmp_path / "kept"
    kept.mkdir()
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "workspaces.json").write_text(
        json.dumps({"last": str(remembered.resolve()), "workspaces": [{"path": str(remembered)}]})
        + "\n",
        encoding="utf-8",
    )
    from agent_lbw.runtime.account import load_account
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.projects import ensure_project

    account = load_account(tmp_path)
    with database(tmp_path) as db:
        ensure_project(
            db,
            account["org"]["id"],
            name="kept",
            kind="folder",
            locator=str(kept.resolve()),
        )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/workspaces")
        assert listed.status_code == 200
        paths = [item["path"] for item in listed.json()["workspaces"]]
        assert paths == [str(kept.resolve())]
        assert str(remembered.resolve()) not in paths
    finally:
        manager.shutdown(wait=False)


def test_web_ui_defaults_create_pr_on_for_a_git_workspace(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        meta = client.get("/api/meta")
        assert meta.status_code == 200
        assert meta.json()["default_create_pr"] is True
        home = client.get("/")
        assert b'id="create-pr"' in home.content
        assert b'id="create-pr" type="checkbox" checked' in home.content
        assert b'id="keep-improving"' in home.content
        assert b'id="stop-improving"' in home.content
        assert b"/api/continuous/stop" in home.content
        assert b'getElementById("keep-improving")' in home.content
        assert b'getElementById("stop-improving")' in home.content
    finally:
        manager.shutdown(wait=False)


def test_web_ui_queues_and_stops_keep_improving(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        queued = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "continuous": True},
        )
        assert queued.status_code == 201
        body = queued.json()
        assert body["continuous"] is True
        assert body["create_pr"] is True
        meta = client.get("/api/meta").json()
        assert "continuous_active" in meta
        stopped = client.post(
            "/api/continuous/stop",
            json={"workspace": str(tmp_path)},
        )
        assert stopped.status_code == 200
        assert stopped.json()["ok"] is True
        assert manager.is_continuous_active(tmp_path) is False
    finally:
        manager.shutdown(wait=False)


def test_the_workspace_does_not_archive_or_remove_a_project(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    extra = tmp_path / "extra-app"
    extra.mkdir()
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        selected = client.post("/api/workspaces/select", json={"path": str(extra)})
        assert selected.status_code == 200
        archived = client.post("/api/workspaces/archive", json={"path": str(extra)})
        assert archived.status_code == 400
        assert "Organization" in archived.json()["error"]
        forgotten = client.post("/api/workspaces/forget", json={"path": str(extra)})
        assert forgotten.status_code == 400
        listed = client.get("/api/workspaces")
        paths = [item["path"] for item in listed.json()["workspaces"]]
        assert str(extra.resolve()) in paths
        assert str(tmp_path.resolve()) in paths
        assert extra.exists()
    finally:
        manager.shutdown(wait=False)


def test_web_ui_clones_away_from_the_container_home(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support import init_git_repo

    home = tmp_path / "home"
    store = tmp_path / "workspaces"
    store.mkdir()
    monkeypatch.setattr(
        "agent_lbw.runtime.workspaces.Path.home",
        classmethod(lambda cls: home),
    )
    source = tmp_path / "upstream"
    source.mkdir()
    (source / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(source)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=store)
    client = TestClient(app)
    try:
        listed = client.get("/api/workspaces")
        assert listed.status_code == 200
        assert listed.json()["checkout_root"] == str(store.resolve())
        cloned = client.post(
            "/api/workspaces/clone",
            json={"url": str(source), "parent": str(home / "projects"), "name": "checkout"},
        )
        assert cloned.status_code == 200
        dest = store / "projects" / "checkout"
        assert cloned.json()["current"] == str(dest.resolve())
        assert (dest / "readme.txt").read_text(encoding="utf-8") == "hello\n"
        assert not (home / "projects" / "checkout").exists()
    finally:
        manager.shutdown(wait=False)


def test_web_ui_clones_repository_into_local_workspace(settings: Settings, tmp_path: Path) -> None:
    from tests.support import init_git_repo

    source = tmp_path / "upstream"
    source.mkdir()
    (source / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(source)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        cloned = client.post(
            "/api/workspaces/clone",
            json={
                "url": str(source),
                "parent": str(tmp_path / "projects"),
                "name": "checkout",
            },
        )
        assert cloned.status_code == 200
        dest = tmp_path / "projects" / "checkout"
        assert cloned.json()["current"] == str(dest.resolve())
        assert (dest / "readme.txt").read_text(encoding="utf-8") == "hello\n"
        assert (dest / ".lbw" / "config.yaml").exists()
        again = client.post(
            "/api/workspaces/clone",
            json={"url": str(source), "parent": str(tmp_path / "projects"), "name": "checkout"},
        )
        assert again.status_code == 400
    finally:
        manager.shutdown(wait=False)


def test_web_ui_lists_enables_and_clones_workspace_skills(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    from tests.support import init_git_repo
    from tests.test_rules import MemoryKiwi

    memory = MemoryKiwi()
    monkeypatch.setattr("agent_lbw.rules_http.open_kiwi", lambda *_args, **_kwargs: memory)

    remote = tmp_path / "skill-origin"
    skill = remote / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: Review diffs.\n---\n\nRead the diff.\n",
        encoding="utf-8",
    )
    init_git_repo(remote)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/workspaces/skills", params={"path": str(tmp_path)})
        assert listed.status_code == 200
        names = [item["name"] for item in listed.json()["skills"]]
        assert "tdd" in names
        assert listed.json()["enabled"] == []
        tdd = next(item for item in listed.json()["skills"] if item["name"] == "tdd")
        assert tdd["summary"] == "Test-driven development."
        assert "body" not in tdd
        detail = client.get(
            "/api/workspaces/skills/detail",
            params={"path": str(tmp_path), "name": "tdd"},
        )
        assert detail.status_code == 200
        payload = detail.json()
        assert "red" in payload["body"].lower()
        assert "<h1>" in payload["html"]
        assert "<h2>" in payload["html"]
        assert "<li>" in payload["html"]
        enabled = client.put(
            "/api/workspaces/skills",
            json={"path": str(tmp_path), "enabled": ["tdd"]},
        )
        assert enabled.status_code == 200
        assert "tdd" in enabled.json()["enabled"]
        cloned = client.post(
            "/api/workspaces/skills/sources",
            json={"path": str(tmp_path), "url": str(remote)},
        )
        assert cloned.status_code == 200
        cloned_names = [item["name"] for item in cloned.json()["skills"]]
        assert "code-review" in cloned_names
        added = cloned.json()["added"]
        assert added["sha"]
        assert added["space"].startswith("sk-org-")
        assert tmp_path.name.casefold() not in added["space"]
        assert "code-review" not in added["space"]
        assert memory.files
        pulled = client.post(
            "/api/workspaces/skills/sources/sync",
            json={
                "path": str(tmp_path),
                "slug": added["id"],
            },
        )
        assert pulled.status_code == 200
        assert pulled.json()["synced"]["sha"] == added["sha"]
    finally:
        manager.shutdown(wait=False)


def test_web_ui_progress_includes_file_history_and_timestamps(
    settings: Settings, tmp_path: Path
) -> None:
    from agent_lbw.progress import record_event

    def runner(task: Task) -> CycleResult:
        record_event(
            kind="file",
            path="hello.py",
            action="created",
            diff="--- /dev/null\n+++ b/hello.py\n+print('hi')\n",
        )
        record_event(kind="llm", purpose="agent", ok=True, elapsed_ms=12)
        record_event(
            kind="test",
            command="python3 check.py",
            ok=False,
            phase="after",
            elapsed_ms=90,
            output="AssertionError: expected 5",
        )
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b".timeline" in home.content
        assert b'id="progress-timeline"' in home.content
        assert b"captureProgressScroll" in home.content
        assert b"restoreProgressScroll" in home.content
        assert b"data-history-key" in home.content
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Show diffs", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            match = next(
                (item for item in client.get("/api/tasks").json() if item["id"] == task_id),
                None,
            )
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["logs"]
        assert body["logs"][0][:4].isdigit()
        assert body["logs"][0].split(" ", 1)[0].endswith("Z")
        kinds = [event["kind"] for event in body["events"]]
        assert "file" in kinds
        assert "llm" in kinds
        assert "test" in kinds
        file_event = next(event for event in body["events"] if event["kind"] == "file")
        assert file_event["path"] == "hello.py"
        assert "print('hi')" in file_event["diff"]
        test_event = next(event for event in body["events"] if event["kind"] == "test")
        assert test_event["ok"] is False
        assert "AssertionError" in test_event["output"]
        assert b"Tests failed" in home.content
        assert b'event.kind === "test"' in home.content
        assert b"groupTimelineEvents" in home.content
        assert b"items.reverse()" in home.content
        assert b"Thinking" in home.content
        assert b"timeline-item think" in home.content
        assert b"function renderFileChanges(" in home.content
        assert b"file-change-tree" in home.content
        assert b"data-change-path" in home.content
        assert b'data-task-pane="changes"' in home.content
        assert b'data-task-pane="screenshots"' in home.content
        assert b"No file changes in this task." in home.content
    finally:
        manager.shutdown(wait=False)


def test_ui_screenshot_api_serves_pngs(settings: Settings, tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    shots = lbw / "ui-screenshots"
    shots.mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    (lbw / "ui-review.png").write_bytes(png)
    (shots / "ui-review_goal.png").write_bytes(png)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        legacy = client.get("/api/ui-screenshot", params={"name": "ui-review.png"})
        assert legacy.status_code == 200
        assert legacy.content[:8] == b"\x89PNG\r\n\x1a\n"
        unique = client.get("/api/ui-screenshot", params={"name": "ui-review_goal.png"})
        assert unique.status_code == 200
        missing = client.get("/api/ui-screenshot", params={"name": "missing.png"})
        assert missing.status_code == 404
        traversal = client.get("/api/ui-screenshot", params={"name": "../config.yaml"})
        assert traversal.status_code == 404
    finally:
        manager.shutdown(wait=False)


def _github_workspace(root: Path) -> None:
    from tests.support import init_git_repo

    from agent_lbw.sandbox import Workspace
    from agent_lbw.tools.git import run_git

    (root / "readme.txt").write_text("hello\n", encoding="utf-8")
    init_git_repo(root)
    run_git(Workspace(root), ["remote", "add", "origin", "git@github.com:acme/demo.git"])


def test_approve_on_github_requires_a_connected_user(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.runtime.account import update_account
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.github_sync import link_github_login
    from agent_lbw.tools.git import APPROVE_GITHUB

    def runner(task: Task) -> CycleResult:
        if task.decision == "approve":
            return CycleResult(
                status="success",
                goal=task.goal,
                summary="approved",
                tests_passed=True,
                committed=False,
                published=False,
                commit_sha=None,
                reason="approved",
                stage="done",
            )
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary="ready",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="waiting for approval",
            stage="approval",
        )

    _github_workspace(tmp_path)
    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship it", "approval_gate": "ask"},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        waiting = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "waiting":
                waiting = body
                break
            time.sleep(0.05)
        assert waiting is not None
        refused = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "approve"})
        assert refused.status_code == 409
        assert refused.json()["error"] == APPROVE_GITHUB
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "waiting"

        account = update_account(tmp_path, user_name="Ada Lovelace", user_email="ada@example.com")
        with database(tmp_path) as db:
            link_github_login(db, user_id=account["current_user_id"], github_login="ada")
        approved = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "approve"})
        assert approved.status_code == 200
        done = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "success":
                done = body
                break
            time.sleep(0.05)
        assert done is not None
        assert done["stage"] == "done"
    finally:
        manager.shutdown(wait=True)


def test_web_ui_hides_github_issues_for_local_folders(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result())
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b'id="github-goals-section" hidden' in home.content
        missing = client.get("/api/goals")
        assert missing.status_code == 400
        assert missing.json()["error"] == "workspace is not a GitHub repository"
        assert missing.json()["issues"] == []
    finally:
        manager.shutdown(wait=False)


def test_web_ui_loads_github_issues_from_workspace_remote(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_workspace(tmp_path)

    def fake_load(
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 100,
        token: str | None = None,
    ) -> dict:
        assert owner == "acme"
        assert repo == "demo"
        assert state == "open"
        return {
            "issues": [
                {
                    "id": 1,
                    "number": 12,
                    "title": "Add a favicon",
                    "body": "Put favicon.ico in static files.",
                    "url": "https://github.com/acme/demo/issues/12",
                    "state": "open",
                    "label": "#12 Add a favicon",
                    "goal": (
                        "#12 Add a favicon\n"
                        "https://github.com/acme/demo/issues/12\n\n"
                        "Implement this GitHub issue. Do the work it describes; "
                        "do not only summarize it.\n\n"
                        "Put favicon.ico in static files.\n"
                    ),
                }
            ],
            "owner": owner,
            "repo": repo,
            "state": state,
        }

    monkeypatch.setattr("agent_lbw.runtime.importer.load_goals_from_issues", fake_load)
    manager = TaskManager(settings, runner=lambda task: _ok_result())
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b'id="github-goals-section"' in home.content
        assert b'id="github-goals-section" hidden' not in home.content
        assert b'id="github-owner"' not in home.content
        loaded = client.get("/api/goals")
        assert loaded.status_code == 200
        body = loaded.json()
        assert body["owner"] == "acme"
        assert body["repo"] == "demo"
        assert body["issues"][0]["label"] == "#12 Add a favicon"
        assert body["issues"][0]["goal"].startswith("#12 Add a favicon")
        assert "Put favicon.ico in static files." in body["issues"][0]["goal"]
        assert b"issue.label" in home.content
        assert b"fillGoalFromIssue" in home.content
        filtered = client.get(
            "/api/goals",
            params={"workspace": str(tmp_path), "state": "open"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["issues"][0]["number"] == 12
    finally:
        manager.shutdown(wait=False)


def _failed_result(goal: str | None = "do the thing", branch: str | None = None) -> CycleResult:
    return CycleResult(
        status="failed",
        goal=goal,
        summary="not done",
        tests_passed=False,
        committed=False,
        published=False,
        commit_sha=None,
        reason="goal not met",
        branch=branch,
    )


def test_requesting_changes_reopens_the_same_job(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", lambda _url, token=None: "open")
    notes: list[str] = []

    def runner(task: Task) -> CycleResult:
        notes.append(task.review_notes)
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="done",
            tests_passed=True,
            committed=True,
            published=True,
            commit_sha="abc123",
            pr_url="https://github.com/acme/job-finder-agent/pull/2",
            branch="lbw/write-up-the-agents-md-file",
            reason="completed",
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Write the agents file",
                "auto_commit": False,
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["status"] == "success"
        empty = client.post(f"/api/tasks/{task_id}/rework", json={"note": "  "})
        assert empty.status_code == 400
        again = client.post(
            f"/api/tasks/{task_id}/rework",
            json={"note": "Tighten the lifecycle section"},
        )
        assert again.status_code == 200
        reopened = again.json()
        assert reopened["id"] == task_id
        assert reopened["goal"] == "Write the agents file"
        assert reopened["status"] in {"queued", "running"}
        assert "Tighten the lifecycle section" in reopened["review_notes"]
        deadline = time.time() + 3
        while time.time() < deadline and len(notes) < 2:
            time.sleep(0.05)
        assert len(notes) >= 2
        assert "Tighten the lifecycle section" in notes[1]
        listed = client.get("/api/tasks").json()
        pulls = [item for item in listed if "pull/2" in str(item.get("pr_url") or "")]
        assert len(pulls) == 1
        assert pulls[0]["id"] == task_id
    finally:
        manager.shutdown(wait=False)


def test_web_ui_reruns_failed_task(settings: Settings, tmp_path: Path) -> None:
    statuses = iter(["failed", "success"])

    def runner(task: Task) -> CycleResult:
        status = next(statuses)
        if status == "failed":
            return _failed_result(task.goal, branch="lbw/feature")
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Allow rerun", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["status"] == "failed"
        rerun = client.post(f"/api/tasks/{task_id}/rerun", json={})
        assert rerun.status_code == 201
        assert rerun.json()["id"] != task_id
        assert rerun.json()["goal"] == "Allow rerun"
        success = client.post(f"/api/tasks/{rerun.json()['id']}/rerun", json={})
        assert success.status_code == 400
    finally:
        manager.shutdown(wait=False)


def test_web_ui_updates_successful_task_on_its_branch(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", lambda _url, token=None: "open")
    seen: list[Task] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task)
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="done",
            tests_passed=True,
            committed=True,
            published=True,
            commit_sha="abc123",
            reason="completed",
            pr_url="https://example.test/pull/9",
            branch="lbw/feature",
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship the panel", "auto_commit": False},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        assert body["status"] == "success"
        assert body["branch"] == "lbw/feature"
        updated = client.post(
            f"/api/tasks/{task_id}/rework",
            json={"note": "Also leave room for the PR link"},
        )
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["id"] == task_id
        assert payload["goal"] == "Ship the panel"
        assert "PR link" in payload["review_notes"]
        assert payload["create_pr"] is True
        follow = None
        for _ in range(50):
            if len(seen) >= 2:
                follow = seen[1]
                break
            time.sleep(0.05)
        assert follow is not None
        assert follow.id == task_id
        assert follow.resume_branch == "lbw/feature"
        assert follow.resume_sha == "abc123"
    finally:
        manager.shutdown(wait=False)


def test_web_ui_refuses_update_when_pull_request_is_not_open(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", lambda _url, token=None: "merged")

    def runner(task: Task) -> CycleResult:
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="done",
            tests_passed=True,
            committed=True,
            published=True,
            commit_sha="abc123",
            reason="completed",
            pr_url="https://example.test/pull/10",
            branch="lbw/feature",
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship the panel", "auto_commit": False},
        )
        task_id = created.json()["id"]
        body = None
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            match = next((item for item in listed if item["id"] == task_id), None)
            if match and match["status"] not in {"queued", "running"}:
                body = match
                break
            time.sleep(0.05)
        assert body is not None
        refused = client.post(
            f"/api/tasks/{task_id}/rework",
            json={"note": "More context"},
        )
        assert refused.status_code == 409
        queued = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "More context",
                "pr_url": "https://example.test/pull/10",
                "resume_branch": "lbw/feature",
            },
        )
        assert queued.status_code == 409
    finally:
        manager.shutdown(wait=False)


def test_web_ui_file_manager_displays_icons(settings: Settings, tmp_path: Path) -> None:
    """The changes tree ships SVG file-type icons, including a document for unknown types."""

    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert b'class="file-icon"' in home.content
        assert b'class="file-type-icon"' in home.content
        assert b"function getFileIconForExtension" in home.content
        assert b"#f7df1e" in home.content
        assert b"#3178c6" in home.content
        assert b"#3776ab" in home.content
        assert b"#e34c26" in home.content
        assert b'stroke="currentColor"' in home.content
        assert b"-terminal" not in home.content
        assert b"Haskell icon" not in home.content
    finally:
        manager.shutdown(wait=False)


def _clear_pr_state_cache() -> None:
    with _PR_STATE_LOCK:
        _PR_STATE_CACHE.clear()


def test_web_ui_reports_pull_request_states(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_state(url: str, token: str | None = None) -> str:
        if url.endswith("/9"):
            return "open"
        if url.endswith("/10"):
            return "merged"
        return ""

    _clear_pr_state_cache()
    monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", fake_state)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.post(
            "/api/pulls/status",
            json={
                "urls": [
                    "https://example.test/pull/9",
                    "https://example.test/pull/10",
                    "not a pull request",
                ]
            },
        )
        assert listed.status_code == 200
        states = listed.json()["states"]
        assert states["https://example.test/pull/9"] == "open"
        assert states["https://example.test/pull/10"] == "merged"
        assert "not a pull request" not in states
        many = [f"https://example.test/pull/{index}" for index in range(30)]
        listed_many = client.post("/api/pulls/status", json={"urls": many})
        assert listed_many.status_code == 200
        assert len(listed_many.json()["states"]) == 30
    finally:
        _clear_pr_state_cache()
        manager.shutdown(wait=False)


def test_pull_status_caches_closed_and_merged_until_forced(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_state(url: str, token: str | None = None) -> str:
        calls.append(url)
        if url.endswith("/1"):
            return "merged"
        if url.endswith("/2"):
            return "closed"
        if url.endswith("/3"):
            return "open"
        return ""

    _clear_pr_state_cache()
    monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", fake_state)
    monkeypatch.setattr("agent_lbw.web_ui._PR_STATE_TTL", 0)
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    urls = [
        "https://example.test/pull/1",
        "https://example.test/pull/2",
        "https://example.test/pull/3",
    ]
    try:
        first = client.post("/api/pulls/status", json={"urls": urls})
        assert first.status_code == 200
        assert first.json()["states"] == {
            urls[0]: "merged",
            urls[1]: "closed",
            urls[2]: "open",
        }
        saved = json.loads((tmp_path / ".lbw" / "pr-states.json").read_text(encoding="utf-8"))
        assert saved == {urls[0]: "merged", urls[1]: "closed"}

        calls.clear()
        second = client.post("/api/pulls/status", json={"urls": urls})
        assert second.status_code == 200
        assert second.json()["states"][urls[0]] == "merged"
        assert second.json()["states"][urls[1]] == "closed"
        assert second.json()["states"][urls[2]] == "open"
        assert calls == [urls[2]]

        _clear_pr_state_cache()
        calls.clear()
        reloaded = client.post("/api/pulls/status", json={"urls": urls})
        assert reloaded.json()["states"][urls[0]] == "merged"
        assert reloaded.json()["states"][urls[1]] == "closed"
        assert calls == [urls[2]]

        calls.clear()
        forced = client.post("/api/pulls/status", json={"urls": urls, "force": True})
        assert forced.status_code == 200
        assert set(calls) == set(urls)
        assert forced.json()["states"][urls[0]] == "merged"

        def unknown(_url: str, token: str | None = None) -> str:
            calls.append(_url)
            return ""

        calls.clear()
        monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", unknown)
        missed = client.post("/api/pulls/status", json={"urls": urls, "force": True})
        assert set(calls) == set(urls)
        assert missed.json()["states"][urls[0]] == "merged"
        assert missed.json()["states"][urls[1]] == "closed"
        kept = json.loads((tmp_path / ".lbw" / "pr-states.json").read_text(encoding="utf-8"))
        assert kept == {urls[0]: "merged", urls[1]: "closed"}

        def reopened(url: str, token: str | None = None) -> str:
            calls.append(url)
            if url.endswith("/2"):
                return "open"
            if url.endswith("/1"):
                return "merged"
            return ""

        calls.clear()
        monkeypatch.setattr("agent_lbw.web_ui.pull_request_state", reopened)
        opened = client.post("/api/pulls/status", json={"urls": urls, "force": True})
        assert opened.json()["states"][urls[1]] == "open"
        kept = json.loads((tmp_path / ".lbw" / "pr-states.json").read_text(encoding="utf-8"))
        assert kept == {urls[0]: "merged"}
    finally:
        _clear_pr_state_cache()
        manager.shutdown(wait=False)


def test_merge_an_open_pull_request(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.tools.base import ToolResult

    (tmp_path / ".git").mkdir()
    seen: dict[str, str] = {}

    def fake_merge(workspace, url, squash=True, token=None):
        seen["url"] = url
        return ToolResult(True, "merged")

    monkeypatch.setattr("agent_lbw.web_ui.merge_pull_request", fake_merge)
    monkeypatch.setattr(
        "agent_lbw.web_ui.lookup_pull_state",
        lambda url, force=False, token=None: "open",
    )
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        missing = client.post("/api/pulls/merge", json={"url": "not a pull request"})
        assert missing.status_code == 400
        merged = client.post(
            "/api/pulls/merge",
            json={"url": "https://github.com/acme/demo/pull/4", "workspace": str(tmp_path)},
        )
        assert merged.status_code == 200
        assert merged.json()["state"] == "merged"
        assert seen["url"] == "https://github.com/acme/demo/pull/4"
        monkeypatch.setattr(
            "agent_lbw.web_ui.lookup_pull_state",
            lambda url, force=False, token=None: "merged",
        )
        again = client.post(
            "/api/pulls/merge",
            json={"url": "https://github.com/acme/demo/pull/4", "workspace": str(tmp_path)},
        )
        assert again.status_code == 409
        assert again.json()["error"] == "pull request is not open"
    finally:
        _clear_pr_state_cache()
        manager.shutdown(wait=False)


def test_submit_accepts_resume_branch(settings: Settings, tmp_path: Path) -> None:
    seen: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task.resume_branch)
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Continue the work",
                "resume_branch": "lbw/feature",
                "auto_commit": False,
            },
        )
        assert created.status_code == 201
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            if listed and listed[0]["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert seen == ["lbw/feature"]
    finally:
        manager.shutdown(wait=False)


def test_branches_api_lists_head_and_accepts_a_selected_base(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    init_git_repo(tmp_path)
    seen: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task.base_branch)
        return _ok_result(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/branches", params={"workspace": str(tmp_path)})
        assert listed.status_code == 200
        body = listed.json()
        assert body["default"]
        assert body["default"] in body["branches"]
        rejected = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "nope", "base_branch": "not a branch"},
        )
        assert rejected.status_code == 400
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Use the other branch",
                "base_branch": "feature/login",
                "auto_commit": False,
            },
        )
        assert created.status_code == 201
        assert created.json()["base_branch"] == "feature/login"
        for _ in range(50):
            tasks = client.get("/api/tasks").json()
            if tasks and tasks[0]["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert seen == ["feature/login"]
    finally:
        manager.shutdown(wait=False)


def test_home_lists_roles_and_the_change_gates(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok_result(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        home = client.get("/")
        assert b'data-rail="roles"' in home.content
        assert b'id="roles-view"' in home.content
        assert b'data-section="roles"' in home.content
        assert b'id="workspace-roles"' in home.content
        assert b'id="open-roles"' not in home.content
        assert b'id="roles-panel"' not in home.content
        assert b"function changeBoardHtml(" in home.content
        assert b'class="step-rail"' in home.content
        assert b'data-step-pick="' in home.content
        assert b'role="tabpanel"' in home.content
        assert b"function renderInbox(" in home.content
        assert b'id="inbox"' in home.content
        assert b'data-rail="proposals"' in home.content
        assert b'data-rail="changes"' in home.content
        assert b'data-rail="releases"' in home.content
        assert b'data-rail="rules"' in home.content
        assert b'data-rail="skills"' in home.content
        assert b'id="release-list"' in home.content
        assert b"function releaseBucket(" in home.content
        assert b"for (const entry of oneJobPerPull(workspaceRuns()))" in home.content
        assert b"No open pull requests." in home.content
        assert b"No merged pull requests." in home.content
        assert b"No closed pull requests." in home.content
        assert b"release-group release-${bucket}" in home.content
        assert b"Ask for a change" in home.content
        assert b'id="theme-toggle"' in home.content
        assert b"function applyTheme(" in home.content
        assert b"function restoreTheme(" in home.content
        assert b"function toggleTheme(" in home.content
        assert b"Waiting for you" in home.content
        assert b"In progress" in home.content
        assert b"function jobSection(" in home.content
        assert b"function inboxFolded(" in home.content
        assert b"No projects yet" in home.content
        assert b"Add the agent integration" in home.content
        assert b'id="workspace-summary-repo"' in home.content
        assert b'id="workspace-summary-branch"' in home.content
        assert b"function repoLabel(" in home.content
        assert b"function paintWorkspaceSummary(" in home.content
        assert b"function paintViewedBranch(" in home.content
        assert b"data-inbox-fold=" in home.content
        assert b"Pull requests" in home.content
        assert b"No pull request is waiting." in home.content
        assert b"Can't retry" in home.content
        assert b"No failed job is stuck." in home.content
        assert b"data-merge-url=" in home.content
        assert b"data-request-changes=" in home.content
        assert b"/api/pulls/merge" in home.content
        assert b"Request changes" in home.content
        assert b'id="gate-triage" type="checkbox" checked' in home.content
        assert b'id="gate-approval" type="checkbox" checked' in home.content
        assert b'id="bypass-gates"' in home.content
        assert b'data-change-decision="build"' in home.content
        assert b'data-change-decision="approve"' in home.content
        assert b'data-reply-continue="1"' in home.content
        assert b"function continueChange(" in home.content
        assert b"function sentBackText(" in home.content
        assert b"function stepMetrics(" in home.content
        assert b" tok" in home.content
        assert b'class="step-rail"' in home.content
        assert b'${usage}<div class="step-body">' in home.content
        assert b'id="open-profile"' in home.content
        assert b'id="org-name"' in home.content
        assert b"Why it was sent back" in home.content
        layout = client.get("/static/layout.css")
        assert b".inbox" in layout.content
        assert b".inbox-actions" in layout.content
        assert b"button.inbox-fold" in layout.content
        assert b"button.skill-repo-fold" in layout.content
        assert b".inbox-pull" in layout.content
        assert b".inbox-stuck" in layout.content
        assert b".step-card" in layout.content
        assert b".reply-box" in layout.content
        assert b".step-card > summary .step-usage" in layout.content
        assert b".account-menu" in layout.content
        assert b".release-open" in layout.content
        assert b".release-merged" in layout.content
        assert b".release-closed" in layout.content
        catalog = client.get("/api/workspaces/roles", params={"path": str(tmp_path)})
        assert catalog.status_code == 200
        names = [role["name"] for role in catalog.json()["roles"]]
        assert "architect" in names
        assert "security" in names
        assert "security" not in catalog.json()["enabled"]
        saved = client.put(
            "/api/workspaces/roles",
            json={"path": str(tmp_path), "enabled": ["architect", "security"]},
        )
        assert saved.status_code == 200
        assert "security" in saved.json()["enabled"]
        assert "builder" in saved.json()["enabled"]
        assert "qa" in saved.json()["enabled"]
    finally:
        manager.shutdown(wait=False)


def test_inbox_lists_waiting_reviews_across_projects(settings: Settings, tmp_path: Path) -> None:
    one = tmp_path / "alpha"
    two = tmp_path / "beta"
    one.mkdir()
    two.mkdir()

    def runner(task: Task) -> CycleResult:
        stage = "approval" if "pull" in (task.goal or "") else "triage"
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary=task.goal,
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason=f"waiting for {stage}",
            stage=stage,
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=one)
    client = TestClient(app)
    try:
        skipped = client.post(
            "/api/tasks",
            json={
                "workspace": str(one),
                "goal": "Say hello",
                "triage_gate": "auto",
                "approval_gate": "ask",
            },
        )
        assert skipped.status_code == 201
        assert skipped.json()["triage_gate"] == "auto"
        assert skipped.json()["approval_gate"] == "ask"
        assert skipped.json()["gate"] == "ask"
        both = client.post(
            "/api/tasks",
            json={"workspace": str(one), "goal": "Both reviews", "gate": "ask"},
        )
        assert both.status_code == 201
        assert both.json()["triage_gate"] == "ask"
        assert both.json()["approval_gate"] == "ask"
        bad = client.post(
            "/api/tasks",
            json={"workspace": str(one), "goal": "Nope", "triage_gate": "skip"},
        )
        assert bad.status_code == 400
        other = client.post(
            "/api/tasks",
            json={
                "workspace": str(two),
                "goal": "Review the pull request",
                "triage_gate": "ask",
                "approval_gate": "auto",
            },
        )
        assert other.status_code == 201
        wanted = {skipped.json()["id"], other.json()["id"]}
        listed: list[dict] = []
        for _ in range(50):
            listed = client.get("/api/tasks").json()
            ready = [
                item for item in listed if item["id"] in wanted and item["status"] == "waiting"
            ]
            if len(ready) == 2:
                break
            time.sleep(0.02)
        waiting = {
            item["workspace"]: item["stage"]
            for item in listed
            if item["status"] == "waiting" and item["id"] in wanted
        }
        assert waiting == {str(one.resolve()): "triage", str(two.resolve()): "approval"}
    finally:
        manager.shutdown(wait=False)


def test_waiting_change_accepts_build_and_rejects_a_late_decision(
    settings: Settings, tmp_path: Path
) -> None:
    def runner(task: Task) -> CycleResult:
        if task.decision == "build":
            return CycleResult(
                status="success",
                goal=task.goal,
                summary="built",
                tests_passed=True,
                committed=False,
                published=False,
                commit_sha=None,
                reason="completed",
                stage="done",
            )
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary="Implement: hello",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="waiting for triage",
            stage="triage",
            plan="Implement: hello",
            criteria=["hello works"],
            evidence=[
                {
                    "role": "architect",
                    "title": "Architect",
                    "job": "plan",
                    "verdict": "pass",
                    "summary": "planned",
                }
            ],
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Say hello", "gate": "ask"},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        waiting = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "waiting":
                waiting = body
                break
            time.sleep(0.05)
        assert waiting is not None
        assert waiting["stage"] == "triage"
        assert waiting["plan"] == "Implement: hello"
        assert waiting["evidence"][0]["role"] == "architect"
        early = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "approve"})
        assert early.status_code == 409
        built = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert built.status_code == 200
        done = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "success":
                done = body
                break
            time.sleep(0.05)
        assert done is not None
        assert done["stage"] == "done"
        late = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert late.status_code == 409
    finally:
        manager.shutdown(wait=False)


def test_sent_back_change_continues_from_a_note(settings: Settings, tmp_path: Path) -> None:
    seen: list[tuple[str | None, str]] = []

    def runner(task: Task) -> CycleResult:
        seen.append((task.decision, task.review_notes))
        if task.decision == "continue":
            return CycleResult(
                status="success",
                goal=task.goal,
                summary="continued",
                tests_passed=True,
                committed=False,
                published=False,
                commit_sha=None,
                reason="completed",
                stage="done",
            )
        return CycleResult(
            status="failed",
            goal=task.goal,
            summary="QA sent it back",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="goal not met; PR skipped: missing a rollback test",
            stage="review",
            evidence=[
                {
                    "role": "qa",
                    "title": "QA engineer",
                    "job": "review",
                    "verdict": "send_back",
                    "summary": "missing a rollback test",
                }
            ],
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Roll back stuck transactions"},
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        failed = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "failed":
                failed = body
                break
            time.sleep(0.05)
        assert failed is not None
        empty = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "continue"})
        assert empty.status_code == 400
        continued = client.post(
            f"/api/tasks/{task_id}/decide",
            json={"decision": "continue", "note": "Cover the session.rollback path."},
        )
        assert continued.status_code == 200
        done = None
        for _ in range(50):
            body = client.get(f"/api/tasks/{task_id}").json()
            if body["status"] == "success":
                done = body
                break
            time.sleep(0.05)
        assert done is not None
        assert seen[-1] == (
            "continue",
            "QA engineer: missing a rollback test\n\nCover the session.rollback path.",
        )
    finally:
        manager.shutdown(wait=False)
