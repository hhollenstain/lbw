"""A connected GitHub App can place repositories and copy access onto linked people."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.github_sync import link_github_login
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _become(root: Path, identity_id: str) -> None:
    with database(root) as db:
        db.execute("UPDATE install SET current_identity_id = ?", (identity_id,))


class _Response:
    def __init__(self, status: int, payload: object, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


def test_github_app_places_a_repository_and_syncs_linked_access(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    def fake_request(method: str, url: str, headers: dict | None = None, timeout: float = 20):
        assert headers is not None
        assert "BEGIN" not in headers["Authorization"]
        if method == "POST" and url.endswith("/access_tokens"):
            return _Response(200, {"token": "ghs_secret"})
        if method == "GET" and url.endswith("/app/installations"):
            return _Response(
                200,
                [{"id": 42, "account": {"login": "acme", "type": "Organization"}}],
            )
        if method == "GET" and url.endswith("/app"):
            return _Response(200, {"slug": "lbw-local"})
        if "/installation/repositories" in url:
            return _Response(200, {"repositories": [{"full_name": "acme/demo", "private": True}]})
        if "/teams/" in url and url.split("?", 1)[0].endswith("/repos"):
            return _Response(
                200,
                [
                    {
                        "full_name": "acme/demo",
                        "permissions": {"push": True, "pull": True},
                    }
                ],
            )
        if "/teams/" in url and "role=maintainer" in url:
            return _Response(200, [{"login": "ada"}])
        if "/teams/" in url and "role=member" in url:
            return _Response(200, [{"login": "grace"}])
        if url.split("?", 1)[0].endswith("/teams"):
            return _Response(200, [{"name": "Platform", "slug": "platform"}])
        if "role=admin" in url:
            return _Response(200, [{"login": "ada"}, {"login": "bea"}])
        if "role=member" in url:
            return _Response(200, [{"login": "grace"}])
        if url.split("?", 1)[0].endswith("/collaborators"):
            return _Response(
                200,
                [
                    {"login": "ada", "permissions": {"admin": True, "push": True, "pull": True}},
                    {"login": "grace", "permissions": {"pull": True}},
                    {"login": "bea", "permissions": {"pull": False}},
                ],
            )
        return _Response(404, {})

    def fake_clone(url, parent, *, name=None, git_config=None):
        assert git_config is not None
        assert "ghs_secret" not in " ".join(git_config)
        dest = Path(parent) / (name or "demo")
        dest.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", url],
            cwd=dest,
            check=True,
            capture_output=True,
        )
        return {"path": str(dest), "name": dest.name}

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fake_request)
    monkeypatch.setattr("agent_lbw.github_http.clone_workspace", fake_clone)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="org-repo-search"' in page.text
    assert 'id="org-repo-table"' in page.text
    assert 'id="sync-github-access"' in page.text
    assert 'id="add-github-repo"' not in page.text
    assert 'placeholder="GitHub login"' not in page.text
    assert "/api/github/connect" in page.text

    saved = client.put("/api/github/app", json={"app_id": "123", "private_key": _pem()})
    assert saved.status_code == 200
    assert saved.json()["account_login"] == "acme"
    assert "ghs_secret" not in saved.text

    account = client.get("/api/account").json()
    owner = next(user for user in account["users"] if user["id"] == account["current_user_id"])
    with database(tmp_path) as db:
        linked = link_github_login(db, user_id=owner["id"], github_login="ada")
    assert linked["users"][0]["github_login"] == "ada"
    for name, login in (("Bea", "bea"), ("Grace", "grace")):
        added = client.post("/api/account/users", json={"name": name}).json()
        person = next(user for user in added["users"] if user["name"] == name)
        with database(tmp_path) as db:
            link_github_login(db, user_id=person["id"], github_login=login)

    parent = tmp_path / "checkouts"
    placed = client.post(
        "/api/github/workspaces",
        json={
            "full_name": "acme/demo",
            "subject_type": "tenant",
            "subject_id": account["org"]["id"],
            "parent": str(parent),
        },
    )
    assert placed.status_code == 200
    body = placed.json()
    assert body["placed"] == "the organization"
    assert body["full_name"] == "acme/demo"
    assert "ghs_secret" not in placed.text
    assert (parent / "demo" / ".git").is_dir()

    synced = client.post("/api/github/sync")
    assert synced.status_code == 200
    summary = synced.json()
    assert "ghs_secret" not in synced.text
    roles = {user["name"]: user["role"] for user in summary["account"]["users"]}
    assert roles[owner["name"]] == "owner"
    assert roles["Bea"] == "admin"
    assert roles["Grace"] == "member"
    assert summary["teams"] == [{"name": "Platform", "members": ["ada", "grace"]}]

    with database(tmp_path) as db:
        grants = db.fetchall(
            """
            SELECT identities.github_login, project_grants.subject_type, project_grants.access
            FROM project_grants
            JOIN identities ON identities.id = project_grants.subject_id
            WHERE project_grants.subject_type = 'identity'
            ORDER BY identities.github_login
            """
        )
        team = db.fetchone("SELECT id FROM teams WHERE name = ?", ("Platform",))
    assert [(row["github_login"], row["access"]) for row in grants] == [
        ("ada", "manage"),
        ("grace", "use"),
    ]
    assert team is not None
    for_team = client.post(
        "/api/github/workspaces",
        json={
            "full_name": "acme/demo",
            "subject_type": "team",
            "subject_id": team["id"],
            "parent": str(parent),
        },
    )
    assert for_team.status_code == 200
    assert for_team.json()["placed"] == "team Platform"
    with database(tmp_path) as db:
        team_grant = db.fetchone(
            """
            SELECT access FROM project_grants
            WHERE subject_type = 'team' AND subject_id = ?
            """,
            (team["id"],),
        )
    assert team_grant is not None
    assert team_grant["access"] == "use"

    grace = next(user for user in summary["account"]["users"] if user["name"] == "Grace")
    _become(tmp_path, grace["id"])
    refused = client.post(
        "/api/github/workspaces",
        json={"full_name": "acme/demo", "subject_type": "tenant", "subject_id": ""},
    )
    assert refused.status_code == 403
    typed = client.put(
        "/api/github/login",
        json={"user_id": owner["id"], "github_login": "someone"},
    )
    assert typed.status_code == 405


def test_a_person_connects_by_signing_in_with_github(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    secret = "lbw-client-secret-value"
    seen = {"exchange": 0, "user": 0}
    logins = ["octocat", "gracehub"]

    def fake_request(method: str, url: str, headers: dict | None = None, timeout: float = 20):
        if method == "GET" and url.endswith("/app"):
            return _Response(200, {"slug": "lbw-local", "client_id": "Iv1.abc123"})
        if method == "GET" and url.endswith("/app/installations"):
            return _Response(200, [])
        if method == "GET" and url.endswith("/user"):
            assert headers is not None
            assert headers["Authorization"] == "Bearer ghu_user_token"
            seen["user"] += 1
            return _Response(200, {"login": logins[seen["user"] - 1]})
        return _Response(404, {})

    def fake_post(
        url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 20
    ):
        assert url == "https://github.com/login/oauth/access_token"
        assert data is not None
        assert data["client_secret"] == secret
        assert data["code"] == "the-code"
        assert data["code_verifier"]
        seen["exchange"] += 1
        return _Response(200, {"access_token": "ghu_user_token", "token_type": "bearer"})

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fake_request)
    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.post", fake_post)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    bare = client.put("/api/github/app", json={"app_id": "123", "private_key": _pem()})
    assert bare.status_code == 200
    missing = client.get("/api/github/connect")
    assert missing.status_code == 400
    saved = client.put(
        "/api/github/app",
        json={"app_id": "123", "private_key": "", "client_secret": secret},
    )
    assert saved.status_code == 200
    assert saved.json()["has_client_secret"] is True
    assert saved.json()["client_id"] == "Iv1.abc123"
    assert secret not in saved.text
    with database(tmp_path) as db:
        row = db.fetchone("SELECT client_secret_ciphertext FROM github_apps")
    assert row is not None
    assert secret not in row["client_secret_ciphertext"]

    started = client.get("/api/github/connect")
    assert started.status_code == 200
    url = started.json()["url"]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "github.com"
    assert query["client_id"] == ["Iv1.abc123"]
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]

    done = client.get(
        "/api/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/?github=connected"
    assert "ghu_user_token" not in done.headers["location"]
    assert seen == {"exchange": 1, "user": 1}
    account = client.get("/api/account").json()
    owner = next(user for user in account["users"] if user["id"] == account["current_user_id"])
    assert owner["github_login"] == "octocat"

    added = client.post("/api/account/users", json={"name": "Grace"}).json()
    grace = next(user for user in added["users"] if user["name"] == "Grace")
    _become(tmp_path, grace["id"])
    again = client.get("/api/github/connect")
    grace_state = parse_qs(urlparse(again.json()["url"]).query)["state"][0]
    replay = client.get(
        "/api/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert replay.status_code == 303
    assert replay.headers["location"] == "/?github=expired"
    finished = client.get(
        "/api/github/callback",
        params={"code": "the-code", "state": grace_state},
        follow_redirects=False,
    )
    assert finished.headers["location"] == "/?github=connected"
    fresh = client.get("/api/account").json()
    by_name = {user["name"]: user["github_login"] for user in fresh["users"]}
    assert by_name[owner["name"]] == "octocat"
    assert by_name["Grace"] == "gracehub"

    cleared = client.delete("/api/github/login")
    assert cleared.status_code == 200
    after = {user["name"]: user["github_login"] for user in cleared.json()["users"]}
    assert after["Grace"] == ""
    assert after[owner["name"]] == "octocat"


def test_sync_keeps_a_renamed_team_and_copies_repository_access(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    state = {
        "name": "Platform",
        "repos": [
            {"full_name": "acme/demo", "permissions": {"push": True, "pull": True}},
        ],
        "teams": 200,
    }

    def fake_request(method: str, url: str, headers: dict | None = None, timeout: float = 20):
        assert headers is not None
        path = url.split("?", 1)[0]
        if method == "POST" and path.endswith("/access_tokens"):
            return _Response(200, {"token": "ghs_secret"})
        if method == "GET" and path.endswith("/app/installations"):
            return _Response(
                200,
                [{"id": 42, "account": {"login": "acme", "type": "Organization"}}],
            )
        if method == "GET" and path.endswith("/app"):
            return _Response(200, {"slug": "lbw-local"})
        if "/installation/repositories" in path:
            return _Response(200, {"repositories": [{"full_name": "acme/demo", "private": True}]})
        if "/teams/" in path and path.endswith("/repos"):
            return _Response(200, state["repos"])
        if "/teams/" in path and "role=maintainer" in url:
            return _Response(200, [])
        if "/teams/" in path and "role=member" in url:
            return _Response(200, [{"login": "ada"}])
        if path.endswith("/teams"):
            if state["teams"] != 200:
                return _Response(state["teams"], {})
            return _Response(200, [{"name": state["name"], "slug": "platform"}])
        if "role=admin" in url:
            return _Response(200, [{"login": "ada"}])
        if "role=member" in url:
            return _Response(200, [])
        if path.endswith("/collaborators"):
            return _Response(
                200,
                [{"login": "ada", "permissions": {"admin": True, "push": True, "pull": True}}],
            )
        return _Response(404, {})

    def fake_clone(url, parent, *, name=None, git_config=None):
        dest = Path(parent) / (name or "demo")
        dest.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", url],
            cwd=dest,
            check=True,
            capture_output=True,
        )
        return {"path": str(dest), "name": dest.name}

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fake_request)
    monkeypatch.setattr("agent_lbw.github_http.clone_workspace", fake_clone)
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    saved = client.put("/api/github/app", json={"app_id": "123", "private_key": _pem()})
    assert saved.status_code == 200
    account = client.get("/api/account").json()
    owner = next(user for user in account["users"] if user["id"] == account["current_user_id"])
    with database(tmp_path) as db:
        link_github_login(db, user_id=owner["id"], github_login="ada")
    parent = tmp_path / "checkouts"
    placed = client.post(
        "/api/github/workspaces",
        json={
            "full_name": "acme/demo",
            "subject_type": "tenant",
            "subject_id": account["org"]["id"],
            "parent": str(parent),
        },
    )
    assert placed.status_code == 200

    def team_rows():
        with database(tmp_path) as db:
            return db.fetchall("SELECT id, name, github_slug FROM teams ORDER BY name")

    def team_access():
        with database(tmp_path) as db:
            return db.fetchall(
                """
                SELECT access FROM project_grants
                WHERE subject_type = 'team'
                ORDER BY access
                """
            )

    original = team_rows()
    assert [(row["name"], row["github_slug"]) for row in original] == [("Platform", "platform")]
    assert [row["access"] for row in team_access()] == ["use"]

    state["name"] = "Platforms"
    renamed = client.post("/api/github/sync")
    assert renamed.status_code == 200
    assert "ghs_secret" not in renamed.text
    again = team_rows()
    assert [(row["id"], row["name"], row["github_slug"]) for row in again] == [
        (original[0]["id"], "Platforms", "platform")
    ]
    assert renamed.json()["teams"] == [{"name": "Platforms", "members": ["ada"]}]

    state["repos"] = [
        {
            "full_name": "acme/demo",
            "permissions": {"admin": True, "maintain": True, "push": True, "pull": True},
        }
    ]
    admin = client.post("/api/github/sync")
    assert admin.status_code == 200
    assert [row["access"] for row in team_access()] == ["manage"]

    state["repos"] = [
        {"full_name": f"acme/r{index}", "permissions": {"pull": True}} for index in range(100)
    ]
    partial = client.post("/api/github/sync")
    assert partial.status_code == 200
    assert "partial" in partial.json()["summary"]
    assert [row["access"] for row in team_access()] == ["manage"]

    state["teams"] = 500
    failed = client.post("/api/github/sync")
    assert failed.status_code == 502
    assert [row["access"] for row in team_access()] == ["manage"]
    assert team_rows()[0]["id"] == original[0]["id"]

    state["teams"] = 200
    state["repos"] = []
    removed = client.post("/api/github/sync")
    assert removed.status_code == 200
    assert team_access() == []
    kept = team_rows()
    assert [(row["id"], row["github_slug"]) for row in kept] == [(original[0]["id"], "platform")]


def test_github_sign_in_opens_a_session_for_a_stored_login(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    secret = "lbw-client-secret-value"
    login = {"name": "octocat"}

    def fake_request(method: str, url: str, headers: dict | None = None, timeout: float = 20):
        if method == "GET" and url.endswith("/app"):
            return _Response(200, {"slug": "lbw-local", "client_id": "Iv1.abc123"})
        if method == "GET" and url.endswith("/app/installations"):
            return _Response(200, [])
        if method == "GET" and url.endswith("/user"):
            return _Response(200, {"login": login["name"]})
        return _Response(404, {})

    def fake_post(
        url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 20
    ):
        assert data is not None
        assert data["client_secret"] == secret
        return _Response(200, {"access_token": "ghu_user_token", "token_type": "bearer"})

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fake_request)
    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.post", fake_post)
    app = create_app(TaskManager(settings), default_workspace=tmp_path)
    owner = TestClient(app)
    created = owner.post(
        "/api/auth/setup",
        json={"name": "Ada", "email": "ada@example.com", "password": "correct-horse"},
    )
    assert created.status_code == 200
    ada = next(user for user in created.json()["users"] if user["role"] == "owner")
    saved = owner.put(
        "/api/github/app",
        json={"app_id": "123", "private_key": _pem(), "client_secret": secret},
    )
    assert saved.status_code == 200
    assert secret not in saved.text
    with database(tmp_path) as db:
        link_github_login(db, user_id=ada["id"], github_login="octocat")
        before = db.fetchone("SELECT COUNT(*) AS n FROM identities")

    guest = TestClient(app)
    started = guest.get("/api/auth/github", follow_redirects=False)
    assert started.status_code == 303
    parsed = urlparse(started.headers["location"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "github.com"
    assert secret not in started.headers["location"]
    done = guest.get(
        "/api/github/callback",
        params={"code": "the-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/?github=connected"
    assert "ghu_user_token" not in done.headers["location"]
    assert "lbw_session" in done.cookies
    signed = guest.get("/api/account")
    assert signed.status_code == 200
    assert signed.json()["current_user_id"] == ada["id"]

    login["name"] = "stranger"
    unknown = TestClient(app)
    again = unknown.get("/api/auth/github", follow_redirects=False)
    stranger = parse_qs(urlparse(again.headers["location"]).query)["state"][0]
    refused = unknown.get(
        "/api/github/callback",
        params={"code": "the-code", "state": stranger},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == "/?github=email"
    assert "lbw_session" not in refused.cookies
    with database(tmp_path) as db:
        after = db.fetchone("SELECT COUNT(*) AS n FROM identities")
    assert after["n"] == before["n"]

    password = TestClient(app)
    still = password.post(
        "/api/auth/login",
        json={"email": "ada@example.com", "password": "correct-horse"},
    )
    assert still.status_code == 200
    assert still.json()["current_user_id"] == ada["id"]


def test_github_sign_in_matches_a_verified_email_and_stores_the_login(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    secret = "lbw-client-secret-value"

    def fake_request(method: str, url: str, headers: dict | None = None, timeout: float = 20):
        if method == "GET" and url.endswith("/app"):
            return _Response(200, {"slug": "lbw-local", "client_id": "Iv1.abc123"})
        if method == "GET" and url.endswith("/app/installations"):
            return _Response(200, [])
        if method == "GET" and url.endswith("/user/emails"):
            return _Response(
                200,
                [{"email": "ada@example.com", "primary": True, "verified": True}],
            )
        if method == "GET" and url.endswith("/user"):
            return _Response(200, {"login": "ada-gh"})
        return _Response(404, {})

    def fake_post(
        url: str, data: dict | None = None, headers: dict | None = None, timeout: float = 20
    ):
        return _Response(200, {"access_token": "ghu_user_token", "token_type": "bearer"})

    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", fake_request)
    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.post", fake_post)
    app = create_app(TaskManager(settings), default_workspace=tmp_path)
    owner = TestClient(app)
    created = owner.post(
        "/api/auth/setup",
        json={"name": "Ada", "email": "ada@example.com", "password": "correct-horse"},
    )
    assert created.status_code == 200
    ada = next(user for user in created.json()["users"] if user["role"] == "owner")
    saved = owner.put(
        "/api/github/app",
        json={"app_id": "123", "private_key": _pem(), "client_secret": secret},
    )
    assert saved.status_code == 200
    owner.post("/api/auth/logout")

    guest = TestClient(app)
    started = guest.get("/api/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    done = guest.get(
        "/api/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/?github=connected"
    assert "lbw_session" in done.cookies
    signed = guest.get("/api/account")
    assert signed.status_code == 200
    assert signed.json()["current_user_id"] == ada["id"]
    person = next(user for user in signed.json()["users"] if user["id"] == ada["id"])
    assert person["github_login"] == "ada-gh"
