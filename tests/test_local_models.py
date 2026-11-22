"""A person's own model server, such as Ollama, reached through their own runner.

The organization turns it on for everyone or for chosen people. A person sets the
address on their profile and syncs its models. A job on one of them runs LBW's
loop, and each model call is relayed by that person's runner, never anyone else's.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_runners import _git, _link, _repo, _usage_rows

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.llm_relay import LlmRelay, RelayError
from agent_lbw.runtime.store.rules import ensure_workspace_project
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

_MODELS = {"object": "list", "data": [{"id": "qwen2.5-coder:7b"}, {"id": "llama3.2:3b"}]}
_ANSWER = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "qwen2.5-coder:7b",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Nothing needs to change."},
        }
    ],
    "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
}


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


def _relay_beat(client: TestClient, token: str, *, relay: bool = True) -> None:
    sent = client.post(
        "/api/runner/heartbeat",
        json={"version": "0.2.0", "llm_relay": relay},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert sent.status_code == 200


class _FakeRunner:
    """Plays lbw-runner's relay loop: take each call and answer as the model server."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.auth = {"Authorization": f"Bearer {token}"}
        self.calls: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _FakeRunner:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            got = self.client.get("/api/runner/llm/next?wait=1", headers=self.auth)
            if got.status_code != 200:
                continue
            call = got.json()
            acked = self.client.post(f"/api/runner/llm/{call['call_id']}/ack", headers=self.auth)
            assert acked.status_code == 200, acked.text
            self.calls.append(call)
            body = _MODELS if call["path"] == "/v1/models" else _ANSWER
            answered = self.client.post(
                f"/api/runner/llm/{call['call_id']}",
                headers=self.auth,
                json={"status": 200, "body": json.dumps(body)},
            )
            assert answered.status_code == 200, answered.text


def _org(client: TestClient, workspace: Path) -> tuple[dict, str]:
    """An organization with Grace on the project as a writer."""
    client.get("/api/directory")
    client.post("/api/directory/promote")
    account = client.get("/api/account").json()
    with database(workspace) as db:
        project = ensure_workspace_project(db, account["org"]["id"], workspace)
    people = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace_id = next(user["id"] for user in people["users"] if user["name"] == "Grace")
    client.post(
        f"/api/directory/projects/{project['id']}/grants",
        json={"subject_type": "identity", "subject_id": grace_id, "access": "write"},
    )
    return account, grace_id


def test_the_organization_turns_local_models_on_for_chosen_people(
    settings: Settings, tmp_path: Path
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    account, grace_id = _org(client, tmp_path)
    ada_id = account["current_user_id"]
    server = {"enabled": True, "provider": "ollama", "base_url": ""}

    assert client.get("/api/org/local-models").json()["policy"] == "off"
    mine = client.get("/api/me/local-model").json()
    assert mine["allowed"] is False
    assert mine["base_url"] == "http://127.0.0.1:11434"
    assert client.put("/api/me/local-model", json=server).status_code == 403

    chosen = client.put("/api/org/local-models", json={"policy": "chosen"})
    assert chosen.status_code == 200
    assert {person["id"] for person in chosen.json()["people"]} == {ada_id, grace_id}
    picked = client.put(f"/api/org/local-models/people/{grace_id}", json={"allowed": True})
    assert picked.status_code == 200
    assert client.put("/api/org/local-models", json={"policy": "most"}).status_code == 400

    assert client.put("/api/me/local-model", json=server).status_code == 403
    _become(tmp_path, grace_id)
    assert client.get("/api/me/local-model").json()["allowed"] is True
    assert client.put("/api/org/local-models", json={"policy": "everyone"}).status_code == 403
    assert client.get("/api/org/local-models").json()["people"] == []
    saved = client.put(
        "/api/me/local-model",
        json={"enabled": True, "provider": "ollama", "base_url": "http://127.0.0.1:11434/v1/"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == "http://127.0.0.1:11434"
    for bad in ("ftp://box:21", "http://me:pw@127.0.0.1:11434", "http://127.0.0.1:1?x=1"):
        refused = client.put(
            "/api/me/local-model", json={"enabled": True, "provider": "ollama", "base_url": bad}
        )
        assert refused.status_code == 400, bad

    _become(tmp_path, ada_id)
    client.put("/api/org/local-models", json={"policy": "everyone"})
    assert client.put("/api/me/local-model", json=server).status_code == 200
    with database(tmp_path) as db:
        actions = [row["action"] for row in db.fetchall("SELECT action FROM audit_log")]
    assert actions.count("local_models.policy") == 2
    assert "local_models.person" in actions
    assert actions.count("local_models.server") == 2


def test_sync_lists_the_models_through_the_persons_runner(
    settings: Settings, tmp_path: Path
) -> None:
    _repo(tmp_path)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    _org(client, tmp_path)
    client.put("/api/org/local-models", json={"policy": "everyone"})
    client.put("/api/me/local-model", json={"enabled": True, "provider": "ollama"})

    refused = client.post("/api/me/local-model/sync")
    assert refused.status_code == 409
    assert refused.json()["code"] == "runner_required"
    token = _link(client)
    _relay_beat(client, token, relay=False)
    assert client.post("/api/me/local-model/sync").status_code == 409

    _relay_beat(client, token)
    with _FakeRunner(client, token) as runner:
        synced = client.post("/api/me/local-model/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["models"] == ["llama3.2:3b", "qwen2.5-coder:7b"]
    assert [(call["method"], call["path"], call["base_url"]) for call in runner.calls] == [
        ("GET", "/v1/models", "http://127.0.0.1:11434")
    ]

    listed = client.get("/api/org/models", params={"workspace": str(tmp_path)}).json()["models"]
    local = [item for item in listed if item.get("local")]
    assert [item["id"] for item in local] == ["local:llama3.2:3b", "local:qwen2.5-coder:7b"]
    assert local[0]["endpoint_name"] == "Ollama"

    moved = client.put(
        "/api/me/local-model",
        json={"enabled": True, "provider": "lmstudio", "base_url": "http://127.0.0.1:1234"},
    )
    assert moved.json()["models"] == []


def test_a_job_on_a_local_model_relays_each_call_through_the_persons_runner(
    settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    _repo(workspace)
    manager = TaskManager(settings)
    client = TestClient(create_app(manager, default_workspace=workspace))
    try:
        _org(client, workspace)
        client.put("/api/org/local-models", json={"policy": "everyone"})
        client.put("/api/me/local-model", json={"enabled": True, "provider": "ollama"})
        token = _link(client)
        _relay_beat(client, token)
        job = {"workspace": str(workspace), "goal": "Tidy the README"}
        with _FakeRunner(client, token) as runner:
            client.post("/api/me/local-model/sync")
            created = client.post("/api/tasks", json={**job, "model_id": "local:qwen2.5-coder:7b"})
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]
            body: dict = {}
            for _ in range(400):
                body = client.get(f"/api/tasks/{task_id}").json()
                if body["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
        chats = [call for call in runner.calls if call["path"] == "/v1/chat/completions"]
        assert chats, body.get("reason")
        assert {json.loads(call["body"])["model"] for call in chats} == {"qwen2.5-coder:7b"}
        assert {call["base_url"] for call in chats} == {"http://127.0.0.1:11434"}
        assert body["agent_kind"] in {"", None}
        usage = _usage_rows(workspace)
        assert {(row["service"], row["model"]) for row in usage} == {
            ("Ollama (your machine)", "qwen2.5-coder:7b")
        }
        assert _git(workspace, "branch", "--show-current").strip()

        missing = client.post("/api/tasks", json={**job, "model_id": "local:mistral"})
        assert missing.status_code == 403
        assert "sync your local models" in missing.json()["error"]
    finally:
        manager.shutdown(wait=False)


def test_a_coworker_never_reaches_someone_elses_local_model(
    settings: Settings, tmp_path: Path
) -> None:
    _repo(tmp_path)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    account, grace_id = _org(client, tmp_path)
    client.put("/api/org/local-models", json={"policy": "chosen"})
    client.put(f"/api/org/local-models/people/{account['current_user_id']}", json={"allowed": True})
    client.put("/api/me/local-model", json={"enabled": True, "provider": "ollama"})
    token = _link(client)
    _relay_beat(client, token)
    with _FakeRunner(client, token):
        client.post("/api/me/local-model/sync")

    _become(tmp_path, grace_id)
    listed = client.get("/api/org/models", params={"workspace": str(tmp_path)}).json()["models"]
    assert not [item for item in listed if item.get("local")]
    refused = client.post(
        "/api/tasks",
        json={
            "workspace": str(tmp_path),
            "goal": "Tidy the README",
            "model_id": "local:qwen2.5-coder:7b",
        },
    )
    assert refused.status_code == 403
    assert "not turned on local models for you" in refused.json()["error"]


def test_the_relay_hands_a_call_only_to_its_owners_runners() -> None:
    relay = LlmRelay()
    answered: dict = {}

    def ask() -> None:
        answered.update(
            relay.call(
                identity_id="ada",
                method="POST",
                path="/v1/chat/completions",
                body="{}",
                base_url="http://127.0.0.1:11434",
                pickup_seconds=5,
                call_seconds=5,
            )
        )

    thread = threading.Thread(target=ask)
    thread.start()
    assert relay.next("run_grace", "grace", 0.3) is None
    call = relay.next("run_ada_desk", "ada", 2)
    assert call is not None and call["path"] == "/v1/chat/completions"
    assert relay.ack(call["call_id"], "run_ada_laptop") is False
    assert relay.ack(call["call_id"], "run_ada_desk") is True
    assert relay.next("run_ada_laptop", "ada", 0.1) is None
    assert relay.answer(call["call_id"], "run_grace", {"status": 200, "body": "{}"}) is False
    assert relay.answer(call["call_id"], "run_ada_laptop", {"status": 200, "body": "{}"}) is False
    assert relay.answer(call["call_id"], "run_ada_desk", {"status": 200, "body": "{}"}) is True
    thread.join(timeout=5)
    assert answered == {"status": 200, "body": "{}"}


def test_a_call_no_runner_confirms_is_offered_to_the_persons_other_runner() -> None:
    """A poll left by a runner that died takes the call but never confirms it."""
    relay = LlmRelay(ack_seconds=0.2)
    answered: dict = {}

    def ask() -> None:
        answered.update(
            relay.call(
                identity_id="ada",
                method="GET",
                path="/v1/models",
                body="",
                base_url="http://127.0.0.1:11434",
                pickup_seconds=5,
                call_seconds=5,
            )
        )

    thread = threading.Thread(target=ask)
    thread.start()
    lost = relay.next("run_dead", "ada", 2)
    assert lost is not None
    again = relay.next("run_alive", "ada", 2)
    assert again is not None and again["call_id"] == lost["call_id"]
    assert relay.ack(lost["call_id"], "run_dead") is False
    assert relay.answer(lost["call_id"], "run_dead", {"status": 200}) is False
    assert relay.ack(again["call_id"], "run_alive") is True
    assert relay.answer(again["call_id"], "run_alive", {"status": 200, "body": "{}"}) is True
    thread.join(timeout=5)
    assert answered == {"status": 200, "body": "{}"}


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/api/pull"), ("GET", "/v1/chat/completions"), ("DELETE", "/")]
)
def test_the_relay_only_carries_model_calls(method: str, path: str) -> None:
    with pytest.raises(RelayError):
        LlmRelay().call(
            identity_id="ada",
            method=method,
            path=path,
            body="",
            base_url="http://127.0.0.1:11434",
            pickup_seconds=0,
        )
