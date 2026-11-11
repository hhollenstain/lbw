from __future__ import annotations

import hashlib
import json
import logging
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_lbw.agent.loop import AgentResult, CodingAgent
from agent_lbw.config import Settings
from agent_lbw.llm.client import LLMClient
from agent_lbw.progress import bind_progress, current_events, record_event, reset_progress
from agent_lbw.runtime.importer import goal_headline
from agent_lbw.runtime.plan import GoalPlan, PlanStep, format_plan, ground_plan, plan_goal
from agent_lbw.runtime.polloco_review import blocking_review_findings
from agent_lbw.runtime.project import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    ProjectConfig,
    collect_context,
    ensure_run_gitignore,
    load_goals,
    load_project,
    mark_goal_done,
)
from agent_lbw.runtime.related import format_goal_files
from agent_lbw.runtime.review import (
    GoalReview,
    error_search_needle,
    explain_miss,
    failure_excerpt,
    guard_only_fix,
    half_baked_diff_markers,
    incomplete_agent_run,
    invalid_doc_commands,
    is_open_pr_goal,
    missing_added_resources,
    review_goal,
    review_reason,
    unused_new_symbols,
    unwired_ui_markers,
)
from agent_lbw.runtime.roles import (
    optional_review_roles,
    plan_role,
    review_with_role,
    role_evidence,
)
from agent_lbw.runtime.rules import rules_for_prompt
from agent_lbw.runtime.skills import compose_system_prompt
from agent_lbw.runtime.steward import propose_improvement_goal
from agent_lbw.runtime.uireview import (
    UiEvidence,
    collect_ui_evidence,
    format_ui_evidence,
    resolve_ui_screenshot,
    ui_review_needed,
    unverified_interactive_ui,
)
from agent_lbw.runtime.workdiff import (
    collect_current_evidence,
    collect_work_diff,
    nested_projects_changed,
)
from agent_lbw.sandbox import SandboxError, Workspace
from agent_lbw.tools import build_tools
from agent_lbw.tools.codechange import workspace_fingerprint
from agent_lbw.tools.git import (
    CO_AUTHORED_BY,
    branch_named_in_goal,
    checkout_base_branch,
    commit_changes,
    commits_ahead_of_base,
    create_pull_request,
    current_branch,
    current_sha,
    default_base_branch,
    ensure_pr_branch,
    existing_pull_request,
    extract_pr_url,
    fold_unpublished_commits,
    github_owner_repo,
    goal_keeps_current_branch,
    has_changes,
    intended_pr_base,
    is_protected_branch,
    is_tracked,
    merge_pull_request,
    move_commits_off_base,
    prepare_clean_base,
    push_changes,
    update_pull_request,
    upstream_state,
)
from agent_lbw.tools.kiwi import resolve_kiwifs_prefix
from agent_lbw.tools.lint import (
    apply_ruff_autofix,
    current_lint_result,
    ruff_unavailable,
    run_project_lint,
)
from agent_lbw.tools.tests import current_test_result, run_project_tests, runner_unavailable

log = logging.getLogger("lbw")

_TASK_ID: ContextVar[str | None] = ContextVar("lbw_palace_task_id", default=None)
_PALACE: ContextVar[object] = ContextVar("lbw_palace", default=None)


def log_progress(message: str) -> None:
    record_event(kind="step", message=message)
    log.info("%s", message)


def compact_handoff(
    *,
    goal: str,
    summary: str,
    diff: str,
    last_tool_error: str | None,
    stopped_reason: str,
) -> str:
    """Build a compact handoff brief for the agent after hitting max_iterations.

    Includes goal, changed file paths (not full hunks), last tool error, and
    a prompt to finish remaining work. Cap at 4000 characters.
    """
    # Extract file paths from diff
    paths: list[str] = []
    if diff:
        for line in diff.splitlines():
            # Match diff headers like "diff --git a/... b/file" or "+++ b/file"
            if line.startswith("diff --git "):
                parts = line.split(" b/")
                if len(parts) >= 2:
                    paths.append(parts[1].strip())
            elif line.startswith("+++ b/"):
                path = line.split(" b/")[1].strip()
                if path and path != "/dev/null":
                    paths.append(path)
    # Limit paths to 20
    paths = paths[:20]
    # Build handoff message
    lines = [
        "COMPACT HANDOFF",
        f"Goal: {goal.strip()}",
        f"Previous run summary: {summary.strip() if summary else 'None'}",
        f"Stopped: {stopped_reason}",
    ]
    if paths:
        lines.append(f"Changed files ({len(paths)}):")
        for p in paths:
            lines.append(f"  - {p}")
    if last_tool_error:
        lines.append(f"Last tool error: {last_tool_error[:200]}")
    lines.append(
        "\nFinish the remaining work. Do not re-read the whole repo.\n"
        "Only edit files that implement the goal."
    )
    result = "\n".join(lines)
    if len(result) > 3997:
        result = result[:3997]
    return result


def _compute_goal_key(goal: str) -> str:
    """Compute a 16-char hex prefix of sha256 of stripped goal."""
    stripped = goal.strip()
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
    return digest[:16]


def _write_last_failure(root: Path, goal: str, result: CycleResult) -> None:
    """Write last-failure.json after a failed cycle.

    Does not fail the cycle if JSON write fails; logs and continues.
    """
    LAST_FAILURE_FILE = ".lbw/last-failure.json"
    try:
        failure_path = root / LAST_FAILURE_FILE
        # Ensure .lbw directory exists before writing the file
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        summary = (result.summary or "")[:500] if result.summary else ""
        data = {
            "goal": goal,
            "goal_key": _compute_goal_key(goal),
            "status": result.status,
            "reason": result.reason or "unknown",
            "summary": summary,
            "finished_at": result.created_at,
        }
        failure_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log_progress("Remembered last failure for the next run.")
    except (OSError, TypeError) as e:
        log.warning(f"Failed to write last-failure.json: {e}")
        # Do not fail the cycle; just log and continue


def _delete_last_failure(root: Path) -> None:
    """Delete last-failure.json if it exists.

    Does not fail if deletion fails; logs and continues.
    """
    LAST_FAILURE_FILE = ".lbw/last-failure.json"
    try:
        failure_path = root / LAST_FAILURE_FILE
        if failure_path.exists():
            failure_path.unlink()
            log.debug("Deleted last-failure.json after successful cycle.")
    except OSError as e:
        log.warning(f"Failed to delete last-failure.json: {e}")


@dataclass
class CycleResult:
    status: str
    goal: str | None
    summary: str | None
    tests_passed: bool | None
    committed: bool
    published: bool
    commit_sha: str | None
    reason: str | None
    pr_url: str | None = None
    branch: str | None = None
    merged: bool = False
    stage: str = ""
    plan: str = ""
    criteria: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    events: list[dict] = field(default_factory=list)


@dataclass
class _EmptyDiffOutcome:
    result: CycleResult | None
    summary: str
    tests_passed: bool | None


def _resolve_start_branch(
    workspace: Workspace,
    goal: str | None,
    selected: str | None,
    publish_branch: str | None,
) -> str:
    """Dropdown selection wins, then a branch named in the goal, then head."""
    picked = (selected or "").strip()
    if picked:
        return picked
    noted = branch_named_in_goal(goal)
    if noted:
        return noted
    return default_base_branch(workspace, publish_branch)


def resolve_create_pr(
    settings: Settings,
    project: ProjectConfig,
    cli_create_pr: bool | None = None,
) -> bool:
    """`--no-create-pr` always wins. Otherwise CLI, settings, or a git repo can enable it."""
    if cli_create_pr is False:
        return False
    if cli_create_pr is True:
        return True
    return bool(settings.create_pr or project.publish_enabled)


def resolve_human_gates(
    gate: str = "auto",
    triage_gate: str = "",
    approval_gate: str = "",
) -> tuple[str, str]:
    """Return the triage and approval gates. A specific gate overrides `gate`."""
    base = "ask" if (gate or "").strip() == "ask" else "auto"

    def pick(value: str) -> str:
        picked = (value or "").strip()
        if picked in {"ask", "auto"}:
            return picked
        return base

    return pick(triage_gate), pick(approval_gate)


def run_cycle(
    workspace_path: Path,
    settings: Settings,
    llm: LLMClient,
    goal: str | None = None,
    *,
    mark_checkbox: bool = True,
    cli_create_pr: bool | None = None,
    continuous: bool = False,
    sibling_tasks: list[dict] | None = None,
    context_window: int | None = None,
    task_id: str | None = None,
    base_branch: str | None = None,
    keep_branch: bool = False,
    gate: str = "auto",
    triage_gate: str = "",
    approval_gate: str = "",
    decision: str | None = None,
    review_notes: str = "",
    held_summary: str = "",
    store_root: Path | None = None,
) -> CycleResult:
    progress = bind_progress()
    token = _TASK_ID.set(task_id)
    palace_token = _PALACE.set(None)
    try:
        return _run_cycle(
            workspace_path,
            settings,
            llm,
            goal,
            mark_checkbox=mark_checkbox,
            cli_create_pr=cli_create_pr,
            continuous=continuous,
            sibling_tasks=sibling_tasks,
            context_window=context_window,
            task_id=task_id,
            base_branch=base_branch,
            keep_branch=keep_branch,
            gate=gate,
            triage_gate=triage_gate,
            approval_gate=approval_gate,
            decision=decision,
            review_notes=review_notes,
            held_summary=held_summary,
            store_root=store_root,
        )
    finally:
        _PALACE.reset(palace_token)
        _TASK_ID.reset(token)
        reset_progress(progress)


def _run_cycle(
    workspace_path: Path,
    settings: Settings,
    llm: LLMClient,
    goal: str | None = None,
    *,
    mark_checkbox: bool = True,
    cli_create_pr: bool | None = None,
    continuous: bool = False,
    sibling_tasks: list[dict] | None = None,
    context_window: int | None = None,
    task_id: str | None = None,
    base_branch: str | None = None,
    keep_branch: bool = False,
    gate: str = "auto",
    triage_gate: str = "",
    approval_gate: str = "",
    decision: str | None = None,
    review_notes: str = "",
    held_summary: str = "",
    store_root: Path | None = None,
) -> CycleResult:
    log_progress("Initializing workspace...")
    triage_choice, approval_choice = resolve_human_gates(gate, triage_gate, approval_gate)
    normalized_decision = (decision or "").strip()
    evidence: list[dict] = []
    plan_text = ""
    criteria: list[str] = []
    workspace = Workspace(workspace_path)
    try:
        project = load_project(workspace.root)
    except ValueError as exc:
        log_progress(str(exc))
        return CycleResult(
            status="failed",
            goal=goal,
            summary=None,
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason=str(exc),
        )
    if normalized_decision == "hand_off":
        log_progress("Handing this change to a person. Nothing will be built.")
        result = CycleResult(
            status="handed_off",
            goal=goal,
            summary="Handed to a person. Nothing was built.",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=None,
            reason="handed to a person",
            stage="handed_off",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return result
    if normalized_decision == "approve":
        log_progress("Publishing the approved change...")
        allow_create_pr = resolve_create_pr(settings, project, cli_create_pr)
        return _publish_held(
            workspace,
            settings,
            project,
            goal or "",
            held_summary or "",
            allow_create_pr=allow_create_pr,
            mark_checkbox=mark_checkbox,
            store_root=store_root,
            pr_base=(base_branch or "").strip() or None,
        )
    stay_here = goal_keeps_current_branch(goal) and not (base_branch or "").strip()
    pr_base = (base_branch or "").strip() or None
    if not keep_branch and not stay_here and (workspace.root / ".git").exists():
        start = _resolve_start_branch(
            workspace,
            goal,
            base_branch,
            project.publish_branch,
        )
        pr_base = start
        log_progress(f"Starting from a clean {start}...")
        prepared = prepare_clean_base(workspace, start, project.publish_remote)
        if not prepared.ok:
            log_progress(prepared.output)
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes(prepared.output, None),
                tests_passed=None,
                committed=False,
                published=False,
                commit_sha=None,
                reason=prepared.output,
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return result
        log_progress(prepared.output)
    ensure_run_gitignore(workspace.root)
    allow_create_pr = resolve_create_pr(settings, project, cli_create_pr)
    tests_before = None
    if goal is None:
        log_progress("Running before tests...")
        tests_before = _maybe_test(workspace, project, settings, phase="before")
    selected_goal = goal or _choose_goal(project, tests_before)
    if not selected_goal and continuous:
        log_progress("Reviewing the codebase for the next best-practice or UX change...")
        selected_goal = propose_improvement_goal(
            llm,
            workspace,
            project,
            allow_publish=allow_create_pr,
        )
        if selected_goal:
            log_progress(f"Proposed improvement: {goal_headline(selected_goal)}")
    if not selected_goal:
        log_progress("No pending goals and tests are green.")
        result = CycleResult(
            status="skipped",
            goal=None,
            summary=None,
            tests_passed=_tests_passed_value(tests_before),
            committed=False,
            published=False,
            commit_sha=None,
            reason="no open goals and tests are green",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return result

    tools = build_tools(
        workspace,
        test_command=project.test_command,
        lint_command=project.lint_command,
        command_timeout_seconds=settings.command_timeout_seconds,
        git_author_name=settings.git_author_name,
        git_author_email=settings.git_author_email,
        allow_publish=False,
        goal=selected_goal,
        kiwifs_url=settings.kiwifs_url,
        kiwifs_api_key=settings.kiwifs_api_key,
        kiwifs_prefix=resolve_kiwifs_prefix(
            workspace.root,
            project.kiwifs_prefix,
            settings.kiwifs_prefix,
        ),
        store_root=store_root,
        database_url=settings.database_url,
    )

    log_progress(f"Selected goal: {goal_headline(selected_goal)}")
    log_progress("Fetching current SHA...")
    sha_before = current_sha(workspace)
    log_progress("Initializing coding agent...")
    extra_skills = ("steward",) if continuous else ()
    extra_skills = tuple(extra_skills) + tuple(
        _assigned_skills(store_root, settings.database_url, workspace.root)
    )
    included_skills = _scoped_skills(store_root, settings, workspace.root)
    palace = _workspace_palace(
        workspace.root,
        siblings=sibling_tasks,
        task_id=task_id,
        store_root=store_root,
        database_url=settings.database_url,
    )
    agent = CodingAgent(
        llm,
        tools,
        max_iterations=settings.max_iterations,
        system_prompt=compose_system_prompt(workspace.root, extra_skills, included_skills),
        palace=palace,
        context_window=context_window,
    )
    base_context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    if store_root is not None:
        try:
            rules_text = rules_for_prompt(
                store_root,
                workspace.root,
                database_url=settings.database_url,
                kiwifs_url=settings.kiwifs_url,
                kiwifs_api_key=settings.kiwifs_api_key,
            )
        except Exception as exc:  # noqa: BLE001 - a rules outage must not stop the cycle
            log_progress(f"Rules were not loaded: {exc}")
            rules_text = ""
        if rules_text:
            base_context = f"{base_context}\n\n{rules_text}"
    try:
        from agent_lbw.runtime.repo_rules import text_for_prompt

        repo_text = text_for_prompt(workspace.root)
    except Exception as exc:  # noqa: BLE001 - a missing instruction file must not stop the cycle
        log_progress(f"Repository instructions were not loaded: {exc}")
        repo_text = ""
    if repo_text:
        base_context = f"{base_context}\n\n{repo_text}"
    named_files = format_goal_files(workspace.root, selected_goal)
    plan = None
    architect = plan_role(workspace.root)
    if settings.plan_goals and architect is not None:
        log_progress("Architect is breaking the goal into steps...")
        plan = ground_plan(
            plan_goal(
                llm,
                selected_goal,
                context=base_context,
                files=named_files,
                opinions=architect.body,
            ),
            selected_goal,
            workspace.root,
        )
        _log_plan(plan)
    plan_text, criteria = _plan_record(plan, selected_goal)
    if plan is not None and plan.steps:
        evidence.append(
            role_evidence(
                architect.name if architect is not None else "architect",
                architect.title if architect is not None else "Architect",
                "plan",
                "pass",
                plan.cause or "Planned the change.",
            )
        )
    if triage_choice == "ask" and normalized_decision not in {"build", "send_back", "continue"}:
        log_progress("Plan is ready. Waiting for you to build it or hand it off.")
        return CycleResult(
            status="waiting",
            goal=selected_goal,
            summary=plan_text,
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=sha_before,
            reason="waiting for triage",
            stage="triage",
            plan=plan_text,
            criteria=criteria,
            evidence=list(evidence),
        )
    brief = _brief(plan, named_files, base_context)
    if (review_notes or "").strip():
        brief = (
            f"{brief}\n\nThis change was sent back. Use this note and continue. "
            "Do not start a different task.\n"
            f"{review_notes.strip()[:4000]}"
        )
    log_progress("Builder is writing the change...")
    code_before_agent = workspace_fingerprint(workspace.root)
    if plan is not None and plan.steps:
        agent_result = _run_plan_steps(
            workspace,
            agent,
            plan,
            selected_goal,
            brief,
            sha_before,
            research=_goal_needs_research(selected_goal),
        )
    else:
        agent_result = agent.run(
            selected_goal,
            brief,
            require_research=_goal_needs_research(selected_goal),
        )

    # Check for compact handoff if max_iterations was hit after changes
    sha = current_sha(workspace)
    has_work = _has_project_work(workspace, sha_before, sha)
    if agent_result.stopped_reason == "max_iterations" and has_work:
        log_progress("Iteration limit reached; continuing once from a compact handoff.")
        work_diff = collect_work_diff(workspace, sha_before, goal=selected_goal)
        handoff_prompt = compact_handoff(
            goal=selected_goal,
            summary=agent_result.summary,
            diff=work_diff,
            last_tool_error=agent_result.last_error,
            stopped_reason=agent_result.stopped_reason,
        )
        agent_result_2 = agent.run(
            handoff_prompt,
            _brief(
                plan,
                named_files,
                collect_context(workspace.root, project, allow_publish=allow_create_pr),
            ),
            require_change=True,
        )
        agent_result = agent_result_2

    question = _user_question(agent_result)
    if question:
        return _wait_for_answer(
            workspace,
            selected_goal,
            question,
            plan_text=plan_text,
            criteria=criteria,
            evidence=evidence,
        )

    code_after_agent = workspace_fingerprint(workspace.root)
    code_changed = code_after_agent != code_before_agent
    tests_after = _tests_after_edits(
        workspace,
        project,
        settings,
        tests_before,
        code_changed=code_changed,
    )
    tests_after = _ensure_test_command(
        workspace,
        project,
        settings,
        agent,
        tests_after,
        allow_create_pr=allow_create_pr,
    )
    tests_after = _repair_failing_tests(
        workspace,
        project,
        settings,
        agent,
        tests_after,
        allow_create_pr=allow_create_pr,
    )

    tests_passed = _tests_passed_value(tests_after)
    if settings.require_tests and _tests_block_commit(tests_after):
        log_progress("Tests failed after repairs.")
        result = CycleResult(
            status="failed",
            goal=selected_goal,
            summary=_failure_notes("tests failed; commit skipped", agent_result.summary),
            tests_passed=False,
            committed=False,
            published=False,
            commit_sha=None,
            reason="tests failed; commit skipped",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        _write_last_failure(workspace.root, selected_goal, result)
        return result

    log_progress("Checking for changes...")
    sha = current_sha(workspace)
    has_work = _has_project_work(workspace, sha_before, sha)
    if not has_work:
        empty = _handle_empty_diff(
            workspace,
            project,
            settings,
            llm,
            agent,
            selected_goal,
            agent_result.summary,
            tests_passed,
            sha,
            sha_before=sha_before,
            mark_checkbox=mark_checkbox,
            allow_create_pr=allow_create_pr,
        )
        if empty.result is not None:
            return empty.result
        agent_result.summary = empty.summary
        tests_passed = empty.tests_passed
        sha = current_sha(workspace)

    review = _ensure_goal_met(
        workspace,
        settings,
        project,
        llm,
        agent,
        selected_goal,
        agent_result.summary,
        sha_before,
        tests_passed,
        allow_create_pr,
        stopped_reason=agent_result.stopped_reason,
        plan=plan,
    )
    tests_passed = review["tests_passed"]
    agent_result_summary = review["summary"]
    evidence.append(
        role_evidence(
            "qa",
            "QA engineer",
            "review",
            "pass" if review["met"] else "send_back",
            str(review.get("reason") or ""),
        )
    )
    if not review["met"] and review.get("asked"):
        return _wait_for_answer(
            workspace,
            selected_goal,
            str(review.get("reason") or "What should change?"),
            plan_text=plan_text,
            criteria=list(criteria),
            evidence=list(evidence),
            tests_passed=tests_passed,
        )
    if not review["met"]:
        log_progress(f"Goal not met: {review['reason']}")
        stopped_reason = agent_result.stopped_reason
        if stopped_reason == "max_iterations":
            reason_info = "iteration limit"
        else:
            reason_info = review.get("reason", "goal not met")
        reason = (
            "tests failed; commit skipped"
            if review.get("tests_failed")
            else f"goal not met; PR skipped: {reason_info}"
        )
        result = CycleResult(
            status="failed",
            goal=selected_goal,
            summary=_failure_notes(reason, agent_result_summary),
            tests_passed=tests_passed,
            committed=False,
            published=False,
            commit_sha=current_sha(workspace),
            reason=reason,
            stage="review",
            plan=plan_text,
            criteria=list(criteria),
            evidence=list(evidence),
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        _write_last_failure(workspace.root, selected_goal, result)
        return result
    agent_result.summary = agent_result_summary

    code_changed = workspace_fingerprint(workspace.root) != code_before_agent
    if not code_changed:
        log_progress("No code changes; skipping lint.")
        lint_after = None
    else:
        covered_lint = current_lint_result(workspace, project.lint_command)
        if covered_lint is not None and covered_lint.ok:
            log_progress("Lint already covers this code; not running it again.")
            lint_after = covered_lint
        else:
            log_progress("Running linter before publish...")
            lint_after = _ensure_lint(
                workspace,
                project,
                settings,
                agent,
                allow_create_pr=allow_create_pr,
                sha_before=sha_before,
            )
    if lint_after is not None and not lint_after.ok:
        lint_output = (lint_after.output or "").strip()
        log_progress("Lint failed after repairs.")
        result = CycleResult(
            status="failed",
            goal=selected_goal,
            summary=_failure_notes(
                _lint_failure_reason(lint_output),
                _with_lint_output(agent_result.summary, lint_output),
            ),
            tests_passed=tests_passed,
            committed=False,
            published=False,
            commit_sha=current_sha(workspace),
            reason=_lint_failure_reason(lint_output),
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        _write_last_failure(workspace.root, selected_goal, result)
        return result

    role_failure = _apply_optional_reviews(
        workspace,
        project,
        settings,
        llm,
        agent,
        selected_goal,
        agent_result,
        sha_before,
        tests_passed,
        evidence,
        plan_text=plan_text,
        criteria=criteria,
    )
    if role_failure is not None:
        return role_failure

    if approval_choice == "ask":
        log_progress("Reviews passed. Waiting for you to approve the commit.")
        return CycleResult(
            status="waiting",
            goal=selected_goal,
            summary=agent_result.summary,
            tests_passed=tests_passed,
            committed=False,
            published=False,
            commit_sha=current_sha(workspace),
            reason="waiting for approval",
            stage="approval",
            plan=plan_text,
            criteria=list(criteria),
            evidence=list(evidence),
        )

    log_progress("Goal confirmed; checking for publishable changes...")
    committed = False
    published = False
    pr_url = None
    sha = current_sha(workspace)
    if sha_before and sha and sha != sha_before:
        committed = True

    screenshot_files = _pr_screenshot_files(workspace) if allow_create_pr else []
    will_commit = settings.auto_commit and has_changes(workspace)
    if allow_create_pr and (committed or will_commit):
        log_progress("Moving work onto a pull-request branch...")
        branched = ensure_pr_branch(workspace, selected_goal, sha_before, base=pr_base)
        if not branched.ok:
            log_progress(f"Could not create PR branch: {branched.output}")
            result = CycleResult(
                status="failed",
                goal=selected_goal,
                summary=_failure_notes(
                    f"could not leave main: {branched.output}",
                    agent_result.summary,
                ),
                tests_passed=tests_passed,
                committed=False,
                published=False,
                commit_sha=sha,
                reason=f"could not leave main: {branched.output}",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return result
        log_progress(f"On branch {branched.output}")

    if will_commit:
        log_progress("Committing changes...")
        message = _commit_message(selected_goal, agent_result.summary)
        commit = commit_changes(
            workspace,
            message,
            env=_git_env(settings),
            store_root=store_root,
            database_url=settings.database_url,
        )
        if not commit.ok:
            log_progress("Commit failed.")
            result = CycleResult(
                status="failed",
                goal=selected_goal,
                summary=_failure_notes(f"commit failed: {commit.output}", agent_result.summary),
                tests_passed=tests_passed,
                committed=False,
                published=False,
                commit_sha=None,
                reason=f"commit failed: {commit.output}",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return result
        committed = True
        sha = current_sha(workspace)
        log_progress(f"Committed SHA: {sha}")

    github_token = None
    if allow_create_pr:
        github_token = _github_token(store_root, settings.database_url)
        published, pr_url, failed, committed = _publish_pull_request(
            workspace,
            project,
            selected_goal,
            agent_result.summary,
            tests_passed=tests_passed,
            sha=sha,
            committed=committed,
            screenshot_files=screenshot_files,
            token=github_token,
            settings=settings,
            store_root=store_root,
            pr_base=pr_base,
            rewind_sha=sha_before,
        )
        sha = current_sha(workspace) or sha
        if failed is not None:
            return failed

    if committed and mark_checkbox:
        log_progress("Marking goal as done...")
        mark_goal_done(workspace.root, project.goals_file, selected_goal)

    log_progress("Generating cycle result...")
    merged = False
    if continuous and pr_url and tests_passed is not False:
        log_progress(f"Merging pull request {pr_url}...")
        merged_pr = merge_pull_request(workspace, pr_url, token=github_token)
        if merged_pr.ok:
            merged = True
            log_progress(merged_pr.output or f"merged {pr_url}")
            base = default_base_branch(workspace, project.publish_branch)
            returned = checkout_base_branch(
                workspace, base, project.publish_remote, token=github_token
            )
            log_progress(returned.output)
        else:
            log_progress(f"Could not merge pull request: {merged_pr.output}")
    result = CycleResult(
        status="success",
        goal=selected_goal,
        summary=agent_result.summary,
        tests_passed=tests_passed,
        committed=committed,
        published=published,
        commit_sha=sha,
        reason=agent_result.stopped_reason,
        pr_url=pr_url,
        merged=merged,
        stage="done",
        plan=plan_text,
        criteria=list(criteria),
        evidence=list(evidence),
    )
    _write_run_log(workspace.root, result)
    _append_to_history(workspace.root, result)
    _delete_last_failure(workspace.root)
    return result


def _user_question(result: AgentResult) -> str | None:
    if result.stopped_reason == "ask":
        question = (result.question or result.summary or "").strip() or "What should I do next?"
    elif result.stopped_reason == "oscillating":
        question = (
            "The builder is stuck repeating the same edit. "
            "What should it change, or what should it leave alone?"
        )
    else:
        return None
    error = (result.last_error or "").strip()
    if error:
        question = f"{question}\n\nLast error: {error}"
    return question[:2000]


def _wait_for_answer(
    workspace: Workspace,
    goal: str,
    question: str,
    *,
    plan_text: str,
    criteria: list[str],
    evidence: list[dict],
    tests_passed: bool | None = None,
) -> CycleResult:
    log_progress(question)
    result = CycleResult(
        status="waiting",
        goal=goal,
        summary=question,
        tests_passed=tests_passed,
        committed=False,
        published=False,
        commit_sha=current_sha(workspace),
        reason=question,
        stage="ask",
        plan=plan_text,
        criteria=list(criteria),
        evidence=list(evidence),
    )
    return result


def _plan_record(plan: GoalPlan | None, goal: str) -> tuple[str, list[str]]:
    text = format_plan(plan) if plan is not None else ""
    checks = [step.check for step in plan.steps if step.check] if plan is not None else []
    headline = goal_headline(goal) if goal else ""
    if not checks and headline:
        checks = [headline]
    if not text and headline:
        text = f"Implement: {headline}"
    return text, checks[:6]


def _apply_optional_reviews(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    llm: LLMClient,
    agent: CodingAgent,
    goal: str,
    agent_result: AgentResult,
    sha_before: str | None,
    tests_passed: bool | None,
    evidence: list[dict],
    *,
    plan_text: str,
    criteria: list[str],
) -> CycleResult | None:
    """Run enabled review roles. A block or a second send-back stops the commit."""
    roles = optional_review_roles(workspace.root)
    if not roles:
        return None
    diff = collect_work_diff(workspace, sha_before, goal=goal)
    for role in roles:
        log_progress(f"{role.title} is reviewing the change...")
        verdict = review_with_role(
            llm,
            role,
            goal=goal,
            diff=diff,
            summary=agent_result.summary or "",
            tests_passed=tests_passed,
        )
        evidence.append(
            role_evidence(role.name, role.title, role.job, verdict.verdict, verdict.summary)
        )
        record_event(
            kind="role",
            role=role.name,
            verdict=verdict.verdict,
            parsed=verdict.parsed,
            message=f"{role.title}: {verdict.summary}",
        )
        if not verdict.parsed or verdict.verdict == "pass":
            continue
        if verdict.verdict == "block":
            return _role_stopped(
                workspace,
                goal,
                agent_result.summary or "",
                tests_passed,
                role.title,
                verdict.verdict,
                verdict.summary,
                plan_text=plan_text,
                criteria=criteria,
                evidence=evidence,
            )
        log_progress(f"{role.title} sent the change back.")
        follow = agent.run(
            (
                f"The {role.title} sent this change back: {verdict.summary}\n"
                "Fix that point. Do not start a different task."
            ),
            collect_context(workspace.root, project, allow_publish=False),
            require_change=True,
        )
        if follow.stopped_reason == "ask":
            return _wait_for_answer(
                workspace,
                goal,
                _user_question(follow) or follow.summary or "What should change?",
                plan_text=plan_text,
                criteria=criteria,
                evidence=evidence,
                tests_passed=tests_passed,
            )
        if follow.summary:
            agent_result.summary = follow.summary
        diff = collect_work_diff(workspace, sha_before, goal=goal)
        again = review_with_role(
            llm,
            role,
            goal=goal,
            diff=diff,
            summary=agent_result.summary or "",
            tests_passed=tests_passed,
        )
        evidence.append(
            role_evidence(role.name, role.title, role.job, again.verdict, again.summary)
        )
        record_event(
            kind="role",
            role=role.name,
            verdict=again.verdict,
            parsed=again.parsed,
            message=f"{role.title}: {again.summary}",
        )
        if again.parsed and again.verdict != "pass":
            return _role_stopped(
                workspace,
                goal,
                agent_result.summary or "",
                tests_passed,
                role.title,
                again.verdict,
                again.summary,
                plan_text=plan_text,
                criteria=criteria,
                evidence=evidence,
            )
        tests_after = _maybe_test(workspace, project, settings, phase="after")
        if settings.require_tests and _tests_block_commit(tests_after):
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes("tests failed; commit skipped", agent_result.summary),
                tests_passed=False,
                committed=False,
                published=False,
                commit_sha=current_sha(workspace),
                reason="tests failed; commit skipped",
                stage="review",
                plan=plan_text,
                criteria=list(criteria),
                evidence=list(evidence),
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            _write_last_failure(workspace.root, goal, result)
            return result
    return None


def _role_stopped(
    workspace: Workspace,
    goal: str,
    summary: str,
    tests_passed: bool | None,
    title: str,
    verdict: str,
    detail: str,
    *,
    plan_text: str,
    criteria: list[str],
    evidence: list[dict],
) -> CycleResult:
    reason = f"{title} {verdict.replace('_', ' ')}: {detail}"
    log_progress(reason)
    result = CycleResult(
        status="failed",
        goal=goal,
        summary=_failure_notes(reason, summary),
        tests_passed=tests_passed,
        committed=False,
        published=False,
        commit_sha=current_sha(workspace),
        reason=reason,
        stage="review",
        plan=plan_text,
        criteria=list(criteria),
        evidence=list(evidence),
    )
    _write_run_log(workspace.root, result)
    _append_to_history(workspace.root, result)
    _write_last_failure(workspace.root, goal, result)
    return result


def _publish_held(
    workspace: Workspace,
    settings: Settings,
    project: ProjectConfig,
    goal: str,
    held_summary: str,
    *,
    allow_create_pr: bool,
    mark_checkbox: bool,
    store_root: Path | None = None,
    pr_base: str | None = None,
) -> CycleResult:
    """Commit and publish work a person already approved. Does not run the builder."""
    selected = goal.strip() or "Approved change"
    summary = (held_summary or selected).strip()
    tests_after = _maybe_test(workspace, project, settings, phase="after")
    tests_passed = _tests_passed_value(tests_after)
    if settings.require_tests and _tests_block_commit(tests_after):
        result = CycleResult(
            status="failed",
            goal=selected,
            summary=_failure_notes("tests failed; commit skipped", summary),
            tests_passed=False,
            committed=False,
            published=False,
            commit_sha=current_sha(workspace),
            reason="tests failed; commit skipped",
            stage="approval",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return result
    sha = current_sha(workspace)
    committed = False
    if settings.auto_commit and has_changes(workspace):
        if allow_create_pr:
            log_progress("Moving work onto a pull-request branch...")
            branched = ensure_pr_branch(workspace, selected, sha, base=pr_base)
            if not branched.ok:
                result = CycleResult(
                    status="failed",
                    goal=selected,
                    summary=_failure_notes(f"could not leave main: {branched.output}", summary),
                    tests_passed=tests_passed,
                    committed=False,
                    published=False,
                    commit_sha=sha,
                    reason=f"could not leave main: {branched.output}",
                    stage="approval",
                )
                _write_run_log(workspace.root, result)
                _append_to_history(workspace.root, result)
                return result
            log_progress(f"On branch {branched.output}")
        log_progress("Committing the approved change...")
        commit = commit_changes(
            workspace,
            _commit_message(selected, summary),
            env=_git_env(settings),
            store_root=store_root,
            database_url=settings.database_url,
        )
        if not commit.ok:
            result = CycleResult(
                status="failed",
                goal=selected,
                summary=_failure_notes(f"commit failed: {commit.output}", summary),
                tests_passed=tests_passed,
                committed=False,
                published=False,
                commit_sha=None,
                reason=f"commit failed: {commit.output}",
                stage="approval",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return result
        committed = True
        sha = current_sha(workspace)
        log_progress(f"Committed SHA: {sha}")
    published = False
    pr_url = None
    if allow_create_pr:
        github_token = _github_token(store_root, settings.database_url)
        published, pr_url, failed, committed = _publish_pull_request(
            workspace,
            project,
            selected,
            summary,
            tests_passed=tests_passed,
            sha=sha,
            committed=committed,
            screenshot_files=[],
            token=github_token,
            settings=settings,
            store_root=store_root,
            pr_base=pr_base,
        )
        sha = current_sha(workspace) or sha
        if failed is not None:
            return failed
    if committed and mark_checkbox:
        mark_goal_done(workspace.root, project.goals_file, selected)
    result = CycleResult(
        status="success",
        goal=selected,
        summary=summary,
        tests_passed=tests_passed,
        committed=committed,
        published=published,
        commit_sha=sha,
        reason="approved",
        pr_url=pr_url,
        stage="done",
        evidence=[role_evidence("you", "You", "approve", "pass", "Approved the commit.")],
    )
    _write_run_log(workspace.root, result)
    _append_to_history(workspace.root, result)
    _delete_last_failure(workspace.root)
    return result


def _scoped_skills(store_root: Path | None, settings: Settings, workspace: Path) -> list:
    if store_root is None:
        return []
    from agent_lbw.rules_http import open_kiwi
    from agent_lbw.runtime.db import database
    from agent_lbw.runtime.skills import enabled_skill_names
    from agent_lbw.runtime.store.install import require_account
    from agent_lbw.runtime.store.rules import project_for_workspace
    from agent_lbw.runtime.store.skill_repos import skills_for_project, visible_repos
    from agent_lbw.tools.kiwi import KiwiError, normalize_kiwifs_url

    url = normalize_kiwifs_url(settings.kiwifs_url)
    if not url:
        return []
    try:
        with database(store_root, settings.database_url) as db:
            account = require_account(db)
            project = project_for_workspace(db, account["org"]["id"], workspace)
            if project is None or not visible_repos(db, account["org"]["id"], project["id"]):
                return []
            return skills_for_project(
                db,
                open_kiwi(url, settings.kiwifs_api_key),
                tenant_id=account["org"]["id"],
                project_id=project["id"],
                enabled=set(enabled_skill_names(workspace)),
            )
    except (KiwiError, OSError, ValueError):
        return []


def _assigned_skills(store_root: Path | None, database_url: str, workspace: Path) -> list[str]:
    if store_root is None:
        return []
    from agent_lbw.org_http import assigned_skill_names

    return assigned_skill_names(store_root, workspace, database_url)


def _github_token(store_root: Path | None, database_url: str) -> str | None:
    if store_root is None:
        return None
    from agent_lbw.runtime.store.github import GitHubAppError, current_token

    try:
        return current_token(store_root, database_url)
    except GitHubAppError as exc:
        log_progress(f"GitHub App token was not loaded: {exc}")
        return None
    except Exception:
        log_progress("GitHub App token was not loaded")
        return None


def _publish_pull_request(
    workspace: Workspace,
    project: ProjectConfig,
    goal: str,
    summary: str,
    *,
    tests_passed: bool | None,
    sha: str | None,
    committed: bool,
    screenshot_files: list[Path],
    token: str | None = None,
    settings: Settings | None = None,
    store_root: Path | None = None,
    pr_base: str | None = None,
    rewind_sha: str | None = None,
) -> tuple[bool, str | None, CycleResult | None, bool]:
    """Commit project changes, push that branch, then open or update the pull request.

    The pull request is not opened until the push succeeds. ``.lbw/`` and
    ``history.json`` stay out of the commit. A protected branch is not pushed.
    """
    branch = current_branch(workspace)
    if is_protected_branch(branch):
        if committed:
            log_progress(f"Refusing to publish {branch} directly.")
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes(f"refusing to push commits on {branch}", summary),
                tests_passed=tests_passed,
                committed=committed,
                published=False,
                commit_sha=sha,
                reason=f"refusing to push commits on {branch}",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return False, None, result, committed
        return False, None, None, committed
    if settings is not None and settings.auto_commit and has_changes(workspace):
        log_progress("Committing changes before the push...")
        commit = commit_changes(
            workspace,
            _commit_message(goal, summary),
            env=_git_env(settings),
            store_root=store_root,
            database_url=settings.database_url,
        )
        if not commit.ok:
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes(f"commit failed: {commit.output}", summary),
                tests_passed=tests_passed,
                committed=False,
                published=False,
                commit_sha=sha,
                reason=f"commit failed: {commit.output}",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return False, None, result, False
        committed = True
        sha = current_sha(workspace)
        log_progress(f"Committed SHA: {sha}")
    existing = existing_pull_request(workspace, token=token)
    base = intended_pr_base(workspace, pr_base, project.publish_branch)
    branch = current_branch(workspace)
    if branch and branch == base and committed:
        log_progress(f"Moving commits off {base} before opening the pull request...")
        moved = move_commits_off_base(workspace, goal, base, rewind_sha)
        if not moved.ok:
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes(moved.output, summary),
                tests_passed=tests_passed,
                committed=committed,
                published=False,
                commit_sha=sha,
                reason=moved.output,
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return False, None, result, committed
        branch = current_branch(workspace)
        log_progress(f"On branch {branch}")
    if not committed and commits_ahead_of_base(workspace, base) <= 0:
        if existing:
            log_progress(f"Branch already has pull request {existing}")
            record_event(kind="pr", url=existing, message="existing pull request")
            return True, existing, None, committed
        return False, None, None, committed
    folded = fold_unpublished_commits(
        workspace,
        base,
        _commit_message(goal, summary),
        env=_git_env(settings) if settings is not None else None,
        store_root=store_root,
        database_url=settings.database_url if settings is not None else "",
    )
    if not folded.ok:
        result = CycleResult(
            status="failed",
            goal=goal,
            summary=_failure_notes(folded.output, summary),
            tests_passed=tests_passed,
            committed=committed,
            published=False,
            commit_sha=sha,
            reason=folded.output,
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return False, None, result, committed
    if folded.output.startswith("folded"):
        log_progress(folded.output)
        sha = current_sha(workspace) or sha
        committed = True
        branch = current_branch(workspace)
    log_progress(f"Pushing branch {branch}...")
    pushed = push_changes(workspace, project.publish_remote, branch, token)
    if not pushed.ok:
        log_progress(f"Push failed: {pushed.output}")
        result = CycleResult(
            status="failed",
            goal=goal,
            summary=_failure_notes(f"push failed: {pushed.output}", summary),
            tests_passed=tests_passed,
            committed=committed,
            published=False,
            commit_sha=sha,
            reason=f"push failed: {pushed.output}",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return False, None, result, committed
    if existing:
        log_progress(f"Updating pull request {existing}")
        updated = update_pull_request(
            workspace,
            _commit_message(goal, summary),
            _pr_body(
                goal,
                summary,
                tests_passed=tests_passed,
                commit_sha=sha,
                branch=branch or "",
                screenshots=[path.name for path in screenshot_files if is_tracked(workspace, path)],
                image_base=_pr_image_base(
                    workspace,
                    sha,
                    project.publish_remote,
                ),
            ),
            token=token,
        )
        if updated.ok:
            log_progress(f"Updated pull request {existing}")
        else:
            log_progress(
                f"Pushed the branch; pull request text was not refreshed: {updated.output}"
            )
        record_event(kind="pr", url=existing, message=updated.output or "existing pull request")
        return True, existing, None, committed
    log_progress("Opening pull request...")
    pr = create_pull_request(
        workspace,
        _commit_message(goal, summary),
        _pr_body(
            goal,
            summary,
            tests_passed=tests_passed,
            commit_sha=sha,
            branch=branch or "",
            screenshots=[path.name for path in screenshot_files if is_tracked(workspace, path)],
            image_base=_pr_image_base(
                workspace,
                sha,
                project.publish_remote,
            ),
        ),
        base=base,
        token=token,
    )
    pr_url = extract_pr_url(pr.output)
    if not pr.ok:
        log_progress(f"PR creation failed: {pr.output}")
        result = CycleResult(
            status="failed",
            goal=goal,
            summary=_failure_notes(f"PR failed: {pr.output}", summary),
            tests_passed=tests_passed,
            committed=committed,
            published=False,
            commit_sha=sha,
            reason=f"PR failed: {pr.output}",
        )
        _write_run_log(workspace.root, result)
        _append_to_history(workspace.root, result)
        return False, None, result, committed
    log_progress(pr.output)
    if pr_url:
        record_event(kind="pr", url=pr_url, message=pr.output)
    return True, pr_url, None, committed


def _handle_empty_diff(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    llm: LLMClient,
    agent: CodingAgent,
    goal: str,
    summary: str,
    tests_passed: bool | None,
    sha: str | None,
    *,
    sha_before: str | None,
    mark_checkbox: bool,
    allow_create_pr: bool,
) -> _EmptyDiffOutcome:
    log_progress("No project files changed; checking whether the goal is already met...")
    upstream = upstream_state(workspace, project.publish_remote)
    log_progress(f"Upstream: {upstream.detail}")
    evidence = collect_current_evidence(workspace, goal)
    verdict = _review_goal(
        workspace,
        project,
        llm,
        goal,
        evidence,
        summary,
        tests_passed,
        existing=True,
        upstream=upstream.detail,
    )
    log_progress(f"Goal review: met={verdict.met} ({review_reason(verdict)})")
    if verdict.met or is_open_pr_goal(goal):
        published = False
        pr_url = None
        published_commit = False
        if allow_create_pr:
            published, pr_url, failed, published_commit = _publish_pull_request(
                workspace,
                project,
                goal,
                summary,
                tests_passed=tests_passed,
                sha=sha,
                committed=False,
                screenshot_files=[],
                settings=settings,
            )
            if published_commit:
                sha = current_sha(workspace) or sha
            if failed is not None:
                return _EmptyDiffOutcome(result=failed, summary=summary, tests_passed=tests_passed)
        if verdict.met or published:
            log_progress(f"No work needed: {review_reason(verdict)}")
            if mark_checkbox:
                log_progress("Marking goal as done...")
                mark_goal_done(workspace.root, project.goals_file, goal)
            if published and pr_url:
                reason = f"opened pull request without new files: {pr_url}"
                cycle_summary = (
                    f"Opened pull request {pr_url} from the current branch "
                    "without new file changes."
                )
            else:
                reason = f"no changes needed: {review_reason(verdict)} {upstream.detail}"
                cycle_summary = _no_work_summary(summary, verdict, upstream.detail)
            result = CycleResult(
                status="success",
                goal=goal,
                summary=cycle_summary,
                tests_passed=tests_passed,
                committed=published_commit,
                published=published,
                commit_sha=sha,
                reason=reason,
                pr_url=pr_url,
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return _EmptyDiffOutcome(result=result, summary=summary, tests_passed=tests_passed)

    context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    for attempt in range(project.max_repair_attempts):
        log_progress(
            "Agent finished without edits; sending it back to implement "
            f"({attempt + 1}/{project.max_repair_attempts})"
        )
        before_retry = workspace_fingerprint(workspace.root)
        follow = agent.run(
            _empty_diff_retry_prompt(goal, review_reason(verdict)),
            context,
            require_change=True,
        )
        if follow.summary:
            summary = follow.summary
        if follow.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            break
        if workspace_fingerprint(workspace.root) == before_retry:
            log_progress("No code changes; not running tests again.")
            continue
        tests_after = _maybe_test(workspace, project, settings, phase="retry")
        tests_after = _ensure_test_command(
            workspace,
            project,
            settings,
            agent,
            tests_after,
            allow_create_pr=allow_create_pr,
        )
        tests_after = _repair_failing_tests(
            workspace,
            project,
            settings,
            agent,
            tests_after,
            allow_create_pr=allow_create_pr,
        )
        tests_passed = _tests_passed_value(tests_after)
        if settings.require_tests and _tests_block_commit(tests_after):
            result = CycleResult(
                status="failed",
                goal=goal,
                summary=_failure_notes(
                    "tests failed after empty-diff retry; commit skipped",
                    summary,
                ),
                tests_passed=False,
                committed=False,
                published=False,
                commit_sha=current_sha(workspace),
                reason="tests failed after empty-diff retry; commit skipped",
            )
            _write_run_log(workspace.root, result)
            _append_to_history(workspace.root, result)
            return _EmptyDiffOutcome(result=result, summary=summary, tests_passed=False)
        sha_now = current_sha(workspace)
        if _has_project_work(workspace, sha_before, sha_now):
            log_progress("Agent produced file changes on retry.")
            return _EmptyDiffOutcome(result=None, summary=summary, tests_passed=tests_passed)

    log_progress("Goal is not already met; no files were changed.")
    result = CycleResult(
        status="skipped",
        goal=goal,
        summary=_no_work_summary(summary, verdict, upstream.detail),
        tests_passed=tests_passed,
        committed=False,
        published=False,
        commit_sha=sha,
        reason=(
            "no files changed and the goal is not already met: "
            f"{review_reason(verdict)} {upstream.detail}"
        ),
    )
    _write_run_log(workspace.root, result)
    _append_to_history(workspace.root, result)
    return _EmptyDiffOutcome(result=result, summary=summary, tests_passed=tests_passed)


def _empty_diff_retry_prompt(goal: str, reason: str) -> str:
    return (
        "You inspected the repo but did not change any files that implement the goal. "
        "The goal is not already done. Do not summarize. Do not only read more files "
        "or run tests. Do not write a placeholder, status note, or unrelated "
        "verification test. Do not ship a stub, mock, or unused form field. "
        "Call str_replace on the existing files (or write_file "
        "for a new/small file) and implement this exact goal. Do not rewrite a "
        "large file with write_file. If existing tests assert old markup, APIs, "
        "or layout that this goal replaces, update those tests in the same change.\n\n"
        f"Goal:\n{goal.strip()}\n\n"
        f"Why it is not done:\n{reason.strip()}"
    )


def _no_work_summary(agent_summary: str, verdict: GoalReview, upstream_detail: str) -> str:
    if verdict.met:
        lines = [
            f"No work to do: {verdict.reason}",
            upstream_detail,
        ]
    else:
        lines = [
            "No files changed this cycle, and the current tree does not already "
            f"meet the goal: {review_reason(verdict)}",
            upstream_detail,
        ]
    notes = (agent_summary or "").strip()
    skip_notes = {
        "stopped after reaching the iteration limit.",
        "agent finished without a summary.",
    }
    if notes and notes.lower() not in skip_notes:
        lines.extend(["", "Agent notes:", notes])
    return "\n".join(lines)


def _review_goal(
    workspace: Workspace,
    project: ProjectConfig,
    llm: LLMClient,
    goal: str,
    diff: str,
    summary: str,
    tests_passed: bool | None,
    *,
    existing: bool = False,
    upstream: str | None = None,
    stopped_reason: str | None = None,
    plan: GoalPlan | None = None,
) -> GoalReview:
    ui_text = ""
    ui_evidence = None
    if ui_review_needed(goal, diff, workspace.root):
        log_progress("Reviewing rendered UI...")
        try:
            ui_evidence = collect_ui_evidence(workspace, project, goal)
        except Exception as exc:  # noqa: BLE001 - UI capture must not abort the cycle
            ui_evidence = UiEvidence(ok=False, notes=f"could not render UI: {exc}")
        if ui_evidence is not None:
            ui_text = format_ui_evidence(ui_evidence)
            log_progress(ui_evidence.summary)
    verdict = review_goal(
        llm,
        goal,
        diff=diff,
        summary=summary,
        tests_passed=tests_passed,
        existing=existing,
        upstream=upstream,
        ui_evidence=ui_text or None,
        plan=format_plan(plan) or None,
    )
    blocking = []
    smashed = []
    gaps: list[str] = []
    if ui_evidence is not None:
        blocking = ui_evidence.blocking_errors
        smashed = ui_evidence.smashed
        gaps = list(ui_evidence.visual_gaps)
    ui_errors = tuple([*blocking, *gaps][:12])
    missing_preview = (
        ui_evidence is not None
        and not ui_evidence.ok
        and "no preview server" in (ui_evidence.notes or "").lower()
    )
    if verdict.met and missing_preview:
        overridden = GoalReview(
            False,
            "No preview server, so the visual change was not shown. "
            "Set preview_command in .lbw/config.yaml to a command that listens on {port} "
            "and serves the changed screen, then call review_ui.",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    if verdict.met and blocking:
        first = blocking[0]
        engine = bool(ui_evidence and ui_evidence.engine_errors)
        js = bool(ui_evidence and (ui_evidence.page_errors or ui_evidence.console_errors))
        if engine:
            reason = f"Running the scene still prints an engine error: {first}"
        elif js:
            reason = f"Rendered UI has JavaScript errors: {first}"
        else:
            reason = f"Rendered UI failed to load a required resource: {first}"
        overridden = GoalReview(False, reason, parsed=True, ui_errors=ui_errors)
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    if verdict.met and gaps:
        overridden = GoalReview(
            False,
            f"The captured scene still draws nothing for: {gaps[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    if verdict.met and smashed:
        overridden = GoalReview(
            False,
            f"Rendered UI controls are unusable: {smashed[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    unverified = unverified_interactive_ui(goal, ui_evidence) if ui_evidence else None
    if verdict.met and unverified:
        overridden = GoalReview(False, unverified, parsed=True, ui_errors=ui_errors)
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    guarded = guard_only_fix(diff, goal)
    ran_clean = bool(
        ui_evidence is not None
        and ui_evidence.ok
        and (ui_evidence.notes or "").startswith("Godot ran")
        and not blocking
        and not gaps
    )
    if verdict.met and guarded and not existing and not ran_clean:
        overridden = GoalReview(
            False,
            f"The diff only hides the failure with a null check or log. {guarded[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    markers = half_baked_diff_markers(diff)
    if verdict.met and markers and not existing:
        overridden = GoalReview(
            False,
            f"diff still has unfinished work: {markers[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    unwired = unwired_ui_markers(diff)
    if verdict.met and unwired and not existing:
        overridden = GoalReview(
            False,
            f"diff adds a control or API that is not wired: {unwired[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    missing = missing_added_resources(diff, workspace.root)
    if verdict.met and missing and not existing:
        overridden = GoalReview(
            False,
            f"diff references a file that is not in the repo: res://{missing[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    invented = invalid_doc_commands(diff)
    if verdict.met and invented and not existing:
        overridden = GoalReview(
            False,
            f"diff documents a command or mount that cannot work: {invented[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    unused = unused_new_symbols(diff)
    if verdict.met and unused and not existing:
        overridden = GoalReview(
            False,
            f"diff adds a helper that nothing calls: {unused[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    reviewed = blocking_review_findings(diff, goal=goal)
    if verdict.met and reviewed and not existing:
        overridden = GoalReview(
            False,
            f"code review found an error: {reviewed[0]}",
            parsed=True,
            ui_errors=ui_errors,
        )
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    incomplete = incomplete_agent_run(summary, stopped_reason)
    ui_verified = bool(
        ui_evidence is not None
        and ui_evidence.ok
        and not blocking
        and not smashed
        and not ui_evidence.dead_controls
        and (ui_evidence.snapshot or ui_evidence.screenshot)
    )
    if verdict.met and incomplete and not existing and not ui_verified:
        overridden = GoalReview(False, incomplete, parsed=True, ui_errors=ui_errors)
        record_event(kind="review", attempt=1, met=False, parsed=True, reason=overridden.reason)
        return overridden
    if ui_errors and verdict.ui_errors != ui_errors:
        return GoalReview(
            met=verdict.met,
            reason=verdict.reason,
            raw=verdict.raw,
            parsed=verdict.parsed,
            ui_errors=ui_errors,
        )
    return verdict


def _ensure_goal_met(
    workspace: Workspace,
    settings: Settings,
    project: ProjectConfig,
    llm: LLMClient,
    agent: CodingAgent,
    goal: str,
    summary: str,
    sha_before: str | None,
    tests_passed: bool | None,
    allow_create_pr: bool,
    *,
    stopped_reason: str | None = None,
    plan: GoalPlan | None = None,
) -> dict[str, object]:
    base_context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    work_diff = collect_work_diff(workspace, sha_before, goal=goal)
    verdict = _review_goal(
        workspace,
        project,
        llm,
        goal,
        work_diff,
        summary,
        tests_passed,
        stopped_reason=stopped_reason,
        plan=plan,
    )
    log_progress(f"Goal review: met={verdict.met} ({review_reason(verdict)})")
    if not verdict.met and stopped_reason == "oscillating":
        log_progress("The agent kept adding and removing the same small edit. Stopping.")
        return {
            "met": False,
            "reason": "agent kept undoing the same small edit",
            "summary": summary,
            "tests_passed": tests_passed,
            "tests_failed": False,
        }
    attempts = 0
    stalls = 0
    extra_granted = False
    replanned = False
    last_diff = work_diff
    last_errors: tuple[str, ...] = ()
    previous_reason = review_reason(verdict)
    limit = project.max_repair_attempts
    while not verdict.met and attempts < limit:
        carry_out_plan = False
        if attempts >= 1 and _repeated_miss(previous_reason, review_reason(verdict)):
            log_progress("The same miss came back. Stopping instead of repeating it.")
            break
        previous_reason = review_reason(verdict)
        attempts += 1
        log_progress(f"Goal retry {attempts}/{limit}: {review_reason(verdict)}")
        try:
            project = load_project(workspace.root)
        except ValueError as exc:
            log_progress(str(exc))
        work_diff = collect_work_diff(workspace, sha_before, goal=goal)
        named_files = format_goal_files(workspace.root, goal)
        repeated = bool(verdict.ui_errors) and tuple(verdict.ui_errors) == last_errors
        last_errors = tuple(verdict.ui_errors)
        architect = plan_role(workspace.root)
        if (
            settings.plan_goals
            and architect is not None
            and attempts >= REPLAN_AFTER
            and not replanned
        ):
            replanned = True
            log_progress("Two retries missed the goal; making a new plan from what failed...")
            fresh = plan_goal(
                llm,
                goal,
                context=base_context,
                files=named_files,
                failure=_goal_miss(verdict),
                previous=plan,
                diff=work_diff,
                opinions=architect.body if architect is not None else "",
            )
            if fresh is not None:
                grounded = ground_plan(fresh, goal, workspace.root)
                if grounded is not None:
                    plan = grounded
                    _log_plan(plan)
                    carry_out_plan = True
        context = _brief(plan, named_files, base_context)
        guarded = bool(guard_only_fix(work_diff, goal))
        lookup = _needs_validation_lookup(review_reason(verdict), project) and not guarded
        research = guarded or _needs_cause_research(review_reason(verdict))
        if lookup:
            log_progress("Looking up how to test and preview this stack...")
        if research:
            log_progress("Researching the stated failure before another edit...")
        before_retry = workspace_fingerprint(workspace.root)
        if carry_out_plan and plan is not None and plan.steps:
            log_progress("Carrying out the new plan.")
            follow = _run_plan_steps(
                workspace,
                agent,
                plan,
                goal,
                context,
                sha_before,
                research=research,
            )
        else:
            follow = agent.run(
                _goal_retry_prompt(
                    goal,
                    _goal_miss(verdict),
                    work_diff,
                    ui_errors=verdict.ui_errors,
                    stalled=stalls > 0,
                    lookup=lookup,
                    research=research,
                    repeated=repeated,
                ),
                context,
                require_change=True,
                require_lookup=lookup,
                require_research=research,
                require_docs=research,
            )
        if (
            follow.stopped_reason == "max_iterations"
            and workspace_fingerprint(workspace.root) != before_retry
        ):
            log_progress(
                "Iteration limit reached during goal retry; continuing once from a compact handoff."
            )
            follow = agent.run(
                compact_handoff(
                    goal=goal,
                    summary=follow.summary,
                    diff=collect_work_diff(workspace, sha_before, goal=goal),
                    last_tool_error=follow.last_error,
                    stopped_reason=follow.stopped_reason,
                ),
                context,
                require_change=True,
            )
        if follow.summary:
            summary = follow.summary
        if follow.stopped_reason == "ask":
            return {
                "met": False,
                "reason": _user_question(follow) or "The builder asked a question.",
                "summary": follow.summary,
                "tests_passed": tests_passed,
                "tests_failed": False,
                "asked": True,
            }
        if follow.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            return {
                "met": False,
                "reason": "agent kept undoing the same small edit",
                "summary": summary,
                "tests_passed": tests_passed,
                "tests_failed": False,
                "asked": True,
            }
        if workspace_fingerprint(workspace.root) == before_retry:
            log_progress("No code changes; not running tests or another review.")
            stalls += 1
            if not extra_granted and attempts >= project.max_repair_attempts:
                extra_granted = True
                limit = attempts + GOAL_RETRY_STALL_EXTRA
                log_progress("Retry made no file changes; continuing instead of giving up.")
            continue
        tests_after = _maybe_test(workspace, project, settings, phase="retry")
        tests_after = _ensure_test_command(
            workspace,
            project,
            settings,
            agent,
            tests_after,
            allow_create_pr=allow_create_pr,
        )
        tests_after = _repair_failing_tests(
            workspace,
            project,
            settings,
            agent,
            tests_after,
            allow_create_pr=allow_create_pr,
        )
        tests_passed = _tests_passed_value(tests_after)
        if settings.require_tests and _tests_block_commit(tests_after):
            return {
                "met": False,
                "reason": "tests failed after goal retry",
                "summary": summary,
                "tests_passed": False,
                "tests_failed": True,
            }
        work_diff = collect_work_diff(workspace, sha_before, goal=goal)
        if work_diff == last_diff:
            stalls += 1
            if not extra_granted and attempts >= project.max_repair_attempts:
                extra_granted = True
                limit = attempts + GOAL_RETRY_STALL_EXTRA
                log_progress("Retry made no file changes; continuing instead of giving up.")
        else:
            stalls = 0
        last_diff = work_diff
        verdict = _review_goal(
            workspace,
            project,
            llm,
            goal,
            work_diff,
            summary,
            tests_passed,
            stopped_reason=follow.stopped_reason,
            plan=plan,
        )
        log_progress(f"Goal review: met={verdict.met} ({review_reason(verdict)})")
    return {
        "met": verdict.met,
        "reason": _goal_miss(verdict),
        "summary": summary,
        "tests_passed": tests_passed,
        "tests_failed": False,
    }


GOAL_RETRY_STALL_EXTRA = 2
REPLAN_AFTER = 2


_CHECK_STEP = re.compile(r"\b(run_tests|review_ui)\b", re.IGNORECASE)
_MISS_MARKS = (
    "null check",
    "scene hierarchy",
    "not found",
    "missing node",
    "only hides",
    "does not address",
    "does not actually fix",
)


def _is_check_step(step: PlanStep) -> bool:
    return not step.file and bool(_CHECK_STEP.search(step.do))


def _repo_rel(filename: str) -> str:
    """Normalize a plan path without stripping a leading dot from a hidden directory.

    str.lstrip("./") treats every leading '.' and '/' as trim characters, so
    '.omp-plugin/marketplace.json' became 'omp-plugin/marketplace.json' and no
    longer matched the git diff. The step then looked untouched and was run again.
    """
    rel = (filename or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _diff_touches(diff: str, filename: str) -> bool:
    rel = _repo_rel(filename)
    if not rel:
        return True
    text = diff or ""
    needles = (
        f"b/{rel}",
        f"a/{rel}",
        f"/{rel}",
        f" {rel}",
        f"\t{rel}",
    )
    if any(needle in text for needle in needles):
        return True
    return text.rstrip().endswith(rel)


def _step_prompt(goal: str, plan: GoalPlan, index: int) -> str:
    step = plan.steps[index - 1]
    later = " Do not start a later step." if index < len(plan.steps) else ""
    where = f" Edit {step.file}." if step.file else ""
    check = step.check or "the change is in the file"
    return (
        f"Goal:\n{goal.strip()}\n\n"
        f"Do only step {index} of {len(plan.steps)}.{later}{where}\n"
        f"Step: {step.do}\n"
        f"This step is done only when: {check}\n"
        "A null check, print, or log does not finish this step. "
        "Do not say the whole task is complete."
    )


def _run_plan_steps(
    workspace: Workspace,
    agent: CodingAgent,
    plan: GoalPlan,
    goal: str,
    brief: str,
    sha_before: str | None,
    *,
    research: bool,
) -> AgentResult:
    """Carry out one plan step, then the next, after that step's file has changed."""
    last: AgentResult | None = None
    notes: list[str] = []
    total = len(plan.steps)
    for index, step in enumerate(plan.steps, start=1):
        log_progress(f"Step {index}/{total}: {step.do}")
        result = agent.run(
            _step_prompt(goal, plan, index),
            brief,
            require_change=not _is_check_step(step),
            require_research=research and index == 1,
        )
        if step.file and not _is_check_step(step):
            diff = collect_work_diff(workspace, sha_before, goal=goal)
            if not _diff_touches(diff, step.file):
                log_progress(
                    f"Step {index} did not change {step.file}; editing that file once more."
                )
                result = agent.run(
                    _step_prompt(goal, plan, index)
                    + f"\nYou have not edited {step.file}. Edit that file now.",
                    brief,
                    require_change=True,
                )
        last = result
        if result.stopped_reason == "ask":
            log_progress(result.question or "The builder asked a question.")
            break
        if result.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            break
        if result.summary:
            notes.append(result.summary)
    assert last is not None
    return AgentResult(
        summary="\n".join(notes) or last.summary,
        iterations=last.iterations,
        tool_calls=last.tool_calls,
        stopped_reason=last.stopped_reason,
        last_error=last.last_error,
        question=last.question,
    )


def _goal_miss(verdict: GoalReview) -> str:
    """The sent-back text: the named gap, each miss, and what the check still sees."""
    if verdict.met:
        return review_reason(verdict)
    return explain_miss(verdict)


def _repeated_miss(previous: str, current: str) -> bool:
    left = (previous or "").lower()
    right = (current or "").lower()
    return any(mark in left and mark in right for mark in _MISS_MARKS)


def _log_plan(plan: GoalPlan | None) -> None:
    if plan is None:
        return
    total = len(plan.steps)
    for index, step in enumerate(plan.steps, start=1):
        where = f" [{step.file}]" if step.file else ""
        log_progress(f"Plan {index}/{total}: {step.do}{where}")


def _brief(plan: GoalPlan | None, named_files: str, context: str) -> str:
    return "\n\n".join(part for part in (format_plan(plan), named_files, context) if part)


_RESEARCH_MARKS = (
    "previous attempt did not fix",
    "error handling",
    "logging",
    "null check",
    "does not actually fix",
    "without actually fixing",
    "only hides the failure",
    "only guards the crash",
    "not found",
    "engine error",
    "draws nothing",
    "scene hierarchy",
    "not rendering",
    "blank screen",
    "not playable",
)


def _needs_research_text(text: str | None) -> bool:
    body = (text or "").lower()
    return any(mark in body for mark in _RESEARCH_MARKS)


def _goal_needs_research(goal: str | None) -> bool:
    return _needs_research_text(goal)


def _needs_cause_research(reason: str) -> bool:
    return _needs_research_text(reason)


def _needs_validation_lookup(reason: str, project: ProjectConfig) -> bool:
    text = (reason or "").lower()
    if project.preview_command:
        return False
    return "no preview server" in text or "preview_command" in text


def _goal_retry_prompt(
    goal: str,
    reason: str,
    diff: str,
    *,
    ui_errors: list[str] | tuple[str, ...] | None = None,
    stalled: bool = False,
    lookup: bool = False,
    research: bool = False,
    repeated: bool = False,
) -> str:
    stall = ""
    if repeated:
        stall += (
            "Your last edit did not change what the review sees: the same problems "
            "are still reported. That edit did not reach the cause. Read the files "
            "listed under 'Files named by the goal' again, find the step of the plan "
            "that is still not true, and take a different approach to it.\n\n"
        )
    if research:
        excerpt = failure_excerpt(goal)
        failure = f"Failure:\n{excerpt}\n\n" if excerpt else ""
        needle = error_search_needle(goal)
        exact = (
            f' The web_search query must include "{needle}" exactly. Do not search a paraphrase.\n'
            if needle
            else ""
        )
        stall += (
            "The last edit did not fix the stated failure. Stop repeating it. "
            "Call web_search for this exact error, then fetch_url an "
            "official page. Change the scene, template, or resource the docs "
            "say owns the missing object. Do not add another null check, print, "
            "or log, and do not invent a file this repo does not have. Write "
            "how to run, build, and test the project into AGENTS.md from those "
            "docs when that section is missing or wrong.\n"
            f"{exact}{failure}"
        )
    if lookup:
        stall += (
            "Validation is not configured, so this screen cannot be checked. "
            "Before other edits, name the language and framework from the repo. "
            "Call web_search for official docs on testing, linting, and previewing "
            "or capturing that stack, including the version when the repo states it. "
            "Call fetch_url on an official page. Call configure_project with the "
            "commands those docs support: test_command runs the suite and exits "
            "non-zero on failure, lint_command is the linter when the docs name one, "
            "and preview_command listens on {port}. A web app serves the app. A "
            "native or Godot app captures the changed screen and serves that image "
            "on {port}. Do not invent a command the fetched page does not support. "
            "Then call review_ui.\n\n"
        )
    if stalled:
        stall += (
            "You inspected the repo on the last retry but did not change files. "
            "This is not done. Call str_replace or write_file now.\n\n"
        )
    broken = ""
    errors = [str(item).strip() for item in (ui_errors or []) if str(item).strip()]
    if errors:
        listed = "\n".join(f"- {item}" for item in errors[:12])
        broken = (
            "\n\nThe rendered result still has these problems. Fix each one in this "
            "retry. An engine error names the node, path, or line that is wrong: "
            "open the scene or file it points at and correct the structure or the "
            "path. A node that draws nothing needs something to draw (a texture, "
            "a shape, or a ColorRect). A 404 means the HTML/CSS/JS href does not "
            "match a file the web server serves: mount or route it (FastAPI "
            "StaticFiles, express.static, or the project's existing static path), "
            "or correct the href. Then call review_ui and confirm the list is empty.\n"
            f"Problems:\n{listed}"
        )
    return (
        f"{stall}"
        "The stated goal is not done. The current diff does not fulfill it. "
        "Do not summarize. Do not switch to a different task. "
        "Do not write placeholder, status, or unrelated verification files. "
        "Do not leave a stub, mock, unused required field, or a follow-up for "
        "a later run. If the workspace already has the data (git remote, config), "
        "use it instead of asking a human to re-type it. "
        "If the diff is unrelated, replace or remove it and implement this exact goal. "
        "Use str_replace for surgical edits; do not rewrite large files. "
        "If existing tests assert old markup, APIs, or layout that this goal "
        "replaces, update those tests. "
        "If this is a UI change, call review_ui after editing, click new tabs, "
        "and fix render errors, 404s, or dead controls. Do not stop while the "
        "page fails to load CSS or JS you added. "
        "If the rendered UI section says there is no preview server, look up "
        "how this language or framework is tested and previewed before you "
        "invent a command. Call web_search, then fetch_url on an official "
        "docs page, then configure_project. preview_command must listen on "
        "{port} and serve a page that shows the control this goal changes. "
        "In a Godot project, review_ui runs the scene and lists engine errors "
        "and nodes that draw nothing; pass the .tscn as path. Then call "
        "review_ui and click the control. A note that "
        "says the screen works is not validation. "
        "Follow enabled workspace skills. Do not add a helper that nothing calls. "
        "If you edited docs or compose files, the commands must actually exist "
        "(lbw clone, docker compose, git clone — never docker clone). Run tests. "
        "If tests fail, fix them and call run_tests again. If this project has "
        "no test command, add a test at the public seam and write test_command "
        "in .lbw/config.yaml. Do not exit 0 without running the suite. "
        "Do not bind-mount this app's .lbw over the mounted workspace.\n\n"
        f"Goal:\n{goal.strip()}\n\n"
        f"Why it is not done:\n{reason.strip()}"
        f"{broken}\n\n"
        f"Current diff:\n{diff}"
    )


def _test_setup_prompt() -> str:
    return (
        "This project has no test command, so the cycle cannot tell whether "
        "the goal works. Name the language and framework from the repo. Call "
        "web_search for official docs on how that stack is tested, then "
        "fetch_url an official page. Add a test at the public seam (CLI, HTTP, "
        "rendered page, or the project's real runner) that fails when the "
        "behavior is wrong. Call configure_project with the test_command those "
        "docs support. The command must run the suite and exit non-zero on "
        "failure. Printing that tests would run, or exiting 0 without "
        "executing them, is not a test. Call run_tests. If it fails, fix the "
        "code or the test and call run_tests again. Do not invent a command "
        "the fetched page does not support."
    )


def _test_repair_prompt(output: str) -> str:
    return (
        "The test suite ran and failed. Fix the failing tests. "
        "If a test asserts old markup, API shape, or behavior that this goal "
        "intentionally changed, update that test to match the new implementation. "
        "Do not revert the goal. Do not write placeholder or unrelated "
        "verification tests. Do not change the test command so it exits 0 "
        "without running the failing check.\n\n"
        f"{(output or '').strip()}"
    )


def _ensure_test_command(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    agent: CodingAgent,
    tests_after,
    *,
    allow_create_pr: bool,
):
    """Create a real test command when the cycle would otherwise skip tests."""
    if not settings.require_tests or tests_after is not None:
        return tests_after
    context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    attempts = max(project.max_repair_attempts, 1)
    for attempt in range(attempts):
        log_progress(f"No test command; setting up tests ({attempt + 1}/{attempts})")
        setup = agent.run(
            _test_setup_prompt(),
            context,
            require_change=True,
            require_tests=True,
            require_lookup=True,
        )
        if setup.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            break
        tests_after = _maybe_test(workspace, project, settings, phase="setup")
        if tests_after is not None:
            return tests_after
    return tests_after


def _repair_failing_tests(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    agent: CodingAgent,
    tests_after,
    *,
    allow_create_pr: bool,
):
    if tests_after is None or tests_after.ok:
        return tests_after
    if runner_unavailable(tests_after.output or ""):
        log_progress("Test runner is not installed; this is not a failing test.")
        return tests_after
    context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    for attempt in range(project.max_repair_attempts):
        log_progress(f"Repair attempt {attempt + 1}/{project.max_repair_attempts}")
        before_repair = workspace_fingerprint(workspace.root)
        repair = agent.run(
            _test_repair_prompt(tests_after.output),
            context,
            require_change=True,
        )
        if repair.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            break
        if workspace_fingerprint(workspace.root) == before_repair:
            log_progress("Repair did not change code; not running tests again.")
            break
        tests_after = _maybe_test(workspace, project, settings, phase="repair")
        if tests_after is None or tests_after.ok or runner_unavailable(tests_after.output or ""):
            if tests_after is not None and runner_unavailable(tests_after.output or ""):
                log_progress("Test runner is not installed; this is not a failing test.")
            break
    return tests_after


def _choose_goal(project: ProjectConfig, tests_before) -> str | None:
    if _tests_block_commit(tests_before):
        return "Make the project's test suite pass.\n\n" + tests_before.output
    goals = load_goals(project.root, project.goals_file)
    return goals[0] if goals else None


def _has_project_work(workspace: Workspace, sha_before: str | None, sha: str | None) -> bool:
    """Root git changes, plus edits inside another project in this workspace."""
    if has_changes(workspace) or bool(sha_before and sha and sha != sha_before):
        return True
    return nested_projects_changed(workspace)


def _tests_after_edits(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    tests_before,
    *,
    code_changed: bool,
):
    """Run the suite only when this code has not already been tested."""
    if not code_changed and tests_before is not None:
        log_progress("No code changes; skipping tests.")
        return tests_before
    covered = current_test_result(workspace, project.test_command)
    if covered is not None:
        log_progress("Tests already cover this code; not running them again.")
        return covered
    log_progress("Running after tests...")
    return _maybe_test(workspace, project, settings, phase="after")


def _runner_missing(result) -> bool:
    return result is not None and not result.ok and runner_unavailable(result.output or "")


def _tests_passed_value(result) -> bool | None:
    """True when the suite passed, False when it failed, None when it did not run."""
    if result is None or _runner_missing(result):
        return None
    return bool(result.ok)


def _tests_block_commit(result) -> bool:
    """A suite that ran and failed blocks the commit. A missing runner does not."""
    return result is not None and not result.ok and not _runner_missing(result)


def _maybe_test(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    *,
    phase: str = "tests",
):
    command = project.test_command or load_project(workspace.root).test_command
    if not command:
        return None
    return run_project_tests(
        workspace,
        command,
        settings.command_timeout_seconds,
        phase=phase,
    )


def _lint_failure_reason(output: str, *, limit: int = 400) -> str:
    text = " ".join((output or "").split())
    if not text:
        return "lint failed; commit skipped"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"lint failed; commit skipped: {text}"


def _failure_notes(reason: str, summary: str | None) -> str:
    """The stored notes for a failed cycle lead with the failure, not a success claim."""
    lead = f"Failed: {(reason or 'the goal was not met').strip()}"
    notes = (summary or "").strip()
    if not notes or notes.lower().startswith("failed:"):
        return notes or lead
    return f"{lead}\n\nAgent notes:\n{notes}"


def _with_lint_output(summary: str | None, output: str) -> str:
    body = (summary or "").strip()
    lint = (output or "").strip()
    if not lint:
        return body
    block = f"Lint:\n{lint}"
    return f"{body}\n\n{block}".strip() if body else block


def _lint_repair_prompt(output: str) -> str:
    return (
        "The linter failed. These are the remaining errors. Read every reported "
        "file, then str_replace each finding until the configured lint command "
        "passes. Do not skip a finding because it looks older than this task. "
        "F821 means a name is used where it does not exist — class-body "
        "decorators cannot use `self`; wire the command after the instance "
        "exists or use a method decorator. F401 means remove or use the unused "
        "import. E501 means wrap the line to 100 characters or less. I001 means "
        "sort imports the way ruff does. If ruff reports files that would be "
        "reformatted, run the formatter so the files match `ruff format`. Do "
        "not disable the linter.\n\n"
        f"{(output or '').strip()}"
    )


def _maybe_lint(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    *,
    phase: str = "lint",
):
    command = project.lint_command or load_project(workspace.root).lint_command
    if not command:
        return None
    return run_project_lint(
        workspace,
        command,
        settings.command_timeout_seconds,
        phase=phase,
    )


def _lint_repair_attempts(project: ProjectConfig) -> int:
    if project.max_repair_attempts <= 0:
        return 0
    return max(project.max_repair_attempts, DEFAULT_MAX_REPAIR_ATTEMPTS)


def _ensure_lint(
    workspace: Workspace,
    project: ProjectConfig,
    settings: Settings,
    agent: CodingAgent,
    *,
    allow_create_pr: bool,
    sha_before: str | None = None,
):
    del sha_before
    lint_after = _maybe_lint(workspace, project, settings, phase="after")
    if lint_after is None or lint_after.ok:
        return lint_after
    if ruff_unavailable(lint_after.output or ""):
        log_progress("Lint did not start because ruff is not installed.")
        return lint_after
    log_progress("Lint failed; applying automatic fixes, then repairing remaining errors.")
    lint_command = project.lint_command or load_project(workspace.root).lint_command
    if apply_ruff_autofix(
        workspace,
        lint_command,
        settings.command_timeout_seconds,
    ):
        lint_after = _maybe_lint(workspace, project, settings, phase="autofix")
        if lint_after is None or lint_after.ok:
            return lint_after
    attempts = _lint_repair_attempts(project)
    if attempts <= 0:
        return lint_after
    context = collect_context(workspace.root, project, allow_publish=allow_create_pr)
    for attempt in range(attempts):
        log_progress(f"Lint repair {attempt + 1}/{attempts}")
        log_progress(f"Lint errors to fix:\n{(lint_after.output or '').strip()}")
        before_repair = workspace_fingerprint(workspace.root)
        repair = agent.run(
            _lint_repair_prompt(lint_after.output),
            context,
            require_change=True,
        )
        if repair.stopped_reason == "oscillating":
            log_progress("The agent kept adding and removing the same small edit. Stopping.")
            break
        if workspace_fingerprint(workspace.root) == before_repair:
            log_progress("Lint repair did not change code; not running lint again.")
            break
        lint_after = _maybe_lint(workspace, project, settings, phase="repair")
        if lint_after is None or lint_after.ok:
            break
    return lint_after


_PROMPT_STARTS = (
    "start from the ",
    "if this project already meets the goal",
    "if you find a fix",
    "this folder is for documents",
    "do the task below",
    "do not add tests",
)


def _is_prompt_line(line: str) -> bool:
    lowered = " ".join(line.split()).lower()
    return any(lowered.startswith(prefix) for prefix in _PROMPT_STARTS)


def _user_request(goal: str) -> str:
    """The person's request, without the schedule or document-run instructions."""
    text = (goal or "").strip()
    if "\n\n" in text:
        head, tail = text.split("\n\n", 1)
        if _is_prompt_line(head):
            text = tail.strip()
    lines = [
        line.strip() for line in text.splitlines() if line.strip() and not _is_prompt_line(line)
    ]
    return lines[0] if lines else ""


def _same_request(left: str, right: str) -> bool:
    a = " ".join((left or "").split()).strip().lower().rstrip(".")
    b = " ".join((right or "").split()).strip().lower().rstrip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 24 and shorter in longer


def _clip_title(text: str, limit: int = 72) -> str:
    clean = " ".join((text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0].rstrip(".,;:-")
    return cut or clean[:limit].rstrip()


def _commit_message(goal: str, summary: str) -> str:
    request = _user_request(goal)
    summary_line = summary.strip().splitlines()[0] if summary.strip() else ""
    if (
        summary_line
        and not _is_prompt_line(summary_line)
        and not _same_request(summary_line, request)
    ):
        title = _clip_title(summary_line)
    else:
        title = _clip_title(request or summary_line)
    return title or "Update the project"


_AGENT_REVIEW_UI_SLUG = "ui-review_review-ui_"


def _pr_screenshot_rel(name: str) -> str:
    filename = Path(name).name
    if filename == "ui-review.png":
        return f".lbw/{filename}"
    return f".lbw/ui-screenshots/{filename}"


def _pr_screenshot_names() -> list[str]:
    """Keep the last successful capture of the change, not every review_ui dump."""
    change_shots: list[str] = []
    fallback: list[str] = []
    for event in current_events():
        if event.get("kind") != "ui":
            continue
        if event.get("ok") is False:
            continue
        name = str(event.get("screenshot_filename") or "").strip()
        if not name:
            name = Path(str(event.get("screenshot") or "")).name
        name = Path(name).name
        if not name:
            continue
        if _AGENT_REVIEW_UI_SLUG in name.lower():
            fallback.append(name)
            continue
        change_shots.append(name)
    chosen = change_shots or fallback
    return chosen[-1:] if chosen else []


def _pr_screenshot_files(workspace: Workspace) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for name in _pr_screenshot_names():
        path = resolve_ui_screenshot(workspace.root, name)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
    return files


def _pr_image_base(
    workspace: Workspace,
    commit_sha: str | None,
    remote: str = "origin",
) -> str | None:
    if not commit_sha:
        return None
    parsed = github_owner_repo(workspace, remote)
    if not parsed:
        return None
    owner, repo = parsed
    return f"https://github.com/{owner}/{repo}/raw/{commit_sha}"


def _pr_body(
    goal: str,
    summary: str,
    *,
    tests_passed: bool | None,
    commit_sha: str | None,
    branch: str,
    screenshots: list[str] | None = None,
    image_base: str | None = None,
) -> str:
    request = _user_request(goal)
    if tests_passed is True:
        tests_line = "- [x] Project tests passed locally"
    elif tests_passed is False:
        tests_line = "- [ ] Project tests failed locally — do not merge until green"
    else:
        tests_line = "- [ ] No project test command; verify manually"

    changes_section = _extract_changes_from_summary(summary)
    changed = changes_section if changes_section else ((summary or "").strip() or request)
    if changed and _is_prompt_line(changed.splitlines()[0]):
        changed = request or "No changes described."
    changed = changed or "No changes described."
    testing_steps = _extract_testing_steps_from_summary(summary)
    reason = _extract_reason_from_summary(summary)
    if reason and (
        _same_request(reason, request) or reason.lower().startswith("to address the stated goal")
    ):
        reason = ""
    lines = ["## What Changed", "", changed]
    if reason:
        lines.extend(["", "## Why This Update", "", reason])
    if request and not _same_request(request, changed) and not _same_request(request, reason):
        lines.extend(["", "## Goal", "", request])
    lines.extend(
        [
            "",
            "## Test plan",
            "",
            tests_line,
            "- [x] Goal review confirmed the requested outcome",
            "- [ ] Review this feature branch; do not merge unreviewed commits to main",
        ]
    )

    # Add testing steps if available
    if testing_steps:
        lines.extend(["", "## Steps to Test"])
        lines.extend([""])
        for i, step in enumerate(testing_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    shots = [Path(name).name for name in (screenshots or []) if str(name).strip()]
    hosted = (image_base or "").rstrip("/")
    if shots and hosted:
        lines.extend(["", "## Screenshots", ""])
        for name in shots:
            lines.append(f"![{name}]({hosted}/{_pr_screenshot_rel(name)})")
    if commit_sha:
        lines.extend(["", f"Commit: `{commit_sha}`"])
    if branch:
        lines.append(f"Branch: `{branch}`")
    lines.extend(["", CO_AUTHORED_BY])
    return "\n".join(lines)


def _extract_changes_from_summary(summary: str | None) -> str:
    """Extract what changed from the summary, focusing on file changes and modifications."""
    if not summary:
        return "No changes described."

    summary_lines = [line.strip() for line in summary.split("\n") if line.strip()]

    # Look for sections in the summary like "Files changed:", "Changes:", etc.
    change_sections = []
    current_section = []

    for line in summary_lines:
        lower = line.lower()
        if any(
            trigger in lower
            for trigger in (
                "files changed",
                "changes:",
                "this commit",
                "modified ",
                "added ",
                "removed ",
            )
        ):
            if current_section:
                change_sections.append("\n".join(current_section))
            current_section = [line]
        elif current_section:
            current_section.append(line)

    if current_section:
        change_sections.append("\n".join(current_section))

    # If we found explicit change sections, use them
    if change_sections:
        return change_sections[0] if len(change_sections) == 1 else "\n\n".join(change_sections)

    # Otherwise, use the first non-goal line from the summary
    for index, line in enumerate(summary_lines):
        lowered = line.lower()
        if lowered.startswith("goal:") or lowered.startswith("why:"):
            continue
        rest = summary_lines[index + 1 : index + 50]
        if rest:
            return line + "\n" + "\n".join(rest)
        return line
    return ""


def _extract_reason_from_summary(summary: str | None) -> str:
    """Extract why the update was made from the summary."""
    if not summary:
        return ""

    summary_lower = summary.lower()

    reason_words = ("why", "rationale", "motivation", "justification")
    if any(word in summary_lower for word in reason_words):
        lines = [line.strip() for line in summary.split("\n") if line.strip()]
        for index, line in enumerate(lines):
            if any(trigger in line.lower() for trigger in (*reason_words, "because")):
                return " ".join(lines[index : min(index + 4, len(lines))])

    # Look for "to address" or "to implement" patterns
    to_patterns = ["to address", "to implement", "to fix", "to add", "to update", "to resolve"]
    for pattern in to_patterns:
        if pattern in summary_lower:
            idx = summary_lower.index(pattern)
            # Get the rest of the line after the pattern
            after = summary[idx:].split("\n")[0]
            return after.strip()

    return ""


def _extract_testing_steps_from_summary(summary: str | None) -> list[str]:
    """Short unique checks: commands that ran, then numbered steps under a test heading."""
    steps: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        clean = " ".join(text.split()).strip()
        clean = re.sub(r"^\d+[.)]\s+", "", clean).lstrip("-").strip()
        if not clean or len(clean) > 140:
            return
        key = clean.lower().rstrip(".")
        if key in seen:
            return
        seen.add(key)
        steps.append(clean)

    for event in current_events():
        if event.get("kind") != "test":
            continue
        command = " ".join(str(event.get("command") or "").split())
        if not command:
            continue
        outcome = "passed" if event.get("ok") else "failed"
        add(f"`{command}` — {outcome}")

    capture = False
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(heading in lower for heading in ("how to test", "steps to test", "testing:")):
            capture = True
            continue
        numbered = bool(re.match(r"^(\d+[.)]|-)\s+", stripped))
        if capture and numbered:
            add(stripped)
        elif capture:
            capture = False
        if len(steps) >= 5:
            break
    return steps[:5]


def _git_env(settings: Settings) -> dict[str, str]:
    env: dict[str, str] = {}
    if settings.git_author_name:
        env["GIT_AUTHOR_NAME"] = settings.git_author_name
        env["GIT_COMMITTER_NAME"] = settings.git_author_name
    if settings.git_author_email:
        env["GIT_AUTHOR_EMAIL"] = settings.git_author_email
        env["GIT_COMMITTER_EMAIL"] = settings.git_author_email
    return env


def _stamp_git(root: Path, result: CycleResult) -> None:
    try:
        workspace = Workspace(root)
    except (OSError, SandboxError):
        return
    if not result.branch:
        result.branch = current_branch(workspace)
    if not result.commit_sha:
        result.commit_sha = current_sha(workspace)


def _attach_events(result: CycleResult) -> None:
    if not result.events:
        result.events = current_events()


def _write_run_log(root: Path, result: CycleResult) -> None:
    _stamp_git(root, result)
    _attach_events(result)
    ensure_run_gitignore(root)
    runs = root / ".lbw" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = runs / f"{stamp}.json"
    payload = asdict(result)
    task_id = _TASK_ID.get()
    payload["id"] = task_id or stamp
    payload.setdefault("created_at", result.created_at)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _workspace_palace(
    root: Path,
    *,
    siblings: list[dict] | None,
    task_id: str | None,
    store_root: Path | None = None,
    database_url: str = "",
):
    from agent_lbw.runtime.db import database, database_configured
    from agent_lbw.runtime.palace import Palace, open_palace, seed_workspace

    tenant_id = ""
    project_id = ""
    stored: list[dict] = []
    if store_root is not None and database_configured(store_root, database_url):
        from agent_lbw.runtime.account import load_account
        from agent_lbw.runtime.store.recording import project_for_workspace, recorded_history

        account = load_account(store_root, database_url=database_url)
        with database(store_root, database_url) as db:
            project = project_for_workspace(db, account, root, root.name or "project")
        tenant_id = str(account["org"]["id"])
        project_id = str(project["id"])
        stored = recorded_history(store_root, database_url, root)
    palace: Palace = open_palace(root, tenant_id=tenant_id, project_id=project_id)
    _PALACE.set(palace)
    records = [*stored, *(siblings or [])]
    if task_id:
        records = [record for record in records if str(record.get("id") or "") != task_id]
    seed_workspace(root, palace, siblings=records, include_files=not tenant_id)
    return palace


def _remember_cycle(root: Path, result: CycleResult) -> None:
    from agent_lbw.runtime.palace import open_palace, remember_record

    if not (result.goal or result.summary):
        return
    palace = _PALACE.get()
    if palace is None:
        palace = open_palace(root)
    remember_record(
        palace,
        {
            "id": _TASK_ID.get(),
            "goal": result.goal,
            "summary": result.summary,
            "status": result.status,
            "reason": result.reason,
            "created_at": result.created_at,
            "pr_url": result.pr_url,
            "branch": result.branch,
        },
    )


def _append_to_history(root: Path, result: CycleResult) -> None:
    """Append cycle result to history.json for persistent storage across restarts."""
    history_file = root / "history.json"
    # Read existing history
    try:
        if history_file.exists():
            with open(history_file, encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []
    except (OSError, json.JSONDecodeError):
        # If we can't read the file for any reason, start fresh
        history = []

    # Add the new result to the beginning of the list
    # (most recent at the beginning) and keep only the last 100 entries
    _stamp_git(root, result)
    _attach_events(result)
    entry = asdict(result)
    task_id = _TASK_ID.get()
    if task_id:
        entry["id"] = task_id
    history.insert(0, entry)

    # Limit history size to avoid file becoming too large
    if len(history) > 100:
        history = history[:100]

    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except OSError:
        # If we can't write the file, silently ignore (not critical for functionality)
        pass
    _remember_cycle(root, result)
