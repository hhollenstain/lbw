"""People see a project's jobs only with access to it, and change them only with write.

Bob has no grant, Cy can read (`use`), and Dee can write. Ada owns the
account. A job from another account is never shown, even to an owner.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult
from agent_lbw.runtime.store.projects import grant_project
from agent_lbw.runtime.store.recording import project_id_for_checkout
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.tools.base import ToolResult
from agent_lbw.web_ui import create_app


def _waiting(task: Task) -> CycleResult:
    return CycleResult(
        status="waiting",
        goal=task.goal,
        summary="Plan: say hello",
        tests_passed=None,
        committed=False,
        published=False,
        commit_sha=None,
        reason="waiting for triage",
        stage="triage",
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["remote", "add", "origin", "https://github.com/acme/demo.git"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    return path


class _Office:
    """One LBW site, its people, and a job waiting in Ada's project."""

    def __init__(self, settings: Settings, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.demo = _repo(tmp_path / "demo")
        self.merged: list[str] = []
        monkeypatch.setattr(
            "agent_lbw.web_ui.merge_pull_request",
            lambda workspace, url, squash=True, token=None: (
                self.merged.append(url) or ToolResult(True, "merged")
            ),
        )
        monkeypatch.setattr(
            "agent_lbw.web_ui.lookup_pull_state", lambda url, force=False, token=None: "open"
        )
        self.manager = TaskManager(settings, runner=_waiting)
        self.client = TestClient(create_app(self.manager, default_workspace=tmp_path))
        created = self.client.post(
            "/api/tasks", json={"workspace": str(self.demo), "goal": "Say hello", "gate": "ask"}
        )
        assert created.status_code == 201, created.text
        self.task_id = created.json()["id"]
        for _ in range(100):
            if self.client.get(f"/api/tasks/{self.task_id}").json()["status"] == "waiting":
                break
            time.sleep(0.05)
        account = self.client.get("/api/account").json()
        self.ada = account["current_user_id"]
        self.tenant = account["org"]["id"]
        self.people = {"Ada": self.ada}
        for name in ("Bob", "Cy", "Dee"):
            added = self.client.post("/api/account/users", json={"name": name}).json()
            self.people[name] = next(u["id"] for u in added["users"] if u["name"] == name)
        with database(tmp_path) as db:
            self.project = project_id_for_checkout(db, self.tenant, self.demo)
            assert self.project
            for name, access in (("Cy", "use"), ("Dee", "write")):
                grant_project(
                    db,
                    self.project,
                    subject_type="identity",
                    subject_id=self.people[name],
                    access=access,
                )

    def be(self, name: str) -> TestClient:
        with database(self.root) as db:
            db.execute("UPDATE install SET current_identity_id = ?", (self.people[name],))
        return self.client

    def listed(self, name: str) -> set[str]:
        return {item["id"] for item in self.be(name).get("/api/tasks").json()}


@pytest.fixture
def office(settings: Settings, tmp_path: Path, monkeypatch):
    built = _Office(settings, tmp_path, monkeypatch)
    yield built
    built.manager.shutdown(wait=False)


def test_without_access_a_project_and_its_jobs_are_not_there(office: _Office) -> None:
    bob = office.be("Bob")
    assert office.task_id not in office.listed("Bob")
    for path in (f"/api/tasks/{office.task_id}", f"/api/tasks/{office.task_id}/checks"):
        assert bob.get(path).status_code == 404, path
    # Changing it is refused as if it did not exist, so its existence is not confirmed.
    decided = bob.post(f"/api/tasks/{office.task_id}/decide", json={"decision": "build"})
    assert decided.status_code == 404
    assert bob.post(f"/api/tasks/{office.task_id}/stop").status_code == 404
    assert bob.post(f"/api/tasks/{office.task_id}/rerun", json={}).status_code == 404
    started = bob.post("/api/tasks", json={"workspace": str(office.demo), "goal": "Sneak in"})
    assert started.status_code == 404
    assert bob.get("/api/history", params={"workspace": str(office.demo)}).status_code == 404
    assert bob.get("/api/branches", params={"workspace": str(office.demo)}).status_code == 404
    assert bob.post("/api/workspaces/select", json={"path": str(office.demo)}).status_code == 404
    listed = bob.get("/api/workspaces").json()
    assert all(item["path"] != str(office.demo) for item in listed["workspaces"])
    assert listed["current"] != str(office.demo)
    merged = bob.post(
        "/api/pulls/merge",
        json={"url": "https://github.com/acme/demo/pull/4", "workspace": str(office.demo)},
    )
    assert merged.status_code == 404 and office.merged == []


def test_reading_a_project_does_not_let_you_change_or_merge(office: _Office) -> None:
    cy = office.be("Cy")
    assert office.task_id in office.listed("Cy")
    assert cy.get(f"/api/tasks/{office.task_id}").status_code == 200
    assert cy.get("/api/history", params={"workspace": str(office.demo)}).status_code == 200
    refused = cy.post(f"/api/tasks/{office.task_id}/decide", json={"decision": "build"})
    assert refused.status_code == 403
    assert cy.post(f"/api/tasks/{office.task_id}/stop").status_code == 403
    assert cy.post(f"/api/tasks/{office.task_id}/rerun", json={}).status_code == 403
    started = cy.post("/api/tasks", json={"workspace": str(office.demo), "goal": "Add a page"})
    assert started.status_code == 403
    merged = cy.post(
        "/api/pulls/merge",
        json={"url": "https://github.com/acme/demo/pull/4", "workspace": str(office.demo)},
    )
    assert merged.status_code == 403 and office.merged == []
    # Pull request states are looked up only for repositories Cy can read.
    states = cy.post(
        "/api/pulls/status",
        json={
            "urls": [
                "https://github.com/acme/demo/pull/4",
                "https://github.com/secret/vault/pull/9",
            ]
        },
    ).json()["states"]
    assert set(states) == {"https://github.com/acme/demo/pull/4"}


def test_write_access_merges_only_the_projects_own_pull_requests(office: _Office) -> None:
    dee = office.be("Dee")
    other = dee.post(
        "/api/pulls/merge",
        json={"url": "https://github.com/acme/billing/pull/2", "workspace": str(office.demo)},
    )
    assert other.status_code == 403 and office.merged == []
    merged = dee.post(
        "/api/pulls/merge",
        json={"url": "https://github.com/acme/demo/pull/4", "workspace": str(office.demo)},
    )
    assert merged.status_code == 200, merged.text
    assert office.merged == ["https://github.com/acme/demo/pull/4"]
    built = dee.post(f"/api/tasks/{office.task_id}/decide", json={"decision": "build"})
    assert built.status_code == 200, built.text


def test_a_job_from_another_account_is_never_shown(office: _Office) -> None:
    stranger = Task(
        id="job-elsewhere",
        workspace=str(office.demo),
        goal="Another account's secret plan",
        auto_commit=False,
        create_pr=False,
        model_name="scripted",
        model_base_url="",
        model_api_key="",
        status="success",
        tenant_id="org_someone_else",
    )
    with office.manager._lock:
        office.manager._tasks[stranger.id] = stranger
    ada = office.be("Ada")
    assert "job-elsewhere" not in office.listed("Ada")
    assert ada.get("/api/tasks/job-elsewhere").status_code == 404
    assert ada.post("/api/tasks/job-elsewhere/rerun", json={}).status_code == 404
    assert "secret plan" not in ada.get("/api/tasks").text


def test_schedules_follow_the_project(office: _Office) -> None:
    plan = {
        "name": "Nightly tidy",
        "goal": "Tidy the README",
        "workspace": str(office.demo),
        "auto_commit": False,
        "create_pr": False,
        "cadence": {"kind": "weekly", "hour": 8, "minute": 0, "weekdays": [0]},
    }
    # A schedule starts jobs, so reading the project is not enough to make one.
    assert office.be("Cy").post("/api/schedules", json=plan).status_code == 403
    made = office.be("Dee").post("/api/schedules", json=plan)
    assert made.status_code == 200, made.text
    schedule_id = made.json()["id"]
    assert office.be("Cy").post(f"/api/schedules/{schedule_id}/run").status_code == 403
    assert office.be("Cy").delete(f"/api/schedules/{schedule_id}").status_code == 403
    listed = office.be("Cy").get("/api/schedules").json()["schedules"]
    assert [item["id"] for item in listed] == [schedule_id]
    bob = office.be("Bob")
    assert bob.get("/api/schedules").json()["schedules"] == []
    assert bob.delete(f"/api/schedules/{schedule_id}").status_code == 404
    assert bob.get(f"/api/schedules/{schedule_id}/file").status_code == 404
