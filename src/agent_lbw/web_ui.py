from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from lbw_shared.directory import bind_directory
from lbw_shared.hosts import crossing_url
from pydantic import BaseModel

from agent_lbw.auth_http import register_auth
from agent_lbw.config import Settings
from agent_lbw.llm.client import effective_context_window, normalize_model_base_url
from agent_lbw.openapi_models import AuditList, ProjectModelList, ProjectModelWrite, ScheduleWrite
from agent_lbw.progress import attach_token_usage, clip_text, llm_turn_snippets, public_run_item
from agent_lbw.runtime.account import (
    add_user,
    load_account,
    remove_user,
    update_account,
    update_person,
)
from agent_lbw.runtime.importer import load_goals_from_workspace
from agent_lbw.runtime.project import (
    default_guidelines,
    guidelines_are_custom,
    save_guidelines,
)
from agent_lbw.runtime.roles import roles_catalog, save_enabled_roles
from agent_lbw.runtime.schedules import (
    ScheduleClock,
    agenda,
    attach_task,
    delete_schedule,
    fail_run,
    file_task_goal,
    get_schedule,
    list_schedules,
    project_task_goal,
    resolve_output_file,
    start_run,
    sync_runs,
    upsert_schedule,
)
from agent_lbw.runtime.servers import (
    list_known_servers,
    load_selection,
    remember_server,
    update_server_alias,
)
from agent_lbw.runtime.skills import (
    get_skill,
    save_enabled_skills,
    skills_catalog,
    sync_skill_source,
)
from agent_lbw.runtime.tasks import Task, TaskManager
from agent_lbw.runtime.uireview import resolve_ui_screenshot
from agent_lbw.runtime.workspaces import (
    archive_workspace,
    browse_directory,
    clone_workspace,
    create_workspace,
    default_checkout_parent,
    durable_checkout_parent,
    forget_workspace,
    is_checkout_shelf,
    is_store_bookkeeping,
    last_workspace,
    load_archived_workspaces,
    load_workspaces,
    remember_workspace,
    repo_label,
)
from agent_lbw.tools.git import (
    extract_pr_url,
    list_branches,
    merge_pull_request,
    pull_request_state,
    valid_branch_name,
)
from agent_lbw.tools.kiwi import KiwiError

log = logging.getLogger("lbw")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
FAVICON_SVG = (STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")


_PR_STATE_TTL = 45.0
_TERMINAL_PR_STATES = frozenset({"closed", "merged"})
_PR_STATE_CACHE: dict[str, tuple[float, str]] = {}
_PR_STATE_LOCK = threading.Lock()


def _pr_states_path(root: Path) -> Path:
    return Path(root).expanduser().resolve() / ".lbw" / "pr-states.json"


def load_cached_pr_states(root: Path) -> None:
    """Remember closed and merged pull requests stored for this workspace."""
    path = _pr_states_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    now = time.monotonic()
    with _PR_STATE_LOCK:
        for url, state in raw.items():
            clean = extract_pr_url(str(url)) or ""
            if not clean or state not in _TERMINAL_PR_STATES:
                continue
            current = _PR_STATE_CACHE.get(clean)
            if current is None or current[1] not in _TERMINAL_PR_STATES:
                _PR_STATE_CACHE[clean] = (now, str(state))


def store_pr_states(root: Path, states: dict[str, str]) -> None:
    """Keep closed and merged states on disk. Drop a URL that is open again."""
    path = _pr_states_path(root)
    try:
        current: dict[str, str] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = {
                    str(url): str(state)
                    for url, state in loaded.items()
                    if state in _TERMINAL_PR_STATES
                }
    except (OSError, json.JSONDecodeError):
        current = {}
    changed = False
    for url, state in states.items():
        if state in _TERMINAL_PR_STATES:
            if current.get(url) != state:
                current[url] = state
                changed = True
        elif state == "open" and url in current:
            del current[url]
            changed = True
    if not changed:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def lookup_pull_state(url: str, *, force: bool = False, token: str | None = None) -> str:
    """Cached pull-request state. Closed and merged stay cached until a forced refresh."""
    clean = extract_pr_url(url or "") or ""
    if not clean:
        return ""
    now = time.monotonic()
    if not force:
        with _PR_STATE_LOCK:
            cached = _PR_STATE_CACHE.get(clean)
            if cached is not None:
                if cached[1] in _TERMINAL_PR_STATES:
                    return cached[1]
                if now - cached[0] < _PR_STATE_TTL:
                    return cached[1]
    state = pull_request_state(clean, token=token)
    if not state and force:
        with _PR_STATE_LOCK:
            previous = _PR_STATE_CACHE.get(clean)
        if previous is not None and previous[1] in _TERMINAL_PR_STATES:
            state = previous[1]
    with _PR_STATE_LOCK:
        _PR_STATE_CACHE[clean] = (time.monotonic(), state)
    return state


class TaskCreate(BaseModel):
    workspace: str | None = None
    goal: str | None = None
    model: str | None = None
    model_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auto_commit: bool | None = None
    create_pr: bool | None = None
    resume_branch: str | None = None
    resume_sha: str | None = None
    base_branch: str | None = None
    pr_url: str | None = None
    continuous: bool = False
    context_window: int | None = None
    gate: str | None = None
    triage_gate: str | None = None
    approval_gate: str | None = None


class ContinuousStop(BaseModel):
    workspace: str | None = None


class RerunPayload(BaseModel):
    sha: str | None = None
    goal: str | None = None


class DecidePayload(BaseModel):
    decision: str = ""
    note: str = ""


class ReworkPayload(BaseModel):
    note: str = ""


class WorkspaceRolesUpdate(BaseModel):
    path: str | None = None
    enabled: list[str] | None = None


class PullStatusQuery(BaseModel):
    urls: list[str] = []
    force: bool = False


class MergePull(BaseModel):
    url: str = ""
    workspace: str | None = None


class ModelsQuery(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class HistoryQuery(BaseModel):
    page: int = 1
    page_size: int = 10
    search: str | None = None


class ServerAliasUpdate(BaseModel):
    url: str
    alias: str | None = None


class UrlCreate(BaseModel):
    url: str
    alias: str | None = None


class WorkspaceSelect(BaseModel):
    path: str
    guidelines: str | None = None


class WorkspaceGuidelines(BaseModel):
    path: str | None = None
    guidelines: str | None = None


class WorkspaceSkillsUpdate(BaseModel):
    path: str | None = None
    enabled: list[str] | None = None


class WorkspaceSkillSource(BaseModel):
    path: str | None = None
    url: str
    ref: str | None = None
    team_id: str | None = None


class WorkspaceSkillSync(BaseModel):
    path: str | None = None
    slug: str
    ref: str | None = None


class AccountWrite(BaseModel):
    org_name: str | None = None
    user_name: str | None = None
    user_email: str | None = None


class AccountUserWrite(BaseModel):
    name: str
    email: str = ""


class AccountPersonWrite(BaseModel):
    email: str | None = None
    role: str | None = None


class WorkspaceClone(BaseModel):
    url: str
    parent: str | None = None
    name: str | None = None
    guidelines: str | None = None


class UiState:
    def __init__(
        self,
        manager: TaskManager,
        *,
        default_workspace: Path,
        default_goal: str | None,
        default_create_pr: bool | None,
    ) -> None:
        self.manager = manager
        self.store_root = str(default_workspace.expanduser().resolve())
        if manager.store_root is None:
            manager.store_root = Path(self.store_root)
        manager.adopt_open_jobs()
        self.default_workspace = self.store_root
        self.default_goal = default_goal or ""
        self.default_auto_commit = manager.settings.auto_commit
        self._history_lock = threading.Lock()
        self._history_cache: dict[str, tuple[object, list[dict[str, Any]]]] = {}
        self._checkouts_restored = False
        self.pending_workspace: str | None = None
        if default_create_pr is not None:
            self.default_create_pr = bool(default_create_pr)
        else:
            workspace = Path(self.default_workspace)
            self.default_create_pr = bool(
                manager.settings.create_pr or (workspace / ".git").exists()
            )

    def known_servers(self) -> list[dict[str, str]]:
        return list_known_servers(
            Path(self.default_workspace),
            default=self.manager.settings.model_base_url,
        )

    def selection(self) -> dict[str, str | list[dict[str, str]] | None]:
        return load_selection(
            Path(self.default_workspace),
            default_url=self.manager.settings.model_base_url,
            default_model=self.manager.settings.model_name,
        )

    def remember_current_workspace(self) -> list[dict[str, str | bool]]:
        return remember_workspace(Path(self.store_root), self.default_workspace)

    def shown_workspace(self) -> str:
        if self.pending_workspace and self._listed_project(self.pending_workspace):
            return self.pending_workspace
        projects = self.visible_workspaces()
        if any(item["path"] == self.default_workspace for item in projects):
            return self.default_workspace
        if projects:
            return str(projects[-1]["path"])
        return ""

    def _listed_project(self, path: str) -> bool:
        store = Path(self.store_root)
        if is_checkout_shelf(path, store) or is_store_bookkeeping(path, store):
            return False
        return Path(path).expanduser().is_dir()

    def visible_workspaces(self) -> list[dict[str, Any]]:
        """Project rows when a database is configured. Otherwise the local folders."""
        store = Path(self.store_root)
        url = self.manager.settings.database_url
        from agent_lbw.runtime.db import database_configured

        if database_configured(store, url):
            if self._listed_project(self.default_workspace):
                self._record_project(self.default_workspace)
            self.ensure_recorded_checkouts()
            from agent_lbw.runtime.store.github_sync import recorded_workspaces

            archived = {str(item["path"]) for item in load_archived_workspaces(store)}
            return [
                item
                for item in recorded_workspaces(store, url)
                if str(item["path"]) not in archived
            ]
        if self._listed_project(self.default_workspace):
            current = remember_workspace(store, self.default_workspace)
        else:
            current = load_workspaces(store)
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in current:
            path = str(item["path"])
            if path in seen or not self._listed_project(path):
                continue
            seen.add(path)
            merged.append(item)
        return merged

    def set_workspace(self, path: str) -> list[dict[str, str | bool]]:
        self.pending_workspace = None
        remembered = remember_workspace(Path(self.store_root), path)
        self.default_workspace = last_workspace(Path(self.store_root), default=path)
        self._record_project(path)
        return remembered

    def _record_project(self, path: str) -> None:
        store = Path(self.store_root)
        if is_checkout_shelf(path, store) or is_store_bookkeeping(path, store):
            return
        folder = Path(path).expanduser()
        if not folder.is_dir():
            return
        from agent_lbw.runtime.account import load_account
        from agent_lbw.runtime.db import database, database_configured
        from agent_lbw.runtime.store.recording import project_for_workspace

        url = self.manager.settings.database_url
        if not database_configured(store, url):
            return
        try:
            remember_workspace(store, folder)
        except ValueError:
            return
        account = load_account(store, database_url=url)
        with database(store, url) as db:
            project_for_workspace(db, account, folder, folder.name or "project")

    def _refresh_current_workspace(self) -> None:
        self.default_workspace = last_workspace(Path(self.store_root), default=self.store_root)

    def archive_workspace_path(self, path: str) -> list[dict[str, str | bool]]:
        remembered = archive_workspace(Path(self.store_root), path)
        self._refresh_current_workspace()
        return remembered

    def forget_workspace_path(self, path: str) -> list[dict[str, str | bool]]:
        remembered = forget_workspace(Path(self.store_root), path)
        self._drop_project(path)
        self._refresh_current_workspace()
        return remembered

    def _drop_project(self, path: str) -> None:
        """Remove a project row that has no jobs. The checkout folder stays."""
        store = Path(self.store_root)
        from agent_lbw.runtime.db import database, database_configured
        from agent_lbw.runtime.store.install import read_account
        from agent_lbw.runtime.store.recording import project_id_for_checkout

        url = self.manager.settings.database_url
        if not database_configured(store, url):
            return
        folder = Path(path).expanduser()
        with database(store, url) as db:
            account = read_account(db)
            if account is None:
                return
            project_id = project_id_for_checkout(db, account["org"]["id"], folder)
            if project_id is None:
                return
            jobs = db.fetchone("SELECT id FROM tasks WHERE project_id = ? LIMIT 1", (project_id,))
            if jobs is not None:
                return
            db.execute("DELETE FROM project_grants WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM rule_requirements WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM schedules WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def remember_server(self, url: str, model: str | None = None) -> list[dict[str, str]]:
        return remember_server(
            Path(self.default_workspace),
            url,
            model=model,
            default=self.manager.settings.model_base_url,
        )

    def selection_context_window(self) -> int | None:
        selected = self.selection()
        return effective_context_window(
            configured=self.manager.settings.context_window,
            base_url=selected["last_base_url"] or self.manager.settings.model_base_url,
            api_key=self.manager.settings.model_api_key,
            model=selected["last_model"] or self.manager.settings.model_name,
            timeout=0.5,
        )

    def ensure_recorded_checkouts(self) -> None:
        if self._checkouts_restored:
            return
        self._checkouts_restored = True
        from agent_lbw.runtime.store.github_sync import restore_checkouts

        try:
            restore_checkouts(Path(self.store_root), self.manager.settings.database_url)
        except Exception as exc:
            log.warning("could not restore recorded checkouts: %s", exc)

    def template_vars(self) -> dict[str, Any]:
        selected = self.selection()
        workspaces = self.visible_workspaces()
        shown = self.shown_workspace()
        current = next(
            (item for item in workspaces if item["path"] == shown),
            None,
        )
        return {
            "default_workspace": shown,
            "known_workspaces": workspaces,
            "workspace_is_git": bool((current or {}).get("is_git")),
            "workspace_git_remote": (current or {}).get("git_remote") or "",
            "workspace_repo_label": (
                repo_label(
                    str((current or {}).get("git_remote") or ""),
                    str((current or {}).get("name") or ""),
                )
                if current
                else "No projects yet"
            ),
            "guidelines": (current or {}).get("guidelines") or default_guidelines(),
            "custom_guidelines": bool((current or {}).get("custom_guidelines")),
            "default_guidelines": default_guidelines(),
            "default_goal": self.default_goal,
            "default_auto_commit": self.default_auto_commit,
            "default_create_pr": self.default_create_pr,
            "default_model": selected["last_model"] or self.manager.settings.model_name,
            "default_base_url": selected["last_base_url"] or self.manager.settings.model_base_url,
            "default_context_window": self.manager.settings.context_window or "",
            "known_servers": selected["servers"],
            "max_concurrent": self.manager.max_concurrent,
            "continuous_active": self.manager.is_continuous_active(self.default_workspace),
        }

    def meta(self) -> dict[str, Any]:
        selected = self.selection()
        return {
            "default_workspace": self.shown_workspace(),
            "known_workspaces": self.visible_workspaces(),
            "default_goal": self.default_goal,
            "default_auto_commit": self.default_auto_commit,
            "default_create_pr": self.default_create_pr,
            "default_model": selected["last_model"] or self.manager.settings.model_name,
            "default_base_url": selected["last_base_url"] or self.manager.settings.model_base_url,
            "known_servers": selected["servers"],
            "max_concurrent": self.manager.max_concurrent,
            "continuous_active": self.manager.is_continuous_active(self.default_workspace),
            **self.manager.counts(),
        }

    def load_history(self, workspace_root: Path) -> list[dict[str, Any]]:
        """Load cycle logs and stored jobs, newest first. Logs stay in the run files."""
        root = Path(workspace_root).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        key = str(root)
        store = Path(self.store_root)
        database_url = self.manager.settings.database_url
        fingerprint = _history_fingerprint(root, store_root=store, database_url=database_url)
        with self._history_lock:
            cached = self._history_cache.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            from agent_lbw.runtime.store.recording import recorded_history, runs_come_from_database

            if runs_come_from_database(store, database_url):
                merged = _collapse_history(recorded_history(store, database_url, root))
            else:
                merged = _collapse_history(self._read_history(root))
            items = [public_run_item(item) for item in merged]
            self._history_cache[key] = (fingerprint, items)
            return items

    def _read_history(self, workspace_root: Path) -> list[dict[str, Any]]:
        """Read run files and history.json without caching or slimming."""
        runs_dir = Path(workspace_root) / ".lbw" / "runs"
        results: list[dict[str, Any]] = []

        # Load from .lbw/runs/ directory (current runs)
        if runs_dir.exists():
            for path in sorted(runs_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                data.setdefault("id", path.stem)
                if not data.get("created_at"):
                    parsed = _created_at_from_run_id(path.stem)
                    if parsed:
                        data["created_at"] = parsed
                results.append(data)

        # Load from history.json (persisted history)
        history_file = Path(workspace_root) / "history.json"
        try:
            if history_file.exists():
                with open(history_file, encoding="utf-8") as f:
                    history = json.load(f)
                if isinstance(history, list):
                    # Add all historical items (the file has them newest first)
                    results.extend(history)
        except (OSError, json.JSONDecodeError):
            # If we can't read the history file, continue with current runs
            pass

        return results

    def load_goals_from_github(
        self,
        workspace: str | Path | None = None,
        state: str = "open",
    ) -> dict[str, Any]:
        """Load issues as selectable goals from the workspace's GitHub remote."""
        root = Path(workspace or self.default_workspace)
        return load_goals_from_workspace(root, state=state, token=self._installation_token())

    def _installation_token(self) -> str | None:
        from agent_lbw.runtime.store.github import GitHubAppError, current_token

        try:
            return current_token(Path(self.store_root), self.manager.settings.database_url)
        except GitHubAppError:
            return None

    def paginate_history(
        self,
        workspace_root: Path,
        *,
        page: int = 1,
        page_size: int = 10,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Load and paginate history with optional search filter."""
        all_items = self.load_history(workspace_root)

        # Apply search filter if provided
        if search:
            search_lower = search.lower()
            all_items = [
                item
                for item in all_items
                if any(
                    str(value).lower().find(search_lower) >= 0
                    for value in [
                        item.get("goal", ""),
                        item.get("summary", ""),
                        item.get("reason", ""),
                        item.get("status", ""),
                        item.get("id", ""),
                        item.get("pr_url", ""),
                        item.get("created_at", ""),
                        " ".join(
                            str(
                                event.get("path")
                                or event.get("url")
                                or event.get("message")
                                or event.get("output")
                                or event.get("command")
                                or ""
                            )
                            for event in item.get("events") or []
                            if isinstance(event, dict)
                        ),
                    ]
                )
            ]

        total = len(all_items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        items = [
            attach_token_usage(item, default_window=self.selection_context_window())
            for item in all_items[start_idx:end_idx]
        ]

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }


_CONSOLE_SNIPPET = 400


def _history_fingerprint(
    root: Path,
    *,
    store_root: Path | None = None,
    database_url: str = "",
) -> tuple:
    """Cheap signature so history polls can reuse a parsed cache."""
    runs_dir = root / ".lbw" / "runs"
    entries: list[tuple[str, int, int]] = []
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    history_file = root / "history.json"
    hist = (0, 0)
    try:
        if history_file.exists():
            stat = history_file.stat()
            hist = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass
    stamp: tuple = ()
    if store_root is not None:
        from agent_lbw.runtime.store.recording import recorded_job_stamp

        stamp = recorded_job_stamp(store_root, database_url, root)
    return (tuple(entries), hist, stamp)


def _has_file_diff(item: dict[str, Any]) -> bool:
    for event in item.get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "file":
            continue
        if event.get("net_diff") or event.get("diff"):
            return True
    return False


_RUN_STAMP = re.compile(r"^\d{8}T\d{6}Z$")


def _same_recorded_job(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """A run log and the stored job are one change when they share an id or a cycle."""
    left_id = str(left.get("id") or "")
    right_id = str(right.get("id") or "")
    if left_id and left_id == right_id:
        return True
    goal = str(left.get("goal") or "")
    if not goal or goal != str(right.get("goal") or ""):
        return False
    created = str(left.get("created_at") or "")
    if created and created == str(right.get("created_at") or ""):
        return True
    sha = str(left.get("commit_sha") or "")
    return bool(sha and sha == str(right.get("commit_sha") or ""))


def _prefer_history_id(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if _RUN_STAMP.match(current) and incoming and not _RUN_STAMP.match(incoming):
        return incoming
    return current


def _fold_history_item(current: dict[str, Any], extra: dict[str, Any]) -> None:
    current["id"] = _prefer_history_id(str(current.get("id") or ""), str(extra.get("id") or ""))
    if _has_file_diff(extra) and not _has_file_diff(current):
        events = list(current.get("events") or [])
        events.extend(
            event
            for event in extra.get("events") or []
            if isinstance(event, dict) and event.get("kind") == "file"
        )
        current["events"] = events
    for key in ("plan", "summary", "reason", "pr_url", "branch", "commit_sha", "workspace"):
        if not current.get(key) and extra.get(key):
            current[key] = extra[key]


def _collapse_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per job. A run log and history.json otherwise both describe it."""
    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        match = next((current for current in kept if _same_recorded_job(current, item)), None)
        if match is None:
            kept.append(item)
            continue
        _fold_history_item(match, item)
    return kept


def _merge_history(
    file_items: list[dict[str, Any]], stored: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep run files when they exist, and add stored jobs once the files are gone."""
    merged = [item for item in file_items if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in merged if item.get("id")}
    for item in stored:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "")
        current = by_id.get(key) if key else None
        if current is None:
            merged.append(item)
            if key:
                by_id[key] = item
            continue
        if _has_file_diff(current) or not _has_file_diff(item):
            continue
        events = list(current.get("events") or [])
        events.extend(
            event
            for event in item.get("events") or []
            if isinstance(event, dict) and event.get("kind") == "file"
        )
        current["events"] = events
        if not current.get("plan") and item.get("plan"):
            current["plan"] = item["plan"]
    return merged


def history_token(
    root: Path,
    *,
    store_root: Path | None = None,
    database_url: str = "",
) -> str:
    """Short signature of the run logs, history file, and stored jobs."""
    resolved = Path(root).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    payload = (
        f"{resolved}\n"
        f"{repr(_history_fingerprint(resolved, store_root=store_root, database_url=database_url))}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _live_task(manager: TaskManager) -> Task | None:
    """Newest running or queued task, else the newest task."""
    tasks = manager.list()
    running = [task for task in tasks if task.status in {"queued", "running"}]
    return running[0] if running else (tasks[0] if tasks else None)


def _console_snippet(text: object, limit: int = _CONSOLE_SNIPPET) -> str:
    return clip_text(text, limit)


def _agent_model_snippets(event: dict[str, Any]) -> tuple[str, str]:
    """Last non-assistant prompt and the model reply, clipped for the live console."""
    agent = str(event.get("agent") or "")
    model = str(event.get("model") or "")
    if agent or model:
        return agent, model or _console_snippet(event.get("response"))
    return llm_turn_snippets(event.get("messages"), event.get("response"))


def _console_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    agent, model = _agent_model_snippets(event)
    return {
        "kind": event.get("kind"),
        "at": event.get("at"),
        "purpose": event.get("purpose"),
        "elapsed_ms": event.get("elapsed_ms"),
        "ok": event.get("ok"),
        "path": event.get("path"),
        "action": event.get("action"),
        "net_diff": event.get("net_diff") or "",
        "diff": event.get("diff") or "",
        "command": event.get("command"),
        "phase": event.get("phase"),
        "message": event.get("message"),
        "url": event.get("url"),
        "agent": agent,
        "model": model,
    }


def _live_task_logs(manager: TaskManager) -> tuple[str, list[str]]:
    """Logs for the newest running task, or the newest task if none is running.

    Returns both the traditional log lines AND the detailed event-based logs
    for the in-depth back-and-forth between LLM and agent.
    """
    from agent_lbw.logging import format_elapsed

    task = _live_task(manager)
    if task is None:
        return "", []

    # Build enriched log output: interleaved timestamps from original logs
    # plus detailed event logs showing LLM reasoning, file changes, tests, and PRs
    all_lines = []
    seen_logs = set()

    # Process events in order to create rich console output
    for event in task.events:
        kind = event.get("kind", "")
        at = event.get("at", "")
        purpose = event.get("purpose", "")
        elapsed = event.get("elapsed_ms")
        ok = event.get("ok")
        path = event.get("path", "")
        action = event.get("action", "")
        diff = event.get("net_diff", "") or event.get("diff", "")
        command = event.get("command", "")
        test_ok = event.get("ok", ok)
        phase = event.get("phase", "")
        message = event.get("message", "")
        url = event.get("url", "")

        parts = [f"[{at}]"]

        if kind == "llm":
            status = "✓" if ok else "✗"
            messages = event.get("messages")
            response = event.get("response")
            if elapsed:
                time_str = format_elapsed(elapsed / 1000)
            else:
                time_str = ""

            # Show detailed LLM conversation when messages/response are available
            if messages and response:
                # LLM interaction details
                parts.append(f" {status} LLM ({purpose})")
                count = len(messages) if isinstance(messages, list) else "msg"
                parts.append(f"  ↓ {count} messages")
                if isinstance(messages, list) and len(messages) > 0:
                    for i, msg in enumerate(messages[:3]):
                        if isinstance(msg, dict):
                            role = str(msg.get("role", "unknown"))[:3]
                        else:
                            role = str(msg)[:20]
                        parts.append(f"  → [{i + 1}] {role}")
                    if len(messages) > 3:
                        all_lines.append(" ".join(parts))
                        parts = [f"[{at}]"]
                        parts.append(f"  · {len(messages) - 3} more messages")
                        all_lines.append(" ".join(parts))
                        parts = [f"[{at}] {status} LLM ({purpose})"]
                parts.append(f"  ↑ Response: {response[:100]}{'…' if len(response) > 100 else ''}")
                all_lines.append(" ".join(parts))
                parts = [f"[{at}]"]
            else:
                # Sparse legacy format - no detailed conversation data
                if time_str:
                    parts.append(f" {status} LLM ({purpose}) in {time_str}")
                else:
                    parts.append(f" {status} LLM ({purpose})")
                all_lines.append(" ".join(parts))

        elif kind == "file":
            if diff:
                # Show a trimmed repr of the diff
                preview = diff.replace("\n", " ").strip()[:80]
                if len(diff) > 80:
                    preview += "…"
                parts.append(f" {action} {path} [{preview}]")
            else:
                parts.append(f" {action} {path}")
            all_lines.append(" ".join(parts))

        elif kind == "test":
            status = "✓" if test_ok else "✗"
            if command:
                cmd_display = command[:50] + ("…" if len(command) > 50 else "")
                parts.append(f" {status} test ({cmd_display})")
            elif phase:
                parts.append(f" {status} {phase} tests")
            else:
                parts.append(f" {status} test run")
            all_lines.append(" ".join(parts))

        elif kind == "step":
            if message:
                parts.append(f" → {message}")
            else:
                parts.append(f" step at {at}")
            all_lines.append(" ".join(parts))

        elif kind == "pr":
            status = "✓" if ok else "✗"
            if url:
                parts.append(f" {status} PR: {url}")
            elif message:
                parts.append(f" {status} PR: {message}")
            else:
                parts.append(f" {status} PR created")
            all_lines.append(" ".join(parts))

        elif message:
            parts.append(f" {message}")
            all_lines.append(" ".join(parts))

    # Also include original logs for background details (deduplicated)
    for log_line in task.logs:
        stripped = log_line.strip()
        if stripped and log_line not in seen_logs:
            all_lines.append(log_line)
            seen_logs.add(log_line)

    return task.id, all_lines


def create_app(
    manager: TaskManager,
    *,
    default_workspace: Path,
    default_goal: str | None = None,
    default_create_pr: bool | None = None,
) -> FastAPI:
    app = FastAPI(title="lbw", docs_url=None, redoc_url=None)
    app.state.ui = UiState(
        manager,
        default_workspace=default_workspace,
        default_goal=default_goal,
        default_create_pr=default_create_pr,
    )
    _audit_middleware(app)
    register_auth(app)
    bind_directory(
        app,
        Path(app.state.ui.store_root),
        app.state.ui.manager.settings.database_url,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        ui: UiState = request.app.state.ui
        body = ui.template_vars()
        body["crossing_url"] = crossing_url(
            request.headers.get("host", ""),
            scheme=request.url.scheme,
        )
        return templates.TemplateResponse(request, "index.html", body)

    @app.get("/crossingguard", response_model=None)
    def crossing_guard(request: Request) -> JSONResponse | RedirectResponse:
        target = crossing_url(request.headers.get("host", ""), scheme=request.url.scheme)
        if target.startswith("/"):
            return JSONResponse(
                {"error": "the engineer dashboard is its own service"},
                status_code=404,
            )
        return RedirectResponse(target, status_code=303)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(FAVICON_SVG, media_type="image/svg+xml")

    @app.get("/api/meta")
    def meta(request: Request) -> dict[str, Any]:
        ui: UiState = request.app.state.ui
        return ui.meta()

    @app.get("/api/events/stream", response_model=None)
    async def events_stream(request: Request) -> StreamingResponse | JSONResponse:
        """Stream the running task's agent and LLM log for an LBW engineer."""
        from agent_lbw.runtime.db import database
        from agent_lbw.runtime.store.auth import current_actor

        ui: UiState = request.app.state.ui
        actor = current_actor()
        if not actor:
            return JSONResponse({"error": "sign in"}, status_code=401)
        with database(Path(ui.store_root), ui.manager.settings.database_url) as db:
            row = db.fetchone("SELECT crossing FROM identities WHERE id = ?", (actor,))
        if row is None or not int(row["crossing"] or 0):
            return JSONResponse(
                {"error": "crossing guard is for LBW engineers"},
                status_code=403,
            )
        manager = ui.manager

        async def event_stream():
            seen_id = ""
            seen_events = 0
            seen_logs = 0
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    task = _live_task(manager)
                    task_id = task.id if task else ""
                    if task_id != seen_id:
                        seen_id = task_id
                        seen_events = 0
                        seen_logs = 0
                    if task is not None:
                        events = list(task.events)
                        for event in events[seen_events:]:
                            yield f"data: {json.dumps(_console_event_payload(event))}\n\n"
                        seen_events = len(events)
                        logs = list(task.logs)
                        for line in logs[seen_logs:]:
                            yield f"data: {json.dumps({'line': line})}\n\n"
                        seen_logs = len(logs)
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def _models_payload(
        ui: UiState,
        base_url: str | None,
        api_key: str | None,
        *,
        remember: bool = False,
        model: str | None = None,
    ) -> Any:
        try:
            resolved = normalize_model_base_url(base_url or ui.manager.settings.model_base_url)
            names = ui.manager.list_models(base_url=resolved, api_key=api_key)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "models": []}, status_code=400)
        if remember:
            servers = ui.remember_server(resolved, model=model)
        else:
            servers = ui.known_servers()
        selected = ui.selection()
        chosen = (
            model
            or selected["last_model"]
            or (names[0] if names else ui.manager.settings.model_name)
        )
        return {
            "default": ui.manager.settings.model_name,
            "last_model": selected["last_model"],
            "base_url": resolved,
            "models": names,
            "servers": servers,
            "context_window": effective_context_window(
                configured=ui.manager.settings.context_window,
                base_url=resolved,
                api_key=api_key or ui.manager.settings.model_api_key,
                model=chosen,
                timeout=0.5,
            ),
        }

    @app.get("/api/models", response_model=None)
    def models(
        request: Request,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> Any:
        return _models_payload(request.app.state.ui, base_url, api_key)

    @app.post("/api/models", response_model=None)
    def models_post(
        request: Request,
        payload: ModelsQuery | None = None,
    ) -> Any:
        body = payload or ModelsQuery()
        return _models_payload(
            request.app.state.ui,
            body.base_url,
            body.api_key,
            remember=True,
            model=body.model,
        )

    @app.get("/api/servers")
    def servers(request: Request) -> dict[str, Any]:
        ui: UiState = request.app.state.ui
        selected = ui.selection()
        return {
            "default": ui.manager.settings.model_base_url,
            "servers": selected["servers"],
            "last_base_url": selected["last_base_url"],
            "last_model": selected["last_model"],
        }

    @app.get("/api/goals", response_model=None)
    def goals_from_issues(
        request: Request,
        workspace: str | None = None,
        state: str = "open",
    ) -> Any:
        """Get selectable goals from the current workspace's GitHub issues."""
        ui: UiState = request.app.state.ui
        result = ui.load_goals_from_github(workspace, state=state)
        if result.get("error"):
            status = 400 if result["error"] == "workspace is not a GitHub repository" else 502
            return JSONResponse(result, status_code=status)
        return result

    @app.get("/api/tasks", response_model=None)
    def list_tasks(
        request: Request,
        page: int | None = None,
        page_size: int | None = None,
        workspace: str | None = None,
    ) -> Any:
        ui: UiState = request.app.state.ui
        tasks = ui.manager.list()
        if workspace and workspace.strip():
            wanted = Path(workspace).expanduser().resolve()
            tasks = [
                task for task in tasks if Path(task.workspace).expanduser().resolve() == wanted
            ]
        if page is None and page_size is None:
            return [task.to_dict() for task in tasks]
        size = max(1, min(page_size or 10, 100))
        total = len(tasks)
        total_pages = max(1, math.ceil(total / size) if size else 1)
        current = max(1, min(page or 1, total_pages))
        start = (current - 1) * size
        return {
            "items": [task.to_dict() for task in tasks[start : start + size]],
            "page": current,
            "page_size": size,
            "total": total,
            "total_pages": total_pages,
        }

    @app.get("/api/tasks/{task_id}", response_model=None)
    def get_task(task_id: str, request: Request) -> Any:
        ui: UiState = request.app.state.ui
        task = ui.manager.get(task_id)
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        return task.to_dict()

    @app.post("/api/tasks/{task_id}/rerun")
    def rerun_task(task_id: str, request: Request, payload: RerunPayload | None = None) -> Any:
        ui: UiState = request.app.state.ui
        run_from_sha = payload.sha if payload else None
        new_goal = payload.goal if payload else None
        old_task = ui.manager.get(task_id)
        if old_task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        if old_task.status not in ("failed", "error", "success"):
            return JSONResponse({"error": "task not rerunnable"}, status_code=400)
        if (
            old_task.status == "success"
            and lookup_pull_state(old_task.pr_url or "", token=_ui_github_token(ui)) != "open"
        ):
            return JSONResponse({"error": "pull request is not open"}, status_code=409)
        goal_to_use = new_goal or old_task.goal
        new_task = ui.manager.rerun(
            task_id,
            from_sha=run_from_sha,
            goal=goal_to_use,
        )
        if new_task is None:
            return JSONResponse({"error": "task not found or not rerunnable"}, status_code=404)
        return JSONResponse(new_task.to_dict(), status_code=201)

    @app.post("/api/tasks/{task_id}/rework")
    def rework_task(task_id: str, request: Request, payload: ReworkPayload | None = None) -> Any:
        ui: UiState = request.app.state.ui
        note = (payload.note if payload else "") or ""
        if not " ".join(note.split()):
            return JSONResponse({"error": "write what should change"}, status_code=400)
        if ui.manager.get(task_id) is None:
            ui.manager.adopt_job(task_id)
        current = ui.manager.get(task_id)
        if current is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        if current.pr_url and lookup_pull_state(current.pr_url, token=_ui_github_token(ui)) in {
            "merged",
            "closed",
        }:
            return JSONResponse({"error": "pull request is not open"}, status_code=409)
        updated = ui.manager.rework(task_id, note)
        if updated is None:
            if ui.manager.get(task_id) is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            current = ui.manager.get(task_id)
            if current is not None and current.status in {"queued", "running"}:
                return JSONResponse({"error": "this job is already in progress"}, status_code=409)
            return JSONResponse({"error": "this job cannot take more changes"}, status_code=409)
        _remember_gate(ui, updated, "rework", note, path=f"/api/tasks/{task_id}/rework")
        request.state.audit_explicit = True
        return updated.to_dict()

    @app.post("/api/tasks/{task_id}/decide")
    def decide_task(task_id: str, request: Request, payload: DecidePayload | None = None) -> Any:
        ui: UiState = request.app.state.ui
        body = payload or DecidePayload()
        choice = (body.decision or "").strip()
        if choice not in {"build", "hand_off", "approve", "send_back", "continue"}:
            return JSONResponse({"error": "unknown decision"}, status_code=400)
        if choice in {"send_back", "continue"} and not (body.note or "").strip():
            return JSONResponse(
                {"error": "write what should change so the work can continue"},
                status_code=400,
            )
        try:
            updated = ui.manager.decide(task_id, choice, body.note)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        if updated is None:
            if ui.manager.get(task_id) is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            return JSONResponse(
                {"error": "this change is not waiting for that decision"},
                status_code=409,
            )
        _remember_gate(ui, updated, choice, body.note)
        request.state.audit_explicit = True
        return updated.to_dict()

    @app.get("/api/branches")
    def branches(request: Request, workspace: str | None = None) -> Any:
        ui: UiState = request.app.state.ui
        root = Path(workspace or ui.default_workspace).expanduser()
        if not root.is_dir():
            return JSONResponse({"error": "workspace is not a directory"}, status_code=400)
        if not (root / ".git").exists():
            return {"default": "", "branches": []}
        try:
            from agent_lbw.sandbox import Workspace

            default, names = list_branches(Workspace(root))
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"default": default, "branches": names}

    @app.post("/api/tasks")
    def create_task(
        request: Request,
        payload: TaskCreate | None = None,
    ) -> JSONResponse:
        ui: UiState = request.app.state.ui
        body = payload or TaskCreate()
        if body.pr_url and lookup_pull_state(body.pr_url, token=_ui_github_token(ui)) != "open":
            return JSONResponse({"error": "pull request is not open"}, status_code=409)
        picked = (body.base_branch or "").strip()
        if picked and not valid_branch_name(picked):
            return JSONResponse({"error": f"invalid branch name: {picked}"}, status_code=400)
        body.base_branch = picked or None
        if body.context_window is not None and not 512 <= body.context_window <= 1_048_576:
            return JSONResponse(
                {"error": "context_window must be between 512 and 1048576 tokens"},
                status_code=400,
            )
        workspace_raw = body.workspace or ui.shown_workspace()
        try:
            from agent_lbw.runtime.store.github_sync import materialize_checkout

            workspace_raw = materialize_checkout(
                Path(ui.store_root),
                ui.manager.settings.database_url,
                workspace_raw,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        model_name = body.model
        model_base_url = body.base_url
        model_api_key = body.api_key
        chosen_id = (body.model_id or "").strip()
        if chosen_id:
            from agent_lbw.runtime.db import database
            from agent_lbw.runtime.store.catalog import use_model

            root = Path(ui.store_root)
            try:
                load_account(root, database_url=ui.manager.settings.database_url)
                with database(root, ui.manager.settings.database_url) as db:
                    creds = use_model(db, root, Path(workspace_raw), chosen_id)
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except KeyError:
                return JSONResponse({"error": "not found"}, status_code=404)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            model_name = creds["model_name"]
            model_base_url = creds["base_url"]
            model_api_key = creds["api_key"]
        try:
            task = ui.manager.submit(
                Path(workspace_raw),
                body.goal,
                auto_commit=body.auto_commit,
                create_pr=body.create_pr,
                model_name=model_name,
                model_base_url=model_base_url,
                model_api_key=model_api_key,
                resume_branch=body.resume_branch,
                resume_sha=body.resume_sha,
                base_branch=body.base_branch,
                continuous=bool(body.continuous),
                context_window=body.context_window,
                gate=(body.gate or "auto"),
                triage_gate=body.triage_gate or "",
                approval_gate=body.approval_gate or "",
            )
            if not chosen_id:
                ui.remember_server(task.model_base_url, model=task.model_name)
            ui.set_workspace(task.workspace)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(task.to_dict(), status_code=201)

    @app.post("/api/continuous/stop")
    def stop_continuous(
        request: Request,
        payload: ContinuousStop | None = None,
    ) -> dict[str, object]:
        ui: UiState = request.app.state.ui
        body = payload or ContinuousStop()
        workspace = body.workspace or ui.default_workspace
        ui.manager.stop_continuous(workspace)
        return {
            "ok": True,
            "workspace": str(Path(workspace).expanduser().resolve()),
            "continuous_active": False,
        }

    @app.post("/api/pulls/status")
    def pull_status(request: Request, payload: PullStatusQuery | None = None) -> dict[str, Any]:
        ui: UiState = request.app.state.ui
        root = Path(ui.default_workspace)
        body = payload or PullStatusQuery()
        if not body.force:
            load_cached_pr_states(root)
        urls: list[str] = []
        for raw in body.urls[:80]:
            clean = extract_pr_url(raw or "") or ""
            if clean and clean not in urls:
                urls.append(clean)
        if not urls:
            return {"states": {}}

        token = _ui_github_token(ui)

        def one(url: str) -> str:
            return lookup_pull_state(url, force=body.force, token=token)

        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            states = dict(zip(urls, pool.map(one, urls), strict=True))
        store_pr_states(root, states)
        return {"states": states}

    @app.post("/api/pulls/merge")
    def merge_pull(request: Request, payload: MergePull | None = None) -> Any:
        ui: UiState = request.app.state.ui
        body = payload or MergePull()
        url = extract_pr_url(body.url or "") or ""
        if not url:
            return JSONResponse({"error": "that is not a pull request"}, status_code=400)
        root = Path(body.workspace or ui.shown_workspace()).expanduser()
        if not root.is_dir() or not (root / ".git").exists():
            return JSONResponse({"error": "that checkout is not a git repository"}, status_code=400)
        token = _ui_github_token(ui)
        if lookup_pull_state(url, force=True, token=token) != "open":
            return JSONResponse({"error": "pull request is not open"}, status_code=409)
        from agent_lbw.sandbox import Workspace

        merged = merge_pull_request(Workspace(root), url, token=token)
        if not merged.ok:
            return JSONResponse({"error": merged.output or "could not merge"}, status_code=400)
        with _PR_STATE_LOCK:
            _PR_STATE_CACHE[url] = (time.monotonic(), "merged")
        store_pr_states(Path(ui.store_root), {url: "merged"})
        return {"ok": True, "url": url, "state": "merged"}

    @app.get("/api/history")
    def get_history(
        request: Request,
        page: int = 1,
        page_size: int = 10,
        search: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        ui: UiState = request.app.state.ui
        root = Path(ui.shown_workspace() or ui.default_workspace)
        current = history_token(
            root,
            store_root=Path(ui.store_root),
            database_url=ui.manager.settings.database_url,
        )
        if token and token == current and not (search and search.strip()):
            return {
                "unchanged": True,
                "token": current,
                "items": [],
                "page": 1,
                "page_size": page_size,
                "total": 0,
                "total_pages": 1,
            }
        payload = ui.paginate_history(
            root,
            page=page,
            page_size=page_size,
            search=search,
        )
        payload["token"] = current
        payload["unchanged"] = False
        return payload

    @app.get("/api/ui-screenshot", response_model=None)
    def ui_screenshot(
        request: Request,
        name: str,
        workspace: str | None = None,
    ) -> Any:
        ui: UiState = request.app.state.ui
        root = Path(workspace or ui.default_workspace).expanduser()
        path = resolve_ui_screenshot(root, name)
        if path is None:
            return JSONResponse({"error": "screenshot not found"}, status_code=404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/servers/alias")
    def update_alias(
        request: Request,
        payload: ServerAliasUpdate | None = None,
    ) -> dict[str, Any]:
        """Update or add an alias for a server URL."""
        ui: UiState = request.app.state.ui
        body = payload or ServerAliasUpdate(url="", alias=None)
        if not body.url or not body.url.strip():
            return JSONResponse({"error": "url is required"}, status_code=400)
        servers = update_server_alias(
            Path(ui.default_workspace),
            body.url,
            body.alias,
        )
        return {
            "servers": servers,
            "last_base_url": ui.selection()["last_base_url"],
            "last_model": ui.selection()["last_model"],
        }

    @app.post("/api/servers")
    def create_server(
        request: Request,
        payload: UrlCreate | None = None,
    ) -> dict[str, Any]:
        """Create a new server with optional alias."""
        from agent_lbw.runtime.servers import create_or_update_server

        ui: UiState = request.app.state.ui
        body = payload or UrlCreate(url="", alias=None)
        if not body.url or not body.url.strip():
            return JSONResponse({"error": "url is required"}, status_code=400)

        servers = create_or_update_server(
            Path(ui.default_workspace),
            body.url,
            body.alias or None,
        )
        return {
            "servers": servers,
            "last_base_url": ui.selection()["last_base_url"],
            "last_model": ui.selection()["last_model"],
        }

    def _workspace_payload(ui: UiState) -> dict[str, Any]:
        workspaces = ui.visible_workspaces()
        shown = ui.shown_workspace()
        current = next(
            (item for item in workspaces if item["path"] == shown),
            None,
        )
        return {
            "current": shown,
            "home": str(Path.home()),
            "checkout_root": str(default_checkout_parent(Path(ui.store_root))),
            "default_guidelines": default_guidelines(),
            "guidelines": (current or {}).get("guidelines") or default_guidelines(),
            "custom_guidelines": bool((current or {}).get("custom_guidelines")),
            "workspaces": workspaces,
            "archived": load_archived_workspaces(Path(ui.store_root)),
        }

    def _seed_guidelines(path: str, text: str | None) -> None:
        if text is None:
            return
        root = Path(path)
        if guidelines_are_custom(root):
            return
        save_guidelines(root, text)

    def _account_store(ui: UiState) -> tuple[Path, str]:
        return Path(ui.store_root), ui.manager.settings.database_url

    @app.get("/api/account")
    def read_account(request: Request) -> dict[str, Any]:
        ui: UiState = request.app.state.ui
        root, url = _account_store(ui)
        return load_account(root, database_url=url)

    @app.put("/api/account")
    def write_account(request: Request, payload: AccountWrite) -> Any:
        ui: UiState = request.app.state.ui
        root, url = _account_store(ui)
        try:
            return update_account(
                root,
                org_name=payload.org_name,
                user_name=payload.user_name,
                user_email=payload.user_email,
                database_url=url,
            )
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/account/users")
    def create_account_user(request: Request, payload: AccountUserWrite) -> Any:
        ui: UiState = request.app.state.ui
        root, url = _account_store(ui)
        try:
            account = add_user(root, payload.name, email=payload.email, database_url=url)
            request.state.audit_action = "person.add"
            request.state.audit_summary = f"Added {payload.name.strip()}"
            request.state.audit_target_type = "person"
            return account
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.put("/api/account/users/{user_id}")
    def change_account_user(request: Request, user_id: str, payload: AccountPersonWrite) -> Any:
        ui: UiState = request.app.state.ui
        root, url = _account_store(ui)
        try:
            return update_person(
                root,
                user_id,
                email=payload.email,
                role=payload.role,
                database_url=url,
            )
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError:
            return JSONResponse({"error": "person not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete("/api/account/users/{user_id}")
    def delete_account_user(request: Request, user_id: str) -> Any:
        ui: UiState = request.app.state.ui
        root, url = _account_store(ui)
        try:
            return remove_user(root, user_id, database_url=url)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError:
            return JSONResponse({"error": "person not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/workspaces")
    def list_workspaces(request: Request) -> dict[str, Any]:
        return _workspace_payload(request.app.state.ui)

    @app.get("/api/workspaces/browse")
    def browse_workspaces(path: str | None = None) -> Any:
        try:
            return browse_directory(path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/workspaces/select")
    def select_workspace(request: Request, payload: WorkspaceSelect) -> Any:
        ui: UiState = request.app.state.ui
        from agent_lbw.runtime.store.github_sync import CheckoutMissing, materialize_checkout

        try:
            place = materialize_checkout(
                Path(ui.store_root),
                ui.manager.settings.database_url,
                payload.path,
            )
        except CheckoutMissing as exc:
            ui.pending_workspace = exc.path
            return _workspace_payload(ui)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            _seed_guidelines(place, payload.guidelines)
            ui.set_workspace(place)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _workspace_payload(ui)

    @app.post("/api/workspaces/create")
    def make_workspace(request: Request, payload: WorkspaceSelect) -> Any:
        ui: UiState = request.app.state.ui
        try:
            created = create_workspace(payload.path)
            _seed_guidelines(str(created["path"]), payload.guidelines)
            ui.set_workspace(str(created["path"]))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {**_workspace_payload(ui), "created": created}

    @app.post("/api/workspaces/clone")
    def clone_repo(request: Request, payload: WorkspaceClone) -> Any:
        ui: UiState = request.app.state.ui
        parent = durable_checkout_parent(Path(ui.store_root), payload.parent or "")
        try:
            cloned = clone_workspace(
                payload.url,
                parent,
                name=payload.name,
                git_config=_github_clone_config(ui, payload.url),
            )
            _seed_guidelines(str(cloned["path"]), payload.guidelines)
            ui.set_workspace(str(cloned["path"]))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {**_workspace_payload(ui), "created": cloned}

    @app.put("/api/workspaces/guidelines")
    def update_workspace_guidelines(
        request: Request, payload: WorkspaceGuidelines | None = None
    ) -> Any:
        ui: UiState = request.app.state.ui
        body = payload or WorkspaceGuidelines()
        path = body.path or ui.default_workspace
        try:
            save_guidelines(Path(path), body.guidelines)
            ui.set_workspace(path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _workspace_payload(ui)

    def _workspace_root(ui: UiState, path: str | None) -> Path:
        return Path(path or ui.default_workspace).expanduser().resolve()

    @app.get("/api/workspaces/models", response_model=ProjectModelList)
    def list_project_models(
        request: Request, path: str | None = None
    ) -> ProjectModelList | JSONResponse:
        ui: UiState = request.app.state.ui
        return _project_model_list(ui, path)

    @app.put("/api/workspaces/models", response_model=ProjectModelList)
    def update_project_model(
        request: Request, payload: ProjectModelWrite
    ) -> ProjectModelList | JSONResponse:
        ui: UiState = request.app.state.ui
        from agent_lbw.runtime.db import database, database_configured
        from agent_lbw.runtime.store.catalog import set_project_model

        root = Path(ui.store_root)
        url = ui.manager.settings.database_url
        workspace = _model_workspace(ui, payload.path)
        if not database_configured(root, url):
            return JSONResponse({"error": "no organization database"}, status_code=400)
        try:
            with database(root, url) as db:
                body = set_project_model(db, workspace, payload.model_id, enabled=payload.enabled)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError:
            return JSONResponse({"error": "unknown model"}, status_code=404)
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        state = "on" if payload.enabled else "off"
        request.state.audit_action = "model.project"
        request.state.audit_summary = f"Turned a project model {state}"
        request.state.audit_target_type = "model"
        request.state.audit_target_id = payload.model_id
        return ProjectModelList.model_validate(body)

    @app.get("/api/workspaces/roles")
    def list_workspace_roles(request: Request, path: str | None = None) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            return _roles_for_workspace(ui, root)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.put("/api/workspaces/roles")
    def update_workspace_roles(
        request: Request, payload: WorkspaceRolesUpdate | None = None
    ) -> Any:
        ui: UiState = request.app.state.ui
        body = payload or WorkspaceRolesUpdate()
        try:
            root = _workspace_root(ui, body.path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            enforced = _enforced_roles(ui)
            save_enabled_roles(root, [*(body.enabled or []), *enforced])
            return _roles_for_workspace(ui, root)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/workspaces/skills")
    def list_workspace_skills(request: Request, path: str | None = None) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            return _skills_for_workspace(ui, root)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/workspaces/skills/detail")
    def workspace_skill_detail(request: Request, name: str, path: str | None = None) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            skill = get_skill(root, name)
            if skill is None:
                skill = _scoped_skill(ui, root, name)
            if skill is None:
                return JSONResponse({"error": f"unknown skill: {name}"}, status_code=404)
            return skill.public_dict(include_body=True)
        except ValueError as extra:
            return JSONResponse({"error": str(extra)}, status_code=400)

    @app.put("/api/workspaces/skills")
    def update_workspace_skills(
        request: Request, payload: WorkspaceSkillsUpdate | None = None
    ) -> Any:
        ui: UiState = request.app.state.ui
        body = payload or WorkspaceSkillsUpdate()
        try:
            root = _workspace_root(ui, body.path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            extra = _scoped_skill_names(ui, root)
            save_enabled_skills(root, body.enabled or [], extra)
            return _skills_for_workspace(ui, root)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/workspaces/skills/sources")
    def add_workspace_skill_source(request: Request, payload: WorkspaceSkillSource) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, payload.path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            added = _mutate_skill_repo(
                ui,
                lambda db, account, kiwi, cache: _add_workspace_repo(
                    db, kiwi, account, root, cache, payload
                ),
            )
            return {**_skills_for_workspace(ui, root), "added": added}
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KiwiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.post("/api/workspaces/skills/sources/sync")
    def sync_workspace_skill_source(request: Request, payload: WorkspaceSkillSync) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, payload.path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            synced = _sync_workspace_repo(ui, root, payload.slug, payload.ref)
            return {**_skills_for_workspace(ui, root), "synced": synced}
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KiwiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.post("/api/workspaces/skills/sources/remove")
    def remove_workspace_skill_source(request: Request, payload: WorkspaceSkillSync) -> Any:
        ui: UiState = request.app.state.ui
        try:
            root = _workspace_root(ui, payload.path)
            if not root.is_dir():
                raise ValueError(f"workspace is not a directory: {root}")
            _mutate_skill_repo(
                ui,
                lambda db, account, kiwi, _cache: _remove_workspace_repo(
                    db, kiwi, account, root, payload.slug
                ),
            )
            return _skills_for_workspace(ui, root)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KiwiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.post("/api/workspaces/archive")
    def archive_workspace_tab(request: Request, payload: WorkspaceSelect) -> Any:
        return JSONResponse(
            {"error": "An owner or admin turns repositories on in Organization."},
            status_code=400,
        )

    @app.post("/api/workspaces/restore")
    def restore_workspace_tab(request: Request, payload: WorkspaceSelect) -> Any:
        ui: UiState = request.app.state.ui
        try:
            ui.set_workspace(payload.path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _workspace_payload(ui)

    @app.post("/api/workspaces/forget")
    def forget_workspace_tab(request: Request, payload: WorkspaceSelect) -> Any:
        return JSONResponse(
            {"error": "An owner or admin turns repositories on in Organization."},
            status_code=400,
        )

    @app.get("/api/schedules", response_model=None)
    def list_schedule_route(request: Request, workspace: str | None = None) -> Any:
        ui: UiState = request.app.state.ui
        root = Path(ui.store_root)
        sync_runs(root, _task_lookup(ui.manager))
        schedules = list_schedules(root)
        if workspace and workspace.strip():
            wanted = Path(workspace).expanduser().resolve()
            schedules = [
                item
                for item in schedules
                if Path(str(item.get("workspace") or "")).expanduser().resolve() == wanted
            ]
        _attach_schedule_live(schedules, ui.manager)
        return {"schedules": schedules, "agenda": agenda(schedules)}

    @app.post("/api/schedules", response_model=None)
    def create_schedule_route(request: Request, payload: ScheduleWrite) -> Any:
        ui: UiState = request.app.state.ui
        try:
            return upsert_schedule(
                Path(ui.store_root),
                payload.model_dump(),
                database_url=ui.manager.settings.database_url,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.patch("/api/schedules/{schedule_id}", response_model=None)
    def update_schedule_route(request: Request, schedule_id: str, payload: ScheduleWrite) -> Any:
        ui: UiState = request.app.state.ui
        try:
            return upsert_schedule(
                Path(ui.store_root),
                payload.model_dump(),
                schedule_id,
                database_url=ui.manager.settings.database_url,
            )
        except KeyError:
            return JSONResponse({"error": "Schedule not found."}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete("/api/schedules/{schedule_id}", response_model=None)
    def delete_schedule_route(request: Request, schedule_id: str) -> Any:
        ui: UiState = request.app.state.ui
        try:
            delete_schedule(
                Path(ui.store_root),
                schedule_id,
                database_url=ui.manager.settings.database_url,
            )
        except KeyError:
            return JSONResponse({"error": "Schedule not found."}, status_code=404)
        return {"ok": True}

    @app.get("/api/schedules/{schedule_id}/file", response_model=None)
    def schedule_file(request: Request, schedule_id: str, path: str = "") -> Any:
        ui: UiState = request.app.state.ui
        try:
            schedule = get_schedule(Path(ui.store_root), schedule_id)
        except KeyError:
            return JSONResponse({"error": "Schedule not found."}, status_code=404)
        target = resolve_output_file(schedule, path)
        if target is None:
            return JSONResponse({"error": "File not found."}, status_code=404)
        return FileResponse(target, filename=target.name)

    @app.post("/api/schedules/{schedule_id}/run", response_model=None)
    def run_schedule_route(request: Request, schedule_id: str) -> Any:
        ui: UiState = request.app.state.ui
        root = Path(ui.store_root)
        try:
            schedule = start_run(root, schedule_id, datetime.now().astimezone())
        except KeyError:
            return JSONResponse({"error": "Schedule not found."}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        _launch_scheduled_run(ui, schedule)
        sync_runs(root, _task_lookup(ui.manager))
        try:
            return get_schedule(root, schedule_id)
        except KeyError:
            return JSONResponse({"error": "Schedule not found."}, status_code=404)

    _bind_schedule_clock(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    from agent_lbw.directory_http import register_directory
    from agent_lbw.github_http import register_github
    from agent_lbw.org_http import register_org
    from agent_lbw.rules_http import register_rules

    register_directory(app)
    register_rules(app)
    register_github(app)
    register_org(app)
    _register_audit(app)
    return app


def _audit_middleware(app: FastAPI) -> None:
    """Run inside the session middleware so the signed-in person is still set."""

    @app.middleware("http")
    async def record_api_change(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (
            request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or not path.startswith("/api/")
            or response.status_code >= 400
            or getattr(request.state, "audit_explicit", False)
            or (request.method == "POST" and path == "/api/tasks")
        ):
            return response
        _write_request_audit(request)
        return response


def _register_audit(app: FastAPI) -> None:
    @app.get("/api/audit", response_model=AuditList)
    def read_audit(request: Request) -> AuditList | JSONResponse:
        ui: UiState = request.app.state.ui
        from agent_lbw.runtime.account import load_account
        from agent_lbw.runtime.db import database, database_configured
        from agent_lbw.runtime.store.audit import list_audit

        root = Path(ui.store_root)
        url = ui.manager.settings.database_url
        if not database_configured(root, url):
            return AuditList()
        try:
            account = load_account(root, database_url=url)
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        person = next(
            (user for user in account["users"] if user["id"] == account["current_user_id"]),
            None,
        )
        if person is None or person.get("role") not in {"owner", "admin"}:
            return JSONResponse({"error": "an owner or admin reads the audit log"}, status_code=403)
        with database(root, url) as db:
            return AuditList(entries=list_audit(db, account["org"]["id"]))


def _write_request_audit(request: Request) -> None:
    from agent_lbw.runtime.account import load_account
    from agent_lbw.runtime.db import database, database_configured
    from agent_lbw.runtime.store.audit import record_audit
    from agent_lbw.runtime.store.auth import current_actor, current_tenant

    ui: UiState | None = getattr(request.app.state, "ui", None)
    if ui is None:
        return
    root = Path(ui.store_root)
    url = ui.manager.settings.database_url
    if not database_configured(root, url):
        return
    try:
        account = load_account(root, database_url=url)
        tenant_id = current_tenant() or account["org"]["id"]
        actor = current_actor() or ""
        action = str(getattr(request.state, "audit_action", "") or "")
        summary = str(getattr(request.state, "audit_summary", "") or "")
        if not action:
            action = f"{request.method.lower()} {request.url.path}"
        if not summary:
            summary = f"{request.method} {request.url.path}"
        with database(root, url) as db:
            record_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor,
                action=action,
                summary=summary,
                method=request.method,
                path=request.url.path,
                target_type=str(getattr(request.state, "audit_target_type", "") or ""),
                target_id=str(getattr(request.state, "audit_target_id", "") or ""),
                detail=str(getattr(request.state, "audit_detail", "") or ""),
            )
    except (OSError, PermissionError, ValueError) as exc:
        log.warning("audit note was not stored: %s", exc)


def _project_model_list(ui: UiState, path: str | None) -> ProjectModelList | JSONResponse:
    from agent_lbw.runtime.db import database, database_configured
    from agent_lbw.runtime.store.catalog import project_models

    root = Path(ui.store_root)
    url = ui.manager.settings.database_url
    workspace = _model_workspace(ui, path)
    if not database_configured(root, url):
        return ProjectModelList(can_manage=False, models=[])
    try:
        with database(root, url) as db:
            return ProjectModelList.model_validate(project_models(db, workspace))
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _workspace_root_path(ui: UiState, path: str | None) -> Path:
    return Path(path or ui.default_workspace).expanduser().resolve()


def _model_workspace(ui: UiState, path: str | None) -> Path:
    """A checkout folder, or owner/repo when that repository has no checkout yet."""
    raw = Path((path or ui.default_workspace).strip()).expanduser()
    if len(raw.parts) == 2 and not raw.is_absolute() and not raw.is_dir():
        return raw
    try:
        return raw.resolve()
    except OSError:
        return raw


def _roles_for_workspace(ui: UiState, root: Path) -> dict:
    catalog = roles_catalog(root)
    enforced = set(_enforced_roles(ui))
    roles = catalog.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict) and role.get("name") in enforced:
                role["enabled"] = True
                role["enforced"] = True
            elif isinstance(role, dict):
                role["enforced"] = False
    enabled = [name for name in catalog.get("enabled") or [] if isinstance(name, str)]
    for name in sorted(enforced):
        if name not in enabled:
            enabled.append(name)
    catalog["enabled"] = enabled
    catalog["enforced"] = sorted(enforced)
    return catalog


def _skills_for_workspace(ui: UiState, root: Path) -> dict:
    from agent_lbw.org_http import assigned_skill_names

    catalog = skills_catalog(root)
    assigned = set(
        assigned_skill_names(Path(ui.store_root), root, ui.manager.settings.database_url)
    )
    skills = catalog.get("skills")
    if isinstance(skills, list):
        for skill in skills:
            if isinstance(skill, dict):
                skill["assigned"] = skill.get("name") in assigned
    catalog["assigned"] = sorted(assigned)
    repos, scoped, teams = _read_scoped(ui, root)
    catalog["skill_teams"] = teams
    if isinstance(skills, list) and scoped:
        by_name = {
            item.get("name"): item for item in skills if isinstance(item, dict) and item.get("name")
        }
        for skill in scoped:
            current = by_name.get(skill.name)
            if current is not None and current.get("origin") not in {"bundled", "source"}:
                continue
            payload = skill.public_dict()
            payload["assigned"] = skill.name in assigned
            by_name[skill.name] = payload
        catalog["skills"] = sorted(by_name.values(), key=lambda item: str(item.get("name")))
    sources = [item for item in catalog.get("sources") or [] if isinstance(item, dict)]
    known = {str(item.get("slug") or "") for item in sources}
    for repo in repos:
        if str(repo.get("slug") or "") in known:
            continue
        sources.append(repo)
    catalog["sources"] = sources
    return catalog


def _read_scoped(ui: UiState, root: Path) -> tuple[list[dict], list, list[dict]]:
    from agent_lbw.rules_http import open_kiwi
    from agent_lbw.runtime.account import load_account
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.install import require_account
    from agent_lbw.runtime.store.skill_repos import skills_on_workspace
    from agent_lbw.tools.kiwi import normalize_kiwifs_url

    url = normalize_kiwifs_url(ui.manager.settings.kiwifs_url)
    if not url:
        return [], [], []
    cache = Path(ui.store_root)
    try:
        load_account(cache, database_url=ui.manager.settings.database_url)
        kiwi = open_kiwi(url, ui.manager.settings.kiwifs_api_key)
        with database(cache, ui.manager.settings.database_url) as db:
            account = require_account(db)
            return skills_on_workspace(db, kiwi, account, root)
    except (KiwiError, OSError, PermissionError, ValueError):
        return [], [], []


def _scoped_skill(ui: UiState, root: Path, name: str):
    _repos, skills, _teams = _read_scoped(ui, root)
    return next((skill for skill in skills if skill.name == name), None)


def _scoped_skill_names(ui: UiState, root: Path) -> list[str]:
    _repos, skills, _teams = _read_scoped(ui, root)
    return [skill.name for skill in skills]


def _mutate_skill_repo(ui: UiState, fn):
    from agent_lbw.rules_http import open_kiwi
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.install import require_account
    from agent_lbw.tools.kiwi import normalize_kiwifs_url

    url = normalize_kiwifs_url(ui.manager.settings.kiwifs_url)
    if not url:
        raise ValueError("KiwiFS is not configured")
    cache = Path(ui.store_root)
    load_account(cache, database_url=ui.manager.settings.database_url)
    kiwi = open_kiwi(url, ui.manager.settings.kiwifs_api_key)
    with database(cache, ui.manager.settings.database_url) as db:
        account = require_account(db)
        return fn(db, account, kiwi, cache)


def _add_workspace_repo(
    db, kiwi, account, workspace: Path, cache: Path, payload: WorkspaceSkillSource
):
    from agent_lbw.runtime.store.skill_repos import add_workspace_repo

    return add_workspace_repo(
        db,
        kiwi,
        account,
        workspace,
        cache=cache,
        url=payload.url,
        team_id=payload.team_id or "",
        ref=payload.ref or "",
    )


def _remove_workspace_repo(db, kiwi, account, workspace: Path, slug: str) -> None:
    from agent_lbw.runtime.store.skill_repos import remove_workspace_repo

    remove_workspace_repo(db, kiwi, account, workspace, repo_id=(slug or "").strip())


def _sync_workspace_repo(ui: UiState, workspace: Path, slug: str, ref: str | None) -> dict:
    import re

    from agent_lbw.runtime.store.skill_repos import sync_repo

    wanted = (slug or "").strip()
    if re.fullmatch(r"src_[a-f0-9]{12}", wanted):
        return _mutate_skill_repo(
            ui,
            lambda db, account, kiwi, cache: sync_repo(
                db, kiwi, account, repo_id=wanted, cache=cache
            ),
        )
    return sync_skill_source(workspace, wanted, ref=ref).public_dict()


def _enforced_roles(ui: UiState) -> list[str]:
    from agent_lbw.org_http import enforced_roles_for

    return enforced_roles_for(Path(ui.store_root), ui.manager.settings.database_url)


def _ui_github_token(ui: UiState) -> str | None:
    from agent_lbw.runtime.store.github import GitHubAppError, current_token

    try:
        return current_token(Path(ui.store_root), ui.manager.settings.database_url)
    except (GitHubAppError, OSError, ValueError):
        return None


def _github_clone_config(ui: UiState, url: str) -> list[str] | None:
    """Use the installation token for a GitHub URL. A public clone still works without one."""
    from agent_lbw.tools.git import github_auth_args, parse_github_remote

    if parse_github_remote(url) is None:
        return None
    token = _ui_github_token(ui)
    if not token:
        return None
    return github_auth_args(token)


_LIVE_EVENT_KEYS = ("kind", "at", "message", "purpose", "name", "command", "phase", "path")


def _live_event(event: object) -> dict:
    if not isinstance(event, dict):
        return {}
    return {key: event[key] for key in _LIVE_EVENT_KEYS if event.get(key) not in (None, "")}


def _attach_schedule_live(schedules: list[dict], manager: TaskManager) -> None:
    """Add the in-memory log and the latest progress onto a run that is still open."""
    for item in schedules:
        for run in item.get("runs") or []:
            if run.get("status") not in {"queued", "running"}:
                continue
            task = manager.get(str(run.get("task_id") or ""))
            if task is None:
                run["logs"] = []
                run["events"] = []
                continue
            run["status"] = task.status
            run["logs"] = list(task.logs[-40:])
            run["events"] = [
                _live_event(event) for event in task.events[-8:] if isinstance(event, dict)
            ]


def _task_lookup(manager: TaskManager):
    def lookup(task_id: str) -> dict | None:
        task = manager.get(task_id)
        if task is None:
            return None
        return {
            "status": task.status,
            "summary": task.summary or "",
            "error": task.error or "",
            "finished_at": task.finished_at,
            "pr_url": task.pr_url or "",
            "reason": task.reason or "",
        }

    return lookup


def _settings_model(ui: UiState) -> tuple[str, str, str]:
    """The model from this install, used when the project has none granted."""
    selected = ui.selection()
    return (
        str(selected.get("last_model") or ui.manager.settings.model_name or ""),
        str(selected.get("last_base_url") or ui.manager.settings.model_base_url or ""),
        str(ui.manager.settings.model_api_key or ""),
    )


def _schedule_model(ui: UiState, workspace: Path, model_id: str) -> tuple[str, str, str] | str:
    """A granted model's name, host, and key, or a sentence when it cannot run."""
    from agent_lbw.runtime.db import database_configured

    root = Path(ui.store_root)
    url = ui.manager.settings.database_url
    if not database_configured(root, url):
        return _settings_model(ui)
    from agent_lbw.runtime.account import load_account
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.store.catalog import models_for_workspace, use_model

    chosen = model_id.strip()
    try:
        load_account(root, database_url=url)
        with database(root, url) as db:
            if not chosen:
                if models_for_workspace(db, workspace):
                    return "Choose a model turned on for this project."
                return _settings_model(ui)
            creds = use_model(db, root, workspace, chosen)
    except PermissionError:
        return "That model is off for this project."
    except KeyError:
        return "That model is off for this project."
    except (OSError, ValueError) as exc:
        return str(exc)
    host = str(creds.get("base_url") or "").strip()
    if not host:
        return "That model has no server."
    return (str(creds.get("model_name") or ""), host, str(creds.get("api_key") or ""))


def _remember_gate(ui: UiState, task, action: str, note: str, path: str = "") -> None:
    """Write who accepted a gate, or who sent the work back, onto the job."""
    from agent_lbw.runtime.account import load_account
    from agent_lbw.runtime.db import database, database_configured
    from agent_lbw.runtime.store.audit import note_gate
    from agent_lbw.runtime.store.auth import current_actor

    root = Path(ui.store_root)
    url = ui.manager.settings.database_url
    if not database_configured(root, url):
        return
    try:
        account = load_account(root, database_url=url)
        actor = current_actor() or account["current_user_id"]
        name = next((user["name"] for user in account["users"] if user["id"] == actor), "")
        with database(root, url) as db:
            gate = note_gate(
                db,
                tenant_id=account["org"]["id"],
                task_id=task.id,
                actor_id=actor,
                actor_name=name,
                action=action,
                note=note,
                path=path,
            )
        task.gates.append(gate)
    except (OSError, PermissionError, ValueError) as exc:
        log.warning("task %s gate was not recorded: %s", task.id, exc)


def _schedule_base_branch(schedule: dict, workspace: Path) -> str | None:
    if schedule.get("kind") == "files":
        return None
    picked = str(schedule.get("base_branch") or "").strip()
    if picked:
        return picked
    if (workspace / ".git").exists():
        from agent_lbw.sandbox import Workspace
        from agent_lbw.tools.git import default_base_branch

        return default_base_branch(Workspace(workspace)) or "main"
    return "main"


def _launch_scheduled_run(ui: UiState, schedule: dict) -> None:
    runs = schedule.get("runs") or []
    if not runs:
        return
    run_id = str(runs[0].get("id") or "")
    if not run_id:
        return
    root = Path(ui.store_root)
    workspace = Path(str(schedule.get("workspace") or ""))
    goal = str(schedule.get("goal") or "")
    auto_commit = bool(schedule.get("auto_commit"))
    create_pr = bool(schedule.get("create_pr"))
    base_branch = _schedule_base_branch(schedule, workspace)
    if schedule.get("kind") == "files":
        goal = file_task_goal(goal)
        auto_commit = False
        create_pr = False
    else:
        goal = project_task_goal(goal, base_branch or "main")
        auto_commit = True
        create_pr = True
    from agent_lbw.runtime.store.auth import current_actor

    requester = current_actor() or str(schedule.get("created_by") or "")
    granted = _schedule_model(ui, workspace, str(schedule.get("model_id") or ""))
    if isinstance(granted, str):
        fail_run(root, str(schedule.get("id") or ""), run_id, granted)
        return
    model_name, model_base_url, model_api_key = granted
    try:
        task = ui.manager.submit(
            workspace,
            goal,
            auto_commit=auto_commit,
            create_pr=create_pr,
            base_branch=base_branch,
            model_name=model_name,
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            requester_id=requester or None,
        )
    except Exception as exc:
        fail_run(root, str(schedule.get("id") or ""), run_id, str(exc))
        return
    attach_task(root, str(schedule.get("id") or ""), run_id, task.id)


def _bind_schedule_clock(app: FastAPI) -> None:
    ui: UiState = app.state.ui

    def on_due(schedule: dict) -> None:
        _launch_scheduled_run(ui, schedule)

    app.state.schedules = ScheduleClock(
        Path(ui.store_root),
        on_due,
        _task_lookup(ui.manager),
    )


def _created_at_from_run_id(stem: str) -> str | None:
    try:
        parsed = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def serve(
    workspace: Path,
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    max_concurrent: int = 1,
    default_goal: str | None = None,
    default_create_pr: bool | None = None,
) -> None:
    manager = TaskManager(settings, max_concurrent=max_concurrent)
    app = create_app(
        manager,
        default_workspace=workspace,
        default_goal=default_goal,
        default_create_pr=default_create_pr,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=1,
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.schedules.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.state.schedules.stop()
        manager.shutdown(wait=False)
