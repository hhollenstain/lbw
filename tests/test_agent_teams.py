"""An organization sets up agent teams, each in charge of steps, and a project turns them on."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.llm.client import AssistantTurn, ScriptedClient, ToolCall
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import run_cycle
from agent_lbw.runtime.roles import save_enabled_roles
from agent_lbw.runtime.store.agent_teams import enabled_steps, enabled_teams
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.store.schema import SCHEMA_VERSION, read_schema_version
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _client(settings: Settings, root: Path) -> TestClient:
    return TestClient(create_app(TaskManager(settings), default_workspace=root))


def _model(client: TestClient, name: str) -> str:
    with patch(
        "agent_lbw.runtime.store.catalog._fetch_names",
        return_value=["gpt-4.1"],
    ):
        saved = client.post(
            "/api/org/endpoints",
            json={"name": name, "provider": "openai", "api_key": "sk-test-secret"},
        )
    assert saved.status_code == 200
    endpoint = next(item for item in saved.json()["endpoints"] if item["name"] == name)
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": "gpt-4.1", "name": name},
    )
    assert added.status_code == 200
    model = next(item for item in added.json()["models"] if item["name"] == name)
    return model["id"]


def _organization(client: TestClient, workspace: Path, root: Path) -> str:
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    with database(root) as db:
        ensure_workspace_project(db, account["org"]["id"], workspace)
    return account["current_user_id"]


def _teams(response) -> dict[str, dict]:
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["agent_teams"]}


def test_many_teams_each_use_a_model(settings: Settings, tmp_path: Path) -> None:
    client = _client(settings, tmp_path)
    owner = _organization(client, tmp_path, tmp_path)
    planner = _model(client, "Planner")
    reviewer = _model(client, "Reviewer")

    created = client.post(
        "/api/org/agent-teams",
        json={
            "name": "Plan desk",
            "job": "plan",
            "instruction": "Break the goal into steps.",
            "model_id": planner,
        },
    )
    assert created.status_code == 200
    teams = _teams(
        client.post(
            "/api/org/agent-teams",
            json={
                "name": "Review desk",
                "job": "review",
                "instruction": "Look for a missing test.",
                "model_id": reviewer,
            },
        )
    )
    assert teams["Plan desk"]["job"] == "plan"
    assert teams["Plan desk"]["model_id"] == planner
    assert teams["Plan desk"]["steps"] == [
        {"key": "plan-desk", "job": "plan", "after": [], "paths": []}
    ]
    assert teams["Review desk"]["model_name"] == "Reviewer"
    assert teams["Review desk"]["service"] == "Reviewer"
    assert teams["Plan desk"]["enabled"] is False

    listed = client.get("/api/workspaces/roles", params={"path": str(tmp_path)})
    assert listed.status_code == 200
    body = listed.json()
    assert body["can_manage"] is True
    assert body["can_edit_teams"] is True
    assert body["uses_roles"] is True
    shown = {item["name"]: item for item in body["agent_teams"]}
    assert shown["Plan desk"]["enabled"] is False
    assert {item["id"] for item in body["models"]} >= {planner, reviewer}

    turned = client.put(
        "/api/workspaces/agent-teams",
        json={"path": str(tmp_path), "team_id": teams["Review desk"]["id"], "enabled": True},
    )
    assert turned.status_code == 200
    on = {item["name"]: item["enabled"] for item in turned.json()["agent_teams"]}
    assert on == {"Plan desk": False, "Review desk": True}
    assert turned.json()["uses_roles"] is False
    with database(tmp_path) as db:
        running = enabled_teams(db, tmp_path)
        steps = enabled_steps(db, tmp_path)
    assert [item["name"] for item in running] == ["Review desk"]
    assert [(step.key, step.job, step.model_id) for step in steps] == [
        ("review-desk", "review", reviewer)
    ]
    assert "missing test" in steps[0].instruction

    unknown = client.post(
        "/api/org/agent-teams",
        json={"name": "Ghost", "job": "build", "model_id": "model_missing"},
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "unknown model"

    added = client.post("/api/account/users", json={"name": "Grace"})
    grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (grace["id"],))
    refused = client.post(
        "/api/org/agent-teams",
        json={"name": "Member desk", "job": "build"},
    )
    assert refused.status_code == 403
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (owner,))

    removed = client.delete(f"/api/org/agent-teams/{teams['Review desk']['id']}")
    assert removed.status_code == 200
    assert "Review desk" not in {item["name"] for item in removed.json()["agent_teams"]}
    with database(tmp_path) as db:
        assert enabled_teams(db, tmp_path) == []


def test_a_team_changes_its_model_instruction_and_steps(settings: Settings, tmp_path: Path) -> None:
    client = _client(settings, tmp_path)
    _organization(client, tmp_path, tmp_path)
    first = _model(client, "Local")
    second = _model(client, "Hosted")
    team = _teams(
        client.post(
            "/api/org/agent-teams",
            json={"name": "Web desk", "job": "build", "model_id": first},
        )
    )["Web desk"]

    changed = _teams(
        client.put(
            f"/api/org/agent-teams/{team['id']}",
            json={
                "name": "Web desk",
                "instruction": "Own the React app.",
                "model_id": second,
                "steps": [
                    {"key": "web", "job": "build", "paths": ["web/src/**"]},
                    {"key": "web-review", "job": "review", "after": []},
                ],
            },
        )
    )["Web desk"]
    assert changed["model_name"] == "Hosted"
    assert changed["instruction"] == "Own the React app."
    assert changed["job"] == "build"
    assert changed["steps"] == [
        {"key": "web", "job": "build", "after": [], "paths": ["web/src/**"]},
        {"key": "web-review", "job": "review", "after": [], "paths": []},
    ]

    cleared = _teams(
        client.put(
            f"/api/org/agent-teams/{team['id']}",
            json={"name": "Web team", "instruction": "", "model_id": "", "steps": []},
        )
    )
    assert "Web desk" not in cleared
    assert cleared["Web team"]["model_id"] == ""
    assert [step["key"] for step in cleared["Web team"]["steps"]] == ["web", "web-review"]

    missing = client.put(
        "/api/org/agent-teams/agt_missing",
        json={"name": "x", "instruction": "", "model_id": "", "steps": []},
    )
    assert missing.status_code == 404


def test_steps_across_teams_must_form_a_graph(settings: Settings, tmp_path: Path) -> None:
    client = _client(settings, tmp_path)
    _organization(client, tmp_path, tmp_path)
    qa = _teams(
        client.post(
            "/api/org/agent-teams",
            json={"name": "QA desk", "steps": [{"key": "qa-desk", "job": "review"}]},
        )
    )["QA desk"]
    release = _teams(
        client.post(
            "/api/org/agent-teams",
            json={
                "name": "Release desk",
                "steps": [{"key": "release", "job": "review", "after": ["qa-desk"]}],
            },
        )
    )["Release desk"]
    assert release["steps"][0]["after"] == ["qa-desk"]

    loop = client.put(
        f"/api/org/agent-teams/{qa['id']}",
        json={
            "name": "QA desk",
            "instruction": "",
            "model_id": "",
            "steps": [{"key": "qa-desk", "job": "review", "after": ["release"]}],
        },
    )
    assert loop.status_code == 400
    assert "loop" in loop.json()["error"]

    backwards = client.post(
        "/api/org/agent-teams",
        json={"name": "Early", "steps": [{"key": "early", "job": "plan", "after": ["release"]}]},
    )
    assert backwards.status_code == 400
    assert "plans, then builds, then reviews" in backwards.json()["error"]

    taken = client.post(
        "/api/org/agent-teams",
        json={"name": "Copy", "steps": [{"key": "release", "job": "review"}]},
    )
    assert taken.status_code == 400
    assert "already belongs to Release desk" in taken.json()["error"]

    waited_on = client.delete(f"/api/org/agent-teams/{qa['id']}")
    assert waited_on.status_code == 400
    assert "waits for qa-desk" in waited_on.json()["error"]

    turned = None
    for team_id in (qa["id"], release["id"]):
        turned = client.put(
            "/api/workspaces/agent-teams",
            json={"path": str(tmp_path), "team_id": team_id, "enabled": True},
        )
    assert turned is not None
    assert turned.json()["graph"] == [
        {"job": "review", "wave": 1, "steps": ["qa-desk"], "parallel": False},
        {"job": "review", "wave": 2, "steps": ["release"], "parallel": False},
    ]


def test_a_project_turns_its_roles_into_teams(settings: Settings, tmp_path: Path) -> None:
    client = _client(settings, tmp_path)
    _organization(client, tmp_path, tmp_path)
    save_enabled_roles(tmp_path, ["architect", "security", "quality"])

    imported = client.post("/api/workspaces/agent-teams/import-roles", json={"path": str(tmp_path)})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    teams = {item["name"]: item for item in body["agent_teams"]}
    assert set(teams) == {"architect", "quality", "security"}
    assert all(item["enabled"] for item in teams.values())
    assert teams["security"]["steps"] == [
        {"key": "security", "job": "review", "after": [], "paths": []}
    ]
    assert "Pass when" in teams["security"]["instruction"]
    assert body["uses_roles"] is False
    assert body["graph"] == [
        {"job": "plan", "wave": 1, "steps": ["architect"], "parallel": False},
        {"job": "review", "wave": 1, "steps": ["quality", "security"], "parallel": True},
    ]

    again = client.post("/api/workspaces/agent-teams/import-roles", json={"path": str(tmp_path)})
    assert again.status_code == 200
    assert len(again.json()["agent_teams"]) == 3


def test_an_existing_database_gains_team_steps(tmp_path: Path) -> None:
    tenant = load_account(tmp_path)["org"]
    with database(tmp_path) as db:
        db.execute("DROP TABLE agent_team_steps")
        db.execute(
            """
            INSERT INTO agent_teams (id, tenant_id, name, job, instruction)
            VALUES ('agt_old', ?, 'Old Review', 'review', 'Check the docs.')
            """,
            (tenant["id"],),
        )
        db.execute("UPDATE schema_meta SET version = 7 WHERE id = 1")
    with database(tmp_path) as db:
        assert read_schema_version(db) == SCHEMA_VERSION
        rows = db.fetchall("SELECT step_key, job FROM agent_team_steps WHERE team_id = 'agt_old'")
    assert [(row["step_key"], row["job"]) for row in rows] == [("old-review", "review")]


def _broken_adder(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        "def add(left, right):\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    (root / "check.py").write_text(
        "from app import add\nassert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    lbw = root / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "max_repair_attempts: 0\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    (lbw / "goals.md").write_text("- [ ] Make the adder work\n", encoding="utf-8")
    (root / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(root)


def _fix_turns() -> list[AssistantTurn]:
    return [
        AssistantTurn(
            text=None,
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="write_file",
                    arguments={
                        "path": "app.py",
                        "content": "def add(left, right):\n    return left + right\n",
                    },
                )
            ],
        ),
        AssistantTurn(text="Implemented add."),
        AssistantTurn(text='{"met": true, "reason": "adder returns 5"}'),
    ]


def test_review_teams_on_their_own_models_replace_the_roles(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _broken_adder(repo)
    role_dir = repo / ".lbw" / "roles" / "policy"
    role_dir.mkdir(parents=True)
    (role_dir / "ROLE.md").write_text(
        "---\nname: policy\ntitle: Policy reviewer\njob: review\n"
        "description: Blocks unsafe changes.\n---\n\nBlock secrets.\n",
        encoding="utf-8",
    )
    save_enabled_roles(repo, ["policy"])

    client = _client(settings, tmp_path)
    _organization(client, repo, tmp_path)
    fast = _model(client, "Fast")
    careful = _model(client, "Careful")
    ids = []
    for name, model in (("Tests desk", fast), ("Docs desk", careful)):
        made = _teams(
            client.post(
                "/api/org/agent-teams",
                json={"name": name, "job": "review", "model_id": model},
            )
        )
        ids.append(made[name]["id"])
    for team_id in ids:
        client.put(
            "/api/workspaces/agent-teams",
            json={"path": str(repo), "team_id": team_id, "enabled": True},
        )

    reviewers = {
        fast: ScriptedClient([AssistantTurn(text='{"verdict": "pass", "summary": "tested"}')]),
        careful: ScriptedClient(
            [AssistantTurn(text='{"verdict": "block", "summary": "the docs are wrong"}')]
        ),
    }
    monkeypatch.setattr(
        "agent_lbw.runtime.improve._client_for_team",
        lambda team, *_args: reviewers[team["model_id"]],
    )

    result = run_cycle(
        repo,
        settings,
        ScriptedClient(_fix_turns()),
        goal="Make the adder work",
        store_root=tmp_path,
    )
    assert result.status == "failed"
    assert result.committed is False
    assert "Docs desk block" in (result.reason or "")
    verdicts = {item["role"]: item["verdict"] for item in result.evidence if "role" in item}
    assert verdicts.get("docs-desk") == "block"
    assert "policy" not in verdicts
    # Both reviews ran in the same wave, each on its own model; the block stops the commit.
    assert len(reviewers[fast].calls) == 1
    assert len(reviewers[careful].calls) == 1
