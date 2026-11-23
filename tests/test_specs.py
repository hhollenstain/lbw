"""Spec-driven proposals: write a spec, pause for a person, then build to it."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.llm.client import AssistantTurn, ScriptedClient, ToolCall
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult, SpecRequest, run_cycle
from agent_lbw.runtime.spec import acceptance_criteria, clean_spec, save_spec, spec_path_for
from agent_lbw.runtime.store.recording import open_job
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

SPEC = """# Adder adds

## Problem
`add` raises instead of adding.

## Requirements
- R1: `add(2, 3)` returns 5.

## Acceptance criteria
- [ ] `python3 check.py` exits 0 (R1)

## Out of scope
- Subtraction.

## Open questions
- None
"""


def _broken_adder(root: Path) -> None:
    (root / "app.py").write_text(
        "def add(left, right):\n    raise NotImplementedError\n", encoding="utf-8"
    )
    (root / "check.py").write_text("from app import add\nassert add(2, 3) == 5\n", encoding="utf-8")
    lbw = root / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "max_repair_attempts: 0\n"
        "publish:\n  enabled: false\n",
        encoding="utf-8",
    )
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


def test_spec_helpers_name_the_file_and_read_the_criteria(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    day = datetime(2026, 11, 21, tzinfo=UTC)
    assert spec_path_for("Add a logout button!\nMore detail", now=day) == (
        "specs/2026-11-21-add-a-logout-button.md"
    )
    assert spec_path_for("", now=day) == "specs/2026-11-21-change.md"
    assert acceptance_criteria(SPEC) == ["`python3 check.py` exits 0 (R1)"]
    assert clean_spec(f"```markdown\n{SPEC}\n```") == SPEC.strip()
    saved = save_spec(tmp_path, "specs/one.md", SPEC)
    assert saved.read_text(encoding="utf-8") == SPEC.strip() + "\n"
    for outside in ("README.md", "specs/../app.py", "/tmp/spec.md"):
        with pytest.raises(ValueError):
            save_spec(tmp_path, outside, SPEC)


def test_a_spec_driven_proposal_pauses_before_planning(tmp_path: Path, settings: Settings) -> None:
    _broken_adder(tmp_path)
    llm = ScriptedClient([AssistantTurn(text=SPEC)])
    held = run_cycle(tmp_path, settings, llm, goal="Make the adder work", spec=SpecRequest())
    assert held.status == "waiting", held.reason
    assert held.stage == "spec"
    assert held.spec_text == SPEC.strip()
    assert held.spec_path.startswith("specs/") and held.spec_path.endswith("make-the-adder-work.md")
    assert held.criteria == ["`python3 check.py` exits 0 (R1)"]
    assert len(llm.calls) == 1
    system = llm.calls[0][0]["content"]
    assert "## Acceptance criteria" in system
    assert "Make the adder work" in llm.calls[0][1]["content"]
    assert not (tmp_path / "specs").exists()
    assert "raise NotImplementedError" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_a_sent_back_spec_is_rewritten_from_the_note(tmp_path: Path, settings: Settings) -> None:
    _broken_adder(tmp_path)
    revised = SPEC.replace("- Subtraction.", "- Subtraction.\n- Floats.")
    llm = ScriptedClient([AssistantTurn(text=revised)])
    held = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Make the adder work",
        decision="send_back",
        review_notes="Say floats are out of scope",
        spec=SpecRequest(path="specs/adder.md", text=SPEC, accepted=False),
    )
    assert held.stage == "spec"
    assert held.spec_path == "specs/adder.md"
    assert "Floats" in held.spec_text
    asked = llm.calls[0][1]["content"]
    assert "Say floats are out of scope" in asked
    assert "Your previous spec" in asked


def test_an_accepted_spec_is_built_to_and_committed(tmp_path: Path, settings: Settings) -> None:
    _broken_adder(tmp_path)
    llm = ScriptedClient(_fix_turns())
    done = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Make the adder work",
        decision="accept_spec",
        spec=SpecRequest(path="specs/adder.md", text=SPEC, accepted=True),
    )
    assert done.status == "success", done.reason
    assert done.committed is True
    assert "`python3 check.py` exits 0 (R1)" in done.criteria
    assert "Spec (specs/adder.md)" in llm.calls[0][1]["content"]
    shipped = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "specs/adder.md" in shipped
    assert "app.py" in shipped


def test_a_spec_alone_never_ships(tmp_path: Path, settings: Settings) -> None:
    _broken_adder(tmp_path)
    (tmp_path / "app.py").write_text("def add(left, right):\n    return left + right\n")
    subprocess.run(["git", "commit", "-qam", "fixed"], cwd=tmp_path, check=True)
    llm = ScriptedClient(
        [
            AssistantTurn(text="The adder already works."),
            AssistantTurn(text='{"met": true, "reason": "add(2, 3) is 5"}'),
        ]
    )
    done = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Make the adder work",
        spec=SpecRequest(path="specs/adder.md", text=SPEC, accepted=True),
    )
    assert done.committed is False, done.reason
    assert not (tmp_path / "specs" / "adder.md").exists()


def _stub(task) -> CycleResult:
    """Wait at the spec gate until a person accepts the spec, then succeed."""
    if task.spec and not task.spec_accepted:
        return CycleResult(
            status="waiting",
            goal=task.goal,
            summary="Spec ready",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="waiting for spec review",
            stage="spec",
            spec_path="specs/ship.md",
            spec_text=SPEC,
        )
    return CycleResult(
        status="success",
        goal=task.goal,
        summary="built",
        tests_passed=True,
        committed=True,
        published=False,
        commit_sha=None,
        reason="completed",
        stage="done",
    )


def _settle(client: TestClient, task_id: str) -> dict:
    body: dict = {}
    for _ in range(200):
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] not in {"queued", "running"}:
            return body
        time.sleep(0.02)
    return body


def test_an_organization_or_project_can_require_a_spec(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, runner=_stub)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        client.get("/api/directory")
        client.post("/api/directory/promote")
        added = client.post("/api/account/users", json={"name": "Grace"})
        grace = next(user for user in added.json()["users"] if user["name"] == "Grace")
        account = client.get("/api/account").json()
        owner_id = account["current_user_id"]
        with database(tmp_path) as db:
            project = ensure_workspace_project(db, account["org"]["id"], tmp_path)
        path = str(tmp_path)

        optional = client.get("/api/workspaces/specs", params={"path": path}).json()
        assert optional["required"] is False and optional["can_manage"] is True
        plain = client.post("/api/tasks", json={"workspace": path, "goal": "Ship"})
        assert plain.status_code == 201 and plain.json()["spec"] is False
        _settle(client, plain.json()["id"])

        org = client.put("/api/org/specs", json={"required": True})
        assert org.status_code == 200 and org.json()["required"] is True
        forced = client.get("/api/workspaces/specs", params={"path": path}).json()
        assert forced["required"] is True and forced["org_required"] is True
        held = client.post("/api/tasks", json={"workspace": path, "goal": "Ship", "spec": False})
        assert held.json()["spec"] is True
        assert _settle(client, held.json()["id"])["stage"] == "spec"

        client.put("/api/org/specs", json={"required": False})
        project_on = client.put("/api/workspaces/specs", json={"path": path, "required": True})
        assert project_on.json()["project_required"] is True
        assert project_on.json()["org_required"] is False
        assert client.post("/api/tasks", json={"workspace": path, "goal": "Ship"}).json()["spec"]

        client.post(
            f"/api/directory/projects/{project['id']}/grants",
            json={"subject_type": "identity", "subject_id": grace["id"], "access": "write"},
        )
        with database(tmp_path) as db:
            db.execute("UPDATE install SET current_identity_id = ?", (grace["id"],))
        refused = client.put("/api/workspaces/specs", json={"path": path, "required": False})
        assert refused.status_code == 403
        assert client.put("/api/org/specs", json={"required": True}).status_code == 403
        seen = client.get("/api/workspaces/specs", params={"path": path}).json()
        assert seen["required"] is True and seen["can_manage"] is False
        with database(tmp_path) as db:
            db.execute("UPDATE install SET current_identity_id = ?", (owner_id,))
    finally:
        manager.shutdown(wait=False)


def test_a_person_accepts_an_edited_spec_and_the_job_builds(
    settings: Settings, tmp_path: Path
) -> None:
    manager = TaskManager(settings, runner=_stub)
    client = TestClient(create_app(manager, default_workspace=tmp_path))
    try:
        created = client.post(
            "/api/tasks", json={"workspace": str(tmp_path), "goal": "Ship", "spec": True}
        )
        task_id = created.json()["id"]
        held = _settle(client, task_id)
        assert held["status"] == "waiting" and held["stage"] == "spec"
        assert held["spec_text"] == SPEC and held["spec_path"] == "specs/ship.md"

        for _ in range(100):
            stored = open_job(tmp_path, settings.database_url, task_id) or {}
            if (stored.get("spec") or {}).get("on"):
                break
            time.sleep(0.05)
        restarted = TaskManager(settings, runner=_stub)
        restarted.store_root = tmp_path
        try:
            again = restarted.adopt_job(task_id)
            assert again is not None
            assert (again.spec, again.spec_accepted, again.spec_path) == (
                True,
                False,
                "specs/ship.md",
            )
            assert again.spec_text == SPEC
            assert (again.stage, again.triage_gate) == ("spec", "auto")
        finally:
            restarted.shutdown(wait=False)

        wrong = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert wrong.status_code == 409
        bare = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "send_back"})
        assert bare.status_code == 400

        edited = SPEC.replace("- None", "- Ship on Friday?")
        accepted = client.post(
            f"/api/tasks/{task_id}/decide",
            json={"decision": "accept_spec", "spec_text": edited},
        )
        assert accepted.status_code == 200, accepted.text
        done = _settle(client, task_id)
        assert done["status"] == "success"
        assert done["spec_accepted"] is True
        assert "Ship on Friday?" in done["spec_text"]
        assert done["spec_path"] == "specs/ship.md"
    finally:
        manager.shutdown(wait=False)
