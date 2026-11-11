"""The audit log notes who changed the organization and who acted on a job."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.audit import gates_for_tasks, list_audit, note_gate, record_audit
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def test_a_note_names_the_actor_and_a_gate_keeps_the_send_back(tmp_path: Path) -> None:
    with database(tmp_path) as db:
        db.execute(
            """
                INSERT INTO identities (
                    id, display_name, crossing, failed_attempts, locked, created
                ) VALUES ('idn_ada', 'Ada', 0, 0, 0, 1),
                         ('idn_grace', 'Grace', 0, 0, 0, 1)
                """
        )
        db.execute(
            """
            INSERT INTO tenants (id, kind, name, slug, mode)
            VALUES ('ten_ada', 'personal', 'Ada', 'ada', 'local')
            """
        )
        record_audit(
            db,
            tenant_id="ten_ada",
            actor_id="idn_ada",
            action="model.add",
            summary="Added model Qwen",
            method="POST",
            path="/api/org/models",
            target_type="model",
            target_id="model_qwen",
        )
        note_gate(
            db,
            tenant_id="ten_ada",
            task_id="task_1",
            actor_id="idn_ada",
            actor_name="Ada",
            action="send_back",
            note="Cover the header.",
        )
        note_gate(
            db,
            tenant_id="ten_ada",
            task_id="task_1",
            actor_id="idn_grace",
            actor_name="Grace",
            action="rework",
            note="Tighten the lifecycle section.",
        )
        listed = list_audit(db, "ten_ada")
        gates = gates_for_tasks(db, ["task_1"])

    summaries = [item["summary"] for item in listed]
    assert "Added model Qwen" in summaries
    assert any(item["actor_name"] == "Ada" and item["action"] == "model.add" for item in listed)
    by_action = {item["action"]: item for item in gates["task_1"]}
    gate = by_action["send_back"]
    assert gate["note"] == "Cover the header."
    assert gate["actor"] == {"id": "idn_ada", "name": "Ada"}
    rework = by_action["rework"]
    assert rework["actor"]["name"] == "Grace"
    assert rework["note"] == "Tighten the lifecycle section."
    assert any("Grace requested changes" in item for item in summaries)


def test_an_owner_reads_who_added_a_person_and_a_model_and_a_member_cannot(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    assert client.get("/api/audit").json() == {"entries": []}

    client.get("/api/directory")
    client.post("/api/directory/promote")
    added = client.post("/api/account/users", json={"name": "Grace"})
    assert added.status_code == 200
    grace = next(user for user in added.json()["users"] if user["name"] == "Grace")

    with patch(
        "agent_lbw.runtime.store.catalog._fetch_names",
        return_value=["qwen"],
    ):
        saved = client.post(
            "/api/org/endpoints",
            json={"name": "Work", "provider": "openai", "api_key": "sk-test-secret"},
        )
        assert saved.status_code == 200
        endpoint = saved.json()["endpoints"][0]
        modeled = client.post(
            "/api/org/models",
            json={"endpoint_id": endpoint["id"], "model_name": "qwen", "name": "Qwen"},
        )
        assert modeled.status_code == 200

    body = client.get("/api/audit")
    assert body.status_code == 200
    text = body.text
    assert "sk-test-secret" not in text
    summaries = [item["summary"] for item in body.json()["entries"]]
    assert "Added Grace" in summaries
    assert "Added model Qwen" in summaries

    _become(tmp_path, grace["id"])
    refused = client.get("/api/audit")
    assert refused.status_code == 403
    assert refused.json()["error"] == "an owner or admin reads the audit log"
