from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agent_lbw.config import Settings
from agent_lbw.llm.client import (
    LLMClient,
    OpenAICompatClient,
    list_remote_models,
    lookup_context_window,
    normalize_model_base_url,
)
from agent_lbw.logging import UtcFormatter, utcnow_iso
from agent_lbw.progress import (
    bind_progress,
    public_event,
    record_event,
    reset_progress,
    token_usage_from_events,
)
from agent_lbw.runtime.improve import CycleResult, resolve_human_gates, run_cycle

log = logging.getLogger("lbw")

PROTECTED_BRANCHES = frozenset({"", "main", "master", "trunk", "HEAD"})


Runner = Callable[["Task"], CycleResult]


def _task_fault(task: Task) -> str:
    from agent_lbw.runtime.store.tasks import fault_for

    return fault_for(
        status=task.status,
        reason=str(task.reason or ""),
        error=str(task.error or ""),
    )


def _workspace_is_github(path: str) -> bool:
    if not path:
        return False
    from agent_lbw.sandbox import SandboxError, Workspace
    from agent_lbw.tools.git import github_owner_repo

    try:
        workspace = Workspace(Path(path))
    except (OSError, SandboxError):
        return False
    if not (workspace.root / ".git").exists():
        return False
    return github_owner_repo(workspace) is not None


def _gate_value(value: str | None, default: str) -> str:
    picked = (value or "").strip()
    if not picked:
        return default
    if picked not in {"ask", "auto"}:
        raise ValueError("gate must be ask or auto")
    return picked


def _utcnow() -> str:
    return utcnow_iso()


def _join_notes(*parts: str) -> str:
    lines = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(lines)[:4000]


def _sent_back(evidence: list[dict]) -> bool:
    return any(
        isinstance(item, dict) and item.get("verdict") in {"send_back", "block"}
        for item in evidence
    )


def _review_notes(evidence: list[dict]) -> str:
    lines: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if item.get("verdict") not in {"send_back", "block"} and item.get("job") != "review":
            continue
        title = str(item.get("title") or item.get("role") or "reviewer")
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"{title}: {summary}")
    return "\n".join(lines)[:4000]


def sibling_task_records(manager: TaskManager, task: Task) -> list[dict]:
    """Goals and summaries from other tasks in the same workspace."""
    records: list[dict] = []
    for other in manager.list_for_workspace(task.workspace):
        if other.id == task.id:
            continue
        if not (other.goal or other.summary):
            continue
        records.append(
            {
                "id": other.id,
                "goal": other.goal,
                "summary": other.summary,
                "status": other.status,
                "reason": other.reason,
                "created_at": other.created_at,
                "pr_url": other.pr_url,
            }
        )
    return records


def _default_llm(settings: Settings) -> LLMClient:
    return OpenAICompatClient(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        context_window=settings.context_window,
    )


def _pinned_window(requested: int | None, configured: int | None) -> int | None:
    for value in (requested, configured):
        if isinstance(value, int) and value >= 512:
            return value
    return None


@dataclass
class Task:
    id: str
    workspace: str
    goal: str | None
    auto_commit: bool
    create_pr: bool | None
    model_name: str
    model_base_url: str
    model_api_key: str
    status: str = "queued"
    created_at: str = field(default_factory=_utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    summary: str | None = None
    reason: str | None = None
    tests_passed: bool | None = None
    committed: bool = False
    published: bool = False
    commit_sha: str | None = None
    pr_url: str | None = None
    error: str | None = None
    sha_before: str | None = None
    branch: str | None = None
    resume_branch: str | None = None
    resume_sha: str | None = None
    base_branch: str | None = None
    context_window: int | None = None
    pin_context_window: bool = False
    continuous: bool = False
    merged: bool = False
    gate: str = "auto"
    triage_gate: str = "auto"
    approval_gate: str = "auto"
    decision: str | None = None
    stage: str = ""
    plan: str = ""
    criteria: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    review_notes: str = ""
    committer_name: str = ""
    committer_email: str = ""
    requester_id: str = ""
    requester_name: str = ""
    gates: list[dict] = field(default_factory=list)

    def to_dict(self, *, include_logs: bool = True) -> dict:
        payload = {
            "id": self.id,
            "workspace": self.workspace,
            "goal": self.goal,
            "model": self.model_name,
            "base_url": self.model_base_url,
            "auto_commit": self.auto_commit,
            "create_pr": self.create_pr,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "reason": self.reason,
            "tests_passed": self.tests_passed,
            "committed": self.committed,
            "published": self.published,
            "commit_sha": self.commit_sha,
            "pr_url": self.pr_url,
            "error": self.error,
            "fault": _task_fault(self),
            "sha_before": self.sha_before,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "events": [public_event(event) for event in self.events],
            **token_usage_from_events(self.events),
            "context_window": self.context_window,
            "continuous": self.continuous,
            "merged": self.merged,
            "gate": self.gate,
            "triage_gate": self.triage_gate,
            "approval_gate": self.approval_gate,
            "stage": self.stage,
            "plan": self.plan,
            "criteria": list(self.criteria),
            "evidence": list(self.evidence),
            "review_notes": self.review_notes,
            "started_by": (
                {"id": self.requester_id, "name": self.requester_name}
                if self.requester_id or self.requester_name
                else None
            ),
            "gates": [dict(gate) for gate in self.gates if isinstance(gate, dict)],
        }
        if include_logs:
            payload["logs"] = list(self.logs)
        return payload


class _TaskLogHandler(logging.Handler):
    """Capture `lbw` log lines from the worker thread that owns a task."""

    def __init__(self, task: Task, thread_id: int) -> None:
        super().__init__(level=logging.INFO)
        self._task = task
        self._thread_id = thread_id
        self.setFormatter(UtcFormatter("%(asctime)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if threading.get_ident() != self._thread_id:
            return
        try:
            self._task.logs.append(self.format(record))
        except Exception:
            self.handleError(record)


class TaskManager:
    """Queue coding-agent cycles and run a bounded number of them at once."""

    def __init__(
        self,
        settings: Settings,
        *,
        max_concurrent: int = 1,
        runner: Runner | None = None,
        llm_factory: Callable[[Settings], LLMClient] | None = None,
        models_fn: Callable[..., list[str]] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.settings = settings
        self.max_concurrent = max_concurrent
        self._runner = runner
        self._llm_factory = llm_factory or _default_llm
        self._models_fn = models_fn
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._continuous_stopped: set[str] = set()
        self.store_root: Path | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent,
            thread_name_prefix="lbw-task",
        )

    def submit(
        self,
        workspace: Path,
        goal: str | None,
        *,
        auto_commit: bool | None = None,
        create_pr: bool | None = None,
        model_name: str | None = None,
        model_base_url: str | None = None,
        model_api_key: str | None = None,
        resume_branch: str | None = None,
        resume_sha: str | None = None,
        base_branch: str | None = None,
        continuous: bool = False,
        context_window: int | None = None,
        gate: str = "auto",
        triage_gate: str = "",
        approval_gate: str = "",
        requester_id: str | None = None,
    ) -> Task:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        selected_model = (model_name or "").strip() or self.settings.model_name
        selected_url = normalize_model_base_url(
            (model_base_url or "").strip() or self.settings.model_base_url
        )
        selected_key = model_api_key if model_api_key is not None else self.settings.model_api_key
        pinned = _pinned_window(context_window, self.settings.context_window)
        if continuous:
            with self._lock:
                self._continuous_stopped.discard(str(workspace))
        picked_gate = "auto" if continuous else _gate_value(gate, "auto")
        if continuous:
            picked_triage, picked_approval = "auto", "auto"
        else:
            if (triage_gate or "").strip() not in {"", "ask", "auto"}:
                raise ValueError("triage_gate must be ask or auto")
            if (approval_gate or "").strip() not in {"", "ask", "auto"}:
                raise ValueError("approval_gate must be ask or auto")
            picked_triage, picked_approval = resolve_human_gates(
                picked_gate, triage_gate, approval_gate
            )
        if picked_triage == "ask" or picked_approval == "ask":
            picked_gate = "ask"
        task = Task(
            id=uuid4().hex,
            workspace=str(workspace),
            goal=(goal.strip() if goal and goal.strip() else None),
            auto_commit=self.settings.auto_commit if auto_commit is None else auto_commit,
            create_pr=True if continuous and create_pr is None else create_pr,
            model_name=selected_model,
            model_base_url=selected_url,
            model_api_key=selected_key,
            resume_branch=(resume_branch or "").strip() or None,
            resume_sha=(resume_sha or "").strip() or None,
            base_branch=(base_branch or "").strip() or None,
            branch=(resume_branch or "").strip() or None,
            context_window=pinned,
            pin_context_window=pinned is not None,
            continuous=continuous,
            gate=picked_gate,
            triage_gate=picked_triage,
            approval_gate=picked_approval,
            requester_id=(requester_id or "").strip(),
        )
        with self._lock:
            self._tasks[task.id] = task
        self._stamp_committer(task)
        if self.store_root is not None:
            try:
                from agent_lbw.runtime.store.recording import record_task

                record_task(self.store_root, self.settings.database_url, task)
            except Exception:
                with self._lock:
                    self._tasks.pop(task.id, None)
                raise
        self._executor.submit(self._run, task)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda item: item.created_at, reverse=True)
        return tasks

    def list_for_workspace(self, workspace: str) -> list[Task]:
        """Return tasks for a specific workspace, sorted newest first."""
        if not workspace:
            return []
        resolved = Path(workspace).expanduser().resolve()
        with self._lock:
            tasks = [
                task
                for task in self._tasks.values()
                if Path(task.workspace).expanduser().resolve() == resolved
            ]
        tasks.sort(key=lambda item: item.created_at, reverse=True)
        return tasks

    def has_running_task(self, workspace: str, model_name: str, model_base_url: str) -> bool:
        """Check if there's already a running task for the same LLM server on this workspace."""
        workspace_path = workspace.expanduser().resolve() if workspace else ""
        with self._lock:
            for task in self._tasks.values():
                if Path(task.workspace).resolve() != Path(workspace_path).resolve():
                    continue
                if task.status != "running":
                    continue
                if task.model_name == model_name and task.model_base_url == model_base_url:
                    return True
        return False

    def counts(self) -> dict[str, int]:
        with self._lock:
            statuses = [task.status for task in self._tasks.values()]
        return {
            "queued": statuses.count("queued"),
            "running": statuses.count("running"),
            "total": len(statuses),
        }

    def rerun(
        self,
        task_id: str,
        from_sha: str | None = None,
        goal: str | None = None,
    ) -> Task | None:
        """Queue a new cycle on the task's branch, with an optional revised goal."""
        with self._lock:
            old_task = self._tasks.get(task_id)
        if not old_task:
            return None
        if old_task.status not in ("failed", "error", "success"):
            return None
        resume_branch = old_task.branch
        resume_sha = (from_sha or old_task.commit_sha or old_task.sha_before or "").strip() or None
        base_branch = old_task.base_branch
        if (resume_branch or "").strip() in PROTECTED_BRANCHES and not from_sha:
            base_branch = base_branch or resume_branch
            resume_branch = None
            resume_sha = None
        revised = (goal or "").strip()
        create_pr = True if old_task.pr_url else old_task.create_pr
        return self.submit(
            Path(old_task.workspace),
            revised or old_task.goal,
            auto_commit=old_task.auto_commit,
            create_pr=create_pr,
            model_name=old_task.model_name,
            model_base_url=old_task.model_base_url,
            model_api_key=old_task.model_api_key,
            resume_branch=resume_branch,
            resume_sha=resume_sha,
            base_branch=base_branch,
            continuous=old_task.continuous,
            context_window=old_task.context_window if old_task.pin_context_window else None,
            gate=old_task.gate,
            triage_gate=old_task.triage_gate,
            approval_gate=old_task.approval_gate,
        )

    def rework(self, task_id: str, note: str) -> Task | None:
        """Put this job back in progress and keep the request on it.

        A later note extends the same job. It does not open a second one.
        """
        reply = " ".join((note or "").split())
        if not reply:
            return None
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in {"queued", "running"}:
                return None
            if task.status not in {"success", "failed", "error"}:
                return None
            task.review_notes = _join_notes(task.review_notes, f"Rework request: {reply}")
            task.decision = "continue"
            task.stage = "building"
            task.status = "queued"
            task.finished_at = None
            task.error = None
            branch = (task.branch or "").strip()
            if branch not in PROTECTED_BRANCHES:
                task.resume_branch = branch
                task.resume_sha = (task.commit_sha or task.sha_before or "").strip() or None
            if task.pr_url:
                task.create_pr = True
        self._start(task)
        return task

    def adopt_open_jobs(self) -> None:
        """Put stored queued, running, and waiting jobs back after a restart."""
        if self.store_root is None:
            return
        try:
            from agent_lbw.runtime.store.recording import open_jobs

            records = open_jobs(self.store_root, self.settings.database_url)
        except Exception as exc:
            log.warning("open jobs were not restored: %s", exc)
            return
        for record in records:
            self._adopt(record, start=str(record.get("status") or "") in {"queued", "running"})

    def adopt_job(self, task_id: str) -> Task | None:
        """Load one stored job without starting it. A click decides whether it runs."""
        with self._lock:
            current = self._tasks.get(task_id)
        if current is not None:
            return current
        if self.store_root is None:
            return None
        try:
            from agent_lbw.runtime.store.recording import open_job

            record = open_job(self.store_root, self.settings.database_url, task_id)
        except Exception as exc:
            log.warning("task %s was not restored: %s", task_id, exc)
            return None
        if record is None:
            return None
        return self._adopt(record, start=False)

    def decide(self, task_id: str, decision: str, note: str = "") -> Task | None:
        """Resume a change that is waiting, or continue one that was sent back."""
        choice = (decision or "").strip()
        reply = " ".join((note or "").split())
        if self.get(task_id) is None:
            self.adopt_job(task_id)
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status == "waiting":
                if task.stage == "triage" and choice not in {"build", "hand_off"}:
                    return None
                if task.stage == "approval" and choice not in {"approve", "send_back"}:
                    return None
                if task.stage in {"ask", "reply"} and choice != "continue":
                    return None
                if task.stage not in {"triage", "approval", "ask", "reply"}:
                    return None
            elif task.status in {"failed", "error"} and choice == "continue":
                sent_back = _sent_back(task.evidence) or task.stage in {"review", "ask", "reply"}
                if not sent_back:
                    return None
            else:
                return None
            if choice in {"send_back", "continue"} and not reply:
                return None
            if choice == "approve" and self._approval_needs_github(task):
                from agent_lbw.tools.git import APPROVE_GITHUB

                raise ValueError(APPROVE_GITHUB)
            if choice == "approve":
                self._stamp_committer(task)
            task.decision = choice
            if choice == "send_back":
                task.review_notes = _join_notes(_review_notes(task.evidence), f"From you: {reply}")
            elif choice == "continue":
                task.review_notes = _join_notes(
                    _review_notes(task.evidence),
                    task.review_notes,
                    reply,
                )
            task.status = "queued"
            task.finished_at = None
            task.error = None
        self._start(task)
        return task

    def stop_continuous(self, workspace: str | Path) -> None:
        """Stop auto-queuing keep-improving cycles for this workspace."""
        key = str(Path(workspace).expanduser().resolve())
        with self._lock:
            self._continuous_stopped.add(key)

    def is_continuous_active(self, workspace: str | Path) -> bool:
        path = Path(workspace).expanduser().resolve()
        key = str(path)
        with self._lock:
            if key in self._continuous_stopped:
                return False
            return any(
                task.continuous
                and task.status in {"queued", "running"}
                and Path(task.workspace).resolve() == path
                for task in self._tasks.values()
            )

    def list_models(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> list[str]:
        url = normalize_model_base_url(base_url or self.settings.model_base_url)
        key = self.settings.model_api_key if not api_key else api_key
        if self._models_fn is not None:
            try:
                names = list(self._models_fn(url, key))
            except TypeError:
                names = list(self._models_fn())
        else:
            names = list_remote_models(url, key)
        default_url = normalize_model_base_url(self.settings.model_base_url)
        default = self.settings.model_name
        if url == default_url and default and default not in names:
            names.insert(0, default)
        return names

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _approval_needs_github(self, task: Task) -> bool:
        if self.store_root is None:
            return False
        from agent_lbw.runtime.db import database_configured

        if not database_configured(self.store_root, self.settings.database_url):
            return False
        if not _workspace_is_github(task.workspace):
            return False
        return self._connected_identity() is None

    def _stamp_committer(self, task: Task) -> None:
        """A GitHub commit is authored by the connected person."""
        if not _workspace_is_github(task.workspace):
            return
        identity = self._connected_identity()
        if identity is None:
            return
        task.committer_name, task.committer_email = identity

    def _connected_identity(self) -> tuple[str, str] | None:
        if self.store_root is None:
            return None
        from agent_lbw.runtime.store.install import commit_identity_for

        return commit_identity_for(self.store_root, self.settings.database_url)

    def _adopt(self, record: dict, *, start: bool) -> Task:
        with self._lock:
            current = self._tasks.get(str(record.get("id") or ""))
            if current is not None:
                return current
            task = self._task_from_record(record)
            if start:
                task.status = "queued"
                task.finished_at = None
                task.error = None
            self._tasks[task.id] = task
        if start:
            self._start(task)
        return task

    def _start(self, task: Task) -> None:
        """Remember the decision, then run. A restart can pick the job up from the row."""
        self._sync_stored_task(task)
        try:
            self._executor.submit(self._run, task)
        except RuntimeError as exc:
            log.warning("task %s was not started: %s", task.id, exc)

    def _task_from_record(self, record: dict) -> Task:
        stage = str(record.get("stage") or "")
        triage = "ask" if stage == "triage" else "auto"
        approval = "ask" if stage == "approval" else "auto"
        asking = triage == "ask" or approval == "ask" or stage in {"ask", "reply"}
        gate = "ask" if asking else "auto"
        goal = record.get("goal")
        if isinstance(goal, str):
            goal = goal.strip() or None
        raw_url = str(record.get("model_base_url") or "")
        model_url = normalize_model_base_url(raw_url) if raw_url else self.settings.model_base_url
        criteria = [
            str(item)
            for item in (record.get("criteria") or [])
            if isinstance(item, str) and item.strip()
        ]
        evidence = [item for item in (record.get("evidence") or []) if isinstance(item, dict)]
        decision = str(record.get("decision") or "") or None
        return Task(
            id=str(record["id"]),
            workspace=str(record.get("workspace") or ""),
            goal=goal,
            auto_commit=self.settings.auto_commit,
            create_pr=None,
            model_name=str(record.get("model_name") or "") or self.settings.model_name,
            model_base_url=model_url,
            model_api_key=self.settings.model_api_key,
            status=str(record.get("status") or "waiting"),
            created_at=str(record.get("created_at") or "") or _utcnow(),
            summary=record.get("summary") or None,
            reason=record.get("reason") or None,
            tests_passed=record.get("tests_passed"),
            committed=bool(record.get("committed")),
            published=bool(record.get("published")),
            pr_url=record.get("pr_url") or None,
            branch=record.get("branch") or None,
            gate=gate,
            triage_gate=triage,
            approval_gate=approval,
            decision=decision,
            stage=stage,
            plan=str(record.get("plan") or ""),
            criteria=criteria,
            evidence=evidence,
            requester_id=str((record.get("started_by") or {}).get("id") or ""),
            requester_name=str((record.get("started_by") or {}).get("name") or ""),
            gates=[item for item in (record.get("gates") or []) if isinstance(item, dict)],
            review_notes=str(record.get("review_notes") or ""),
        )

    def _run(self, task: Task) -> None:
        task.status = "running"
        task.started_at = _utcnow()
        if task.decision in {"build", "send_back", "continue"}:
            task.stage = "building"
        elif task.decision == "approve":
            task.stage = "approval"
            if self.store_root is not None:
                from agent_lbw.runtime.store.recording import accept_recorded_review

                accept_recorded_review(self.store_root, self.settings.database_url, task.id)
        if self._runner is None and task.context_window is None:
            task.context_window = lookup_context_window(
                task.model_base_url,
                task.model_api_key,
                task.model_name,
            )
        handler = _TaskLogHandler(task, threading.get_ident())
        lbw_log = logging.getLogger("lbw")
        lbw_log.setLevel(logging.INFO)
        lbw_log.addHandler(handler)
        exchange_logs = [logging.getLogger(name) for name in ("httpx", "openai")]
        for exchange_log in exchange_logs:
            exchange_log.addHandler(handler)
        progress = bind_progress(task.events)
        log.info("task %s model=%s url=%s", task.id, task.model_name, task.model_base_url)
        try:
            result = self._execute(task)
            task.summary = result.summary
            task.reason = result.reason
            task.tests_passed = result.tests_passed
            task.committed = result.committed
            task.published = result.published
            task.commit_sha = result.commit_sha
            task.pr_url = result.pr_url
            task.merged = bool(getattr(result, "merged", False))
            if getattr(result, "stage", ""):
                task.stage = result.stage
            if getattr(result, "plan", ""):
                task.plan = result.plan
            if getattr(result, "criteria", None):
                task.criteria = list(result.criteria)
            if getattr(result, "evidence", None):
                task.evidence = list(result.evidence)
            if getattr(result, "branch", None):
                task.branch = result.branch
            self._refresh_git_state(task)
            task.finished_at = _utcnow()
            task.status = result.status
        except Exception as exc:
            log.exception("task %s crashed", task.id)
            task.error = str(exc)
            task.reason = f"task crashed: {exc}"
            task.finished_at = _utcnow()
            task.status = "error"
        finally:
            reset_progress(progress)
            lbw_log.removeHandler(handler)
            for exchange_log in exchange_logs:
                exchange_log.removeHandler(handler)
            self._sync_stored_task(task)
        self._maybe_continue(task)

    def _execute(self, task: Task) -> CycleResult:
        self._resume_workspace(task)
        if self._runner is not None:
            return self._runner(task)
        updates: dict[str, object] = {
            "auto_commit": task.auto_commit,
            "model_name": task.model_name,
            "model_base_url": task.model_base_url,
            "model_api_key": task.model_api_key,
        }
        if task.pin_context_window and task.context_window:
            updates["context_window"] = task.context_window
        settings = self.settings.model_copy(update=updates)
        llm = self._llm_factory(settings)
        from agent_lbw.tools.git import reset_committer, use_committer

        committer = use_committer(task.committer_name, task.committer_email)
        try:
            return run_cycle(
                Path(task.workspace),
                settings,
                llm,
                task.goal,
                cli_create_pr=task.create_pr,
                continuous=task.continuous,
                sibling_tasks=sibling_task_records(self, task),
                context_window=task.context_window,
                task_id=task.id,
                base_branch=task.base_branch,
                keep_branch=bool(
                    task.resume_branch
                    or task.resume_sha
                    or task.decision in {"approve", "send_back", "continue"}
                ),
                gate=task.gate,
                triage_gate=task.triage_gate,
                approval_gate=task.approval_gate,
                decision=task.decision,
                review_notes=task.review_notes,
                held_summary=task.summary or "",
                store_root=self.store_root,
            )
        finally:
            reset_committer(committer)

    def _sync_stored_task(self, task: Task) -> None:
        if self.store_root is None:
            return
        try:
            from agent_lbw.runtime.store.recording import sync_task

            sync_task(self.store_root, self.settings.database_url, task)
        except Exception as exc:
            log.warning("task %s status was not stored: %s", task.id, exc)

    def _maybe_continue(self, task: Task) -> None:
        if not task.continuous or task.status != "success":
            return
        if not (task.committed or task.published or task.merged):
            return
        workspace = Path(task.workspace)
        with self._lock:
            if str(workspace.resolve()) in self._continuous_stopped:
                return
        log.info("keep improving: queueing the next cycle for %s", workspace)
        try:
            self.submit(
                workspace,
                None,
                auto_commit=task.auto_commit,
                create_pr=task.create_pr,
                model_name=task.model_name,
                model_base_url=task.model_base_url,
                model_api_key=task.model_api_key,
                base_branch=task.base_branch,
                continuous=True,
            )
        except RuntimeError as exc:
            log.warning("could not queue next keep-improving cycle: %s", exc)

    def _resume_workspace(self, task: Task) -> None:
        from agent_lbw.sandbox import SandboxError, Workspace
        from agent_lbw.tools.git import current_branch, current_sha, resume_workspace

        try:
            workspace = Workspace(Path(task.workspace))
        except (OSError, SandboxError):
            return
        if not task.sha_before:
            task.sha_before = current_sha(workspace)
        if not task.branch:
            task.branch = current_branch(workspace)
        branch = task.resume_branch
        sha = task.resume_sha
        if not branch and not sha:
            return
        result = resume_workspace(workspace, branch=branch, sha=sha)
        if result.ok:
            task.branch = result.output or branch or task.branch
            log.info("resumed task on %s", task.branch)
            record_event(
                kind="step",
                message=f"Resuming on branch {task.branch}",
            )
        else:
            log.warning("could not resume %s: %s", branch or sha, result.output)
            record_event(
                kind="step",
                message=f"Could not checkout {branch or sha}; continuing on the current tree",
            )

    def _refresh_git_state(self, task: Task) -> None:
        from agent_lbw.sandbox import SandboxError, Workspace
        from agent_lbw.tools.git import current_branch, current_sha

        try:
            workspace = Workspace(Path(task.workspace))
        except (OSError, SandboxError):
            return
        task.branch = current_branch(workspace) or task.branch
        if not task.commit_sha:
            task.commit_sha = current_sha(workspace)
