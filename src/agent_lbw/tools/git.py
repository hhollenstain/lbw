from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult, ToolSpec, object_schema
from agent_lbw.tools.files import is_probably_secret_path

SECRET_REFUSAL = "refusing to commit likely secrets: {paths}"
PROTECTED_COMMIT_REFUSAL = "refusing to commit on {branch}; the cycle commits after review passes"
AGENT_COMMIT_REFUSAL = (
    "refusing to commit during the run; leave the files in the tree. "
    "The cycle makes one commit after review. Do not retry git_commit."
)
MAX_PUBLISHED_COMMITS = 3
CONNECT_GITHUB = "Connect GitHub before committing to this repository."
APPROVE_GITHUB = "Connect GitHub before approving a commit."
_COMMITTER: ContextVar[tuple[str, str] | None] = ContextVar("lbw_committer", default=None)
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk"})
LBW_COAUTHOR_NAME = "agent-lbw"
LBW_COAUTHOR_EMAIL = "agent-lbw@users.noreply.github.com"
CO_AUTHORED_BY = f"Co-authored-by: {LBW_COAUTHOR_NAME} <{LBW_COAUTHOR_EMAIL}>"
_PR_URL_RE = re.compile(
    r"https://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/pull/\d+"
    r"|https://[^\s<>\"']+/(?:pull|merge_requests)/\d+",
    re.IGNORECASE,
)


def with_lbw_coauthor(message: str) -> str:
    """Keep a one-line subject and credit agent-lbw the way GitHub expects."""
    subject = " ".join((message or "").split()).strip()
    if not subject:
        return ""
    if "co-authored-by: agent-lbw" in (message or "").lower():
        return message.strip()
    return f"{subject}\n\n{CO_AUTHORED_BY}"


def pull_request_state(url: str, *, token: str | None = None) -> str:
    """Return open, closed, or merged for a pull-request URL. Empty when unknown."""
    target = extract_pr_url(url)
    if not target:
        return ""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", target, "--json", "state"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=_gh_env(token),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    state = str(payload.get("state") or "").strip().lower()
    if state in {"open", "closed", "merged"}:
        return state
    return ""


def extract_pr_url(text: str | None) -> str | None:
    """Return the first GitHub/GitLab-style pull-request URL in command output."""
    if not text:
        return None
    match = _PR_URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(").,;")


_GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/]|github\.com/)(?P<owner>[^/]+)/(?P<repo>[^/#?\s]+)",
    re.IGNORECASE,
)


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) from an origin-style GitHub remote URL."""
    match = _GITHUB_REMOTE_RE.search((url or "").strip())
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo").rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return owner, repo


def github_https_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def github_auth_args(token: str) -> list[str]:
    """Ask git to send the installation token without putting it in the remote URL."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def use_committer(name: str | None, email: str | None) -> Token[tuple[str, str] | None]:
    """Remember who approved this run. The worker thread commits as that person."""
    if name and email:
        return _COMMITTER.set((name, email))
    return _COMMITTER.set(None)


def reset_committer(token: Token[tuple[str, str] | None]) -> None:
    _COMMITTER.reset(token)


def github_owner_repo(
    workspace: Workspace,
    remote: str = "origin",
) -> tuple[str, str] | None:
    result = run_git(workspace, ["remote", "get-url", remote or "origin"])
    if result.returncode != 0:
        return None
    return parse_github_remote(result.stdout.strip())


def is_tracked(workspace: Workspace, path: Path | str) -> bool:
    candidate = Path(path)
    root = workspace.root.resolve()
    try:
        if candidate.is_absolute():
            rel = candidate.resolve().relative_to(root).as_posix()
        else:
            rel = _posix_rel(candidate)
    except (OSError, ValueError):
        return False
    if not rel:
        return False
    result = run_git(workspace, ["ls-files", "--error-unmatch", "--", rel])
    return result.returncode == 0


@dataclass(frozen=True)
class UpstreamState:
    branch: str | None
    tracking: str | None
    ahead: int
    behind: int
    pushed: bool
    detail: str


def git_tools(
    workspace: Workspace,
    author_name: str | None,
    author_email: str | None,
    *,
    allow_publish: bool = False,
    store_root: Path | None = None,
    database_url: str = "",
) -> list[ToolSpec]:
    env_extras = _git_identity_env(author_name, author_email)
    tools = [
        ToolSpec(
            name="git_status",
            description="Show git status and the current branch for the workspace.",
            parameters=object_schema({}),
            handler=lambda: _git_status(workspace),
        ),
        ToolSpec(
            name="git_diff",
            description=(
                "Show the unstaged and staged diff. Optionally limit to one path. "
                "Lockfiles are summarized as package version changes."
            ),
            parameters=object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "Optional workspace-relative path to diff.",
                    }
                }
            ),
            handler=lambda path=None: _git_diff(workspace, path),
        ),
        ToolSpec(
            name="git_log",
            description="Show recent commit subjects.",
            parameters=object_schema(
                {
                    "limit": {
                        "type": "integer",
                        "description": "Number of commits to show. Default 8.",
                    }
                }
            ),
            handler=lambda limit=8: _git_log(workspace, int(limit)),
        ),
        ToolSpec(
            name="git_commit",
            description=(
                "Refuses. Do not commit each step. The cycle commits the finished change "
                "once after review. Leave edits in the working tree."
            ),
            parameters=object_schema(
                {
                    "message": {
                        "type": "string",
                        "description": "Concise commit message explaining why the change exists.",
                    }
                },
                ["message"],
            ),
            handler=lambda message: agent_commit(
                workspace,
                message,
                env_extras,
                store_root=store_root,
                database_url=database_url,
            ),
        ),
    ]
    if allow_publish:
        tools.append(
            ToolSpec(
                name="git_push",
                description="Push the current feature branch. Refuses main/master.",
                parameters=object_schema(
                    {
                        "remote": {
                            "type": "string",
                            "description": "Remote name. Default origin.",
                        },
                        "branch": {
                            "type": "string",
                            "description": "Optional remote branch. Default: current branch.",
                        },
                    }
                ),
                handler=lambda remote="origin", branch=None: push_changes(
                    workspace,
                    remote,
                    branch,
                    token=_publish_token(store_root, database_url),
                ),
            )
        )
    return tools


def _commit_author_env(
    workspace: Workspace,
    env: dict[str, str] | None,
    *,
    store_root: Path | None,
    database_url: str,
) -> dict[str, str] | None:
    """Author env for this commit. None when a GitHub repo has no connected person."""
    provided = dict(env or {})
    if github_owner_repo(workspace) is None:
        return provided
    stamped = _COMMITTER.get()
    if stamped and stamped[0] and stamped[1]:
        return {**provided, **_git_identity_env(stamped[0], stamped[1])}
    if store_root is not None:
        from agent_lbw.runtime.db import database_configured
        from agent_lbw.runtime.store.install import commit_identity_for

        if database_configured(Path(store_root), database_url):
            identity = commit_identity_for(Path(store_root), database_url)
            if identity is None:
                return None
            return {**provided, **_git_identity_env(identity[0], identity[1])}
    return provided


def _git_identity_env(name: str | None, email: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if name:
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_COMMITTER_NAME"] = name
    if email:
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_EMAIL"] = email
    return env


def _publish_token(store_root: Path | None, database_url: str) -> str | None:
    if store_root is None:
        return None
    from agent_lbw.runtime.store.github import current_token

    return current_token(Path(store_root), database_url)


def _gh_env(token: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return env


def _gh(
    workspace: Workspace,
    args: list[str],
    token: str | None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=workspace.root,
            check=False,
            capture_output=True,
            text=True,
            env=_gh_env(token),
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args,
            124,
            stdout=_timeout_text(exc.stdout),
            stderr=_timeout_text(exc.stderr) or "timed out",
        )


def run_git(
    workspace: Workspace,
    args: list[str],
    env: dict[str, str] | None = None,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace.root,
            check=False,
            capture_output=True,
            text=True,
            env=_merged_env(env),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            ["git", *args],
            124,
            stdout=_timeout_text(exc.stdout),
            stderr=_timeout_text(exc.stderr) or "git timed out",
        )


def _merged_env(extra: dict[str, str] | None) -> dict[str, str]:
    merged = os.environ.copy()
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged["GCM_INTERACTIVE"] = "Never"
    if extra:
        merged.update(extra)
    return merged


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return value.strip()


def _output(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return err or text or f"git exited {result.returncode}"
    return text or "(clean)"


def _git_status(workspace: Workspace) -> ToolResult:
    if not (workspace.root / ".git").exists():
        return ToolResult(False, "not a git repository")
    result = run_git(workspace, ["status", "--short", "--branch"])
    return ToolResult(result.returncode == 0, _output(result))


def _git_diff(workspace: Workspace, path: str | None) -> ToolResult:
    from agent_lbw.runtime.workdiff import (
        collect_work_diff,
        is_lockfile,
        summarize_lockfile_pair,
    )

    if path and is_lockfile(path):
        old = run_git(workspace, ["show", f"HEAD:{path}"])
        new_path = workspace.root / path
        try:
            new = new_path.read_text(encoding="utf-8") if new_path.is_file() else ""
        except (OSError, UnicodeDecodeError):
            new = ""
        summary = summarize_lockfile_pair(
            path,
            old.stdout if old.returncode == 0 else "",
            new,
        )
        return ToolResult(True, summary or "(no diff)")
    if path:
        result = run_git(workspace, ["diff", "HEAD", "--", str(workspace.resolve(path))])
        if result.returncode != 0:
            return ToolResult(False, _output(result))
        return ToolResult(True, result.stdout.strip() or "(no diff)")
    return ToolResult(True, collect_work_diff(workspace, None) or "(no diff)")


def _git_log(workspace: Workspace, limit: int) -> ToolResult:
    result = run_git(workspace, ["log", f"-{max(1, min(limit, 30))}", "--oneline"])
    return ToolResult(result.returncode == 0, _output(result))


_TOOL_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def is_runtime_artifact(path: Path | str) -> bool:
    """Local lbw state, cycle logs, and caches left by the test command are not project work."""
    posix = _posix_rel(path)
    if posix == "history.json":
        return True
    if posix in {".lbw", ".loco"} or posix.startswith(".lbw/") or posix.startswith(".loco/"):
        return True
    parts = posix.split("/")
    if any(part in _TOOL_CACHE_DIRS for part in parts):
        return True
    return parts[-1].endswith((".pyc", ".pyo"))


def has_changes(workspace: Workspace) -> bool:
    return any(_is_project_change(path) for path in _changed_paths(workspace))


def changed_paths(workspace: Workspace, sha_before: str | None = None) -> list[str]:
    """Project files this cycle changed: commits after sha_before plus the working tree."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        path = _posix_rel(raw)
        if not path or path in seen or not _is_project_change(path):
            return
        seen.add(path)
        found.append(path)

    if sha_before:
        committed = run_git(workspace, ["diff", "--name-only", sha_before, "HEAD"])
        if committed.returncode == 0:
            for line in committed.stdout.splitlines():
                add(line)
    for path in _changed_paths(workspace):
        add(path.as_posix())
    return found


def _posix_rel(path: Path | str) -> str:
    posix = Path(str(path).strip().strip('"')).as_posix()
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def _is_project_change(path: Path | str) -> bool:
    return not is_runtime_artifact(path)


def current_branch(workspace: Workspace) -> str | None:
    result = run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def is_protected_branch(name: str | None) -> bool:
    if not name:
        return False
    return name.lower() in PROTECTED_BRANCHES


def branch_exists(workspace: Workspace, name: str) -> bool:
    if not name:
        return False
    result = run_git(workspace, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"])
    return result.returncode == 0


def resume_workspace(
    workspace: Workspace,
    branch: str | None = None,
    sha: str | None = None,
) -> ToolResult:
    """Check out a failed run's feature branch so a rerun can continue that work."""
    target = (branch or "").strip()
    if target and is_protected_branch(target):
        return ToolResult(False, f"refusing to resume on protected branch {target}")
    if target and target != current_branch(workspace):
        if not branch_exists(workspace, target):
            return ToolResult(False, f"branch not found: {target}")
        checked = run_git(workspace, ["checkout", target])
        if checked.returncode != 0:
            return ToolResult(False, _output(checked))
    current = current_branch(workspace)
    sha_value = (sha or "").strip()
    if sha_value:
        head = current_sha(workspace)
        if head and head != sha_value:
            # Keep uncommitted work; only move HEAD when the tree is clean enough.
            status = run_git(workspace, ["status", "--porcelain"])
            dirty = bool((status.stdout or "").strip())
            if not dirty:
                moved = run_git(workspace, ["checkout", sha_value])
                if moved.returncode != 0:
                    return ToolResult(False, _output(moved))
                if current and not is_protected_branch(current):
                    run_git(workspace, ["checkout", "-B", current])
    return ToolResult(True, current_branch(workspace) or target or "")


_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_GOAL_BRANCH = re.compile(
    r"(?ix)"
    r"(?:"
    r"\b(?:base\s+branch|on\s+branch|from\s+branch|use\s+branch)\s*[:=]?\s*"
    r"|\bbranch\s*[:=]\s*"
    r")"
    r"""[`"']?(?P<name>[A-Za-z0-9][A-Za-z0-9._/-]*)[`"']?"""
)
_BRANCH_STOP_WORDS = {"the", "this", "that", "a", "an", "head", "current", "default", "same"}
_KEEP_CURRENT_BRANCH = re.compile(r"(?i)\b(?:current|this|same) branch\b")


def valid_branch_name(name: str) -> bool:
    if not name or name.upper() == "HEAD" or name.startswith("-") or ".." in name or "@{" in name:
        return False
    return bool(_BRANCH_NAME.fullmatch(name))


def goal_keeps_current_branch(goal: str | None) -> bool:
    """True when the goal says to continue on the branch that is already checked out."""
    return bool(goal and _KEEP_CURRENT_BRANCH.search(goal))


def branch_named_in_goal(goal: str | None) -> str | None:
    """Return a branch the goal text asks to start from."""
    if not goal:
        return None
    for match in _GOAL_BRANCH.finditer(goal):
        name = match.group("name")
        if name.lower() in _BRANCH_STOP_WORDS or not valid_branch_name(name):
            continue
        return name
    return None


def list_branches(workspace: Workspace) -> tuple[str, list[str]]:
    """Default head branch, then every local and origin branch."""
    default = default_base_branch(workspace)
    listed = run_git(
        workspace,
        ["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin"],
    )
    names: list[str] = []
    seen: set[str] = set()
    for raw in (listed.stdout or "").splitlines():
        ref = raw.strip()
        if ref.startswith("refs/heads/"):
            name = ref.removeprefix("refs/heads/")
        elif ref.startswith("refs/remotes/origin/"):
            name = ref.removeprefix("refs/remotes/origin/")
            if name == "HEAD":
                continue
        else:
            continue
        if not valid_branch_name(name) or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if default and default not in seen and valid_branch_name(default):
        names.append(default)
    names.sort(key=lambda item: (item != default, item.lower()))
    return default, names


def prepare_clean_base(
    workspace: Workspace,
    branch: str,
    remote: str = "origin",
) -> ToolResult:
    """Check out `branch` at its remote head, stashing local edits first."""
    if not (workspace.root / ".git").exists():
        return ToolResult(True, "not a git repository")
    target = (branch or "").strip()
    if not valid_branch_name(target):
        return ToolResult(False, f"invalid branch name: {target or '(empty)'}")
    dirty = run_git(workspace, ["status", "--porcelain"])
    if dirty.returncode != 0:
        return ToolResult(False, _output(dirty))
    if (dirty.stdout or "").strip():
        stashed = run_git(
            workspace,
            ["stash", "push", "-m", "lbw: stashed before a clean start"],
        )
        if stashed.returncode != 0:
            return ToolResult(False, _output(stashed))
    _fetch_branch(workspace, remote or "origin", target)
    remote_name = remote or "origin"
    remote_ref = f"{remote_name}/{target}"
    has_remote = (
        run_git(workspace, ["rev-parse", "--verify", "--quiet", remote_ref]).returncode == 0
    )
    has_local = (
        run_git(
            workspace,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{target}"],
        ).returncode
        == 0
    )
    if has_remote:
        checked = run_git(workspace, ["checkout", "-B", target, remote_ref])
        if checked.returncode != 0:
            return ToolResult(False, _output(checked))
        return ToolResult(True, f"clean {target} at {remote_ref}")
    if has_local:
        checked = run_git(workspace, ["checkout", target])
        if checked.returncode != 0:
            return ToolResult(False, _output(checked))
        return ToolResult(True, f"clean {target}")
    return ToolResult(False, f"branch not found: {target}")


def _fetch_branch(workspace: Workspace, remote: str, branch: str) -> None:
    import os

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", remote, branch],
            cwd=workspace.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def default_base_branch(
    workspace: Workspace,
    configured: str | None = None,
    *,
    avoid: str | None = None,
) -> str:
    """The branch a change merges into.

    A configured name wins. Otherwise the local or origin ``main`` or
    ``master``, then ``origin/HEAD``. ``avoid`` is the branch being published,
    so the result is not that branch.
    """
    skip = (avoid or "").strip()
    chosen = (configured or "").strip()
    if chosen and chosen != skip:
        return chosen
    for candidate in ("main", "master"):
        if candidate == skip:
            continue
        for ref in (f"refs/heads/{candidate}", f"refs/remotes/origin/{candidate}"):
            exists = run_git(workspace, ["show-ref", "--verify", "--quiet", ref])
            if exists.returncode == 0:
                return candidate
    symbolic = run_git(workspace, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if symbolic.returncode == 0:
        name = symbolic.stdout.strip().removeprefix("origin/")
        if name and name != skip and valid_branch_name(name):
            return name
    current = current_branch(workspace) or "main"
    if current != skip:
        return current
    return "main"


def intended_pr_base(
    workspace: Workspace,
    start: str | None,
    configured: str | None,
) -> str:
    """The branch this run's pull request targets.

    The start branch is the one the run checked out. It stays the target even
    when the commits are still sitting on it; the caller moves those commits
    onto a new branch first.
    """
    for candidate in (start, configured):
        name = (candidate or "").strip()
        if name:
            return name
    return default_base_branch(workspace)


def _branch_slug(goal: str) -> str:
    from agent_lbw.runtime.importer import goal_headline

    first = goal_headline(goal)
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return slug[:40].strip("-") or "change"


def ensure_pr_branch(
    workspace: Workspace,
    goal: str,
    sha_before: str | None,
    *,
    base: str | None = None,
) -> ToolResult:
    """Put this cycle's work on a lbw/* branch.

    A protected branch, and the branch the run started from, keep the commit
    they had before this work. The new commits stay on the lbw/* branch so a
    pull request is not opened from a branch into itself.
    """
    current = current_branch(workspace)
    started = (base or "").strip()
    leave = is_protected_branch(current) or bool(started and current == started)
    if current and current != "HEAD" and not leave:
        return ToolResult(True, current)
    name = f"lbw/{_branch_slug(goal)}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    created = run_git(workspace, ["checkout", "-b", name])
    if created.returncode != 0:
        return ToolResult(False, _output(created))
    if current and sha_before and leave:
        moved = run_git(workspace, ["branch", "-f", current, sha_before])
        if moved.returncode != 0:
            return ToolResult(False, _output(moved))
    return ToolResult(True, name)


def move_commits_off_base(
    workspace: Workspace,
    goal: str,
    base: str,
    rewind_sha: str | None,
) -> ToolResult:
    """Move this run's commits off `base` onto a new branch.

    The base ref goes back to the commit from before the run, or to
    ``origin/<base>`` when that ref is behind. A base that already matches
    the remote has nothing left to open a pull request for.
    """
    current = current_branch(workspace)
    if not current or current != base:
        return ToolResult(True, current or "")
    rewind = (rewind_sha or "").strip()
    head = current_sha(workspace)
    if not rewind or rewind == head:
        remote = run_git(workspace, ["rev-parse", "--verify", f"origin/{base}"])
        if remote.returncode == 0:
            rewind = remote.stdout.strip()
    if not rewind or rewind == head:
        return ToolResult(
            False,
            f"the commits are already on {base}, so a pull request from that "
            "branch has nothing to merge",
        )
    return ensure_pr_branch(workspace, goal, rewind, base=base)


def current_sha(workspace: Workspace) -> str | None:
    result = run_git(workspace, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def upstream_state(workspace: Workspace, remote: str = "origin") -> UpstreamState:
    """Whether HEAD is already on the tracked remote branch."""
    branch = current_branch(workspace)
    tracking = _tracking_ref(workspace, remote, branch)
    if not tracking:
        where = branch or "HEAD"
        return UpstreamState(
            branch=branch,
            tracking=None,
            ahead=0,
            behind=0,
            pushed=False,
            detail=f"HEAD is on {where}; no upstream tracking branch (not pushed).",
        )
    ahead, behind = _ahead_behind(workspace, tracking)
    pushed = ahead == 0
    if pushed and behind == 0:
        detail = f"HEAD is on {branch} and matches {tracking} (pushed)."
    elif pushed and behind > 0:
        detail = (
            f"HEAD is on {branch} and is contained in {tracking} "
            f"(pushed; {behind} remote commit(s) not in HEAD)."
        )
    elif behind == 0:
        detail = f"HEAD is on {branch}, {ahead} local commit(s) ahead of {tracking} (not pushed)."
    else:
        detail = (
            f"HEAD is on {branch}, diverged from {tracking} "
            f"(ahead {ahead}, behind {behind}; not pushed)."
        )
    return UpstreamState(
        branch=branch,
        tracking=tracking,
        ahead=ahead,
        behind=behind,
        pushed=pushed,
        detail=detail,
    )


def _tracking_ref(workspace: Workspace, remote: str, branch: str | None) -> str | None:
    configured = run_git(
        workspace,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
    )
    if configured.returncode == 0 and configured.stdout.strip():
        return configured.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    candidate = f"{remote}/{branch}"
    exists = run_git(
        workspace,
        ["show-ref", "--verify", "--quiet", f"refs/remotes/{candidate}"],
    )
    if exists.returncode == 0:
        return candidate
    return None


def _ahead_behind(workspace: Workspace, tracking: str) -> tuple[int, int]:
    result = run_git(workspace, ["rev-list", "--left-right", "--count", f"{tracking}...HEAD"])
    if result.returncode != 0:
        return 0, 0
    parts = result.stdout.strip().split()
    if len(parts) < 2:
        return 0, 0
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return 0, 0
    return ahead, behind


def agent_commit(
    workspace: Workspace,
    message: str,
    env: dict[str, str] | None = None,
    *,
    store_root: Path | None = None,
    database_url: str = "",
) -> ToolResult:
    """Refuse. The cycle commits once after review, so a run is not one commit per step."""
    return ToolResult(False, AGENT_COMMIT_REFUSAL)


def commit_changes(
    workspace: Workspace,
    message: str,
    env: dict[str, str] | None = None,
    extra_paths: list[Path | str] | None = None,
    *,
    store_root: Path | None = None,
    database_url: str = "",
) -> ToolResult:
    message = with_lbw_coauthor(message)
    if not message:
        return ToolResult(False, "commit message is required")
    if not (workspace.root / ".git").exists():
        return ToolResult(False, "not a git repository")

    secrets = _secret_changes(workspace)
    if secrets:
        return ToolResult(False, SECRET_REFUSAL.format(paths=", ".join(secrets)))

    add = run_git(workspace, ["add", "-A"])
    if add.returncode != 0:
        return ToolResult(False, _output(add))
    _unstage_runtime_artifacts(workspace)
    _force_add_paths(workspace, extra_paths)

    leftover_secrets = _staged_secrets(workspace)
    if leftover_secrets:
        run_git(workspace, ["reset", "HEAD", "--", *leftover_secrets])
        return ToolResult(False, SECRET_REFUSAL.format(paths=", ".join(leftover_secrets)))

    author_env = _commit_author_env(
        workspace,
        env,
        store_root=store_root,
        database_url=database_url,
    )
    if author_env is None:
        return ToolResult(False, CONNECT_GITHUB)
    commit = run_git(workspace, ["commit", "-m", message], env=author_env)
    if commit.returncode != 0:
        return ToolResult(False, _output(commit))
    sha = current_sha(workspace) or ""
    return ToolResult(True, f"committed {sha}: {message}")


def _resolved_base_ref(workspace: Workspace, base: str | None) -> str | None:
    target = (base or "").strip() or default_base_branch(workspace)
    for ref in (f"origin/{target}", target):
        exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", ref])
        if exists.returncode == 0:
            return ref
    return None


def fold_unpublished_commits(
    workspace: Workspace,
    base: str | None,
    message: str,
    env: dict[str, str] | None = None,
    *,
    store_root: Path | None = None,
    database_url: str = "",
) -> ToolResult:
    """Fold a long unpublished run into one commit before the first push.

    A branch already on the remote is left as it is. Published history is not
    rewritten.
    """
    branch = current_branch(workspace)
    if not branch or is_protected_branch(branch):
        return ToolResult(True, "left history on a protected branch")
    ref = _resolved_base_ref(workspace, base)
    if ref is None:
        return ToolResult(True, "no base branch to fold onto")
    ahead = commits_ahead_of_base(workspace, (base or "").strip() or None)
    if ahead <= MAX_PUBLISHED_COMMITS:
        return ToolResult(True, f"{ahead} commit(s) on {branch}")
    remote = run_git(workspace, ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"])
    if remote.returncode == 0:
        base_sha = run_git(workspace, ["rev-parse", ref])
        if base_sha.returncode != 0 or remote.stdout.strip() != base_sha.stdout.strip():
            return ToolResult(True, f"left {ahead} commits; {branch} is already on the remote")
    reset = run_git(workspace, ["reset", "--soft", ref])
    if reset.returncode != 0:
        return ToolResult(False, _output(reset))
    committed = commit_changes(
        workspace,
        message,
        env,
        store_root=store_root,
        database_url=database_url,
    )
    if not committed.ok:
        return committed
    return ToolResult(True, f"folded {ahead} commits into one on {branch}")


def push_changes(
    workspace: Workspace,
    remote: str = "origin",
    branch: str | None = None,
    token: str | None = None,
) -> ToolResult:
    if not remote:
        remote = "origin"
    target = (branch or current_branch(workspace) or "").strip()
    if not target or target == "HEAD":
        return ToolResult(False, "no branch to push")
    if is_protected_branch(target):
        return ToolResult(False, f"refusing to push directly to {target}")
    if token:
        parsed = github_owner_repo(workspace, remote)
        if parsed is None:
            return ToolResult(False, "the GitHub App can push a GitHub remote")
        url = github_https_url(*parsed)
        result = run_git(
            workspace,
            [*github_auth_args(token), "push", url, f"HEAD:{target}"],
            timeout=120,
        )
    else:
        result = run_git(
            workspace,
            ["push", "-u", remote, f"HEAD:{target}"],
            timeout=120,
        )
    if result.returncode != 0:
        return ToolResult(False, _output(result))
    _remember_pushed_branch(workspace, remote, target)
    return ToolResult(True, _output(result))


def _remember_pushed_branch(workspace: Workspace, remote: str, branch: str) -> None:
    """Record the branch that was just pushed so the pull request can use it."""
    tracking = f"{remote}/{branch}"
    ref = f"refs/remotes/{tracking}"
    exists = run_git(workspace, ["show-ref", "--verify", "--quiet", ref])
    if exists.returncode != 0:
        run_git(workspace, ["update-ref", ref, "HEAD"])
    run_git(workspace, ["branch", f"--set-upstream-to={tracking}"])


def create_pull_request(
    workspace: Workspace,
    title: str,
    body: str,
    *,
    base: str | None = None,
    token: str | None = None,
) -> ToolResult:
    """Open a pull request for a branch that has already been pushed.

    ``--head`` names that branch. Without it, ``gh`` stops when the local
    branch has no upstream, even after a successful push.
    """
    title = " ".join(title.split()).strip() or "lbw changes"
    head = current_branch(workspace)
    if is_protected_branch(head):
        return ToolResult(False, f"refusing to open a PR from {head}")
    if not head:
        return ToolResult(False, "no branch to publish")
    if base and base.strip() == head:
        return ToolResult(
            False,
            f"pull request base {head} is the same branch as the commits",
        )
    body = (body or title).rstrip()
    if "co-authored-by: agent-lbw" not in body.lower():
        body = f"{body}\n\n{CO_AUTHORED_BY}"
    args = ["gh", "pr", "create", "--head", head, "--title", title, "--body", body]
    if base:
        args.extend(["--base", base])
    result = _gh(workspace, args, token)
    output = (result.stdout or result.stderr).strip()
    if result.returncode == 0:
        return ToolResult(True, output)
    if extract_pr_url(output):
        return ToolResult(True, output)
    return ToolResult(False, output)


def merge_pull_request(
    workspace: Workspace,
    url: str | None = None,
    *,
    squash: bool = True,
    token: str | None = None,
) -> ToolResult:
    """Squash-merge a GitHub pull request after cycle review. Does not push to main."""
    args = ["gh", "pr", "merge"]
    if url:
        args.append(url)
    args.append("--squash" if squash else "--merge")
    args.append("--delete-branch")
    try:
        result = _gh(workspace, args, token)
    except FileNotFoundError:
        return ToolResult(False, "gh not found")
    output = (result.stdout or result.stderr).strip()
    return ToolResult(result.returncode == 0, output or "merged pull request")


def checkout_base_branch(
    workspace: Workspace,
    base: str,
    remote: str = "origin",
    *,
    token: str | None = None,
) -> ToolResult:
    """Return HEAD to the default branch after a merge so the next cycle starts clean."""
    target = (base or "").strip() or "main"
    switched = run_git(workspace, ["checkout", target])
    if switched.returncode != 0:
        return ToolResult(False, _output(switched))
    if token:
        parsed = github_owner_repo(workspace, remote or "origin")
        if parsed is None:
            return ToolResult(True, f"checked out {target}; pull: not a GitHub remote")
        pulled = run_git(
            workspace,
            [*github_auth_args(token), "pull", "--ff-only", github_https_url(*parsed), target],
        )
    else:
        pulled = run_git(workspace, ["pull", "--ff-only", remote or "origin", target])
    if pulled.returncode != 0:
        return ToolResult(True, f"checked out {target}; pull: {_output(pulled)}")
    return ToolResult(True, f"checked out {target}")


def update_pull_request(
    workspace: Workspace,
    title: str,
    body: str,
    *,
    token: str | None = None,
) -> ToolResult:
    """Refresh the open pull request on the current branch."""
    title = " ".join(title.split()).strip() or "lbw changes"
    body = (body or title).rstrip()
    if "co-authored-by: agent-lbw" not in body.lower():
        body = f"{body}\n\n{CO_AUTHORED_BY}"
    args = ["gh", "pr", "edit", "--title", title, "--body", body]
    try:
        result = _gh(workspace, args, token)
    except FileNotFoundError:
        return ToolResult(False, "gh not found")
    output = (result.stdout or result.stderr).strip()
    return ToolResult(result.returncode == 0, output or "updated pull request")


def existing_pull_request(workspace: Workspace, *, token: str | None = None) -> str | None:
    """URL of the open PR for the current branch, if one already exists."""
    try:
        result = _gh(workspace, ["gh", "pr", "view", "--json", "url"], token)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return extract_pr_url((result.stdout or result.stderr).strip())
    try:
        data = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return extract_pr_url(result.stdout)
    if not isinstance(data, dict):
        return extract_pr_url(result.stdout)
    url = str(data.get("url") or "").strip()
    return url or extract_pr_url(result.stdout)


def commits_ahead_of_base(workspace: Workspace, base: str | None = None) -> int:
    """How many commits HEAD has that are not on the default base branch."""
    target = (base or default_base_branch(workspace)).strip() or "main"
    for ref in (f"origin/{target}", target):
        exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", ref])
        if exists.returncode != 0:
            continue
        counted = run_git(workspace, ["rev-list", "--count", f"{ref}..HEAD"])
        if counted.returncode != 0:
            continue
        try:
            return int(counted.stdout.strip() or "0")
        except ValueError:
            return 0
    return 0


def _unstage_runtime_artifacts(workspace: Workspace) -> None:
    result = run_git(workspace, ["diff", "--cached", "--name-only", "-z"])
    if result.returncode != 0 or not result.stdout:
        return
    runtime = [path for path in result.stdout.split("\0") if path and is_runtime_artifact(path)]
    if runtime:
        run_git(workspace, ["reset", "HEAD", "--", *runtime])


def _force_add_paths(workspace: Workspace, extra_paths: list[Path | str] | None) -> None:
    """Stage gitignored files that should ship with a PR, such as one UI screenshot."""
    root = workspace.root.resolve()
    for raw in extra_paths or []:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            path = candidate.resolve()
            rel = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        if is_runtime_artifact(rel):
            continue
        run_git(workspace, ["add", "-f", "--", rel.as_posix()])


def _changed_paths(workspace: Workspace) -> list[Path]:
    result = run_git(workspace, ["status", "--porcelain", "-uall"])
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        if raw:
            paths.append(Path(raw))
    return paths


def _secret_changes(workspace: Workspace) -> list[str]:
    return [path.as_posix() for path in _changed_paths(workspace) if is_probably_secret_path(path)]


def _staged_secrets(workspace: Workspace) -> list[str]:
    result = run_git(workspace, ["diff", "--cached", "--name-only"])
    if result.returncode != 0:
        return []
    secrets: list[str] = []
    for line in result.stdout.splitlines():
        if is_probably_secret_path(Path(line.strip())):
            secrets.append(line.strip())
    return secrets
