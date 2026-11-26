"""Login and the first admin account. The session cookie is not returned."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from lbw_shared.directory import request_directory
from pydantic import BaseModel

from agent_lbw.openapi_models import SignupBody
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import Database, database
from agent_lbw.runtime.store.auth import (
    auth_status,
    claim_admin,
    close_session,
    current_actor,
    has_password,
    login,
    open_organization,
    open_personal_account,
    reset_actor,
    reset_tenant,
    session_context,
    set_actor,
    set_tenant,
)
from agent_lbw.runtime.store.github import GitHubAppError, begin_github_login
from agent_lbw.runtime.store.install import read_account
from agent_lbw.runtime.store.runners import RUNNER_PREFIX
from agent_lbw.runtime.store.sso import begin_sso, finish_sso

_COOKIE = "lbw_session"
_MAX_AGE = 14 * 24 * 60 * 60


class SetupBody(BaseModel):
    name: str
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


def register_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_session(request: Request, call_next):
        root, url = _store(request)
        cookie = request.cookies.get(_COOKIE, "")
        identity = None
        tenant = None
        locked = False
        try:
            with database(root, url) as db:
                found = session_context(db, cookie)
                if found is not None:
                    identity, tenant = found
                locked = has_password(db)
        except (OSError, ValueError):
            identity = None
            tenant = None
        token = set_actor(identity) if identity else None
        place = set_tenant(tenant) if tenant else None
        try:
            path = request.url.path
            if (
                locked
                and identity is None
                and path.startswith("/api/")
                and not path.startswith("/api/auth")
                and path != "/api/github/callback"
                and not path.startswith(RUNNER_PREFIX)
            ):
                return JSONResponse({"error": "sign in"}, status_code=401)
            return await call_next(request)
        finally:
            if token is not None:
                reset_actor(token)
            if place is not None:
                reset_tenant(place)

    @app.get("/api/auth/status", response_model=None)
    def status(request: Request) -> object:
        root, url = _store(request)
        with database(root, url) as db:
            body = auth_status(db)
        body["signed_in"] = current_actor() is not None
        return body

    @app.post("/api/auth/setup", response_model=None)
    def setup(request: Request, payload: SetupBody) -> object:
        root, url = _store(request)
        try:
            load_account(root, database_url=url)
            with database(root, url) as db:
                session = claim_admin(
                    db, name=payload.name, email=payload.email, password=payload.password
                )
                account = read_account(db)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _session(account or {}, session)

    @app.post("/api/auth/login", response_model=None)
    def sign_in(request: Request, payload: LoginBody) -> object:
        root, url = _store(request)
        try:
            with database(root, url) as db:
                session = login(db, email=payload.email, password=payload.password)
                account = _account_for_session(db, session)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _session(account or {}, session)

    @app.post("/api/auth/signup", response_model=None)
    def signup(request: Request, payload: SignupBody) -> object:
        root, url = _store(request)
        try:
            with database(root, url) as db:
                if payload.kind == "personal":
                    session = open_personal_account(
                        db, name=payload.name, email=payload.email, password=payload.password
                    )
                elif payload.kind == "organization":
                    session = open_organization(
                        db,
                        name=payload.name,
                        email=payload.email,
                        password=payload.password,
                        organization=payload.organization,
                    )
                else:
                    raise ValueError("choose a personal account or an organization")
                account = _account_for_session(db, session)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _session(account or {}, session)

    @app.get("/api/auth/sso", response_model=None)
    def start_sso(request: Request) -> object:
        root, url = _store(request)
        try:
            load_account(root, database_url=url)
            with database(root, url) as db:
                target = begin_sso(db, root, redirect_uri=_sso_callback(request))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse(target, status_code=303)

    @app.get("/api/auth/sso/callback", response_model=None)
    def sso_callback(request: Request, code: str = "", state: str = "") -> object:
        return _finish_sso(request, code=code, state=state, saml_response="")

    @app.post("/api/auth/sso/callback", response_model=None)
    async def sso_callback_post(request: Request) -> object:
        raw = (await request.body()).decode("utf-8", "replace")
        fields = parse_qs(raw)
        return _finish_sso(
            request,
            code="",
            state=fields.get("RelayState", [""])[0],
            saml_response=fields.get("SAMLResponse", [""])[0],
        )

    @app.get("/api/auth/github", response_model=None)
    def github_sign_in(request: Request) -> object:
        root, url = _store(request)
        try:
            load_account(root, database_url=url)
            with database(root, url) as db:
                target = begin_github_login(db, root, redirect_uri=_github_callback(request))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except GitHubAppError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return RedirectResponse(target, status_code=303)

    @app.post("/api/auth/logout", response_model=None)
    def sign_out(request: Request) -> object:
        root, url = _store(request)
        with database(root, url) as db:
            close_session(db, request.cookies.get(_COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(_COOKIE, path="/")
        return response


def _store(request: Request) -> tuple[Path, str]:
    directory = request_directory(request)
    return directory.root, directory.database_url


def _account_for_session(db: Database, token: str) -> dict | None:
    found = session_context(db, token)
    if found is None:
        return read_account(db)
    actor = set_actor(found[0])
    place = set_tenant(found[1])
    try:
        return read_account(db)
    finally:
        reset_actor(actor)
        reset_tenant(place)


def _finish_sso(request: Request, *, code: str, state: str, saml_response: str) -> object:
    root, url = _store(request)
    try:
        load_account(root, database_url=url)
        with database(root, url) as db:
            session = finish_sso(
                db,
                root,
                code=code,
                state=state,
                saml_response=saml_response,
                redirect_uri=_sso_callback(request),
            )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        _COOKIE,
        session,
        httponly=True,
        samesite="lax",
        max_age=_MAX_AGE,
        path="/",
    )
    return response


def _sso_callback(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/auth/sso/callback"


def _github_callback(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/github/callback"


def _session(payload: dict, token: str) -> JSONResponse:
    response = JSONResponse(payload)
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=_MAX_AGE,
        path="/",
    )
    return response
