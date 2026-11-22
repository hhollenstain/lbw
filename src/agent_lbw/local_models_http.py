"""Routes for local models: the organization's policy and each person's own server.

Syncing asks the person's own runner to list the models on the server they set.
LBW never calls that address itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_lbw.llm.client import model_ids_from_payload
from agent_lbw.openapi_models import LocalModelPerson, LocalModelPolicy, LocalModelServer
from agent_lbw.runner_http import runner_required_response
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.llm_relay import RelayError
from agent_lbw.runtime.store.audit import record_audit
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.local_models import (
    my_local_model,
    org_local_models,
    relay_target,
    remember_models,
    save_my_local_model,
    set_person_allowed,
    set_policy,
)
from agent_lbw.runtime.store.runners import RunnerRequired, pick_relay_runner

SYNC_SECONDS = 20.0


def register_local_models(app: FastAPI) -> None:
    @app.get("/api/org/local-models", response_model=None)
    def get_local_model_policy(request: Request) -> object:
        return _act(request, org_local_models)

    @app.put("/api/org/local-models", response_model=None)
    def put_local_model_policy(request: Request, payload: LocalModelPolicy) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            view = set_policy(db, payload.policy)
            _audit(
                db,
                "local_models.policy",
                f"Set local models to {payload.policy}",
                target_type="tenant",
                target_id=require_account(db)["org"]["id"],
                path="/api/org/local-models",
            )
            return view

        return _act(request, apply)

    @app.put("/api/org/local-models/people/{identity_id}", response_model=None)
    def put_local_model_person(
        request: Request, identity_id: str, payload: LocalModelPerson
    ) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            view = set_person_allowed(db, identity_id, allowed=payload.allowed)
            name = next((p["name"] for p in view["people"] if p["id"] == identity_id), "")
            _audit(
                db,
                "local_models.person",
                f"Turned local models {'on' if payload.allowed else 'off'} for {name}",
                target_type="identity",
                target_id=identity_id,
                path=f"/api/org/local-models/people/{identity_id}",
            )
            return view

        return _act(request, apply)

    @app.get("/api/me/local-model", response_model=None)
    def get_my_local_model(request: Request) -> object:
        return _act(request, my_local_model)

    @app.put("/api/me/local-model", response_model=None)
    def put_my_local_model(request: Request, payload: LocalModelServer) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            view = save_my_local_model(
                db,
                enabled=payload.enabled,
                provider=payload.provider,
                base_url=payload.base_url,
            )
            account = require_account(db)
            _audit(
                db,
                "local_models.server",
                f"Saved {_label(view)} as the model server, {'on' if view['enabled'] else 'off'}",
                target_type="identity",
                target_id=account["current_user_id"],
                path="/api/me/local-model",
            )
            return view

        return _act(request, apply)

    @app.post("/api/me/local-model/sync", response_model=None)
    def sync_my_local_model(request: Request) -> object:
        request.state.audit_explicit = True
        relay = request.app.state.ui.manager.llm_relay

        def target(db) -> dict:
            found = relay_target(db)
            pick_relay_runner(db, found["tenant_id"], found["identity_id"])
            return found

        found = _act(request, target)
        if isinstance(found, JSONResponse):
            return found
        try:
            answer = relay.call(
                identity_id=found["identity_id"],
                method="GET",
                path="/v1/models",
                body="",
                base_url=found["base_url"],
                pickup_seconds=SYNC_SECONDS,
                call_seconds=SYNC_SECONDS,
            )
        except RelayError as exc:
            return JSONResponse({"error": str(exc)}, status_code=504)
        if answer.get("error"):
            return JSONResponse({"error": str(answer["error"])}, status_code=502)
        if int(answer.get("status") or 0) >= 400:
            return JSONResponse(
                {"error": f"your model server answered {answer.get('status')}"}, status_code=502
            )
        try:
            names = model_ids_from_payload(json.loads(str(answer.get("body") or "")))
        except ValueError:
            return JSONResponse(
                {"error": "your model server did not return a model list"}, status_code=502
            )

        def apply(db) -> dict:
            view = remember_models(db, names)
            _audit(
                db,
                "local_models.sync",
                f"Synced {len(view['models'])} local models",
                target_type="identity",
                target_id=found["identity_id"],
                path="/api/me/local-model/sync",
            )
            return view

        return _act(request, apply)


def _label(view: dict) -> str:
    return next(
        (item["label"] for item in view["providers"] if item["id"] == view["provider"]),
        view["provider"],
    )


def _act(request: Request, fn):
    ui = request.app.state.ui
    root, url = Path(ui.store_root), ui.manager.settings.database_url
    try:
        load_account(root, database_url=url)
        with database(root, url) as db:
            return fn(db)
    except RunnerRequired as exc:
        return runner_required_response(exc)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _audit(db, action: str, summary: str, *, target_type: str, target_id: str, path: str) -> None:
    account = require_account(db)
    record_audit(
        db,
        tenant_id=account["org"]["id"],
        actor_id=account["current_user_id"],
        action=action,
        summary=summary,
        method="PUT" if not path.endswith("/sync") else "POST",
        path=path,
        target_type=target_type,
        target_id=target_id,
    )
