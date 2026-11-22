"""Local models: a person's own model server, reached through their own runner.

An organization owner or admin turns local models on for everyone, or for the
people they choose. A person it is on for sets the address of their model
server, such as Ollama, on their profile and syncs its model names. A job on
one of those models runs LBW's own loop in LBW, and every model call goes
through that person's runner to the address they set. LBW never calls the
address itself, and a runner only relays calls for its own owner.

The address and model names are settings, not secrets, so they are stored as
they are. Prompts and answers pass through the relay in memory only.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.install import require_account

POLICIES = ("off", "everyone", "chosen")
PROVIDERS = (
    {"id": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434"},
    {"id": "lmstudio", "label": "LM Studio", "base_url": "http://127.0.0.1:1234"},
    {"id": "vllm", "label": "vLLM", "base_url": "http://127.0.0.1:8000"},
    {"id": "openai_compatible", "label": "OpenAI-compatible", "base_url": ""},
)
LOCAL_PREFIX = "local:"
# The OpenAI client needs a URL. `.invalid` never resolves, so a call that
# misses the relay fails instead of reaching some other host.
RELAY_BASE_URL = "http://your-machine.invalid/v1"
_PROVIDER_IDS = {item["id"]: item for item in PROVIDERS}
_URL_LIMIT = 200
_NAME_LIMIT = 200
_NAMES_LIMIT = 500


def is_local_model(model_id: str | None) -> bool:
    return str(model_id or "").startswith(LOCAL_PREFIX)


def local_model_name(model_id: str) -> str:
    return model_id[len(LOCAL_PREFIX) :] if is_local_model(model_id) else ""


# Organization -------------------------------------------------------------------


def org_local_models(db: Database) -> dict:
    """The policy. An owner or admin also sees each person and whether it is on for them."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    manage = can_manage_org(account)
    view = {"policy": _policy(db, tenant_id), "can_manage": manage, "people": []}
    if not manage:
        return view
    rows = {
        row["identity_id"]: row
        for row in db.fetchall("SELECT * FROM local_models WHERE tenant_id = ?", (tenant_id,))
    }
    for user in account["users"]:
        row = rows.get(user["id"]) or {}
        view["people"].append(
            {
                "id": user["id"],
                "name": user["name"],
                "allowed": bool(row.get("allowed")),
                "enabled": bool(row.get("enabled")),
                "models": len(_names(row.get("models_json"))),
            }
        )
    return view


def set_policy(db: Database, policy: str) -> dict:
    account = _require_manager(db)
    if policy not in POLICIES:
        raise ValueError("local models are off, on for everyone, or on for chosen people")
    db.execute("UPDATE tenants SET local_models = ? WHERE id = ?", (policy, account["org"]["id"]))
    return org_local_models(db)


def set_person_allowed(db: Database, identity_id: str, *, allowed: bool) -> dict:
    """Turn local models on or off for one person when the policy is chosen people."""
    account = _require_manager(db)
    tenant_id = account["org"]["id"]
    if not any(user["id"] == identity_id for user in account["users"]):
        raise KeyError(identity_id)
    db.execute(
        """
        INSERT INTO local_models (tenant_id, identity_id, allowed) VALUES (?, ?, ?)
        ON CONFLICT (tenant_id, identity_id) DO UPDATE SET allowed = excluded.allowed
        """,
        (tenant_id, identity_id, 1 if allowed else 0),
    )
    return org_local_models(db)


def allowed_for(db: Database, tenant_id: str, identity_id: str) -> bool:
    """Whether the organization lets this person use a model server on their machine."""
    member = db.fetchone(
        "SELECT rank FROM tenant_memberships WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )
    if member is None:
        return False
    policy = _policy(db, tenant_id)
    if policy == "everyone":
        return True
    if policy == "chosen":
        row = _row(db, tenant_id, identity_id)
        return bool(row and row["allowed"])
    return False


# The person ---------------------------------------------------------------------


def my_local_model(db: Database) -> dict:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    row = _row(db, tenant_id, identity_id) or {}
    provider = str(row.get("provider") or "ollama")
    return {
        "policy": _policy(db, tenant_id),
        "allowed": allowed_for(db, tenant_id, identity_id),
        "enabled": bool(row.get("enabled")),
        "provider": provider,
        "base_url": str(row.get("base_url") or "") or _default_url(provider),
        "models": _names(row.get("models_json")),
        "synced": int(row.get("synced") or 0),
        "providers": [dict(item) for item in PROVIDERS],
    }


def save_my_local_model(db: Database, *, enabled: bool, provider: str, base_url: str) -> dict:
    """Save this person's server. A new address forgets the names synced from the old one."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    if not allowed_for(db, tenant_id, identity_id):
        raise PermissionError("your organization has not turned on local models for you")
    if provider not in _PROVIDER_IDS:
        raise ValueError("choose Ollama, LM Studio, vLLM, or an OpenAI-compatible server")
    url = clean_server_url(base_url or _default_url(provider))
    row = _row(db, tenant_id, identity_id) or {}
    same = row.get("provider") == provider and row.get("base_url") == url
    db.execute(
        """
        INSERT INTO local_models
            (tenant_id, identity_id, enabled, provider, base_url, models_json, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tenant_id, identity_id) DO UPDATE SET
            enabled = excluded.enabled,
            provider = excluded.provider,
            base_url = excluded.base_url,
            models_json = excluded.models_json,
            synced = excluded.synced
        """,
        (
            tenant_id,
            identity_id,
            1 if enabled else 0,
            provider,
            url,
            (row.get("models_json") or "[]") if same else "[]",
            int(row.get("synced") or 0) if same else 0,
        ),
    )
    return my_local_model(db)


def remember_models(db: Database, names: list[str]) -> dict:
    """Store the model names the person's server returned through their runner."""
    target = relay_target(db)
    clean = sorted({str(name).strip()[:_NAME_LIMIT] for name in names if str(name).strip()})
    db.execute(
        """
        UPDATE local_models SET models_json = ?, synced = ?
        WHERE tenant_id = ? AND identity_id = ?
        """,
        (
            json.dumps(clean[:_NAMES_LIMIT]),
            int(time.time()),
            target["tenant_id"],
            target["identity_id"],
        ),
    )
    return my_local_model(db)


def relay_target(db: Database) -> dict:
    """Where the acting person's model calls go. Only ever their own server."""
    account = require_account(db)
    tenant_id = account["org"]["id"]
    identity_id = account["current_user_id"]
    if not allowed_for(db, tenant_id, identity_id):
        raise PermissionError("your organization has not turned on local models for you")
    row = _row(db, tenant_id, identity_id)
    if row is None or not row["enabled"] or not row["base_url"]:
        raise PermissionError("turn on your model server under Profile, Integrations")
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "provider": row["provider"],
        "base_url": row["base_url"],
        "models": _names(row["models_json"]),
    }


def local_choices(db: Database) -> list[dict]:
    """The acting person's synced models, for the job form."""
    try:
        target = relay_target(db)
    except PermissionError:
        return []
    return [
        {
            "id": LOCAL_PREFIX + name,
            "name": name,
            "provider": target["provider"],
            "model_name": name,
            "endpoint_name": _PROVIDER_IDS[target["provider"]]["label"],
            "runner": False,
            "local": True,
        }
        for name in target["models"]
    ]


def use_local_model(db: Database, model_id: str) -> dict:
    """A local model's name and the acting person's server.

    The model resolves against whoever acts now. A coworker who continues a job
    uses their own server and runner, or is refused.
    """
    target = relay_target(db)
    name = local_model_name(model_id)
    if name not in target["models"]:
        raise PermissionError(
            f"{name or 'that model'} is not on your model server: "
            "sync your local models under Profile, Integrations"
        )
    label = _PROVIDER_IDS[target["provider"]]["label"]
    return {
        "model_name": name,
        "base_url": RELAY_BASE_URL,
        "api_key": "",
        "model_id": model_id,
        "endpoint_id": "",
        "service": f"{label} (your machine)",
        "provider": target["provider"],
        "personal": True,
        "runner": False,
        "relay": True,
    }


def clean_server_url(value: str) -> str:
    """An http or https address with no user name, password, query, or fragment."""
    url = str(value or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    if not url:
        raise ValueError("enter your model server's address, such as http://127.0.0.1:11434")
    if len(url) > _URL_LIMIT or " " in url or not url.startswith(("http://", "https://")):
        raise ValueError("the address must be an http or https address")
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("the address takes a host and port, with no user name or query")
    if parsed.fragment or parsed.params:
        raise ValueError("the address takes a host and port, with no user name or query")
    return url


def _default_url(provider: str) -> str:
    item = _PROVIDER_IDS.get(provider)
    return str(item["base_url"]) if item else ""


def _policy(db: Database, tenant_id: str) -> str:
    row = db.fetchone("SELECT local_models FROM tenants WHERE id = ?", (tenant_id,))
    value = str(row["local_models"] or "off") if row else "off"
    return value if value in POLICIES else "off"


def _row(db: Database, tenant_id: str, identity_id: str) -> dict | None:
    return db.fetchone(
        "SELECT * FROM local_models WHERE tenant_id = ? AND identity_id = ?",
        (tenant_id, identity_id),
    )


def _names(raw: object) -> list[str]:
    try:
        names = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(name) for name in names if isinstance(name, str)] if isinstance(names, list) else []


def _require_manager(db: Database) -> dict:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin turns on local models")
    return account
