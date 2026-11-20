"""A person's sealed credential for one endpoint: their linked integration.

An organization key bills everyone together. An endpoint an owner or admin
sets to personal keys or sign-in is an integration each person links on their
profile: a personal key, a gateway sign-in, or for Claude a linked Claude
organization (``claude_link``). It is sealed for that person, and a run they
start or continue at a gate uses it. The ordinary sign-in token is not stored.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.sso import begin_access, finish_access, refresh_access
from agent_lbw.runtime.store.tasks import tenant_dek

CLAUDE_LINK = "claude_link"


def start_gateway_sign_in(db: Database, root: Path, endpoint_id: str, *, redirect_uri: str) -> str:
    """Send this person to the organization provider for this gateway."""
    account = require_account(db)
    row = _member_endpoint(db, account["org"]["id"], endpoint_id)
    if row["provider"] not in {"litellm", "gateway"}:
        raise KeyError(endpoint_id)
    return begin_access(
        db,
        root,
        redirect_uri=redirect_uri,
        audience=str(row["audience"] or ""),
        endpoint_id=row["id"],
    )


def complete_gateway_sign_in(
    db: Database,
    root: Path,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> str:
    """Seal the access token for the signed-in person. The token is not returned."""
    account = require_account(db)
    issued = finish_access(db, root, code=code, state=state, redirect_uri=redirect_uri)
    endpoint_id = str(issued["endpoint_id"] or "")
    if not endpoint_id:
        raise ValueError("this sign-in could not be checked")
    row = _member_endpoint(db, account["org"]["id"], endpoint_id)
    if row["provider"] not in {"litellm", "gateway"}:
        raise KeyError(endpoint_id)
    expected = _email(account)
    if not expected or expected.casefold() != str(issued["email"]).casefold():
        raise ValueError("sign in with the same email you use here")
    remember_gateway_login(
        db,
        root,
        endpoint_id,
        access_token=str(issued["access_token"]),
        refresh_token=str(issued["refresh_token"] or ""),
        expires_in=int(issued["expires_in"]),
    )
    return endpoint_id


def remember_gateway_login(
    db: Database,
    root: Path,
    endpoint_id: str,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> None:
    """Store this person's gateway token. A later sign-in replaces it."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    row = _member_endpoint(db, tenant_id, endpoint_id)
    if row["provider"] not in {"litellm", "gateway"}:
        raise KeyError(endpoint_id)
    _store(
        db,
        root,
        tenant_id,
        endpoint_id,
        account["current_user_id"],
        secret=access_token,
        refresh=refresh_token,
        expires_at=int(time.time()) + max(int(expires_in), 60),
        kind="sso",
    )


def save_personal_key(db: Database, root: Path, endpoint_id: str, *, api_key: str) -> None:
    """Seal a key this person already checked. It replaces their previous credential."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    row = _member_endpoint(db, tenant_id, endpoint_id)
    if row["auth"] != "personal":
        raise ValueError("this endpoint uses one organization key")
    _store(
        db,
        root,
        tenant_id,
        endpoint_id,
        account["current_user_id"],
        secret=api_key,
        refresh="",
        expires_at=0,
        kind="key",
    )


class LinkRequired(PermissionError):
    """The person acting has not linked the integration this model runs on."""

    def __init__(self, row: dict) -> None:
        super().__init__(_missing(row))
        self.endpoint_id = str(row["id"])
        self.integration = str(row.get("name") or "")
        self.provider = str(row.get("provider") or "")

    def body(self) -> dict:
        return {
            "error": str(self),
            "code": "link_required",
            "endpoint_id": self.endpoint_id,
            "integration": self.integration,
            "provider": self.provider,
        }


def gateway_bearer(db: Database, root: Path, endpoint_id: str) -> str:
    """The current person's key or access token. A token is refreshed near expiry."""
    return str(person_credential(db, root, endpoint_id)["api_key"])


def person_credential(db: Database, root: Path, endpoint_id: str) -> dict:
    """The current person's credential for a run: a key or token, and any link headers.

    Raises LinkRequired when they have not linked this integration.
    """
    account = require_account(db)
    tenant_id = account["org"]["id"]
    row = _member_endpoint(db, tenant_id, endpoint_id)
    identity_id = account["current_user_id"]
    saved = db.fetchone(
        """
        SELECT access_ciphertext, refresh_ciphertext, expires_at, kind,
               connection_id, profile_id
        FROM endpoint_credentials
        WHERE endpoint_id = ? AND identity_id = ?
        """,
        (endpoint_id, identity_id),
    )
    if saved is None:
        raise LinkRequired(row)
    key = tenant_dek(db, root, tenant_id)
    kind = str(saved["kind"] or "sso")
    if kind == "key":
        return {"api_key": open_secret(key, saved["access_ciphertext"]), "kind": kind}
    if kind == CLAUDE_LINK:
        from agent_lbw.runtime.store.claude_link import link_credential

        try:
            found = link_credential(db, root, tenant_id, endpoint_id, identity_id, saved)
        except PermissionError as exc:
            raise LinkRequired(row) from exc
        return {**found, "kind": kind}
    expires_at = int(saved["expires_at"] or 0)
    if expires_at > int(time.time()) + 60:
        return {"api_key": open_secret(key, saved["access_ciphertext"]), "kind": kind}
    refresh_token = ""
    if saved["refresh_ciphertext"]:
        refresh_token = open_secret(key, saved["refresh_ciphertext"])
    if not refresh_token:
        raise LinkRequired(row)
    try:
        renewed = refresh_access(
            db,
            root,
            refresh_token=refresh_token,
            audience=str(row["audience"] or ""),
        )
    except ValueError as exc:
        raise LinkRequired(row) from exc
    remember_gateway_login(
        db,
        root,
        endpoint_id,
        access_token=str(renewed["access_token"]),
        refresh_token=str(renewed["refresh_token"] or ""),
        expires_in=int(renewed["expires_in"]),
    )
    return {"api_key": str(renewed["access_token"]), "kind": kind}


def unlink(db: Database, root: Path, endpoint_id: str) -> None:
    """Remove the current person's credential for this integration."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    _member_endpoint(db, tenant_id, endpoint_id)
    identity_id = account["current_user_id"]
    saved = db.fetchone(
        """
        SELECT kind, connection_id FROM endpoint_credentials
        WHERE endpoint_id = ? AND identity_id = ?
        """,
        (endpoint_id, identity_id),
    )
    if saved is None:
        return
    if str(saved["kind"] or "") == CLAUDE_LINK:
        from agent_lbw.runtime.store.claude_link import revoke_link

        revoke_link(db, root, tenant_id, str(saved["connection_id"] or ""))
    db.execute(
        "DELETE FROM endpoint_credentials WHERE endpoint_id = ? AND identity_id = ?",
        (endpoint_id, identity_id),
    )


def person_integrations(db: Database) -> list[dict]:
    """The integrations this organization turned on, and how this person linked each."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    claude_ready = (
        db.fetchone("SELECT 1 AS found FROM claude_link_apps WHERE tenant_id = ?", (tenant_id,))
        is not None
    )
    rows = db.fetchall(
        """
        SELECT model_endpoints.id, model_endpoints.name, model_endpoints.provider,
               model_endpoints.auth, endpoint_credentials.kind AS linked_kind,
               endpoint_credentials.expires_at
        FROM model_endpoints
        LEFT JOIN endpoint_credentials
          ON endpoint_credentials.endpoint_id = model_endpoints.id
         AND endpoint_credentials.identity_id = ?
        WHERE model_endpoints.tenant_id = ? AND model_endpoints.auth IN ('sso', 'personal')
        ORDER BY model_endpoints.name, model_endpoints.id
        """,
        (identity_id, tenant_id),
    )
    found: list[dict] = []
    for row in rows:
        gateway = row["provider"] in {"litellm", "gateway"}
        claude = row["provider"] == "anthropic" and row["auth"] == "personal"
        found.append(
            {
                "endpoint_id": row["id"],
                "name": row["name"],
                "provider": row["provider"],
                "auth": row["auth"],
                "linked": row["linked_kind"] is not None,
                "linked_with": str(row["linked_kind"] or ""),
                "can_add_key": row["auth"] == "personal",
                "can_sign_in": gateway,
                "can_link_claude": claude and claude_ready,
            }
        )
    return found


def signed_in(db: Database, endpoint_id: str, identity_id: str) -> bool:
    row = db.fetchone(
        """
        SELECT endpoint_id FROM endpoint_credentials
        WHERE endpoint_id = ? AND identity_id = ?
        """,
        (endpoint_id, identity_id),
    )
    return row is not None


def _store(
    db: Database,
    root: Path,
    tenant_id: str,
    endpoint_id: str,
    identity_id: str,
    *,
    secret: str,
    refresh: str,
    expires_at: int,
    kind: str,
) -> None:
    key = tenant_dek(db, root, tenant_id)
    access = seal(key, secret)
    sealed_refresh = seal(key, refresh) if refresh else None
    db.execute(
        """
        INSERT INTO endpoint_credentials (
            endpoint_id, identity_id, access_ciphertext, refresh_ciphertext, expires_at, kind
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (endpoint_id, identity_id) DO UPDATE SET
            access_ciphertext = excluded.access_ciphertext,
            refresh_ciphertext = excluded.refresh_ciphertext,
            expires_at = excluded.expires_at,
            kind = excluded.kind
        """,
        (endpoint_id, identity_id, access, sealed_refresh, expires_at, kind),
    )


def _member_endpoint(db: Database, tenant_id: str, endpoint_id: str) -> dict:
    row = db.fetchone(
        """
        SELECT id, name, provider, auth, audience
        FROM model_endpoints WHERE id = ? AND tenant_id = ?
        """,
        (endpoint_id, tenant_id),
    )
    if row is None or row["auth"] not in {"sso", "personal"}:
        raise KeyError(endpoint_id)
    return row


def _missing(row: dict) -> str:
    name = str(row.get("name") or "this integration")
    if row["auth"] == "personal" and row["provider"] in {"litellm", "gateway"}:
        return f"link {name} on your profile: add your API key or sign in"
    if row["auth"] == "personal" and row["provider"] == "anthropic":
        return f"link {name} on your profile: add your API key or link your Claude organization"
    if row["auth"] == "personal":
        return f"link {name} on your profile: add your API key"
    return f"link {name} on your profile: sign in to this gateway"


def _email(account: dict) -> str:
    current = account["current_user_id"]
    for user in account.get("users") or []:
        if user.get("id") == current:
            return str(user.get("email") or "").strip()
    return ""
