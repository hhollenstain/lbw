"""A person links their own machine as a runner, and a job runs there with their Claude Code."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.local_runner import clean_remote, runner_event
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.store.schema import apply_schema
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

_ENV = {
    "GIT_AUTHOR_NAME": "Ada",
    "GIT_AUTHOR_EMAIL": "ada@example.com",
    "GIT_COMMITTER_NAME": "Ada",
    "GIT_COMMITTER_EMAIL": "ada@example.com",
}


def _git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(_ENV)
    done = subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout


def _repo(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".lbw/\nhistory.json\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "start")
    return _git(root, "rev-parse", "HEAD").strip()


def _link(client: TestClient, name: str = "ada-laptop") -> str:
    started = client.post("/api/runner/link", json={"name": name, "platform": "darwin/arm64"})
    assert started.status_code == 200
    body = started.json()
    assert body["verification_url"].endswith("tab=integrations")
    waiting = client.post("/api/runner/link/token", json={"device_code": body["device_code"]})
    assert waiting.status_code == 428
    approved = client.post("/api/me/runners/approve", json={"user_code": body["user_code"]})
    assert approved.status_code == 200
    claimed = client.post("/api/runner/link/token", json={"device_code": body["device_code"]})
    assert claimed.status_code == 200
    token = claimed.json()["token"]
    assert token.startswith("lbwr_")
    again = client.post("/api/runner/link/token", json={"device_code": body["device_code"]})
    assert again.status_code == 400
    return token


def _beat(client: TestClient, token: str) -> dict:
    sent = client.post(
        "/api/runner/heartbeat",
        json={"version": "0.1.0", "claude_version": "2.1.0", "claude_ready": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert sent.status_code == 200
    return sent.json()


def _runner_model(client: TestClient, workspace: Path, root: Path, name: str = "opus") -> str:
    """A Claude endpoint on each person's machine, and one of its models turned on."""
    account = client.get("/api/account").json()
    with database(root) as db:
        ensure_workspace_project(db, account["org"]["id"], workspace)
    saved = client.post(
        "/api/org/endpoints",
        json={"name": "Claude Code", "provider": "anthropic", "auth": "runner"},
    )
    assert saved.status_code == 200, saved.text
    endpoint = next(item for item in saved.json()["endpoints"] if item["name"] == "Claude Code")
    assert endpoint["auth"] == "runner"
    assert endpoint["base_url"] == ""
    assert endpoint["has_api_key"] is False
    assert endpoint["model_names"] == ["sonnet", "opus", "haiku"]
    added = client.post(
        "/api/org/models",
        json={"endpoint_id": endpoint["id"], "model_name": name, "name": f"Claude {name}"},
    )
    assert added.status_code == 200, added.text
    model = next(item for item in added.json()["models"] if item["model_name"] == name)
    turned = client.put(f"/api/org/models/{model['id']}/enabled", json={"enabled": True})
    assert turned.status_code == 200
    listed = client.get("/api/org/models", params={"workspace": str(workspace)}).json()
    assert [item["runner"] for item in listed["models"]] == [True]
    return model["id"]


def _usage_rows(root: Path) -> list[dict]:
    """Usage is rolled up just after the task reports its status, so wait for it."""
    rows: list[dict] = []
    for _ in range(100):
        with database(root) as db:
            rows = db.fetchall("SELECT service, model, input_tokens, calls FROM usage_daily")
        if rows:
            return rows
        time.sleep(0.05)
    return rows


def _wait(client: TestClient, task_id: str, status: str) -> dict:
    body: dict = {}
    for _ in range(200):
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task never reached {status}: {body.get('status')} {body.get('reason')}")


def test_a_runner_links_with_a_code_and_only_its_hash_is_stored(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    token = _link(client)

    listed = client.get("/api/me/runners").json()["runners"]
    assert [runner["name"] for runner in listed] == ["ada-laptop"]
    assert listed[0]["online"] is False
    assert _beat(client, token)["commands"] == []
    runner = client.get("/api/me/runners").json()["runners"][0]
    assert runner["online"] is True
    assert runner["claude_version"] == "2.1.0"
    assert token not in client.get("/api/me/runners").text
    with database(tmp_path) as db:
        stored = db.fetchone("SELECT token_hash FROM runners")
        actions = [row["action"] for row in db.fetchall("SELECT action FROM audit_log")]
    assert stored["token_hash"] != token
    assert "runner.approve" in actions
    assert "runner.link" in actions

    asked = client.post(f"/api/me/runners/{runner['id']}/update-claude")
    assert asked.status_code == 200
    assert _beat(client, token)["commands"] == ["update_claude"]
    assert _beat(client, token)["commands"] == []

    bad = client.post("/api/runner/heartbeat", json={}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    removed = client.delete(f"/api/me/runners/{runner['id']}")
    assert removed.json()["runners"] == []
    gone = client.post(
        "/api/runner/heartbeat", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert gone.status_code == 401


def test_a_wrong_or_reused_code_is_refused(settings: Settings, tmp_path: Path) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    assert client.post("/api/me/runners/approve", json={"user_code": "ABCD"}).status_code == 400
    wrong = client.post("/api/me/runners/approve", json={"user_code": "ZZZZ-ZZZZ"})
    assert wrong.status_code == 400


def test_a_job_on_a_runner_model_needs_a_runner_online(settings: Settings, tmp_path: Path) -> None:
    _repo(tmp_path)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    model_id = _runner_model(client, tmp_path, tmp_path)
    refused = client.post(
        "/api/tasks",
        json={"workspace": str(tmp_path), "goal": "Add a file", "model_id": model_id},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "runner_required"


def test_only_claude_runs_on_each_persons_machine(settings: Settings, tmp_path: Path) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    refused = client.post(
        "/api/org/endpoints",
        json={"name": "Desk", "provider": "openai", "auth": "runner"},
    )
    assert refused.status_code == 400
    assert "only Claude" in refused.json()["error"]


def test_a_runner_model_stays_out_of_teams_and_schedules(
    settings: Settings, tmp_path: Path
) -> None:
    _repo(tmp_path)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    model_id = _runner_model(client, tmp_path, tmp_path)
    team = client.post(
        "/api/org/agent-teams",
        json={"name": "Local", "job": "build", "model_id": model_id},
    )
    assert team.status_code == 400
    assert "person's machine" in team.json()["error"]
    listed = client.get("/api/org/agent-teams").json()
    assert model_id not in [item["id"] for item in listed.get("models", [])]


def test_the_claude_oauth_link_is_dropped_on_upgrade(tmp_path: Path) -> None:
    with database(tmp_path) as db:
        db.execute("CREATE TABLE claude_link_apps (tenant_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE link_pending (state TEXT PRIMARY KEY)")
        apply_schema(db)
        left = db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('claude_link_apps', 'claude_profiles', 'link_pending')"
        )
    assert left == []


def test_a_job_runs_on_the_persons_runner_and_lands_on_a_branch(
    settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    base = _repo(workspace)
    manager = TaskManager(settings)
    client = TestClient(create_app(manager, default_workspace=workspace))
    try:
        client.get("/api/directory")
        model_id = _runner_model(client, workspace, workspace)
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/runner/jobs/next", headers=auth).status_code == 204

        created = client.post(
            "/api/tasks",
            json={"workspace": str(workspace), "goal": "Add a hello file", "model_id": model_id},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        leased = client.get("/api/runner/jobs/next?wait=10", headers=auth)
        assert leased.status_code == 200, leased.text
        job = leased.json()
        assert job["task_id"] == task_id
        assert job["goal"] == "Add a hello file"
        assert job["base_sha"] == base
        assert job["model"] == "opus"

        clone = tmp_path / "runner-worktree"
        subprocess.run(["git", "clone", "-q", str(workspace), str(clone)], check=True)
        (clone / "hello.txt").write_text("hello\n", encoding="utf-8")
        _git(clone, "add", "hello.txt")
        _git(clone, "commit", "-q", "-m", "Add a hello file")
        patch = _git(clone, "format-patch", "--stdout", f"{base}..HEAD")

        posted = client.post(
            f"/api/runner/jobs/{job['job_id']}/events",
            headers=auth,
            json={
                "events": [
                    {"kind": "step", "message": "Claude Code 2.1.0 started"},
                    {
                        "kind": "llm",
                        "model_name": "claude-sonnet-4-5",
                        "prompt_tokens": 1200,
                        "completion_tokens": 300,
                        "total_tokens": 1500,
                        "cache_read_tokens": 800,
                    },
                    {"kind": "secret", "message": "dropped"},
                ]
            },
        )
        assert posted.json() == {"cancel": False}
        finished = client.post(
            f"/api/runner/jobs/{job['job_id']}/finish",
            headers=auth,
            json={"status": "done", "summary": "Added hello.txt", "patch": patch},
        )
        assert finished.status_code == 200, finished.text

        body = _wait(client, task_id, "success")
        assert body["agent_kind"] == "claude_code"
        assert body["branch"].startswith("lbw/runner-")
        assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
        author = _git(workspace, "log", "-1", "--format=%an")
        assert author.strip() == "Ada"
        messages = [event.get("message") for event in body.get("events", [])]
        assert "dropped" not in messages
        usage = _usage_rows(workspace)
        with database(workspace) as db:
            jobs = db.fetchall("SELECT status FROM runner_jobs")
            audit = [row["action"] for row in db.fetchall("SELECT action FROM audit_log")]
        assert [(row["service"], row["model"], row["calls"]) for row in usage] == [
            ("claude-code (runner)", "claude-sonnet-4-5", 1)
        ]
        assert [row["status"] for row in jobs] == ["done"]
        assert "task.runner" in audit
        assert "task.usage" in audit
    finally:
        manager.shutdown(wait=False)


def test_a_runner_failure_fails_the_job(settings: Settings, tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    _repo(workspace)
    manager = TaskManager(settings)
    client = TestClient(create_app(manager, default_workspace=workspace))
    try:
        client.get("/api/directory")
        model_id = _runner_model(client, workspace, workspace)
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/tasks",
            json={"workspace": str(workspace), "goal": "Break", "model_id": model_id},
        )
        job = client.get("/api/runner/jobs/next?wait=10", headers=auth).json()
        client.post(
            f"/api/runner/jobs/{job['job_id']}/finish",
            headers=auth,
            json={"status": "failed", "error": "claude exited with status 1"},
        )
        body = _wait(client, created.json()["id"], "failed")
        assert "claude exited" in body["reason"]
        late = client.post(
            f"/api/runner/jobs/{job['job_id']}/events", headers=auth, json={"events": []}
        )
        assert late.json() == {"cancel": True}
    finally:
        manager.shutdown(wait=False)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://x-access-token:abc@github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("https://gitlab.example.com:8443/o/r", "https://gitlab.example.com:8443/o/r"),
    ],
)
def test_a_remote_sent_to_a_runner_carries_no_credentials(raw: str, clean: str) -> None:
    assert clean_remote(raw) == clean


def test_runner_events_keep_only_known_fields() -> None:
    assert runner_event({"kind": "shell", "message": "x"}) is None
    kept = runner_event(
        {"kind": "llm", "prompt_tokens": "12", "completion_tokens": -3, "service": "evil"}
    )
    assert kept["prompt_tokens"] == 12
    assert kept["completion_tokens"] == 0
    assert kept["service"] == "claude-code (runner)"


def test_a_runner_job_plans_first_and_stops_at_both_gates(
    settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    base = _repo(workspace)
    manager = TaskManager(settings.model_copy(update={"plan_goals": True}))
    client = TestClient(create_app(manager, default_workspace=workspace))
    try:
        client.get("/api/directory")
        model_id = _runner_model(client, workspace, workspace)
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(workspace),
                "goal": "Add a hello file",
                "model_id": model_id,
                "triage_gate": "ask",
                "approval_gate": "ask",
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        planning = client.get("/api/runner/jobs/next?wait=10", headers=auth).json()
        assert planning["phase"] == "plan"
        assert "Architect" in planning["instruction"]
        assert planning["base_sha"] == base
        plan = "## Plan\n1. Add hello.txt"
        client.post(
            f"/api/runner/jobs/{planning['job_id']}/finish",
            headers=auth,
            json={"status": "done", "summary": plan},
        )
        held = _wait(client, task_id, "waiting")
        assert held["stage"] == "triage"
        assert held["plan"] == plan
        assert _git(workspace, "rev-parse", "HEAD").strip() == base

        _beat(client, token)
        built = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "build"})
        assert built.status_code == 200, built.text
        building = client.get("/api/runner/jobs/next?wait=10", headers=auth).json()
        assert building["phase"] == "build"
        assert building["plan"] == plan
        assert building["base_sha"] == base

        clone = tmp_path / "runner-worktree"
        subprocess.run(["git", "clone", "-q", str(workspace), str(clone)], check=True)
        (clone / "hello.txt").write_text("hello\n", encoding="utf-8")
        _git(clone, "add", "hello.txt")
        _git(clone, "commit", "-q", "-m", "Add a hello file")
        patch = _git(clone, "format-patch", "--stdout", f"{base}..HEAD")
        client.post(
            f"/api/runner/jobs/{building['job_id']}/finish",
            headers=auth,
            json={"status": "done", "summary": "Added hello.txt", "patch": patch},
        )
        waiting = _wait(client, task_id, "waiting")
        assert waiting["stage"] == "approval"
        assert waiting["branch"].startswith("lbw/runner-")
        assert _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
        assert not (workspace / "hello.txt").exists()

        approved = client.post(f"/api/tasks/{task_id}/decide", json={"decision": "approve"})
        assert approved.status_code == 200, approved.text
        done = _wait(client, task_id, "success")
        assert done["branch"] == waiting["branch"]
        assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
        assert client.get("/api/runner/jobs/next", headers=auth).status_code == 204
    finally:
        manager.shutdown(wait=False)


def test_a_sent_back_runner_change_continues_from_its_commits(
    settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    base = _repo(workspace)
    manager = TaskManager(settings)
    client = TestClient(create_app(manager, default_workspace=workspace))
    try:
        client.get("/api/directory")
        model_id = _runner_model(client, workspace, workspace)
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        task_id = client.post(
            "/api/tasks",
            json={
                "workspace": str(workspace),
                "goal": "Add a hello file",
                "model_id": model_id,
                "approval_gate": "ask",
            },
        ).json()["id"]
        first = client.get("/api/runner/jobs/next?wait=10", headers=auth).json()
        assert first["phase"] == "build"
        assert first["prior_patch"] == ""
        clone = tmp_path / "runner-worktree"
        subprocess.run(["git", "clone", "-q", str(workspace), str(clone)], check=True)
        (clone / "hello.txt").write_text("hello\n", encoding="utf-8")
        _git(clone, "add", "hello.txt")
        _git(clone, "commit", "-q", "-m", "Add a hello file")
        patch = _git(clone, "format-patch", "--stdout", f"{base}..HEAD")
        client.post(
            f"/api/runner/jobs/{first['job_id']}/finish",
            headers=auth,
            json={"status": "done", "summary": "Added", "patch": patch},
        )
        assert _wait(client, task_id, "waiting")["stage"] == "approval"

        _beat(client, token)
        sent = client.post(
            f"/api/tasks/{task_id}/decide",
            json={"decision": "send_back", "note": "Say hello twice"},
        )
        assert sent.status_code == 200, sent.text
        again = client.get("/api/runner/jobs/next?wait=10", headers=auth).json()
        assert again["phase"] == "build"
        assert "Subject: [PATCH] Add a hello file" in again["prior_patch"]
        assert "Say hello twice" in again["review_notes"]
    finally:
        manager.shutdown(wait=False)
