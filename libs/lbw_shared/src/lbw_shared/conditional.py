"""Conditional GET for JSON API responses.

Every JSON ``GET`` under ``/api/`` carries an ``ETag`` made from its body and
``Cache-Control: private, no-cache``. A browser keeps the response and sends
``If-None-Match`` on the next request. When the body has not changed, the
answer is ``304 Not Modified`` with no body, and the browser hands the page its
stored copy. A page that polls downloads data only when it moved.

Streams, files, errors, and responses that set ``no-store`` or their own
``ETag`` pass through untouched.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CACHE_CONTROL = "private, no-cache"
# The fields a 304 keeps from the 200 it stands for (RFC 9110, section 15.4.5).
_KEPT_ON_304 = {b"cache-control", b"etag", b"vary", b"expires", b"content-location", b"date"}


def body_etag(body: bytes) -> str:
    """A strong validator for exactly these bytes."""
    return '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def etag_matches(if_none_match: str, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names this ETag. GET compares weakly."""
    value = if_none_match.strip()
    if value == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(item.strip().removeprefix("W/") == bare for item in value.split(","))


class ConditionalGetMiddleware:
    """Answer an unchanged JSON GET under ``prefix`` with 304 Not Modified."""

    def __init__(self, app: ASGIApp, prefix: str = "/api/") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in ("GET", "HEAD")
            or not scope["path"].startswith(self.prefix)
        ):
            await self.app(scope, receive, send)
            return
        if_none_match = ""
        for name, value in scope.get("headers") or []:
            if name == b"if-none-match":
                if_none_match = value.decode("latin-1")
        start: Message | None = None
        chunks: list[bytes] = []
        passing = False

        async def wrapped(message: Message) -> None:
            nonlocal start, passing
            if passing:
                await send(message)
                return
            if message["type"] == "http.response.start":
                if _eligible(message):
                    start = message
                    return
                passing = True
                await send(message)
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            await _finish(send, start, b"".join(chunks), if_none_match)

        await self.app(scope, receive, wrapped)


def _eligible(start: Message) -> bool:
    if start["status"] != 200:
        return False
    headers = {name.lower(): value for name, value in start.get("headers") or []}
    content_type = headers.get(b"content-type", b"").split(b";")[0].strip()
    if content_type != b"application/json":
        return False
    if b"etag" in headers:
        return False
    return b"no-store" not in headers.get(b"cache-control", b"")


async def _finish(send: Send, start: Message, body: bytes, if_none_match: str) -> None:
    etag = body_etag(body)
    original = start.get("headers") or []
    headers = [(name, value) for name, value in original if name.lower() not in (b"vary",)]
    # The body depends on who is signed in, so a stored copy is per session.
    vary = [value for name, value in original if name.lower() == b"vary"]
    if b"cookie" not in b",".join(vary).lower():
        vary.append(b"Cookie")
    headers.append((b"vary", b", ".join(vary)))
    if not any(name.lower() == b"cache-control" for name, _value in headers):
        headers.append((b"cache-control", CACHE_CONTROL.encode()))
    headers.append((b"etag", etag.encode()))
    if if_none_match and etag_matches(if_none_match, etag):
        kept = [(name, value) for name, value in headers if name.lower() in _KEPT_ON_304]
        await send({"type": "http.response.start", "status": 304, "headers": kept})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return
    await send({**start, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
