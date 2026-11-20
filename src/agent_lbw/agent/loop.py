from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from agent_lbw.agent.prompts import SYSTEM_PROMPT, adapt_system_prompt, user_prompt
from agent_lbw.llm.client import AssistantTurn, LLMClient, ToolCall
from agent_lbw.llm.toolparse import parse_tool_calls
from agent_lbw.progress import record_event, timed_complete
from agent_lbw.runtime.palace import Palace, fit_prompt, memory_preface
from agent_lbw.runtime.review import error_search_needle, search_uses_error
from agent_lbw.tools import ToolSpec, execute_tool
from agent_lbw.tools.tests import runner_unavailable

log = logging.getLogger("lbw")

MUTATING_TOOLS = {"write_file", "str_replace", "delete_file", "configure_project"}
VERIFY_TOOLS = {"review_ui", "run_tests"}
CHECK_TOOLS = {"review_ui", "run_tests", "run_lint"}
UI_SUFFIXES = {".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
OPS_NAMES = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}
OPS_SUFFIXES = {".md", ".rst", ".adoc"}
MAX_PLAN_NUDGES = 3
MAX_REQUIRE_CHANGE_NUDGES = 8
MAX_EDIT_CHURN = 4
MAX_UNFINISHED_NUDGES = 4
MAX_VALIDATE_NUDGES = 4
MAX_INSPECT_ROUNDS = 3
MAX_LOOKUP_NUDGES = 2
CONTINUE_NUDGE = (
    "You have not changed any files that implement the goal. Inspection is over. "
    "Call str_replace on the existing files (or write_file for a new/small file) "
    "and implement this exact goal — not a placeholder, status note, "
    "verification test, or a different task you noticed in the repo. "
    "Do not rewrite a large file with write_file."
)
UNFINISHED_NUDGE = (
    "That reply is not a finish. You still have work left on the stated goal. "
    "Finish the real behavior now: no stubs, mocks, TODOs, unused fields, or "
    "follow-up for a later run. Call a tool and apply the next edit. "
    "Do not narrate the change; str_replace it."
)
MUST_EDIT_NUDGE = (
    "The goal is still unmet. Do not stop. Call str_replace or write_file "
    "and fix the failure. If CSS or JS returned 404, the file is missing or "
    "the web server is not serving that path — add the static mount/route "
    "or correct the href, then call review_ui until those URLs load."
)
INSPECT_NUDGE = (
    "You have been inspecting the repo without changing files. "
    "Stop reading. Call str_replace and implement this exact goal. "
    "Use write_file only for a new or small file. "
    "Do not write placeholder, status, or verification files."
)
VALIDATE_UI_NUDGE = (
    "You changed UI files but have not called review_ui since the last edit. "
    "Call review_ui, click the new control, and fix errors, 404s, or dead buttons. "
    "A control that is visible but does not fetch or change the page is not done."
)
VALIDATE_TESTS_NUDGE = (
    "You changed code but have not called run_tests since the last edit. "
    "Run the project tests and fix failures before summarizing."
)
VALIDATE_TESTS_SETUP_NUDGE = (
    "run_tests cannot pass because this project has no test command. "
    "Call web_search, then fetch_url on an official docs page for this "
    "stack, then configure_project with the test_command those docs support. "
    "Call run_tests again."
)
LOOKUP_NUDGE = (
    "This stack is not configured for lbw yet. Call web_search for official "
    "docs on how to test, lint, and preview this language or framework. Then "
    "fetch_url an official page. Then call configure_project with the commands "
    "those docs support. preview_command must listen on {port}. Do not invent "
    "a command the fetched page does not support."
)
RESEARCH_NUDGE = (
    "The stated failure is still unfixed. Call web_search for the error and "
    "this framework, then fetch_url an official page. Edit the scene, template, "
    "or resource that should contain the missing object. Do not add another "
    "null check, print, or log in the crashing file. Do not invent a file "
    "the repo and the docs do not have."
)
DOCS_NUDGE = (
    "Write how to run, build, and test this project into AGENTS.md, using the "
    "commands from the official page you fetched. Name the real entry scene "
    "or program and the test command. If that section already matches the "
    "docs, correct it instead of adding a second copy. Then apply the code fix."
)
COMMENT_NUDGE = (
    "The last edit only added or removed a comment, blank lines, or the same "
    "text. That does not implement the goal. Call str_replace with the code "
    "change that makes the goal true. Do not put that change back and take it out."
)
STOP_RERUN_NUDGE = (
    "Lint, tests, and review already ran for the current code. No code file "
    "has changed since, in this project or any other project in the workspace. "
    "Do not call run_tests, run_lint, or review_ui again until you edit code. "
    "If the goal is done, summarize. If it is not, edit the code."
)
VALIDATE_OPS_NUDGE = (
    "You changed Docker, compose, or docs but have not called run_tests since "
    "the last edit. Run tests. Commands in README must exist: use lbw clone or "
    "git clone, never docker clone. Do not bind-mount this app's .lbw over "
    "/workspaces/.lbw. init requires a directory that already exists."
)
RECOVERY_TWICE_NUDGE = (
    "Tool {name} failed twice: {message}. Do not repeat the same arguments. "
    "If this is str_replace, call read_file on {path}, copy a unique old_string "
    "from the file body (not the N| prefix), then str_replace once."
)
RECOVERY_THREE_NUDGE = (
    "Tool {name} failed three times in a row. "
    "Search the workspace or list the directory to understand the current state "
    "before trying again."
)
_UNFINISHED_RE = re.compile(
    r"(?is)("
    r"\blet me\b|"
    r"\bi(?:'m about to|'ll| will| am going to)\b|"
    r"\bi need to (?:fix|update|write|add|change|edit|run|implement|adjust)\b|"
    r"\bhalf[- ]baked\b|"
    r"\bfor now\b|"
    r"\bin a real implementation\b|"
    r"\bleave (?:it|this|the rest) (?:for|to) (?:a )?later\b|"
    r"\bthis enables\b|"
    r"\bgenerated? sample\b|"
    r"\bmock(?:ed)? data\b|"
    r"\bnot fully wired\b|"
    r"\bstarting point\b|"
    r"\bhandler will\b|"
    r"\biteration limit\b"
    r")"
)


_READ_PREFIX = re.compile(r"^\s*\d+\|")
_DOC_SUFFIXES = {".md", ".rst", ".adoc", ".txt"}


def normalize_edit_text(text: str) -> str:
    """Compare edits without read_file line numbers or trailing whitespace."""
    lines: list[str] = []
    for line in (text or "").replace("\r\n", "\n").splitlines():
        lines.append(_READ_PREFIX.sub("", line).rstrip())
    return "\n".join(lines).strip()


def _is_comment_line(stripped: str) -> bool:
    if stripped.startswith(("//", "/*", "*/", "<!--", "-->")):
        return True
    return stripped.startswith("#") and not stripped.startswith("#!")


def _code_lines(text: str) -> str:
    kept: list[str] = []
    for line in normalize_edit_text(text).splitlines():
        stripped = line.strip()
        if not stripped or _is_comment_line(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def is_comment_only_edit(path: str, old: str, new: str) -> bool:
    """True when the edit does not change code. Docs may change heading text."""
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if suffix in _DOC_SUFFIXES or name.startswith("readme"):
        return normalize_edit_text(old) == normalize_edit_text(new)
    return _code_lines(old) == _code_lines(new)


def note_edit(
    recent: list[tuple[str, str, str]],
    path: str,
    old: str,
    new: str,
) -> str:
    """Record an edit. Return 'churn' when it undoes, repeats, or only comments."""
    left = normalize_edit_text(old)
    right = normalize_edit_text(new)
    churn = _edit_churns(recent, path, left, right, old, new)
    recent.append((path, left, right))
    if len(recent) > 12:
        del recent[:-12]
    return "churn" if churn else "substantive"


def previous_written(recent: list[tuple[str, str, str]], path: str) -> str:
    for prev_path, _old, new in reversed(recent):
        if prev_path == path:
            return new
    return ""


def _edit_churns(
    recent: list[tuple[str, str, str]],
    path: str,
    left: str,
    right: str,
    old: str,
    new: str,
) -> bool:
    for prev_path, prev_old, prev_new in reversed(recent):
        if prev_path != path:
            continue
        if prev_old == right and prev_new == left:
            return True
        break
    if any(
        prev_path == path and prev_old == left and prev_new == right
        for prev_path, prev_old, prev_new in recent
    ):
        return True
    return is_comment_only_edit(path, old, new)


def looks_unfinished(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if value.endswith(":") or value.endswith("..."):
        return True
    return bool(_UNFINISHED_RE.search(value))


def _is_ui_path(path: str) -> bool:
    raw = (path or "").replace("\\", "/").lower()
    suffix = Path(raw).suffix
    return suffix in UI_SUFFIXES or "/templates/" in raw or raw.endswith(".html")


def _is_ops_path(path: str) -> bool:
    raw = (path or "").replace("\\", "/").lower()
    name = Path(raw).name
    suffix = Path(raw).suffix
    return (
        name in OPS_NAMES
        or name.startswith("dockerfile")
        or "docker-compose" in name
        or suffix in OPS_SUFFIXES
        or name.startswith("readme")
    )


def _tool_path(arguments: dict) -> str:
    value = arguments.get("path") if isinstance(arguments, dict) else None
    return str(value or "")


def _writes_project_config(path: str) -> bool:
    posix = path.replace("\\", "/").strip()
    while posix.startswith("./"):
        posix = posix[2:]
    return posix == ".lbw/config.yaml" or posix.endswith("/.lbw/config.yaml")


@dataclass
class FailureTracker:
    """Track consecutive failures for recovery logic."""

    consecutive_failures: int = 0
    last_failed: str | None = None
    failed_by_tool_path: dict[tuple[str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    last_path: str | None = None

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.last_failed = None
        self.failed_by_tool_path.clear()
        self.last_path = None

    def record_failure(self, tool_name: str, path: str, message: str) -> None:
        """Record a tool failure, updating counters and tracking."""
        self.consecutive_failures += 1
        self.last_failed = message
        self.last_path = path if path else None
        key = (tool_name, path)
        self.failed_by_tool_path[key] += 1

    def get_same_tool_failures(self, tool_name: str, path: str) -> int:
        """Get failure count for the same tool on the same path."""
        key = (tool_name, path)
        return self.failed_by_tool_path.get(key, 0)

    def has_recovery_triggered(self) -> bool:
        """Check if recovery should trigger (3 consecutive failures but not auto-read)."""
        return self.consecutive_failures >= 3


@dataclass
class AgentResult:
    summary: str
    iterations: int
    tool_calls: int
    stopped_reason: str
    last_error: str | None = None
    question: str = ""


class CodingAgent:
    def __init__(
        self,
        llm: LLMClient,
        tools: list[ToolSpec],
        *,
        max_iterations: int,
        system_prompt: str | None = None,
        palace: Palace | None = None,
        context_window: int | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.palace = palace
        self.context_window = context_window
        prompt = (system_prompt or "").strip()
        model = getattr(llm, "model", "") or ""
        self.system_prompt = adapt_system_prompt(prompt or SYSTEM_PROMPT, model)

    def run(
        self,
        goal: str,
        context: str = "",
        *,
        require_change: bool = False,
        require_tests: bool = False,
        require_lookup: bool = False,
        require_research: bool = False,
        require_docs: bool = False,
    ) -> AgentResult:
        context = memory_preface(self.palace, goal, context)
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt(goal, context)},
        ]
        schemas = [tool.openai_schema() for tool in self.tools]
        known_names = {tool.name for tool in self.tools}
        tool_calls = 0
        mutated = False
        mutated_ui = False
        mutated_ops = False
        verified_ui = False
        verified_tests = False
        tests_need_command = False
        plan_nudges = 0
        unfinished_nudges = 0
        validate_nudges = 0
        lookup_nudges = 0
        inspect_rounds = 0
        looked_up = False
        fetched_docs = False
        configured_validation = False
        wrote_agents = False
        docs_nudges = 0
        since_edit: set[str] = set()
        repeat_only_turns = 0
        substantive = False
        recent_edits: list[tuple[str, str, str]] = []
        churn = 0
        oscillation_nudges = 0

        last_text = ""
        last_error: str | None = None
        failure_tracker = FailureTracker()

        for iteration in range(1, self.max_iterations + 1):
            messages = fit_prompt(
                messages,
                self.palace,
                query=goal,
                context_window=self.context_window,
            )
            turn = timed_complete(self.llm, messages, schemas, purpose="agent")
            last_text = turn.text or last_text
            snippet = " ".join((turn.text or "").split())
            if snippet:
                log.info("model %s", snippet[:400])
            calls = turn.tool_calls or parse_tool_calls(turn.text, known_names)
            if calls:
                native = bool(turn.tool_calls)
                if native:
                    messages.append(_assistant_tool_message(turn))
                else:
                    messages.append({"role": "assistant", "content": turn.text or ""})
                wrote = False
                inspected = False
                turn_new_check = False
                turn_repeat_check = False
                stop_oscillation = False
                for call in calls:
                    tool_calls += 1
                    result = execute_tool(self.tools, call.name, call.arguments)
                    log.info("tool %s ok=%s", call.name, result.ok)
                    if call.name == "ask_user" and result.ok:
                        question = (result.output or "").strip()[:2000]
                        record_event(kind="step", message=question)
                        return AgentResult(
                            summary=question,
                            iterations=iteration,
                            tool_calls=tool_calls,
                            stopped_reason="ask",
                            question=question,
                            last_error=last_error,
                        )
                    if call.name == "web_search":
                        query = str((call.arguments or {}).get("query") or "")
                        if search_uses_error(query, goal):
                            looked_up = True
                        else:
                            needle = error_search_needle(goal)
                            record_event(
                                kind="step",
                                message=f"Search omitted the error {needle}; use that text.",
                            )
                    elif call.name == "fetch_url":
                        fetched_docs = True
                    log.debug("%s args=%s", call.name, call.arguments)
                    if not result.ok:
                        last_error = (result.output or "")[:200]
                        # Track failure with path
                        path = _tool_path(call.arguments)
                        failure_tracker.record_failure(call.name, path, result.output or "")

                        # Check for recovery trigger after 3 consecutive failures
                        if failure_tracker.has_recovery_triggered():
                            # Avoid auto-reading on a failed auto-read
                            if call.name != "read_file" or "recovery" not in (result.output or ""):
                                path_to_read = failure_tracker.last_path
                                if path_to_read:
                                    log.info("tool %s ok=… (recovery)", "read_file")
                                    read_result = execute_tool(
                                        self.tools, "read_file", {"path": path_to_read}
                                    )
                                    tool_calls += 1  # Count the recovery read_file call
                                    log.info("tool read_file ok=%s (recovery)", read_result.ok)
                                    messages.append(
                                        _tool_result_message(
                                            ToolCall(
                                                id="auto-read",
                                                name="read_file",
                                                arguments={"path": path_to_read},
                                            ),
                                            read_result.output,
                                            native=True,
                                        )
                                    )
                                    failure_tracker.reset()
                                else:
                                    # No path known, nudge agent to search/list
                                    nudge_msg = RECOVERY_THREE_NUDGE.format(
                                        name=call.name,
                                    )
                                    log.info("nudging agent to search or list after hammering")
                                    messages.append(
                                        {"role": "user", "content": _nudge(nudge_msg, goal)}
                                    )
                                    failure_tracker.reset()
                                continue
                        # Check for 2 failures of same tool on same path
                        same_path_failures = failure_tracker.get_same_tool_failures(call.name, path)
                        if same_path_failures >= 2:
                            nudge_msg = RECOVERY_TWICE_NUDGE.format(
                                name=call.name,
                                message=(result.output or "")[:200],
                                path=path or "the file",
                            )
                            log.info("injecting recovery nudge: tool %s failed twice", call.name)
                            messages.append({"role": "user", "content": _nudge(nudge_msg, goal)})
                    else:
                        # Success resets failure counter
                        failure_tracker.reset()

                        if call.name == "configure_project":
                            configured_validation = True
                        if call.name in MUTATING_TOOLS:
                            mutated = True
                            wrote = True
                            verified_tests = False
                            since_edit.clear()
                            tool_path = _tool_path(call.arguments)
                            if tool_path.replace("\\", "/").endswith("AGENTS.md"):
                                wrote_agents = True
                            if _writes_project_config(tool_path):
                                configured_validation = True
                            if _is_ui_path(tool_path):
                                mutated_ui = True
                                verified_ui = False
                            if _is_ops_path(tool_path):
                                mutated_ops = True
                            if call.name == "configure_project" or _writes_project_config(
                                tool_path
                            ):
                                substantive = True
                            elif call.name == "delete_file":
                                substantive = True
                            elif call.name in {"str_replace", "write_file"}:
                                arguments = call.arguments or {}
                                if call.name == "write_file":
                                    old_text = previous_written(recent_edits, tool_path)
                                    new_text = str(arguments.get("content") or "")
                                else:
                                    old_text = str(arguments.get("old_string") or "")
                                    new_text = str(arguments.get("new_string") or "")
                                kind = note_edit(recent_edits, tool_path, old_text, new_text)
                                if kind == "substantive":
                                    substantive = True
                                    churn = 0
                                else:
                                    churn += 1
                                    if churn >= MAX_EDIT_CHURN:
                                        stop_oscillation = True
                        elif call.name == "review_ui":
                            verified_ui = bool(result.ok)
                        elif call.name == "run_tests":
                            missing_runner = runner_unavailable(result.output or "")
                            verified_tests = bool(result.ok) or missing_runner
                            tests_need_command = (not result.ok) and (
                                result.output or ""
                            ).startswith("no test command configured")
                        elif call.name not in VERIFY_TOOLS:
                            inspected = True
                    if call.name in CHECK_TOOLS:
                        if call.name in since_edit and not wrote:
                            turn_repeat_check = True
                        else:
                            since_edit.add(call.name)
                            turn_new_check = True
                    messages.append(_tool_result_message(call, result.output, native=native))
                    if stop_oscillation:
                        break
                if stop_oscillation:
                    log.info("stopping after repeated add and remove of the same small edit")
                    record_event(
                        kind="step",
                        message="Stopped repeating the same small edit.",
                    )
                    return AgentResult(
                        summary=(
                            "Stopped because the same small edit was added and removed again."
                        ),
                        iterations=iteration,
                        tool_calls=tool_calls,
                        stopped_reason="oscillating",
                        last_error=last_error,
                    )
                if churn >= 2 and oscillation_nudges < 1:
                    oscillation_nudges += 1
                    log.info("nudging agent away from a comment-only edit")
                    record_event(
                        kind="step",
                        message="That edit did not implement the goal; asking for the real change.",
                    )
                    messages.append({"role": "user", "content": _nudge(COMMENT_NUDGE, goal)})
                if wrote:
                    inspect_rounds = 0
                    repeat_only_turns = 0
                elif turn_repeat_check and not turn_new_check:
                    if repeat_only_turns == 0:
                        repeat_only_turns = 1
                        log.info("nudging agent to stop re-running checks")
                        record_event(
                            kind="step",
                            message="Checks already cover this code; not running them again.",
                        )
                        messages.append({"role": "user", "content": _nudge(STOP_RERUN_NUDGE, goal)})
                elif inspected and not mutated:
                    inspect_rounds += 1
                    if inspect_rounds >= MAX_INSPECT_ROUNDS:
                        inspect_rounds = 0
                        log.info("nudging agent to stop inspecting and write files")
                        messages.append({"role": "user", "content": _nudge(INSPECT_NUDGE, goal)})
                continue

            if not substantive:
                nudge_limit = MAX_REQUIRE_CHANGE_NUDGES if require_change else MAX_PLAN_NUDGES
                if plan_nudges < nudge_limit:
                    plan_nudges += 1
                    log.info("nudging agent to keep working after a plan-only turn")
                    messages.append({"role": "assistant", "content": turn.text or ""})
                    if recent_edits:
                        nudge = COMMENT_NUDGE
                    elif require_change:
                        nudge = MUST_EDIT_NUDGE
                    else:
                        nudge = CONTINUE_NUDGE
                    messages.append({"role": "user", "content": _nudge(nudge, goal)})
                    continue
                if recent_edits:
                    log.info("stopping after edits that did not implement the goal")
                    record_event(
                        kind="step",
                        message="Stopped because the edits did not implement the goal.",
                    )
                    return AgentResult(
                        summary="Stopped because the edits did not implement the goal.",
                        iterations=iteration,
                        tool_calls=tool_calls,
                        stopped_reason="oscillating",
                        last_error=last_error,
                    )

            if looks_unfinished(turn.text) and unfinished_nudges < MAX_UNFINISHED_NUDGES:
                unfinished_nudges += 1
                log.info("nudging agent after an unfinished reply")
                record_event(kind="step", message="Agent tried to stop mid-work; continuing.")
                messages.append({"role": "assistant", "content": turn.text or ""})
                messages.append({"role": "user", "content": _nudge(UNFINISHED_NUDGE, goal)})
                continue

            if mutated_ui and not verified_ui and validate_nudges < MAX_VALIDATE_NUDGES:
                validate_nudges += 1
                log.info("nudging agent to validate UI changes")
                record_event(kind="step", message="Agent skipped UI validation; continuing.")
                messages.append({"role": "assistant", "content": turn.text or ""})
                messages.append({"role": "user", "content": _nudge(VALIDATE_UI_NUDGE, goal)})
                continue

            if (
                require_research
                and not (looked_up and fetched_docs)
                and lookup_nudges < MAX_LOOKUP_NUDGES
            ):
                lookup_nudges += 1
                log.info("nudging agent to research the stated failure")
                record_event(
                    kind="step",
                    message="Agent skipped research for the stated failure; continuing.",
                )
                messages.append({"role": "assistant", "content": turn.text or ""})
                messages.append({"role": "user", "content": _nudge(research_nudge(goal), goal)})
                continue

            if require_docs and not wrote_agents and docs_nudges < 1:
                docs_nudges += 1
                log.info("nudging agent to record how to run this project")
                record_event(
                    kind="step",
                    message="Writing how to run and build this project into AGENTS.md.",
                )
                messages.append({"role": "assistant", "content": turn.text or ""})
                messages.append({"role": "user", "content": _nudge(DOCS_NUDGE, goal)})
                continue

            if (
                require_lookup
                and not (looked_up and fetched_docs and configured_validation)
                and lookup_nudges < MAX_LOOKUP_NUDGES
            ):
                lookup_nudges += 1
                log.info("nudging agent to look up validation commands")
                record_event(
                    kind="step",
                    message="Agent skipped docs lookup for test and preview setup; continuing.",
                )
                messages.append({"role": "assistant", "content": turn.text or ""})
                messages.append({"role": "user", "content": _nudge(LOOKUP_NUDGE, goal)})
                continue

            needs_tests = require_tests or mutated_ops
            if (
                needs_tests
                and mutated
                and not verified_tests
                and "run_tests" in known_names
                and validate_nudges < MAX_VALIDATE_NUDGES
            ):
                validate_nudges += 1
                log.info("nudging agent to run tests after edits")
                record_event(kind="step", message="Agent skipped tests; continuing.")
                messages.append({"role": "assistant", "content": turn.text or ""})
                if tests_need_command:
                    nudge = VALIDATE_TESTS_SETUP_NUDGE
                elif mutated_ops:
                    nudge = VALIDATE_OPS_NUDGE
                else:
                    nudge = VALIDATE_TESTS_NUDGE
                messages.append({"role": "user", "content": _nudge(nudge, goal)})
                continue

            summary = (turn.text or "").strip() or "Agent finished without a summary."
            messages.append({"role": "assistant", "content": summary})
            return AgentResult(
                summary=summary,
                iterations=iteration,
                tool_calls=tool_calls,
                stopped_reason="completed",
                last_error=last_error,
            )

        if mutated_ui and not verified_ui and "review_ui" in known_names:
            result = execute_tool(self.tools, "review_ui", {})
            tool_calls += 1
            log.info("tool review_ui ok=%s (end-of-run)", result.ok)
            if result.ok:
                record_event(kind="step", message="Verified rendered UI at the end of the run.")
                summary = (last_text or "").strip() or "Verified rendered UI after edits."
                return AgentResult(
                    summary=summary,
                    iterations=self.max_iterations,
                    tool_calls=tool_calls,
                    stopped_reason="completed",
                )
            record_event(
                kind="step",
                message="Rendered UI still failing at iteration limit.",
            )

        return AgentResult(
            summary="Stopped after reaching the iteration limit.",
            iterations=self.max_iterations,
            tool_calls=tool_calls,
            stopped_reason="max_iterations",
            last_error=last_error,
        )


def research_nudge(goal: str) -> str:
    needle = error_search_needle(goal)
    if not needle:
        return RESEARCH_NUDGE
    return (
        f"{RESEARCH_NUDGE} The web_search query must include this exact error "
        f'text: "{needle}". A paraphrase or a general topic does not count.'
    )


def _nudge(template: str, goal: str) -> str:
    return f"{template}\n\nGoal:\n{goal.strip()}\n\nOnly edit files that implement that goal."


def _assistant_tool_message(turn: AssistantTurn) -> dict:
    payload: dict = {"role": "assistant", "content": turn.text or None, "tool_calls": []}
    for call in turn.tool_calls:
        payload["tool_calls"].append(_openai_tool_call(call))
    return payload


def _tool_result_message(call: ToolCall, output: str, *, native: bool) -> dict:
    clipped = _clip(output)
    if native:
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": clipped,
        }
    return {
        "role": "user",
        "content": f"Tool {call.name} returned:\n{clipped}",
    }


def _openai_tool_call(call: ToolCall) -> dict:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments),
        },
    }


def _clip(text: str, limit: int = 16_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... truncated"
