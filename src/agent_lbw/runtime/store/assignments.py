"""Which skills and agent-team tasks an organization applies to projects.

A skill reaches a project only through a grant on the organization, on a team
that can use the project, or on the project itself. An enforced task is on for
every project. The text of a skill or task stays in its file.
"""

from __future__ import annotations

import re

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.install import require_account

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SCOPES = frozenset({"tenant", "team", "project"})


def can_manage_org(account: dict) -> bool:
    current = next(
        (user for user in account["users"] if user["id"] == account["current_user_id"]),
        None,
    )
    return current is not None and current["role"] in {"owner", "admin"}


def skills_for_project(db: Database, tenant_id: str, project_id: str) -> list[str]:
    """Skill names assigned to this project from the organization, a team, or itself."""
    names: list[str] = []
    rows = db.fetchall(
        """
        SELECT skill_name FROM skill_grants
        WHERE tenant_id = ? AND scope_type = 'tenant' AND scope_id = ?
        ORDER BY skill_name
        """,
        (tenant_id, tenant_id),
    )
    rows.extend(
        db.fetchall(
            """
            SELECT skill_grants.skill_name
            FROM skill_grants
            JOIN project_grants
              ON project_grants.subject_type = 'team'
             AND project_grants.subject_id = skill_grants.scope_id
            WHERE skill_grants.tenant_id = ?
              AND skill_grants.scope_type = 'team'
              AND project_grants.project_id = ?
            ORDER BY skill_grants.skill_name
            """,
            (tenant_id, project_id),
        )
    )
    rows.extend(
        db.fetchall(
            """
            SELECT skill_name FROM skill_grants
            WHERE tenant_id = ? AND scope_type = 'project' AND scope_id = ?
            ORDER BY skill_name
            """,
            (tenant_id, project_id),
        )
    )
    for row in rows:
        name = str(row["skill_name"])
        if name not in names:
            names.append(name)
    return names


def list_skill_grants(db: Database) -> list[dict]:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    rows = db.fetchall(
        """
        SELECT skill_name, scope_type, scope_id
        FROM skill_grants
        WHERE tenant_id = ?
        ORDER BY skill_name, scope_type, scope_id
        """,
        (tenant_id,),
    )
    return [
        {
            "skill_name": row["skill_name"],
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "label": _scope_label(db, tenant_id, row["scope_type"], row["scope_id"]),
        }
        for row in rows
    ]


def set_skill_grant(
    db: Database,
    *,
    skill_name: str,
    scope_type: str,
    scope_id: str,
    assigned: bool,
) -> list[dict]:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    name = _clean_name(skill_name, "skill")
    _require_scope(db, tenant_id, scope_type, scope_id)
    if assigned:
        db.execute(
            """
            INSERT INTO skill_grants (tenant_id, skill_name, scope_type, scope_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (tenant_id, skill_name, scope_type, scope_id) DO NOTHING
            """,
            (tenant_id, name, scope_type, scope_id),
        )
    else:
        db.execute(
            """
            DELETE FROM skill_grants
            WHERE tenant_id = ? AND skill_name = ? AND scope_type = ? AND scope_id = ?
            """,
            (tenant_id, name, scope_type, scope_id),
        )
    return list_skill_grants(db)


def enforced_role_names(db: Database) -> list[str]:
    account = require_account(db)
    rows = db.fetchall(
        """
        SELECT role_name FROM role_requirements
        WHERE tenant_id = ?
        ORDER BY role_name
        """,
        (account["org"]["id"],),
    )
    return [str(row["role_name"]) for row in rows]


def set_enforced_role(db: Database, role_name: str, *, enforced: bool) -> list[str]:
    account = _require_manager(db)
    name = _clean_name(role_name, "role")
    tenant_id = account["org"]["id"]
    if enforced:
        db.execute(
            """
            INSERT INTO role_requirements (tenant_id, role_name) VALUES (?, ?)
            ON CONFLICT (tenant_id, role_name) DO NOTHING
            """,
            (tenant_id, name),
        )
    else:
        db.execute(
            "DELETE FROM role_requirements WHERE tenant_id = ? AND role_name = ?",
            (tenant_id, name),
        )
    return enforced_role_names(db)


def _require_manager(db: Database) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin manages organization settings")
    return account


def _clean_name(value: str, label: str) -> str:
    name = (value or "").strip()
    if not _NAME.fullmatch(name):
        raise ValueError(f"{label} name is not valid")
    return name


def _require_scope(db: Database, tenant_id: str, scope_type: str, scope_id: str) -> None:
    if scope_type not in _SCOPES:
        raise ValueError("unknown scope")
    if scope_type == "tenant":
        if scope_id != tenant_id:
            raise ValueError("unknown scope")
        return
    table = "teams" if scope_type == "team" else "projects"
    row = db.fetchone(
        f"SELECT id FROM {table} WHERE id = ? AND tenant_id = ?",
        (scope_id, tenant_id),
    )
    if row is None:
        raise ValueError("unknown scope")


def _scope_label(db: Database, tenant_id: str, scope_type: str, scope_id: str) -> str:
    if scope_type == "tenant":
        return "Every project"
    table = "teams" if scope_type == "team" else "projects"
    row = db.fetchone(
        f"SELECT name FROM {table} WHERE id = ? AND tenant_id = ?",
        (scope_id, tenant_id),
    )
    if row is None:
        return scope_id
    prefix = "Team" if scope_type == "team" else "Project"
    return f"{prefix} {row['name']}"
