"""Merge a pull request's base branch into it, so GitHub can merge it again.

GitHub reports a conflict when the base branch moved under an open pull
request. The fix is a real merge commit pushed on top of the branch: the
pull request's history is not rewritten, and nothing is force-pushed.

A merge git completes on its own is pushed at once, as GitHub's Update branch
does. When files conflict, a model resolves them: the person's runner, or
LBW's own coding agent. LBW then checks that no conflict markers are left,
writes the merge commit itself with both parents, and waits for approval
before pushing it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_lbw.agent.loop import CodingAgent
from agent_lbw.config import Settings
from agent_lbw.llm.client import LLMClient
from agent_lbw.progress import record_event
from agent_lbw.runtime.improve import CycleResult, publish_runner_change
from agent_lbw.runtime.project import load_project
from agent_lbw.runtime.skills import compose_system_prompt
from agent_lbw.runtime.store.recording import file_events_from_diff
from agent_lbw.sandbox import Workspace
from agent_lbw.tools import build_tools
from agent_lbw.tools.files import is_probably_secret_path
from agent_lbw.tools.git import (
    current_branch,
    current_sha,
    github_auth_args,
    github_https_url,
    github_owner_repo,
    is_protected_branch,
    run_git,
    stash_local_edits,
)

_MARKERS = r"^(<<<<<<<|>>>>>>>)( |$)"
# "CONFLICT (content): Merge conflict in a.py", or
# "CONFLICT (modify/delete): a.py deleted in HEAD and modified in master."
_CONFLICT_LINE = re.compile(
    r"^CONFLICT \([^)]*\): (?:Merge conflict in (.+)|(\S+) deleted in )", re.MULTILINE
)


@dataclass
class Conflict:
    """A merge that stopped on conflicts. The checkout is still mid-merge."""

    workspace: Workspace
    branch: str
    base: str
    head: str
    target: str
    files: list[str]
    snapshot: str


# Resolves the conflict and returns (tree, error). The tree holds the resolved
# files; an error leaves the merge unresolved.
Resolver = Callable[[Conflict], tuple[str, str]]


def merge_goal(pr_url: str, base: str) -> str:
    return (
        f"Merge {base} into the branch of {pr_url} and resolve the merge conflicts. "
        "Keep what both sides meant. Do not change anything else."
    )


def resolution_notes(conflict: Conflict) -> str:
    """What a model is told about the files it resolves."""
    names = "\n".join(f"- {name}" for name in conflict.files)
    return (
        f"Merging {conflict.base} into {conflict.branch} stopped on conflicts in these files:\n"
        f"{names}\n\n"
        "Each one has git conflict markers (<<<<<<<, =======, >>>>>>>). Edit only these "
        "files. Keep what both sides meant, remove every marker, and leave the rest of "
        "the project as it is. The merge is already in this checkout, and LBW writes "
        "the merge commit itself. Do not run git merge, rebase, pull, or cherry-pick, "
        f"and do not merge {conflict.base} again."
    )


def fix_merge_conflict(
    task,
    *,
    settings: Settings,
    store_root: Path | None,
    resolve: Resolver,
) -> CycleResult:
    workspace = Workspace(Path(task.workspace))
    base = str(task.merge_base or "").strip()
    if task.decision == "approve":
        return _publish_approved(workspace, task, settings, store_root, base)
    branch = current_branch(workspace) or ""
    if not branch or is_protected_branch(branch) or branch == base:
        return _failed(task, f"the pull request's branch is not checked out (on {branch or '?'})")
    fetched = _fetch(workspace, [base, branch], _token(store_root, settings))
    if fetched:
        return _failed(task, f"could not fetch {base} and {branch}: {fetched}")
    caught_up = _catch_up(workspace, branch)
    if caught_up:
        return _failed(task, caught_up)
    target = _sha(workspace, f"refs/remotes/origin/{base}")
    head = current_sha(workspace) or ""
    if not target or not head:
        return _failed(task, f"could not read {branch} or origin/{base}")
    ancestor = run_git(workspace, ["merge-base", "--is-ancestor", target, head])
    if ancestor.returncode == 0 and _ahead_of_github(workspace, branch, head):
        # An earlier attempt merged here and stopped before the push.
        record_event(
            kind="step",
            message=f"{branch} here already has {base} merged, and GitHub does not have it yet",
        )
        return _hold_or_publish(
            workspace, task, settings, store_root, base, branch, head, "merged earlier"
        )
    if ancestor.returncode == 0:
        record_event(kind="step", message=f"{branch} already has every commit on {base}")
        return CycleResult(
            status="success",
            goal=task.goal,
            summary=f"{branch} already has every commit on {base}. Nothing to merge.",
            tests_passed=None,
            committed=False,
            published=False,
            commit_sha=head,
            reason="nothing to merge",
            branch=branch,
            stage="done",
        )
    if _abort(workspace):
        record_event(kind="step", message="Cleared a merge an earlier attempt left unfinished")
    cleared = stash_local_edits(workspace, f"lbw: stashed before merging {base}")
    if not cleared.ok:
        return _failed(task, f"could not set local edits aside: {cleared.output}")
    message = f"Merge {base} into {branch}"
    env = _committer(task, settings)
    merged = run_git(workspace, ["merge", "--no-ff", "--no-edit", "-m", message, target], env=env)
    if merged.returncode == 0:
        record_event(kind="step", message=f"Merged {base} into {branch} with no conflicts")
        return _publish(workspace, task, settings, store_root, base, "merged with no conflicts")
    files = _unmerged(workspace, f"{merged.stdout}\n{merged.stderr}")
    if not files:
        _abort(workspace)
        output = (merged.stderr or merged.stdout).strip()
        return _failed(task, f"could not merge {base}: {output}")
    record_event(
        kind="step",
        message=f"Merging {base} left conflicts in {len(files)} file(s): {', '.join(files)}",
    )
    snapshot = _snapshot(workspace, head, files, env)
    if not snapshot:
        _abort(workspace)
        return _failed(task, "could not record the conflicted files")
    conflict = Conflict(workspace, branch, base, head, target, files, snapshot)
    tree, error = resolve(conflict)
    if error:
        _abort(workspace)
        return _failed(task, error)
    problem = _check_tree(workspace, conflict, tree)
    if problem:
        _abort(workspace)
        return _failed(task, problem)
    commit = run_git(
        workspace, ["commit-tree", tree, "-p", head, "-p", target, "-m", message], env=env
    )
    if commit.returncode != 0:
        _abort(workspace)
        return _failed(task, f"could not write the merge commit: {commit.stderr.strip()}")
    merge_sha = commit.stdout.strip()
    _abort(workspace)
    moved = _move_branch(workspace, branch, merge_sha)
    if moved:
        return _failed(task, f"could not check out the merge on {branch}: {moved}")
    record_event(kind="step", message=f"Resolved the conflicts in a merge commit on {branch}")
    diff = run_git(
        workspace, ["diff", "--no-color", "--no-ext-diff", head, merge_sha, "--", *files]
    )
    if diff.returncode == 0:
        for event in file_events_from_diff(diff.stdout):
            record_event(**event)
    summary = f"Merged {base} into {branch} and resolved conflicts in {', '.join(files)}."
    return _hold_or_publish(
        workspace,
        task,
        settings,
        store_root,
        base,
        branch,
        merge_sha,
        "resolved merge conflicts",
        summary=summary,
    )


def _hold_or_publish(
    workspace: Workspace,
    task,
    settings: Settings,
    store_root: Path | None,
    base: str,
    branch: str,
    merge_sha: str,
    reason: str,
    *,
    summary: str = "",
) -> CycleResult:
    """Push the merge, or wait for a person to approve it first when the job asks."""
    if task.approval_gate != "ask":
        return _publish(workspace, task, settings, store_root, base, reason)
    record_event(
        kind="step",
        message="Waiting for you to approve the merge",
        approved_sha=merge_sha,
    )
    return CycleResult(
        status="waiting",
        goal=task.goal,
        summary=summary or f"Merged {base} into {branch}. GitHub does not have the merge yet.",
        tests_passed=None,
        committed=True,
        published=False,
        commit_sha=merge_sha,
        reason="waiting for approval",
        stage="approval",
        branch=branch,
    )


def _ahead_of_github(workspace: Workspace, branch: str, head: str) -> bool:
    """The branch here has commits the pull request's branch on GitHub does not."""
    remote = _sha(workspace, f"refs/remotes/origin/{branch}")
    if not remote or remote == head:
        return False
    return run_git(workspace, ["merge-base", "--is-ancestor", remote, head]).returncode == 0


def resolve_with_agent(conflict: Conflict, run_agent: Callable[[str], None]) -> tuple[str, str]:
    """LBW's own coding agent edits the conflicted files in the checkout."""
    run_agent(resolution_notes(conflict))
    added = run_git(conflict.workspace, ["add", "--", *conflict.files])
    if added.returncode != 0:
        return "", f"could not stage the resolved files: {added.stderr.strip()}"
    tree = run_git(conflict.workspace, ["write-tree"])
    if tree.returncode != 0:
        return "", f"the merge is still unresolved: {tree.stderr.strip()}"
    return tree.stdout.strip(), ""


def agent_runner(
    workspace: Workspace, llm: LLMClient, settings: Settings, store_root: Path | None
) -> Callable[[str], None]:
    """LBW's coding agent, with the project's tools, told only to resolve the conflicts."""
    project = load_project(workspace.root)
    tools = build_tools(
        workspace,
        test_command=project.test_command,
        lint_command=project.lint_command,
        command_timeout_seconds=settings.command_timeout_seconds,
        git_author_name=settings.git_author_name,
        git_author_email=settings.git_author_email,
        allow_publish=False,
        goal="Resolve the merge conflicts",
        store_root=store_root,
        database_url=settings.database_url,
    )
    agent = CodingAgent(
        llm,
        tools,
        max_iterations=settings.max_iterations,
        system_prompt=compose_system_prompt(workspace.root, (), ()),
    )

    def run(notes: str) -> None:
        record_event(kind="step", message="The coding agent is resolving the conflicts")
        agent.run("Resolve the merge conflicts", notes)

    return run


def _publish_approved(workspace: Workspace, task, settings, store_root, base: str) -> CycleResult:
    branch = task.branch or current_branch(workspace) or ""
    approved = next(
        (
            str(event.get("approved_sha"))
            for event in reversed(task.events or [])
            if isinstance(event, dict) and event.get("approved_sha")
        ),
        "",
    )
    if approved and _sha(workspace, "HEAD") != approved:
        if not _sha(workspace, f"{approved}^{{commit}}"):
            return _failed(task, f"the approved merge {approved[:8]} is not in this checkout")
        moved = _move_branch(workspace, branch, approved)
        if moved:
            return _failed(task, f"could not check out the approved merge: {moved}")
        record_event(kind="step", message=f"Put the approved merge back on {branch}")
    return _publish(workspace, task, settings, store_root, base, "approved the merge")


def _publish(workspace: Workspace, task, settings, store_root, base: str, reason: str):
    record_event(kind="step", message="Pushing the merge to the pull request")
    return publish_runner_change(
        workspace.root,
        settings,
        task.goal or "",
        task.summary or f"Merged {base}",
        base_sha=None,
        create_pr=True,
        store_root=store_root,
        pr_base=base,
        keep_pull_request=True,
        reason=reason,
    )


def _check_tree(workspace: Workspace, conflict: Conflict, tree: str) -> str:
    if not tree:
        return "the conflicts were not resolved"
    left = run_git(
        workspace,
        ["grep", "-I", "-l", "-E", _MARKERS, tree, "--", *conflict.files],
    )
    if left.returncode == 0 and left.stdout.strip():
        names = sorted({line.split(":", 1)[-1] for line in left.stdout.splitlines() if line})
        return f"conflict markers are still in {', '.join(names)}"
    changed = set(_names(workspace, conflict.head, tree)) & set(
        _names(workspace, conflict.target, tree)
    )
    secrets = sorted(name for name in changed if is_probably_secret_path(Path(name)))
    if secrets:
        return f"refusing to commit likely secrets: {', '.join(secrets)}"
    return ""


def _snapshot(workspace: Workspace, head: str, files: list[str], env: dict) -> str:
    """A commit on `head` holding git's merge with the conflicted files as they are."""
    staged = run_git(workspace, ["add", "--", *files])
    if staged.returncode != 0:
        return ""
    tree = run_git(workspace, ["write-tree"])
    if tree.returncode != 0:
        return ""
    commit = run_git(
        workspace,
        ["commit-tree", tree.stdout.strip(), "-p", head, "-m", "Merge conflicts to resolve"],
        env=env,
    )
    return commit.stdout.strip() if commit.returncode == 0 else ""


def _catch_up(workspace: Workspace, branch: str) -> str:
    """Start from the pull request as GitHub has it. Returns an error or ''."""
    remote = _sha(workspace, f"refs/remotes/origin/{branch}")
    local = current_sha(workspace) or ""
    if not remote or remote == local:
        return ""
    if run_git(workspace, ["merge-base", "--is-ancestor", remote, local]).returncode == 0:
        return ""
    if run_git(workspace, ["merge-base", "--is-ancestor", local, remote]).returncode == 0:
        moved = _move_branch(workspace, branch, remote)
        if moved:
            return f"could not move {branch} to the pull request's head: {moved}"
        record_event(kind="step", message=f"Moved {branch} to the pull request's head on GitHub")
        return ""
    return (
        f"{branch} here and on GitHub have different commits. "
        "Merge or drop one side before fixing the conflict."
    )


def _fetch(workspace: Workspace, branches: list[str], token: str | None) -> str:
    refspecs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in branches]
    parsed = github_owner_repo(workspace) if token else None
    if token and parsed is not None:
        args = [*github_auth_args(token), "fetch", "--quiet", github_https_url(*parsed), *refspecs]
    else:
        args = ["fetch", "--quiet", "origin", *refspecs]
    fetched = run_git(workspace, args, timeout=120)
    return "" if fetched.returncode == 0 else (fetched.stderr or fetched.stdout).strip()


def _token(store_root: Path | None, settings: Settings) -> str | None:
    if store_root is None:
        return None
    from agent_lbw.runtime.store.github import GitHubAppError, current_token

    try:
        return current_token(store_root, settings.database_url)
    except (GitHubAppError, OSError, ValueError):
        return None


def _move_branch(workspace: Workspace, branch: str, sha: str) -> str:
    cleared = stash_local_edits(workspace, "lbw: stashed before checking out a merge")
    if not cleared.ok:
        return cleared.output
    moved = run_git(workspace, ["checkout", "-q", "-f", "-B", branch, sha])
    return "" if moved.returncode == 0 else (moved.stderr or moved.stdout).strip()


def _abort(workspace: Workspace) -> bool:
    """Leave no merge in progress. Returns whether one was."""
    if run_git(workspace, ["rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode != 0:
        return False
    if run_git(workspace, ["merge", "--abort"]).returncode != 0:
        run_git(workspace, ["reset", "--merge"])
    return True


def _unmerged(workspace: Workspace, output: str = "") -> list[str]:
    """The files git could not merge: the index's conflict entries, and each one
    the merge named, such as both sides changing it or one side deleting it."""
    found: list[str] = []
    listed = run_git(workspace, ["ls-files", "--unmerged", "-z"])
    for entry in listed.stdout.split("\0"):
        name = entry.split("\t", 1)[1] if "\t" in entry else ""
        if name and name not in found:
            found.append(name)
    for match in _CONFLICT_LINE.finditer(output or ""):
        name = (match.group(1) or match.group(2) or "").strip()
        if name and name not in found and (workspace.root / name).exists():
            found.append(name)
    return found


def _names(workspace: Workspace, left: str, right: str) -> list[str]:
    listed = run_git(workspace, ["diff", "--name-only", left, right])
    return [line for line in listed.stdout.splitlines() if line.strip()]


def _sha(workspace: Workspace, ref: str) -> str:
    found = run_git(workspace, ["rev-parse", "-q", "--verify", ref])
    return found.stdout.strip() if found.returncode == 0 else ""


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


def _failed(task, reason: str) -> CycleResult:
    record_event(kind="step", message=f"Could not fix the merge conflict: {reason}")
    return CycleResult(
        status="failed",
        goal=task.goal,
        summary=reason,
        tests_passed=None,
        committed=False,
        published=False,
        commit_sha=None,
        reason=reason,
    )
