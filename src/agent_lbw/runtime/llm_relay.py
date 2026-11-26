"""Model calls relayed through a person's own runner to the model server they set.

LBW's loop calls a local model with the normal OpenAI client. Its HTTP
transport queues each request for the person acting on the job. The first of
that person's runners to poll takes it, sends it to the person's server, and
posts the answer back. Only the runner that took a call can answer it.
Requests and answers stay in memory. A runner is only ever handed calls for its
own owner: the queue is keyed by the person, and the runner's owner picks the
queue.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from agent_lbw.llm.client import OpenAICompatClient
from agent_lbw.progress import raise_if_stopped, stop_requested
from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.local_models import RELAY_BASE_URL, relay_target
from agent_lbw.runtime.store.runners import pick_relay_runner

MAX_BODY_BYTES = 16 * 1024 * 1024
CALL_SECONDS = 600.0
PICKUP_SECONDS = 30.0
# A runner confirms a call as soon as it has it. A call no runner confirms in
# this time goes back to the person's queue: the poll that took it may have
# belonged to a runner that died while it waited.
ACK_SECONDS = 5.0
# The only calls a runner relays. The runner checks the same list.
ROUTES = frozenset({("GET", "/v1/models"), ("POST", "/v1/chat/completions")})


class RelayError(RuntimeError):
    """The runner did not take or answer the call in time."""


@dataclass
class _Call:
    id: str
    identity_id: str
    request: dict
    # The runner that took the call. Only it can confirm or answer it.
    runner_id: str = ""
    taken_at: float = 0.0
    acked: bool = False
    answer: dict | None = None
    done: threading.Event = field(default_factory=threading.Event)


class LlmRelay:
    def __init__(self, *, ack_seconds: float = ACK_SECONDS) -> None:
        self._lock = threading.Condition()
        self._queues: dict[str, deque[_Call]] = {}
        self._calls: dict[str, _Call] = {}
        self.ack_seconds = ack_seconds

    def call(
        self,
        *,
        identity_id: str,
        method: str,
        path: str,
        body: str,
        base_url: str,
        pickup_seconds: float = PICKUP_SECONDS,
        call_seconds: float = CALL_SECONDS,
    ) -> dict:
        """Send one model call to this person's runners and wait for the answer."""
        if (method, path) not in ROUTES:
            raise RelayError(f"LBW does not relay {method} {path}")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise RelayError("the model call is larger than the relay accepts")
        item = _Call(
            id="llm_" + secrets.token_hex(12),
            identity_id=identity_id,
            request={"method": method, "path": path, "body": body, "base_url": base_url},
        )
        with self._lock:
            self._calls[item.id] = item
            self._queues.setdefault(identity_id, deque()).append(item)
            self._lock.notify_all()
        try:
            if not self._wait_acked(item, pickup_seconds):
                raise_if_stopped()
                raise RelayError(
                    "your runner did not pick up the model call: check that lbw-runner is running"
                )
            deadline = time.monotonic() + call_seconds
            # Wait in short steps, so a person who stops the task ends the wait.
            while not item.done.wait(min(1.0, max(deadline - time.monotonic(), 0.0))):
                raise_if_stopped()
                if time.monotonic() >= deadline:
                    raise RelayError("your model server did not answer in time")
            return dict(item.answer or {})
        finally:
            with self._lock:
                self._calls.pop(item.id, None)
                queue = self._queues.get(identity_id)
                if queue is not None and item in queue:
                    queue.remove(item)

    def next(self, runner_id: str, identity_id: str, timeout: float) -> dict | None:
        """The oldest call for this runner's owner, or None after the wait."""
        with self._lock:
            left = timeout
            while True:
                queue = self._queues.get(identity_id)
                if queue:
                    item = queue.popleft()
                    item.runner_id = runner_id
                    item.taken_at = time.monotonic()
                    self._lock.notify_all()
                    return {"call_id": item.id, **item.request}
                if left <= 0:
                    return None
                started = time.monotonic()
                self._lock.wait(left)
                left -= time.monotonic() - started

    def ack(self, call_id: str, runner_id: str) -> bool:
        """The runner that took this call confirms it has it."""
        with self._lock:
            item = self._calls.get(call_id)
            if item is None or not item.runner_id or item.runner_id != runner_id:
                return False
            item.acked = True
            self._lock.notify_all()
            return True

    def answer(self, call_id: str, runner_id: str, answer: dict) -> bool:
        with self._lock:
            item = self._calls.get(call_id)
            if item is None or not item.runner_id or item.runner_id != runner_id:
                return False
            item.acked = True
            item.answer = dict(answer)
            item.done.set()
            self._lock.notify_all()
            return True

    def _wait_acked(self, item: _Call, timeout: float) -> bool:
        """Wait for a runner to confirm the call, offering it again when one never does."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not item.acked:
                now = time.monotonic()
                if item.runner_id and now - item.taken_at >= self.ack_seconds:
                    item.runner_id = ""
                    self._queues.setdefault(item.identity_id, deque()).appendleft(item)
                    self._lock.notify_all()
                if now >= deadline or stop_requested():
                    return False
                wait = min(deadline - now, 1.0)
                if item.runner_id:
                    wait = min(wait, item.taken_at + self.ack_seconds - now)
                self._lock.wait(max(wait, 0.01))
            return True


class RelayTransport(httpx.BaseTransport):
    """An httpx transport that sends each request through one person's runners."""

    def __init__(self, relay: LlmRelay, *, identity_id: str, base_url: str):
        self.relay = relay
        self.identity_id = identity_id
        self.base_url = base_url

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        try:
            answer = self.relay.call(
                identity_id=self.identity_id,
                method=request.method,
                path=path,
                body=request.read().decode("utf-8"),
                base_url=self.base_url,
            )
        except RelayError as exc:
            # 424 is not retried: a runner that is not there will not appear in a second.
            return _error_response(request, 424, str(exc))
        if answer.get("error"):
            return _error_response(request, 424, str(answer["error"]))
        return httpx.Response(
            int(answer.get("status") or 502),
            headers={"content-type": "application/json"},
            content=str(answer.get("body") or "").encode("utf-8"),
            request=request,
        )


def _error_response(request: httpx.Request, status: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps({"error": {"message": message}}).encode("utf-8"),
        request=request,
    )


def relay_route(db: Database) -> dict:
    """The acting person's own server. One of their runners that relays must be online."""
    target = relay_target(db)
    pick_relay_runner(db, target["tenant_id"], target["identity_id"])
    return {
        "relay_identity_id": target["identity_id"],
        "relay_base_url": target["base_url"],
    }


def relay_client(
    relay: LlmRelay, credential: dict, context_window: int | None
) -> OpenAICompatClient:
    """LBW's usual OpenAI client, sending each call through the person's runner."""
    transport = RelayTransport(
        relay,
        identity_id=str(credential["relay_identity_id"]),
        base_url=str(credential["relay_base_url"]),
    )
    return OpenAICompatClient(
        model=str(credential["model_name"]),
        base_url=RELAY_BASE_URL,
        api_key="",
        context_window=context_window,
        http_client=httpx.Client(transport=transport, timeout=CALL_SECONDS + PICKUP_SECONDS),
    )
