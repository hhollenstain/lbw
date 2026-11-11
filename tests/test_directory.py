"""Directory tables, sealed task requests, and who may read them."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.schedules import delete_schedule, upsert_schedule
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import read_account, save_account
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.schema import TASK_COLUMNS, table_columns
from agent_lbw.runtime.store.tasks import read_task
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

_GOAL = "north-star-goal-9f3c"
_INSTRUCTION = "schedule-instruction-stays-in-the-file"


def test_seal_roundtrips_a_result_longer_than_one_counter_byte() -> None:
    key = b"k" * 32
    text = "diff " + ("x" * 9000)
    assert open_secret(key, seal(key, text)) == text


def test_seal_fails_closed_on_the_wrong_key() -> None:
    key = b"k" * 32
    token = seal(key, _GOAL)
    assert _GOAL not in token
    assert open_secret(key, token) == _GOAL
    with pytest.raises(ValueError, match="unreadable"):
        open_secret(b"q" * 32, token)


def test_directory_seals_the_request_and_checks_the_grant(
    settings: Settings, tmp_path: Path
) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(task):
        started.set()
        assert release.wait(5)
        return CycleResult(
            status="success",
            goal=task.goal,
            summary="kept in the run",
            tests_passed=True,
            committed=False,
            published=False,
            commit_sha=None,
            reason="done",
            branch="lbw/directory",
            pr_url="https://example.test/pull/7",
        )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    manager = TaskManager(settings, runner=runner)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        opened = client.get("/api/directory")
        assert opened.status_code == 200
        assert opened.json()["tenant"]["kind"] == "personal"
        assert "goal" not in _keys(opened.json())

        refused = client.post("/api/directory/teams", json={"name": "Platform"})
        assert refused.status_code == 400
        assert refused.json()["error"] == "teams belong to an organization"

        promoted = client.post("/api/directory/promote")
        assert promoted.status_code == 200
        assert promoted.json()["tenant"]["kind"] == "organization"
        tenant_id = promoted.json()["tenant"]["id"]
        with database(tmp_path) as db:
            slug = db.fetchone("SELECT slug FROM tenants WHERE id = ?", (tenant_id,))["slug"]
        renamed = client.put("/api/account", json={"org_name": "North"})
        assert renamed.status_code == 200
        with database(tmp_path) as db:
            again = db.fetchone("SELECT kind, slug FROM tenants WHERE id = ?", (tenant_id,))
        assert again["kind"] == "organization"
        assert again["slug"] == slug

        team = client.post("/api/directory/teams", json={"name": "Platform"})
        assert team.status_code == 200
        added = client.post("/api/account/users", json={"name": "Grace"})
        assert added.status_code == 200
        grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
        owner = next(user for user in added.json()["users"] if user["role"] == "owner")
        joined = client.post(
            f"/api/directory/teams/{team.json()['id']}/members",
            json={"identity_id": grace["id"], "rank": "member"},
        )
        assert joined.status_code == 200
        outsider = client.post(
            f"/api/directory/teams/{team.json()['id']}/members",
            json={"identity_id": "user_missing", "rank": "member"},
        )
        assert outsider.status_code == 400

        owner_duty = client.post(
            "/api/directory/duties",
            json={
                "scope_type": "tenant",
                "scope_id": tenant_id,
                "identity_id": owner["id"],
                "duty": "billing",
            },
        )
        assert owner_duty.status_code == 400
        assert "owner already holds" in owner_duty.json()["error"]
        duty = client.post(
            "/api/directory/duties",
            json={
                "scope_type": "tenant",
                "scope_id": tenant_id,
                "identity_id": grace["id"],
                "duty": "security",
            },
        )
        assert duty.status_code == 200
        onboarded = client.post(
            "/api/directory/projects",
            json={
                "name": "LBW",
                "kind": "github",
                "locator": "https://github.com/example/lbw",
            },
        )
        assert onboarded.status_code == 200
        assert onboarded.json()["kind"] == "github"
        again_project = client.post(
            "/api/directory/projects",
            json={
                "name": "LBW again",
                "kind": "github",
                "locator": "https://github.com/example/lbw",
            },
        )
        assert again_project.json()["id"] == onboarded.json()["id"]

        created = client.post("/api/tasks", json={"workspace": str(workspace), "goal": _GOAL})
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert started.wait(5)
        with database(tmp_path) as db:
            assert table_columns(db, "tasks") == TASK_COLUMNS
            row = db.fetchone("SELECT goal_ciphertext, status FROM tasks WHERE id = ?", (task_id,))
        assert row["status"] == "queued"
        assert _GOAL not in row["goal_ciphertext"]
        stored = (tmp_path / ".lbw" / "lbw.db").read_bytes()
        master = (tmp_path / ".lbw" / "master.key").read_bytes()
        assert _GOAL.encode() not in stored
        assert master not in stored
        assert (tmp_path / ".lbw" / "master.key").stat().st_mode & 0o777 == 0o600

        readable = client.get(f"/api/directory/tasks/{task_id}")
        assert readable.status_code == 200
        assert readable.json()["goal"] == _GOAL
        with database(tmp_path) as db:
            with pytest.raises(PermissionError):
                read_task(db, tmp_path, task_id=task_id, identity_id=grace["id"])
            _set_rank(db, tenant_id, grace["id"], "admin")
            assert (
                read_task(db, tmp_path, task_id=task_id, identity_id=grace["id"])["goal"] == _GOAL
            )
            _set_rank(db, tenant_id, grace["id"], "member")
            with pytest.raises(PermissionError):
                read_task(db, tmp_path, task_id=task_id, identity_id=grace["id"])
        project_id = readable.json()["project_id"]
        granted = client.post(
            f"/api/directory/projects/{project_id}/grants",
            json={"subject_type": "team", "subject_id": team.json()["id"], "access": "use"},
        )
        assert granted.status_code == 200
        with database(tmp_path) as db:
            assert (
                read_task(db, tmp_path, task_id=task_id, identity_id=grace["id"])["goal"] == _GOAL
            )
            assert can_access(db, grace["id"], project_id, need="manage") is False
        managed = client.post(
            f"/api/directory/projects/{project_id}/grants",
            json={"subject_type": "team", "subject_id": team.json()["id"], "access": "manage"},
        )
        assert managed.status_code == 200
        with database(tmp_path) as db:
            assert can_access(db, grace["id"], project_id, need="manage") is True

        listed = client.get("/api/directory")
        assert listed.status_code == 200
        assert "goal" not in _keys(listed.json())
        assert listed.json()["teams"][0]["name"] == "Platform"

        release.set()
        deadline = time.time() + 5
        finished = None
        while time.time() < deadline:
            finished = manager.get(task_id)
            if finished is not None and finished.status == "success":
                break
            time.sleep(0.02)
        assert finished is not None and finished.status == "success"
        with database(tmp_path) as db:
            after = db.fetchone(
                "SELECT goal_ciphertext, status, branch, pr_url FROM tasks WHERE id = ?",
                (task_id,),
            )
        assert after["goal_ciphertext"] == row["goal_ciphertext"]
        assert after["status"] == "success"
        assert after["branch"] == "lbw/directory"
        assert after["pr_url"] == "https://example.test/pull/7"

        saved = upsert_schedule(
            tmp_path,
            {
                "name": "Nightly",
                "goal": _INSTRUCTION,
                "workspace": str(workspace),
                "kind": "project",
                "cadence": {"kind": "daily", "hour": 9, "minute": 0},
            },
        )
        with database(tmp_path) as db:
            indexed = db.fetchone(
                "SELECT kind, cadence FROM schedules WHERE id = ?", (saved["id"],)
            )
        assert indexed["kind"] == "scan"
        assert "daily" in indexed["cadence"]
        assert _INSTRUCTION.encode() not in (tmp_path / ".lbw" / "lbw.db").read_bytes()
        scan = client.post(
            "/api/directory/schedules",
            json={"project_id": project_id, "cadence": "weekly"},
        )
        assert scan.status_code == 200
        assert scan.json()["kind"] == "scan"
        delete_schedule(tmp_path, saved["id"])
        with database(tmp_path) as db:
            assert db.fetchone("SELECT id FROM schedules WHERE id = ?", (saved["id"],)) is None
            assert (
                db.fetchone("SELECT kind FROM schedules WHERE id = ?", (scan.json()["id"],))["kind"]
                == "scan"
            )
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_older_organization_tables_import_as_an_organization(tmp_path: Path) -> None:
    path = tmp_path / ".lbw" / "lbw.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            mode TEXT NOT NULL
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            created INTEGER NOT NULL
        );
        CREATE TABLE install (
            id INTEGER PRIMARY KEY,
            org_id TEXT NOT NULL,
            current_user_id TEXT NOT NULL
        );
        INSERT INTO organizations (id, name, mode) VALUES ('org_fixed', 'Imported', 'local');
        INSERT INTO users (id, org_id, name, role, created)
            VALUES ('user_fixed', 'org_fixed', 'Ada', 'owner', 10);
        INSERT INTO install (id, org_id, current_user_id) VALUES (1, 'org_fixed', 'user_fixed');
        """
    )
    conn.commit()
    conn.close()
    loaded = load_account(tmp_path)
    assert loaded["org"]["id"] == "org_fixed"
    assert loaded["org"]["kind"] == "organization"
    assert loaded["users"][0]["id"] == "user_fixed"
    with database(tmp_path) as db:
        left = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'organizations'"
        )
    assert left is None


def _set_rank(db, tenant_id: str, identity_id: str, rank: str) -> None:
    account = read_account(db)
    assert account is not None and account["org"]["id"] == tenant_id
    for user in account["users"]:
        if user["id"] == identity_id:
            user["role"] = rank
    save_account(db, account)


def _keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(value)
        for item in value.values():
            found.update(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_keys(item))
    return found
