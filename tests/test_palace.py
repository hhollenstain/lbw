from __future__ import annotations

import json
from pathlib import Path

from tests.test_improve import _broken_project

from agent_lbw.agent.loop import CodingAgent
from agent_lbw.config import Settings
from agent_lbw.llm.client import AssistantTurn, ScriptedClient
from agent_lbw.runtime.improve import run_cycle
from agent_lbw.runtime.palace import (
    MEMORY_MARK,
    MemoryHit,
    NullPalace,
    fit_prompt,
    memory_preface,
    message_chars,
    prompt_budget_chars,
    record_source,
    render_task_record,
    seed_workspace,
    wing_name,
)
from agent_lbw.runtime.tasks import Task, TaskManager, sibling_task_records
from agent_lbw.sandbox import Workspace
from agent_lbw.tools import build_tools


class RecordingPalace:
    def __init__(self) -> None:
        self.drawers: list[tuple[str, str, str]] = []

    def file(self, content: str, *, room: str, source: str) -> None:
        self.drawers = [
            item for item in self.drawers if not (item[0] == room and item[1] == source)
        ]
        self.drawers.append((room, source, content))

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        room: str | None = None,
    ) -> list[MemoryHit]:
        del query
        hits: list[MemoryHit] = []
        for item_room, source, content in reversed(self.drawers):
            if room and item_room != room:
                continue
            hits.append(MemoryHit(text=content, room=item_room, source=source))
            if len(hits) >= limit:
                break
        return hits


def test_wing_name_uses_the_workspace_directory() -> None:
    assert wing_name(Path("/tmp/agent-lbw")) == "agent-lbw"


def test_render_task_record_includes_goal_and_summary() -> None:
    text = render_task_record(
        {
            "id": "abc",
            "status": "success",
            "goal": "Add logout",
            "summary": "Token sessions now expire on logout.",
        }
    )
    assert "Add logout" in text
    assert "Token sessions now expire on logout." in text
    assert record_source({"id": "abc"}) == "task-abc"


def test_seed_workspace_files_history_and_sibling_tasks(tmp_path: Path) -> None:
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {
                    "created_at": "2026-10-04T00:00:00Z",
                    "goal": "Switch auth to token sessions",
                    "summary": "Login now issues a bearer token.",
                    "status": "success",
                }
            ]
        ),
        encoding="utf-8",
    )
    palace = RecordingPalace()
    filed = seed_workspace(
        tmp_path,
        palace,
        siblings=[
            {
                "id": "task-2",
                "goal": "Add a logout route",
                "summary": "Logout clears the session cookie.",
                "status": "success",
            }
        ],
    )
    assert filed == 2
    joined = "\n".join(item[2] for item in palace.drawers)
    assert "bearer token" in joined
    assert "session cookie" in joined
    assert seed_workspace(tmp_path, NullPalace()) == 0


def test_memory_preface_pulls_same_workspace_tasks() -> None:
    palace = RecordingPalace()
    palace.file(
        "Login now issues a bearer token.",
        room="tasks",
        source="task-earlier",
    )
    context = memory_preface(palace, "Add logout", "Project: demo")
    assert "bearer token" in context
    assert MEMORY_MARK in context
    assert memory_preface(NullPalace(), "Add logout", "Project: demo") == "Project: demo"


def test_agent_prompt_includes_workspace_task_memory(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    tools = build_tools(
        workspace,
        test_command=None,
        command_timeout_seconds=10,
        git_author_name=None,
        git_author_email=None,
    )
    palace = RecordingPalace()
    palace.file(
        "Login now issues a bearer token.",
        room="tasks",
        source="task-earlier",
    )
    llm = ScriptedClient([AssistantTurn(text="ok") for _ in range(4)])
    CodingAgent(llm, tools, max_iterations=6, palace=palace).run("Add logout")
    assert "bearer token" in llm.calls[0][1]["content"]


def test_fit_prompt_files_older_turns_and_keeps_tool_pairs() -> None:
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Goal: add logout"},
        {"role": "assistant", "content": "A" * 4000},
        {"role": "user", "content": "B" * 4000},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": "C" * 4000},
    ]
    palace = RecordingPalace()
    budget = prompt_budget_chars(2000)
    assert budget is not None
    assert sum(len(str(message.get("content") or "")) for message in messages) > budget
    fitted = fit_prompt(messages, palace, query="add logout", context_window=2000)
    assert any(room == "transcript" for room, _source, _text in palace.drawers)
    assert MEMORY_MARK in json.dumps(fitted)
    assert _tool_pairs_intact(fitted)
    assert fitted[0]["role"] == "system"
    assert "add logout" in fitted[1]["content"]
    assert fit_prompt(messages[:2], palace, query="add logout", context_window=2000) == messages[:2]


def test_fit_prompt_trims_without_memory() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Goal: add logout"},
        {"role": "assistant", "content": "A" * 8000},
        {"role": "user", "content": "B" * 8000},
    ]
    fitted = fit_prompt(messages, None, query="add logout", context_window=2000)
    assert message_chars(fitted) < message_chars(messages)
    assert fitted[0]["role"] == "system"
    assert "add logout" in fitted[1]["content"]


def test_palace_cache_is_outside_the_checkout(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.palace import palace_cache_path

    home = tmp_path / "home"
    monkeypatch.setattr(
        "agent_lbw.runtime.palace.Path.home",
        classmethod(lambda cls: home),
    )
    path = palace_cache_path(tmp_path / "repo", tenant_id="org_1", project_id="proj_1")
    assert path == home / ".cache" / "lbw" / "palace" / "org_1" / "proj_1"
    assert ".lbw" not in path.parts


def test_sibling_task_records_skip_the_current_task(settings: Settings, tmp_path: Path) -> None:
    manager = TaskManager(settings, max_concurrent=1)
    try:
        current = _task(tmp_path, "current", goal="Add logout")
        earlier = _task(
            tmp_path,
            "earlier",
            goal="Switch auth to token sessions",
            summary="Login now issues a bearer token.",
            status="success",
        )
        with manager._lock:
            manager._tasks[current.id] = current
            manager._tasks[earlier.id] = earlier
        records = sibling_task_records(manager, current)
    finally:
        manager.shutdown(wait=False)
    assert [record["id"] for record in records] == ["earlier"]
    assert "bearer token" in records[0]["summary"]


def test_cycle_recalls_history_from_the_same_workspace(
    tmp_path: Path,
    settings: Settings,
    monkeypatch,
) -> None:
    _broken_project(tmp_path)
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {
                    "created_at": "2026-10-04T00:00:00Z",
                    "goal": "Switch auth to token sessions",
                    "summary": "Login now issues a bearer token.",
                    "status": "success",
                }
            ]
        ),
        encoding="utf-8",
    )
    palace = RecordingPalace()
    monkeypatch.setattr(
        "agent_lbw.runtime.palace.open_palace",
        lambda root, **_kwargs: palace,
    )
    llm = ScriptedClient([AssistantTurn(text="I looked around and stopped.")])
    run_cycle(tmp_path, settings, llm, goal="Add logout")
    assert "bearer token" in llm.calls[0][1]["content"]
    filed = "\n".join(text for _room, _source, text in palace.drawers)
    assert "Add logout" in filed


def _task(
    root: Path,
    task_id: str,
    *,
    goal: str,
    summary: str | None = None,
    status: str = "running",
) -> Task:
    return Task(
        id=task_id,
        workspace=str(root),
        goal=goal,
        auto_commit=False,
        create_pr=False,
        model_name="scripted",
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="ollama",
        summary=summary,
        status=status,
    )


def _tool_pairs_intact(messages: list[dict]) -> bool:
    pending = False
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            pending = True
            continue
        if role == "tool":
            if not pending:
                return False
            continue
        if pending and role != "tool":
            pending = False
    return True
