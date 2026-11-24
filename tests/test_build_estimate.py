"""A planned change shows what building it should cost before anyone presses Build."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.store.usage import DEFAULT_BUILD_RATIO, estimate_build, record_run
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.web_ui import create_app

_TODAY = date(2026, 11, 22)
_PLANNED = [
    {
        "kind": "llm",
        "purpose": "plan",
        "model_name": "claude-sonnet-5-5",
        "provider": "anthropic",
        "prompt_tokens": 4_000,
        "completion_tokens": 1_000,
    },
    {"kind": "llm", "purpose": "plan", "ok": False, "prompt_tokens": 99_999},
]


def _history(db, org: str, project: str) -> None:
    """One earlier change: 10k tokens to plan, 200k to build and review it."""
    record_run(
        db,
        tenant_id=org,
        project_id=project,
        identity_id="ada",
        events=[
            {"kind": "llm", "purpose": "spec", "prompt_tokens": 500_000},
            {"kind": "llm", "purpose": "plan", "prompt_tokens": 8_000, "completion_tokens": 2_000},
            {
                "kind": "llm",
                "purpose": "agent",
                "prompt_tokens": 180_000,
                "completion_tokens": 20_000,
                "cache_read_tokens": 90_000,
            },
        ],
        when=datetime(2026, 11, 1, tzinfo=UTC),
    )


def test_a_project_scales_the_plan_by_its_own_recent_builds(tmp_path: Path) -> None:
    org = load_account(tmp_path)["org"]["id"]
    with database(tmp_path) as db:
        _history(db, org, "proj_1")
        estimate = estimate_build(
            db,
            tenant_id=org,
            project_id="proj_1",
            events=_PLANNED,
            model="",
            today=_TODAY,
        )
    assert estimate["available"] is True
    assert estimate["basis"] == "project"
    assert estimate["ratio"] == 20
    assert estimate["planning_tokens"] == 5_000
    assert estimate["tokens"] == 100_000
    assert estimate["output_tokens"] == 10_000
    assert estimate["input_tokens"] == 90_000
    assert estimate["cache_read_tokens"] == 45_000
    assert estimate["model"] == "claude-sonnet-5-5"
    # 45k fresh input at $2, 45k cache reads at $0.20, and 10k output at $10 per million.
    assert estimate["cost_micros"] == 199_000
    assert estimate["price_source"] == "anthropic.com"
    assert estimate["planning_cost_micros"] == 18_000


def test_a_new_project_borrows_the_organization_then_a_rough_default(tmp_path: Path) -> None:
    org = load_account(tmp_path)["org"]["id"]
    with database(tmp_path) as db:
        rough = estimate_build(
            db, tenant_id=org, project_id="proj_2", events=_PLANNED, model="", today=_TODAY
        )
        assert rough["basis"] == "default"
        assert rough["tokens"] == 5_000 * DEFAULT_BUILD_RATIO
        assert "rough" in rough["note"]
        _history(db, org, "proj_1")
        borrowed = estimate_build(
            db, tenant_id=org, project_id="proj_2", events=_PLANNED, model="", today=_TODAY
        )
        assert borrowed["basis"] == "organization"
        assert borrowed["tokens"] == 100_000
        stale = estimate_build(
            db,
            tenant_id=org,
            project_id="proj_1",
            events=_PLANNED,
            model="",
            today=date(2027, 6, 1),
        )
        assert stale["basis"] == "default"
        unpriced = estimate_build(
            db,
            tenant_id=org,
            project_id="proj_1",
            events=[{**_PLANNED[0], "model_name": "llama3"}],
            model="",
            today=_TODAY,
        )
        assert unpriced["cost_micros"] is None
        assert "has no price" in unpriced["note"]
        nothing = estimate_build(
            db, tenant_id=org, project_id="proj_1", events=[], model="sonnet", today=_TODAY
        )
        assert nothing["available"] is False


def test_the_plan_review_reads_the_estimate_for_a_person_who_can_use_the_project(
    settings: Settings, tmp_path: Path
) -> None:
    manager = TaskManager(settings)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    org = account["org"]["id"]
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, org, tmp_path)
    task = Task(
        id="task_1",
        workspace=str(tmp_path),
        goal="Add a button",
        auto_commit=True,
        create_pr=True,
        model_name="sonnet",
        model_base_url="",
        model_api_key="",
        status="waiting",
        stage="triage",
        events=list(_PLANNED),
        tenant_id=org,
    )
    manager._tasks[task.id] = task

    reply = client.get("/api/tasks/task_1/estimate")
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["available"] is True
    assert body["basis"] == "default"
    assert body["cost_micros"] > 0
    assert client.get("/api/tasks/nope/estimate").status_code == 404

    people = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace = next(user["id"] for user in people["users"] if user["name"] == "Grace")
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (grace,))
    assert client.get("/api/tasks/task_1/estimate").status_code == 403
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (account["current_user_id"],))
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace, "access": "use"},
    )
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (grace,))
    assert client.get("/api/tasks/task_1/estimate").status_code == 200
