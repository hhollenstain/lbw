"""Agent teams an organization sets up for the steps of a proposal.

A team has a name, an instruction, and a model. The model's endpoint is the
service the team calls. A team owns one or more steps. A step plans, builds,
or reviews, and can wait for other steps; steps that do not wait on each
other run side by side (``runtime/team_graph.py``). A build step can own part
of the plan by path.

A project turns a team on. Once any team is on, that project's proposals run
the teams' steps instead of the ROLE.md roles. ``import_roles`` turns the
roles a project uses into teams so nothing is lost when it switches.
The proposal still pauses for a person with write access. The instruction
stays on the team row; it is not a prompt log.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.roles import REQUIRED_ROLES, ROLE_JOBS, Role
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.names import clean_name, new_id
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.recording import project_id_for_checkout
from agent_lbw.runtime.team_graph import (
    GraphError,
    Step,
    clean_step,
    describe,
    prepare,
    step_key,
    validate,
)

_JOBS = {"plan": "Plan", "build": "Build", "review": "Review"}
_INSTRUCTION_LIMIT = 4000
_STEP_LIMIT = 12


def create_team(
    db: Database,
    *,
    name: str,
    job: str = "",
    instruction: str = "",
    model_id: str = "",
    steps: Sequence[dict] | None = None,
) -> str:
    """Add a team. It stays off on each project until that project turns it on."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    label = clean_name(name, "team")
    linked = _checked_model(db, tenant_id, model_id)
    if _team_named(db, tenant_id, label) is not None:
        raise ValueError(f"{label} is already a team")
    chosen = _clean_steps(steps, name=label, job=job)
    team_id = new_id("agt")
    _check_graph(db, tenant_id, team_id, chosen)
    db.execute(
        """
        INSERT INTO agent_teams (id, tenant_id, name, job, instruction, model_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            team_id,
            tenant_id,
            label,
            chosen[0].job,
            (instruction or "").strip()[:_INSTRUCTION_LIMIT],
            linked or None,
        ),
    )
    _write_steps(db, tenant_id, team_id, chosen)
    return team_id


def update_team(
    db: Database,
    team_id: str,
    *,
    name: str | None = None,
    instruction: str | None = None,
    model_id: str | None = None,
    steps: Sequence[dict] | None = None,
) -> None:
    """Change a team's name, instruction, model, or steps. Fields left as None stay."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    current = _team(db, tenant_id, team_id)
    if current is None:
        raise KeyError(team_id)
    label = str(current["name"])
    if name is not None:
        label = clean_name(name, "team")
        taken = _team_named(db, tenant_id, label)
        if taken is not None and taken["id"] != team_id:
            raise ValueError(f"{label} is already a team")
    linked = current["model_id"] or ""
    if model_id is not None:
        linked = _checked_model(db, tenant_id, model_id)
    text = current["instruction"] or ""
    if instruction is not None:
        text = instruction.strip()[:_INSTRUCTION_LIMIT]
    job = str(current["job"])
    if steps is not None:
        chosen = _clean_steps(steps, name=label, job=job)
        _check_graph(db, tenant_id, team_id, chosen)
        removed = {step.key for step in _team_steps(db, tenant_id, team_id)} - {
            step.key for step in chosen
        }
        _refuse_if_waited_on(db, tenant_id, team_id, removed)
        db.execute("DELETE FROM agent_team_steps WHERE team_id = ?", (team_id,))
        _write_steps(db, tenant_id, team_id, chosen)
        job = chosen[0].job
    db.execute(
        """
        UPDATE agent_teams SET name = ?, job = ?, instruction = ?, model_id = ?
        WHERE id = ? AND tenant_id = ?
        """,
        (label, job, text, linked or None, team_id, tenant_id),
    )


def delete_team(db: Database, team_id: str) -> None:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _team(db, tenant_id, team_id) is None:
        raise KeyError(team_id)
    owned = {step.key for step in _team_steps(db, tenant_id, team_id)}
    _refuse_if_waited_on(db, tenant_id, team_id, owned)
    db.execute("DELETE FROM agent_team_steps WHERE team_id = ?", (team_id,))
    db.execute("DELETE FROM agent_team_projects WHERE team_id = ?", (team_id,))
    db.execute("DELETE FROM agent_teams WHERE id = ? AND tenant_id = ?", (team_id, tenant_id))


def teams_for_settings(db: Database) -> list[dict]:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    steps = _steps_by_team(db, tenant_id)
    return [
        _public(row, enabled=False, steps=steps.get(row["id"], [])) for row in _rows(db, tenant_id)
    ]


def project_view(db: Database, workspace: Path) -> dict:
    """Teams for this checkout, their steps as a graph, and the models a team can use."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    manage = can_manage_org(account)
    if project_id is not None and not manage:
        manage = can_access(db, identity_id, project_id, need="manage")
    enabled = _enabled_ids(db, project_id) if project_id else set()
    if manage:
        models = _model_rows(db, tenant_id)
    elif project_id:
        from agent_lbw.runtime.store.catalog import models_for_workspace

        allowed = {item["id"] for item in models_for_workspace(db, workspace)}
        models = [row for row in _model_rows(db, tenant_id) if row["id"] in allowed]
    else:
        models = []
    steps = _steps_by_team(db, tenant_id)
    rows = _rows(db, tenant_id)
    running = [step for row in rows if row["id"] in enabled for step in steps.get(row["id"], [])]
    graph: list[dict] = []
    graph_error = ""
    try:
        graph = describe(prepare(running))
    except GraphError as exc:
        graph_error = str(exc)
    return {
        "can_manage": manage,
        "can_edit_teams": can_manage_org(account),
        "agent_teams": [
            _public(row, enabled=row["id"] in enabled, steps=steps.get(row["id"], []))
            for row in rows
        ],
        "models": [_model_choice(row) for row in models],
        "graph": graph,
        "graph_error": graph_error,
        "uses_roles": not running,
    }


def set_project_team(db: Database, workspace: Path, team_id: str, *, enabled: bool) -> dict:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    if _team(db, tenant_id, team_id) is None:
        raise KeyError(team_id)
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        raise ValueError("this workspace is not a project")
    if not can_manage_org(account) and not can_access(
        db, account["current_user_id"], project_id, need="manage"
    ):
        raise PermissionError("an organization admin or a repository admin changes agent teams")
    _set_enabled(db, tenant_id, team_id, project_id, enabled)
    return project_view(db, workspace)


def import_roles(db: Database, workspace: Path, roles: Sequence[Role]) -> dict:
    """Make a team for each role a project runs, and turn those teams on for it.

    A role whose name is already a team is not copied again; that team is
    turned on instead. Review roles wait for nothing, so they run side by side.
    Builder and QA are skipped: the coding agent and the goal check always run.
    """
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        raise ValueError("this workspace is not a project")
    for role in roles:
        if not role.enabled or role.job not in ROLE_JOBS or role.name in REQUIRED_ROLES:
            continue
        label = clean_name(role.name, "team")
        found = _team_named(db, tenant_id, label)
        if found is not None:
            team_id = str(found["id"])
        else:
            key = _free_key(db, tenant_id, step_key(role.name))
            team_id = create_team(
                db,
                name=label,
                instruction=role.body,
                steps=[{"key": key, "job": role.job}],
            )
        _set_enabled(db, tenant_id, team_id, project_id, True)
    return project_view(db, workspace)


def enabled_steps(db: Database, workspace: Path) -> list[Step]:
    """Steps of the teams this project turned on, with each team's model and instruction."""
    try:
        account = require_account(db)
    except ValueError:
        return []
    tenant_id = account["org"]["id"]
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        return []
    enabled = _enabled_ids(db, project_id)
    steps = _steps_by_team(db, tenant_id)
    return [
        step
        for row in _rows(db, tenant_id)
        if row["id"] in enabled
        for step in steps.get(row["id"], [])
    ]


def enabled_teams(db: Database, workspace: Path) -> list[dict]:
    """Teams this project turned on."""
    try:
        account = require_account(db)
    except ValueError:
        return []
    tenant_id = account["org"]["id"]
    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        return []
    enabled = _enabled_ids(db, project_id)
    steps = _steps_by_team(db, tenant_id)
    return [
        _public(row, enabled=True, steps=steps.get(row["id"], []))
        for row in _rows(db, tenant_id)
        if row["id"] in enabled
    ]


def _clean_steps(steps: Sequence[dict] | None, *, name: str, job: str) -> list[Step]:
    if not steps:
        chosen = (job or "").strip().lower()
        if chosen not in ROLE_JOBS:
            raise ValueError("a team plans, builds, or reviews")
        steps = [{"key": step_key(name), "job": chosen}]
    if len(steps) > _STEP_LIMIT:
        raise ValueError(f"a team owns at most {_STEP_LIMIT} steps")
    cleaned = [
        clean_step(dict(raw), fallback_key=step_key(name), position=index)
        for index, raw in enumerate(steps)
    ]
    keys = [step.key for step in cleaned]
    if len(set(keys)) != len(keys):
        raise ValueError("each step of a team needs its own name")
    return cleaned


def _check_graph(db: Database, tenant_id: str, team_id: str, chosen: list[Step]) -> None:
    """The organization's steps, with this team's replaced, must still form a graph."""
    others = [
        step
        for owner, items in _steps_by_team(db, tenant_id).items()
        if owner != team_id
        for step in items
    ]
    taken = {step.key: step.team_name for step in others}
    for step in chosen:
        if step.key in taken:
            raise ValueError(f"step {step.key} already belongs to {taken[step.key]}")
    try:
        validate([*others, *chosen], one_planner=False)
    except GraphError as exc:
        raise ValueError(str(exc)) from exc


def _refuse_if_waited_on(db: Database, tenant_id: str, team_id: str, keys: set[str]) -> None:
    if not keys:
        return
    for owner, items in _steps_by_team(db, tenant_id).items():
        if owner == team_id:
            continue
        for step in items:
            waits = keys.intersection(step.after)
            if waits:
                raise ValueError(
                    f"{step.title} step {step.key} waits for {', '.join(sorted(waits))}; "
                    "remove that wait first"
                )


def _write_steps(db: Database, tenant_id: str, team_id: str, steps: list[Step]) -> None:
    for position, step in enumerate(steps):
        db.execute(
            """
            INSERT INTO agent_team_steps
                (id, tenant_id, team_id, step_key, job, after_json, paths_json, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("ats"),
                tenant_id,
                team_id,
                step.key,
                step.job,
                json.dumps(list(step.after)),
                json.dumps(list(step.paths)),
                position,
            ),
        )


def _free_key(db: Database, tenant_id: str, base: str) -> str:
    base = base or "team"
    taken = {
        str(row["step_key"])
        for row in db.fetchall(
            "SELECT step_key FROM agent_team_steps WHERE tenant_id = ?", (tenant_id,)
        )
    }
    if base not in taken:
        return base
    number = 2
    while f"{base}-{number}" in taken:
        number += 1
    return f"{base}-{number}"


def _set_enabled(db: Database, tenant_id: str, team_id: str, project_id: str, on: bool) -> None:
    if on:
        db.execute(
            """
            INSERT INTO agent_team_projects (tenant_id, team_id, project_id, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (team_id, project_id) DO UPDATE SET enabled = 1
            """,
            (tenant_id, team_id, project_id),
        )
    else:
        db.execute(
            "DELETE FROM agent_team_projects WHERE team_id = ? AND project_id = ?",
            (team_id, project_id),
        )


def _require_manager(db: Database) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin manages agent teams")
    return account


def _public(row: dict, *, enabled: bool, steps: list[Step]) -> dict:
    job = str(row["job"])
    return {
        "id": row["id"],
        "name": row["name"],
        "job": job,
        "team": _JOBS.get(job, "Review"),
        "instruction": row["instruction"] or "",
        "summary": (row["instruction"] or row["name"] or "")[:180],
        "model_id": row["model_id"] or "",
        "model_name": row["model_name"] or "",
        "service": row["service"] or "",
        "enabled": enabled,
        "steps": [
            {
                "key": step.key,
                "job": step.job,
                "after": list(step.after),
                "paths": list(step.paths),
            }
            for step in steps
        ],
    }


def _rows(db: Database, tenant_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT agent_teams.id, agent_teams.name, agent_teams.job,
               agent_teams.instruction, agent_teams.model_id,
               models.name AS model_name,
               model_endpoints.name AS service
        FROM agent_teams
        LEFT JOIN models ON models.id = agent_teams.model_id
        LEFT JOIN model_endpoints ON model_endpoints.id = models.endpoint_id
        WHERE agent_teams.tenant_id = ?
        ORDER BY agent_teams.name, agent_teams.id
        """,
        (tenant_id,),
    )


def _steps_by_team(db: Database, tenant_id: str) -> dict[str, list[Step]]:
    rows = db.fetchall(
        """
        SELECT agent_team_steps.team_id, agent_team_steps.step_key, agent_team_steps.job,
               agent_team_steps.after_json, agent_team_steps.paths_json,
               agent_team_steps.position,
               agent_teams.name AS team_name, agent_teams.instruction, agent_teams.model_id
        FROM agent_team_steps
        JOIN agent_teams ON agent_teams.id = agent_team_steps.team_id
        WHERE agent_team_steps.tenant_id = ?
        ORDER BY agent_teams.name, agent_team_steps.position, agent_team_steps.step_key
        """,
        (tenant_id,),
    )
    found: dict[str, list[Step]] = {}
    for order, row in enumerate(rows):
        team_id = str(row["team_id"])
        found.setdefault(team_id, []).append(
            Step(
                key=str(row["step_key"]),
                job=str(row["job"]),
                team_id=team_id,
                team_name=str(row["team_name"] or ""),
                instruction=str(row["instruction"] or ""),
                model_id=str(row["model_id"] or ""),
                after=tuple(_json_list(row["after_json"])),
                paths=tuple(_json_list(row["paths_json"])),
                position=order,
            )
        )
    return found


def _team_steps(db: Database, tenant_id: str, team_id: str) -> list[Step]:
    return _steps_by_team(db, tenant_id).get(team_id, [])


def _json_list(value: object) -> list[str]:
    try:
        items = json.loads(str(value or "[]"))
    except ValueError:
        return []
    return [str(item) for item in items] if isinstance(items, list) else []


def _enabled_ids(db: Database, project_id: str) -> set[str]:
    rows = db.fetchall(
        """
        SELECT team_id FROM agent_team_projects
        WHERE project_id = ? AND enabled = 1
        """,
        (project_id,),
    )
    return {str(row["team_id"]) for row in rows}


def _team(db: Database, tenant_id: str, team_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT id, name, job, instruction, model_id FROM agent_teams
        WHERE id = ? AND tenant_id = ?
        """,
        (team_id, tenant_id),
    )


def _team_named(db: Database, tenant_id: str, name: str) -> dict | None:
    return db.fetchone(
        "SELECT id FROM agent_teams WHERE tenant_id = ? AND name = ?",
        (tenant_id, name),
    )


def _checked_model(db: Database, tenant_id: str, model_id: str) -> str:
    linked = (model_id or "").strip()
    if linked and _model(db, tenant_id, linked) is None:
        raise ValueError("unknown model")
    return linked


def _model(db: Database, tenant_id: str, model_id: str) -> dict | None:
    return db.fetchone(
        "SELECT id FROM models WHERE id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    )


def _model_rows(db: Database, tenant_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT models.id, models.name, models.model_name, models.provider,
               model_endpoints.name AS service
        FROM models
        LEFT JOIN model_endpoints ON model_endpoints.id = models.endpoint_id
        WHERE models.tenant_id = ?
        ORDER BY model_endpoints.name, models.name, models.id
        """,
        (tenant_id,),
    )


def _model_choice(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "model_name": row["model_name"] or "",
        "service": row["service"] or row["provider"] or "",
    }
