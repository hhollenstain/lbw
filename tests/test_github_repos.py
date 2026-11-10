"""Synced GitHub repositories, and who can see them in the project list."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.auth import reset_actor, set_actor
from agent_lbw.runtime.store.github_sync import (
    collapse_repo_projects,
    projects_for_person,
    recorded_workspaces,
    set_repo_enabled,
    stored_repos,
    sync_installation_repos,
)
from agent_lbw.runtime.store.projects import ensure_project, grant_project
from agent_lbw.runtime.store.recording import project_for_workspace
from agent_lbw.runtime.store.schema import (
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
    table_columns,
)
from agent_lbw.runtime.store.teams import (
    add_team_member,
    ensure_github_team,
    promote_to_organization,
)


def test_sync_stores_the_github_id_and_hides_an_archived_repository(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    repos = [
        {"id": 10, "full_name": "acme/demo", "archived": False},
        {"id": 11, "full_name": "acme/old", "archived": True},
    ]
    with database(tmp_path) as db:
        sync_installation_repos(db, account, repos)
        rows = {
            row["locator"]: row
            for row in db.fetchall(
                "SELECT locator, provider_id, enabled, archived_at, tenant_id FROM projects"
            )
        }
        assert rows["acme/demo"]["provider_id"] == "10"
        assert rows["acme/demo"]["enabled"] == 0
        assert rows["acme/demo"]["archived_at"] is None
        assert rows["acme/demo"]["tenant_id"] == account["org"]["id"]
        assert rows["acme/old"]["provider_id"] == "11"
        stamp = rows["acme/old"]["archived_at"]
        assert stamp
        listed = stored_repos(db)
        assert [item["full_name"] for item in listed] == ["acme/demo"]
        turned = set_repo_enabled(db, listed[0]["project_id"], True)
        assert turned[0]["enabled"] is True
        sync_installation_repos(db, account, repos)
        again = {
            row["locator"]: row
            for row in db.fetchall("SELECT locator, enabled, archived_at FROM projects")
        }
        assert again["acme/demo"]["enabled"] == 1
        assert again["acme/old"]["archived_at"] == stamp


def test_a_person_sees_only_enabled_repositories_they_can_use(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    person = account["current_user_id"]
    tenant = account["org"]["id"]
    with database(tmp_path) as db:
        promote_to_organization(db, tenant)
        sync_installation_repos(
            db,
            account,
            [
                {"id": 1, "full_name": "acme/a", "archived": False},
                {"id": 2, "full_name": "acme/b", "archived": False},
                {"id": 3, "full_name": "acme/old", "archived": True},
                {"id": 4, "full_name": "acme/off", "archived": False},
            ],
        )
        found = {
            row["locator"]: row["id"] for row in db.fetchall("SELECT id, locator FROM projects")
        }
        for locator in ("acme/a", "acme/b", "acme/old", "acme/off"):
            set_repo_enabled(db, found[locator], True)
        set_repo_enabled(db, found["acme/off"], False)
        grant_project(
            db,
            found["acme/a"],
            subject_type="identity",
            subject_id=person,
            access="use",
        )
        grant_project(
            db,
            found["acme/old"],
            subject_type="identity",
            subject_id=person,
            access="use",
        )
        grant_project(
            db,
            found["acme/off"],
            subject_type="identity",
            subject_id=person,
            access="use",
        )
        team = ensure_github_team(db, tenant, "Platform", "platform")
        grant_project(
            db,
            found["acme/b"],
            subject_type="team",
            subject_id=team["id"],
            access="use",
        )
        names = [row["locator"] for row in projects_for_person(db, tenant, person)]
        assert names == ["acme/a"]
        add_team_member(db, team["id"], person, "member")
        names = [row["locator"] for row in projects_for_person(db, tenant, person)]
        assert names == ["acme/a", "acme/b"]
    token = set_actor(person)
    try:
        listed = [item["path"] for item in recorded_workspaces(tmp_path)]
    finally:
        reset_actor(token)
    assert listed == ["acme/a", "acme/b"]


def test_the_same_repository_is_stored_once(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    tenant = account["org"]["id"]
    person = account["current_user_id"]
    checkout = tmp_path / "autochannel-bot"
    checkout.mkdir()
    subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/autochannel-bot.git"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    with database(tmp_path) as db:
        path_row = ensure_project(
            db,
            tenant,
            name="autochannel-bot",
            kind="github",
            locator=str(checkout),
        )
        synced = ensure_project(
            db,
            tenant,
            name="autochannel-bot",
            kind="github",
            locator="acme/autochannel-bot",
        )
        db.execute(
            "UPDATE projects SET provider_id = ?, enabled = 1 WHERE id = ?",
            ("42", synced["id"]),
        )
        db.execute(
            """
            INSERT INTO tasks (id, tenant_id, project_id, requester_id, status, created)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("task_kept", tenant, path_row["id"], person, "done", 1),
        )
        collapse_repo_projects(db, tenant)
        rows = db.fetchall("SELECT id, locator, provider_id FROM projects ORDER BY locator")
        assert [row["locator"] for row in rows] == ["acme/autochannel-bot"]
        assert rows[0]["provider_id"] == "42"
        task = db.fetchone("SELECT project_id FROM tasks WHERE id = ?", ("task_kept",))
        assert task is not None
        assert task["project_id"] == rows[0]["id"]


def test_the_same_folder_is_stored_once(tmp_path: Path) -> None:
    account = load_account(tmp_path)
    tenant = account["org"]["id"]
    folder = tmp_path / "demo"
    folder.mkdir()
    alias = tmp_path / "link"
    alias.symlink_to(folder, target_is_directory=True)
    with database(tmp_path) as db:
        ensure_project(db, tenant, name="demo", kind="folder", locator=str(folder))
        ensure_project(db, tenant, name="demo", kind="folder", locator=str(alias))
        collapse_repo_projects(db, tenant)
        rows = db.fetchall("SELECT locator FROM projects")
        assert [row["locator"] for row in rows] == [str(folder.resolve())]
        again = project_for_workspace(db, account, alias, "demo")
        assert again["locator"] == str(folder.resolve())
        assert len(db.fetchall("SELECT id FROM projects")) == 1


def test_schema_is_applied_once(tmp_path: Path, monkeypatch) -> None:
    calls = {"n": 0}
    real = apply_schema

    def wrapped(db):
        calls["n"] += 1
        real(db)

    monkeypatch.setattr("agent_lbw.runtime.store.schema.apply_schema", wrapped)
    with database(tmp_path) as db:
        assert read_schema_version(db) == SCHEMA_VERSION
    with database(tmp_path) as db:
        assert read_schema_version(db) == SCHEMA_VERSION
    assert calls["n"] == 1


def test_an_older_database_creates_project_model_settings(tmp_path: Path) -> None:
    with database(tmp_path) as db:
        db.execute("DROP TABLE model_project_settings")
        db.execute("UPDATE schema_meta SET version = 2 WHERE id = 1")
    with database(tmp_path) as db:
        assert read_schema_version(db) == SCHEMA_VERSION
        assert "enabled" in table_columns(db, "model_project_settings")
