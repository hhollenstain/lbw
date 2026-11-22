"""End to end: the real lbw-runner relays a job's model calls to a fake Ollama.

This builds the Go runner, serves LBW on a real port, and runs a small
Ollama-compatible server on this machine. It needs Go, so it is skipped where
Go is not installed. CI runs it in its own job: `pytest -m runner_e2e`.

It also covers a runner that dies while it waits for work: the person's next
model call must reach their runner that is still running, not the dead one.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from tests.test_runners import _link, _repo

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen2.5-coder:7b"

pytestmark = [
    pytest.mark.runner_e2e,
    # CI sets LBW_REQUIRE_RUNNER_E2E so a missing Go fails instead of skipping.
    pytest.mark.skipif(
        shutil.which("go") is None and not os.environ.get("LBW_REQUIRE_RUNNER_E2E"),
        reason="needs Go to build lbw-runner",
    ),
]


class _FakeOllama(BaseHTTPRequestHandler):
    """Answers the two calls the runner relays, like Ollama's OpenAI API."""

    chats: list[str] = []

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self._send({"object": "list", "data": [{"id": MODEL}, {"id": "llama3.2:3b"}]})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).chats.append(str(request.get("model")))
        self._send(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Nothing needs to change."},
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
            }
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def runner_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("bin") / "lbw-runner"
    subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/lbw-runner"],
        cwd=ROOT / "runner",
        env={**os.environ, "CGO_ENABLED": "0"},
        check=True,
        capture_output=True,
        timeout=300,
    )
    return out


@pytest.fixture
def ollama() -> Iterator[str]:
    _FakeOllama.chats = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def lbw(settings: Settings, tmp_path: Path) -> Iterator[tuple[TestClient, str, Path]]:
    """LBW on a real port, for the runner, and a test client on the same app."""
    workspace = tmp_path / "demo"
    workspace.mkdir()
    _repo(workspace)
    manager = TaskManager(settings)
    app = create_app(manager, default_workspace=workspace)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        yield TestClient(app), f"http://127.0.0.1:{port}", workspace
    finally:
        manager.shutdown(wait=False)
        server.should_exit = True
        thread.join(timeout=10)


def _start(binary: Path, home: Path, server: str, token: str) -> subprocess.Popen:
    """Run lbw-runner with no Claude Code, so it only relays model calls."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"server": server, "claude_path": str(home / "no-claude")}), encoding="utf-8"
    )
    env = {**os.environ, "LBW_RUNNER_HOME": str(home), "LBW_RUNNER_TOKEN": token}
    env.pop("LBW_SERVER", None)
    return subprocess.Popen(
        [str(binary), "run"],
        env=env,
        stdout=(home / "runner.log").open("w"),
        stderr=subprocess.STDOUT,
    )


def _wait_relaying(client: TestClient, name: str) -> None:
    for _ in range(200):
        runners = client.get("/api/me/runners").json()["runners"]
        if any(item["name"] == name and item["online"] and item["llm_relay"] for item in runners):
            return
        time.sleep(0.05)
    raise AssertionError(f"{name} never came online as a relay")


def test_the_real_runner_relays_a_job_to_the_persons_model_server(
    runner_binary: Path, ollama: str, lbw: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, server, workspace = lbw
    client.get("/api/directory")
    account = client.get("/api/account").json()
    with database(workspace) as db:
        ensure_workspace_project(db, account["org"]["id"], workspace)
    assert client.put("/api/org/local-models", json={"policy": "everyone"}).status_code == 200
    saved = client.put(
        "/api/me/local-model", json={"enabled": True, "provider": "ollama", "base_url": ollama}
    )
    assert saved.status_code == 200, saved.text

    first = _start(runner_binary, tmp_path / "desk", server, _link(client, "desk"))
    second = None
    try:
        _wait_relaying(client, "desk")
        synced = client.post("/api/me/local-model/sync")
        assert synced.status_code == 200, synced.text
        assert MODEL in synced.json()["models"]

        # The desk dies while it waits for work, yet looks like the freshest runner.
        time.sleep(1)
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        with database(workspace) as db:
            db.execute(
                "UPDATE runners SET last_seen = ? WHERE name = 'desk'", (int(time.time()) + 60,)
            )
        second = _start(runner_binary, tmp_path / "laptop", server, _link(client, "laptop"))
        _wait_relaying(client, "laptop")

        created = client.post(
            "/api/tasks",
            json={
                "workspace": str(workspace),
                "goal": "Say whether the README needs a change",
                "model_id": f"local:{MODEL}",
            },
        )
        assert created.status_code == 201, created.text
        body: dict = {}
        for _ in range(600):
            body = client.get(f"/api/tasks/{created.json()['id']}").json()
            if body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.1)
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    reason = str(body.get("reason") or "")
    assert "did not pick up" not in reason, reason
    assert body["status"] not in {"queued", "running", "error"}, reason
    assert _FakeOllama.chats, reason
    assert set(_FakeOllama.chats) == {MODEL}
