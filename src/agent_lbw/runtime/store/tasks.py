"""Task requests and outcomes. The goal and the result are ciphertext.

Logs, prompts, and tool traces stay in the run directory.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.crypto import master_key, open_secret, seal, unwrap_key, wrap_key
from agent_lbw.runtime.store.projects import can_access

_KEY_BYTES = 32
_CODE_FAILURE = (
    "tests failed",
    "lint",
    "review",
    "send back",
    "handed to a person",
    "waiting for",
    "no open goals",
    "approved",
    "empty diff",
    "criteria",
    "refusing to push",
)
_INFRA_PREFIXES = (
    "could not leave main:",
    "commit failed:",
    "push failed:",
    "pr failed:",
)


def _clip(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return "LBW crashed while running this job"
    return clean[:200]


def save_task_request(
    db: Database,
    root: Path,
    *,
    task_id: str,
    tenant_id: str,
    project_id: str,
    requester_id: str,
    goal: str | None,
    status: str,
    branch: str | None = None,
    pr_url: str | None = None,
    run_ref: str | None = None,
    agent_id: str | None = None,
    model_id: str | None = None,
    agent_kind: str | None = None,
    attempt_of: str | None = None,
    attempt: int = 1,
) -> None:
    """Insert a request. A person who cannot use the project is refused.

    A rerun names the proposal's first run in `attempt_of` and carries its run number.
    """
    if not can_access(db, requester_id, project_id, need="use"):
        raise PermissionError("that project is closed to this person")
    ciphertext = None if goal is None else seal(tenant_dek(db, root, tenant_id), goal)
    db.execute(
        """
        INSERT INTO tasks (
            id, tenant_id, project_id, requester_id, status, branch, pr_url,
            run_ref, goal_ciphertext, created, agent_id, model_id, agent_kind,
            attempt_of, attempt
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            task_id,
            tenant_id,
            project_id,
            requester_id,
            status,
            branch,
            pr_url,
            run_ref,
            ciphertext,
            int(time.time()),
            agent_id or None,
            model_id or None,
            agent_kind or None,
            attempt_of or None,
            max(1, int(attempt or 1)),
        ),
    )


def next_attempt(db: Database, tenant_id: str, first_id: str) -> int:
    """The next run number of the proposal whose first run is `first_id`."""
    row = db.fetchone(
        """
        SELECT MAX(attempt) AS last FROM tasks
        WHERE tenant_id = ? AND (id = ? OR attempt_of = ?)
        """,
        (tenant_id, first_id, first_id),
    )
    last = row["last"] if row is not None else None
    return int(last or 1) + 1


def fault_for(*, status: str, reason: str = "", error: str = "") -> str:
    """A short LBW fault. A test, review, or other code failure stays blank."""
    if status == "error":
        return _clip(error or reason or "LBW crashed while running this job")
    if status != "failed":
        return ""
    text = (reason or error or "").strip()
    lowered = text.casefold()
    if any(mark in lowered for mark in _CODE_FAILURE):
        return ""
    if lowered.startswith(_INFRA_PREFIXES):
        label = text.split(":", 1)[0].strip()
        return _clip(f"{label}: LBW could not finish this job")
    if "fatal:" in lowered or "not a git" in lowered or lowered.startswith("unable to"):
        return "LBW could not prepare the workspace for this job"
    return ""


def touch_task(
    db: Database,
    *,
    task_id: str,
    tenant_id: str,
    status: str,
    branch: str | None,
    pr_url: str | None,
    run_ref: str | None,
    fault: str = "",
) -> None:
    """Update status and publish fields. The goal ciphertext stays as first written."""
    row = db.fetchone(
        "SELECT agent_id, reviewer_id FROM tasks WHERE id = ? AND tenant_id = ?",
        (task_id, tenant_id),
    )
    if row is not None and row["agent_id"] and status == "success" and not row["reviewer_id"]:
        raise PermissionError("a person must accept work an agent built")
    db.execute(
        """
        UPDATE tasks
        SET status = ?, branch = ?, pr_url = ?, run_ref = ?, fault = ?
        WHERE id = ? AND tenant_id = ?
        """,
        (status, branch, pr_url, run_ref, fault or None, task_id, tenant_id),
    )


def read_task(db: Database, root: Path, *, task_id: str, identity_id: str) -> dict:
    row = db.fetchone(
        """
        SELECT id, tenant_id, project_id, requester_id, status, branch, pr_url,
               run_ref, goal_ciphertext, agent_id, model_id, reviewer_id, created
        FROM tasks WHERE id = ?
        """,
        (task_id,),
    )
    if row is None:
        raise KeyError(task_id)
    if not can_access(db, identity_id, row["project_id"], need="use"):
        raise PermissionError("that project is closed to this person")
    goal = None
    if row["goal_ciphertext"]:
        goal = open_secret(tenant_dek(db, root, row["tenant_id"]), row["goal_ciphertext"])
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "requester_id": row["requester_id"],
        "status": row["status"],
        "branch": row["branch"],
        "pr_url": row["pr_url"],
        "run_ref": row["run_ref"],
        "goal": goal,
        "agent_id": row["agent_id"] or "",
        "model_id": row["model_id"] or "",
        "reviewer_id": row["reviewer_id"] or "",
        "created": row["created"],
    }


def save_task_result(
    db: Database,
    root: Path,
    *,
    task_id: str,
    tenant_id: str,
    payload: dict,
) -> None:
    """Seal the plan, steps, outcome, and final diff. An empty new diff keeps the last one."""
    stored = _stored_result(db, root, tenant_id, task_id)
    if not str(payload.get("diff") or "").strip() and stored.get("diff"):
        payload = {**payload, "diff": stored["diff"]}
    token = seal(tenant_dek(db, root, tenant_id), json.dumps(payload, ensure_ascii=False))
    db.execute(
        """
        UPDATE tasks SET result_ciphertext = ?
        WHERE id = ? AND tenant_id = ?
        """,
        (token, task_id, tenant_id),
    )


def _stored_result(db: Database, root: Path, tenant_id: str, task_id: str) -> dict:
    row = db.fetchone(
        "SELECT result_ciphertext FROM tasks WHERE id = ? AND tenant_id = ?",
        (task_id, tenant_id),
    )
    if row is None or not row["result_ciphertext"]:
        return {}
    try:
        data = json.loads(open_secret(tenant_dek(db, root, tenant_id), row["result_ciphertext"]))
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def tenant_dek(db: Database, root: Path, tenant_id: str) -> bytes:
    row = db.fetchone("SELECT wrapped_key FROM tenant_keys WHERE tenant_id = ?", (tenant_id,))
    master = master_key(root)
    if row is None:
        dek = secrets.token_bytes(_KEY_BYTES)
        db.execute(
            "INSERT INTO tenant_keys (tenant_id, wrapped_key) VALUES (?, ?)",
            (tenant_id, wrap_key(master, dek)),
        )
        return dek
    return unwrap_key(master, row["wrapped_key"])
