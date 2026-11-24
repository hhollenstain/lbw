"""Favorite projects: a person stars a project to keep it at the top of their list."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.account import add_user, load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.store.auth import reset_actor, set_actor
from agent_lbw.runtime.store.favorites import set_favorite
from agent_lbw.runtime.store.github_sync import collapse_repo_projects, recorded_workspaces
from agent_lbw.runtime.store.projects import ensure_project
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


def _by_name(client: TestClient) -> dict[str, dict]:
    listed = client.get("/api/workspaces").json()["workspaces"]
    return {item["name"]: item for item in listed}


def test_a_star_keeps_a_project_in_favorites(settings: Settings, tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    manager = TaskManager(settings, runner=_ok)
    app = create_app(manager, default_workspace=tmp_path)
    client = TestClient(app)
    try:
        page = client.get("/")
        assert b'id="project-list"' in page.content
        assert b'id="project-list-more"' in page.content
        assert b'"/api/workspaces/favorites"' in page.content
        for folder in (alpha, beta):
            chosen = client.post("/api/workspaces/select", json={"path": str(folder)})
            assert chosen.status_code == 200
        rows = _by_name(client)
        assert rows["alpha"]["favorite"] is False
        assert rows["beta"]["favorite"] is False

        starred = client.put(
            "/api/workspaces/favorites",
            json={"project_id": rows["beta"]["project_id"], "favorite": True},
        )
        assert starred.status_code == 200
        assert starred.json() == {"favorites": [rows["beta"]["project_id"]]}
        again = _by_name(client)
        assert again["beta"]["favorite"] is True
        assert again["alpha"]["favorite"] is False

        twice = client.put(
            "/api/workspaces/favorites",
            json={"project_id": rows["beta"]["project_id"], "favorite": True},
        )
        assert twice.json() == {"favorites": [rows["beta"]["project_id"]]}

        unstarred = client.put(
            "/api/workspaces/favorites",
            json={"project_id": rows["beta"]["project_id"], "favorite": False},
        )
        assert unstarred.json() == {"favorites": []}
        assert _by_name(client)["beta"]["favorite"] is False

        unknown = client.put(
            "/api/workspaces/favorites",
            json={"project_id": "proj_missing", "favorite": True},
        )
        assert unknown.status_code == 404
        assert unknown.json() == {"error": "unknown project"}
        with database(Path(app.state.ui.store_root)) as db:
            notes = db.fetchall("SELECT action FROM audit_log")
        assert all("favorite" not in str(row["action"]) for row in notes)
    finally:
        manager.shutdown(wait=False)


def test_a_favorite_belongs_to_one_person(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    tenant = account["org"]["id"]
    owner = account["current_user_id"]
    folder = tmp_path / "shared"
    folder.mkdir()
    other = add_user(tmp_path, "Sam", email="sam@example.com")
    coworker = next(user["id"] for user in other["users"] if user["id"] != owner)
    with database(tmp_path) as db:
        project = ensure_project(db, tenant, name="shared", kind="folder", locator=str(folder))
        token = set_actor(owner)
        try:
            assert set_favorite(db, project["id"], favorite=True) == [project["id"]]
        finally:
            reset_actor(token)
    for person, starred in ((owner, True), (coworker, False)):
        token = set_actor(person)
        try:
            listed = {item["project_id"]: item for item in recorded_workspaces(tmp_path)}
        finally:
            reset_actor(token)
        assert listed[project["id"]]["favorite"] is starred


def test_a_favorite_follows_a_merged_project(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    tenant = account["org"]["id"]
    folder = tmp_path / "demo"
    folder.mkdir()
    alias = tmp_path / "link"
    alias.symlink_to(folder, target_is_directory=True)
    with database(tmp_path) as db:
        kept = ensure_project(db, tenant, name="demo", kind="folder", locator=str(folder))
        merged = ensure_project(db, tenant, name="demo", kind="folder", locator=str(alias))
        set_favorite(db, merged["id"], favorite=True)
        collapse_repo_projects(db, tenant)
        rows = db.fetchall("SELECT project_id FROM project_favorites")
    assert [row["project_id"] for row in rows] == [kept["id"]]
