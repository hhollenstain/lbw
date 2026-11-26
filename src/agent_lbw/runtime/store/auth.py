"""Sign-in for this install.

The first password belongs to an owner and is the admin account. Later people
sign in with the email stored on their identity. The password is hashed. The
session cookie is random; only its hash is stored. A session belongs to the
person and the organization they signed in to. Someone can create another
organization from the sign-in page. A later SSO or SAML login from Entra,
Auth0, or another provider matches that same email and opens a session the
same way.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextvars import ContextVar, Token

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.names import clean_name, new_id, slug_for

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_SESSION_TTL = 14 * 24 * 60 * 60
_LOCK_AFTER = 5
_actor: ContextVar[str | None] = ContextVar("lbw_actor", default=None)
_tenant: ContextVar[str | None] = ContextVar("lbw_tenant", default=None)


def set_actor(identity_id: str) -> Token[str | None]:
    return _actor.set(identity_id)


def reset_actor(token: Token[str | None]) -> None:
    _actor.reset(token)


def current_actor() -> str | None:
    return _actor.get()


def set_tenant(tenant_id: str) -> Token[str | None]:
    return _tenant.set(tenant_id)


def reset_tenant(token: Token[str | None]) -> None:
    _tenant.reset(token)


def current_tenant() -> str | None:
    return _tenant.get()


def has_password(db: Database) -> bool:
    row = db.fetchone("SELECT id FROM identities WHERE password_hash IS NOT NULL LIMIT 1")
    return row is not None


def auth_status(db: Database) -> dict:
    ready = has_password(db)
    row = db.fetchone(
        """
        SELECT sign_in.provider
        FROM sign_in
        JOIN install ON install.tenant_id = sign_in.tenant_id
        WHERE install.id = 1
        """
    )
    provider = str(row["provider"]) if row is not None else "password"
    app = db.fetchone(
        """
        SELECT github_apps.client_id
        FROM github_apps
        JOIN install ON install.tenant_id = github_apps.tenant_id
        WHERE install.id = 1
        """
    )
    github = bool(app is not None and str(app["client_id"] or "").strip())
    return {
        "setup": not ready,
        "signed_in": current_actor() is not None,
        "provider": provider,
        "github": github,
    }


def claim_admin(db: Database, *, name: str, email: str, password: str) -> str:
    """Give the owner an email and password. This is the first admin account."""
    if has_password(db):
        raise ValueError("an admin account already exists")
    from agent_lbw.runtime.store.install import require_account

    account = require_account(db)
    owner = next((user for user in account["users"] if user["role"] == "owner"), None)
    if owner is None:
        raise ValueError("this install has no owner")
    clean_email = _email(email)
    _available(db, clean_email, owner["id"])
    db.execute(
        """
        UPDATE identities
        SET display_name = ?, email = ?, password_hash = ?, crossing = 1
        WHERE id = ?
        """,
        (clean_name(name, "name"), clean_email, hash_password(password), owner["id"]),
    )
    return open_session(db, owner["id"], account["org"]["id"])


def login(db: Database, *, email: str, password: str) -> str:
    clean_email = _email(email)
    rows = db.fetchall(
        """
        SELECT id, email, password_hash, locked
        FROM identities WHERE password_hash IS NOT NULL
        """
    )
    match = next(
        (row for row in rows if str(row["email"] or "").casefold() == clean_email),
        None,
    )
    if match is not None and int(match["locked"] or 0):
        raise ValueError("this account is locked")
    if match is None or not verify_password(password, str(match["password_hash"])):
        if match is not None:
            _note_failure(db, str(match["id"]))
        raise ValueError("email or password is wrong")
    db.execute(
        "UPDATE identities SET failed_attempts = 0 WHERE id = ?",
        (match["id"],),
    )
    return open_session(db, str(match["id"]), _session_tenant(db, str(match["id"])))


def _note_failure(db: Database, identity_id: str) -> None:
    row = db.fetchone(
        "SELECT failed_attempts, locked FROM identities WHERE id = ?",
        (identity_id,),
    )
    if row is None or int(row["locked"] or 0):
        return
    count = int(row["failed_attempts"] or 0) + 1
    locked = 1 if count >= _LOCK_AFTER else 0
    db.execute(
        "UPDATE identities SET failed_attempts = ?, locked = ? WHERE id = ?",
        (count, locked, identity_id),
    )
    db.commit()


def identity_for_session(db: Database, token: str) -> str | None:
    found = session_context(db, token)
    if found is None:
        return None
    return found[0]


def session_context(db: Database, token: str) -> tuple[str, str] | None:
    if not token:
        return None
    row = db.fetchone(
        "SELECT identity_id, tenant_id, expires FROM sessions WHERE id = ?",
        (_digest(token),),
    )
    if row is None or int(row["expires"]) < int(time.time()):
        return None
    identity_id = str(row["identity_id"])
    tenant_id = str(row["tenant_id"] or "")
    if not tenant_id:
        tenant_id = _session_tenant(db, identity_id)
    return identity_id, tenant_id


def close_session(db: Database, token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE id = ?", (_digest(token),))


def open_session_for_email(db: Database, *, tenant_id: str, email: str) -> str:
    """Open a session for a person who already belongs to this organization."""
    try:
        clean = _email(email)
    except ValueError as exc:
        raise ValueError("email or password is wrong") from exc
    rows = db.fetchall(
        """
        SELECT identities.id, identities.email, identities.locked
        FROM identities
        JOIN tenant_memberships ON tenant_memberships.identity_id = identities.id
        WHERE tenant_memberships.tenant_id = ? AND identities.email IS NOT NULL
        """,
        (tenant_id,),
    )
    match = next((row for row in rows if str(row["email"]).casefold() == clean), None)
    if match is None:
        raise ValueError("email or password is wrong")
    if int(match["locked"] or 0):
        raise ValueError("this account is locked")
    return open_session(db, str(match["id"]), tenant_id)


def open_session_for_github(db: Database, *, github_login: str) -> str:
    """Open a session for a stored GitHub login. An unknown login adds nobody."""
    wanted = github_login.strip().casefold()
    rows = db.fetchall(
        """
        SELECT id, github_login, locked
        FROM identities
        WHERE github_login IS NOT NULL AND github_login <> ''
        """
    )
    match = next(
        (row for row in rows if str(row["github_login"]).casefold() == wanted),
        None,
    )
    if match is None:
        raise ValueError("email or password is wrong")
    if int(match["locked"] or 0):
        raise ValueError("this account is locked")
    identity_id = str(match["id"])
    return open_session(db, identity_id, _session_tenant(db, identity_id))


def open_session_for_github_account(db: Database, *, github_login: str, emails: list[str]) -> str:
    """Open a session for a stored login, or for a GitHub email already on a person."""
    login = github_login.strip()
    if _GITHUB_LOGIN.fullmatch(login) is None:
        raise ValueError("GitHub did not complete that sign-in")
    try:
        return open_session_for_github(db, github_login=login)
    except ValueError as exc:
        if str(exc) != "email or password is wrong":
            raise
    wanted: list[str] = []
    for email in emails:
        try:
            clean = _email(email)
        except ValueError:
            continue
        if clean not in wanted:
            wanted.append(clean)
    if not wanted:
        raise ValueError("GitHub did not share an email for that account")
    rows = db.fetchall(
        """
        SELECT id, email, locked, github_login
        FROM identities
        WHERE email IS NOT NULL
        """
    )
    match = next((row for row in rows if str(row["email"]).casefold() in wanted), None)
    if match is None:
        raise ValueError("that GitHub account is not a person here")
    if int(match["locked"] or 0):
        raise ValueError("this account is locked")
    stored = str(match["github_login"] or "")
    if stored and stored.casefold() != login.casefold():
        raise ValueError("that GitHub login is already connected")
    if not stored:
        db.execute(
            "UPDATE identities SET github_login = ? WHERE id = ?",
            (login, match["id"]),
        )
    identity_id = str(match["id"])
    return open_session(db, identity_id, _session_tenant(db, identity_id))


def open_session(db: Database, identity_id: str, tenant_id: str) -> str:
    now = int(time.time())
    db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    raw = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions (id, identity_id, tenant_id, expires) VALUES (?, ?, ?, ?)",
        (_digest(raw), identity_id, tenant_id, now + _SESSION_TTL),
    )
    return raw


def open_organization(
    db: Database, *, name: str, email: str, password: str, organization: str
) -> str:
    """Create an organization and its owner. The owner is not an LBW engineer."""
    return _open_account(
        db,
        name=name,
        email=email,
        password=password,
        kind="organization",
        tenant_name=clean_name(organization, "organization"),
    )


def open_personal_account(db: Database, *, name: str, email: str, password: str) -> str:
    """Create a personal account: one person and their own repositories.

    It has its owner and no teams. Adding a team promotes it to an organization
    first, which keeps its id, so its projects and jobs stay put.
    """
    person = clean_name(name, "name")
    return _open_account(
        db, name=name, email=email, password=password, kind="personal", tenant_name=person
    )


def _open_account(
    db: Database, *, name: str, email: str, password: str, kind: str, tenant_name: str
) -> str:
    if not has_password(db):
        raise ValueError("create the admin account first")
    clean_email = _email(email)
    _password(password)
    _available(db, clean_email, "")
    person = clean_name(name, "name")
    user_id = new_id("user")
    tenant_id = new_id("org")
    now = int(time.time())
    db.execute(
        """
        INSERT INTO identities (id, email, display_name, password_hash, crossing, created)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (user_id, clean_email, person, hash_password(password), now),
    )
    db.execute(
        """
        INSERT INTO tenants (id, kind, name, slug, mode)
        VALUES (?, ?, ?, ?, 'local')
        """,
        (tenant_id, kind, tenant_name, slug_for(tenant_name, tenant_id)),
    )
    db.execute(
        """
        INSERT INTO tenant_memberships (tenant_id, identity_id, rank, created)
        VALUES (?, ?, 'owner', ?)
        """,
        (tenant_id, user_id, now),
    )
    return open_session(db, user_id, tenant_id)


def add_crossing_guard(db: Database, *, name: str, email: str, password: str) -> str:
    """Mark an LBW engineer. An organization rank stays as it is."""
    clean_email = _email(email)
    _password(password)
    rows = db.fetchall("SELECT id, email FROM identities")
    match = next(
        (row for row in rows if str(row["email"] or "").casefold() == clean_email),
        None,
    )
    digest = hash_password(password)
    if match is not None:
        db.execute(
            "UPDATE identities SET password_hash = ?, crossing = 1 WHERE id = ?",
            (digest, match["id"]),
        )
        return str(match["id"])
    user_id = new_id("user")
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    if install is None:
        raise ValueError("this install has no account")
    now = int(time.time())
    db.execute(
        """
        INSERT INTO identities (id, email, display_name, password_hash, crossing, created)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (user_id, clean_email, clean_name(name, "name"), digest, now),
    )
    db.execute(
        """
        INSERT INTO tenant_memberships (tenant_id, identity_id, rank, created)
        VALUES (?, ?, 'member', ?)
        """,
        (install["tenant_id"], user_id, now),
    )
    return user_id


def _session_tenant(db: Database, identity_id: str) -> str:
    rows = db.fetchall(
        """
        SELECT tenant_id FROM tenant_memberships
        WHERE identity_id = ? ORDER BY created
        """,
        (identity_id,),
    )
    if not rows:
        raise ValueError("this person is not in an organization")
    return str(rows[-1]["tenant_id"])


def hash_password(password: str) -> str:
    secret = _password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 200:
        raise ValueError("password must be 200 characters or fewer")
    return password


def _email(value: str) -> str:
    email = value.strip().casefold()
    if _EMAIL.fullmatch(email) is None or len(email) > 200:
        raise ValueError("email should look like ada@example.com")
    return email


def assign_email(db: Database, identity_id: str, email: str) -> str:
    clean = _email(email) if email.strip() else ""
    if clean:
        _available(db, clean, identity_id)
    db.execute(
        "UPDATE identities SET email = ? WHERE id = ?",
        (clean or None, identity_id),
    )
    return clean


def _available(db: Database, email: str, identity_id: str) -> None:
    taken = db.fetchall("SELECT id, email FROM identities WHERE email IS NOT NULL")
    for row in taken:
        if row["id"] != identity_id and str(row["email"]).casefold() == email:
            raise ValueError("that email is already used")
