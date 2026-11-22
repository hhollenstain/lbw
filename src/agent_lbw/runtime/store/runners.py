"""Runners: a person's own machines that run jobs with their local Claude Code.

A runner links to one person with a device code, the same way a TV links to an
account: the runner shows a short code, the person types it on their profile,
and the runner then receives a token of its own. Only a hash of that token is
stored. A runner only makes outbound requests, so it works behind NAT and
needs no open port.

A job a person starts, or continues at a gate, goes to that person's runner.
It never runs on a coworker's machine, for the same reason a run never spends
a coworker's credential.

Prompts and logs are not stored here. The job row holds ids, status, and times.
The job's goal and the runner's events pass through memory (see runner_hub).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from uuid import uuid4

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.install import require_account

TOKEN_PREFIX = "lbwr_"
RUNNER_PREFIX = "/api/runner/"
LINK_SECONDS = 10 * 60
POLL_SECONDS = 5
ONLINE_SECONDS = 90
LEASE_SECONDS = 120
COMMANDS = frozenset({"update_claude"})
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_NAME_LIMIT = 80
_FACT_LIMIT = 120


class RunnerRequired(PermissionError):
    """This person has no runner online, so their job cannot run on their machine."""

    def __init__(self, message: str = "") -> None:
        super().__init__(
            message
            or "connect a runner on your profile and start it: this job runs on your own machine"
        )

    def body(self) -> dict:
        return {"error": str(self), "code": "runner_required"}


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clip(value: object, limit: int = _FACT_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _user_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _clean_code(code: str) -> str:
    raw = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    if len(raw) != 8:
        raise ValueError("enter the 8-character code the runner shows")
    return f"{raw[:4]}-{raw[4:]}"


# Linking ---------------------------------------------------------------------


def start_link(db: Database, *, name: str, platform: str = "") -> dict:
    """Begin linking a runner. Returns the secret device code and the code to show."""
    clean = _clip(name, _NAME_LIMIT)
    if not clean:
        raise ValueError("a runner needs a name")
    now = _now()
    db.execute("DELETE FROM runner_links WHERE created < ?", (now - LINK_SECONDS,))
    device_code = secrets.token_urlsafe(32)
    user_code = _user_code()
    db.execute(
        """
        INSERT INTO runner_links (device_hash, user_code, name, platform, created)
        VALUES (?, ?, ?, ?, ?)
        """,
        (_hash(device_code), user_code, clean, _clip(platform), now),
    )
    return {
        "device_code": device_code,
        "user_code": user_code,
        "expires_in": LINK_SECONDS,
        "interval": POLL_SECONDS,
    }


def approve_link(db: Database, user_code: str) -> dict:
    """The signed-in person claims the runner showing this code."""
    account = require_account(db)
    code = _clean_code(user_code)
    row = db.fetchone(
        "SELECT device_hash, name, platform, created, approved FROM runner_links "
        "WHERE user_code = ?",
        (code,),
    )
    if row is None or int(row["created"]) < _now() - LINK_SECONDS:
        raise ValueError("that code has expired or is wrong: run `lbw-runner login` again")
    if row["approved"]:
        raise ValueError("that code was already used")
    db.execute(
        "UPDATE runner_links SET tenant_id = ?, identity_id = ?, approved = ? "
        "WHERE device_hash = ?",
        (account["org"]["id"], account["current_user_id"], _now(), row["device_hash"]),
    )
    return {"name": row["name"], "platform": row["platform"]}


def claim_link(db: Database, device_code: str) -> dict | None:
    """The runner collects its token once the person approves. None while waiting."""
    device_hash = _hash(str(device_code or ""))
    row = db.fetchone(
        "SELECT name, platform, created, tenant_id, identity_id, approved "
        "FROM runner_links WHERE device_hash = ?",
        (device_hash,),
    )
    if row is None or int(row["created"]) < _now() - LINK_SECONDS:
        raise ValueError("this link expired: run `lbw-runner login` again")
    if not row["approved"]:
        return None
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    runner_id = uuid4().hex
    db.execute(
        """
        INSERT INTO runners (id, tenant_id, identity_id, name, token_hash, platform, created)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            runner_id,
            row["tenant_id"],
            row["identity_id"],
            row["name"],
            _hash(token),
            row["platform"],
            _now(),
        ),
    )
    db.execute("DELETE FROM runner_links WHERE device_hash = ?", (device_hash,))
    return {
        "token": token,
        "runner_id": runner_id,
        "tenant_id": row["tenant_id"],
        "identity_id": row["identity_id"],
        "name": row["name"],
    }


# Runner authentication and health ---------------------------------------------


def runner_for_token(db: Database, token: str) -> dict:
    """The runner a bearer token belongs to. A revoked or unknown token is refused."""
    clean = str(token or "").strip()
    if not clean.startswith(TOKEN_PREFIX):
        raise PermissionError("this runner is not linked")
    row = db.fetchone(
        "SELECT id, tenant_id, identity_id, name, platform, command, revoked "
        "FROM runners WHERE token_hash = ?",
        (_hash(clean),),
    )
    if row is None or row["revoked"]:
        raise PermissionError("this runner was disconnected: run `lbw-runner login` again")
    return row


def heartbeat(db: Database, runner: dict, facts: dict) -> dict:
    """Record what the runner reports and hand it any waiting command once."""
    db.execute(
        """
        UPDATE runners
        SET last_seen = ?, version = ?, claude_version = ?, claude_ready = ?, platform = ?,
            llm_relay = ?, command = ''
        WHERE id = ?
        """,
        (
            _now(),
            _clip(facts.get("version")),
            _clip(facts.get("claude_version")),
            1 if facts.get("claude_ready") else 0,
            _clip(facts.get("platform")) or runner.get("platform") or "",
            1 if facts.get("llm_relay") else 0,
            runner["id"],
        ),
    )
    command = str(runner.get("command") or "")
    return {"commands": [command] if command else []}


def touch_runner(db: Database, runner: dict) -> None:
    """A job poll shows the runner is online. Its reported facts stay as they were."""
    db.execute("UPDATE runners SET last_seen = ? WHERE id = ?", (_now(), runner["id"]))


def _view(row: dict, now: int) -> dict:
    seen = int(row.get("last_seen") or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "owner_id": row["identity_id"],
        "platform": row["platform"],
        "version": row["version"],
        "claude_version": row["claude_version"],
        "claude_ready": bool(row["claude_ready"]),
        "llm_relay": bool(row.get("llm_relay")),
        "last_seen": seen,
        "online": bool(seen) and now - seen <= ONLINE_SECONDS,
        "pending_command": row["command"],
    }


def list_runners(db: Database) -> dict:
    """The signed-in person's runners. An owner or admin also sees everyone's."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    manager = can_manage_org(account)
    if manager:
        rows = db.fetchall(
            "SELECT * FROM runners WHERE tenant_id = ? AND revoked IS NULL ORDER BY created",
            (tenant_id,),
        )
    else:
        rows = db.fetchall(
            "SELECT * FROM runners WHERE tenant_id = ? AND identity_id = ? AND revoked IS NULL "
            "ORDER BY created",
            (tenant_id, account["current_user_id"]),
        )
    names = {user["id"]: user["name"] for user in account["users"]}
    now = _now()
    mine = account["current_user_id"]
    runners = []
    for row in rows:
        view = _view(row, now)
        view["owner"] = names.get(row["identity_id"], "")
        view["mine"] = row["identity_id"] == mine
        runners.append(view)
    return {"runners": runners, "can_manage": manager}


def _owned(db: Database, runner_id: str) -> dict:
    account = require_account(db)
    row = db.fetchone(
        "SELECT * FROM runners WHERE id = ? AND tenant_id = ? AND revoked IS NULL",
        (runner_id, account["org"]["id"]),
    )
    if row is None:
        raise KeyError(runner_id)
    if row["identity_id"] != account["current_user_id"] and not can_manage_org(account):
        raise PermissionError("only its owner, an owner, or an admin changes a runner")
    return row


def revoke_runner(db: Database, runner_id: str) -> dict:
    """Disconnect a runner. Its token stops working and its waiting jobs are cancelled."""
    row = _owned(db, runner_id)
    now = _now()
    db.execute("UPDATE runners SET revoked = ? WHERE id = ?", (now, runner_id))
    db.execute(
        "UPDATE runner_jobs SET status = 'cancelled', finished = ?, error = ? "
        "WHERE runner_id = ? AND status IN ('queued', 'running')",
        (now, "the runner was disconnected", runner_id),
    )
    return {"id": runner_id, "name": row["name"]}


def request_command(db: Database, runner_id: str, command: str) -> dict:
    """Ask a runner to do a maintenance step at its next heartbeat."""
    if command not in COMMANDS:
        raise ValueError(f"unknown runner command: {command}")
    row = _owned(db, runner_id)
    db.execute("UPDATE runners SET command = ? WHERE id = ?", (command, runner_id))
    return {"id": runner_id, "name": row["name"], "command": command}


def pick_runner(db: Database, tenant_id: str, identity_id: str) -> dict:
    """This person's most recently seen online runner."""
    row = db.fetchone(
        """
        SELECT * FROM runners
        WHERE tenant_id = ? AND identity_id = ? AND revoked IS NULL AND last_seen >= ?
        ORDER BY last_seen DESC
        LIMIT 1
        """,
        (tenant_id, identity_id, _now() - ONLINE_SECONDS),
    )
    if row is None:
        raise RunnerRequired()
    return row


def pick_relay_runner(db: Database, tenant_id: str, identity_id: str) -> dict:
    """This person's most recently seen online runner that relays model calls."""
    row = db.fetchone(
        """
        SELECT * FROM runners
        WHERE tenant_id = ? AND identity_id = ? AND revoked IS NULL AND last_seen >= ?
          AND llm_relay = 1
        ORDER BY last_seen DESC
        LIMIT 1
        """,
        (tenant_id, identity_id, _now() - ONLINE_SECONDS),
    )
    if row is None:
        raise RunnerRequired(
            "start lbw-runner on your machine: your local model is reached through it"
        )
    return row


# Jobs --------------------------------------------------------------------------


def create_job(
    db: Database,
    *,
    tenant_id: str,
    identity_id: str,
    runner_id: str,
    task_id: str,
    job_id: str = "",
) -> str:
    job_id = job_id or uuid4().hex
    db.execute(
        """
        INSERT INTO runner_jobs (id, tenant_id, runner_id, identity_id, task_id, status, created)
        VALUES (?, ?, ?, ?, ?, 'queued', ?)
        """,
        (job_id, tenant_id, runner_id, identity_id, task_id, _now()),
    )
    return job_id


def lease_job(db: Database, runner: dict) -> dict | None:
    """Hand the runner its oldest waiting job. Two polls cannot take the same job."""
    row = db.fetchone(
        "SELECT id FROM runner_jobs WHERE runner_id = ? AND status = 'queued' "
        "ORDER BY created LIMIT 1",
        (runner["id"],),
    )
    if row is None:
        return None
    now = _now()
    cursor = db.execute(
        "UPDATE runner_jobs SET status = 'running', started = ?, leased_until = ? "
        "WHERE id = ? AND status = 'queued'",
        (now, now + LEASE_SECONDS, row["id"]),
    )
    if getattr(cursor, "rowcount", 1) == 0:
        return None
    return db.fetchone("SELECT * FROM runner_jobs WHERE id = ?", (row["id"],))


def runner_job(db: Database, runner: dict, job_id: str) -> dict:
    row = db.fetchone(
        "SELECT * FROM runner_jobs WHERE id = ? AND runner_id = ?", (job_id, runner["id"])
    )
    if row is None:
        raise KeyError(job_id)
    return row


def extend_lease(db: Database, job_id: str) -> None:
    db.execute(
        "UPDATE runner_jobs SET leased_until = ? WHERE id = ? AND status = 'running'",
        (_now() + LEASE_SECONDS, job_id),
    )


def finish_job(db: Database, job_id: str, status: str, error: str = "") -> None:
    if status not in {"done", "failed", "cancelled"}:
        raise ValueError(f"unknown job status: {status}")
    db.execute(
        "UPDATE runner_jobs SET status = ?, finished = ?, error = ? "
        "WHERE id = ? AND status IN ('queued', 'running')",
        (status, _now(), _clip(error, 500), job_id),
    )


def job_row(db: Database, job_id: str) -> dict | None:
    return db.fetchone("SELECT * FROM runner_jobs WHERE id = ?", (job_id,))


def lease_lapsed(row: dict) -> bool:
    """A running job whose runner stopped checking in."""
    return row["status"] == "running" and int(row["leased_until"] or 0) < _now()
