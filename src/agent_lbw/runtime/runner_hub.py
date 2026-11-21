"""In-memory handoff between a job waiting on a runner and that runner's HTTP calls.

The goal, the runner's events, and the patch never touch the database: the job
row holds only ids, status, and times. The worker thread that owns the task
opens a slot, the runner's requests fill it, and the worker drains it with the
task's progress bound so events land on the right task.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

MAX_EVENTS_PER_POST = 200
MAX_PATCH_BYTES = 5 * 1024 * 1024


@dataclass
class _Slot:
    payload: dict
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    cancelled: bool = False


class RunnerHub:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._slots: dict[str, _Slot] = {}

    def open(self, job_id: str, payload: dict) -> None:
        with self._lock:
            self._slots[job_id] = _Slot(payload=dict(payload))

    def close(self, job_id: str) -> None:
        with self._lock:
            self._slots.pop(job_id, None)
            self._lock.notify_all()

    def payload(self, job_id: str) -> dict | None:
        with self._lock:
            slot = self._slots.get(job_id)
            return dict(slot.payload) if slot else None

    def push(self, job_id: str, events: list[dict]) -> bool:
        """Add runner events. Returns True when the job was cancelled."""
        with self._lock:
            slot = self._slots.get(job_id)
            if slot is None:
                return True
            slot.events.extend(events[:MAX_EVENTS_PER_POST])
            self._lock.notify_all()
            return slot.cancelled

    def finish(self, job_id: str, result: dict) -> bool:
        with self._lock:
            slot = self._slots.get(job_id)
            if slot is None:
                return False
            slot.result = dict(result)
            self._lock.notify_all()
            return True

    def cancel(self, job_id: str) -> None:
        with self._lock:
            slot = self._slots.get(job_id)
            if slot is not None:
                slot.cancelled = True

    def wait(self, job_id: str, timeout: float) -> tuple[list[dict], dict | None]:
        """Wait for events or a result. Returns what arrived since the last call."""
        with self._lock:
            slot = self._slots.get(job_id)
            if slot is None:
                return [], {"status": "failed", "error": "the job was closed"}
            if not slot.events and slot.result is None:
                self._lock.wait(timeout)
            events, slot.events = slot.events, []
            return events, slot.result
