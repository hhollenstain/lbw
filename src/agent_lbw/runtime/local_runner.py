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
from agent_lbw.runtime.store.recording import file_events_from_diff, job_start
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
    is_protected_branch,
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
    started = _start_event(task)
    if started is not None:
        base = str(started["start_sha"])
        tip = str(started.get("tip_sha") or base)
    else:
        tip = facts["base_sha"]
        if not tip:
            return _failed(task, "a runner job needs a git checkout with at least one commit")
        base = published_base(workspace, tip)
        record_event(
            kind="step", message=_start_message(workspace, base, tip), start_sha=base, tip_sha=tip
        )
    facts["base_sha"] = base
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
    held = task.branch or ""
    if decision == "send_back" and _holds_runner_work(task, held):
        tip = held
    start_patch = ""
    if tip != base:
        earlier = run_git(workspace, ["format-patch", "--stdout", f"{base}..{tip}"])
        if earlier.returncode != 0:
            return _failed(task, f"could not read the work on {tip}: {earlier.stderr.strip()}")
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
    _record_file_changes(workspace, base)
    if approval == "ask" and not task.continuous:
        record_event(
            kind="step",
            message="Waiting for you to approve the change",
            approved_sha=current_sha(workspace) or "",
        )
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
        keep_pull_request=bool(getattr(task, "ci_pr_url", "")),
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


def published_base(workspace: Workspace, tip: str) -> str:
    """The newest commit of `tip` that a remote branch already has.

    The runner fetches its start from the remote, so it cannot start from a
    commit only this checkout has, such as a failed job's applied commits. The
    commits after this base go to the runner as its start patch instead. With
    no remote branch to compare against, `tip` itself is the base.
    """
    unpublished = run_git(
        workspace, ["rev-list", "--reverse", "--topo-order", tip, "--not", "--remotes"]
    )
    if unpublished.returncode != 0:
        return tip
    first = next(iter(unpublished.stdout.split()), "")
    if not first:
        return tip
    parent = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"{first}^"])
    if parent.returncode != 0 or not parent.stdout.strip():
        return tip
    has_remote = run_git(workspace, ["branch", "-r", "--contains", parent.stdout.strip()])
    if has_remote.returncode != 0 or not has_remote.stdout.strip():
        return tip
    return parent.stdout.strip()


def _start_message(workspace: Workspace, base: str, tip: str) -> str:
    if base == tip:
        return f"Starting from {base[:8]}"
    counted = run_git(workspace, ["rev-list", "--count", f"{base}..{tip}"])
    count = counted.stdout.strip() if counted.returncode == 0 else "some"
    return (
        f"Starting from {base[:8]}, the newest commit on the remote, "
        f"and sending the runner {count} commit(s) only this server has"
    )


def _start_event(task) -> dict | None:
    """The step that recorded where this job starts, and what it continues from."""
    for event in getattr(task, "events", None) or []:
        if isinstance(event, dict) and event.get("start_sha"):
            return event
    return None


def _notes_for_runner(task) -> str:
    """Review notes, and what failed checks printed when the run investigates them.

    The checks' output goes to the runner in memory. It is not stored.
    """
    parts = [str(task.review_notes or "").strip(), str(getattr(task, "ci_context", "") or "")]
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _continues_pull_request(task) -> bool:
    """A run that continues an open pull request, such as a fix for its failed checks."""
    return bool(task.resume_branch and (task.change_base or getattr(task, "ci_pr_url", "")))


def _branch_for(task) -> str:
    """Where the runner's commits go: the pull request's branch, or a fresh one."""
    if _continues_pull_request(task) and not is_protected_branch(task.resume_branch):
        return str(task.resume_branch)
    return f"{_BRANCH_PREFIX}{task.id[:8]}"


def _holds_runner_work(task, branch: str) -> bool:
    return bool(branch) and (branch.startswith(_BRANCH_PREFIX) or branch == _branch_for(task))


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
    if not _holds_runner_work(task, held):
        return _failed(task, "the approved change is not on a runner branch")
    moved = _switch(workspace, [held])
    if moved:
        return _failed(task, f"could not check out {held}: {moved}")
    start = _recorded_start(task)
    approved = _approved_tip(workspace, task, held, start)
    if not approved:
        return _failed(
            task,
            f"the approved commits are not on {held} or anywhere in this checkout. "
            "Rerun all to build the change again.",
        )
    if _tree(workspace, "HEAD") != _tree(workspace, approved):
        # Forced, like any switch: LBW's own .gitignore line can be back by now.
        restored = _switch(workspace, ["-B", held, approved])
        if restored:
            return _failed(task, f"could not put the approved commits back on {held}: {restored}")
        record_event(
            kind="step",
            message=f"Put the approved commits back on {held} at {approved[:8]}",
            approved_sha=approved,
        )
    record_event(
        kind="step", message=f"Publishing the approved change on {held}", approved_sha=approved
    )
    return publish_runner_change(
        workspace.root,
        settings,
        task.goal or "",
        task.summary or task.goal or "",
        base_sha=_recorded_start(task) or None,
        create_pr=_wants_pr(task, workspace, settings),
        store_root=store_root,
        pr_base=task.base_branch,
        keep_pull_request=bool(getattr(task, "ci_pr_url", "")),
    )


def _approved_tip(workspace: Workspace, task, held: str, start: str) -> str:
    """The commit a person approved for this change.

    The approval step records it. A run that restarts publishing has not
    recorded one yet, so the runner branch's own history is read instead: the
    newest commit it held that has work past the job's start. LBW owns that
    branch, so nothing else moves it after approval.
    """
    recorded = [
        str(event.get("approved_sha") or "")
        for event in reversed(getattr(task, "events", None) or [])
        if isinstance(event, dict)
    ]
    history = run_git(workspace, ["reflog", "show", "--format=%H", held, "--"])
    held_before = history.stdout.split() if history.returncode == 0 else []
    for sha in [*recorded, *held_before]:
        if sha and _has_work(workspace, start, sha):
            return sha
    return ""


def _has_work(workspace: Workspace, start: str, sha: str) -> bool:
    if run_git(workspace, ["cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0:
        return False
    if not start:
        return True
    ahead = run_git(workspace, ["rev-list", "--count", f"{start}..{sha}"])
    return ahead.returncode == 0 and ahead.stdout.strip() not in {"", "0"}


def _tree(workspace: Workspace, ref: str) -> str:
    found = run_git(workspace, ["rev-parse", f"{ref}^{{tree}}"])
    return found.stdout.strip() if found.returncode == 0 else ""


def _record_file_changes(workspace: Workspace, base: str) -> None:
    """Show the runner's change file by file, as a change built in LBW shows it."""
    diff = run_git(workspace, ["diff", "--no-color", "--no-ext-diff", f"{base}..HEAD"])
    if diff.returncode != 0:
        return
    for event in file_events_from_diff(diff.stdout):
        fields = {key: value for key, value in event.items() if key != "kind"}
        record_event(kind="file", **fields)


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
            "review_notes": _notes_for_runner(task),
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
    branch = _branch_for(task)
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
