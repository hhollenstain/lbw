"""A person links an integration on their profile, and a job runs on the acting person's link."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.store.auth import current_actor
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.tasks import Task, TaskManager, _act_as, _stop_acting
from agent_lbw.web_ui import create_app


class _Names:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def _fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, headers, timeout, follow_redirects):
        if headers.get("Authorization") == "Bearer sk-bad":
            return _Names(401, {})
        return _Names(200, {"data": [{"id": "proxy-gpt"}]})

    monkeypatch.setattr("agent_lbw.runtime.store.catalog.httpx.get", fake_get)


def _personal_model(client: TestClient, tmp_path: Path) -> tuple[dict, dict, dict, str]:
    """An organization with a personal-key endpoint that Ada linked, and Grace on the project."""
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
    endpoint = next(item for item in saved.json()["endpoints"] if item["name"] == "Proxy")
    linked = client.post(
        f"/api/org/endpoints/{endpoint['id']}/credential", json={"api_key": "sk-ada"}
    )
    assert linked.status_code == 200
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": "proxy-gpt", "name": "Proxy"},
    )
    model = added.json()["models"][0]
    client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
    people = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace_id = next(user["id"] for user in people["users"] if user["name"] == "Grace")
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace_id, "access": "write"},
    )
    return account, endpoint, model, grace_id


def test_a_person_sees_links_and_unlinks_an_integration(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_models(monkeypatch)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    account, endpoint, _model, grace_id = _personal_model(client, tmp_path)

    mine = client.get("/api/me/integrations")
    assert mine.status_code == 200
    listed = {item["endpoint_id"]: item for item in mine.json()["integrations"]}
    assert listed[endpoint["id"]]["linked"] is True
    assert listed[endpoint["id"]]["linked_with"] == "key"
    assert listed[endpoint["id"]]["can_add_key"] is True
    assert listed[endpoint["id"]]["can_sign_in"] is True
    assert "sk-ada" not in mine.text

    _become(tmp_path, grace_id)
    hers = client.get("/api/me/integrations").json()["integrations"]
    assert hers[0]["linked"] is False

    _become(tmp_path, account["current_user_id"])
    removed = client.delete(f"/api/me/integrations/{endpoint['id']}")
    assert removed.status_code == 200
    assert removed.json()["integrations"][0]["linked"] is False
    with database(tmp_path) as db:
        audit = db.fetchall(
            "SELECT action, actor_id, target_id FROM audit_log WHERE action = 'integration.unlink'"
        )
    assert [(row["actor_id"], row["target_id"]) for row in audit] == [
        (account["current_user_id"], endpoint["id"])
    ]


def test_a_coworker_at_a_gate_runs_the_job_on_their_own_link(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_models(monkeypatch)
    actors: list[str] = []

    def runner(task: Task) -> CycleResult:
        actors.append(current_actor() or "")
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
            summary="planned",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="waiting for triage",
            stage="triage",
        )

    manager = TaskManager(settings, runner=runner)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        account, endpoint, model, grace_id = _personal_model(client, tmp_path)
        ada_id = account["current_user_id"]
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(tmp_path),
                "goal": "Say hello",
                "gate": "ask",
                "model_id": model["id"],
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert created.json()["model_id"] == model["id"]
        _wait(client, task_id, "waiting")

        _become(tmp_path, grace_id)
        refused = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert refused.status_code == 409
        body = refused.json()
        assert body["code"] == "link_required"
        assert body["endpoint_id"] == endpoint["id"]
        assert "link Proxy on your profile" in body["error"]

        linked = client.post(
            f"/api/org/endpoints/{endpoint['id']}/credential", json={"api_key": "sk-grace"}
        )
        assert linked.status_code == 200
        built = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert built.status_code == 200, built.text
        done = _wait(client, task_id, "success")
        assert done["actor_id"] == grace_id
        assert actors == [ada_id, grace_id]

        task = manager.get(task_id)
        assert task is not None
        acting = _act_as(task)
        try:
            assert manager._fresh_credential(task)["api_key"] == "sk-grace"
        finally:
            _stop_acting(acting)

        _become(tmp_path, ada_id)
        with database(tmp_path) as db:
            notes = db.fetchall(
                "SELECT actor_id, summary FROM audit_log WHERE action = 'task.credentials'"
            )
        assert [row["actor_id"] for row in notes] == [grace_id]
        assert "Grace's credentials run this job" in notes[0]["summary"]
        runs = client.get(
            "/api/usage", params={"scope": "org", "dataset": "runs", "group_by": "person"}
        )
        assert runs.status_code == 200, runs.text
        by_person = {row["person"]: row["runs"] for row in runs.json()["rows"]}
        assert by_person == {ada_id: 1, grace_id: 1}
        assert runs.json()["labels"]["person"][grace_id] == "Grace"
    finally:
        manager.shutdown(wait=False)


def _wait(client: TestClient, task_id: str, status: str) -> dict:
    for _ in range(100):
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task never reached {status}: {body['status']}")
