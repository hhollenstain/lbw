"""Organization settings assign skills and enforce agent-team tasks on a project."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.assignments import skills_for_project
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def test_profile_holds_org_settings_and_a_project_shows_only_assignments(
    settings: Settings, tmp_path: Path
) -> None:
    app = create_app(TaskManager(settings), default_workspace=tmp_path)
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    rules = page.text.split('id="rules-view"', 1)[1].split('id="skills-view"', 1)[0]
    assert "org-rules-list" not in rules
    assert 'id="personal-settings"' in page.text
    assert 'id="organization-settings"' in page.text
    assert 'id="ask-org-rule"' in page.text

    client.get("/api/directory")
    client.post("/api/directory/promote")
    added = client.post("/api/account/users", json={"name": "Grace"})
    assert added.status_code == 200
    grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
    account = load_account(tmp_path)
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, account["org"]["id"], tmp_path)
    team = client.post("/api/directory/teams", json={"name": "Platform"})
    assert team.status_code == 200
    team_id = team.json()["id"]

    settings_body = client.get("/api/org/settings")
    assert settings_body.status_code == 200
    assert settings_body.json()["can_manage"] is True

    assigned = client.put(
        "/api/org/skills",
        json={
            "skill_name": "tdd",
            "scope_type": "project",
            "scope_id": project["id"],
            "assigned": True,
        },
    )
    assert assigned.status_code == 200
    waiting = client.put(
        "/api/org/skills",
        json={
            "skill_name": "kiwi",
            "scope_type": "team",
            "scope_id": team_id,
            "assigned": True,
        },
    )
    assert waiting.status_code == 200
    with database(tmp_path) as db:
        names = skills_for_project(db, account["org"]["id"], project["id"])
    assert "tdd" in names
    assert "kiwi" not in names

    granted = client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "team", "subject_id": team_id, "access": "use"},
    )
    assert granted.status_code == 200
    with database(tmp_path) as db:
        names = skills_for_project(db, account["org"]["id"], project["id"])
    assert "kiwi" in names

    enforced = client.put(
        "/api/org/roles",
        json={"role_name": "security", "enforced": True},
    )
    assert enforced.status_code == 200
    saved = client.put(
        "/api/workspaces/roles",
        json={"path": str(tmp_path), "enabled": ["builder", "qa"]},
    )
    assert saved.status_code == 200
    roles = {role["name"]: role for role in saved.json()["roles"]}
    assert roles["security"]["enabled"] is True
    assert roles["security"]["enforced"] is True
    assert "security" in saved.json()["enabled"]

    _become(tmp_path, grace["id"])
    blocked_skill = client.put(
        "/api/org/skills",
        json={
            "skill_name": "review",
            "scope_type": "tenant",
            "scope_id": account["org"]["id"],
            "assigned": True,
        },
    )
    assert blocked_skill.status_code == 403
    blocked_person = client.post("/api/account/users", json={"name": "Pat"})
    assert blocked_person.status_code == 403
    seen = client.get("/api/org/settings")
    assert seen.status_code == 200
    assert seen.json()["can_manage"] is False
