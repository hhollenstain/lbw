"""Usage rollups: one write per run, bounded cached reports, and who may read them."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.store.usage import (
    build_report,
    clear_cache,
    file_delta,
    file_stats,
    record_run,
    registry,
    run_report,
    save_price,
)
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

_EVENTS = [
    {"kind": "skills", "names": ["tdd", "ui"]},
    {
        "kind": "llm",
        "purpose": "agent",
        "ok": True,
        "model_name": "claude-sonnet",
        "service": "Claude",
        "provider": "anthropic",
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "cache_read_tokens": 400_000,
        "cache_write_tokens": 100_000,
        "elapsed_ms": 2000,
    },
    {
        "kind": "llm",
        "purpose": "role",
        "role": "security",
        "ok": False,
        "model_name": "claude-sonnet",
        "service": "Claude",
        "provider": "anthropic",
    },
]
_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n-x\n+y\n+z\n"
    "diff --git a/Dockerfile b/Dockerfile\n+++ b/Dockerfile\n+RUN x\n"
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_cache()


def test_a_run_folds_into_daily_rollups_with_cache_aware_cost(tmp_path: Path) -> None:
    when = datetime(2026, 11, 20, tzinfo=UTC)
    org = load_account(tmp_path)["org"]["id"]
    with database(tmp_path) as db:
        save_price(
            db,
            org,
            provider="anthropic",
            model="claude-sonnet",
            input_per_mtok=3,
            output_per_mtok=15,
            cache_read_per_mtok=0.3,
            cache_write_per_mtok=3.75,
        )
        totals = record_run(
            db,
            tenant_id=org,
            project_id="proj_1",
            identity_id="ada",
            events=_EVENTS,
            files=file_stats(_DIFF),
            outcome="success",
            duration_ms=5000,
            when=when,
        )
        assert totals["calls"] == 2
        assert totals["errors"] == 1
        assert totals["tokens"] == 1_100_000
        # 500k fresh input at $3, 400k cache reads at $0.30, 100k cache writes at
        # $3.75, and 100k output at $15 per million tokens.
        assert totals["cost_micros"] == 3_495_000
        report = build_report(
            dataset="tokens",
            group_by=["step"],
            measures=["tokens", "cost", "errors"],
            today=date(2026, 11, 20),
        )
        first = run_report(db, org, report)
        assert first["cached"] is False
        assert {row["step"]: row["errors"] for row in first["rows"]} == {"build": 0, "security": 1}
        assert first["totals"]["cost"] == 3_495_000
        assert first["series"] == [
            {"day": "2026-11-20", "tokens": 1_100_000, "cost": 3_495_000, "errors": 1}
        ]
        assert run_report(db, org, report)["cached"] is True
        assert run_report(db, "org_2", report)["totals"]["tokens"] == 0
        files = run_report(
            db,
            org,
            build_report(dataset="files", group_by=["extension"], today=date(2026, 11, 20)),
        )
        assert {row["extension"]: row["files"] for row in files["rows"]} == {
            ".py": 1,
            "Dockerfile": 1,
        }
        skills = run_report(
            db,
            org,
            build_report(dataset="skills", group_by=["skill"], today=date(2026, 11, 20)),
        )
        assert sorted(row["skill"] for row in skills["rows"]) == ["tdd", "ui"]


def test_a_rework_counts_only_new_file_changes() -> None:
    first = file_stats(_DIFF)
    assert first[".py"] == {"files": 1, "lines_added": 2, "lines_removed": 1}
    later = file_stats(_DIFF + "diff --git a/src/b.py b/src/b.py\n+++ b/src/b.py\n+new\n")
    assert file_delta(later, first)[".py"] == {"files": 1, "lines_added": 1, "lines_removed": 0}
    assert file_delta(later, first)["Dockerfile"] == {
        "files": 0,
        "lines_added": 0,
        "lines_removed": 0,
    }


def test_a_report_stays_inside_the_registry_and_the_date_bound() -> None:
    today = date(2026, 11, 20)
    with pytest.raises(ValueError, match="unknown dataset"):
        build_report(dataset="secrets", today=today)
    with pytest.raises(ValueError, match="cannot be broken down by extension"):
        build_report(dataset="tokens", group_by=["extension"], today=today)
    with pytest.raises(ValueError, match="at most two"):
        build_report(group_by=["model", "person", "step"], today=today)
    with pytest.raises(ValueError, match="at most 366 days"):
        build_report(since="2025-01-01", until="2026-11-20", today=today)
    with pytest.raises(ValueError, match="no measure"):
        build_report(measures=["password"], today=today)
    report = build_report(today=today)
    assert (report.since, report.until) == ("2026-10-22", "2026-11-20")
    datasets = {item["key"]: item for item in registry()["datasets"]}
    assert {item["key"] for item in datasets["tokens"]["dimensions"]} >= {
        "service",
        "model",
        "project",
        "person",
        "step",
    }
    otel = {item["key"]: item["otel"] for item in datasets["tokens"]["measures"]}
    assert otel["input_tokens"] == "gen_ai.usage.input_tokens"


def test_people_see_their_own_usage_and_admins_see_the_organization(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    org = account["org"]["id"]
    ada = account["current_user_id"]
    people = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace = next(user["id"] for user in people["users"] if user["name"] == "Grace")
    with database(tmp_path) as db:
        project = ensure_workspace_project(db, org, tmp_path)
        for person in (ada, grace):
            record_run(
                db,
                tenant_id=org,
                project_id=project["id"],
                identity_id=person,
                events=_EVENTS[1:2],
                outcome="success",
            )

    meta = client.get("/api/usage/meta")
    assert meta.status_code == 200
    assert meta.json()["can_see_org"] is True
    everyone = client.get("/api/usage", params={"scope": "org", "group_by": "person"})
    assert everyone.status_code == 200
    assert everyone.headers["cache-control"] == "private, max-age=60"
    assert {row["person"] for row in everyone.json()["rows"]} == {ada, grace}
    csv = client.get("/api/usage", params={"scope": "org", "group_by": "person", "format": "csv"})
    assert csv.headers["content-type"].startswith("text/csv")
    assert "person,person_name,tokens" in csv.text
    assert "Grace" in csv.text
    priced = client.put(
        "/api/org/prices",
        json={
            "provider": "anthropic",
            "model": "claude-sonnet",
            "input_per_mtok": 3,
            "output_per_mtok": 15,
        },
    )
    assert priced.status_code == 200
    assert priced.json()["prices"][0]["output_per_mtok"] == 15

    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (grace,))
    clear_cache()
    assert client.get("/api/usage/meta").json()["can_see_org"] is False
    assert client.get("/api/usage", params={"scope": "org"}).status_code == 403
    assert (
        client.put(
            "/api/org/prices",
            json={"model": "x", "input_per_mtok": 1, "output_per_mtok": 1},
        ).status_code
        == 403
    )
    own = client.get("/api/usage", params={"scope": "me", "group_by": "model", "person": ada})
    assert own.status_code == 200
    assert own.json()["totals"]["tokens"] == 1_100_000
    assert own.json()["filters"]["person"] == grace
    closed = client.get("/api/usage", params={"scope": "project", "path": str(tmp_path)})
    assert closed.status_code == 403
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (ada,))
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace, "access": "use"},
    )
    with database(tmp_path) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (grace,))
    shared = client.get(
        "/api/usage", params={"scope": "project", "path": str(tmp_path), "group_by": "person"}
    )
    assert shared.status_code == 200, shared.text
    assert {row["person"] for row in shared.json()["rows"]} == {ada, grace}
