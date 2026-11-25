"""The engineer dashboard process. It shares the directory and nothing else."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from lbw_shared.conditional import ConditionalGetMiddleware
from lbw_shared.directory import bind_directory
from lbw_shared.hosts import github_callback_url, is_crossing_host

import agent_lbw
from agent_lbw.auth_http import register_auth
from agent_lbw.github_http import register_github_app
from lbw_crossing.http import register_crossing

_PACKAGE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_PACKAGE / "templates"))
_STATIC = Path(agent_lbw.__file__).resolve().parent / "static"
_FAVICON = (_STATIC / "favicon.svg").read_text(encoding="utf-8")


def create_app(root: Path, database_url: str = "") -> FastAPI:
    app = FastAPI(title="lbw-crossing", docs_url=None, redoc_url=None)
    app.add_middleware(ConditionalGetMiddleware)
    bind_directory(app, root, database_url)
    register_auth(app)
    register_crossing(app)
    register_github_app(app)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _page(request)

    @app.get("/crossingguard", response_model=None)
    def legacy(request: Request) -> HTMLResponse | RedirectResponse:
        if is_crossing_host(request.headers.get("host", "")):
            return RedirectResponse("/", status_code=303)
        return _page(request)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(_FAVICON, media_type="image/svg+xml")

    return app


def _page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "crossing.html",
        {
            "github_callback": github_callback_url(
                request.headers.get("host", ""),
                scheme=request.url.scheme,
            )
        },
    )
