from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.store.projects import ensure_project
from agent_lbw.runtime.store.recording import record_task, sync_task
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.web_ui import create_app


def _ok(task: Task) -> CycleResult:
    return CycleResult(
        status="success",
        goal=task.goal,
        summary="done",
        tests_passed=True,
        committed=False,
        published=False,
        commit_sha=None,
        reason="completed",
    )


def test_a_project_without_a_checkout_stays_selectable(settings: Settings, tmp_path: Path) -> None:
    account = load_account(tmp_path)
    gone = tmp_path / "removed" / "demo"
    with database(tmp_path) as db:
        ensure_project(
            db,
            account["org"]["id"],
            name="demo",
            kind="folder",
            locator=str(gone),
        )
    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/workspaces")
        assert listed.status_code == 200
        missing = next(item for item in listed.json()["workspaces"] if item["name"] == "demo")
        assert missing["missing"] is True
        assert missing["path"] == str(gone)

        selected = client.post("/api/workspaces/select", json={"path": str(gone)})
        assert selected.status_code == 200
        assert selected.json()["current"] == str(gone)
        assert any(item["name"] == "demo" for item in selected.json()["workspaces"])

        refused = client.post("/api/tasks", json={"workspace": str(gone), "goal": "try again"})
        assert refused.status_code == 400
        assert "restore" in refused.json()["error"]
    finally:
        manager.shutdown(wait=False)


def test_a_missing_github_project_can_start_a_proposal(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = load_account(tmp_path)
    gone = tmp_path / "removed" / "demo"
    with database(tmp_path) as db:
        ensure_project(
            db,
            account["org"]["id"],
            name="demo",
            kind="github",
            locator=str(gone),
        )

    def one_repo(_db, _root):
        return [{"full_name": "acme/demo", "private": False}]

    def fake_clone(url, parent, *, name=None, git_config=None):
        dest = Path(parent) / (name or "demo")
        dest.mkdir(parents=True)
        (dest / ".git").mkdir()
        return {"path": str(dest)}

    monkeypatch.setattr("agent_lbw.runtime.store.github_sync.list_repos", one_repo)
    monkeypatch.setattr("agent_lbw.runtime.workspaces.clone_workspace", fake_clone)
    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post("/api/tasks", json={"workspace": str(gone), "goal": "ship it"})
        assert created.status_code == 201
        assert created.json()["workspace"] == str((tmp_path / "demo").resolve())
        assert created.json()["goal"] == "ship it"
    finally:
        manager.shutdown(wait=False)


def test_a_finished_job_keeps_its_plan_steps_and_diff(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    init_git_repo(tmp_path)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "note.txt").write_text("hello from the job\n", encoding="utf-8")
    subprocess.run(["git", "add", "note.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "add note"], cwd=tmp_path, check=True)
    task = Task(
        id="job-kept",
        workspace=str(tmp_path),
        goal="add a note",
        auto_commit=False,
        create_pr=False,
        model_name="scripted",
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="ollama",
        status="success",
        plan="write note.txt",
        summary="added the note",
        reason="completed",
        tests_passed=True,
        sha_before=sha,
        events=[
            {"kind": "step", "at": "2026-11-04T12:00:00Z", "message": "writing the note"},
            {
                "kind": "test",
                "ok": False,
                "phase": "tests",
                "command": "pytest",
                "output": "SECRET-TEST-OUTPUT",
            },
            {"kind": "llm", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]},
        ],
        logs=["SECRET-LOG-LINE"],
    )
    record_task(tmp_path, "", task)
    sync_task(tmp_path, "", task)

    stored = (tmp_path / ".lbw" / "lbw.db").read_bytes()
    assert b"SECRET-LOG-LINE" not in stored
    assert b"SECRET-TEST-OUTPUT" not in stored
    assert b"SECRET-PROMPT" not in stored
    assert b"hello from the job" not in stored
    assert b"write note.txt" not in stored

    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        history = client.get("/api/history")
        assert history.status_code == 200
        item = next(entry for entry in history.json()["items"] if entry["id"] == "job-kept")
        assert item["plan"] == "write note.txt"
        assert item["summary"] == "added the note"
        assert item["reason"] == "completed"
        assert item["tests_passed"] is True
        assert "logs" not in item
        steps = [event for event in item["events"] if event.get("kind") == "step"]
        assert steps[0]["message"] == "writing the note"
        tests = [event for event in item["events"] if event.get("kind") == "test"]
        assert tests[0]["ok"] is False
        assert "output" not in tests[0]
        files = [event for event in item["events"] if event.get("kind") == "file"]
        assert any(event["path"] == "note.txt" for event in files)
        assert any("hello from the job" in (event.get("net_diff") or "") for event in files)
    finally:
        manager.shutdown(wait=False)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def _job(root: Path, task_id: str, **fields) -> Task:
    values = {
        "id": task_id,
        "workspace": str(root),
        "goal": "add a note",
        "auto_commit": True,
        "create_pr": True,
        "model_name": "scripted",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "ollama",
        "status": "success",
        "summary": "added the note",
        "reason": "completed",
    }
    values.update(fields)
    return Task(**values)


def _files(item: dict) -> dict[str, str]:
    return {
        event["path"]: event.get("net_diff") or event.get("diff") or ""
        for event in item.get("events") or []
        if event.get("kind") == "file"
    }


def test_a_merged_job_shows_only_its_files_after_a_restart(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    init_git_repo(tmp_path)
    _git(tmp_path, "branch", "-M", "main")
    start = _git(tmp_path, "rev-parse", "HEAD")
    # The last job left the checkout on its own branch.
    _git(tmp_path, "checkout", "-q", "-b", "lbw/earlier")
    (tmp_path / "earlier.txt").write_text("earlier job\n", encoding="utf-8")
    _git(tmp_path, "add", "earlier.txt")
    _git(tmp_path, "commit", "-q", "-m", "earlier")
    stale = _git(tmp_path, "rev-parse", "HEAD")
    # This job starts from a clean main and commits on its own branch.
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "checkout", "-q", "-b", "lbw/note")
    (tmp_path / "note.txt").write_text("hello from the job\n", encoding="utf-8")
    _git(tmp_path, "add", "note.txt")
    _git(tmp_path, "commit", "-q", "-m", "add note")
    task = _job(
        tmp_path,
        "job-merged",
        committed=True,
        published=True,
        branch="lbw/note",
        pr_url="https://github.com/acme/repo/pull/7",
        sha_before=stale,
        commit_sha=_git(tmp_path, "rev-parse", "HEAD"),
        events=[{"kind": "step", "message": "Starting from main", "start_sha": start}],
    )
    record_task(tmp_path, "", task)
    sync_task(tmp_path, "", task)
    # Someone else lands work, then the pull request merges and the checkout returns to main.
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "other.txt").write_text("another change\n", encoding="utf-8")
    _git(tmp_path, "add", "other.txt")
    _git(tmp_path, "commit", "-q", "-m", "other")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "merge note", "lbw/note")
    task.merged = True
    sync_task(tmp_path, "", task)

    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        items = client.get("/api/history", params={"page_size": 100}).json()["items"]
        item = next(entry for entry in items if entry["id"] == "job-merged")
        files = _files(item)
        assert set(files) == {"note.txt"}
        assert "hello from the job" in files["note.txt"]
    finally:
        manager.shutdown(wait=False)


def test_a_waiting_job_shows_its_changes_after_a_restart(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    init_git_repo(tmp_path)
    start = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "note.txt").write_text("waiting for approval\n", encoding="utf-8")
    task = _job(
        tmp_path,
        "job-waiting",
        status="waiting",
        stage="approval",
        approval_gate="ask",
        sha_before=start,
        events=[{"kind": "step", "message": "Starting from main", "start_sha": start}],
    )
    record_task(tmp_path, "", task)
    sync_task(tmp_path, "", task)
    # The checkout is scratch. The stored job still has the change.
    (tmp_path / "note.txt").unlink()

    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/tasks").json()
        restored = next(item for item in listed if item["id"] == "job-waiting")
        assert restored["status"] == "waiting"
        files = _files(restored)
        assert "waiting for approval" in files["note.txt"]
    finally:
        manager.shutdown(wait=False)


def test_a_rerun_on_an_open_pull_request_keeps_the_whole_change(
    settings: Settings, tmp_path: Path
) -> None:
    from agent_lbw.runtime.store.recording import job_start

    manager = TaskManager(settings, runner=_ok)
    try:
        first = _job(
            tmp_path,
            "job-first",
            branch="lbw/note",
            pr_url="https://github.com/acme/repo/pull/7",
            commit_sha="b" * 40,
            events=[{"kind": "step", "message": "Starting from main", "start_sha": "a" * 40}],
        )
        with manager._lock:
            manager._tasks[first.id] = first
        again = manager.rerun(first.id, goal="also update the docs")
        assert again is not None
        assert again.change_base == "a" * 40
        assert job_start(again) == "a" * 40
    finally:
        manager.shutdown(wait=False)


def test_a_change_that_failed_publishing_restarts_at_publishing(
    settings: Settings, tmp_path: Path
) -> None:
    seen: list[tuple[str | None, str, str | None, str | None]] = []

    def runner(task: Task) -> CycleResult:
        seen.append((task.decision, task.stage, task.resume_sha, task.summary))
        return _ok(task)

    manager = TaskManager(settings, runner=runner)
    try:
        failed = _job(
            tmp_path,
            "job-publish",
            status="failed",
            decision="approve",
            stage="approval",
            branch="lbw/note",
            commit_sha="b" * 40,
            plan="Plan:\n1. Write the note",
            criteria=["the note exists"],
            summary="added the note",
            reason="refusing to commit likely secrets: .env.local",
            events=[{"kind": "step", "message": "Starting from main", "start_sha": "a" * 40}],
        )
        with manager._lock:
            manager._tasks[failed.id] = failed
        assert failed.to_dict()["restart_from"] == "publish"
        with pytest.raises(ValueError, match="cannot restart from build"):
            manager.rerun(failed.id, from_phase="build")
        again = manager.rerun(failed.id, from_phase="publish")
        assert again is not None
        assert (again.attempt_of, again.attempt) == (failed.id, 2)
        assert (again.plan, again.criteria) == (failed.plan, failed.criteria)
        assert again.change_base == "a" * 40
        assert again.resume_branch == "lbw/note"
        # The rerun keeps the change's name and notes the phase it starts at.
        assert again.goal == failed.goal
        assert (again.to_dict()["started_from"], again.to_dict()["instruction"]) == ("publish", "")
        for _ in range(50):
            if seen:
                break
            time.sleep(0.05)
        assert seen == [("approve", "approval", None, "added the note")]
    finally:
        manager.shutdown(wait=False)


def test_a_rerun_with_an_edited_goal_keeps_the_change_name(
    settings: Settings, tmp_path: Path
) -> None:
    asked: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        asked.append(task.run_goal())
        return _ok(task)

    manager = TaskManager(settings, runner=runner)
    try:
        failed = _job(tmp_path, "job-edit", status="failed", reason="tests failed")
        with manager._lock:
            manager._tasks[failed.id] = failed
        edited = manager.rerun(failed.id, goal="Write the note in French")
        assert edited is not None
        assert (edited.goal, edited.instruction) == (failed.goal, "Write the note in French")
        assert edited.started_from == ""
        for _ in range(100):
            if asked and edited.status not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert asked == ["Write the note in French"]
        # A plain rerun of that run keeps its request.
        with manager._lock:
            edited.status = "failed"
        again = manager.rerun(edited.id)
        assert again is not None
        assert (again.goal, again.instruction) == (failed.goal, "Write the note in French")
    finally:
        manager.shutdown(wait=False)


def test_a_running_job_stops_at_its_next_model_call(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.progress import raise_if_stopped

    calls: list[int] = []

    def runner(task: Task) -> CycleResult:
        # Stands in for the agent loop: each turn checks before its model call.
        for turn in range(200):
            raise_if_stopped()
            calls.append(turn)
            time.sleep(0.02)
        return _ok(task)

    manager = TaskManager(settings, runner=runner)
    try:
        task = manager.submit(
            tmp_path,
            "Tidy the README",
            auto_commit=False,
            create_pr=False,
            model_name="scripted",
            model_base_url="http://127.0.0.1:9/v1",
            model_api_key="",
        )
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        stopped = manager.stop(task.id, by="Ada")
        assert stopped is not None and stopped.stopped_by == "Ada"
        for _ in range(100):
            if task.status != "running":
                break
            time.sleep(0.02)
        assert (task.status, task.reason) == ("failed", "Stopped by Ada")
        assert len(calls) < 200
        assert task.to_dict()["events"][-1]["message"] == "Stopped by Ada"
        with pytest.raises(ValueError, match="only a queued or running job"):
            manager.stop(task.id, by="Ada")
        # A stopped job can run again.
        again = manager.rerun(task.id)
        assert again is not None and again.goal == task.goal
    finally:
        manager.shutdown(wait=False)


def test_a_queued_job_that_is_stopped_never_runs(settings: Settings, tmp_path: Path) -> None:
    ran: list[str] = []

    def runner(task: Task) -> CycleResult:
        ran.append(task.id)
        return _ok(task)

    manager = TaskManager(settings, runner=runner)
    try:
        queued = _job(tmp_path, "job-queued", status="queued")
        with manager._lock:
            manager._tasks[queued.id] = queued
        manager.stop(queued.id, by="Grace")
        assert (queued.status, queued.reason) == ("failed", "Stopped by Grace")
        manager._run(queued)
        assert ran == [] and queued.status == "failed"
    finally:
        manager.shutdown(wait=False)


def test_stopping_a_job_over_http_needs_a_queued_or_running_job(
    settings: Settings, tmp_path: Path
) -> None:
    manager = TaskManager(settings, runner=_ok)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        assert client.post("/api/tasks/nope/stop").status_code == 404
        done = _job(tmp_path, "job-done", status="success")
        queued = _job(tmp_path, "job-wait", status="queued")
        with manager._lock:
            manager._tasks[done.id] = done
            manager._tasks[queued.id] = queued
        assert client.post(f"/api/tasks/{done.id}/stop").status_code == 409
        stopped = client.post(f"/api/tasks/{queued.id}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "failed"
        assert stopped.json()["reason"].startswith("Stopped by")
    finally:
        manager.shutdown(wait=False)


def test_only_a_failed_run_offers_a_phase_to_restart_from(tmp_path: Path) -> None:
    runner_plan = {"plan": "Plan:\n1. Write the note", "agent_kind": "claude_code"}
    cases = [
        ({"status": "failed", "decision": "build", **runner_plan}, "build"),
        ({"status": "failed", "decision": "build", "plan": "Plan:\n1. Write it"}, ""),
        ({"status": "failed", "decision": "build", "agent_kind": "claude_code"}, ""),
        (
            {
                "status": "failed",
                "decision": "build",
                "agent_kind": "claude_code",
                "committed": True,
                "branch": "lbw/runner-1",
            },
            "publish",
        ),
        ({"status": "failed", "decision": "build", "committed": True, "branch": "lbw/x"}, ""),
        ({"status": "failed", "decision": "approve", "branch": "lbw/note"}, "publish"),
        ({"status": "success", "decision": "approve", "branch": "lbw/note"}, ""),
        ({"status": "failed"}, ""),
    ]
    for fields, phase in cases:
        assert _job(tmp_path, "job-case", **fields).to_dict()["restart_from"] == phase, fields


def _wait_for(client: TestClient, task_id: str, status: str) -> dict:
    for _ in range(50):
        response = client.get(f"/api/tasks/{task_id}")
        body = response.json()
        if response.status_code == 200 and body.get("status") == status:
            return body
        time.sleep(0.05)
    raise AssertionError(status)


def _parked_change(settings: Settings, tmp_path: Path) -> str:
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
        waiting = _wait_for(client, task_id, "waiting")
        assert waiting["stage"] == "triage"
        assert waiting["plan"] == "Implement: hello"
        return task_id
    finally:
        manager.shutdown(wait=True)


def test_build_starts_after_the_agent_restarts(settings: Settings, tmp_path: Path) -> None:
    task_id = _parked_change(settings, tmp_path)
    started: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        started.append(task.decision)
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

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        listed = client.get("/api/tasks").json()
        restored = next(item for item in listed if item["id"] == task_id)
        assert restored["status"] == "waiting"
        assert restored["stage"] == "triage"
        assert started == []
        with manager._lock:
            manager._tasks.pop(task_id)
        built = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert built.status_code == 200
        done = _wait_for(client, task_id, "success")
        assert done["stage"] == "done"
        assert started == ["build"]
    finally:
        manager.shutdown(wait=True)


def test_a_queued_plan_waits_for_build_after_restart(settings: Settings, tmp_path: Path) -> None:
    task_id = _parked_change(settings, tmp_path)
    with database(tmp_path) as db:
        db.execute("UPDATE tasks SET status = ? WHERE id = ?", ("queued", task_id))
    started: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        started.append(task.decision)
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

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        time.sleep(0.2)
        assert started == []
        listed = client.get("/api/tasks").json()
        restored = next(item for item in listed if item["id"] == task_id)
        assert restored["status"] == "waiting"
        assert restored["stage"] == "triage"
        history = client.get("/api/history").json()["items"]
        saved = next(item for item in history if item["id"] == task_id)
        assert saved["status"] == "waiting"
        built = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert built.status_code == 200
        assert _wait_for(client, task_id, "success")["stage"] == "done"
        assert started == ["build"]
    finally:
        manager.shutdown(wait=True)


def test_a_build_choice_resumes_after_restart(settings: Settings, tmp_path: Path) -> None:
    task_id = _parked_change(settings, tmp_path)
    parked = TaskManager(settings, runner=_ok)
    parked.store_root = tmp_path
    task = parked.adopt_job(task_id)
    assert task is not None
    task.decision = "build"
    task.status = "queued"
    sync_task(tmp_path, "", task)
    parked.shutdown(wait=True)

    started: list[str | None] = []

    def runner(task: Task) -> CycleResult:
        started.append(task.decision)
        return _ok(task)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        done = _wait_for(client, task_id, "success")
        assert done["status"] == "success"
        assert started == ["build"]
    finally:
        manager.shutdown(wait=True)


def test_a_queued_job_runs_again_after_restart(settings: Settings, tmp_path: Path) -> None:
    started: list[str] = []

    def runner(task: Task) -> CycleResult:
        started.append(task.id)
        return _ok(task)

    waiting = Task(
        id="queued-job",
        workspace=str(tmp_path),
        goal="Say hello",
        auto_commit=False,
        create_pr=False,
        model_name="scripted",
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="ollama",
    )
    record_task(tmp_path, "", waiting)
    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        assert _wait_for(client, "queued-job", "success")["summary"] == "done"
        assert started == ["queued-job"]
    finally:
        manager.shutdown(wait=True)
