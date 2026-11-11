"""An organization brings its own models and grants them before an agent can build."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.catalog import _fetch_names
from agent_lbw.runtime.store.names import new_id
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.store.tasks import save_task_request, touch_task
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def _task(root: Path, project_id: str, requester_id: str, tenant_id: str) -> str:
    task_id = new_id("task")
    with database(root) as db:
        save_task_request(
            db,
            root,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            requester_id=requester_id,
            goal="Ship the change",
            status="running",
        )
    return task_id


def test_a_model_is_sealed_and_an_agent_needs_a_grant_and_a_person(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    page = client.get("/")
    bundle = client.get("/static/app/app.js")
    assert bundle.status_code == 200
    script = bundle.text
    assert 'id="org-tab-models"' in page.text
    assert 'id="org-panel-rules" data-org-panel="rules" hidden' in page.text
    assert 'id="org-models-root"' in page.text
    assert 'data-rail="models"' in page.text
    assert 'id="workspace-models"' in page.text
    assert 'src="/static/app/app.js"' in page.text
    for token in (
        'id:"org-endpoints"',
        'id:"add-org-endpoint"',
        'id:"org-models"',
        'id:"add-org-model"',
        'id:"org-agents"',
        'id:"add-org-agent"',
        'id:"open-org-model"',
        'id:"open-org-agent"',
        'id:"endpoint-remove-dialog"',
        'id:"confirm-endpoint-remove"',
        '"data-model-enable"',
        '"data-project-model"',
        '"data-agent-remove"',
    ):
        assert token in script
    assert 'id="org-skill-repo-scope"' not in page.text

    client.get("/api/directory")
    client.post("/api/directory/promote")
    added = client.post("/api/account/users", json={"name": "Grace"})
    grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
    account = client.get("/api/account").json()
    owner_id = account["current_user_id"]
    tenant_id = account["org"]["id"]
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, tenant_id, tmp_path)

    missing = client.post(
        "/api/org/endpoints",
        json={"name": "Local", "provider": "vllm", "base_url": ""},
    )
    assert missing.status_code == 400

    secret = "sk-test-secret"
    with patch(
        "agent_lbw.runtime.store.catalog._fetch_names",
        return_value=["gpt-4.1"],
    ) as fetch:
        saved = client.post(
            "/api/org/endpoints",
            json={"name": "Work", "provider": "openai", "api_key": secret},
        )
        assert saved.status_code == 200
        body = saved.json()
        assert secret not in saved.text
        endpoint = body["endpoints"][0]
        assert endpoint["id"].startswith("ep_")
        assert endpoint["has_api_key"] is True
        assert endpoint["model_names"] == ["gpt-4.1"]
        assert "api_key" not in endpoint
        assert {item["id"] for item in body["providers"]} >= {
            "openai",
            "anthropic",
            "google",
            "ollama",
            "vllm",
            "lmstudio",
        }
        with database(tmp_path) as db:
            row = db.fetchone("SELECT api_key_ciphertext, models_json FROM model_endpoints")
        assert row is not None
        assert secret not in row["api_key_ciphertext"]
        assert "gpt-4.1" in row["models_json"]

        refused_name = client.post(
            "/api/org/models",
            json={"endpoint_id": endpoint["id"], "model_name": "missing"},
        )
        assert refused_name.status_code == 400
        added_model = client.post(
            "/api/org/models",
            json={"endpoint_id": endpoint["id"], "model_name": "gpt-4.1", "name": "Work"},
        )
        assert added_model.status_code == 200
        model = added_model.json()["models"][0]
        assert model["id"].startswith("model_")
        assert model["endpoint_id"] == endpoint["id"]
        kept = client.delete(f"/api/org/endpoints/{endpoint['id']}")
        assert kept.status_code == 400

        fetch.return_value = ["gpt-4.1", "gpt-4.1-mini"]
        synced = client.post(f"/api/org/endpoints/{endpoint['id']}/sync")
        assert synced.status_code == 200
        assert synced.json()["endpoints"][0]["model_names"] == ["gpt-4.1", "gpt-4.1-mini"]
        fetch.side_effect = AssertionError("the stored list should not ask the endpoint again")
        again = client.get("/api/org/settings")
        assert again.status_code == 200
        assert again.json()["endpoints"][0]["model_names"] == ["gpt-4.1", "gpt-4.1-mini"]

    agent = client.post(
        "/api/org/agents",
        json={"name": "Builder", "kind": "lbw", "model_id": model["id"]},
    )
    assert agent.status_code == 200
    worker = agent.json()["agents"][0]
    assert worker["id"].startswith("agent_")
    assert worker["human_review"] is True
    assert any(item["id"] == "claude_code" for item in agent.json()["agent_kinds"])

    task_id = _task(tmp_path, project["id"], owner_id, tenant_id)
    refused = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert refused.status_code == 403

    for path, key, prefix in (
        (f"/api/org/models/{model['id']}/grants", "model_grants", "mgr_"),
        (f"/api/org/agents/{worker['id']}/grants", "agent_grants", "agr_"),
    ):
        granted = client.put(
            path,
            json={"scope_type": "identity", "scope_id": grace["id"], "granted": True},
        )
        assert granted.status_code == 200
        assert granted.json()[key][0]["id"].startswith(prefix)

    still = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert still.status_code == 403

    _become(tmp_path, grace["id"])
    closed = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert closed.status_code == 403
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace["id"], "access": "use"},
    )
    _become(tmp_path, owner_id)
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace["id"], "access": "use"},
    )
    turned = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
    assert turned.status_code == 200
    _become(tmp_path, grace["id"])
    attached = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert attached.status_code == 200
    assert attached.json()["model_id"] == model["id"]
    assert attached.json()["reviewer_id"] == ""

    with database(tmp_path) as db:
        with pytest.raises(PermissionError):
            touch_task(
                db,
                task_id=task_id,
                tenant_id=tenant_id,
                status="success",
                branch=None,
                pr_url=None,
                run_ref=None,
            )
    reviewed = client.post(f"/api/tasks/{task_id}/review")
    assert reviewed.status_code == 200
    assert reviewed.json()["reviewer_id"] == grace["id"]
    with database(tmp_path) as db:
        touch_task(
            db,
            task_id=task_id,
            tenant_id=tenant_id,
            status="success",
            branch=None,
            pr_url=None,
            run_ref=None,
        )

    _become(tmp_path, owner_id)
    team = client.post("/api/directory/teams", json={"name": "Platform"})
    assert team.status_code == 200
    team_id = team.json()["id"]
    assert (
        client.post(
            f"/api/directory/teams/{team_id}/members",
            json={"identity_id": grace["id"], "rank": "member"},
        ).status_code
        == 200
    )
    for path in (
        f"/api/org/models/{model['id']}/grants",
        f"/api/org/agents/{worker['id']}/grants",
    ):
        removed = client.put(
            path,
            json={"scope_type": "identity", "scope_id": grace["id"], "granted": False},
        )
        assert removed.status_code == 200
        added = client.put(
            path,
            json={"scope_type": "team", "scope_id": team_id, "granted": True},
        )
        assert added.status_code == 200
    assert (
        client.post(
            f"/api/directory/projects/{project['id']}/grants",
            json={"subject_type": "team", "subject_id": team_id, "access": "use"},
        ).status_code
        == 200
    )
    _become(tmp_path, grace["id"])
    team_task = _task(tmp_path, project["id"], grace["id"], tenant_id)
    by_team = client.post(f"/api/tasks/{team_task}/builder", json={"agent_id": worker["id"]})
    assert by_team.status_code == 200

    blocked = client.post(
        "/api/org/endpoints",
        json={"name": "Other", "provider": "openai", "api_key": secret},
    )
    assert blocked.status_code == 403


def test_the_proposal_chooses_a_granted_model(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.runtime.tasks import CycleResult

    def runner(task):
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

    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        page = client.get("/")
        assert 'id="base-url"' not in page.text
        assert 'id="load-models"' not in page.text
        assert 'id:"add-org-endpoint"' in client.get("/static/app/app.js").text
        client.get("/api/directory")
        client.post("/api/directory/promote")
        account = client.get("/api/account").json()
        with database(tmp_path) as db:
            project_id = ensure_workspace_project(db, account["org"]["id"], tmp_path)["id"]
        with patch(
            "agent_lbw.runtime.store.catalog._fetch_names",
            return_value=["gpt-4.1"],
        ):
            saved = client.post(
                "/api/org/endpoints",
                json={"name": "Work", "provider": "openai", "api_key": "sk-test-secret"},
            )
        assert saved.status_code == 200
        endpoint = saved.json()["endpoints"][0]
        added = client.post(
            "/api/org/models",
            json={"endpoint_id": endpoint["id"], "model_name": "gpt-4.1", "name": "Work"},
        )
        assert added.status_code == 200
        model = added.json()["models"][0]
        empty = client.get("/api/org/models", params={"workspace": str(tmp_path)})
        assert empty.status_code == 200
        assert empty.json()["models"] == []
        refused = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship", "model_id": model["id"]},
        )
        assert refused.status_code == 403
        turned = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
        assert turned.status_code == 200
        listed = client.get("/api/org/models", params={"workspace": str(tmp_path)})
        assert listed.status_code == 200
        assert listed.json()["models"] == [
            {
                "id": model["id"],
                "name": "Work",
                "provider": "openai",
                "model_name": "gpt-4.1",
                "endpoint_name": "Work",
            }
        ]
        assert "sk-test-secret" not in listed.text
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Ship",
                "model_id": model["id"],
                "base_url": "http://evil.example/v1",
                "model": "other",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["model"] == "gpt-4.1"
        assert body["base_url"] == "https://api.openai.com/v1"
        assert "sk-test-secret" not in created.text
        assert not (tmp_path / ".lbw" / "servers.json").exists()
        off = client.put(
            "/api/workspaces/models",
            json={"path": str(tmp_path), "model_id": model["id"], "enabled": False},
        )
        assert off.status_code == 200
        assert off.json()["can_manage"] is True
        assert off.json()["models"][0]["enabled"] is False
        assert off.json()["models"][0]["org_enabled"] is True
        hidden = client.get("/api/org/models", params={"workspace": str(tmp_path)})
        assert hidden.json()["models"] == []
        org_off = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": False})
        assert org_off.status_code == 200
        kept = client.get("/api/workspaces/models", params={"path": str(tmp_path)})
        assert kept.json()["models"][0]["enabled"] is False
        assert kept.json()["models"][0]["org_enabled"] is False
        org_on = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
        assert org_on.status_code == 200
        still = client.get("/api/workspaces/models", params={"path": str(tmp_path)})
        assert still.json()["models"][0]["enabled"] is False
        assert still.json()["models"][0]["org_enabled"] is True
        assert (
            client.get("/api/org/models", params={"workspace": str(tmp_path)}).json()["models"]
            == []
        )
        back = client.put(
            "/api/workspaces/models",
            json={"path": str(tmp_path), "model_id": model["id"], "enabled": True},
        )
        assert back.status_code == 200
        assert back.json()["models"][0]["enabled"] is True
        shown = client.get("/api/org/models", params={"workspace": str(tmp_path)})
        assert shown.json()["models"][0]["id"] == model["id"]
        added_user = client.post("/api/account/users", json={"name": "Grace"})
        grace = next(user for user in added_user.json()["users"] if user["name"] == "Grace")
        _become(tmp_path, grace["id"])
        blocked_change = client.put(
            "/api/workspaces/models",
            json={"path": str(tmp_path), "model_id": model["id"], "enabled": False},
        )
        assert blocked_change.status_code == 403
        _become(tmp_path, account["current_user_id"])
        assert (
            client.post(
                f"/api/directory/projects/{project_id}/grants",
                json={"subject_type": "identity", "subject_id": grace["id"], "access": "manage"},
            ).status_code
            == 200
        )
        _become(tmp_path, grace["id"])
        managed = client.put(
            "/api/workspaces/models",
            json={"path": str(tmp_path), "model_id": model["id"], "enabled": False},
        )
        assert managed.status_code == 200
        assert managed.json()["models"][0]["enabled"] is False
    finally:
        manager.shutdown(wait=False)


class _Names:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_an_ollama_endpoint_lists_model_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, headers, timeout, follow_redirects):
        assert url == "http://127.0.0.1:11434/v1/models"
        assert follow_redirects is False
        assert timeout == 8.0
        assert "Authorization" not in headers
        return _Names(200, {"data": [{"id": "qwen2.5-coder:14b"}, {"id": "qwen2.5-coder:14b"}]})

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)
    assert _fetch_names("ollama", "http://127.0.0.1:11434/v1", "") == ["qwen2.5-coder:14b"]


def test_a_refused_endpoint_does_not_invent_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.store.catalog.httpx.get",
        lambda *args, **kwargs: _Names(401, {}),
    )
    with pytest.raises(ValueError, match="refused"):
        _fetch_names("openai", "https://api.openai.com/v1", "sk-test-secret")


def test_turning_a_model_and_agent_on_makes_them_available(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, account["org"]["id"], tmp_path)
    with patch("agent_lbw.runtime.store.catalog._fetch_names", return_value=["gpt-4.1"]):
        saved = client.post(
            "/api/org/endpoints",
            json={"name": "Work", "provider": "openai", "api_key": "sk-test-secret"},
        )
    assert saved.status_code == 200
    endpoint = saved.json()["endpoints"][0]
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": "gpt-4.1", "name": "Work"},
    )
    model = added.json()["models"][0]
    assert model["enabled"] is False
    agent = client.post(
        "/api/org/agents",
        json={"name": "Builder", "kind": "lbw", "model_id": model["id"]},
    )
    worker = agent.json()["agents"][0]
    task_id = _task(tmp_path, project["id"], account["current_user_id"], account["org"]["id"])
    refused = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert refused.status_code == 403
    turned_on = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
    assert turned_on.status_code == 200
    assert turned_on.json()["models"][0]["enabled"] is True
    still = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert still.status_code == 403
    agents = client.put(f"/api/org/agents/{worker['id']}/enabled", json={"enabled": True})
    assert agents.status_code == 200
    assert agents.json()["agents"][0]["enabled"] is True
    attached = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert attached.status_code == 200
    removed = client.delete(f"/api/org/agents/{worker['id']}")
    assert removed.status_code == 200
    assert removed.json()["agents"] == []


def test_a_github_repository_shows_organization_models(settings: Settings, tmp_path: Path) -> None:
    import subprocess

    from agent_lbw.runtime.store.projects import ensure_project

    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    checkout = tmp_path / "widget"
    checkout.mkdir()
    subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/widget.git"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    with database(tmp_path) as db:
        ensure_project(
            db,
            account["org"]["id"],
            name="widget",
            kind="github",
            locator="acme/widget",
        )
    with patch("agent_lbw.runtime.store.catalog._fetch_names", return_value=["gpt-4.1"]):
        saved = client.post(
            "/api/org/endpoints",
            json={"name": "Work", "provider": "openai", "api_key": "sk-test-secret"},
        )
    endpoint = saved.json()["endpoints"][0]
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": "gpt-4.1", "name": "Work"},
    )
    model = added.json()["models"][0]
    assert (
        client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True}).status_code
        == 200
    )
    listed = client.get("/api/workspaces/models", params={"path": str(checkout)})
    assert listed.status_code == 200
    assert listed.json()["models"][0]["model_name"] == "gpt-4.1"
    assert listed.json()["models"][0]["enabled"] is True
    named = client.get("/api/workspaces/models", params={"path": "acme/widget"})
    assert named.status_code == 200
    assert named.json()["models"][0]["id"] == model["id"]
