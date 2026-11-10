"""Organization models and the agents that may build with them.

LBW stays the orchestrator. A model is a bring-your-own endpoint. Turning it on
makes it available on every project until that project turns it off. That
project choice stays when the organization turns the model off and back on.
An agent is a worker LBW can hand the change to. An agent grant is on a
project, a team, or a person. Rank does not include one. Work an agent builds
stays open until a person accepts it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from agent_lbw.llm.client import model_ids_from_payload
from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.crypto import open_secret, seal
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.names import clean_name, new_id
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.tasks import tenant_dek

PROVIDERS = (
    {"id": "openai", "label": "OpenAI", "key": True, "base_url": "https://api.openai.com/v1"},
    {
        "id": "anthropic",
        "label": "Anthropic",
        "key": True,
        "base_url": "https://api.anthropic.com/v1",
    },
    {
        "id": "google",
        "label": "Google",
        "key": True,
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    {"id": "ollama", "label": "Ollama", "key": False, "base_url": "http://127.0.0.1:11434/v1"},
    {"id": "vllm", "label": "vLLM", "key": False, "base_url": ""},
    {"id": "lmstudio", "label": "LM Studio", "key": False, "base_url": "http://127.0.0.1:1234/v1"},
    {"id": "openai_compatible", "label": "OpenAI-compatible", "key": False, "base_url": ""},
)
AGENT_KINDS = (
    {"id": "lbw", "label": "LBW", "model": True},
    {"id": "claude_code", "label": "Claude Code", "model": False},
    {"id": "codex", "label": "Codex", "model": False},
    {"id": "cursor", "label": "Cursor", "model": False},
)
_PROVIDER_IDS = {item["id"] for item in PROVIDERS}
_HOSTED = frozenset({"ollama", "vllm", "lmstudio", "openai_compatible"})
_KIND_IDS = {item["id"] for item in AGENT_KINDS}
_SCOPES = frozenset({"project", "team", "identity"})
_KEY_PROVIDERS = {item["id"] for item in PROVIDERS if item["key"]}


def catalog_for_settings(db: Database) -> dict:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    return {
        "providers": [dict(item) for item in PROVIDERS],
        "agent_kinds": [dict(item) for item in AGENT_KINDS],
        "people": _people(db, tenant_id),
        "endpoints": [_endpoint_view(row) for row in _endpoint_rows(db, tenant_id)],
        "models": [_model_view(row) for row in _model_rows(db, tenant_id)],
        "model_grants": _grants(db, tenant_id, "model"),
        "agents": [_agent_view(row) for row in _agent_rows(db, tenant_id)],
        "agent_grants": _grants(db, tenant_id, "agent"),
    }


def save_endpoint(
    db: Database,
    root: Path,
    *,
    name: str,
    provider: str,
    base_url: str = "",
    api_key: str = "",
) -> str:
    """Save a bring-your-own endpoint and store the model names it returns."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    chosen = provider.strip()
    if chosen not in _PROVIDER_IDS:
        raise ValueError("unknown model provider")
    label = clean_name(name, "endpoint")
    if _named(db, "model_endpoints", tenant_id, label) is not None:
        raise ValueError("that name is already used")
    url = _endpoint_url(chosen, base_url)
    secret = api_key.strip()
    if chosen in _KEY_PROVIDERS and not secret:
        raise ValueError("paste an API key")
    ciphertext = seal(tenant_dek(db, root, tenant_id), secret) if secret else None
    endpoint_id = new_id("ep")
    db.execute(
        """
        INSERT INTO model_endpoints (
            id, tenant_id, name, provider, base_url, api_key_ciphertext, models_json
        ) VALUES (?, ?, ?, ?, ?, ?, '[]')
        """,
        (endpoint_id, tenant_id, label, chosen, url, ciphertext),
    )
    row = _endpoint_row(db, tenant_id, endpoint_id)
    assert row is not None
    return _refresh_names(db, root, row)


def sync_endpoint(db: Database, root: Path, endpoint_id: str) -> str:
    """Ask the endpoint for its model names again and replace the stored list."""
    account = _require_manager(db)
    row = _endpoint_row(db, account["org"]["id"], endpoint_id)
    if row is None:
        raise KeyError(endpoint_id)
    return _refresh_names(db, root, row)


def delete_endpoint(db: Database, endpoint_id: str) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _endpoint_row(db, tenant_id, endpoint_id) is None:
        raise KeyError(endpoint_id)
    used = db.fetchone("SELECT id FROM models WHERE endpoint_id = ?", (endpoint_id,))
    if used is not None:
        raise ValueError("a model still uses this endpoint")
    db.execute(
        "DELETE FROM model_endpoints WHERE id = ? AND tenant_id = ?",
        (endpoint_id, tenant_id),
    )
    return catalog_for_settings(db)


def save_model(
    db: Database,
    _root: Path,
    *,
    endpoint_id: str,
    model_name: str,
    name: str = "",
) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    endpoint = _endpoint_row(db, tenant_id, endpoint_id.strip())
    if endpoint is None:
        raise ValueError("unknown endpoint")
    remote_name = " ".join(str(model_name).split())
    if remote_name not in _stored_names(endpoint["models_json"]):
        raise ValueError("choose a model name that endpoint returned")
    label = clean_name(name or remote_name, "model")
    if _named(db, "models", tenant_id, label) is not None:
        raise ValueError("that name is already used")
    db.execute(
        """
        INSERT INTO models (
            id, tenant_id, endpoint_id, name, provider, model_name, base_url, api_key_ciphertext
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            new_id("model"),
            tenant_id,
            endpoint["id"],
            label,
            endpoint["provider"],
            remote_name,
            endpoint["base_url"],
        ),
    )
    return catalog_for_settings(db)


def delete_model(db: Database, model_id: str) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    row = _model_row(db, tenant_id, model_id)
    if row is None:
        raise KeyError(model_id)
    used = db.fetchone("SELECT id FROM agents WHERE model_id = ?", (model_id,))
    if used is not None:
        raise ValueError("an agent still uses this model")
    db.execute("DELETE FROM model_grants WHERE model_id = ?", (model_id,))
    db.execute("DELETE FROM model_project_settings WHERE model_id = ?", (model_id,))
    db.execute("DELETE FROM models WHERE id = ? AND tenant_id = ?", (model_id, tenant_id))
    return catalog_for_settings(db)


def set_model_grant(
    db: Database,
    *,
    model_id: str,
    scope_type: str,
    scope_id: str,
    granted: bool,
) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _model_row(db, tenant_id, model_id) is None:
        raise KeyError(model_id)
    _require_scope(db, tenant_id, scope_type, scope_id)
    if granted:
        db.execute(
            """
            INSERT INTO model_grants (id, tenant_id, model_id, scope_type, scope_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (model_id, scope_type, scope_id) DO NOTHING
            """,
            (new_id("mgr"), tenant_id, model_id, scope_type, scope_id),
        )
    else:
        db.execute(
            """
            DELETE FROM model_grants
            WHERE model_id = ? AND scope_type = ? AND scope_id = ?
            """,
            (model_id, scope_type, scope_id),
        )
    return catalog_for_settings(db)


def set_model_enabled(db: Database, model_id: str, *, enabled: bool) -> None:
    """Turn a model on for every project, or off."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _model_row(db, tenant_id, model_id) is None:
        raise KeyError(model_id)
    db.execute(
        "UPDATE models SET enabled = ? WHERE id = ? AND tenant_id = ?",
        (1 if enabled else 0, model_id, tenant_id),
    )


def save_agent(
    db: Database,
    *,
    name: str,
    kind: str,
    model_id: str = "",
) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    chosen = kind.strip()
    if chosen not in _KIND_IDS:
        raise ValueError("unknown agent")
    label = clean_name(name, "agent")
    linked = model_id.strip()
    if chosen == "lbw" and not linked:
        raise ValueError("the LBW agent needs a model")
    if linked and _model_row(db, tenant_id, linked) is None:
        raise ValueError("unknown model")
    db.execute(
        """
        INSERT INTO agents (id, tenant_id, name, kind, model_id, human_review)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (new_id("agent"), tenant_id, label, chosen, linked or None),
    )
    return catalog_for_settings(db)


def delete_agent(db: Database, agent_id: str) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _agent_row(db, tenant_id, agent_id) is None:
        raise KeyError(agent_id)
    db.execute("DELETE FROM agent_grants WHERE agent_id = ?", (agent_id,))
    db.execute("DELETE FROM agents WHERE id = ? AND tenant_id = ?", (agent_id, tenant_id))
    return catalog_for_settings(db)


def set_agent_enabled(db: Database, agent_id: str, *, enabled: bool) -> None:
    """Turn an agent on for every project, or off."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _agent_row(db, tenant_id, agent_id) is None:
        raise KeyError(agent_id)
    db.execute(
        "UPDATE agents SET enabled = ? WHERE id = ? AND tenant_id = ?",
        (1 if enabled else 0, agent_id, tenant_id),
    )


def set_agent_grant(
    db: Database,
    *,
    agent_id: str,
    scope_type: str,
    scope_id: str,
    granted: bool,
) -> dict:
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if _agent_row(db, tenant_id, agent_id) is None:
        raise KeyError(agent_id)
    _require_scope(db, tenant_id, scope_type, scope_id)
    if granted:
        db.execute(
            """
            INSERT INTO agent_grants (id, tenant_id, agent_id, scope_type, scope_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (agent_id, scope_type, scope_id) DO NOTHING
            """,
            (new_id("agr"), tenant_id, agent_id, scope_type, scope_id),
        )
    else:
        db.execute(
            """
            DELETE FROM agent_grants
            WHERE agent_id = ? AND scope_type = ? AND scope_id = ?
            """,
            (agent_id, scope_type, scope_id),
        )
    return catalog_for_settings(db)


def model_allowed(db: Database, identity_id: str, project_id: str, model_id: str) -> bool:
    """A model the organization has on, unless this project turned it off."""
    if not can_access(db, identity_id, project_id, need="use"):
        return False
    if not _org_enabled(db, "model", model_id):
        return False
    row = db.fetchone(
        """
        SELECT enabled FROM model_project_settings
        WHERE model_id = ? AND project_id = ?
        """,
        (model_id, project_id),
    )
    return row is None or int(row["enabled"] or 0) == 1


def project_models(db: Database, workspace: Path) -> dict:
    """Every organization model, and whether this project has it on."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    project = _checkout_project(db, tenant_id, workspace)
    if project is None:
        return {"can_manage": False, "models": []}
    identity_id = account["current_user_id"]
    manage = can_manage_org(account) or can_access(db, identity_id, project["id"], need="manage")
    if not manage and not can_access(db, identity_id, project["id"], need="use"):
        return {"can_manage": False, "models": []}
    return {
        "can_manage": manage,
        "models": [_project_model(db, project["id"], row) for row in _model_rows(db, tenant_id)],
    }


def set_project_model(db: Database, workspace: Path, model_id: str, *, enabled: bool) -> dict:
    """Turn a model on or off for one project. The choice outlives the organization switch."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    project = _checkout_project(db, tenant_id, workspace)
    if project is None:
        raise KeyError(model_id)
    identity_id = account["current_user_id"]
    if not can_manage_org(account) and not can_access(
        db, identity_id, project["id"], need="manage"
    ):
        raise PermissionError(
            "an organization admin or a repository admin changes models for this project"
        )
    if _model_row(db, tenant_id, model_id) is None:
        raise KeyError(model_id)
    if enabled:
        db.execute(
            "DELETE FROM model_project_settings WHERE model_id = ? AND project_id = ?",
            (model_id, project["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO model_project_settings (tenant_id, model_id, project_id, enabled)
            VALUES (?, ?, ?, 0)
            ON CONFLICT (model_id, project_id) DO UPDATE SET enabled = 0
            """,
            (tenant_id, model_id, project["id"]),
        )
    return project_models(db, workspace)


def _checkout_project(db: Database, tenant_id: str, workspace: Path) -> dict | None:
    """The project for this checkout, including a GitHub repository stored as owner/repo."""
    from agent_lbw.runtime.store.recording import project_id_for_checkout

    project_id = project_id_for_checkout(db, tenant_id, workspace)
    if project_id is None:
        return None
    row = db.fetchone(
        """
        SELECT id, name, kind, locator FROM projects
        WHERE id = ? AND tenant_id = ?
        """,
        (project_id, tenant_id),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "locator": row["locator"],
    }


def _project_model(db: Database, project_id: str, row: dict) -> dict:
    choice = _model_choice(row)
    saved = db.fetchone(
        """
        SELECT enabled FROM model_project_settings
        WHERE model_id = ? AND project_id = ?
        """,
        (row["id"], project_id),
    )
    choice["org_enabled"] = bool(row["enabled"])
    choice["enabled"] = saved is None or int(saved["enabled"] or 0) == 1
    return choice


def models_for_workspace(db: Database, workspace: Path) -> list[dict]:
    """Models this person may choose for a project. Adding one stays in settings."""
    account = require_account(db)
    project = _checkout_project(db, account["org"]["id"], workspace)
    if project is None:
        return []
    identity_id = account["current_user_id"]
    return [
        _model_choice(row)
        for row in _model_rows(db, account["org"]["id"])
        if model_allowed(db, identity_id, project["id"], row["id"])
    ]


def use_model(db: Database, root: Path, workspace: Path, model_id: str) -> dict:
    """The granted model's name, host, and key. The key is not returned to the page."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    project = _checkout_project(db, tenant_id, workspace)
    if project is None or not model_allowed(
        db, account["current_user_id"], project["id"], model_id
    ):
        raise PermissionError("that model is off for this project")
    row = next((item for item in _model_rows(db, tenant_id) if item["id"] == model_id), None)
    if row is None:
        raise KeyError(model_id)
    secret = ""
    token = row["api_key_ciphertext"]
    if token:
        secret = open_secret(tenant_dek(db, root, tenant_id), token)
    return {
        "model_name": row["model_name"],
        "base_url": row["base_url"] or "",
        "api_key": secret,
    }


def agent_allowed(db: Database, identity_id: str, project_id: str, agent_id: str) -> bool:
    return _allowed(db, identity_id, project_id, "agent", agent_id)


def attach_builder(db: Database, task_id: str, *, agent_id: str) -> dict:
    """Record the worker for a task. The model is the one the agent spends."""
    account = require_account(db)
    task = _task(db, account["org"]["id"], task_id)
    agent = _agent_row(db, account["org"]["id"], agent_id.strip())
    if agent is None:
        raise ValueError("unknown agent")
    identity_id = account["current_user_id"]
    project_id = task["project_id"]
    if not agent_allowed(db, identity_id, project_id, agent["id"]):
        raise PermissionError("that agent is not granted for this project")
    model_id = agent["model_id"] or ""
    if model_id and not model_allowed(db, identity_id, project_id, model_id):
        raise PermissionError("that model is off for this project")
    db.execute(
        """
        UPDATE tasks
        SET agent_id = ?, model_id = ?, reviewer_id = NULL
        WHERE id = ?
        """,
        (agent["id"], model_id or None, task_id),
    )
    return _builder_view(db, task_id)


def accept_review(db: Database, task_id: str) -> dict:
    """A person accepts work an agent built. The reviewer is that person."""
    account = require_account(db)
    task = _task(db, account["org"]["id"], task_id)
    if not task["agent_id"]:
        raise ValueError("this task has no agent")
    identity_id = account["current_user_id"]
    if not can_access(db, identity_id, task["project_id"], need="use"):
        raise PermissionError("that project is closed to this person")
    db.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (identity_id, task_id))
    return _builder_view(db, task_id)


def _allowed(
    db: Database,
    identity_id: str,
    project_id: str,
    kind: str,
    resource_id: str,
) -> bool:
    if not can_access(db, identity_id, project_id, need="use"):
        return False
    if _org_enabled(db, kind, resource_id):
        return True
    table = "model_grants" if kind == "model" else "agent_grants"
    column = "model_id" if kind == "model" else "agent_id"
    direct = db.fetchone(
        f"""
        SELECT id FROM {table}
        WHERE {column} = ? AND (
            (scope_type = 'project' AND scope_id = ?)
            OR (scope_type = 'identity' AND scope_id = ?)
        )
        """,
        (resource_id, project_id, identity_id),
    )
    if direct is not None:
        return True
    team = db.fetchone(
        f"""
        SELECT grants.id
        FROM {table} AS grants
        JOIN team_memberships
          ON team_memberships.team_id = grants.scope_id
         AND team_memberships.identity_id = ?
        JOIN project_grants
          ON project_grants.subject_type = 'team'
         AND project_grants.subject_id = grants.scope_id
         AND project_grants.project_id = ?
        WHERE grants.{column} = ? AND grants.scope_type = 'team'
        """,
        (identity_id, project_id, resource_id),
    )
    return team is not None


def _org_enabled(db: Database, kind: str, resource_id: str) -> bool:
    table = "models" if kind == "model" else "agents"
    row = db.fetchone(f"SELECT enabled FROM {table} WHERE id = ?", (resource_id,))
    return row is not None and int(row["enabled"] or 0) == 1


def _require_manager(db: Database) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin manages organization settings")
    return account


def _require_scope(db: Database, tenant_id: str, scope_type: str, scope_id: str) -> None:
    if scope_type not in _SCOPES:
        raise ValueError("unknown scope")
    if scope_type == "project":
        table, column = "projects", "id"
    elif scope_type == "team":
        table, column = "teams", "id"
    else:
        row = db.fetchone(
            """
            SELECT identity_id FROM tenant_memberships
            WHERE tenant_id = ? AND identity_id = ?
            """,
            (tenant_id, scope_id),
        )
        if row is None:
            raise ValueError("unknown scope")
        return
    row = db.fetchone(
        f"SELECT {column} FROM {table} WHERE id = ? AND tenant_id = ?",
        (scope_id, tenant_id),
    )
    if row is None:
        raise ValueError("unknown scope")


def _endpoint_url(provider: str, value: str) -> str:
    url = value.strip() or _provider_url(provider)
    if provider in _HOSTED and not url:
        raise ValueError("a self-hosted model needs a base URL")
    if len(url) > 200 or not url.startswith(("http://", "https://")) or " " in url:
        raise ValueError("base URL must be an http or https address")
    return url.rstrip("/")


def _provider_url(provider: str) -> str:
    for item in PROVIDERS:
        if item["id"] == provider:
            return str(item["base_url"])
    return ""


def _refresh_names(db: Database, root: Path, row: dict) -> str:
    secret = ""
    token = row["api_key_ciphertext"]
    if token:
        secret = open_secret(tenant_dek(db, root, row["tenant_id"]), token)
    try:
        names = _fetch_names(row["provider"], row["base_url"], secret)
    except ValueError as exc:
        return str(exc)
    db.execute(
        "UPDATE model_endpoints SET models_json = ? WHERE id = ?",
        (json.dumps(names), row["id"]),
    )
    return ""


def _fetch_names(provider: str, base_url: str, api_key: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif provider == "google":
        if api_key:
            headers["x-goog-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(url, headers=headers, timeout=8.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise ValueError("could not reach that endpoint") from exc
    if response.status_code in (401, 403):
        raise ValueError("that endpoint refused the credentials")
    if response.status_code >= 400:
        raise ValueError(f"that endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("that endpoint did not return model names") from exc
    names = _names_from(provider, payload)
    if not names:
        raise ValueError("that endpoint returned no model names")
    return names


def _names_from(provider: str, payload: object) -> list[str]:
    if provider == "google" and isinstance(payload, dict):
        rows = payload.get("models")
        found: list[str] = []
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                raw = str(item.get("name") or "")
                if raw.startswith("models/"):
                    raw = raw.split("/", 1)[1]
                if raw:
                    found.append(raw)
        return _limit_names(found)
    return _limit_names(model_ids_from_payload(payload))


def _limit_names(names: list[str]) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = " ".join(str(name).split())
        if not text or len(text) > 120 or text in seen:
            continue
        seen.add(text)
        kept.append(text)
        if len(kept) == 200:
            break
    return kept


def _stored_names(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _named(db: Database, table: str, tenant_id: str, name: str) -> dict | None:
    return db.fetchone(
        f"SELECT id FROM {table} WHERE tenant_id = ? AND name = ?",
        (tenant_id, name),
    )


def _people(db: Database, tenant_id: str) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT identities.id, identities.display_name
        FROM identities
        JOIN tenant_memberships ON tenant_memberships.identity_id = identities.id
        WHERE tenant_memberships.tenant_id = ?
        ORDER BY identities.display_name, identities.id
        """,
        (tenant_id,),
    )
    return [{"id": row["id"], "name": row["display_name"]} for row in rows]


def _endpoint_rows(db: Database, tenant_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT id, name, provider, base_url, api_key_ciphertext, models_json
        FROM model_endpoints WHERE tenant_id = ?
        ORDER BY name, id
        """,
        (tenant_id,),
    )


def _endpoint_row(db: Database, tenant_id: str, endpoint_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT id, tenant_id, name, provider, base_url, api_key_ciphertext, models_json
        FROM model_endpoints WHERE id = ? AND tenant_id = ?
        """,
        (endpoint_id, tenant_id),
    )


def _endpoint_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "base_url": row["base_url"] or "",
        "has_api_key": bool(row["api_key_ciphertext"]),
        "model_names": _stored_names(row["models_json"]),
    }


def _model_rows(db: Database, tenant_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT models.id, models.name, models.provider, models.model_name, models.base_url,
               models.enabled, models.endpoint_id, endpoints.name AS endpoint_name,
               endpoints.api_key_ciphertext
        FROM models
        LEFT JOIN model_endpoints AS endpoints ON endpoints.id = models.endpoint_id
        WHERE models.tenant_id = ?
        ORDER BY models.name, models.id
        """,
        (tenant_id,),
    )


def _model_row(db: Database, tenant_id: str, model_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT id, name, provider, model_name, base_url, api_key_ciphertext
        FROM models WHERE id = ? AND tenant_id = ?
        """,
        (model_id, tenant_id),
    )


def _agent_rows(db: Database, tenant_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT id, name, kind, model_id, human_review, enabled
        FROM agents WHERE tenant_id = ?
        ORDER BY name, id
        """,
        (tenant_id,),
    )


def _agent_row(db: Database, tenant_id: str, agent_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT id, name, kind, model_id, human_review
        FROM agents WHERE id = ? AND tenant_id = ?
        """,
        (agent_id, tenant_id),
    )


def _model_choice(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "model_name": row["model_name"],
        "endpoint_name": row["endpoint_name"] or "",
    }


def _model_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "model_name": row["model_name"],
        "base_url": row["base_url"] or "",
        "endpoint_id": row["endpoint_id"] or "",
        "endpoint_name": row["endpoint_name"] or "",
        "has_api_key": bool(row["api_key_ciphertext"]),
        "enabled": bool(row["enabled"]),
    }


def _agent_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "model_id": row["model_id"] or "",
        "human_review": True,
        "enabled": bool(row["enabled"]),
    }


def _grants(db: Database, tenant_id: str, kind: str) -> list[dict]:
    table = "model_grants" if kind == "model" else "agent_grants"
    column = "model_id" if kind == "model" else "agent_id"
    rows = db.fetchall(
        f"""
        SELECT id, {column} AS resource_id, scope_type, scope_id
        FROM {table}
        WHERE tenant_id = ?
        ORDER BY scope_type, scope_id, id
        """,
        (tenant_id,),
    )
    return [
        {
            "id": row["id"],
            "resource_id": row["resource_id"],
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "label": _scope_label(db, tenant_id, row["scope_type"], row["scope_id"]),
        }
        for row in rows
    ]


def _scope_label(db: Database, tenant_id: str, scope_type: str, scope_id: str) -> str:
    if scope_type == "project":
        row = db.fetchone(
            "SELECT name FROM projects WHERE id = ? AND tenant_id = ?",
            (scope_id, tenant_id),
        )
        return row["name"] if row else scope_id
    if scope_type == "team":
        row = db.fetchone(
            "SELECT name FROM teams WHERE id = ? AND tenant_id = ?",
            (scope_id, tenant_id),
        )
        return f"Team {row['name']}" if row else scope_id
    row = db.fetchone("SELECT display_name FROM identities WHERE id = ?", (scope_id,))
    return row["display_name"] if row else scope_id


def _task(db: Database, tenant_id: str, task_id: str) -> dict:
    row = db.fetchone(
        """
        SELECT id, tenant_id, project_id, agent_id, model_id, reviewer_id
        FROM tasks WHERE id = ? AND tenant_id = ?
        """,
        (task_id, tenant_id),
    )
    if row is None:
        raise KeyError(task_id)
    return row


def _builder_view(db: Database, task_id: str) -> dict:
    row = db.fetchone(
        """
        SELECT id, agent_id, model_id, reviewer_id
        FROM tasks WHERE id = ?
        """,
        (task_id,),
    )
    if row is None:
        raise KeyError(task_id)
    return {
        "id": row["id"],
        "agent_id": row["agent_id"] or "",
        "model_id": row["model_id"] or "",
        "reviewer_id": row["reviewer_id"] or "",
    }
