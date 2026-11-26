"""What waits on one person: jobs paused for them, and their jobs that run.

The runner's app shows this. A paused job is listed for a person who can
write to its project, because only they can continue it. A queued or running
job is listed for the person who started it or acts on it now, with Stop.
Each item carries the goal's first line, never the goal.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from agent_lbw.openapi_models import RunnerInbox, RunnerInboxItem
from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.audit import person_name
from agent_lbw.runtime.store.projects import can_access
from agent_lbw.runtime.store.recording import project_id_for_checkout

# What a person does next at each stage a job waits at, as the change list says it.
WAITING_FOR = {
    "spec": "Accept the spec",
    "triage": "Review the plan",
    "approval": "Approve the change",
    "ask": "Answer a question",
    "reply": "Say what to change",
}
TITLE_LIMIT = 200


def person_inbox(db: Database, tasks: list, *, tenant_id: str, identity_id: str) -> RunnerInbox:
    """The jobs waiting on this person and the ones they have running."""
    projects: dict[str, dict | None] = {}
    names: dict[str, str] = {}

    def project(task) -> dict | None:
        workspace = str(task.workspace or "")
        if workspace not in projects:
            project_id = project_id_for_checkout(db, tenant_id, Path(workspace))
            row = (
                db.fetchone("SELECT id, name, locator FROM projects WHERE id = ?", (project_id,))
                if project_id
                else None
            )
            projects[workspace] = dict(row) if row else None
        return projects[workspace]

    def name_of(identity: str) -> str:
        if identity and identity not in names:
            names[identity] = person_name(db, identity)
        return names.get(identity, "")

    inbox = RunnerInbox()
    for task in tasks:
        if getattr(task, "tenant_id", "") and task.tenant_id != tenant_id:
            continue
        mine = identity_id in {task.requester_id, task.actor_id}
        if task.status == "waiting" and task.stage in WAITING_FOR:
            found = project(task)
            if found is None or not can_access(db, identity_id, found["id"], need="write"):
                continue
            inbox.waiting.append(_item(task, found, name_of(task.requester_id)))
        elif task.status in {"queued", "running"} and mine:
            found = project(task)
            if found is None or not can_access(db, identity_id, found["id"], need="write"):
                continue
            inbox.running.append(_item(task, found, name_of(task.requester_id)))
    inbox.waiting.sort(key=lambda item: item.since)
    inbox.running.sort(key=lambda item: item.since)
    return inbox


def _item(task, project: dict, started_by: str) -> RunnerInboxItem:
    goal = str(task.goal or "")
    title = next((line.strip() for line in goal.splitlines() if line.strip()), "")
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT].rstrip() + "…"
    locator = str(project.get("locator") or project.get("name") or "")
    link = "/?" + urlencode({"section": "changes", "project": locator, "change": task.id})
    # A paused job has waited since its last run ended.
    waiting = task.status == "waiting"
    since = (task.finished_at if waiting else task.started_at) or task.created_at or ""
    return RunnerInboxItem(
        id=task.id,
        title=title or "Improve the project",
        project=locator,
        status=task.status,
        stage=task.stage or "",
        waiting_for=WAITING_FOR.get(task.stage, "") if waiting else "",
        started_by=started_by,
        model=task.model_name or "",
        on_runner=task.agent_kind == "claude_code",
        since=str(since),
        link=link,
    )
