"""A note of who changed what. The row is the note, not a second copy of the change."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.names import new_id

_GATE_PHRASE = {
    "build": "accepted the plan",
    "approve": "accepted the change",
    "send_back": "sent this back",
    "continue": "asked for more changes",
    "rework": "requested changes",
    "hand_off": "handed this off",
}


def record_audit(
    db: Database,
    *,
    tenant_id: str,
    action: str,
    summary: str,
    actor_id: str = "",
    method: str = "",
    path: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: str = "",
) -> str:
    """Append one note. Secrets do not belong in the summary or the detail."""
    audit_id = new_id("aud")
    db.execute(
        """
        INSERT INTO audit_log (
            id, tenant_id, actor_id, at, action, method, path,
            target_type, target_id, summary, detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id,
            tenant_id,
            actor_id or None,
            int(time.time()),
            action[:80],
            method[:12] or None,
            path[:240] or None,
            target_type[:40] or None,
            target_id[:80] or None,
            " ".join(summary.split())[:300] or action,
            " ".join(detail.split())[:500] or None,
        ),
    )
    return audit_id


def list_audit(db: Database, tenant_id: str, *, limit: int = 100) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT audit_log.id, audit_log.actor_id, audit_log.at, audit_log.action,
               audit_log.method, audit_log.path, audit_log.target_type,
               audit_log.target_id, audit_log.summary, audit_log.detail,
               identities.display_name
        FROM audit_log
        LEFT JOIN identities ON identities.id = audit_log.actor_id
        WHERE audit_log.tenant_id = ?
        ORDER BY audit_log.at DESC, audit_log.id DESC
        LIMIT ?
        """,
        (tenant_id, max(1, min(limit, 200))),
    )
    return [_public(row) for row in rows]


def note_gate(
    db: Database,
    *,
    tenant_id: str,
    task_id: str,
    actor_id: str,
    actor_name: str,
    action: str,
    note: str = "",
    path: str = "",
) -> dict:
    """Remember who accepted a gate, or who sent the work back."""
    phrase = _GATE_PHRASE.get(action, action)
    who = actor_name or "Someone"
    clipped = " ".join((note or "").split())[:240]
    summary = f"{who} {phrase}"
    if clipped and action in {"send_back", "continue", "rework"}:
        summary = f"{summary}: {clipped}"
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=f"task.{action}",
        summary=summary,
        method="POST",
        path=path or f"/api/tasks/{task_id}/decide",
        target_type="task",
        target_id=task_id,
        detail=clipped,
    )
    return _gate(action, actor_id, who, clipped, int(time.time()))


def gates_for_tasks(db: Database, task_ids: list[str]) -> dict[str, list[dict]]:
    wanted = [task_id for task_id in task_ids if task_id]
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    rows = db.fetchall(
        f"""
        SELECT audit_log.target_id, audit_log.action, audit_log.detail, audit_log.at,
               audit_log.actor_id, identities.display_name
        FROM audit_log
        LEFT JOIN identities ON identities.id = audit_log.actor_id
        WHERE audit_log.target_type = 'task'
          AND audit_log.target_id IN ({marks})
          AND audit_log.action LIKE 'task.%'
          AND audit_log.action != 'task.start'
        ORDER BY audit_log.at ASC, audit_log.id ASC
        """,
        tuple(wanted),
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        verb = str(row["action"] or "")
        if verb.startswith("task."):
            verb = verb.split(".", 1)[1]
        grouped.setdefault(str(row["target_id"]), []).append(
            _gate(
                verb,
                str(row["actor_id"] or ""),
                str(row["display_name"] or ""),
                str(row["detail"] or ""),
                int(row["at"] or 0),
            )
        )
    return grouped


def person_name(db: Database, identity_id: str) -> str:
    if not identity_id:
        return ""
    row = db.fetchone("SELECT display_name FROM identities WHERE id = ?", (identity_id,))
    if row is None:
        return ""
    return str(row["display_name"] or "")


def _gate(action: str, actor_id: str, actor_name: str, note: str, at: int) -> dict:
    return {
        "action": action,
        "at": datetime.fromtimestamp(at or int(time.time()), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": note,
        "actor": {"id": actor_id, "name": actor_name},
    }


def _public(row) -> dict:
    return {
        "id": row["id"],
        "at": datetime.fromtimestamp(int(row["at"] or 0), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor_id": row["actor_id"] or "",
        "actor_name": row["display_name"] or "",
        "action": row["action"],
        "method": row["method"] or "",
        "path": row["path"] or "",
        "target_type": row["target_type"] or "",
        "target_id": row["target_id"] or "",
        "summary": row["summary"],
        "detail": row["detail"] or "",
    }
