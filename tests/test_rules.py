"""KiwiFS rules stay in the space of one org, team, project, or person."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import Database, database
from agent_lbw.runtime.rules import rules_for_prompt
from agent_lbw.runtime.store.schema import apply_schema
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.tools.rules import rule_tools
from agent_lbw.web_ui import create_app

_SECRET = "RULE-BODY-SECRET-9f3c"
_TEAM_B = "team-b-body-isolated"
_TEAM_C = "team-c-body-not-granted"
_OTHER = "org_deadbeef0001"


class MemoryKiwi:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], str] = {}
        self.seen: list[tuple[str, str, str]] = []

    def ensure_space(self, name: str) -> None:
        self.seen.append(("ensure", name, ""))

    def in_space(self, name: str) -> _Space:
        return _Space(self, name)


class _Space:
    def __init__(self, kiwi: MemoryKiwi, space: str) -> None:
        self.kiwi = kiwi
        self.space = space

    def write(self, path: str, content: str) -> str:
        self.kiwi.seen.append(("write", self.space, path))
        self.kiwi.files[(self.space, path)] = content
        return "wrote"

    def raw(self, path: str) -> str:
        self.kiwi.seen.append(("read", self.space, path))
        return self.kiwi.files.get((self.space, path), "")

    def delete(self, path: str) -> str:
        self.kiwi.seen.append(("delete", self.space, path))
        self.kiwi.files.pop((self.space, path), None)
        return "deleted"


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def test_rules_stay_inside_one_scope(settings: Settings, tmp_path: Path, monkeypatch) -> None:
    memory = MemoryKiwi()
    monkeypatch.setattr(
        "agent_lbw.rules_http.open_kiwi",
        lambda base_url, api_key="": memory,
    )
    app = create_app(TaskManager(settings), default_workspace=tmp_path)
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert 'data-rail="rules"' in page.text
    assert 'id="rules-view"' in page.text
    assert 'id="inherited-rules"' in page.text
    assert 'id="ask-org-rule"' in page.text

    opened = client.get("/api/directory")
    assert opened.status_code == 200
    client.post("/api/directory/promote")
    added = client.post("/api/account/users", json={"name": "Grace"})
    grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
    owner = next(user for user in added.json()["users"] if user["role"] == "owner")
    tenant_id = opened.json()["tenant"]["id"]
    team_a = client.post("/api/directory/teams", json={"name": "Platform"}).json()
    team_b = client.post("/api/directory/teams", json={"name": "Billing"}).json()
    team_c = client.post("/api/directory/teams", json={"name": "Quiet"}).json()
    client.post(
        f"/api/directory/teams/{team_a['id']}/members",
        json={"identity_id": grace["id"], "rank": "member"},
    )

    applied = client.get("/api/rules/applied", params={"workspace": str(tmp_path)})
    assert applied.status_code == 200
    project_id = applied.json()["project"]["id"]
    for team_id, access in ((team_a["id"], "use"), (team_b["id"], "manage")):
        granted = client.post(
            f"/api/directory/projects/{project_id}/grants",
            json={"subject_type": "team", "subject_id": team_id, "access": access},
        )
        assert granted.status_code == 200

    org = client.post(
        "/api/rules",
        json={
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "name": "Standup",
            "body": _SECRET,
        },
    )
    assert org.status_code == 200
    optional = client.post(
        "/api/rules",
        json={
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "name": "Optional",
            "body": "optional-body",
        },
    )
    assert optional.status_code == 200
    required = client.put(
        f"/api/rules/{org.json()['id']}/required",
        json={"project_id": project_id, "required": True},
    )
    assert required.status_code == 200
    assert project_id in required.json()["required_project_ids"]
    for scope_id, name, body in (
        (team_a["id"], "Platform note", "team-a-body"),
        (team_b["id"], "Billing note", _TEAM_B),
        (team_c["id"], "Quiet note", _TEAM_C),
    ):
        saved = client.post(
            "/api/rules",
            json={"scope_type": "team", "scope_id": scope_id, "name": name, "body": body},
        )
        assert saved.status_code == 200, saved.text
    project_rule = client.post(
        "/api/rules",
        json={
            "scope_type": "project",
            "scope_id": project_id,
            "name": "Local",
            "body": "project-body",
        },
    )
    assert project_rule.status_code == 200
    personal = client.post(
        "/api/rules",
        json={
            "scope_type": "identity",
            "scope_id": owner["id"],
            "name": "Mine",
            "body": "# Quiet\n\nowner-only-body\n\n<script>alert(1)</script>",
        },
    )
    assert personal.status_code == 200
    assert "<h1>" in personal.json()["html"]
    assert "owner-only-body" in personal.json()["html"]
    assert "&lt;script&gt;" in personal.json()["html"]

    with database(tmp_path) as db:
        db.execute(
            """
            INSERT INTO tenants (id, kind, name, slug, mode)
            VALUES (?, 'organization', 'Other', 'other-0001', 'local')
            """,
            (_OTHER,),
        )
    stolen = client.post(
        "/api/rules",
        json={
            "scope_type": "tenant",
            "scope_id": _OTHER,
            "name": "Stolen",
            "body": "other-org-body",
        },
    )
    assert stolen.status_code == 403
    assert "org-deadbeef0001" not in {item[1] for item in memory.seen}
    stored = (tmp_path / ".lbw" / "lbw.db").read_bytes()
    for secret in (_SECRET, _TEAM_B, _TEAM_C, "owner-only-body", "other-org-body"):
        assert secret.encode() not in stored

    _become(tmp_path, grace["id"])
    editor = client.get("/api/rules/editor")
    assert editor.status_code == 200
    scopes = {scope["scope_id"]: scope for scope in editor.json()["scopes"]}
    assert team_a["id"] in scopes
    assert team_b["id"] not in scopes
    assert team_c["id"] not in scopes
    assert owner["id"] not in scopes
    assert scopes[tenant_id]["can_write"] is False
    assert scopes[grace["id"]]["name"] == "Your rules"
    standup = next(rule for rule in scopes[tenant_id]["rules"] if rule["name"] == "Standup")
    assert "<p>" in standup["html"]
    assert _SECRET in standup["html"]
    assert "<script>" not in standup["html"]
    before = len(memory.seen)
    refused = client.post(
        "/api/rules",
        json={
            "scope_type": "team",
            "scope_id": team_b["id"],
            "name": "Nope",
            "body": "should-not-write",
        },
    )
    assert refused.status_code == 403
    assert len(memory.seen) == before
    project_refused = client.post(
        "/api/rules",
        json={
            "scope_type": "project",
            "scope_id": project_id,
            "name": "Nope",
            "body": "should-not-write",
        },
    )
    assert project_refused.status_code == 403
    require_refused = client.put(
        f"/api/rules/{org.json()['id']}/required",
        json={"project_id": project_id, "required": False},
    )
    assert require_refused.status_code == 403

    view = client.get("/api/rules/applied", params={"workspace": str(tmp_path)})
    assert view.status_code == 200
    names = {rule["name"]: rule for rule in view.json()["rules"]}
    assert names["Standup"]["source"] == "organization"
    assert names["Standup"]["required"] is True
    assert names["Standup"]["body"] == _SECRET
    assert "Optional" not in names
    assert names["Platform note"]["source"] == "team"
    assert names["Billing note"]["body"] == _TEAM_B
    assert "Quiet note" not in names
    assert names["Local"]["source"] == "project"
    assert "Mine" not in names
    read_spaces = {space for kind, space, _path in memory.seen if kind == "read"}
    assert f"team-{team_c['id'].removeprefix('team_')}" not in read_spaces

    duty = client.post(
        "/api/directory/duties",
        json={
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "identity_id": grace["id"],
            "duty": "rules",
        },
    )
    assert duty.status_code == 200
    _become(tmp_path, owner["id"])
    duty_as_owner = client.post(
        "/api/directory/duties",
        json={
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "identity_id": grace["id"],
            "duty": "rules",
        },
    )
    assert duty_as_owner.status_code == 200
    _become(tmp_path, grace["id"])
    allowed = client.post(
        "/api/rules",
        json={
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "name": "From Grace",
            "body": "grace-org-rule",
        },
    )
    assert allowed.status_code == 200

    monkeypatch.setattr("agent_lbw.runtime.rules.KiwiFS", lambda *args, **kwargs: memory)
    text = rules_for_prompt(tmp_path, tmp_path, kiwifs_url="http://kiwi.test")
    assert _SECRET in text
    assert _TEAM_B in text
    assert _TEAM_C not in text
    assert "owner-only-body" not in text


def test_write_rule_refuses_another_organization(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    memory = MemoryKiwi()
    monkeypatch.setattr("agent_lbw.tools.rules.KiwiFS", lambda *args, **kwargs: memory)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    account = client.get("/api/directory").json()
    tenant_id = account["tenant"]["id"]
    tools = {
        tool.name: tool for tool in rule_tools(base_url="http://kiwi.test", store_root=tmp_path)
    }
    saved = tools["write_rule"].handler(
        scope_type="tenant",
        scope_id=tenant_id,
        name="Standup",
        body=_SECRET,
    )
    assert saved.ok
    assert memory.seen[0][1].startswith("org-")
    refused = tools["write_rule"].handler(
        scope_type="tenant",
        scope_id=_OTHER,
        name="Stolen",
        body="other-org-body",
    )
    assert not refused.ok
    assert "org-deadbeef0001" not in {item[1] for item in memory.seen}
    listed = tools["list_rules"].handler(scope_type="tenant", scope_id=tenant_id)
    assert listed.ok
    assert "Standup" in listed.output


def test_an_existing_duty_table_accepts_rules(tmp_path: Path) -> None:
    path = tmp_path / ".lbw" / "lbw.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE duty_grants (
            id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            identity_id TEXT NOT NULL,
            duty TEXT NOT NULL CHECK (duty IN ('billing', 'security'))
        )
        """
    )
    raw.commit()
    raw.close()
    with database(tmp_path) as db:
        db.execute(
            """
            INSERT INTO identities (id, email, display_name, created)
            VALUES ('user_abc123abc123', NULL, 'Ada', 1)
            """
        )
        db.execute(
            """
            INSERT INTO duty_grants (id, scope_type, scope_id, identity_id, duty)
            VALUES ('duty_1', 'tenant', 'org_abc123abc123', 'user_abc123abc123', 'rules')
            """
        )


def test_postgres_duty_check_keeps_percent_signs_as_sql() -> None:
    seen: list[str] = []

    class Cursor:
        def execute(self, statement: str, params: tuple = ()) -> Cursor:
            seen.append(statement)
            return self

        def fetchone(self) -> dict[str, str]:
            return {"name": "duty_grants", "sql": "CHECK (duty IN ('billing', 'security'))"}

        def fetchall(self) -> list[dict[str, str]]:
            if "FROM agent_teams" in seen[-1]:
                return []
            return [{"name": "duty_grants_duty_check"}]

    class Raw:
        def cursor(self) -> Cursor:
            return Cursor()

    apply_schema(Database(Raw(), "postgres"))
    assert any("%%duty%%" in statement and "%%rules%%" in statement for statement in seen)
    for statement in seen:
        assert "%" not in statement.replace("%%", "").replace("%s", "")
