"""Integrations a person links, the Claude organization link, prices, and usage reports.

An owner or admin turns an integration on by setting a model endpoint to
personal keys or sign-in. Each person links it on their profile. A run they
start, or continue at a gate, uses their linked credential, and usage records
whose credential ran.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from agent_lbw.openapi_models import ClaudeLinkWrite, PriceWrite
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.assignments import can_manage_org
from agent_lbw.runtime.store.audit import record_audit
from agent_lbw.runtime.store.claude_link import (
    CALLBACK_PATH,
    app_view,
    begin_link,
    finish_link,
    remove_app,
    save_app,
)
from agent_lbw.runtime.store.gateway import LinkRequired, person_integrations, unlink
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.recording import project_id_for_checkout
from agent_lbw.runtime.store.usage import (
    CACHE_SECONDS,
    DATASETS,
    build_report,
    can_see_org_usage,
    delete_price,
    list_prices,
    registry,
    report_csv,
    run_report,
    save_price,
)

_REPORT_PARAMS = frozenset(
    {"scope", "dataset", "group_by", "measures", "since", "until", "format", "path", "limit"}
)


def register_integrations(app: FastAPI) -> None:
    @app.get("/api/me/integrations", response_model=None)
    def my_integrations(request: Request) -> object:
        return _act(request, lambda db, root: _mine(db))

    @app.delete("/api/me/integrations/{endpoint_id}", response_model=None)
    def unlink_integration(request: Request, endpoint_id: str) -> object:
        def apply(db, root: Path) -> dict:
            unlink(db, root, endpoint_id)
            _audit(
                db,
                "integration.unlink",
                "Unlinked an integration",
                target_id=endpoint_id,
                path=f"/api/me/integrations/{endpoint_id}",
                method="DELETE",
            )
            request.state.audit_explicit = True
            return _mine(db)

        return _act(request, apply)

    @app.post("/api/me/integrations/{endpoint_id}/claude-link", response_model=None)
    def start_claude_link(request: Request, endpoint_id: str) -> object:
        def apply(db, root: Path) -> dict:
            url = begin_link(db, root, endpoint_id, redirect_uri=_callback(request))
            request.state.audit_explicit = True
            return {"url": url}

        return _act(request, apply)

    @app.get(CALLBACK_PATH, response_model=None)
    def finish_claude_link(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> object:
        def apply(db, root: Path) -> RedirectResponse:
            endpoint_id = finish_link(
                db,
                root,
                code=code,
                state=state,
                error=error,
                redirect_uri=_callback(request),
            )
            _audit(
                db,
                "integration.link",
                "Linked a Claude organization",
                target_id=endpoint_id,
                path=CALLBACK_PATH,
                method="GET",
            )
            return RedirectResponse("/?section=personal&tab=integrations", status_code=303)

        result = _act(request, apply)
        if isinstance(result, JSONResponse) and result.status_code >= 400:
            return RedirectResponse(
                "/?section=personal&tab=integrations&link_error=1", status_code=303
            )
        return result

    @app.get("/api/org/claude-link", response_model=None)
    def read_claude_link(request: Request) -> object:
        return _act(request, lambda db, root: _claude_app(db, request))

    @app.put("/api/org/claude-link", response_model=None)
    def write_claude_link(request: Request, payload: ClaudeLinkWrite) -> object:
        def apply(db, root: Path) -> dict:
            save_app(
                db,
                root,
                client_id=payload.client_id,
                assertion_file=payload.assertion_file,
                profile_api_key=payload.profile_api_key,
            )
            request.state.audit_action = "integration.claude_app"
            request.state.audit_summary = "Saved the Claude organization link settings"
            request.state.audit_target_type = "integration"
            return _claude_app(db, request)

        return _act(request, apply)

    @app.delete("/api/org/claude-link", response_model=None)
    def delete_claude_link(request: Request) -> object:
        def apply(db, root: Path) -> dict:
            remove_app(db)
            request.state.audit_action = "integration.claude_app"
            request.state.audit_summary = "Turned off Claude organization links"
            request.state.audit_target_type = "integration"
            return _claude_app(db, request)

        return _act(request, apply)

    @app.get("/api/org/prices", response_model=None)
    def read_prices(request: Request) -> object:
        return _act(request, lambda db, root: _prices(db))

    @app.put("/api/org/prices", response_model=None)
    def write_price(request: Request, payload: PriceWrite) -> object:
        def apply(db, root: Path) -> dict:
            tenant_id = _manager(db)
            save_price(
                db,
                tenant_id,
                provider=payload.provider,
                model=payload.model,
                input_per_mtok=payload.input_per_mtok,
                output_per_mtok=payload.output_per_mtok,
                cache_read_per_mtok=payload.cache_read_per_mtok,
                cache_write_per_mtok=payload.cache_write_per_mtok,
            )
            request.state.audit_action = "usage.price"
            request.state.audit_summary = f"Priced {payload.model.strip()}"
            request.state.audit_target_type = "price"
            return _prices(db)

        return _act(request, apply)

    @app.delete("/api/org/prices", response_model=None)
    def remove_price(request: Request, model: str, provider: str = "") -> object:
        def apply(db, root: Path) -> dict:
            tenant_id = _manager(db)
            delete_price(db, tenant_id, provider=provider, model=model)
            request.state.audit_action = "usage.price"
            request.state.audit_summary = f"Removed the price for {model.strip()}"
            request.state.audit_target_type = "price"
            return _prices(db)

        return _act(request, apply)

    @app.get("/api/usage/meta", response_model=None)
    def usage_meta(request: Request) -> object:
        def apply(db, root: Path) -> dict:
            account = require_account(db)
            return {**registry(), "can_see_org": can_see_org_usage(db, account)}

        return _act(request, apply)

    @app.get("/api/usage", response_model=None)
    def usage_report(
        request: Request,
        scope: str = "me",
        dataset: str = "tokens",
        group_by: str = "",
        measures: str = "",
        since: str = "",
        until: str = "",
        format: str = "json",
        path: str = "",
        limit: int = 25,
    ) -> object:
        def apply(db, root: Path) -> object:
            account = require_account(db)
            tenant_id = account["org"]["id"]
            filters = {
                key: value
                for key, value in request.query_params.items()
                if key not in _REPORT_PARAMS and value
            }
            chosen = scope.strip() or "me"
            if chosen == "org":
                if not can_see_org_usage(db, account):
                    raise PermissionError("an owner, an admin, or the billing duty sees org usage")
            elif chosen == "project":
                project_id = project_id_for_checkout(
                    db, tenant_id, Path(path or ".").expanduser().resolve()
                )
                if project_id is None:
                    raise ValueError("this workspace is not a project")
                if not (
                    can_see_org_usage(db, account)
                    or can_access(db, account["current_user_id"], project_id, need="use")
                ):
                    raise PermissionError("that project is closed to this person")
                filters["project"] = project_id
            elif chosen == "me":
                filters["person"] = account["current_user_id"]
            else:
                raise ValueError("scope is org, project, or me")
            dataset_dims = DATASETS[dataset].dimensions if dataset in DATASETS else ()
            report = build_report(
                dataset=dataset,
                group_by=[name.strip() for name in group_by.split(",") if name.strip()],
                measures=[name.strip() for name in measures.split(",") if name.strip()],
                since=since,
                until=until,
                filters={key: value for key, value in filters.items() if key in dataset_dims}
                if dataset_dims
                else filters,
                limit=limit,
            )
            result = run_report(db, tenant_id, report, cache_key=str(root))
            headers = {"Cache-Control": f"private, max-age={CACHE_SECONDS}"}
            if format == "csv":
                name = f"lbw-{report.dataset}-{report.since}-{report.until}.csv"
                return Response(
                    report_csv(result),
                    media_type="text/csv",
                    headers={**headers, "Content-Disposition": f'attachment; filename="{name}"'},
                )
            return JSONResponse({**result, "scope": chosen}, headers=headers)

        return _act(request, apply)


def link_required_response(exc: LinkRequired) -> JSONResponse:
    """A run cannot use someone else's credential: ask this person to link theirs."""
    return JSONResponse(exc.body(), status_code=409)


def _mine(db) -> dict:
    account = require_account(db)
    tenant_id = account["org"]["id"]
    ready = db.fetchone("SELECT 1 AS found FROM claude_link_apps WHERE tenant_id = ?", (tenant_id,))
    return {"integrations": person_integrations(db), "claude_link_ready": ready is not None}


def _claude_app(db, request: Request) -> dict:
    view = app_view(db)
    view["callback_url"] = _callback(request)
    return view


def _prices(db) -> dict:
    account = require_account(db)
    return {
        "prices": list_prices(db, account["org"]["id"]),
        "can_manage": can_manage_org(account),
    }


def _manager(db) -> str:
    account = require_account(db)
    if not can_manage_org(account):
        raise PermissionError("an owner or admin sets model prices")
    return account["org"]["id"]


def _audit(db, action: str, summary: str, *, target_id: str, path: str, method: str) -> None:
    account = require_account(db)
    record_audit(
        db,
        tenant_id=account["org"]["id"],
        actor_id=account["current_user_id"],
        action=action,
        summary=summary,
        method=method,
        path=path,
        target_type="integration",
        target_id=target_id,
    )


def _callback(request: Request) -> str:
    return str(request.base_url).rstrip("/") + CALLBACK_PATH


def _act(request: Request, fn):
    ui = request.app.state.ui
    root = Path(ui.store_root)
    database_url = ui.manager.settings.database_url
    try:
        load_account(root, database_url=database_url)
        with database(root, database_url) as db:
            return fn(db, root)
    except LinkRequired as exc:
        return link_required_response(exc)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
