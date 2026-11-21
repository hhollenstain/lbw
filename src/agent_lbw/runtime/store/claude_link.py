"""Link a person's Claude organization with Claude Platform OAuth (beta).

Anthropic does not let other apps use a Claude Pro or Max subscription, so a
Claude account is linked through the API: a personal Anthropic API key, or
this flow, where the person approves LBW on Anthropic's page and their Claude
organization pays for the runs they start.

An owner or admin registers LBW once as an OAuth app in the Claude Console and
saves three things here: the app's client ID, the path of a JWT file from the
identity provider the app trusts (for example a Kubernetes projected service
account token), and an API key from LBW's own Claude organization, used only to
create the Anthropic user profile that stands for each person. Anthropic issues
no client secret; every token request carries that JWT as a client assertion.

The person then picks "Link your Claude organization" on their profile. LBW
sends a pushed authorization request with PKCE, the person approves on
auth.claude.com (they need the Admin or Billing role in their Claude
organization), and the callback exchanges the code for a connection token and
refresh token. Both are sealed with the tenant key. A run sends the connection
token as a bearer token with the person's user profile and
``bill_to: user_profile``, so Anthropic bills only that organization.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.tasks import tenant_dek

AUTH_BASE = "https://auth.claude.com"
API_BASE = "https://api.anthropic.com"
BILLING_BETA = "user-profiles-billing-2026-10-01"
SCOPE = "billing:attribution offline_access"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
CALLBACK_PATH = "/api/integrations/claude/callback"
PENDING_SECONDS = 600
KIND = "claude_link"
_TIMEOUT = 20.0


def app_view(db: Database) -> dict:
    """Whether this organization can link Claude organizations. No secret is returned."""
    account = require_account(db)
    row = _app(db, account["org"]["id"])
    return {
        "configured": row is not None,
        "client_id": str(row["client_id"]) if row else "",
        "assertion_file": str(row["assertion_file"]) if row else "",
        "has_profile_key": bool(row and row["profile_key_ciphertext"]),
        "callback_path": CALLBACK_PATH,
        "can_manage": can_manage_org(account),
    }


def save_app(
    db: Database,
    root: Path,
    *,
    client_id: str,
    assertion_file: str,
    profile_api_key: str = "",
) -> dict:
    """Store the OAuth app. An empty profile key keeps the one already saved."""
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin sets up the Claude link")
    tenant_id = account["org"]["id"]
    chosen = client_id.strip()
    path = assertion_file.strip()
    if not chosen or len(chosen) > 200:
        raise ValueError("paste the OAuth app's client ID from the Claude Console")
    if not path or not path.startswith("/") or len(path) > 500:
        raise ValueError("give the absolute path of the identity provider's JWT file")
    current = _app(db, tenant_id)
    key = tenant_dek(db, root, tenant_id)
    if profile_api_key.strip():
        sealed = seal(key, profile_api_key.strip())
    elif current is not None:
        sealed = str(current["profile_key_ciphertext"])
    else:
        raise ValueError("add an API key from LBW's Claude organization to create user profiles")
    db.execute(
        """
        INSERT INTO claude_link_apps (
            tenant_id, client_id, assertion_file, profile_key_ciphertext, updated
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (tenant_id) DO UPDATE SET
            client_id = excluded.client_id,
            assertion_file = excluded.assertion_file,
            profile_key_ciphertext = excluded.profile_key_ciphertext,
            updated = excluded.updated
        """,
        (tenant_id, chosen, path, sealed, int(time.time())),
    )
    return app_view(db)


def remove_app(db: Database) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin sets up the Claude link")
    db.execute("DELETE FROM claude_link_apps WHERE tenant_id = ?", (account["org"]["id"],))
    return app_view(db)


def begin_link(db: Database, root: Path, endpoint_id: str, *, redirect_uri: str) -> str:
    """Start linking this person's Claude organization. Returns the approval page URL."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    _claude_endpoint(db, tenant_id, endpoint_id)
    app = _app(db, tenant_id)
    if app is None:
        raise ValueError("an owner or admin has not set up the Claude link yet")
    key = tenant_dek(db, root, tenant_id)
    profile_id = _ensure_profile(db, key, app, tenant_id, identity_id, _name(account))
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    db.execute(
        "DELETE FROM link_pending WHERE created < ?",
        (int(time.time()) - PENDING_SECONDS,),
    )
    db.execute(
        """
        INSERT INTO link_pending (
            state, tenant_id, identity_id, endpoint_id, verifier_ciphertext, created
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (state, tenant_id, identity_id, endpoint_id, seal(key, verifier), int(time.time())),
    )
    response = _post(
        f"{AUTH_BASE}/v1/oauth/par",
        {
            "response_type": "code",
            "client_id": app["client_id"],
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "connection_type": "organization_shared",
            "connection_auth_target": "platform",
            "user_profile_id": profile_id,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": _assertion(app),
        },
    )
    request_uri = str(response.get("request_uri") or "")
    if not request_uri:
        raise ValueError("Claude did not start the link")
    query = urlencode({"client_id": app["client_id"], "request_uri": request_uri})
    return f"{AUTH_BASE}/oauth/authorize?{query}"


def finish_link(
    db: Database,
    root: Path,
    *,
    code: str,
    state: str,
    error: str = "",
    redirect_uri: str,
) -> str:
    """Check the callback, exchange the code, and seal the connection's tokens."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    pending = db.fetchone(
        """
        SELECT state, tenant_id, identity_id, endpoint_id, verifier_ciphertext, created
        FROM link_pending WHERE state = ?
        """,
        (state,),
    )
    if pending is None or not hmac.compare_digest(str(pending["state"]), state):
        raise ValueError("this Claude link was not started here, or it expired")
    db.execute("DELETE FROM link_pending WHERE state = ?", (state,))
    if pending["tenant_id"] != tenant_id or pending["identity_id"] != identity_id:
        raise ValueError("finish the Claude link as the person who started it")
    if int(pending["created"]) < int(time.time()) - PENDING_SECONDS:
        raise ValueError("this Claude link expired; start it again")
    if error:
        raise ValueError(_link_error(error))
    if not code:
        raise ValueError("Claude did not return a code")
    endpoint_id = str(pending["endpoint_id"])
    _claude_endpoint(db, tenant_id, endpoint_id)
    app = _app(db, tenant_id)
    if app is None:
        raise ValueError("the Claude link was turned off")
    key = tenant_dek(db, root, tenant_id)
    tokens = _post(
        f"{AUTH_BASE}/v1/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": open_secret(key, pending["verifier_ciphertext"]),
            "redirect_uri": redirect_uri,
            "client_id": app["client_id"],
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": _assertion(app),
        },
    )
    profile = db.fetchone(
        "SELECT profile_id FROM claude_profiles WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )
    _store_tokens(
        db,
        key,
        endpoint_id=endpoint_id,
        identity_id=identity_id,
        tokens=tokens,
        connection_id=str(tokens.get("connection_id") or ""),
        profile_id=str(profile["profile_id"]) if profile else "",
    )
    return endpoint_id


def link_credential(
    db: Database, root: Path, tenant_id: str, endpoint_id: str, identity_id: str, saved: dict
) -> dict:
    """The connection token for a run, refreshed when it is near expiry.

    Returns the bearer token and the headers and body that bill the linked
    organization.
    """
    key = tenant_dek(db, root, tenant_id)
    access = open_secret(key, saved["access_ciphertext"])
    if int(saved["expires_at"] or 0) <= int(time.time()) + 120:
        refresh = (
            open_secret(key, saved["refresh_ciphertext"]) if saved["refresh_ciphertext"] else ""
        )
        app = _app(db, tenant_id)
        if not refresh or app is None:
            raise PermissionError("link your Claude organization again")
        try:
            tokens = _post(
                f"{AUTH_BASE}/v1/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": app["client_id"],
                    "client_assertion_type": ASSERTION_TYPE,
                    "client_assertion": _assertion(app),
                },
            )
        except ValueError as exc:
            raise PermissionError("link your Claude organization again") from exc
        _store_tokens(
            db,
            key,
            endpoint_id=endpoint_id,
            identity_id=identity_id,
            tokens=tokens,
            connection_id=str(saved.get("connection_id") or ""),
            profile_id=str(saved.get("profile_id") or ""),
        )
        access = str(tokens["access_token"])
    headers = {"anthropic-beta": BILLING_BETA}
    profile_id = str(saved.get("profile_id") or "")
    if profile_id:
        headers["anthropic-user-profile-id"] = profile_id
    return {
        "api_key": access,
        "link": {
            "headers": headers,
            "body": {"billing_policy": {"type": "billing_policy", "bill_to": "user_profile"}},
        },
    }


def revoke_link(db: Database, root: Path, tenant_id: str, connection_id: str) -> None:
    """End the connection at Anthropic. A failure is ignored; the local row goes anyway."""
    app = _app(db, tenant_id)
    if app is None or not connection_id:
        return
    try:
        _post(
            f"{AUTH_BASE}/v1/oauth/revoke",
            {
                "connection_id": connection_id,
                "client_id": app["client_id"],
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": _assertion(app),
            },
        )
    except (ValueError, OSError):
        return


def _store_tokens(
    db: Database,
    key: bytes,
    *,
    endpoint_id: str,
    identity_id: str,
    tokens: dict,
    connection_id: str,
    profile_id: str,
) -> None:
    access = str(tokens.get("access_token") or "")
    if not access:
        raise ValueError("Claude did not return a token")
    refresh = str(tokens.get("refresh_token") or "")
    expires_in = int(tokens.get("expires_in") or 3600)
    db.execute(
        """
        INSERT INTO endpoint_credentials (
            endpoint_id, identity_id, access_ciphertext, refresh_ciphertext, expires_at,
            kind, connection_id, profile_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (endpoint_id, identity_id) DO UPDATE SET
            access_ciphertext = excluded.access_ciphertext,
            refresh_ciphertext = excluded.refresh_ciphertext,
            expires_at = excluded.expires_at,
            kind = excluded.kind,
            connection_id = excluded.connection_id,
            profile_id = excluded.profile_id
        """,
        (
            endpoint_id,
            identity_id,
            seal(key, access),
            seal(key, refresh) if refresh else None,
            int(time.time()) + max(min(expires_in, 3600), 60),
            KIND,
            connection_id,
            profile_id,
        ),
    )


def _ensure_profile(
    db: Database, key: bytes, app: dict, tenant_id: str, identity_id: str, name: str
) -> str:
    found = db.fetchone(
        "SELECT profile_id FROM claude_profiles WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )
    if found is not None:
        return str(found["profile_id"])
    body: dict = {"external_user_details": {"reference_id": identity_id}}
    if name:
        body["name"] = name
    response = httpx.post(
        f"{API_BASE}/v1/user_profiles",
        headers={
            "x-api-key": open_secret(key, app["profile_key_ciphertext"]),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=_TIMEOUT,
    )
    payload = _json(response)
    profile_id = str(payload.get("id") or "")
    if not profile_id.startswith("uprof_"):
        raise ValueError("Claude did not create a user profile")
    db.execute(
        "INSERT INTO claude_profiles (tenant_id, identity_id, profile_id) VALUES (?, ?, ?)",
        (tenant_id, identity_id, profile_id),
    )
    return profile_id


def _post(url: str, body: dict) -> dict:
    """One OAuth call. Refresh is not retried: a reused refresh token ends the link."""
    try:
        response = httpx.post(url, json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ValueError(f"Claude could not be reached: {exc}") from exc
    return _json(response)


def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            else:
                detail = str(payload.get("error_description") or error or "")
        raise ValueError(f"Claude refused the request ({response.status_code}) {detail}".strip())
    return payload if isinstance(payload, dict) else {}


def _assertion(app: dict) -> str:
    path = Path(str(app["assertion_file"]))
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"the identity token file {path} could not be read") from exc
    if token.count(".") != 2:
        raise ValueError(f"the identity token file {path} does not hold a JWT")
    return token


def _app(db: Database, tenant_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT client_id, assertion_file, profile_key_ciphertext
        FROM claude_link_apps WHERE tenant_id = ?
        """,
        (tenant_id,),
    )


def _claude_endpoint(db: Database, tenant_id: str, endpoint_id: str) -> dict:
    row = db.fetchone(
        "SELECT id, provider, auth FROM model_endpoints WHERE id = ? AND tenant_id = ?",
        (endpoint_id, tenant_id),
    )
    if row is None or row["provider"] != "anthropic" or row["auth"] != "personal":
        raise KeyError(endpoint_id)
    return row


def _name(account: dict) -> str:
    for user in account.get("users") or []:
        if user.get("id") == account["current_user_id"]:
            return str(user.get("name") or "").strip()[:200]
    return ""


def _link_error(error: str) -> str:
    known = {
        "access_denied": "the link was not approved",
        "user_profile_already_linked": "this profile is already linked; unlink it first",
    }
    return known.get(error, f"Claude did not link: {error[:80]}")
