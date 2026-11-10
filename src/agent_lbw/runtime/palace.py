"""Workspace memory backed by MemPalace.

The palace is a cache, outside the checkout, keyed by tenant and project.
Finished tasks are filed in the `tasks` room so a later run can recall them.
That room is rebuilt from the task rows. Turns that no longer fit in the
model window are filed in the `transcript` room for this run and are not
stored in the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agent_lbw.progress import record_event

log = logging.getLogger("lbw")

MEMORY_MARK = "Workspace memory:"
TASKS_ROOM = "tasks"
TRANSCRIPT_ROOM = "transcript"
_KEEP_TAIL_GROUPS = 2
_CHUNK_CHARS = 6_000


@dataclass(frozen=True)
class MemoryHit:
    text: str
    room: str
    source: str


class Palace(Protocol):
    def file(self, content: str, *, room: str, source: str) -> None:
        """Store verbatim text in a room."""

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        room: str | None = None,
    ) -> list[MemoryHit]:
        """Return verbatim hits for a query."""


class NullPalace:
    """No-op palace used when MemPalace is not installed or cannot be opened."""

    def file(self, content: str, *, room: str, source: str) -> None:
        del content, room, source

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        room: str | None = None,
    ) -> list[MemoryHit]:
        del query, limit, room
        return []


class MemPalaceStore:
    """Verbatim drawers in a cache, keyed so refiling replaces."""

    def __init__(self, root: Path, *, tenant_id: str = "", project_id: str = "") -> None:
        self.root = Path(root)
        self.path = palace_cache_path(root, tenant_id=tenant_id, project_id=project_id)
        self.wing = wing_name(project_id) if project_id else wing_name(self.root)
        self._broken = False

    def file(self, content: str, *, room: str, source: str) -> None:
        text = (content or "").strip()
        if not text or self._broken:
            return
        parts = _chunks(text, _CHUNK_CHARS)
        try:
            for index, part in enumerate(parts):
                part_source = source if len(parts) == 1 else f"{source}#{index}"
                self._upsert(part, room=room, source=part_source)
        except Exception as exc:
            self._broken = True
            log.warning("mempalace file failed: %s", exc)

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        room: str | None = None,
    ) -> list[MemoryHit]:
        if self._broken:
            return []
        text = " ".join((query or "").split())[:1000]
        if not text:
            return []
        try:
            from mempalace.searcher import search_memories

            found = search_memories(
                text,
                palace_path=str(self.path),
                wing=self.wing,
                room=room,
                n_results=max(1, limit),
            )
        except Exception as exc:
            log.warning("mempalace recall failed: %s", exc)
            return []
        if not isinstance(found, dict):
            return []
        hits: list[MemoryHit] = []
        for item in found.get("results") or []:
            if not isinstance(item, dict):
                continue
            body = str(item.get("text") or "").strip()
            if not body:
                continue
            source = str(item.get("source_path") or item.get("source_file") or "")
            hits.append(MemoryHit(text=body, room=str(item.get("room") or ""), source=source))
        return hits

    def _upsert(self, content: str, *, room: str, source: str) -> None:
        from mempalace.palace import get_collection

        self.path.mkdir(parents=True, exist_ok=True)
        collection = get_collection(str(self.path), create=True)
        drawer_id = _drawer_id(self.wing, room, source)
        filed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        collection.upsert(
            ids=[drawer_id],
            documents=[content],
            metadatas=[
                {
                    "wing": self.wing,
                    "room": room,
                    "source_file": source,
                    "chunk_index": 0,
                    "added_by": "lbw",
                    "filed_at": filed_at,
                }
            ],
        )


def palace_cache_path(root: Path, *, tenant_id: str = "", project_id: str = "") -> Path:
    """A cache directory. It is not under the checkout."""
    base = Path.home() / ".cache" / "lbw" / "palace"
    tenant = tenant_id.strip()
    project = project_id.strip()
    if tenant and project:
        return base / _cache_part(tenant) / _cache_part(project)
    digest = hashlib.sha256(str(Path(root).expanduser()).encode()).hexdigest()[:16]
    return base / "local" / digest


def open_palace(
    root: Path, *, tenant_id: str = "", project_id: str = ""
) -> MemPalaceStore | NullPalace:
    """Open the cache for this project, or a no-op when MemPalace cannot be used."""
    try:
        import mempalace  # noqa: F401
    except ImportError:
        log.info("mempalace is not installed; workspace memory is off")
        return NullPalace()
    try:
        return MemPalaceStore(root, tenant_id=tenant_id, project_id=project_id)
    except Exception as exc:
        log.warning("could not open mempalace at %s: %s", root, exc)
        return NullPalace()


def palace_enabled(palace: Palace | None) -> bool:
    return palace is not None and not isinstance(palace, NullPalace)


def wing_name(root: Path | str) -> str:
    """Stable MemPalace wing for every task in this project."""
    raw = re.sub(r"[^0-9A-Za-z .'-]+", " ", Path(root).name).strip(" .'-")
    raw = re.sub(r"\s+", " ", raw)
    if not raw or not raw[0].isalnum():
        raw = f"ws {raw}".strip()
    if not raw[-1].isalnum():
        raw = f"{raw} ws".strip()
    return raw[:128] or "workspace"


def render_task_record(record: dict) -> str:
    goal = str(record.get("goal") or "").strip()
    summary = str(record.get("summary") or "").strip()
    if not goal and not summary:
        return ""
    label = str(record.get("id") or record.get("created_at") or "task")
    status = str(record.get("status") or "").strip()
    lines = [f"Task {label}" + (f" ({status})" if status else "")]
    if goal:
        lines.append(f"Goal:\n{goal}")
    if summary:
        lines.append(f"Summary:\n{summary}")
    reason = str(record.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason: {reason}")
    pr_url = str(record.get("pr_url") or "").strip()
    if pr_url:
        lines.append(f"Pull request: {pr_url}")
    return "\n\n".join(lines)


def record_source(record: dict) -> str:
    task_id = str(record.get("id") or "").strip()
    if task_id:
        return f"task-{task_id}"
    created = re.sub(r"[^0-9A-Za-z_-]+", "-", str(record.get("created_at") or "undated"))[:32]
    basis = str(record.get("goal") or record.get("summary") or "")
    digest = hashlib.sha256(basis.encode()).hexdigest()[:12]
    return f"cycle-{created}-{digest}"


def load_history_records(root: Path) -> list[dict]:
    path = Path(root) / "history.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def remember_record(palace: Palace | None, record: dict) -> bool:
    if not palace_enabled(palace) or palace is None:
        return False
    text = render_task_record(record)
    if not text:
        return False
    palace.file(text, room=TASKS_ROOM, source=record_source(record))
    return True


def seed_workspace(
    root: Path,
    palace: Palace | None,
    *,
    siblings: list[dict] | None = None,
    include_files: bool = True,
) -> int:
    """File task rows, and local history when this install has no database."""
    if not palace_enabled(palace):
        return 0
    count = 0
    saved = load_history_records(root) if include_files else []
    for record in [*saved, *(siblings or [])]:
        if remember_record(palace, record):
            count += 1
    return count


def _cache_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip(".-")[:80] or "project"


def format_memory(hits: list[MemoryHit], *, limit_chars: int = 3500) -> str:
    if not hits:
        return ""
    chunks = [
        MEMORY_MARK,
        "Background from other tasks in this workspace. Do not switch to those "
        "tasks; use them only to stay consistent with earlier work.",
    ]
    used = sum(len(chunk) for chunk in chunks)
    seen: set[str] = set()
    for hit in hits:
        key = hit.text.strip()[:240]
        if not key or key in seen:
            continue
        seen.add(key)
        label = hit.source or hit.room or "memory"
        block = f"[{label}]\n{hit.text.strip()}"
        room_left = limit_chars - used - 2
        if room_left < 200:
            break
        if len(block) > room_left:
            block = block[: room_left - 20].rstrip() + "\n... truncated"
        chunks.append(block)
        used += len(block) + 2
        if used >= limit_chars:
            break
    if len(chunks) <= 2:
        return ""
    return "\n\n".join(chunks)


def memory_preface(palace: Palace | None, goal: str, context: str) -> str:
    """Prepend relevant task memory from this workspace to the agent brief."""
    if not palace_enabled(palace) or palace is None:
        return context
    hits = palace.recall(goal, limit=5, room=TASKS_ROOM)
    block = format_memory(hits)
    if not block:
        return context
    record_event(
        kind="step",
        message="Loaded memory from earlier tasks in this workspace.",
    )
    if context.strip():
        return context.rstrip() + "\n\n" + block
    return block


def prompt_budget_chars(context_window: int | None) -> int | None:
    """Use most of the window, leaving room for the completion and tool list."""
    if context_window is None or context_window < 512:
        return None
    if context_window >= 16_384:
        return int(context_window * 0.72) * 4
    # Small local windows also have to hold the tool schema.
    reserved = max(768, int(context_window * 0.38))
    usable = max(512, context_window - reserved)
    return usable * 4


def fit_prompt(
    messages: list[dict],
    palace: Palace | None,
    *,
    query: str,
    context_window: int | None,
) -> list[dict]:
    """Drop older turns when the prompt outgrows the window.

    When workspace memory is open, the dropped turns are filed and a short
    recall is put back. Without it, the prompt is still trimmed so a small
    local window is not sent a transcript it cannot hold.
    """
    budget = prompt_budget_chars(context_window)
    if budget is None or message_chars(messages) <= budget:
        return messages
    store: Palace = palace if palace is not None else NullPalace()
    try:
        return _fit_prompt(messages, store, query=query, budget=budget)
    except Exception as exc:
        log.warning("could not fit the prompt: %s", exc)
        return messages


def message_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        calls = message.get("tool_calls")
        if calls:
            total += len(json.dumps(calls))
    return total


def _fit_prompt(
    messages: list[dict],
    palace: Palace,
    *,
    query: str,
    budget: int,
) -> list[dict]:
    groups = _groups(messages)
    head, rest = _split_head(groups)
    rest = [group for group in rest if not _is_memory_message(group[0])]
    dropped: list[list[dict]] = []
    while (
        rest and len(rest) > _KEEP_TAIL_GROUPS and message_chars(_flat(head) + _flat(rest)) > budget
    ):
        dropped.append(rest.pop(0))
    for group in dropped:
        text = _render_group(group)
        if text.strip():
            palace.file(text, room=TRANSCRIPT_ROOM, source=_source_for(text))
    fitted = _flat(head) + _flat(rest)
    fitted = _clip_overflow(fitted, budget, palace)
    if not dropped and message_chars(fitted) == message_chars(messages):
        return messages
    recall = format_memory(
        palace.recall(query or "recent work", limit=4, room=TRANSCRIPT_ROOM),
        limit_chars=1800,
    )
    if not recall and dropped:
        excerpt = _render_group(dropped[-1])[-1200:]
        recall = f"{MEMORY_MARK}\n\nMost recent archived turn:\n{excerpt}"
    if recall:
        bridge = {"role": "user", "content": recall}
        head_messages = _flat(head)
        fitted = [*head_messages, bridge, *fitted[len(head_messages) :]]
    if message_chars(fitted) > budget:
        fitted = _clip_overflow(fitted, budget, palace)
    if palace_enabled(palace):
        note = "Filed earlier turns into workspace memory so the run can continue."
    else:
        note = "Dropped earlier turns so the prompt fits the context window."
    record_event(kind="step", message=note)
    return fitted


def _clip_overflow(messages: list[dict], budget: int, palace: Palace) -> list[dict]:
    fitted = [_copy_message(message) for message in messages]
    for _ in range(8):
        if message_chars(fitted) <= budget:
            return fitted
        index = _largest_index(fitted)
        if index is None:
            index = _largest_index(fitted, include_head=True)
        if index is None:
            return fitted
        content = fitted[index].get("content")
        if not isinstance(content, str) or len(content) < 200:
            return fitted
        palace.file(content, room=TRANSCRIPT_ROOM, source=_source_for(content))
        overflow = message_chars(fitted) - budget
        floor = 600 if index < 2 else 200
        keep = max(floor, len(content) - overflow - 80)
        if keep >= len(content):
            return fitted
        note = (
            "\n... remainder filed in workspace memory"
            if palace_enabled(palace)
            else "\n... truncated to fit the context window"
        )
        fitted[index]["content"] = content[:keep].rstrip() + note
    return fitted


def _groups(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            group = [message]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
            groups.append(group)
            continue
        groups.append([message])
        index += 1
    return groups


def _split_head(groups: list[list[dict]]) -> tuple[list[list[dict]], list[list[dict]]]:
    head: list[list[dict]] = []
    rest: list[list[dict]] = []
    seen_user = False
    for group in groups:
        first = group[0]
        if (
            not seen_user
            and first.get("role") in {"system", "user"}
            and not _is_memory_message(first)
        ):
            head.append(group)
            if first.get("role") == "user":
                seen_user = True
            continue
        rest.append(group)
    return head, rest


def _flat(groups: list[list[dict]]) -> list[dict]:
    messages: list[dict] = []
    for group in groups:
        messages.extend(group)
    return messages


def _largest_index(messages: list[dict], *, include_head: bool = False) -> int | None:
    best_index: int | None = None
    best_size = 0
    for index, message in enumerate(messages):
        if (not include_head and index < 2) or _is_memory_message(message):
            continue
        content = message.get("content")
        size = len(content) if isinstance(content, str) else 0
        if size > best_size:
            best_size = size
            best_index = index
    return best_index


def _is_memory_message(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith(MEMORY_MARK)


def _render_group(group: list[dict]) -> str:
    parts: list[str] = []
    for message in group:
        role = str(message.get("role") or "message")
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        calls = message.get("tool_calls")
        if calls:
            content = f"{content}\n{json.dumps(calls)}".strip()
        if content:
            parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def _copy_message(message: dict) -> dict:
    copied = dict(message)
    calls = copied.get("tool_calls")
    if isinstance(calls, list):
        copied["tool_calls"] = [dict(call) for call in calls]
    return copied


def _source_for(text: str) -> str:
    return "transcript-" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _drawer_id(wing: str, room: str, source: str) -> str:
    raw = f"{wing}\n{room}\n{source}"
    return "lbw-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[index : index + size] for index in range(0, len(text), size)]
