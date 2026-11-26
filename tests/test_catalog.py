"""An organization brings its own models and grants them before an agent can build."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.catalog import _fetch_names, save_agent
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
        'id:"open-org-model"',
        'id:"endpoint-remove-dialog"',
        'id:"confirm-endpoint-remove"',
        '"data-model-enable"',
        '"data-project-model"',
    ):
        assert token in script
    assert 'id="agent"' not in page.text
    assert 'id:"org-agents"' not in script
    assert 'id:"open-org-agent"' not in script
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
            "cursor",
            "litellm",
            "gateway",
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

    refused_agent = client.post(
        "/api/org/agents",
        json={"name": "Builder", "kind": "lbw", "model_id": model["id"]},
    )
    assert refused_agent.status_code == 400
    assert "model" in refused_agent.json()["error"]
    with database(tmp_path) as db:
        stored = save_agent(db, name="Builder", kind="lbw", model_id=model["id"])
    worker = stored["agents"][0]
    assert worker["id"].startswith("agent_")
    assert worker["human_review"] is True

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
    # Reading the project is not enough to put an agent to work on it.
    reader = client.post(f"/api/tasks/{task_id}/builder", json={"agent_id": worker["id"]})
    assert reader.status_code == 403
    _become(tmp_path, owner_id)
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace["id"], "access": "write"},
    )
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
                "runner": False,
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


def test_a_claude_endpoint_asks_anthropic_for_model_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, headers, timeout, follow_redirects):
        assert url == "https://api.anthropic.com/v1/models?limit=100"
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers
        assert timeout == 8.0
        assert follow_redirects is False
        return _Names(200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-5"}]})

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)
    assert _fetch_names("anthropic", "https://api.anthropic.com/v1", "sk-ant-test") == [
        "claude-sonnet-4-5",
        "claude-opus-4-5",
    ]


def test_a_cursor_endpoint_lists_account_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, headers, timeout, follow_redirects):
        assert url == "https://api.cursor.com/v1/models"
        assert headers["Authorization"] == "Bearer cursor_test"
        assert timeout == 8.0
        assert follow_redirects is False
        return _Names(
            200,
            {
                "items": [
                    {"id": "grok-4.5", "displayName": "Grok 4.5"},
                    {"id": "composer-2.5", "displayName": "Composer 2.5"},
                    {"displayName": "Missing id"},
                ]
            },
        )

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)
    assert _fetch_names("cursor", "https://api.cursor.com/v1", "cursor_test") == [
        "grok-4.5",
        "composer-2.5",
    ]


def test_an_existing_model_table_accepts_cursor(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / ".lbw" / "lbw.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE model_endpoints (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            provider TEXT NOT NULL CHECK (provider IN (
                'openai', 'anthropic', 'google', 'ollama', 'vllm', 'lmstudio',
                'openai_compatible'
            )),
            base_url TEXT NOT NULL DEFAULT '',
            api_key_ciphertext TEXT,
            models_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    raw.execute(
        """
        INSERT INTO model_endpoints (
            id, tenant_id, name, provider, base_url, models_json
        ) VALUES ('ep_old', 'ten_old', 'Desk', 'ollama', 'http://127.0.0.1:11434/v1', '[]')
        """
    )
    raw.commit()
    raw.close()
    with database(tmp_path) as db:
        db.execute(
            """
            INSERT INTO tenants (id, kind, name, slug, mode)
            VALUES ('ten_old', 'personal', 'Ada', 'ada', 'local')
            """
        )
        db.execute(
            """
            INSERT INTO model_endpoints (
                id, tenant_id, name, provider, base_url, models_json
            ) VALUES (?, ?, ?, ?, ?, '[]')
            """,
            ("ep_cursor", "ten_old", "Cursor", "cursor", "https://api.cursor.com/v1"),
        )
        db.execute(
            """
            INSERT INTO model_endpoints (
                id, tenant_id, name, provider, base_url, models_json
            ) VALUES (?, ?, ?, ?, ?, '[]')
            """,
            ("ep_litellm", "ten_old", "Proxy", "litellm", "http://127.0.0.1:4000/v1"),
        )
        kept = db.fetchone("SELECT provider FROM model_endpoints WHERE id = 'ep_old'")
    assert kept is not None
    assert kept["provider"] == "ollama"


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
    with database(tmp_path) as db:
        stored = save_agent(db, name="Builder", kind="lbw", model_id=model["id"])
    worker = stored["agents"][0]
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


def test_a_litellm_gateway_uses_a_virtual_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, headers, timeout, follow_redirects):
        assert url == "http://127.0.0.1:4000/v1/models"
        assert headers["Authorization"] == "Bearer sk-proxy"
        assert timeout == 8.0
        assert follow_redirects is False
        return _Names(200, {"data": [{"id": "proxy-gpt"}]})

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)
    assert _fetch_names("litellm", "http://127.0.0.1:4000/v1", "sk-proxy") == ["proxy-gpt"]


def test_a_gateway_keeps_sso_until_the_person_signs_in(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.runtime.store.catalog import use_model
    from agent_lbw.runtime.store.gateway import gateway_bearer, remember_gateway_login

    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    with database(tmp_path) as db:
        ensure_workspace_project(db, account["org"]["id"], tmp_path)

    def fail_get(*_args, **_kwargs):
        raise AssertionError("a gateway SSO save does not call the gateway")

    with patch("agent_lbw.runtime.store.catalog.httpx.get", fail_get):
        waiting = client.post(
            "/api/org/endpoints",
            json={
                "name": "Proxy",
                "provider": "litellm",
                "base_url": "http://127.0.0.1:4000",
                "auth": "sso",
                "audience": "litellm-proxy",
            },
        )
    assert waiting.status_code == 200
    endpoint = waiting.json()["endpoints"][0]
    assert endpoint["auth"] == "sso"
    assert endpoint["signed_in"] is False
    assert endpoint["has_api_key"] is False
    assert endpoint["base_url"] == "http://127.0.0.1:4000/v1"
    assert "litellm-proxy" not in waiting.text
    hosted = client.post(
        "/api/org/endpoints",
        json={"name": "Open", "provider": "openai", "api_key": "sk-test", "auth": "sso"},
    )
    assert hosted.status_code == 400
    assert hosted.json()["error"] == "only a gateway can use SSO"
    missing = client.post(
        "/api/org/endpoints",
        json={"name": "Bare", "provider": "gateway", "base_url": ""},
    )
    assert missing.status_code == 400
    assert missing.json()["error"] == "a gateway needs a base URL"
    password = client.get(
        f"/api/org/endpoints/{endpoint['id']}/sso",
        follow_redirects=False,
    )
    assert password.status_code == 400
    assert password.json()["error"] == "this organization uses a password"
    with database(tmp_path) as db:
        db.execute(
            """
            INSERT INTO sign_in (
                tenant_id, provider, issuer, metadata_url, client_id, client_secret_ciphertext
            ) VALUES (?, 'saml', '', '', '', '')
            ON CONFLICT (tenant_id) DO UPDATE SET provider = 'saml'
            """,
            (account["org"]["id"],),
        )
    saml = client.get(f"/api/org/endpoints/{endpoint['id']}/sso", follow_redirects=False)
    assert saml.status_code == 400
    assert saml.json()["error"] == "a gateway sign-in needs Entra or Auth0"

    with patch("agent_lbw.runtime.store.catalog._fetch_names", return_value=["proxy-gpt"]):
        keyed = client.post(
            "/api/org/endpoints",
            json={
                "name": "Keyed",
                "provider": "gateway",
                "base_url": "http://127.0.0.1:4000/v1",
                "api_key": "sk-proxy",
            },
        )
    assert keyed.status_code == 200
    keyed_endpoint = next(item for item in keyed.json()["endpoints"] if item["name"] == "Keyed")
    assert keyed_endpoint["model_names"] == ["proxy-gpt"]
    assert keyed_endpoint["auth"] == "key"
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": keyed_endpoint["id"], "model_name": "proxy-gpt", "name": "Proxy"},
    )
    model = added.json()["models"][0]
    assert (
        client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True}).status_code
        == 200
    )
    with database(tmp_path) as db:
        db.execute(
            """
            UPDATE model_endpoints
            SET auth = 'sso', api_key_ciphertext = NULL
            WHERE id = ?
            """,
            (keyed_endpoint["id"],),
        )
        remember_gateway_login(
            db,
            tmp_path,
            keyed_endpoint["id"],
            access_token="person-token",
            refresh_token="refresh-token",
            expires_in=30,
        )
        renewed = {
            "access_token": "person-token-2",
            "refresh_token": "refresh-token-2",
            "expires_in": 3600,
        }
        with patch("agent_lbw.runtime.store.gateway.refresh_access", return_value=renewed):
            assert gateway_bearer(db, tmp_path, keyed_endpoint["id"]) == "person-token-2"
        used = use_model(db, tmp_path, tmp_path, model["id"])
    assert used["api_key"] == "person-token-2"
    shown = client.get("/api/org/settings")
    assert shown.status_code == 200
    assert "person-token" not in shown.text
    listed = next(item for item in shown.json()["endpoints"] if item["id"] == keyed_endpoint["id"])
    assert listed["signed_in"] is True
    assert listed["auth"] == "sso"


def test_each_person_checks_their_own_key_and_a_run_uses_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.runtime.store.catalog import use_model

    def fake_get(url, headers, timeout, follow_redirects):
        assert url == "http://127.0.0.1:4000/v1/models"
        assert timeout == 8.0
        assert follow_redirects is False
        if headers.get("Authorization") == "Bearer sk-bad":
            return _Names(401, {})
        return _Names(200, {"data": [{"id": "proxy-gpt"}]})

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, account["org"]["id"], tmp_path)
    saved = client.post(
        "/api/org/endpoints",
        json={
            "name": "Proxy",
            "provider": "litellm",
            "base_url": "http://127.0.0.1:4000",
            "auth": "personal",
        },
    )
    assert saved.status_code == 200
    endpoint = next(item for item in saved.json()["endpoints"] if item["name"] == "Proxy")
    assert endpoint["auth"] == "personal"
    assert endpoint["signed_in"] is False
    assert endpoint["has_api_key"] is False
    refused = client.post(
        f"/api/org/endpoints/{endpoint['id']}/credential",
        json={"api_key": "sk-bad"},
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == "that endpoint refused the credentials"
    checked = client.post(
        f"/api/org/endpoints/{endpoint['id']}/credential",
        json={"api_key": "sk-ada"},
    )
    assert checked.status_code == 200
    assert "sk-ada" not in checked.text
    mine = next(item for item in checked.json()["endpoints"] if item["id"] == endpoint["id"])
    assert mine["signed_in"] is True
    assert mine["model_names"] == ["proxy-gpt"]
    assert mine["has_api_key"] is False
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": "proxy-gpt", "name": "Proxy"},
    )
    model = added.json()["models"][0]
    assert (
        client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True}).status_code
        == 200
    )
    grace = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace_id = next(user["id"] for user in grace["users"] if user["name"] == "Grace")
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace_id, "access": "use"},
    )
    _become(tmp_path, grace_id)
    with database(tmp_path) as db:
        with pytest.raises(PermissionError, match="add your API key"):
            use_model(db, tmp_path, tmp_path, model["id"])
    waiting = client.get("/api/workspaces/models", params={"path": str(tmp_path)})
    assert waiting.json()["models"][0]["signed_in"] is False
    hers = client.post(
        f"/api/org/endpoints/{endpoint['id']}/credential",
        json={"api_key": "sk-grace"},
    )
    assert hers.status_code == 200
    assert "sk-grace" not in hers.text
    ready = client.get("/api/workspaces/models", params={"path": str(tmp_path)})
    assert ready.json()["models"][0]["signed_in"] is True
    with database(tmp_path) as db:
        assert use_model(db, tmp_path, tmp_path, model["id"])["api_key"] == "sk-grace"
        row = db.fetchone(
            "SELECT api_key_ciphertext FROM model_endpoints WHERE id = ?",
            (endpoint["id"],),
        )
    assert row is not None
    assert row["api_key_ciphertext"] is None
    _become(tmp_path, account["current_user_id"])
    with database(tmp_path) as db:
        assert use_model(db, tmp_path, tmp_path, model["id"])["api_key"] == "sk-ada"
    shared = client.post(
        "/api/org/endpoints",
        json={
            "name": "Shared",
            "provider": "litellm",
            "base_url": "http://127.0.0.1:4000/v1",
            "api_key": "sk-org",
        },
    )
    shared_id = next(item["id"] for item in shared.json()["endpoints"] if item["name"] == "Shared")
    blocked = client.post(
        f"/api/org/endpoints/{shared_id}/credential",
        json={"api_key": "sk-ada"},
    )
    assert blocked.status_code == 400
    assert blocked.json()["error"] == "this endpoint uses one organization key"


def test_a_rerun_runs_on_the_model_the_person_picks(settings: Settings, tmp_path: Path) -> None:
    from agent_lbw.runtime.tasks import CycleResult

    def runner(task):
        return CycleResult(
            status="failed",
            goal=task.goal,
            summary="tests failed",
            tests_passed=False,
            committed=False,
            published=False,
            commit_sha=None,
            reason="tests failed",
        )

    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        client.get("/api/directory")
        client.post("/api/directory/promote")
        account = client.get("/api/account").json()
        with database(tmp_path) as db:
            ensure_workspace_project(db, account["org"]["id"], tmp_path)
        with patch(
            "agent_lbw.runtime.store.catalog._fetch_names",
            return_value=["gpt-4.1", "gpt-4.1-mini"],
        ):
            saved = client.post(
                "/api/org/endpoints",
                json={"name": "Work", "provider": "openai", "api_key": "sk-test-secret"},
            )
        endpoint = saved.json()["endpoints"][0]
        ids = {}
        for name, model_name in (("Big", "gpt-4.1"), ("Small", "gpt-4.1-mini")):
            added = client.post(
                "/api/org/models",
                json={"endpoint_id": endpoint["id"], "model_name": model_name, "name": name},
            )
            ids[name] = next(m["id"] for m in added.json()["models"] if m["name"] == name)
            client.put(f"/api/org/models/{ids[name]}/enabled", json={"enabled": True})

        def finished(task_id: str) -> dict:
            body: dict = {}
            for _ in range(200):
                body = client.get(f"/api/tasks/{task_id}").json()
                if body["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            return body

        created = client.post(
            "/api/tasks",
            json={"workspace": str(tmp_path), "goal": "Ship", "model_id": ids["Big"]},
        )
        assert created.status_code == 201, created.text
        assert finished(created.json()["id"])["status"] == "failed"

        picked = client.post(
            f"/api/tasks/{created.json()['id']}/rerun", json={"model_id": ids["Small"]}
        )
        assert picked.status_code == 201, picked.text
        assert picked.json()["model_id"] == ids["Small"]
        assert picked.json()["model"] == "gpt-4.1-mini"
        assert finished(picked.json()["id"])["status"] == "failed"

        kept = client.post(f"/api/tasks/{picked.json()['id']}/rerun", json={})
        assert kept.status_code == 201, kept.text
        assert kept.json()["model_id"] == ids["Small"]
        finished(kept.json()["id"])

        gone = client.post(
            f"/api/tasks/{kept.json()['id']}/rerun", json={"model_id": "model_missing"}
        )
        assert gone.status_code == 403, gone.text
        assert "sk-test-secret" not in picked.text
    finally:
        manager.shutdown(wait=False)
