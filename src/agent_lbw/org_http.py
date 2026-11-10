"""Organization settings: skill assignments and enforced roles."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.roles import roles_catalog
from agent_lbw.runtime.skills import list_skills
from agent_lbw.runtime.store.assignments import (
    can_manage_org,
    enforced_role_names,
    list_skill_grants,
    set_enforced_role,
    set_skill_grant,
    skills_for_project,
)
from agent_lbw.runtime.store.catalog import accept_review as accept_agent_review
from agent_lbw.runtime.store.catalog import (
    attach_builder,
    catalog_for_settings,
    delete_agent,
    delete_endpoint,
    delete_model,
    models_for_workspace,
    save_agent,
    save_endpoint,
    save_model,
    set_agent_enabled,
    set_agent_grant,
    set_model_enabled,
    set_model_grant,
    sync_endpoint,
)
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.rules import project_for_workspace


class SkillGrantBody(BaseModel):
    skill_name: str
    scope_type: str
    scope_id: str
    assigned: bool = True


class EnforcedRoleBody(BaseModel):
    role_name: str
    enforced: bool = True


class EndpointBody(BaseModel):
    name: str
    provider: str
    base_url: str = ""
    api_key: str = ""


class ModelBody(BaseModel):
    endpoint_id: str
    model_name: str
    name: str = ""


class AgentBody(BaseModel):
    name: str
    kind: str
    model_id: str = ""


class EnabledBody(BaseModel):
    enabled: bool = False


class CatalogGrantBody(BaseModel):
    scope_type: str
    scope_id: str
    granted: bool = True


class BuilderBody(BaseModel):
    agent_id: str


class SkillRepoBody(BaseModel):
    url: str
    scope_type: str
    scope_id: str
    ref: str = ""


def register_org(app: FastAPI) -> None:
    @app.get("/api/org/settings", response_model=None)
    def read_settings(request: Request) -> object:
        return _act(request, lambda db, root: _settings(db, root))

    @app.put("/api/org/skills", response_model=None)
    def write_skill(request: Request, payload: SkillGrantBody) -> object:
        def apply(db, root: Path) -> dict:
            if payload.skill_name.strip() not in _bundled_skill_names(root):
                raise ValueError("unknown skill")
            set_skill_grant(
                db,
                skill_name=payload.skill_name,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                assigned=payload.assigned,
            )
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/roles", response_model=None)
    def write_role(request: Request, payload: EnforcedRoleBody) -> object:
        def apply(db, root: Path) -> dict:
            known = {role["name"] for role in _bundled_roles(root)}
            if payload.role_name.strip() not in known:
                raise ValueError("unknown role")
            set_enforced_role(db, payload.role_name, enforced=payload.enforced)
            return _settings(db, root)

        return _act(request, apply)

    @app.post("/api/org/skill-repos", response_model=None)
    def add_skill_repo(request: Request, payload: SkillRepoBody) -> object:
        def apply(db, root: Path, kiwi) -> dict:
            from agent_lbw.runtime.store.skill_repos import add_repo

            add_repo(
                db,
                kiwi,
                require_account(db),
                cache=root,
                url=payload.url,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                ref=payload.ref,
            )
            return _settings(db, root)

        return _act_kiwi(request, apply)

    @app.post("/api/org/skill-repos/{repo_id}/sync", response_model=None)
    def sync_skill_repo(request: Request, repo_id: str) -> object:
        def apply(db, root: Path, kiwi) -> dict:
            from agent_lbw.runtime.store.skill_repos import sync_repo

            sync_repo(db, kiwi, require_account(db), repo_id=repo_id, cache=root)
            return _settings(db, root)

        return _act_kiwi(request, apply)

    @app.put("/api/org/skill-repos/{repo_id}", response_model=None)
    def write_skill_repo(request: Request, repo_id: str, payload: EnabledBody) -> object:
        def apply(db, root: Path) -> dict:
            from agent_lbw.runtime.store.skill_repos import set_repo_enabled

            set_repo_enabled(db, require_account(db), repo_id=repo_id, enabled=payload.enabled)
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/skill-repos/{repo_id}/skills/{skill_name}", response_model=None)
    def write_skill_repo_skill(
        request: Request, repo_id: str, skill_name: str, payload: EnabledBody
    ) -> object:
        def apply(db, root: Path) -> dict:
            from agent_lbw.runtime.store.skill_repos import set_skill_enabled

            set_skill_enabled(
                db,
                require_account(db),
                repo_id=repo_id,
                name=skill_name,
                enabled=payload.enabled,
            )
            return _settings(db, root)

        return _act(request, apply)

    @app.get("/api/org/skill-repos/{repo_id}/skills/{skill_name}", response_model=None)
    def read_skill_repo_skill(request: Request, repo_id: str, skill_name: str) -> object:
        def apply(db, _root: Path, kiwi) -> dict:
            from agent_lbw.runtime.store.skill_repos import skill_document

            return skill_document(db, kiwi, require_account(db), repo_id=repo_id, name=skill_name)

        return _act_kiwi(request, apply)

    @app.delete("/api/org/skill-repos/{repo_id}", response_model=None)
    def delete_skill_repo(request: Request, repo_id: str) -> object:
        def apply(db, root: Path, kiwi) -> dict:
            from agent_lbw.runtime.store.skill_repos import remove_repo

            remove_repo(db, kiwi, require_account(db), repo_id=repo_id)
            return _settings(db, root)

        return _act_kiwi(request, apply)

    @app.post("/api/org/endpoints", response_model=None)
    def add_endpoint(request: Request, payload: EndpointBody) -> object:
        def apply(db, root: Path) -> dict:
            warning = save_endpoint(
                db,
                root,
                name=payload.name,
                provider=payload.provider,
                base_url=payload.base_url,
                api_key=payload.api_key,
            )
            body = _settings(db, root)
            if warning:
                body["endpoint_warning"] = warning
            request.state.audit_action = "endpoint.add"
            request.state.audit_summary = f"Added endpoint {payload.name.strip()}"
            request.state.audit_target_type = "endpoint"
            return body

        return _act(request, apply)

    @app.post("/api/org/endpoints/{endpoint_id}/sync", response_model=None)
    def refresh_endpoint(request: Request, endpoint_id: str) -> object:
        def apply(db, root: Path) -> dict:
            warning = sync_endpoint(db, root, endpoint_id)
            body = _settings(db, root)
            if warning:
                body["endpoint_warning"] = warning
            return body

        return _act(request, apply)

    @app.delete("/api/org/endpoints/{endpoint_id}", response_model=None)
    def remove_endpoint(request: Request, endpoint_id: str) -> object:
        def apply(db, root: Path) -> dict:
            delete_endpoint(db, endpoint_id)
            return _settings(db, root)

        return _act(request, apply)

    @app.get("/api/org/models", response_model=None)
    def list_granted_models(request: Request, workspace: str | None = None) -> object:
        ui = request.app.state.ui
        path = Path(workspace or ui.default_workspace)

        def apply(db, _root: Path) -> dict:
            return {"models": models_for_workspace(db, path)}

        return _act(request, apply)

    @app.post("/api/org/models", response_model=None)
    def add_model(request: Request, payload: ModelBody) -> object:
        def apply(db, root: Path) -> dict:
            save_model(
                db,
                root,
                endpoint_id=payload.endpoint_id,
                model_name=payload.model_name,
                name=payload.name,
            )
            label = payload.name.strip() or payload.model_name
            request.state.audit_action = "model.add"
            request.state.audit_summary = f"Added model {label}"
            request.state.audit_target_type = "model"
            return _settings(db, root)

        return _act(request, apply)

    @app.delete("/api/org/models/{model_id}", response_model=None)
    def remove_model(request: Request, model_id: str) -> object:
        def apply(db, root: Path) -> dict:
            delete_model(db, model_id)
            request.state.audit_action = "model.remove"
            request.state.audit_summary = "Removed a model"
            request.state.audit_target_type = "model"
            request.state.audit_target_id = model_id
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/models/{model_id}/enabled", response_model=None)
    def enable_model(request: Request, model_id: str, payload: EnabledBody) -> object:
        def apply(db, root: Path) -> dict:
            set_model_enabled(db, model_id, enabled=payload.enabled)
            state = "on" if payload.enabled else "off"
            request.state.audit_action = "model.enable"
            request.state.audit_summary = f"Turned a model {state}"
            request.state.audit_target_type = "model"
            request.state.audit_target_id = model_id
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/models/{model_id}/grants", response_model=None)
    def grant_model(request: Request, model_id: str, payload: CatalogGrantBody) -> object:
        def apply(db, root: Path) -> dict:
            set_model_grant(
                db,
                model_id=model_id,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                granted=payload.granted,
            )
            state = "granted" if payload.granted else "removed"
            request.state.audit_action = "model.grant"
            request.state.audit_summary = f"Model grant {state} for a {payload.scope_type}"
            request.state.audit_target_type = "model"
            request.state.audit_target_id = model_id
            return _settings(db, root)

        return _act(request, apply)

    @app.post("/api/org/agents", response_model=None)
    def add_agent(request: Request, payload: AgentBody) -> object:
        def apply(db, root: Path) -> dict:
            save_agent(db, name=payload.name, kind=payload.kind, model_id=payload.model_id)
            return _settings(db, root)

        return _act(request, apply)

    @app.delete("/api/org/agents/{agent_id}", response_model=None)
    def remove_agent(request: Request, agent_id: str) -> object:
        def apply(db, root: Path) -> dict:
            delete_agent(db, agent_id)
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/agents/{agent_id}/enabled", response_model=None)
    def enable_agent(request: Request, agent_id: str, payload: EnabledBody) -> object:
        def apply(db, root: Path) -> dict:
            set_agent_enabled(db, agent_id, enabled=payload.enabled)
            return _settings(db, root)

        return _act(request, apply)

    @app.put("/api/org/agents/{agent_id}/grants", response_model=None)
    def grant_agent(request: Request, agent_id: str, payload: CatalogGrantBody) -> object:
        def apply(db, root: Path) -> dict:
            set_agent_grant(
                db,
                agent_id=agent_id,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                granted=payload.granted,
            )
            return _settings(db, root)

        return _act(request, apply)

    @app.post("/api/tasks/{task_id}/builder", response_model=None)
    def write_builder(request: Request, task_id: str, payload: BuilderBody) -> object:
        return _act(
            request, lambda db, _root: attach_builder(db, task_id, agent_id=payload.agent_id)
        )

    @app.post("/api/tasks/{task_id}/review", response_model=None)
    def write_review(request: Request, task_id: str) -> object:
        return _act(request, lambda db, _root: accept_agent_review(db, task_id))


def assigned_skill_names(root: Path, workspace: Path, database_url: str = "") -> list[str]:
    """Skills the organization assigned to the project at this workspace."""
    try:
        with database(root, database_url) as db:
            account = require_account(db)
            project = project_for_workspace(db, account["org"]["id"], workspace)
            if project is None:
                return []
            return skills_for_project(db, account["org"]["id"], project["id"])
    except (OSError, ValueError):
        return []


def enforced_roles_for(root: Path, database_url: str = "") -> list[str]:
    try:
        with database(root, database_url) as db:
            return enforced_role_names(db)
    except (OSError, ValueError):
        return []


def _settings(db, root: Path) -> dict:
    account = require_account(db)
    skills = _bundled_skills(root)
    roles = _bundled_roles(root)
    known_skills = {skill.name for skill in skills}
    known_roles = {role["name"] for role in roles}
    return {
        "can_manage": can_manage_org(account),
        "org_id": account["org"]["id"],
        "skills": [skill.public_dict() for skill in skills],
        "grants": [grant for grant in list_skill_grants(db) if grant["skill_name"] in known_skills],
        "roles": roles,
        "enforced": [name for name in enforced_role_names(db) if name in known_roles],
        "teams": [
            {"id": row["id"], "name": row["name"]}
            for row in db.fetchall(
                "SELECT id, name FROM teams WHERE tenant_id = ? ORDER BY name, id",
                (account["org"]["id"],),
            )
        ],
        "projects": [
            {
                "id": row["id"],
                "name": row["name"],
                "locator": row["locator"] or "",
                "archived": row["archived_at"] is not None,
            }
            for row in db.fetchall(
                """
                SELECT id, name, locator, archived_at
                FROM projects
                WHERE tenant_id = ?
                ORDER BY name, id
                """,
                (account["org"]["id"],),
            )
        ],
        "skill_repos": _skill_repos(db, account),
        **catalog_for_settings(db),
    }


def _bundled_skills(root: Path):
    return [skill for skill in list_skills(root) if skill.origin == "bundled"]


def _bundled_roles(root: Path) -> list[dict]:
    catalog = roles_catalog(root)
    roles = catalog.get("roles")
    if not isinstance(roles, list):
        return []
    return [role for role in roles if role.get("origin") == "bundled"]


def _bundled_skill_names(root: Path) -> set[str]:
    return {skill.name for skill in _bundled_skills(root)}


def _skill_repos(db, account: dict) -> list[dict]:
    from agent_lbw.runtime.store.skill_repos import repos_for_settings

    return repos_for_settings(db, account)


def _act(request: Request, fn):
    root, database_url = _root(request)
    try:
        load_account(root, database_url=database_url)
        with database(root, database_url) as db:
            return fn(db, root)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _act_kiwi(request: Request, fn):
    from agent_lbw.rules_http import open_kiwi
    from agent_lbw.tools.kiwi import KiwiError, normalize_kiwifs_url

    ui = request.app.state.ui
    kiwi_url = normalize_kiwifs_url(ui.manager.settings.kiwifs_url)
    if not kiwi_url:
        return JSONResponse({"error": "KiwiFS is not configured"}, status_code=400)
    root, database_url = _root(request)
    try:
        load_account(root, database_url=database_url)
        kiwi = open_kiwi(kiwi_url, ui.manager.settings.kiwifs_api_key)
        with database(root, database_url) as db:
            return fn(db, root, kiwi)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except KiwiError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def _root(request: Request) -> tuple[Path, str]:
    ui = request.app.state.ui
    return Path(ui.store_root), ui.manager.settings.database_url
