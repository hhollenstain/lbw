"""Spec-driven proposals: who requires a spec before a proposal is planned.

A spec is optional by default: whoever asks for a change chooses whether the
proposal writes one first. An organization owner or admin can require a spec
on every project, and a person who manages a project can require one there.
Either requirement is enough. A project cannot loosen what the organization
requires.

The spec itself is committed to the repository on the proposal branch. These
rows only hold the requirement.
"""

from __future__ import annotations

from pathlib import Path

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.recording import project_id_for_checkout


def org_spec_policy(db: Database) -> dict:
    account = require_account(db)
    return {
        "required": _org_required(db, account["org"]["id"]),
        "can_manage": can_manage_org(account),
    }


def set_org_spec_policy(db: Database, *, required: bool) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an organization owner or admin requires specs")
    db.execute(
        "UPDATE tenants SET spec_required = ? WHERE id = ?",
        (1 if required else 0, account["org"]["id"]),
    )
    return org_spec_policy(db)


def project_spec_policy(db: Database, workspace: Path) -> dict:
    """The requirement for one project, as the job form and project settings see it."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    org_required = _org_required(db, tenant_id)
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    project_required = False
    can_manage = False
    if project_id is not None:
        row = db.fetchone(
            "SELECT spec_required FROM projects WHERE id = ? AND tenant_id = ?",
            (project_id, tenant_id),
        )
        project_required = bool(row and row["spec_required"])
        can_manage = can_manage_org(account) or can_access(
            db, account["current_user_id"], project_id, need="manage"
        )
    return {
        "required": org_required or project_required,
        "org_required": org_required,
        "project_required": project_required,
        "can_manage": can_manage,
    }


def set_project_spec_policy(db: Database, workspace: Path, *, required: bool) -> dict:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        raise KeyError(str(workspace))
    if not can_manage_org(account) and not can_access(
        db, account["current_user_id"], project_id, need="manage"
    ):
        raise PermissionError("a person who manages this project requires specs here")
    db.execute(
        "UPDATE projects SET spec_required = ? WHERE id = ? AND tenant_id = ?",
        (1 if required else 0, project_id, tenant_id),
    )
    return project_spec_policy(db, workspace)


def spec_required(db: Database, workspace: Path) -> bool:
    return bool(project_spec_policy(db, workspace)["required"])


def _org_required(db: Database, tenant_id: str) -> bool:
    row = db.fetchone("SELECT spec_required FROM tenants WHERE id = ?", (tenant_id,))
    return bool(row and row["spec_required"])
