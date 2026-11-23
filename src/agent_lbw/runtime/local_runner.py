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
from agent_lbw.runtime.improve import (
    CycleResult,
    publish_runner_change,
    resolve_create_pr,
    resolve_human_gates,
)
from agent_lbw.runtime.plan import PLAN_SYSTEM, format_plan, parse_plan
from agent_lbw.runtime.project import load_project
from agent_lbw.runtime.runner_hub import MAX_PATCH_BYTES, RunnerHub
from agent_lbw.runtime.spec import (
    acceptance_criteria,
    clean_spec,
    save_spec,
    spec_context,
    spec_instructions,
    spec_path_for,
    spec_request,
)
from agent_lbw.runtime.store.audit import record_audit
from agent_lbw.runtime.store.recording import job_start
from agent_lbw.runtime.store.runners import (
    RunnerRequired,
    create_job,
    finish_job,
    job_row,
    lease_lapsed,
    pick_runner,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.git import (
    current_branch,
    current_sha,
    parse_github_remote,
    run_git,
    stash_local_edits,
)

log = logging.getLogger("lbw.runner")

AGENT_KIND = "claude_code"
SERVICE = "claude-code (runner)"
JOB_SECONDS = 2 * 60 * 60
PICKUP_SECONDS = 10 * 60
_CHECK_SECONDS = 10.0
_BRANCH_PREFIX = "lbw/runner-"
_COMMAND_LIMIT = 300
_WAITING_REASONS = {
    "spec": "waiting for spec review",
    "triage": "waiting for triage",
    "approval": "waiting for approval",
}
_SENT = {
    "spec": "Sent this job's spec to the runner {name}",
    "plan": "Sent this job's plan to the runner {name}",
    "build": "Sent this job to the runner {name}",
}
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
    branch = current_branch(workspace) or ""
    remote = run_git(workspace, ["remote", "get-url", "origin"])
    url = clean_remote(remote.stdout.strip()) if remote.returncode == 0 else ""
    parsed = parse_github_remote(url) if url else None
    return {
        "base_sha": sha.stdout.strip() if sha.returncode == 0 else "",
        "remote_url": url,
        "repository": f"{parsed[0]}/{parsed[1]}" if parsed else "",
        "project": workspace.root.name,
        "branch": branch,
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
    """Take a runner job through the same gates a job in LBW stops at.

    A spec and a plan are written on the runner without changing files, and the
    job waits for a person at each. The runner's commits wait on their branch
    for approval before LBW publishes them.
    """
    workspace = Workspace(Path(task.workspace))
    decision = (task.decision or "").strip()
    if decision == "hand_off":
        record_event(kind="step", message="Handing this change to a person. Nothing will be built.")
        return CycleResult(
            status="handed_off",
            goal=task.goal,
            summary="Handed to a person. Nothing was built.",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="handed to a person",
            stage="handed_off",
        )
    if decision == "approve":
        return _publish_approved(workspace, task, settings, store_root)
    facts = _git_facts(workspace)
    base = _recorded_start(task)
    if base:
        facts["base_sha"] = base
    elif facts["base_sha"]:
        sha = facts["base_sha"]
        record_event(kind="step", message=f"Starting from {sha[:8]}", start_sha=sha)
    if not facts["base_sha"]:
        return _failed(task, "a runner job needs a git checkout with at least one commit")
    triage, approval = resolve_human_gates(task.gate, task.triage_gate, task.approval_gate)
    goal = task.goal or ""
    spec_path = ""
    spec_text = ""
    criteria: list[str] = []
    if task.spec:
        spec_path = task.spec_path or spec_path_for(goal)
        spec_text = task.spec_text
        rewriting = not task.spec_accepted and decision == "send_back"
        if not spec_text.strip() or rewriting:
            note = task.review_notes if rewriting else ""
            asked = spec_request(goal, previous=spec_text, note=note)
            answer = _read_only(
                "spec",
                f"{spec_instructions()}\n\n{asked}",
                task,
                settings,
                store_root,
                hub,
                facts,
                job_seconds,
                pickup_seconds,
            )
            if isinstance(answer, CycleResult):
                return answer
            spec_text = clean_spec(answer)
        criteria = acceptance_criteria(spec_text)
        if not task.spec_accepted and not task.continuous:
            record_event(kind="step", message=f"The spec is ready in {spec_path}")
            return _waiting(
                task,
                facts["base_sha"],
                "spec",
                f"Spec ready for review: {spec_path}",
                criteria=criteria,
                spec_path=spec_path,
                spec_text=spec_text,
            )
    if triage == "ask" and decision not in {"build", "send_back", "continue"}:
        asked = f"{PLAN_SYSTEM}\n\nGoal:\n{goal.strip()}"
        if spec_text.strip():
            asked = f"{asked}\n\n{spec_context(spec_path, spec_text)}"
        answer = _read_only(
            "plan", asked, task, settings, store_root, hub, facts, job_seconds, pickup_seconds
        )
        if isinstance(answer, CycleResult):
            return answer
        plan = parse_plan(answer)
        plan_text = format_plan(plan) if plan is not None and plan.steps else answer.strip()
        checks = [step.check for step in plan.steps if step.check] if plan is not None else []
        record_event(kind="step", message="The plan is ready")
        return _waiting(
            task,
            facts["base_sha"],
            "triage",
            plan_text,
            plan=plan_text,
            criteria=[*criteria, *(item for item in checks if item not in criteria)],
        )
    start_patch = ""
    held = task.branch or ""
    if decision == "send_back" and held.startswith(_BRANCH_PREFIX):
        earlier = run_git(workspace, ["format-patch", "--stdout", f"{facts['base_sha']}..{held}"])
        if earlier.returncode != 0:
            return _failed(task, f"could not read the work on {held}: {earlier.stderr.strip()}")
        start_patch = earlier.stdout
    result = _dispatch(
        {
            "phase": "build",
            "spec_path": spec_path if task.spec_accepted else "",
            "spec": spec_text if task.spec_accepted else "",
            "plan": task.plan or "",
            "start_patch": start_patch,
            "commands": project_commands(workspace.root),
        },
        task,
        settings,
        store_root,
        hub,
        facts,
        job_seconds,
        pickup_seconds,
    )
    if isinstance(result, CycleResult):
        return result
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
    if task.spec_accepted and spec_text.strip():
        saved = _commit_spec(workspace, task, settings, spec_path, spec_text)
        if saved:
            return _failed(task, saved, summary=summary)
    if approval == "ask" and not task.continuous:
        record_event(kind="step", message="Waiting for you to approve the change")
        held_result = _waiting(
            task,
            current_sha(workspace),
            "approval",
            summary,
            plan=task.plan or "",
            criteria=criteria or list(task.criteria or []),
            spec_path=spec_path,
            spec_text=spec_text,
        )
        held_result.committed = True
        held_result.branch = current_branch(workspace)
        return held_result
    return publish_runner_change(
        workspace.root,
        settings,
        goal,
        summary,
        base_sha=facts["base_sha"],
        create_pr=_wants_pr(task, workspace, settings),
        store_root=store_root,
        pr_base=task.base_branch,
    )


def project_commands(root: Path) -> list[str]:
    """The project's setup, test, lint, and preview commands, which a build may run.

    LBW runs the same commands on the result, so the runner allows exactly these.
    """
    try:
        project = load_project(root)
    except ValueError:
        return []
    found: list[str] = []
    for command in (
        project.setup_command,
        project.test_command,
        project.lint_command,
        project.preview_command,
    ):
        text = " ".join(str(command or "").split())
        if text and len(text) <= _COMMAND_LIMIT and text not in found:
            found.append(text)
    return found


def _wants_pr(task, workspace: Workspace, settings: Settings) -> bool:
    """The job's own choice, or the project's setting for a job restored without one."""
    if task.create_pr is not None:
        return bool(task.create_pr)
    return resolve_create_pr(settings, load_project(workspace.root), None)


def _recorded_start(task) -> str:
    """The commit this job's change starts from, once a run recorded it."""
    events = getattr(task, "events", None) or []
    if any(isinstance(event, dict) and event.get("start_sha") for event in events):
        return job_start(task)
    return str(getattr(task, "change_base", "") or "").strip()


def _waiting(
    task,
    sha: str | None,
    stage: str,
    summary: str,
    *,
    plan: str = "",
    criteria: list[str] | None = None,
    spec_path: str = "",
    spec_text: str = "",
) -> CycleResult:
    return CycleResult(
        status="waiting",
        goal=task.goal,
        summary=summary,
        tests_passed=None,
        committed=False,
        published=False,
        commit_sha=sha,
        reason=_WAITING_REASONS[stage],
        stage=stage,
        plan=plan,
        criteria=list(criteria or []),
        spec_path=spec_path,
        spec_text=spec_text,
    )


def _publish_approved(workspace: Workspace, task, settings: Settings, store_root: Path):
    """Publish the runner's commits a person approved. Nothing runs on the runner."""
    held = task.branch or ""
    if not held.startswith(_BRANCH_PREFIX):
        return _failed(task, "the approved change is not on a runner branch")
    moved = _switch(workspace, [held])
    if moved:
        return _failed(task, f"could not check out {held}: {moved}")
    record_event(kind="step", message=f"Publishing the approved change on {held}")
    return publish_runner_change(
        workspace.root,
        settings,
        task.goal or "",
        task.summary or task.goal or "",
        base_sha=_recorded_start(task) or None,
        create_pr=_wants_pr(task, workspace, settings),
        store_root=store_root,
        pr_base=task.base_branch,
    )


def _commit_spec(workspace: Workspace, task, settings: Settings, path: str, text: str) -> str:
    """Commit the accepted spec with the change. Returns an error, or '' when done."""
    try:
        save_spec(workspace.root, path, text)
    except ValueError as exc:
        return str(exc)
    changed = run_git(workspace, ["status", "--porcelain", "--", path])
    if not changed.stdout.strip():
        return ""
    added = run_git(workspace, ["add", "--", path])
    if added.returncode != 0:
        return f"could not add the spec: {(added.stderr or added.stdout).strip()}"
    committed = run_git(
        workspace, ["commit", "-q", "-m", f"Add the spec {path}"], env=_committer(task, settings)
    )
    if committed.returncode != 0:
        return f"could not commit the spec: {(committed.stderr or committed.stdout).strip()}"
    return ""


def _committer(task, settings: Settings) -> dict:
    name = task.committer_name or settings.git_author_name
    email = task.committer_email or settings.git_author_email
    if not (name and email):
        return {}
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _read_only(phase, instructions, task, settings, store_root, hub, facts, job_seconds, pickup):
    """Ask the runner for a spec or a plan. Returns its text, or the failed result."""
    record_event(kind="step", message=f"Writing the {phase} on your runner")
    result = _dispatch(
        {"phase": phase, "instructions": instructions},
        task,
        settings,
        store_root,
        hub,
        facts,
        job_seconds,
        pickup,
    )
    if isinstance(result, CycleResult):
        return result
    if str(result.get("patch") or "").strip():
        return _failed(task, f"this runner cannot write a {phase}: update lbw-runner")
    text = str(result.get("summary") or "").strip()
    if not text:
        return _failed(task, f"the runner did not write the {phase}")
    return text


def _dispatch(extra, task, settings, store_root, hub, facts, job_seconds, pickup_seconds):
    """Send one job to the acting person's runner. Returns its result, or the failed result."""
    identity_id = task.actor_id or task.requester_id
    job_id = uuid4().hex
    hub.open(
        job_id,
        {
            "job_id": job_id,
            "task_id": task.id,
            "goal": task.goal or "",
            "review_notes": task.review_notes or "",
            "base_branch": task.base_branch or facts.get("branch") or "",
            "model": task.model_name if task.model_id else "",
            "max_seconds": int(job_seconds),
            **{key: value for key, value in facts.items() if key != "branch"},
            **extra,
        },
    )
    phase = extra.get("phase") or "build"
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
                summary=_SENT[phase].format(name=runner["name"]),
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
    return result


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
    """Put the runner's commits on a fresh branch. Returns an error, or '' when applied.

    The commits are applied in a worktree of their own, because LBW itself edits
    the checkout while the job runs (it keeps `.lbw/` in the root .gitignore).
    Then the checkout moves to the result.
    """
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return "the runner's change is larger than LBW accepts"
    branch = f"{_BRANCH_PREFIX}{task.id[:8]}"
    committer = _committer(task, settings)
    env = {key: value for key, value in committer.items() if key.startswith("GIT_COMMITTER")}
    with tempfile.TemporaryDirectory(prefix="lbw-runner-") as scratch:
        tree = Path(scratch) / "tree"
        added = run_git(workspace, ["worktree", "add", "--detach", str(tree), base_sha])
        if added.returncode != 0:
            return f"could not start from {base_sha[:8]}: {(added.stderr or added.stdout).strip()}"
        try:
            mbox = Path(scratch) / "change.mbox"
            mbox.write_text(patch, encoding="utf-8")
            side = Workspace(tree)
            applied = run_git(side, ["am", "--3way", "--keep-cr", str(mbox)], env=env)
            if applied.returncode != 0:
                run_git(side, ["am", "--abort"])
                output = (applied.stderr or applied.stdout).strip()
                return f"the runner's commits did not apply: {output}"
            head = current_sha(side) or ""
        finally:
            run_git(workspace, ["worktree", "remove", "--force", str(tree)])
            run_git(workspace, ["worktree", "prune"])
    moved = _switch(workspace, ["-B", branch, head])
    if moved:
        return f"could not check out {branch}: {moved}"
    task.branch = branch
    return ""


def _switch(workspace: Workspace, target: list[str]) -> str:
    """Check out `target`, keeping a person's edits in `git stash`. Returns an error or ''.

    The checkout is forced because LBW may add `.lbw/` to .gitignore again
    between the stash and the checkout. It adds that line back afterwards.
    """
    cleared = stash_local_edits(workspace, "lbw: stashed before checking out a runner's commits")
    if not cleared.ok:
        return f"could not set local edits aside: {cleared.output}"
    moved = run_git(workspace, ["checkout", "-f", *target])
    if moved.returncode != 0:
        return (moved.stderr or moved.stdout).strip()
    return ""
