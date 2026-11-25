"""A JSON GET under /api/ answers 304 when the body has not changed."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.testclient import TestClient
from lbw_crossing.app import create_app as create_crossing_app
from lbw_shared.conditional import ConditionalGetMiddleware, etag_matches

from agent_lbw.config import Settings
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _toy() -> tuple[TestClient, dict]:
    state = {"count": 0}
    app = FastAPI()
    app.add_middleware(ConditionalGetMiddleware)

    @app.get("/api/count")
    def count() -> dict:
        return {"count": state["count"]}

    @app.post("/api/count")
    def bump() -> dict:
        state["count"] += 1
        return {"count": state["count"]}

    @app.get("/api/missing")
    def missing() -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/secret")
    def secret() -> JSONResponse:
        return JSONResponse({"key": "x"}, headers={"Cache-Control": "no-store"})

    @app.get("/api/text")
    def text() -> PlainTextResponse:
        return PlainTextResponse("a,b\n1,2\n")

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(
            iter(["data: 1\n\n", "data: 2\n\n"]), media_type="text/event-stream"
        )

    @app.get("/page.json")
    def outside() -> dict:
        return {"ok": True}

    return TestClient(app), state


def test_an_unchanged_json_get_is_not_sent_again() -> None:
    client, _state = _toy()
    first = client.get("/api/count")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache"
    assert "Cookie" in first.headers["vary"]

    again = client.get("/api/count", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag

    weak = client.get("/api/count", headers={"If-None-Match": f'"other", W/{etag}'})
    assert weak.status_code == 304


def test_a_changed_body_is_sent_with_a_new_etag() -> None:
    client, _state = _toy()
    etag = client.get("/api/count").headers["etag"]
    bumped = client.post("/api/count", headers={"If-None-Match": etag})
    assert bumped.status_code == 200
    assert "etag" not in bumped.headers

    fresh = client.get("/api/count", headers={"If-None-Match": etag})
    assert fresh.status_code == 200
    assert fresh.json() == {"count": 1}
    assert fresh.headers["etag"] != etag


def test_errors_streams_files_and_no_store_pass_through() -> None:
    client, _state = _toy()
    for path in ("/api/missing", "/api/secret", "/api/text", "/page.json"):
        response = client.get(path, headers={"If-None-Match": "*"})
        assert response.status_code in (200, 404), path
        assert "etag" not in response.headers, path
    stream = client.get("/api/stream", headers={"If-None-Match": "*"})
    assert stream.status_code == 200
    assert stream.text == "data: 1\n\ndata: 2\n\n"
    assert "etag" not in stream.headers


def test_etag_matching_follows_if_none_match() -> None:
    assert etag_matches("*", '"a"')
    assert etag_matches('"b", "a"', '"a"')
    assert etag_matches('W/"a"', '"a"')
    assert not etag_matches('"b"', '"a"')


def test_both_sites_answer_polls_with_304(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings)
    app = create_app(manager, default_workspace=tmp_path)
    ui = app.state.ui
    try:
        for client in (
            TestClient(app),
            TestClient(create_crossing_app(Path(ui.store_root), ui.manager.settings.database_url)),
        ):
            first = client.get("/api/auth/status")
            assert first.status_code == 200
            again = client.get("/api/auth/status", headers={"If-None-Match": first.headers["etag"]})
            assert again.status_code == 304

        site = TestClient(app)
        tasks = site.get("/api/tasks")
        assert tasks.status_code == 200
        polled = site.get("/api/tasks", headers={"If-None-Match": tasks.headers["etag"]})
        assert polled.status_code == 304
    finally:
        manager.shutdown(wait=False)
