"""Run a job on the acting person's own machine, through their linked runner.

The runner uses that person's local Claude Code, signed in through Anthropic's
own flow, so LBW never holds a Claude credential for it. LBW hands the runner
the goal and the commit to start from, shows the runner's progress on the job,
applies the commits it sends back on a branch, runs the project's tests, and
publishes with the organization's GitHub App.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from agent_lbw.config import Settings
from agent_lbw.progress import record_event
from agent_lbw.runtime.db import database
from agent_lbw.runtime.improve import CycleResult, publish_runner_change
from agent_lbw.runtime.runner_hub import MAX_PATCH_BYTES, RunnerHub
from agent_lbw.runtime.store.audit import record_audit
from agent_lbw.runtime.store.runners import (
    RunnerRequired,
    create_job,
    finish_job,
    job_row,
    lease_lapsed,
    pick_runner,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.git import current_branch, parse_github_remote, run_git

log = logging.getLogger("lbw.runner")

AGENT_KIND = "claude_code"
SERVICE = "claude-code (runner)"
JOB_SECONDS = 2 * 60 * 60
PICKUP_SECONDS = 10 * 60
_CHECK_SECONDS = 10.0
_EVENT_KINDS = frozenset({"step", "tool", "llm", "note"})
_TEXT_FIELDS = ("message", "name")
_COUNT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "elapsed_ms",
)


def clean_remote(url: str) -> str:
    """A remote URL without any user name, password, or token in it."""
    raw = (url or "").strip()
    if "://" not in raw:
        return raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def runner_event(event: object) -> dict | None:
    """Keep only the fields LBW shows or counts. Anything else a runner sends is dropped."""
    if not isinstance(event, dict):
        return None
    kind = str(event.get("kind") or "")
    if kind not in _EVENT_KINDS:
        return None
    kept: dict = {"kind": kind}
    for name in _TEXT_FIELDS:
        if event.get(name):
            kept[name] = str(event[name])[:2000]
    if kind == "llm":
        for name in _COUNT_FIELDS:
            try:
                kept[name] = max(0, int(event.get(name) or 0))
            except (TypeError, ValueError):
                kept[name] = 0
        kept["model_name"] = str(event.get("model_name") or "")[:120]
        kept["ok"] = event.get("ok") is not False
        kept["service"] = SERVICE
        kept["provider"] = "anthropic"
        kept["role"] = "build"
        kept["purpose"] = "agent"
    return kept


def _git_facts(workspace: Workspace) -> dict:
    sha = run_git(workspace, ["rev-parse", "HEAD"])
    remote = run_git(workspace, ["remote", "get-url", "origin"])
    url = clean_remote(remote.stdout.strip()) if remote.returncode == 0 else ""
    parsed = parse_github_remote(url) if url else None
    return {
        "base_sha": sha.stdout.strip() if sha.returncode == 0 else "",
        "remote_url": url,
        "repository": f"{parsed[0]}/{parsed[1]}" if parsed else "",
        "project": workspace.root.name,
    }


def _failed(task, reason: str, *, summary: str = "") -> CycleResult:
    return CycleResult(
        status="failed",
        goal=task.goal,
        summary=summary or reason,
        tests_passed=None,
        committed=False,
        published=False,
        commit_sha=task.sha_before,
        reason=reason,
    )


def run_on_runner(
    task,
    *,
    settings: Settings,
    store_root: Path,
    hub: RunnerHub,
    job_seconds: float = JOB_SECONDS,
    pickup_seconds: float = PICKUP_SECONDS,
) -> CycleResult:
    workspace = Workspace(Path(task.workspace))
    facts = _git_facts(workspace)
    if not facts["base_sha"]:
        return _failed(task, "a runner job needs a git checkout with at least one commit")
    identity_id = task.actor_id or task.requester_id
    job_id = uuid4().hex
    hub.open(
        job_id,
        {
            "job_id": job_id,
            "task_id": task.id,
            "goal": task.goal or "",
            "review_notes": task.review_notes or "",
            "base_branch": task.base_branch or current_branch(workspace) or "",
            "model": task.model_name if task.model_id else "",
            "max_seconds": int(job_seconds),
            **facts,
        },
    )
    try:
        with database(store_root, settings.database_url) as db:
            runner = pick_runner(db, task.tenant_id, identity_id)
            create_job(
                db,
                job_id=job_id,
                tenant_id=task.tenant_id,
                identity_id=identity_id,
                runner_id=runner["id"],
                task_id=task.id,
            )
            record_audit(
                db,
                tenant_id=task.tenant_id,
                actor_id=identity_id,
                action="task.runner",
                summary=f"Sent this job to the runner {runner['name']}",
                method="POST",
                path=f"/api/tasks/{task.id}",
                target_type="task",
                target_id=task.id,
            )
    except RunnerRequired as exc:
        hub.close(job_id)
        return _failed(task, str(exc))
    except Exception:
        hub.close(job_id)
        raise
    record_event(kind="step", message=f"Waiting for the runner {runner['name']} to start")
    try:
        result = _wait(job_id, task, settings, store_root, hub, job_seconds, pickup_seconds)
    finally:
        hub.close(job_id)
    if result.get("status") != "done":
        reason = str(result.get("error") or "the runner could not finish the job")
        with database(store_root, settings.database_url) as db:
            finish_job(db, job_id, "cancelled" if result.get("cancelled") else "failed", reason)
        return _failed(task, reason, summary=str(result.get("summary") or reason))
    with database(store_root, settings.database_url) as db:
        finish_job(db, job_id, "done")
    summary = str(result.get("summary") or "").strip() or "The runner finished the job."
    patch = str(result.get("patch") or "")
    if not patch.strip():
        record_event(kind="step", message="The runner made no changes")
        return CycleResult(
            status="skipped",
            goal=task.goal,
            summary=summary,
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=facts["base_sha"],
            reason="the runner made no changes",
        )
    applied = _apply(workspace, task, settings, facts["base_sha"], patch)
    if applied:
        return _failed(task, applied, summary=summary)
    record_event(
        kind="step", message=f"Applied the runner's commits on {current_branch(workspace)}"
    )
    return publish_runner_change(
        workspace.root,
        settings,
        task.goal or "",
        summary,
        base_sha=facts["base_sha"],
        create_pr=bool(task.create_pr),
        store_root=store_root,
        pr_base=task.base_branch,
    )


def _wait(job_id, task, settings, store_root, hub, job_seconds, pickup_seconds) -> dict:
    started = time.monotonic()
    checked = started
    picked = False
    while True:
        events, result = hub.wait(job_id, 2.0)
        for event in events:
            kept = runner_event(event)
            if kept is not None:
                fields = {key: value for key, value in kept.items() if key != "kind"}
                record_event(kept["kind"], **fields)
        if result is not None:
            return result
        now = time.monotonic()
        if now - started > job_seconds:
            hub.cancel(job_id)
            return {"status": "failed", "error": "the runner job ran past its time limit"}
        if now - checked < _CHECK_SECONDS:
            continue
        checked = now
        with database(store_root, settings.database_url) as db:
            row = job_row(db, job_id)
        if row is None or row["status"] == "cancelled":
            return {"status": "failed", "cancelled": True, "error": "the runner job was cancelled"}
        if row["status"] == "running" and not picked:
            picked = True
            record_event(kind="step", message="The runner started the job")
        if row["status"] == "queued" and now - started > pickup_seconds:
            return {
                "status": "failed",
                "error": "the runner did not pick up the job: is it running?",
            }
        if lease_lapsed(row):
            return {"status": "failed", "error": "the runner stopped checking in"}


def _apply(workspace: Workspace, task, settings: Settings, base_sha: str, patch: str) -> str:
    """Put the runner's commits on a fresh branch. Returns an error, or '' when applied."""
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return "the runner's change is larger than LBW accepts"
    branch = f"lbw/runner-{task.id[:8]}"
    moved = run_git(workspace, ["checkout", "-B", branch, base_sha])
    if moved.returncode != 0:
        return f"could not start {branch}: {(moved.stderr or moved.stdout).strip()}"
    name = task.committer_name or settings.git_author_name
    email = task.committer_email or settings.git_author_email
    env = {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email} if name and email else {}
    with tempfile.TemporaryDirectory(prefix="lbw-runner-") as scratch:
        mbox = Path(scratch) / "change.mbox"
        mbox.write_text(patch, encoding="utf-8")
        applied = run_git(workspace, ["am", "--3way", "--keep-cr", str(mbox)], env=env)
    if applied.returncode != 0:
        run_git(workspace, ["am", "--abort"])
        return f"the runner's commits did not apply: {(applied.stderr or applied.stdout).strip()}"
    task.branch = branch
    return ""
