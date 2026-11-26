"""A person without an organization signs up and works on their own repositories.

A personal account uses this LBW site's GitHub App. It connects only the
installation on the person's own GitHub account, verified when they sign in
with GitHub, never one picked from the app's list of every installation.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.db import database
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app

_PASSWORD = "correct-horse-battery"
_SECRET = "lbw-site-client-secret"


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


class _Response:
    def __init__(self, status: int, payload: object, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class _FakeGitHub:
    """GitHub as the site's app sees it. `reachable` is what /user/installations lists."""

    def __init__(self, reachable: list[dict]) -> None:
        self.reachable = reachable
        self.tokens_for: list[str] = []

    def request(self, method: str, url: str, headers: dict | None = None, timeout: float = 20):
        if method == "GET" and url.endswith("/app"):
            return _Response(200, {"slug": "lbw-site", "client_id": "Iv1.site123"})
        if method == "GET" and url.endswith("/user"):
            assert headers is not None and headers["Authorization"] == "Bearer ghu_lin"
            return _Response(200, {"login": "Lin"})
        if method == "GET" and "/user/installations" in url:
            assert headers is not None and headers["Authorization"] == "Bearer ghu_lin"
            return _Response(200, {"installations": self.reachable})
        if method == "POST" and url.endswith("/access_tokens"):
            self.tokens_for.append(url.split("/installations/")[1].split("/")[0])
            return _Response(200, {"token": "ghs_installation"})
        if "/installation/repositories" in url:
            return _Response(
                200,
                {
                    "repositories": [
                        {"id": 11, "full_name": "lin/private-notes", "private": True},
                        {"id": 12, "full_name": "lin/site", "private": False},
                    ]
                },
            )
        if "/collaborators" in url:
            return _Response(200, [])
        return _Response(404, {})

    def post(self, url: str, data: dict | None = None, headers: dict | None = None, timeout=20):
        assert data is not None and data["client_secret"] == _SECRET
        return _Response(200, {"access_token": "ghu_lin", "token_type": "bearer"})


def _site(settings: Settings, tmp_path: Path, monkeypatch, github: _FakeGitHub, *, app: bool):
    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.request", github.request)
    monkeypatch.setattr("agent_lbw.runtime.store.github.httpx.post", github.post)
    web = create_app(TaskManager(settings), default_workspace=tmp_path)
    operator = TestClient(web)
    made = operator.post(
        "/api/auth/setup",
        json={"name": "Ada", "email": "ada@example.com", "password": _PASSWORD},
    )
    assert made.status_code == 200, made.text
    if app:
        saved = operator.put(
            "/api/github/app",
            json={"app_id": "123", "private_key": _pem(), "client_secret": _SECRET},
        )
        assert saved.status_code == 200, saved.text
    person = TestClient(web)
    joined = person.post(
        "/api/auth/signup",
        json={"name": "Lin", "email": "lin@example.com", "password": _PASSWORD, "kind": "personal"},
    )
    assert joined.status_code == 200, joined.text
    return operator, person, joined.json()


def _connect(person: TestClient) -> str:
    started = person.get("/api/github/personal/connect")
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["url"]).query)
    assert query["client_id"] == ["Iv1.site123"]
    done = person.get(
        "/api/github/callback",
        params={"code": "the-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert done.status_code == 303
    return done.headers["location"]


def test_a_person_signs_up_for_their_own_account(settings: Settings, tmp_path: Path) -> None:
    web = create_app(TaskManager(settings), default_workspace=tmp_path)
    operator = TestClient(web)
    operator.post(
        "/api/auth/setup",
        json={"name": "Ada", "email": "ada@example.com", "password": _PASSWORD},
    )
    joined = TestClient(web).post(
        "/api/auth/signup",
        json={"name": "Lin", "email": "lin@example.com", "password": _PASSWORD, "kind": "personal"},
    )
    assert joined.status_code == 200, joined.text
    account = joined.json()
    assert (account["org"]["kind"], account["org"]["name"]) == ("personal", "Lin")
    owner = next(user for user in account["users"] if user["id"] == account["current_user_id"])
    assert owner["role"] == "owner"
    odd = TestClient(web).post(
        "/api/auth/signup",
        json={"name": "Max", "email": "max@example.com", "password": _PASSWORD, "kind": "club"},
    )
    assert odd.status_code == 400
    # Leaving the kind out still creates an organization, as before.
    org = TestClient(web).post(
        "/api/auth/signup",
        json={
            "name": "Mina",
            "email": "mina@example.com",
            "password": _PASSWORD,
            "organization": "Naval",
        },
    )
    assert org.json()["org"]["kind"] == "organization"


def test_a_personal_account_connects_its_own_github_and_its_private_repos(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    github = _FakeGitHub(
        [
            # Lin can reach an organization's installation too. It is never taken here.
            {"id": 7, "account": {"login": "acme", "type": "Organization"}},
            {"id": 9, "account": {"login": "lin", "type": "User"}},
        ]
    )
    _operator, person, account = _site(settings, tmp_path, monkeypatch, github, app=True)

    view = person.get("/api/github/app").json()
    assert (view["personal"], view["configured"], view["account_login"]) == (True, True, "")
    # The app's installation list shows every account it is on: never to a personal account.
    assert person.get("/api/github/installations").status_code == 403
    assert person.put("/api/github/installation", json={"installation_id": "7"}).status_code == 403
    assert (
        person.put("/api/github/app", json={"app_id": "1", "private_key": _pem()}).status_code
        == 403
    )

    assert _connect(person) == "/?section=organization&tab=github&github=connected"
    assert github.tokens_for and set(github.tokens_for) == {"9"}
    connected = person.get("/api/github/app").json()
    assert (connected["account_login"], connected["installation_id"]) == ("lin", "9")
    repos = {item["full_name"]: item for item in person.get("/api/github/repos").json()["repos"]}
    assert set(repos) == {"lin/private-notes", "lin/site"}
    # Lin chose these on GitHub's install page, so they start on.
    assert all(item["enabled"] for item in repos.values())
    me = next(user for user in person.get("/api/account").json()["users"])
    assert me["github_login"] == "Lin"

    with database(tmp_path) as db:
        row = db.fetchone("SELECT * FROM github_apps WHERE tenant_id = ?", (account["org"]["id"],))
        stored = " ".join(str(value) for value in dict(row).values())
    assert row["installation_id"] == "9" and row["private_key_ciphertext"] == ""
    assert "ghu_lin" not in stored and "ghs_installation" not in stored


def test_without_the_app_on_their_account_they_are_sent_to_install_it(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    github = _FakeGitHub([{"id": 7, "account": {"login": "acme", "type": "Organization"}}])
    _operator, person, _account = _site(settings, tmp_path, monkeypatch, github, app=True)
    sent = urlparse(_connect(person))
    assert (sent.scheme, sent.netloc, sent.path) == (
        "https",
        "github.com",
        "/apps/lbw-site/installations/new",
    )
    assert github.tokens_for == []
    assert person.get("/api/github/app").json()["installation_id"] == ""

    # Lin installs the app on their own account. GitHub sends them back with
    # the state from the install link and a new sign-in code.
    github.reachable.append({"id": 9, "account": {"login": "lin", "type": "User"}})
    state = parse_qs(sent.query)["state"][0]
    back = person.get(
        "/api/github/callback",
        params={"code": "after-install", "state": state, "installation_id": "9"},
        follow_redirects=False,
    )
    assert back.headers["location"] == "/?section=organization&tab=github&github=connected"
    assert person.get("/api/github/app").json()["installation_id"] == "9"
    # The state is used once.
    again = person.get(
        "/api/github/callback",
        params={"code": "after-install", "state": state},
        follow_redirects=False,
    )
    assert again.headers["location"] == "/?github=expired"


def test_an_organization_keeps_its_own_app_and_a_site_without_one_says_so(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    github = _FakeGitHub([])
    operator, person, _account = _site(settings, tmp_path, monkeypatch, github, app=False)
    assert operator.get("/api/github/personal/connect").status_code == 403
    missing = person.get("/api/github/personal/connect")
    assert missing.status_code == 400
    assert "no GitHub App for personal accounts" in missing.json()["error"]
    assert person.get("/api/github/app").json()["configured"] is False
