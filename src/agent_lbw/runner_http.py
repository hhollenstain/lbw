"""Routes for runners: linking, health, and the job lease a runner polls.

`/api/runner/...` is called by the `lbw-runner` program with its own bearer
token, not a browser session. `/api/me/runners...` is the person's profile.
Runner calls are not written to the audit log one by one: linking, revoking,
maintenance, and each job sent to a runner are.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from agent_lbw.openapi_models import (
    RunnerApprove,
    RunnerEvents,
    RunnerFinish,
    RunnerHeartbeat,
    RunnerLinkStart,
    RunnerLinkToken,
    RunnerModelAnswer,
)
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.llm_relay import MAX_BODY_BYTES
from agent_lbw.runtime.runner_hub import MAX_PATCH_BYTES
from agent_lbw.runtime.store.audit import record_audit
from agent_lbw.runtime.store.install import require_account
from agent_lbw.runtime.store.runners import (
    RunnerRequired,
    approve_link,
    claim_link,
    extend_lease,
    finish_job,
    heartbeat,
    lease_job,
    list_runners,
    request_command,
    revoke_runner,
    runner_for_token,
    runner_job,
    start_link,
    touch_runner,
)

MAX_WAIT_SECONDS = 25


def register_runners(app: FastAPI) -> None:
    @app.post("/api/runner/link", response_model=None)
    def start_runner_link(request: Request, payload: RunnerLinkStart) -> object:
        request.state.audit_explicit = True

        def apply(db, root: Path) -> dict:
            started = start_link(db, name=payload.name, platform=payload.platform)
            started["verification_url"] = (
                str(request.base_url).rstrip("/") + "/?section=personal&tab=integrations"
            )
            return started

        return _store(request, apply)

    @app.post("/api/runner/link/token", response_model=None)
    def claim_runner_token(request: Request, payload: RunnerLinkToken) -> object:
        request.state.audit_explicit = True

        def apply(db, root: Path) -> object:
            claimed = claim_link(db, payload.device_code)
            if claimed is None:
                return JSONResponse({"status": "pending"}, status_code=428)
            record_audit(
                db,
                tenant_id=claimed["tenant_id"],
                actor_id=claimed["identity_id"],
                action="runner.link",
                summary=f"Linked the runner {claimed['name']}",
                method="POST",
                path="/api/runner/link/token",
                target_type="runner",
                target_id=claimed["runner_id"],
            )
            return {"token": claimed["token"], "runner_id": claimed["runner_id"]}

        return _store(request, apply)

    @app.post("/api/runner/heartbeat", response_model=None)
    def runner_heartbeat(request: Request, payload: RunnerHeartbeat) -> object:
        request.state.audit_explicit = True

        def apply(db, runner: dict) -> dict:
            commands = heartbeat(db, runner, payload.model_dump())
            return {"runner_id": runner["id"], "name": runner["name"], **commands}

        return _as_runner(request, apply)

    @app.get("/api/runner/jobs/next", response_model=None)
    def lease_runner_job(request: Request, wait: int = 0) -> object:
        hub = request.app.state.ui.manager.runner_hub
        deadline = time.monotonic() + max(0, min(int(wait), MAX_WAIT_SECONDS))
        while True:

            def apply(db, runner: dict) -> object:
                touch_runner(db, runner)
                job = lease_job(db, runner)
                if job is None:
                    return None
                payload = hub.payload(job["id"])
                if payload is None:
                    finish_job(db, job["id"], "failed", "LBW restarted before the job started")
                    return None
                return payload

            found = _as_runner(request, apply)
            if isinstance(found, JSONResponse) or found is not None:
                return found
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            time.sleep(1.0)

    @app.post("/api/runner/jobs/{job_id}/events", response_model=None)
    def post_runner_events(request: Request, job_id: str, payload: RunnerEvents) -> object:
        request.state.audit_explicit = True
        hub = request.app.state.ui.manager.runner_hub

        def apply(db, runner: dict) -> dict:
            row = runner_job(db, runner, job_id)
            if row["status"] != "running":
                return {"cancel": True}
            extend_lease(db, job_id)
            events = [item for item in payload.events if isinstance(item, dict)]
            return {"cancel": hub.push(job_id, events)}

        return _as_runner(request, apply)

    @app.post("/api/runner/jobs/{job_id}/finish", response_model=None)
    def finish_runner_job(request: Request, job_id: str, payload: RunnerFinish) -> object:
        request.state.audit_explicit = True
        hub = request.app.state.ui.manager.runner_hub
        if len(payload.patch.encode("utf-8")) > MAX_PATCH_BYTES:
            return JSONResponse({"error": "the change is larger than LBW accepts"}, status_code=413)

        def apply(db, runner: dict) -> object:
            row = runner_job(db, runner, job_id)
            if row["status"] != "running":
                return JSONResponse({"error": "this job is no longer running"}, status_code=409)
            status = "done" if payload.status == "done" else "failed"
            delivered = hub.finish(
                job_id,
                {
                    "status": status,
                    "summary": payload.summary,
                    "error": payload.error,
                    "patch": payload.patch,
                },
            )
            if not delivered:
                finish_job(db, job_id, "failed", "LBW restarted while the runner worked")
                return JSONResponse({"error": "LBW is no longer waiting for this job"}, 409)
            return {"ok": True}

        return _as_runner(request, apply)

    @app.get("/api/runner/llm/next", response_model=None)
    def next_runner_model_call(request: Request, wait: int = 0) -> object:
        """A model call for this runner's owner. Only its own calls are ever handed out.

        The runner confirms the call at once. LBW offers an unconfirmed call
        again, in case this poll belonged to a runner that has since died.
        """
        relay = request.app.state.ui.manager.llm_relay

        def apply(db, runner: dict) -> dict:
            touch_runner(db, runner)
            return runner

        runner = _as_runner(request, apply)
        if isinstance(runner, JSONResponse):
            return runner
        seconds = max(0, min(int(wait), MAX_WAIT_SECONDS))
        found = relay.next(runner["id"], runner["identity_id"], seconds)
        if found is None:
            return Response(status_code=204)
        return found

    @app.post("/api/runner/llm/{call_id}/ack", response_model=None)
    def ack_runner_model_call(request: Request, call_id: str) -> object:
        request.state.audit_explicit = True
        relay = request.app.state.ui.manager.llm_relay

        def apply(db, runner: dict) -> object:
            if not relay.ack(call_id, runner["id"]):
                return JSONResponse({"error": "LBW is no longer waiting for this call"}, 409)
            return {"ok": True}

        return _as_runner(request, apply)

    @app.post("/api/runner/llm/{call_id}", response_model=None)
    def answer_runner_model_call(
        request: Request, call_id: str, payload: RunnerModelAnswer
    ) -> object:
        request.state.audit_explicit = True
        relay = request.app.state.ui.manager.llm_relay
        if len(payload.body.encode("utf-8")) > MAX_BODY_BYTES:
            return JSONResponse({"error": "the answer is larger than LBW accepts"}, status_code=413)

        def apply(db, runner: dict) -> object:
            delivered = relay.answer(
                call_id,
                runner["id"],
                {"status": payload.status, "body": payload.body, "error": payload.error},
            )
            if not delivered:
                return JSONResponse({"error": "LBW is no longer waiting for this call"}, 409)
            return {"ok": True}

        return _as_runner(request, apply)

    @app.get("/api/me/runners", response_model=None)
    def my_runners(request: Request) -> object:
        return _as_person(request, lambda db: list_runners(db))

    @app.post("/api/me/runners/approve", response_model=None)
    def approve_runner(request: Request, payload: RunnerApprove) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            approved = approve_link(db, payload.user_code)
            _audit(
                db,
                "runner.approve",
                f"Approved linking the runner {approved['name']}",
                target_id="",
                path="/api/me/runners/approve",
                method="POST",
            )
            return {**list_runners(db), "approved": approved}

        return _as_person(request, apply)

    @app.delete("/api/me/runners/{runner_id}", response_model=None)
    def remove_runner(request: Request, runner_id: str) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            removed = revoke_runner(db, runner_id)
            _audit(
                db,
                "runner.revoke",
                f"Disconnected the runner {removed['name']}",
                target_id=runner_id,
                path=f"/api/me/runners/{runner_id}",
                method="DELETE",
            )
            return list_runners(db)

        return _as_person(request, apply)

    @app.post("/api/me/runners/{runner_id}/update-claude", response_model=None)
    def update_runner_claude(request: Request, runner_id: str) -> object:
        request.state.audit_explicit = True

        def apply(db) -> dict:
            asked = request_command(db, runner_id, "update_claude")
            _audit(
                db,
                "runner.update_claude",
                f"Asked the runner {asked['name']} to update Claude Code",
                target_id=runner_id,
                path=f"/api/me/runners/{runner_id}/update-claude",
                method="POST",
            )
            return list_runners(db)

        return _as_person(request, apply)


def runner_required_response(exc: RunnerRequired) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=409)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _paths(request: Request) -> tuple[Path, str]:
    ui = request.app.state.ui
    return Path(ui.store_root), ui.manager.settings.database_url


def _store(request: Request, fn):
    root, url = _paths(request)
    try:
        with database(root, url) as db:
            return fn(db, root)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _as_runner(request: Request, fn):
    root, url = _paths(request)
    try:
        with database(root, url) as db:
            runner = runner_for_token(db, _bearer(request))
            return fn(db, runner)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _as_person(request: Request, fn):
    root, url = _paths(request)
    try:
        load_account(root, database_url=url)
        with database(root, url) as db:
            return fn(db)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


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
        target_type="runner",
        target_id=target_id,
    )
