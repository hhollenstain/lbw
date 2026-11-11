"""GitHub App routes. The page saves the app here. Tokens are not returned."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from lbw_shared.directory import request_directory
from pydantic import BaseModel

from agent_lbw.openapi_models import RepoEnabled
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.store.github import (
    GitHubAppError,
    begin_user_connect,
    current_token,
    finish_user_connect,
    github_app_state,
    install_url,
    list_installations,
    save_github_app,
    select_installation,
)
from agent_lbw.runtime.store.github_sync import (
    claim_repo,
    clear_github_login,
    place_repo,
    set_repo_enabled,
    stored_repos,
    sync_access,
)
from agent_lbw.runtime.workspaces import clone_workspace, durable_checkout_parent
from agent_lbw.tools.git import github_auth_args, github_https_url


class GitHubAppBody(BaseModel):
    app_id: str
    private_key: str = ""
    client_secret: str = ""


class InstallationBody(BaseModel):
    installation_id: str


class GitHubWorkspaceBody(BaseModel):
    full_name: str
    subject_type: str
    subject_id: str = ""
    parent: str = ""


def register_github_app(app: FastAPI) -> None:
    """The key lives on the engineer dashboard. The organization site reads the same row."""

    @app.get("/api/github/app", response_model=None)
    def read_app(request: Request) -> object:
        return _act(request, github_app_state)

    @app.put("/api/github/app", response_model=None)
    def write_app(request: Request, payload: GitHubAppBody) -> object:
        return _act(
            request,
            lambda db, root: save_github_app(
                db,
                root,
                app_id=payload.app_id,
                private_key=payload.private_key,
                client_secret=payload.client_secret,
            ),
        )


def register_github(app: FastAPI) -> None:
    register_github_app(app)

    @app.get("/api/github/installations", response_model=None)
    def read_installations(request: Request) -> object:
        return _act(
            request,
            lambda db, root: {
                "installations": list_installations(db, root),
                "install_url": install_url(db, root),
            },
        )

    @app.put("/api/github/installation", response_model=None)
    def write_installation(request: Request, payload: InstallationBody) -> object:
        return _act(
            request,
            lambda db, root: select_installation(db, root, payload.installation_id),
        )

    @app.get("/api/github/repos", response_model=None)
    def read_repos(request: Request) -> object:
        return _act(request, lambda db, _root: {"repos": stored_repos(db)})

    @app.put("/api/github/repos/enabled", response_model=None)
    def enable_repo(request: Request, payload: RepoEnabled) -> object:
        return _act(
            request,
            lambda db, _root: {"repos": set_repo_enabled(db, payload.project_id, payload.enabled)},
        )

    @app.get("/api/github/connect", response_model=None)
    def connect_github(request: Request) -> object:
        return _act(
            request,
            lambda db, root: {"url": begin_user_connect(db, root, redirect_uri=_callback(request))},
        )

    @app.delete("/api/github/login", response_model=None)
    def clear_login(request: Request) -> object:
        return _act(request, lambda db, _root: clear_github_login(db))

    @app.get("/api/github/callback", response_model=None)
    def github_callback(request: Request, code: str = "", state: str = "") -> object:
        if not code or not state:
            return RedirectResponse("/?github=expired", status_code=303)
        root, database_url = _root(request)
        try:
            load_account(root, database_url=database_url)
            with database(root, database_url) as db:
                done = finish_user_connect(
                    db, root, code=code, state=state, redirect_uri=_callback(request)
                )
        except ValueError as exc:
            message = str(exc)
            if "already connected" in message:
                kind = "taken"
            elif "expired" in message:
                kind = "expired"
            elif "not a person" in message:
                kind = "unknown"
            elif "did not share an email" in message:
                kind = "email"
            else:
                kind = "refused"
            return RedirectResponse(f"/?github={kind}", status_code=303)
        except (GitHubAppError, PermissionError, KeyError):
            return RedirectResponse("/?github=refused", status_code=303)
        response = RedirectResponse("/?github=connected", status_code=303)
        session = done.get("session") if isinstance(done, dict) else None
        if session:
            response.set_cookie(
                "lbw_session",
                str(session),
                httponly=True,
                samesite="lax",
                max_age=14 * 24 * 60 * 60,
                path="/",
            )
        return response

    @app.post("/api/github/sync", response_model=None)
    def sync_github(request: Request) -> object:
        return _act(request, sync_access)

    @app.post("/api/github/workspaces", response_model=None)
    def add_workspace(request: Request, payload: GitHubWorkspaceBody) -> object:
        root, database_url = _root(request)
        try:
            load_account(root, database_url=database_url)
            with database(root, database_url) as db:
                full_name = claim_repo(db, root, payload.full_name)
            parent = _clone_parent(request, payload.parent)
            owner, repo = full_name.split("/", 1)
            token = current_token(root, database_url)
            dest = Path(parent).expanduser().resolve() / repo
            if dest.exists():
                path = str(dest)
            else:
                cloned = clone_workspace(
                    github_https_url(owner, repo),
                    parent,
                    name=repo,
                    git_config=github_auth_args(token) if token else None,
                )
                path = str(cloned["path"])
            with database(root, database_url) as db:
                placed = place_repo(
                    db,
                    full_name=full_name,
                    path=path,
                    subject_type=payload.subject_type,
                    subject_id=payload.subject_id,
                )
                sync_access(db, root)
                return placed
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except GitHubAppError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except KeyError:
            return JSONResponse({"error": "person not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)


def _callback(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/github/callback"


def _act(request: Request, fn):
    root, database_url = _root(request)
    try:
        load_account(root, database_url=database_url)
        with database(root, database_url) as db:
            return fn(db, root)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except GitHubAppError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except KeyError:
        return JSONResponse({"error": "person not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _clone_parent(request: Request, requested: str) -> str:
    store = Path(request.app.state.ui.store_root)
    return str(durable_checkout_parent(store, requested))


def _root(request: Request) -> tuple[Path, str]:
    directory = request_directory(request)
    return directory.root, directory.database_url
