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


def test_a_job_for_my_machine_needs_a_runner_online(settings: Settings, tmp_path: Path) -> None:
    _repo(tmp_path)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    refused = client.post("/api/tasks", json={"goal": "Add a file", "run_on": "my_runner"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "runner_required"
    odd = client.post("/api/tasks", json={"goal": "Add a file", "run_on": "elsewhere"})
    assert odd.status_code == 400


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
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/runner/jobs/next", headers=auth).status_code == 204

        created = client.post(
            "/api/tasks",
            json={"workspace": str(workspace), "goal": "Add a hello file", "run_on": "my_runner"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        leased = client.get("/api/runner/jobs/next?wait=10", headers=auth)
        assert leased.status_code == 200, leased.text
        job = leased.json()
        assert job["task_id"] == task_id
        assert job["goal"] == "Add a hello file"
        assert job["base_sha"] == base

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
        with database(workspace) as db:
            usage = db.fetchall("SELECT service, model, input_tokens, calls FROM usage_daily")
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
        token = _link(client)
        _beat(client, token)
        auth = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/tasks",
            json={"workspace": str(workspace), "goal": "Break", "run_on": "my_runner"},
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
