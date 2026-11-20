"""A person's sealed credential for one endpoint.

An organization key bills everyone together. A personal key or a gateway
sign-in is sealed for the person who checked it, and a run uses that
credential. The ordinary sign-in token is not stored.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.sso import begin_access, finish_access, refresh_access
from agent_lbw.runtime.store.tasks import tenant_dek


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


def gateway_bearer(db: Database, root: Path, endpoint_id: str) -> str:
    """The current person's key or access token. A token is refreshed near expiry."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    row = _member_endpoint(db, tenant_id, endpoint_id)
    identity_id = account["current_user_id"]
    saved = db.fetchone(
        """
        SELECT access_ciphertext, refresh_ciphertext, expires_at, kind
        FROM endpoint_credentials
        WHERE endpoint_id = ? AND identity_id = ?
        """,
        (endpoint_id, identity_id),
    )
    if saved is None:
        raise PermissionError(_missing(row))
    key = tenant_dek(db, root, tenant_id)
    if str(saved["kind"] or "sso") == "key":
        return open_secret(key, saved["access_ciphertext"])
    expires_at = int(saved["expires_at"] or 0)
    if expires_at > int(time.time()) + 60:
        return open_secret(key, saved["access_ciphertext"])
    refresh_token = ""
    if saved["refresh_ciphertext"]:
        refresh_token = open_secret(key, saved["refresh_ciphertext"])
    if not refresh_token:
        raise PermissionError(_missing(row))
    try:
        renewed = refresh_access(
            db,
            root,
            refresh_token=refresh_token,
            audience=str(row["audience"] or ""),
        )
    except ValueError as exc:
        raise PermissionError(_missing(row)) from exc
    remember_gateway_login(
        db,
        root,
        endpoint_id,
        access_token=str(renewed["access_token"]),
        refresh_token=str(renewed["refresh_token"] or ""),
        expires_in=int(renewed["expires_in"]),
    )
    return str(renewed["access_token"])


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
        SELECT id, provider, auth, audience
        FROM model_endpoints WHERE id = ? AND tenant_id = ?
        """,
        (endpoint_id, tenant_id),
    )
    if row is None or row["auth"] not in {"sso", "personal"}:
        raise KeyError(endpoint_id)
    return row


def _missing(row: dict) -> str:
    if row["auth"] == "personal" and row["provider"] in {"litellm", "gateway"}:
        return "add your API key or sign in to this endpoint"
    if row["auth"] == "personal":
        return "add your API key for this endpoint"
    return "sign in to this gateway"


def _email(account: dict) -> str:
    current = account["current_user_id"]
    for user in account.get("users") or []:
        if user.get("id") == current:
            return str(user.get("email") or "").strip()
    return ""
