from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.schedules import (
    ScheduleClock,
    agenda,
    claim_due,
    cron_expression,
    human_cadence,
    list_schedules,
    next_occurrence,
    upsert_schedule,
)
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.web_ui import create_app


def _local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    zone = datetime.now().astimezone().tzinfo
    return datetime(year, month, day, hour, minute, tzinfo=zone)


def _daily(hour: int = 9, minute: int = 0) -> dict:
    return {"kind": "daily", "hour": hour, "minute": minute, "weekdays": [0, 1, 2, 3, 4]}


def test_cadence_reads_as_a_sentence_and_a_cron_expression() -> None:
    hourly = {"kind": "hourly", "minute": 15, "hour": 0, "weekdays": []}
    assert human_cadence(hourly) == "Every hour at :15"
    assert cron_expression(hourly) == "15 * * * *"
    assert human_cadence({"kind": "hourly", "minute": 0, "hour": 0, "weekdays": []}) == "Every hour"

    daily = _daily(9, 0)
    assert human_cadence(daily) == "Every day at 9:00 AM"
    assert cron_expression(daily) == "0 9 * * *"

    weekdays = {"kind": "weekly", "hour": 8, "minute": 30, "weekdays": [0, 1, 2, 3, 4]}
    assert human_cadence(weekdays) == "Weekdays at 8:30 AM"
    assert cron_expression(weekdays) == "30 8 * * 1,2,3,4,5"

    monday = {"kind": "weekly", "hour": 12, "minute": 0, "weekdays": [0]}
    assert human_cadence(monday) == "Every Monday at 12:00 PM"
    assert cron_expression(monday) == "0 12 * * 1"

    sunday = {"kind": "weekly", "hour": 18, "minute": 5, "weekdays": [6]}
    assert human_cadence(sunday) == "Every Sunday at 6:05 PM"
    assert cron_expression(sunday) == "5 18 * * 0"


def test_next_occurrence_is_strictly_after_the_given_time() -> None:
    after = _local(2026, 10, 23, 9, 30)
    hourly = next_occurrence(
        {"kind": "hourly", "minute": 15, "hour": 0, "weekdays": []},
        after,
    )
    assert hourly == after.replace(hour=10, minute=15, second=0, microsecond=0)

    daily = next_occurrence(_daily(9, 0), after)
    assert daily == after.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)

    same_minute = next_occurrence(_daily(9, 30), after)
    assert same_minute == after.replace(second=0, microsecond=0) + timedelta(days=1)

    weekly = next_occurrence(
        {"kind": "weekly", "hour": 8, "minute": 0, "weekdays": [0]},
        after,
    )
    assert weekly.weekday() == 0
    assert weekly.hour == 8
    assert weekly.minute == 0
    assert 1 <= (weekly.date() - after.date()).days <= 7


def test_claim_due_queues_one_run_and_does_not_overlap(tmp_path: Path) -> None:
    saved = upsert_schedule(
        tmp_path,
        {
            "name": "Job search",
            "goal": "Look up new jobs",
            "workspace": str(tmp_path),
            "enabled": True,
            "cadence": _daily(8, 0),
        },
    )
    paused = upsert_schedule(
        tmp_path,
        {
            "name": "Paused",
            "goal": "Stay quiet",
            "workspace": str(tmp_path),
            "enabled": False,
            "cadence": _daily(8, 0),
        },
    )
    later = datetime.now().astimezone() + timedelta(days=3)
    launched: list[str] = []

    def remember(schedule: dict) -> None:
        launched.append(schedule["id"])

    clock = ScheduleClock(tmp_path, remember, lambda _task: None)

    assert clock.tick(later) == 1
    assert launched == [saved["id"]]
    assert clock.tick(later) == 0
    assert claim_due(tmp_path, later) == []
    stored = {item["id"]: item for item in list_schedules(tmp_path)}
    assert stored[paused["id"]]["runs"] == []
    assert stored[saved["id"]]["runs"][0]["status"] == "queued"
    assert datetime.fromisoformat(stored[saved["id"]]["next_run_at"]) > later


def _ok(goal: str | None = None) -> CycleResult:
    return CycleResult(
        status="success",
        goal=goal,
        summary="Found three roles.",
        tests_passed=True,
        committed=False,
        published=False,
        commit_sha=None,
        reason="completed",
    )


def test_schedule_api_saves_updates_runs_and_lists_results(
    settings: Settings, tmp_path: Path
) -> None:
    manager = TaskManager(settings, runner=lambda task: _ok(task.goal))
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        page = client.get("/")
        assert page.status_code == 200
        assert b'id="app-switch"' not in page.content
        assert b'data-rail="schedules"' in page.content
        assert b'id="schedules-app"' in page.content
        assert b'id="schedule-base"' in page.content
        assert b'id="schedule-output"' not in page.content
        assert b'id="target-project"' not in page.content
        assert b'id="schedule-commit"' not in page.content
        assert b"/api/schedules" in page.content

        missing = client.post(
            "/api/schedules",
            json={
                "name": "",
                "goal": "Look up jobs",
                "workspace": str(tmp_path),
                "cadence": _daily(),
            },
        )
        assert missing.status_code == 400

        created = client.post(
            "/api/schedules",
            json={
                "name": "Job search",
                "goal": "Look up new jobs and write a short report.",
                "workspace": str(tmp_path),
                "enabled": True,
                "auto_commit": False,
                "create_pr": False,
                "cadence": {
                    "kind": "weekly",
                    "hour": 8,
                    "minute": 0,
                    "weekdays": [0, 1, 2, 3, 4],
                },
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["label"] == "Weekdays at 8:00 AM"
        assert body["cron"] == "0 8 * * 1,2,3,4,5"
        assert body["runs"] == []

        updated = client.patch(
            f"/api/schedules/{body['id']}",
            json={
                "name": "Job search",
                "goal": "Look up new jobs and write a short report.",
                "workspace": str(tmp_path),
                "enabled": True,
                "cadence": _daily(7, 15),
            },
        )
        assert updated.status_code == 200
        assert updated.json()["label"] == "Every day at 7:15 AM"

        ran = client.post(f"/api/schedules/{body['id']}/run")
        assert ran.status_code == 200
        assert ran.json()["runs"][0]["status"] in {"queued", "running", "success"}

        deadline = time.time() + 3
        latest = ran.json()["runs"][0]
        while time.time() < deadline and latest["status"] != "success":
            time.sleep(0.05)
            listed = client.get("/api/schedules")
            assert listed.status_code == 200
            latest = listed.json()["schedules"][0]["runs"][0]
        assert latest["status"] == "success"
        assert latest["summary"] == "Found three roles."

        again = client.post(f"/api/schedules/{body['id']}/run")
        assert again.status_code == 200

        removed = client.delete(f"/api/schedules/{body['id']}")
        assert removed.status_code == 200
        assert client.get("/api/schedules").json()["schedules"] == []
        assert client.delete(f"/api/schedules/{body['id']}").status_code == 404
        home = page.content
        assert b'id="schedule-base"' in home
        assert b"schedule-target" not in home
    finally:
        manager.shutdown(wait=False)


def test_file_schedule_saves_documents_outside_a_project(
    settings: Settings, tmp_path: Path
) -> None:
    seen: list[Task] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task)
        (Path(task.workspace) / "jobs.csv").write_text("role,company\n", encoding="utf-8")
        return _ok(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        missing = client.post(
            "/api/schedules",
            json={
                "name": "Repo bot",
                "goal": "Fix the build",
                "kind": "project",
                "workspace": str(tmp_path / "missing"),
                "cadence": _daily(),
            },
        )
        assert missing.status_code == 400

        created = client.post(
            "/api/schedules",
            json={
                "name": "Job search",
                "goal": "Search for new jobs and save them as jobs.csv.",
                "kind": "files",
                "workspace": "",
                "auto_commit": True,
                "create_pr": True,
                "cadence": _daily(8, 0),
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["kind"] == "files"
        assert body["auto_commit"] is False
        assert body["create_pr"] is False
        assert body["files"] == []
        folder = Path(body["workspace"])
        assert folder.is_dir()
        assert folder.name == body["id"]

        reports = tmp_path / "reports"
        moved = client.patch(
            f"/api/schedules/{body['id']}",
            json={
                "name": "Job search",
                "goal": "Search for new jobs and save them as jobs.csv.",
                "kind": "files",
                "workspace": str(reports),
                "cadence": _daily(8, 0),
            },
        )
        assert moved.status_code == 200
        assert Path(moved.json()["workspace"]) == reports.resolve()

        ran = client.post(f"/api/schedules/{body['id']}/run")
        assert ran.status_code == 200
        deadline = time.time() + 3
        latest: dict = {}
        while time.time() < deadline:
            latest = client.get("/api/schedules").json()["schedules"][0]
            if latest["runs"] and latest["runs"][0]["status"] == "success" and latest["files"]:
                break
            time.sleep(0.05)
        assert latest["files"][0]["name"] == "jobs.csv"
        assert seen[0].auto_commit is False
        assert seen[0].create_pr is False
        assert "not a software project" in (seen[0].goal or "")
        assert "jobs.csv" in (seen[0].goal or "")
        downloaded = client.get(
            f"/api/schedules/{body['id']}/file",
            params={"path": "jobs.csv"},
        )
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"role,company")
        escaped = client.get(
            f"/api/schedules/{body['id']}/file",
            params={"path": "../schedules.json"},
        )
        assert escaped.status_code == 404
    finally:
        manager.shutdown(wait=False)


def test_project_schedule_starts_from_the_base_branch_and_reports_the_outcome(
    settings: Settings, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    seen: list[Task] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task)
        if "Fix the security header." in (task.goal or ""):
            return CycleResult(
                status="success",
                goal=task.goal,
                summary="Fixed the header check.",
                tests_passed=True,
                committed=True,
                published=True,
                commit_sha="abc",
                reason="opened a pull request",
                pr_url="https://github.com/acme/app/pull/4",
            )
        if "Review this project" in (task.goal or ""):
            return CycleResult(
                status="success",
                goal=task.goal,
                summary="No work to do: the review found nothing.",
                tests_passed=True,
                committed=False,
                published=False,
                commit_sha=None,
                reason="no changes needed: the tree already meets the goal",
            )
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="Finished.",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="completed",
        )

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        rejected = client.post(
            "/api/schedules",
            json={
                "name": "Security review",
                "goal": "Review this project for security issues.",
                "workspace": str(tmp_path),
                "base_branch": "not a branch",
                "cadence": _daily(),
            },
        )
        assert rejected.status_code == 400

        created = client.post(
            "/api/schedules",
            json={
                "name": "Security review",
                "goal": "Review this project for security issues.",
                "workspace": str(tmp_path),
                "base_branch": "main",
                "cadence": _daily(6, 0),
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["kind"] == "project"
        assert body["base_branch"] == "main"
        assert body["create_pr"] is True
        assert body["auto_commit"] is True

        elsewhere = client.post(
            "/api/schedules",
            json={
                "name": "Other project",
                "goal": "Look here too.",
                "workspace": str(other),
                "base_branch": "develop",
                "cadence": _daily(),
            },
        )
        assert elsewhere.status_code == 200
        listed = client.get("/api/schedules", params={"workspace": str(tmp_path)})
        assert [item["name"] for item in listed.json()["schedules"]] == ["Security review"]

        ran = client.post(f"/api/schedules/{body['id']}/run")
        assert ran.status_code == 200
        deadline = time.time() + 3
        latest: dict = {}
        while time.time() < deadline:
            latest = client.get("/api/schedules", params={"workspace": str(tmp_path)}).json()
            run = (latest["schedules"][0].get("runs") or [{}])[0]
            if run.get("outcome") == "clear":
                break
            time.sleep(0.05)
        assert latest["schedules"][0]["runs"][0]["outcome"] == "clear"
        assert seen[0].base_branch == "main"
        assert seen[0].create_pr is True
        assert seen[0].auto_commit is True
        assert "Start from the main branch." in (seen[0].goal or "")

        fix = client.post(
            "/api/schedules",
            json={
                "name": "Header fix",
                "goal": "Fix the security header.",
                "workspace": str(tmp_path),
                "cadence": _daily(),
            },
        )
        assert fix.status_code == 200
        client.post(f"/api/schedules/{fix.json()['id']}/run")
        deadline = time.time() + 3
        opened: dict = {}
        while time.time() < deadline:
            rows = client.get("/api/schedules", params={"workspace": str(tmp_path)}).json()
            opened = next(item for item in rows["schedules"] if item["name"] == "Header fix")
            if opened["runs"] and opened["runs"][0].get("outcome") == "pull_request":
                break
            time.sleep(0.05)
        assert opened["runs"][0]["pr_url"] == "https://github.com/acme/app/pull/4"
        assert opened["runs"][0]["outcome"] == "pull_request"
        assert seen[-1].base_branch == "main"
    finally:
        manager.shutdown(wait=False)


def test_agenda_shows_the_next_run_and_the_coming_week() -> None:
    now = _local(2026, 10, 23, 21, 0)
    items = [
        {
            "id": "job",
            "name": "Job search",
            "enabled": True,
            "cadence": {"kind": "daily", "hour": 9, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
        },
        {
            "id": "pulse",
            "name": "Pulse",
            "enabled": True,
            "runs": [{"status": "running"}],
            "cadence": {"kind": "hourly", "hour": 0, "minute": 15, "weekdays": []},
        },
        {
            "id": "off",
            "name": "Paused",
            "enabled": False,
            "cadence": {"kind": "daily", "hour": 8, "minute": 0, "weekdays": []},
        },
    ]
    board = agenda(items, now)
    assert [slot["name"] for slot in board["next_up"]] == ["Pulse", "Job search"]
    assert board["next_up"][0]["running"] is True
    assert board["next_up"][0]["at"].startswith("2026-10-23T21:15")
    assert len(board["days"]) == 7
    assert board["days"][0]["today"] is True
    saturday = board["days"][1]
    assert saturday["date"] == "2026-10-24"
    assert any(slot["name"] == "Job search" for slot in saturday["slots"])
    assert sum(1 for slot in saturday["slots"] if slot["name"] == "Pulse") == 1
    assert all(slot.get("hourly") for slot in saturday["slots"] if slot["name"] == "Pulse")


def test_open_schedule_run_includes_live_progress(settings: Settings, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(task: Task) -> CycleResult:
        task.logs.append("looking up jobs")
        task.events.append(
            {"kind": "step", "message": "Searching the web", "at": "2026-10-24T04:00:00Z"}
        )
        started.set()
        release.wait(3)
        return _ok(task.goal)

    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/schedules",
            json={
                "name": "Job search",
                "goal": "Search for new jobs and save them as jobs.csv.",
                "kind": "files",
                "workspace": "",
                "cadence": _daily(),
            },
        )
        assert created.status_code == 200
        ran = client.post(f"/api/schedules/{created.json()['id']}/run")
        assert ran.status_code == 200
        assert started.wait(3)
        body = client.get("/api/schedules").json()
        run = body["schedules"][0]["runs"][0]
        assert run["status"] == "running"
        assert "looking up jobs" in run["logs"]
        assert run["events"][-1]["message"] == "Searching the web"
        assert body["agenda"]["next_up"][0]["name"] == "Job search"
        page = client.get("/")
        assert b'id="schedule-view-agenda"' in page.content
        assert b'id="schedule-live"' in page.content
        assert b"schedule-view" in page.content
        assert b'id="schedule-model"' in page.content
    finally:
        release.set()
        manager.shutdown(wait=False)


def test_a_scheduled_run_uses_a_granted_model_and_refuses_a_missing_one(
    settings: Settings, tmp_path: Path
) -> None:
    seen: list[Task] = []

    def runner(task: Task) -> CycleResult:
        seen.append(task)
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="Finished.",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="completed",
        )

    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        bare = client.post(
            "/api/schedules",
            json={
                "name": "Review",
                "goal": "Review this project.",
                "workspace": str(tmp_path),
                "cadence": _daily(),
            },
        )
        assert bare.status_code == 200
        chosen = client.post(
            "/api/schedules",
            json={
                "name": "Review with a model",
                "goal": "Review this project.",
                "workspace": str(tmp_path),
                "model_id": "model_qwen",
                "cadence": _daily(),
            },
        )
        assert chosen.status_code == 200
        assert chosen.json()["model_id"] == "model_qwen"

        with patch(
            "agent_lbw.runtime.store.catalog.models_for_workspace",
            return_value=[{"id": "model_qwen", "name": "Qwen"}],
        ):
            missed = client.post(f"/api/schedules/{bare.json()['id']}/run")
        assert missed.status_code == 200
        assert missed.json()["runs"][0]["error"] == "Choose a model turned on for this project."
        assert seen == []

        with patch(
            "agent_lbw.runtime.store.catalog.use_model",
            return_value={
                "model_name": "qwen",
                "base_url": "http://models.internal/v1",
                "api_key": "secret",
            },
        ) as use:
            ran = client.post(f"/api/schedules/{chosen.json()['id']}/run")
        assert ran.status_code == 200
        assert "secret" not in ran.text
        task = manager.get(ran.json()["runs"][0]["task_id"])
        assert task is not None
        assert task.model_name == "qwen"
        assert task.model_base_url == "http://models.internal/v1"
        assert task.model_base_url != settings.model_base_url
        assert task.requester_id
        assert task.requester_name
        assert task.to_dict()["started_by"]["name"] == task.requester_name
        assert use.call_args.args[3] == "model_qwen"
    finally:
        manager.shutdown(wait=False)
