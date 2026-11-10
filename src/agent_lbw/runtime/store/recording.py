"""Write a task request or a schedule index when the running app already has one.

The file scheduler and the task queue stay where they are. This module records
the directory row those callers are missing. A finished job also seals its
plan, steps, outcome, and final diff. Logs stay in the run directory.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import Database, database, database_configured
from agent_lbw.runtime.store.crypto import open_secret
from agent_lbw.runtime.store.install import read_account
from agent_lbw.runtime.store.projects import ensure_project
from agent_lbw.runtime.store.schedules import delete_schedule_row, save_schedule
from agent_lbw.runtime.store.tasks import (
    fault_for,
    save_task_request,
    save_task_result,
    tenant_dek,
    touch_task,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.git import github_owner_repo

_DIFF_CAP = 500_000
_FILE_CAP = 80_000
_HUMAN_STAGES = frozenset({"triage", "approval", "ask", "reply"})
_DECISIONS = frozenset({"build", "hand_off", "approve", "send_back", "continue"})
_OPEN_STATUSES = frozenset({"queued", "running", "waiting"})
_STEP_KINDS = frozenset({"step", "test", "lint", "review", "pr", "ui"})
_STEP_DROP = frozenset(
    {"messages", "output", "response", "raw", "snapshot", "errors", "diff", "net_diff"}
)
_DIFF_START = re.compile(r"(?=^diff --git )", re.M)
log = logging.getLogger("lbw")


def record_task(root: Path, database_url: str, task) -> None:
    """Store a queued request before its run starts."""
    account = load_account(root, database_url=database_url)
    workspace = Path(task.workspace)
    requester = str(getattr(task, "requester_id", "") or "") or account["current_user_id"]
    name = next(
        (user["name"] for user in account["users"] if user["id"] == requester),
        "",
    )
    task.requester_id = requester
    task.requester_name = name
    with database(root, database_url) as db:
        project = _project(db, account, workspace, workspace.name or "project")
        save_task_request(
            db,
            root,
            task_id=task.id,
            tenant_id=account["org"]["id"],
            project_id=project["id"],
            requester_id=requester,
            goal=task.goal,
            status=task.status,
            branch=task.branch,
            pr_url=task.pr_url,
            run_ref=f".lbw/runs/{task.id}",
        )
        from agent_lbw.runtime.store.audit import record_audit

        who = name or "Someone"
        record_audit(
            db,
            tenant_id=account["org"]["id"],
            actor_id=requester,
            action="task.start",
            summary=f"{who} started a job",
            method="POST",
            path="/api/tasks",
            target_type="task",
            target_id=task.id,
        )


def accept_recorded_review(root: Path, database_url: str, task_id: str) -> None:
    """When a person approves, record them if an agent built the change."""
    load_account(root, database_url=database_url)
    with database(root, database_url) as db:
        row = db.fetchone(
            "SELECT agent_id, reviewer_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        if row is None or not row["agent_id"] or row["reviewer_id"]:
            return
        from agent_lbw.runtime.store.catalog import accept_review

        accept_review(db, task_id)


def sync_task(root: Path, database_url: str, task) -> None:
    """Update status, and seal the plan, steps, outcome, and final diff."""
    account = load_account(root, database_url=database_url)
    payload = _job_payload(task)
    with database(root, database_url) as db:
        touch_task(
            db,
            task_id=task.id,
            tenant_id=account["org"]["id"],
            status=task.status,
            branch=task.branch,
            pr_url=task.pr_url,
            run_ref=f".lbw/runs/{task.id}",
            fault=fault_for(
                status=task.status,
                reason=str(getattr(task, "reason", "") or ""),
                error=str(getattr(task, "error", "") or ""),
            ),
        )
    try:
        with database(root, database_url) as db:
            save_task_result(
                db,
                root,
                task_id=task.id,
                tenant_id=account["org"]["id"],
                payload=payload,
            )
    except (OSError, ValueError) as exc:
        log.warning("task %s result was not stored: %s", task.id, exc)


def runs_come_from_database(root: Path, database_url: str = "") -> bool:
    """An account stores runs in the task table. A file log is not the record."""
    if not database_configured(root, database_url):
        return False
    try:
        with database(root, database_url) as db:
            return read_account(db) is not None
    except (OSError, ValueError):
        return False


def recorded_history(root: Path, database_url: str, workspace: Path) -> list[dict]:
    """Jobs for this project. File history is not required."""
    rows = _project_tasks(root, database_url, workspace)
    items: list[dict] = []
    for row in rows:
        if not row["result_ciphertext"] and row["status"] in {"queued", "running"}:
            continue
        items.append(_history_item(root, database_url, row))
    return items


def recorded_job_stamp(root: Path, database_url: str, workspace: Path) -> tuple:
    """A short signature so a new result shows up without a run file."""
    rows = _project_tasks(root, database_url, workspace)
    return tuple(
        (
            str(row["id"]),
            str(row["status"]),
            len(str(row["result_ciphertext"] or "")),
        )
        for row in rows
    )


def index_change_schedule(root: Path, database_url: str, item: dict) -> None:
    """Index a schedule. Its instruction text stays in the schedule file."""
    account = load_account(root, database_url=database_url)
    workspace = Path(str(item.get("workspace") or root))
    cadence = item.get("cadence")
    text = cadence if isinstance(cadence, str) else json.dumps(cadence, sort_keys=True)
    kind = "scan" if item.get("kind") == "project" else "change"
    with database(root, database_url) as db:
        project = _project(db, account, workspace, str(item.get("name") or "schedule"))
        save_schedule(
            db,
            schedule_id=str(item["id"]),
            tenant_id=account["org"]["id"],
            project_id=project["id"],
            kind=kind,
            cadence=text,
            enabled=bool(item.get("enabled", True)),
            created_by=account["current_user_id"],
        )


def drop_schedule_index(root: Path, database_url: str, schedule_id: str) -> None:
    with database(root, database_url) as db:
        delete_schedule_row(db, schedule_id)


def _job_payload(task) -> dict:
    evidence = []
    for item in getattr(task, "evidence", None) or []:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "role": str(item.get("role") or ""),
                "title": str(item.get("title") or ""),
                "job": str(item.get("job") or ""),
                "verdict": str(item.get("verdict") or ""),
                "summary": str(item.get("summary") or "")[:2000],
            }
        )
    criteria = [str(item) for item in (getattr(task, "criteria", None) or []) if str(item).strip()]
    decision = str(getattr(task, "decision", "") or "")
    if decision not in _DECISIONS:
        decision = ""
    return {
        "plan": str(getattr(task, "plan", "") or ""),
        "summary": str(getattr(task, "summary", "") or ""),
        "reason": str(getattr(task, "reason", "") or ""),
        "tests_passed": getattr(task, "tests_passed", None),
        "committed": bool(getattr(task, "committed", False)),
        "published": bool(getattr(task, "published", False)),
        "stage": str(getattr(task, "stage", "") or ""),
        "decision": decision,
        "model_name": str(getattr(task, "model_name", "") or ""),
        "model_base_url": str(getattr(task, "model_base_url", "") or ""),
        "criteria": criteria[:40],
        "evidence": evidence[:20],
        "review_notes": str(getattr(task, "review_notes", "") or "")[:4000],
        "steps": _steps(getattr(task, "events", None) or []),
        "diff": _final_diff(task),
    }


def _steps(events: list) -> list[dict]:
    kept: list[dict] = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in _STEP_KINDS:
            continue
        item = {
            key: value
            for key, value in event.items()
            if key not in _STEP_DROP and value is not None
        }
        kept.append(item)
        if len(kept) >= 200:
            break
    return kept


def _final_diff(task) -> str:
    root = Path(str(getattr(task, "workspace", "") or ""))
    if not root.is_dir() or not (root / ".git").exists():
        return ""
    from agent_lbw.sandbox import Workspace
    from agent_lbw.tools.git import is_runtime_artifact, run_git

    workspace = Workspace(root)
    sha = str(getattr(task, "sha_before", "") or "").strip()
    args = ["diff", "--find-renames", sha] if sha else ["diff", "HEAD"]
    result = run_git(workspace, args)
    chunks: list[str] = []
    if result.returncode == 0 and (result.stdout or "").strip():
        chunks.append(result.stdout.strip())
    listed = run_git(workspace, ["ls-files", "--others", "--exclude-standard"])
    if listed.returncode == 0:
        for rel in (listed.stdout or "").splitlines():
            path = rel.strip()
            if not path or is_runtime_artifact(path):
                continue
            full = root / path
            if not full.is_file():
                continue
            try:
                body = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(body) > _FILE_CAP:
                body = body[:_FILE_CAP] + "\n... truncated"
            chunks.append(_untracked_diff(path, body))
    text = "\n".join(chunks)
    if len(text) > _DIFF_CAP:
        return text[:_DIFF_CAP] + "\n... truncated"
    return text


def _untracked_diff(path: str, body: str) -> str:
    lines = body.splitlines() or [""]
    hunk = "\n".join(f"+{line}" for line in lines)
    count = len(lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n"
        f"{hunk}\n"
    )


def file_events_from_diff(diff: str) -> list[dict]:
    """One file event per final diff, so history can open the change."""
    events: list[dict] = []
    for chunk in _DIFF_START.split(diff or ""):
        body = chunk.strip("\n")
        if not body.startswith("diff --git "):
            continue
        path = _diff_path(body)
        if not path:
            continue
        events.append(
            {
                "kind": "file",
                "path": path,
                "action": "changed",
                "diff": body,
                "net_diff": body,
                "net_action": "changed",
            }
        )
    return events


def _diff_path(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                return path
        if line.startswith("diff --git "):
            match = re.search(r" b/(.+)$", line)
            if match:
                found = match.group(1).strip()
                if found:
                    return found
    return ""


def _project_tasks(root: Path, database_url: str, workspace: Path) -> list[dict]:
    if not database_configured(root, database_url):
        return []
    keys = _path_keys(workspace)
    remote = _github_remote(workspace)
    try:
        with database(root, database_url) as db:
            account = read_account(db)
            if account is None:
                return []
            rows = db.fetchall(
                """
                SELECT tasks.id, tasks.status, tasks.branch, tasks.pr_url,
                       tasks.goal_ciphertext, tasks.result_ciphertext, tasks.created,
                       tasks.tenant_id, tasks.requester_id, projects.locator
                FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                WHERE projects.tenant_id = ?
                ORDER BY tasks.created DESC, tasks.id DESC
                """,
                (account["org"]["id"],),
            )
    except (OSError, ValueError):
        return []
    return [dict(row) for row in rows if _matches(str(row["locator"]), keys, remote)]


def open_jobs(root: Path, database_url: str) -> list[dict]:
    """Queued, running, and waiting jobs, so a restart can show or resume them."""
    records: list[dict] = []
    for row in _open_rows(root, database_url):
        record = _open_record(root, database_url, row)
        if record is not None:
            records.append(record)
    return records


def open_job(root: Path, database_url: str, task_id: str) -> dict | None:
    """One stored job, including a failed row whose history is still waiting."""
    row = _task_row(root, database_url, task_id)
    if row is None:
        return None
    return _open_record(root, database_url, row)


def _history_item(root: Path, database_url: str, row: dict) -> dict:
    goal, result = _goal_and_result(root, database_url, row)
    steps = result.get("steps") if isinstance(result.get("steps"), list) else []
    diff = str(result.get("diff") or "")
    created = int(row["created"] or 0)
    stage = str(result.get("stage") or "")
    decision = _decision(result.get("decision"))
    return {
        "id": row["id"],
        "workspace": row["locator"],
        "goal": goal,
        "status": _shown_status(str(row["status"] or ""), stage, decision),
        "summary": str(result.get("summary") or ""),
        "reason": str(result.get("reason") or ""),
        "plan": str(result.get("plan") or ""),
        "criteria": result.get("criteria") if isinstance(result.get("criteria"), list) else [],
        "evidence": result.get("evidence") if isinstance(result.get("evidence"), list) else [],
        "tests_passed": result.get("tests_passed"),
        "committed": bool(result.get("committed")),
        "published": bool(result.get("published")),
        "stage": str(result.get("stage") or ""),
        "branch": row["branch"],
        "pr_url": row["pr_url"],
        "created_at": datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": [*steps, *file_events_from_diff(diff)],
        **_people(root, database_url, row),
    }


def _open_rows(root: Path, database_url: str) -> list[dict]:
    if not database_configured(root, database_url):
        return []
    try:
        with database(root, database_url) as db:
            account = read_account(db)
            if account is None:
                return []
            rows = db.fetchall(
                """
                SELECT tasks.id, tasks.status, tasks.branch, tasks.pr_url,
                       tasks.goal_ciphertext, tasks.result_ciphertext, tasks.created,
                       tasks.tenant_id, tasks.requester_id, projects.locator
                FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                WHERE tasks.tenant_id = ?
                  AND tasks.status IN ('queued', 'running', 'waiting')
                ORDER BY tasks.created ASC, tasks.id ASC
                """,
                (account["org"]["id"],),
            )
    except (OSError, ValueError):
        return []
    return [dict(row) for row in rows]


def _task_row(root: Path, database_url: str, task_id: str) -> dict | None:
    if not database_configured(root, database_url) or not task_id:
        return None
    try:
        with database(root, database_url) as db:
            account = read_account(db)
            if account is None:
                return None
            row = db.fetchone(
                """
                SELECT tasks.id, tasks.status, tasks.branch, tasks.pr_url,
                       tasks.goal_ciphertext, tasks.result_ciphertext, tasks.created,
                       tasks.tenant_id, tasks.requester_id, projects.locator
                FROM tasks
                JOIN projects ON projects.id = tasks.project_id
                WHERE tasks.tenant_id = ? AND tasks.id = ?
                """,
                (account["org"]["id"], task_id),
            )
    except (OSError, ValueError):
        return None
    if row is None:
        return None
    return dict(row)


def _open_record(root: Path, database_url: str, row: dict) -> dict | None:
    goal, result = _goal_and_result(root, database_url, row)
    locator = str(row["locator"] or "")
    saved = _saved_entry(Path(locator), str(row["id"])) if locator else {}
    decision = _decision(result.get("decision"))
    stage = str(result.get("stage") or "")
    status = str(row["status"] or "")
    saved_stage = str(saved.get("stage") or "")
    if (
        not decision
        and str(saved.get("status") or "") == "waiting"
        and saved_stage in _HUMAN_STAGES
        and status in {"queued", "error", "failed"}
    ):
        status = "waiting"
        if stage not in _HUMAN_STAGES:
            stage = saved_stage
    elif not stage:
        stage = saved_stage
    status = _shown_status(status, stage, decision)
    if status not in _OPEN_STATUSES:
        return None
    criteria = result.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        saved_criteria = saved.get("criteria")
        criteria = saved_criteria if isinstance(saved_criteria, list) else []
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        saved_evidence = saved.get("evidence")
        evidence = saved_evidence if isinstance(saved_evidence, list) else []
    created = int(row["created"] or 0)
    created_at = (
        datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if created else ""
    )
    goal_text = goal or str(saved.get("goal") or "")
    branch = row["branch"] or saved.get("branch") or None
    pr_url = row["pr_url"] or saved.get("pr_url") or None
    tests_passed = result.get("tests_passed", saved.get("tests_passed"))
    return {
        "id": str(row["id"]),
        "workspace": locator,
        "goal": goal_text or None,
        "status": status,
        "stage": stage,
        "decision": decision,
        "plan": str(result.get("plan") or saved.get("plan") or ""),
        "summary": str(result.get("summary") or saved.get("summary") or "") or None,
        "reason": str(result.get("reason") or saved.get("reason") or "") or None,
        "criteria": [str(item) for item in criteria if isinstance(item, str) and item.strip()],
        "evidence": [item for item in evidence if isinstance(item, dict)],
        "branch": str(branch) if branch else None,
        "pr_url": str(pr_url) if pr_url else None,
        "tests_passed": tests_passed,
        "committed": bool(result.get("committed") or saved.get("committed")),
        "published": bool(result.get("published") or saved.get("published")),
        "created_at": created_at,
        "model_name": str(result.get("model_name") or ""),
        "model_base_url": str(result.get("model_base_url") or ""),
        "review_notes": str(result.get("review_notes") or ""),
        **_people(root, database_url, row),
    }


def _people(root: Path, database_url: str, row: dict) -> dict:
    requester = str(row.get("requester_id") or "")
    started_by = None
    gates: list[dict] = []
    if not database_configured(root, database_url):
        return {"started_by": started_by, "gates": gates}
    try:
        with database(root, database_url) as db:
            from agent_lbw.runtime.store.audit import gates_for_tasks, person_name

            name = person_name(db, requester)
            if requester or name:
                started_by = {"id": requester, "name": name}
            gates = gates_for_tasks(db, [str(row.get("id") or "")]).get(
                str(row.get("id") or ""), []
            )
    except (OSError, ValueError):
        return {"started_by": started_by, "gates": gates}
    return {"started_by": started_by, "gates": gates}


def _goal_and_result(root: Path, database_url: str, row: dict) -> tuple[str, dict]:
    goal = ""
    result: dict = {}
    try:
        with database(root, database_url) as db:
            key = tenant_dek(db, root, row["tenant_id"])
            if row["goal_ciphertext"]:
                try:
                    goal = open_secret(key, row["goal_ciphertext"])
                except ValueError:
                    goal = ""
            if row["result_ciphertext"]:
                loaded = json.loads(open_secret(key, row["result_ciphertext"]))
                if isinstance(loaded, dict):
                    result = loaded
    except (OSError, ValueError, json.JSONDecodeError):
        return goal, {}
    return goal, result


def _saved_entry(workspace: Path, task_id: str) -> dict:
    path = workspace / "history.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, list):
        return {}
    for entry in loaded:
        if isinstance(entry, dict) and str(entry.get("id") or "") == task_id:
            return entry
    return {}


def _decision(value: object) -> str:
    text = str(value or "")
    return text if text in _DECISIONS else ""


def _shown_status(status: str, stage: str, decision: str) -> str:
    """A queued row still at a human gate is waiting for that person."""
    if status == "queued" and stage in _HUMAN_STAGES and not decision:
        return "waiting"
    return status


def _path_keys(workspace: Path) -> set[str]:
    raw = Path(workspace)
    keys = {str(raw)}
    try:
        keys.add(str(raw.expanduser()))
    except OSError:
        pass
    try:
        keys.add(str(raw.expanduser().resolve()))
    except OSError:
        pass
    return {key for key in keys if key}


def _matches(locator: str, keys: set[str], remote: str = "") -> bool:
    if remote and locator.casefold() == remote.casefold():
        return True
    if locator in keys or locator.casefold() in {key.casefold() for key in keys}:
        return True
    path = Path(locator).expanduser()
    if str(path) in keys:
        return True
    try:
        if path.exists() and str(path.resolve()) in keys:
            return True
    except OSError:
        return False
    return False


def _github_remote(workspace: Path) -> str:
    if not (workspace / ".git").exists():
        return ""
    try:
        origin = github_owner_repo(Workspace(workspace))
    except (OSError, ValueError):
        return ""
    if origin is None:
        return ""
    return f"{origin[0]}/{origin[1]}"


def project_id_for_checkout(db: Database, tenant_id: str, workspace: Path) -> str | None:
    """The project row for this checkout or owner/repo value."""
    remote = _github_remote(workspace) if workspace.is_dir() else ""
    keys = _path_keys(workspace)
    rows = db.fetchall(
        "SELECT id, locator FROM projects WHERE tenant_id = ?",
        (tenant_id,),
    )
    for row in rows:
        if _matches(str(row["locator"]), keys, remote):
            return str(row["id"])
    return None


def project_for_workspace(db: Database, account: dict, workspace: Path, name: str) -> dict:
    """The project for this checkout. A GitHub repository is owner/repo."""
    tenant_id = account["org"]["id"]
    from agent_lbw.runtime.store.github_sync import collapse_repo_projects

    collapse_repo_projects(db, tenant_id)
    remote = _github_remote(workspace)
    keys = _path_keys(workspace)
    rows = db.fetchall(
        "SELECT id, name, kind, locator FROM projects WHERE tenant_id = ?",
        (tenant_id,),
    )
    for row in rows:
        locator = str(row["locator"])
        if not _matches(locator, keys, remote):
            continue
        if remote and (locator != remote or row["kind"] != "github"):
            clash = next(
                (
                    other
                    for other in rows
                    if str(other["locator"]).casefold() == remote.casefold()
                    and other["id"] != row["id"]
                ),
                None,
            )
            if clash is None:
                db.execute(
                    "UPDATE projects SET kind = 'github', locator = ? WHERE id = ?",
                    (remote, row["id"]),
                )
                locator = remote
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": "github" if remote else row["kind"],
            "locator": locator,
        }
    if remote:
        return ensure_project(
            db,
            tenant_id,
            name=remote.split("/", 1)[1],
            kind="github",
            locator=remote,
        )
    kind = "git" if (workspace / ".git").exists() else "folder"
    place = str(workspace.expanduser())
    try:
        place = str(workspace.expanduser().resolve())
    except OSError:
        pass
    return ensure_project(db, tenant_id, name=name, kind=kind, locator=place)


def _project(db: Database, account: dict, workspace: Path, name: str) -> dict:
    return project_for_workspace(db, account, workspace, name)
