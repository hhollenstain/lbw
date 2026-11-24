"""Favorite projects: the ones a person pins to the top of their project list.

A favorite belongs to one person in one tenant. It is a shortcut, not access:
the project list still shows only the projects that person can open, and a
favorite on a project they lose access to stays hidden until it comes back.
"""

from __future__ import annotations

import time

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.install import require_account


def favorite_project_ids(db: Database, tenant_id: str, identity_id: str) -> set[str]:
    rows = db.fetchall(
        "SELECT project_id FROM project_favorites WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )
    return {str(row["project_id"]) for row in rows}


def set_favorite(db: Database, project_id: str, *, favorite: bool) -> list[str]:
    """Pin or unpin one project for the signed-in person. Returns their favorites."""
    from agent_lbw.runtime.store.auth import current_actor
    from agent_lbw.runtime.store.github_sync import projects_for_person

    account = require_account(db)
    tenant_id = str(account["org"]["id"])
    actor = current_actor() or str(account.get("current_user_id") or "")
    if not actor:
        raise PermissionError("sign in to keep favorites")
    visible = {str(row["id"]) for row in projects_for_person(db, tenant_id, actor)}
    if project_id not in visible:
        raise KeyError(project_id)
    if favorite:
        db.execute(
            """
            INSERT INTO project_favorites (tenant_id, identity_id, project_id, created)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (identity_id, project_id) DO NOTHING
            """,
            (tenant_id, actor, project_id, int(time.time())),
        )
    else:
        db.execute(
            "DELETE FROM project_favorites WHERE identity_id = ? AND project_id = ?",
            (actor, project_id),
        )
    favorites = favorite_project_ids(db, tenant_id, actor)
    return sorted(favorites & visible)


def move_favorites(db: Database, source: str, target: str) -> None:
    """Keep favorites when two project rows for one repository merge."""
    db.execute(
        """
        UPDATE project_favorites SET project_id = ?
        WHERE project_id = ?
          AND identity_id NOT IN (
            SELECT identity_id FROM project_favorites WHERE project_id = ?
          )
        """,
        (target, source, target),
    )
    drop_favorites(db, source)


def drop_favorites(db: Database, project_id: str) -> None:
    db.execute("DELETE FROM project_favorites WHERE project_id = ?", (project_id,))
