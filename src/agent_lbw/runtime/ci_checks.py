"""A pull request's CI checks, and what the failed ones printed.

The checks are read from GitHub with the organization's GitHub App: check runs
(GitHub Actions and other apps) and commit statuses (older CI services) on the
pull request's head commit. A failed check's output, annotations, and the end
of its Actions log are read only when an investigation runs. They stay in
memory and are handed to the model. They are never stored in the database.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

from agent_lbw.runtime.store.github import GitHubAppError, github_api, github_download

_PULL = re.compile(r"^https://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_PASSED = frozenset({"success", "neutral", "skipped"})
_CACHE_SECONDS = 30.0
_LOG_LINES = 150
_LOG_CHARS = 12_000
_CONTEXT_CHARS = 40_000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ", re.M)

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

Api = Callable[..., object]
Download = Callable[..., str]


class ChecksError(RuntimeError):
    """GitHub could not say what the checks are."""


def pull_parts(pr_url: str) -> tuple[str, str, int] | None:
    """`owner`, `repo`, and the number of a GitHub pull request URL."""
    match = _PULL.match((pr_url or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def pull_checks(
    pr_url: str,
    token: str,
    *,
    force: bool = False,
    api: Api | None = None,
) -> dict:
    """The checks on the pull request's head commit, failed ones first.

    `state` is `failing`, `pending`, `passing`, or `none`. Cached for 30 seconds.
    """
    parts = pull_parts(pr_url)
    if parts is None:
        raise ChecksError("checks are read from GitHub pull requests only")
    api = api or github_api
    now = time.monotonic()
    with _lock:
        cached = _cache.get(pr_url)
    if cached is not None and not force and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    owner, repo, number = parts
    base = f"/repos/{quote(owner)}/{quote(repo)}"
    try:
        pull = api("GET", f"{base}/pulls/{number}", token=token)
        sha = str(((pull or {}).get("head") or {}).get("sha") or "")
        if not sha:
            raise ChecksError("GitHub did not say which commit the pull request is on")
        runs = api("GET", f"{base}/commits/{sha}/check-runs?per_page=100", token=token)
        statuses = api("GET", f"{base}/commits/{sha}/status", token=token)
    except GitHubAppError as exc:
        raise ChecksError(str(exc)) from exc
    checks = [_check_run(item) for item in (runs or {}).get("check_runs") or []]
    checks += [_status(item) for item in (statuses or {}).get("statuses") or []]
    checks = [check for check in checks if check["name"]]
    order = {"failing": 0, "pending": 1, "passing": 2}
    checks.sort(key=lambda check: (order.get(check["state"], 3), check["name"].lower()))
    result = {
        "pr_url": pr_url,
        "sha": sha,
        "state": _overall(checks),
        "checks": checks,
        "failed": [check["name"] for check in checks if check["state"] == "failing"],
    }
    with _lock:
        _cache[pr_url] = (time.monotonic(), result)
    return result


def failure_context(
    pr_url: str,
    token: str,
    names: list[str] | None = None,
    *,
    api: Api | None = None,
    download: Download | None = None,
) -> str:
    """What the failed checks printed, for the model that investigates them.

    `names` limits it to those checks. Each check gives its summary, its
    annotations, and the end of its GitHub Actions log when the app may read it.
    """
    api = api or github_api
    download = download or github_download
    found = pull_checks(pr_url, token, force=True, api=api)
    wanted = {name.strip().lower() for name in names or [] if name.strip()}
    failed = [
        check
        for check in found["checks"]
        if check["state"] == "failing" and (not wanted or check["name"].lower() in wanted)
    ]
    if not failed:
        return ""
    owner, repo, _number = pull_parts(pr_url) or ("", "", 0)
    base = f"/repos/{quote(owner)}/{quote(repo)}"
    parts = [
        f"These CI checks failed on {pr_url} at commit {found['sha'][:12]}. "
        "Find why each one failed, fix the cause in the code, and keep the change "
        "to what the failures need. Do not turn a check off or skip a test to make "
        "it pass."
    ]
    for check in failed:
        lines = [f"## {check['name']} ({check['conclusion'] or 'failed'})"]
        if check["url"]:
            lines.append(f"Details: {check['url']}")
        if check["summary"]:
            lines.append(check["summary"])
        if check["kind"] == "check_run":
            lines.extend(_annotations(base, check["id"], token, api))
            if check["app"] == "github-actions":
                lines.append(_log_tail(base, check["id"], token, download))
        parts.append("\n".join(line for line in lines if line))
    text = "\n\n".join(parts)
    if len(text) > _CONTEXT_CHARS:
        text = text[:_CONTEXT_CHARS] + "\n[LBW shortened the CI output here]"
    return text


def investigation_goal(pr_url: str, names: list[str]) -> str:
    """The request an investigation run records. It names the checks, not their logs."""
    listed = ", ".join(names) if names else "the failing checks"
    parts = pull_parts(pr_url)
    where = f"PR #{parts[2]}" if parts else "the pull request"
    return (
        f"Fix failing CI checks on {where}: {listed}\n\n"
        f"Pull request: {pr_url}\n"
        "Find why each check failed and fix the cause. Keep the change to what "
        "the failures need."
    )


def _check_run(item: dict) -> dict:
    status = str(item.get("status") or "")
    conclusion = str(item.get("conclusion") or "")
    if status != "completed":
        state = "pending"
    elif conclusion in _FAILED:
        state = "failing"
    elif conclusion in _PASSED:
        state = "passing"
    else:
        state = "pending"
    output = item.get("output") or {}
    summary = " ".join(
        part.strip()
        for part in (str(output.get("title") or ""), str(output.get("summary") or ""))
        if part and part.strip()
    )
    app = (item.get("app") or {}).get("slug") or ""
    return {
        "id": int(item.get("id") or 0),
        "kind": "check_run",
        "name": str(item.get("name") or ""),
        "state": state,
        "conclusion": conclusion,
        "url": str(item.get("html_url") or item.get("details_url") or ""),
        "summary": summary[:600],
        "app": str(app),
    }


def _status(item: dict) -> dict:
    raw = str(item.get("state") or "")
    state = {"success": "passing", "failure": "failing", "error": "failing"}.get(raw, "pending")
    return {
        "id": int(item.get("id") or 0),
        "kind": "status",
        "name": str(item.get("context") or ""),
        "state": state,
        "conclusion": raw,
        "url": str(item.get("target_url") or ""),
        "summary": str(item.get("description") or "")[:600],
        "app": "",
    }


def _overall(checks: list[dict]) -> str:
    states = {check["state"] for check in checks}
    if not states:
        return "none"
    if "failing" in states:
        return "failing"
    if "pending" in states:
        return "pending"
    return "passing"


def _annotations(base: str, run_id: int, token: str, api: Api) -> list[str]:
    try:
        items = api("GET", f"{base}/check-runs/{run_id}/annotations?per_page=30", token=token)
    except GitHubAppError:
        return []
    lines = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        where = str(item.get("path") or "")
        line = item.get("start_line")
        if where and line:
            where = f"{where}:{line}"
        message = " ".join(str(item.get("message") or "").split())[:400]
        if message:
            lines.append(f"- {where}: {message}" if where else f"- {message}")
    return lines


def _log_tail(base: str, job_id: int, token: str, download: Download) -> str:
    try:
        raw = download(f"{base}/actions/jobs/{job_id}/logs", token=token)
    except GitHubAppError as exc:
        return f"(The log was not read: {exc})"
    clean = _STAMP.sub("", _ANSI.sub("", raw))
    # GitHub's own cleanup steps end every Actions log. The failure is above them.
    clean = clean.split("Post job cleanup.", 1)[0]
    lines = clean.splitlines()
    tail = "\n".join(lines[-_LOG_LINES:])
    if len(tail) > _LOG_CHARS:
        tail = tail[-_LOG_CHARS:]
    shortened = len(lines) > _LOG_LINES or len(clean) > len(tail)
    note = "[LBW shortened this log to its last lines]\n" if shortened else ""
    return f"Log:\n{note}{tail}"
