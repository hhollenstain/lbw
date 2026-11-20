"""Linking a Claude organization with Claude Platform OAuth, against a stand-in Anthropic."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.llm.client import AnthropicClient, apply_credential, client_for
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.claude_link import BILLING_BETA
from agent_lbw.runtime.store.gateway import person_credential
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


class _Reply:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Anthropic:
    """Answers the calls LBW makes to auth.claude.com and the user profiles API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.refreshes = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, dict(json or {}), dict(headers or {})))
        if url.endswith("/v1/user_profiles"):
            return _Reply(201, {"id": "uprof_ada", "type": "user_profile"})
        if url.endswith("/v1/oauth/par"):
            return _Reply(201, {"request_uri": "urn:req:1", "expires_in": 600})
        if url.endswith("/v1/oauth/token") and json["grant_type"] == "authorization_code":
            return _Reply(
                200,
                {
                    "access_token": "ocat_first",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "ocrt_first",
                    "refresh_token_expires_in": 31_536_000,
                    "connection_id": "ocon_ada",
                    "scope": "workspace:inference",
                },
            )
        if url.endswith("/v1/oauth/token") and json["grant_type"] == "refresh_token":
            self.refreshes += 1
            return _Reply(
                200,
                {"access_token": "ocat_second", "expires_in": 3600, "refresh_token": "ocrt_2"},
            )
        if url.endswith("/v1/oauth/revoke"):
            return _Reply(200, {"revoked_connection_ids": [json["connection_id"]]})
        return _Reply(404, {"error": {"message": "unexpected"}})


def test_a_person_links_their_claude_organization_and_a_run_bills_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anthropic = _Anthropic()
    monkeypatch.setattr("agent_lbw.runtime.store.claude_link.httpx.post", anthropic.post)
    jwt = tmp_path / "idp.jwt"
    jwt.write_text("header.payload.signature\n", encoding="utf-8")
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    client.get("/api/directory")
    client.post("/api/directory/promote")
    saved = client.post(
        "/api/org/endpoints",
        json={"name": "Claude", "provider": "anthropic", "auth": "personal"},
    )
    assert saved.status_code == 200, saved.text
    endpoint = next(item for item in saved.json()["endpoints"] if item["name"] == "Claude")

    not_ready = client.get("/api/me/integrations").json()
    assert not_ready["claude_link_ready"] is False
    assert not_ready["integrations"][0]["can_link_claude"] is False

    missing_key = client.put(
        "/api/org/claude-link",
        json={"client_id": "app_123", "assertion_file": str(jwt)},
    )
    assert missing_key.status_code == 400
    app = client.put(
        "/api/org/claude-link",
        json={
            "client_id": "app_123",
            "assertion_file": str(jwt),
            "profile_api_key": "sk-ant-lbw-own",
        },
    )
    assert app.status_code == 200, app.text
    assert app.json()["configured"] is True
    assert app.json()["callback_url"].endswith("/api/integrations/claude/callback")
    assert "sk-ant-lbw-own" not in app.text
    assert client.get("/api/me/integrations").json()["integrations"][0]["can_link_claude"] is True

    started = client.post(f"/api/me/integrations/{endpoint['id']}/claude-link")
    assert started.status_code == 200, started.text
    target = urlparse(started.json()["url"])
    assert target.netloc == "auth.claude.com"
    assert parse_qs(target.query) == {"client_id": ["app_123"], "request_uri": ["urn:req:1"]}
    profile_call = next(call for call in anthropic.calls if call[0].endswith("/user_profiles"))
    assert profile_call[2]["x-api-key"] == "sk-ant-lbw-own"
    par = next(call for call in anthropic.calls if call[0].endswith("/oauth/par"))[1]
    assert par["user_profile_id"] == "uprof_ada"
    assert par["scope"] == "billing:attribution offline_access"
    assert par["code_challenge_method"] == "S256"
    assert par["client_assertion"] == "header.payload.signature"
    state = par["state"]

    wrong = client.get(
        "/api/integrations/claude/callback",
        params={"code": "c", "state": "not-it"},
        follow_redirects=False,
    )
    assert wrong.status_code == 303
    assert "link_error=1" in wrong.headers["location"]

    finished = client.get(
        "/api/integrations/claude/callback",
        params={"code": "code-1", "state": state},
        follow_redirects=False,
    )
    assert finished.status_code == 303
    assert finished.headers["location"] == "/?section=personal&tab=integrations"
    exchange = next(
        call[1]
        for call in anthropic.calls
        if call[0].endswith("/oauth/token") and call[1]["grant_type"] == "authorization_code"
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == par["code_challenge"]
    again = client.get(
        "/api/integrations/claude/callback",
        params={"code": "code-1", "state": state},
        follow_redirects=False,
    )
    assert "link_error=1" in again.headers["location"]

    mine = client.get("/api/me/integrations").json()["integrations"][0]
    assert mine["linked"] is True
    assert mine["linked_with"] == "claude_link"
    with database(tmp_path) as db:
        row = db.fetchone("SELECT connection_id, profile_id FROM endpoint_credentials")
        assert row == {"connection_id": "ocon_ada", "profile_id": "uprof_ada"}
        credential = person_credential(db, tmp_path, endpoint["id"])
        assert credential["api_key"] == "ocat_first"
        assert credential["link"]["headers"] == {
            "anthropic-beta": BILLING_BETA,
            "anthropic-user-profile-id": "uprof_ada",
        }
        db.execute("UPDATE endpoint_credentials SET expires_at = 0")
        renewed = person_credential(db, tmp_path, endpoint["id"])
    assert renewed["api_key"] == "ocat_second"
    assert anthropic.refreshes == 1

    removed = client.delete(f"/api/me/integrations/{endpoint['id']}")
    assert removed.status_code == 200
    revoke = next(call[1] for call in anthropic.calls if call[0].endswith("/oauth/revoke"))
    assert revoke["connection_id"] == "ocon_ada"
    assert removed.json()["integrations"][0]["linked"] is False


def test_a_linked_claude_request_sends_the_bearer_token_and_billing_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    class Response:
        status_code = 200

        def json(self) -> dict:
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 4,
                },
            }

    def fake_post(url, headers, json, timeout):
        seen["headers"] = headers
        seen["json"] = json
        return Response()

    monkeypatch.setattr("agent_lbw.llm.client.httpx.post", fake_post)
    llm = client_for(
        model="claude-sonnet-4-5", base_url="https://api.anthropic.com/v1", api_key="ocat_x"
    )
    assert isinstance(llm, AnthropicClient)
    apply_credential(
        llm,
        {
            "model_id": "model_1",
            "service": "Claude",
            "provider": "anthropic",
            "link": {
                "headers": {"anthropic-beta": BILLING_BETA, "anthropic-user-profile-id": "uprof_a"},
                "body": {"billing_policy": {"type": "billing_policy", "bill_to": "user_profile"}},
            },
        },
    )
    turn = llm.complete([{"role": "user", "content": "hi"}], [])
    assert seen["headers"]["authorization"] == "Bearer ocat_x"
    assert "x-api-key" not in seen["headers"]
    assert seen["headers"]["anthropic-user-profile-id"] == "uprof_a"
    assert seen["json"]["billing_policy"]["bill_to"] == "user_profile"
    assert llm.usage_service == "Claude"
    assert turn.prompt_tokens == 95
    assert turn.cache_read_tokens == 80
    assert turn.cache_write_tokens == 5
    assert turn.completion_tokens == 4
