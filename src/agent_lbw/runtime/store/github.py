"""A tenant's GitHub App. The private key is sealed. Tokens are minted when used."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.tasks import tenant_dek

_API = "https://api.github.com"
_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_TOKEN_TTL = 45 * 60
_CONNECT_TTL = 10 * 60
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98})$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_tokens: dict[tuple[str, str, str], tuple[float, str]] = {}
# state -> (expires, mode, identity, PKCE verifier, tenant). A personal
# connect records the tenant, so the callback finishes only for that account.
_pending: dict[str, tuple] = {}


class GitHubAppError(Exception):
    """GitHub refused the app, or the installation token could not be minted."""


def github_app_view(db: Database) -> dict:
    account = require_account(db)
    if uses_site_app(db, account["org"]["id"]):
        return personal_github_view(db)
    row = _row(db, account["org"]["id"])
    if row is None:
        view = _view("", "", "")
    else:
        view = _view(
            row["app_id"],
            row["installation_id"] or "",
            row["account_login"] or "",
            client_id=str(row["client_id"] or ""),
            has_client_secret=bool(row["client_secret_ciphertext"]),
        )
    view["installations"] = saved_installations(db)
    return view


def saved_installations(db: Database) -> list[dict]:
    """Installations last seen for this app. This does not call GitHub."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    rows = db.fetchall(
        """
        SELECT installation_id, account_login, account_type
        FROM github_installations
        WHERE tenant_id = ?
        ORDER BY account_login, installation_id
        """,
        (tenant_id,),
    )
    found = [_installation(row) for row in rows]
    row = _row(db, tenant_id)
    chosen = "" if row is None else str(row["installation_id"] or "")
    if chosen and not any(item["id"] == chosen for item in found):
        found.insert(
            0,
            {
                "id": chosen,
                "account_login": str(row["account_login"] or ""),
                "account_type": str(row["account_type"] or ""),
            },
        )
    return found


def github_app_state(db: Database, root: Path) -> dict:
    """The saved app plus the install link. Members do not call GitHub."""
    view = github_app_view(db)
    view["install_url"] = ""
    if view["configured"] and _is_manager(db):
        view["install_url"] = install_url(db, root)
    return view


def install_url(db: Database, root: Path) -> str:
    """Where an owner installs this app on a GitHub account or organization."""
    try:
        payload = _app_payload(db, root, _app_tenant(db, require_account(db)["org"]["id"]))
    except (GitHubAppError, ValueError):
        return ""
    slug = str(payload.get("slug") or "")
    if _SLUG.fullmatch(slug) is None:
        return ""
    return f"https://github.com/apps/{slug}/installations/new"


def save_github_app(
    db: Database, root: Path, *, app_id: str, private_key: str, client_secret: str = ""
) -> dict:
    account = _require_manager(db)
    _refuse_personal(db, account)
    clean_id = app_id.strip()
    if not clean_id.isdigit() or len(clean_id) > 20:
        raise ValueError("app id is the number GitHub shows for the app")
    tenant_id = account["org"]["id"]
    existing = _row(db, tenant_id)
    key = private_key.strip()
    if key:
        _load_key(key)
        ciphertext = seal(tenant_dek(db, root, tenant_id), key)
    elif existing is not None and existing["app_id"] == clean_id:
        ciphertext = existing["private_key_ciphertext"]
    else:
        raise ValueError("paste the app's private key")
    secret = _clean_secret(client_secret)
    same_app = existing is not None and existing["app_id"] == clean_id
    if secret:
        secret_ciphertext = seal(tenant_dek(db, root, tenant_id), secret)
    elif same_app and existing["client_secret_ciphertext"]:
        secret_ciphertext = existing["client_secret_ciphertext"]
    else:
        secret_ciphertext = None
    installation_id = existing["installation_id"] if same_app else None
    account_login = existing["account_login"] if same_app else None
    account_type = existing["account_type"] if same_app else None
    client_id = existing["client_id"] if same_app else None
    if not same_app:
        db.execute("DELETE FROM github_installations WHERE tenant_id = ?", (tenant_id,))
    db.execute(
        """
            INSERT INTO github_apps (
                tenant_id, app_id, private_key_ciphertext, installation_id,
                account_login, account_type, client_id, client_secret_ciphertext
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id) DO UPDATE SET
                app_id = excluded.app_id,
                private_key_ciphertext = excluded.private_key_ciphertext,
                installation_id = excluded.installation_id,
                account_login = excluded.account_login,
                account_type = excluded.account_type,
                client_id = excluded.client_id,
                client_secret_ciphertext = excluded.client_secret_ciphertext
            """,
        (
            tenant_id,
            clean_id,
            ciphertext,
            installation_id,
            account_login,
            account_type,
            client_id,
            secret_ciphertext,
        ),
    )
    found: list[dict] = []
    warning = ""
    try:
        found = list_installations(db, root)
    except GitHubAppError as exc:
        warning = str(exc)
    else:
        current = _row(db, tenant_id)
        if current is not None and not current["installation_id"] and len(found) == 1:
            chosen = select_installation(db, root, found[0]["id"])
            warning = str(chosen.get("warning") or "")
    link = "" if warning else install_url(db, root)
    view = github_app_view(db)
    view["install_url"] = link
    if warning:
        view["warning"] = warning
    return view


def restore_github_app(
    db: Database,
    root: Path,
    *,
    app_id: str,
    private_key: str,
    client_secret: str = "",
    client_id: str = "",
    installation_id: str = "",
    account_login: str = "",
    account_type: str = "",
    installations: list[dict] | None = None,
) -> dict:
    """Seal a GitHub App from a local file. This does not call GitHub."""
    account = _require_manager(db)
    clean_id = app_id.strip()
    if not clean_id.isdigit() or len(clean_id) > 20:
        raise ValueError("app id is the number GitHub shows for the app")
    key = private_key.strip()
    if not key:
        raise ValueError("paste the app's private key")
    _load_key(key)
    secret = _clean_secret(client_secret) if client_secret.strip() else ""
    client = client_id.strip()
    if client and _CLIENT_ID.fullmatch(client) is None:
        raise ValueError("that client id is not from the GitHub App")
    chosen = installation_id.strip()
    if chosen and not chosen.isdigit():
        raise ValueError("choose an installation")
    login = account_login.strip()
    kind = account_type.strip()
    found = _clean_installations(installations)
    if chosen and not any(item["id"] == chosen for item in found):
        found.insert(0, {"id": chosen, "account_login": login, "account_type": kind})
    tenant_id = account["org"]["id"]
    dek = tenant_dek(db, root, tenant_id)
    db.execute(
        """
            INSERT INTO github_apps (
                tenant_id, app_id, private_key_ciphertext, installation_id,
                account_login, account_type, client_id, client_secret_ciphertext
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id) DO UPDATE SET
                app_id = excluded.app_id,
                private_key_ciphertext = excluded.private_key_ciphertext,
                installation_id = excluded.installation_id,
                account_login = excluded.account_login,
                account_type = excluded.account_type,
                client_id = excluded.client_id,
                client_secret_ciphertext = excluded.client_secret_ciphertext
            """,
        (
            tenant_id,
            clean_id,
            seal(dek, key),
            chosen or None,
            login or None,
            kind or None,
            client or None,
            seal(dek, secret) if secret else None,
        ),
    )
    _replace_installations(db, tenant_id, found)
    return github_app_view(db)


def list_installations(db: Database, root: Path) -> list[dict]:
    account = _require_manager(db)
    _refuse_personal(db, account)
    creds = _credentials(db, root, account["org"]["id"])
    payload = _github_json("GET", "/app/installations", jwt=_jwt(creds))
    if not isinstance(payload, list):
        raise GitHubAppError("GitHub did not list installations")
    found: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        owner = item.get("account") if isinstance(item.get("account"), dict) else {}
        installation_id = str(item.get("id") or "")
        if not installation_id:
            continue
        found.append(
            {
                "id": installation_id,
                "account_login": str(owner.get("login") or ""),
                "account_type": str(owner.get("type") or ""),
            }
        )
    found.sort(key=lambda item: (item["account_login"].casefold(), item["id"]))
    _replace_installations(db, account["org"]["id"], found)
    chosen = {item["id"] for item in found}
    current = _row(db, account["org"]["id"])
    if (
        current is not None
        and current["installation_id"]
        and current["installation_id"] not in chosen
    ):
        db.execute(
            """
            UPDATE github_apps
            SET installation_id = NULL, account_login = NULL, account_type = NULL
            WHERE tenant_id = ?
            """,
            (account["org"]["id"],),
        )
    return found


def select_installation(db: Database, root: Path, installation_id: str) -> dict:
    account = _require_manager(db)
    _refuse_personal(db, account)
    chosen_id = installation_id.strip()
    if not chosen_id.isdigit():
        raise ValueError("choose an installation")
    choices = {item["id"]: item for item in list_installations(db, root)}
    chosen = choices.get(chosen_id)
    if chosen is None:
        raise ValueError("that installation is not on this app")
    db.execute(
        """
        UPDATE github_apps
        SET installation_id = ?, account_login = ?, account_type = ?
        WHERE tenant_id = ?
        """,
        (
            chosen_id,
            chosen["account_login"] or None,
            chosen["account_type"] or None,
            account["org"]["id"],
        ),
    )
    from agent_lbw.runtime.store.github_sync import sync_access

    try:
        sync_access(db, root)
    except FileNotFoundError:
        view = github_app_view(db)
        view["warning"] = "the app is saved; matching a checkout needs git on this service"
        return view
    return github_app_view(db)


def installation_token(db: Database, root: Path) -> str | None:
    """Mint a short-lived installation token. Nothing stores it."""
    from agent_lbw.runtime.store.install import read_account

    account = read_account(db)
    if account is None:
        return None
    row = _row(db, account["org"]["id"])
    if row is None or not row["installation_id"]:
        return None
    creds = _credentials(db, root, account["org"]["id"])
    cache_key = (
        str(Path(root).resolve()),
        str(row["installation_id"]),
        str(row["private_key_ciphertext"])[:24],
    )
    cached = _tokens.get(cache_key)
    now = time.time()
    if cached is not None and cached[0] > now:
        return cached[1]
    payload = _github_json(
        "POST",
        f"/app/installations/{row['installation_id']}/access_tokens",
        jwt=_jwt(creds),
    )
    if not isinstance(payload, dict) or not payload.get("token"):
        raise GitHubAppError("GitHub did not return an installation token")
    token = str(payload["token"])
    _tokens[cache_key] = (now + _TOKEN_TTL, token)
    return token


def begin_user_connect(db: Database, root: Path, *, redirect_uri: str) -> str:
    """The GitHub page where the current person authorizes this app."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    client_id, _secret = _user_client(db, root, tenant_id)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _pending[state] = (time.time() + _CONNECT_TTL, "link", account["current_user_id"], verifier)
    _drop_expired()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTHORIZE}?{query}"


def begin_personal_connect(db: Database, root: Path, *, redirect_uri: str) -> str:
    """The GitHub page where a personal account's owner signs in to connect.

    The callback reads which installations of LBW's app this person can reach
    and keeps the one on their own GitHub account. It never takes one by id.
    """
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if not uses_site_app(db, tenant_id):
        raise PermissionError("an organization connects its own GitHub App")
    client_id, _secret = _user_client(db, root, tenant_id)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _pending[state] = (
        time.time() + _CONNECT_TTL,
        "personal",
        account["current_user_id"],
        verifier,
        tenant_id,
    )
    _drop_expired()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTHORIZE}?{query}"


def begin_github_login(db: Database, root: Path, *, redirect_uri: str) -> str:
    """Start GitHub sign-in for a login that is already stored. No session is required."""
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    if install is None or not install["tenant_id"]:
        raise ValueError("create the admin account first")
    client_id, _secret = _user_client(db, root, str(install["tenant_id"]))
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _pending[state] = (time.time() + _CONNECT_TTL, "login", "", verifier)
    _drop_expired()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTHORIZE}?{query}"


def finish_user_connect(
    db: Database, root: Path, *, code: str, state: str, redirect_uri: str
) -> dict:
    """Read the GitHub login once. Linking stores it. Signing in opens a session."""
    from agent_lbw.runtime.store.auth import open_session_for_github_account
    from agent_lbw.runtime.store.github_sync import link_github_login, refresh_access

    pending = _pending.pop(state, None)
    if pending is None or pending[0] < time.time() or len(pending) < 4:
        raise ValueError("that GitHub connection expired. Try again.")
    _expiry, mode, identity_id, verifier = pending[:4]
    if mode == "personal":
        return _finish_personal_connect(
            db,
            root,
            code=code,
            redirect_uri=redirect_uri,
            identity_id=identity_id,
            verifier=verifier,
            tenant_id=str(pending[4]),
        )
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    if install is None or not install["tenant_id"]:
        raise ValueError("create the admin account first")
    client_id, secret = _user_client(db, root, str(install["tenant_id"]))
    token = _exchange_code(
        client_id=client_id,
        client_secret=secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    try:
        payload = _request("GET", "/user", auth=f"Bearer {token}")
        emails = _github_account_emails(token, payload if isinstance(payload, dict) else {})
    finally:
        del token
    if not isinstance(payload, dict) or not payload.get("login"):
        raise GitHubAppError("GitHub did not complete that sign-in")
    login = str(payload["login"])
    if mode == "login":
        return {
            "mode": "login",
            "session": open_session_for_github_account(db, github_login=login, emails=emails),
        }
    if not identity_id:
        raise ValueError("that GitHub connection expired. Try again.")
    linked = link_github_login(db, user_id=identity_id, github_login=login)
    refresh_access(db, root)
    return linked


def _finish_personal_connect(
    db: Database,
    root: Path,
    *,
    code: str,
    redirect_uri: str,
    identity_id: str,
    verifier: str,
    tenant_id: str,
) -> dict:
    """Keep the installation of LBW's app on this person's own GitHub account.

    Only an installation on the signed-in GitHub user's own account counts.
    An organization's installation is never taken here, even when this
    person can reach it: an organization connects its own app.
    """
    from agent_lbw.runtime.store.github_sync import link_github_login, sync_access

    account = require_account(db)
    if account["org"]["id"] != tenant_id or account["current_user_id"] != identity_id:
        raise ValueError("that GitHub connection expired. Try again.")
    client_id, secret = _user_client(db, root, tenant_id)
    token = _exchange_code(
        client_id=client_id,
        client_secret=secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    try:
        user = _request("GET", "/user", auth=f"Bearer {token}")
        reachable = _user_installations(token)
    finally:
        del token
    if not isinstance(user, dict) or not user.get("login"):
        raise GitHubAppError("GitHub did not complete that sign-in")
    login = str(user["login"])
    own = next(
        (
            item
            for item in reachable
            if item["account_login"].casefold() == login.casefold()
            and item["account_type"] == "User"
        ),
        None,
    )
    if own is None:
        # Not installed on their account yet: send them to install it. GitHub
        # returns here with this state and a new sign-in code once it is, and
        # that return finishes the connection for this person and account only.
        url = install_url(db, root)
        if not url:
            raise GitHubAppError("this LBW site's GitHub App cannot be installed right now")
        state = secrets.token_urlsafe(24)
        _pending[state] = (time.time() + _CONNECT_TTL, "personal", identity_id, "", tenant_id)
        return {"mode": "personal", "install_url": f"{url}?{urlencode({'state': state})}"}
    shared = _app_tenant(db, tenant_id)
    app_row = _row(db, shared)
    db.execute(
        """
        INSERT INTO github_apps (
            tenant_id, app_id, private_key_ciphertext, installation_id,
            account_login, account_type, client_id, client_secret_ciphertext
        )
        VALUES (?, ?, '', ?, ?, 'User', NULL, NULL)
        ON CONFLICT (tenant_id) DO UPDATE SET
            app_id = excluded.app_id,
            private_key_ciphertext = '',
            installation_id = excluded.installation_id,
            account_login = excluded.account_login,
            account_type = 'User',
            client_id = NULL,
            client_secret_ciphertext = NULL
        """,
        (tenant_id, app_row["app_id"], own["id"], own["account_login"]),
    )
    _replace_installations(db, tenant_id, [own])
    link_github_login(db, user_id=identity_id, github_login=login)
    synced = sync_access(db, root)
    return {"mode": "personal", "account_login": own["account_login"], "sync": synced}


def _user_installations(token: str) -> list[dict]:
    """Installations of this app the token's user can reach, from GitHub."""
    found: list[dict] = []
    path = "/user/installations?per_page=100"
    for _page in range(10):
        body, link = _call("GET", path, auth=f"Bearer {token}")
        items = body.get("installations") if isinstance(body, dict) else None
        for item in items if isinstance(items, list) else []:
            owner = item.get("account") if isinstance(item, dict) else None
            if not isinstance(owner, dict) or not item.get("id"):
                continue
            found.append(
                {
                    "id": str(item["id"]),
                    "account_login": str(owner.get("login") or ""),
                    "account_type": str(owner.get("type") or ""),
                }
            )
        next_url = _next_link(link)
        if not next_url:
            break
        path = next_url
    return found


def _next_link(link: str) -> str:
    for part in link.split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return ""


def uses_site_app(db: Database, tenant_id: str) -> bool:
    """Whether this tenant connects GitHub through this LBW site's own app.

    That is a personal account someone signed up for. The install's own
    account is personal too, but it holds the site's app, so it keeps its own.
    """
    row = db.fetchone("SELECT kind FROM tenants WHERE id = ?", (tenant_id,))
    if row is None or row["kind"] != "personal":
        return False
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    return install is None or str(install["tenant_id"] or "") != tenant_id


def shared_app_tenant(db: Database) -> str | None:
    """The tenant whose GitHub App personal accounts use: this site's own app.

    It is the install's first tenant, and it must hold the app's private key
    and client secret, as an LBW engineer saves them.
    """
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    if install is None or not install["tenant_id"]:
        return None
    row = _row(db, str(install["tenant_id"]))
    if row is None or not row["private_key_ciphertext"] or not row["client_secret_ciphertext"]:
        return None
    return str(install["tenant_id"])


def personal_github_view(db: Database) -> dict:
    """A personal account's GitHub: its own installation of this site's app."""
    account = require_account(db)
    row = _row(db, account["org"]["id"])
    shared = shared_app_tenant(db) is not None
    view = _view(
        "",
        str(row["installation_id"] or "") if row is not None else "",
        str(row["account_login"] or "") if row is not None else "",
    )
    view.update(
        {
            "personal": True,
            "configured": shared,
            "installations": [],
            "account_type": "User",
        }
    )
    return view


def _app_tenant(db: Database, tenant_id: str) -> str:
    """Whose app credentials this tenant uses: its own, or the site's for a personal one."""
    if uses_site_app(db, tenant_id):
        shared = shared_app_tenant(db)
        if shared is None:
            raise ValueError(
                "this LBW site has no GitHub App for personal accounts yet: "
                "an LBW engineer sets it up"
            )
        return shared
    return tenant_id


def _refuse_personal(db: Database, account: dict) -> None:
    """The app's installation list shows every account it is on, so a
    personal account never sees or picks from it."""
    if uses_site_app(db, account["org"]["id"]):
        raise PermissionError("a personal account connects GitHub with Connect GitHub")


def current_token(root: Path, database_url: str = "") -> str | None:
    from agent_lbw.runtime.db import database

    with database(root, database_url) as db:
        return installation_token(db, root)


def _view(
    app_id: str,
    installation_id: str,
    account_login: str,
    *,
    client_id: str = "",
    has_client_secret: bool = False,
) -> dict:
    return {
        "configured": bool(app_id),
        "app_id": app_id,
        "installation_id": installation_id,
        "account_login": account_login,
        "client_id": client_id,
        "has_client_secret": has_client_secret,
    }


def _installation(row: dict) -> dict:
    return {
        "id": str(row["installation_id"]),
        "account_login": str(row["account_login"] or ""),
        "account_type": str(row["account_type"] or ""),
    }


def _clean_installations(items: list[dict] | None) -> list[dict]:
    if not items:
        return []
    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("an installation needs an id")
        installation_id = str(item.get("id") or "").strip()
        if not installation_id.isdigit():
            raise ValueError("an installation id is a number")
        found.append(
            {
                "id": installation_id,
                "account_login": str(item.get("account_login") or "").strip(),
                "account_type": str(item.get("account_type") or "").strip(),
            }
        )
    return found


def _replace_installations(db: Database, tenant_id: str, found: list[dict]) -> None:
    db.execute("DELETE FROM github_installations WHERE tenant_id = ?", (tenant_id,))
    for item in found:
        db.execute(
            """
            INSERT INTO github_installations (
                tenant_id, installation_id, account_login, account_type
            ) VALUES (?, ?, ?, ?)
            """,
            (tenant_id, item["id"], item["account_login"], item["account_type"]),
        )


def require_github_manager(db: Database) -> dict:
    return _require_manager(db)


def github_manager(db: Database) -> bool:
    return _is_manager(db)


def github_installation(db: Database, tenant_id: str):
    return _row(db, tenant_id)


def _row(db: Database, tenant_id: str):
    return db.fetchone(
        """
        SELECT app_id, private_key_ciphertext, installation_id, account_login, account_type,
               client_id, client_secret_ciphertext
        FROM github_apps WHERE tenant_id = ?
        """,
        (tenant_id,),
    )


def _is_manager(db: Database) -> bool:
    account = require_account(db)
    current = next(
        (user for user in account["users"] if user["id"] == account["current_user_id"]),
        None,
    )
    return current is not None and current["role"] in {"owner", "admin"}


def _require_manager(db: Database) -> dict:
    account = require_account(db)
    if not _is_manager(db):
        raise PermissionError("an owner or admin connects the GitHub App")
    return account


def _app_payload(db: Database, root: Path, tenant_id: str) -> dict:
    creds = _credentials(db, root, tenant_id)
    payload = _github_json("GET", "/app", jwt=_jwt(creds))
    if not isinstance(payload, dict):
        raise GitHubAppError("GitHub did not return the app")
    client_id = str(payload.get("client_id") or "")
    if _CLIENT_ID.fullmatch(client_id):
        db.execute(
            "UPDATE github_apps SET client_id = ? WHERE tenant_id = ?",
            (client_id, tenant_id),
        )
    return payload


def _user_client(db: Database, root: Path, tenant_id: str) -> tuple[str, str]:
    tenant_id = _app_tenant(db, tenant_id)
    row = _row(db, tenant_id)
    if row is None or not row["client_secret_ciphertext"]:
        raise ValueError("paste the GitHub App client secret before people can connect")
    client_id = str(row["client_id"] or "")
    if _CLIENT_ID.fullmatch(client_id) is None:
        payload = _app_payload(db, root, tenant_id)
        client_id = str(payload.get("client_id") or "")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise ValueError("GitHub did not return a client id for this app")
    secret = open_secret(tenant_dek(db, root, tenant_id), row["client_secret_ciphertext"])
    return client_id, secret


def _clean_secret(value: str) -> str:
    secret = value.strip()
    if not secret:
        return ""
    if "BEGIN" in secret or len(secret) > 200 or any(char.isspace() for char in secret):
        raise ValueError("paste the client secret from the GitHub App")
    if len(secret) < 8:
        raise ValueError("paste the client secret from the GitHub App")
    return secret


def _exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    # A sign-in GitHub asks for right after an install has no PKCE challenge.
    if code_verifier:
        data["code_verifier"] = code_verifier
    try:
        response = httpx.post(
            _TOKEN_URL,
            data=data,
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise GitHubAppError("GitHub could not be reached") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubAppError("GitHub did not complete that sign-in") from exc
    token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise GitHubAppError("GitHub did not complete that sign-in")
    return token


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _drop_expired() -> None:
    now = time.time()
    for key, pending in list(_pending.items()):
        if pending[0] < now:
            _pending.pop(key, None)


def _credentials(db: Database, root: Path, tenant_id: str) -> tuple[str, str]:
    tenant_id = _app_tenant(db, tenant_id)
    row = _row(db, tenant_id)
    if row is None:
        raise ValueError("save the GitHub App id and private key first")
    return row["app_id"], open_secret(
        tenant_dek(db, root, tenant_id), row["private_key_ciphertext"]
    )


def _jwt(creds: tuple[str, str], *, now: int | None = None) -> str:
    app_id, private_key = creds
    issued = int(time.time()) if now is None else now
    header = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64({"iat": issued - 60, "exp": issued + 8 * 60, "iss": int(app_id)})
    signing = f"{header}.{body}".encode()
    signature = _load_key(private_key).sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64_bytes(signature)}"


def _load_key(private_key: str):
    try:
        return serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("private key could not be read") from exc


def github_api(method: str, path: str, *, token: str) -> object:
    """Call GitHub with an installation token. The token is not returned."""
    body, _link = _call(method, path, auth=f"Bearer {token}")
    return body


def github_api_page(method: str, path: str, *, token: str) -> tuple[object, str]:
    """One GitHub page and its Link header. A next link stays on api.github.com."""
    return _call(method, path, auth=f"Bearer {token}")


def _github_account_emails(token: str, profile: dict) -> list[str]:
    """Emails GitHub shared for this account. A missing permission leaves the list short."""
    found: list[str] = []
    public = str(profile.get("email") or "").strip()
    if public:
        found.append(public)
    try:
        response = httpx.request(
            "GET",
            _api_url("/user/emails"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
    except httpx.HTTPError:
        return found
    if response.status_code >= 400:
        return found
    try:
        payload = response.json()
    except ValueError:
        return found
    if not isinstance(payload, list):
        return found
    primary: list[str] = []
    rest: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("verified"):
            continue
        email = str(item.get("email") or "").strip()
        if not email:
            continue
        if item.get("primary"):
            primary.append(email)
        else:
            rest.append(email)
    for email in primary + rest:
        if email not in found:
            found.append(email)
    return found


def _github_json(method: str, path: str, *, jwt: str) -> object:
    body, _link = _call(method, path, auth=f"Bearer {jwt}")
    return body


def github_download(path: str, *, token: str, limit: int = 2_000_000) -> str:
    """Text GitHub serves through a redirect, such as an Actions job log.

    GitHub answers with a short-lived download link on another host. The token
    is sent to GitHub only, never to that host. Returns at most `limit` bytes,
    from the end, where a failing step's output is.
    """
    try:
        first = httpx.get(
            _api_url(path),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
            follow_redirects=False,
        )
        if first.status_code in {401, 403, 404, 410}:
            raise GitHubAppError(
                "GitHub would not share that log. Grant the app Actions read."
                if first.status_code == 403
                else f"GitHub returned {first.status_code} for that log"
            )
        if first.status_code in {301, 302, 303, 307, 308}:
            target = str(first.headers.get("location") or "")
            if not target.startswith("https://"):
                raise GitHubAppError("GitHub returned an unreadable response")
            response = httpx.get(target, timeout=30, follow_redirects=False)
        else:
            response = first
    except httpx.HTTPError as exc:
        raise GitHubAppError("GitHub could not be reached") from exc
    if response.status_code >= 400:
        raise GitHubAppError(f"GitHub returned {response.status_code} for that log")
    data = response.content[-limit:]
    return data.decode("utf-8", errors="replace")


def _request(method: str, path: str, *, auth: str) -> object:
    body, _link = _call(method, path, auth=auth)
    return body


def _call(method: str, path: str, *, auth: str) -> tuple[object, str]:
    try:
        response = httpx.request(
            method,
            _api_url(path),
            headers={
                "Authorization": auth,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise GitHubAppError("GitHub could not be reached") from exc
    if response.status_code == 401:
        raise GitHubAppError("GitHub refused the app credentials")
    if response.status_code == 403:
        raise GitHubAppError(
            "GitHub refused that request. Grant the app Contents and Members read."
        )
    if response.status_code >= 400:
        raise GitHubAppError(f"GitHub returned {response.status_code}")
    headers = getattr(response, "headers", None)
    link = ""
    if headers is not None and hasattr(headers, "get"):
        link = str(headers.get("link") or headers.get("Link") or "")
    try:
        return response.json(), link
    except ValueError as exc:
        raise GitHubAppError("GitHub returned an unreadable response") from exc


def _api_url(path: str) -> str:
    if path.startswith("https://"):
        if path != _API and not path.startswith(f"{_API}/"):
            raise GitHubAppError("GitHub returned an unreadable response")
        return path
    if not path.startswith("/"):
        raise GitHubAppError("GitHub returned an unreadable response")
    return f"{_API}{path}"


def _b64(payload: dict) -> str:
    return _b64_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _b64_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
