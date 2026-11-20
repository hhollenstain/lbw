"""Projects onboarded to a tenant, and who may use them.

An organization owner or admin can open every project in that tenant. Everyone
else needs a grant to their team or to themselves.
"""

from __future__ import annotations

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.names import ACCESS_LEVELS, PROJECT_KINDS, clean_name, new_id


def ensure_project(
    db: Database,
    tenant_id: str,
    *,
    name: str,
    kind: str,
    locator: str,
) -> dict:
    if kind not in PROJECT_KINDS:
        raise ValueError("kind must be github, git, or folder")
    clean = clean_name(name, "project")
    place = locator.strip()
    if not place:
        raise ValueError("locator is required")
    tenant = db.fetchone("SELECT id FROM tenants WHERE id = ?", (tenant_id,))
    if tenant is None:
        raise KeyError(tenant_id)
    existing = db.fetchone(
        "SELECT id, name, kind, locator FROM projects WHERE tenant_id = ? AND locator = ?",
        (tenant_id, place),
    )
    if existing is not None:
        return {
            "id": existing["id"],
            "name": existing["name"],
            "kind": existing["kind"],
            "locator": existing["locator"],
        }
    project_id = new_id("proj")
    db.execute(
        "INSERT INTO projects (id, tenant_id, name, kind, locator) VALUES (?, ?, ?, ?, ?)",
        (project_id, tenant_id, clean, kind, place),
    )
    return {"id": project_id, "name": clean, "kind": kind, "locator": place}


def grant_project(
    db: Database,
    project_id: str,
    *,
    subject_type: str,
    subject_id: str,
    access: str,
) -> dict:
    if subject_type not in {"team", "identity"}:
        raise ValueError("subject must be team or identity")
    if access not in ACCESS_LEVELS:
        raise ValueError("access must be use, write, or manage")
    project = db.fetchone("SELECT tenant_id FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise KeyError(project_id)
    _subject_belongs(db, str(project["tenant_id"]), subject_type, subject_id)
    existing = db.fetchone(
        """
        SELECT id FROM project_grants
        WHERE project_id = ? AND subject_type = ? AND subject_id = ?
        """,
        (project_id, subject_type, subject_id),
    )
    if existing is None:
        grant_id = new_id("grant")
        db.execute(
            """
            INSERT INTO project_grants (id, project_id, subject_type, subject_id, access)
            VALUES (?, ?, ?, ?, ?)
            """,
            (grant_id, project_id, subject_type, subject_id, access),
        )
    else:
        grant_id = str(existing["id"])
        db.execute("UPDATE project_grants SET access = ? WHERE id = ?", (access, grant_id))
    return {
        "id": grant_id,
        "project_id": project_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "access": access,
    }


def can_access(db: Database, identity_id: str, project_id: str, *, need: str = "use") -> bool:
    if need not in ACCESS_LEVELS:
        raise ValueError("access must be use, write, or manage")
    project = db.fetchone("SELECT tenant_id FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return False
    tenant_id = project["tenant_id"]
    membership = db.fetchone(
        "SELECT rank FROM tenant_memberships WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )
    if membership is None:
        return False
    if membership["rank"] in {"owner", "admin"}:
        return True
    if need == "manage":
        wanted = ("manage",)
    elif need == "write":
        wanted = ("write", "manage")
    else:
        wanted = ("use", "write", "manage")
    marks = ", ".join("?" for _ in wanted)
    direct = db.fetchone(
        f"""
        SELECT access FROM project_grants
        WHERE project_id = ? AND subject_type = 'identity' AND subject_id = ?
          AND access IN ({marks})
        """,
        (project_id, identity_id, *wanted),
    )
    if direct is not None:
        return True
    team_grant = db.fetchone(
        f"""
        SELECT project_grants.access
        FROM project_grants
        JOIN team_memberships ON team_memberships.team_id = project_grants.subject_id
        WHERE project_grants.project_id = ?
          AND project_grants.subject_type = 'team'
          AND team_memberships.identity_id = ?
          AND project_grants.access IN ({marks})
        """,
        (project_id, identity_id, *wanted),
    )
    return team_grant is not None


def require_project(db: Database, tenant_id: str, project_id: str) -> dict:
    row = db.fetchone(
        """
        SELECT id, name, kind, locator FROM projects
        WHERE id = ? AND tenant_id = ?
        """,
        (project_id, tenant_id),
    )
    if row is None:
        raise KeyError(project_id)
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "locator": row["locator"],
    }


def list_projects(db: Database, tenant_id: str) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT id, name, kind, locator FROM projects
        WHERE tenant_id = ?
        ORDER BY name, id
        """,
        (tenant_id,),
    )
    return [
        {"id": row["id"], "name": row["name"], "kind": row["kind"], "locator": row["locator"]}
        for row in rows
    ]


def list_grants(db: Database, tenant_id: str) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT project_grants.project_id, project_grants.subject_type,
               project_grants.subject_id, project_grants.access
        FROM project_grants
        JOIN projects ON projects.id = project_grants.project_id
        WHERE projects.tenant_id = ?
        ORDER BY project_grants.project_id, project_grants.subject_type
        """,
        (tenant_id,),
    )
    return [
        {
            "project_id": row["project_id"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "access": row["access"],
        }
        for row in rows
    ]


def _subject_belongs(db: Database, tenant_id: str, subject_type: str, subject_id: str) -> None:
    if subject_type == "team":
        team = db.fetchone(
            "SELECT id FROM teams WHERE id = ? AND tenant_id = ?",
            (subject_id, tenant_id),
        )
        if team is None:
            raise ValueError("that team is not in this organization")
        return
    person = db.fetchone(
        "SELECT identity_id FROM tenant_memberships WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, subject_id),
    )
    if person is None:
        raise ValueError("that person is not in this organization")
