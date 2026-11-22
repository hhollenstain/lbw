from __future__ import annotations

import re
from pathlib import Path

from agent_lbw.sandbox import Workspace
from agent_lbw.tools.files import SKIP_DIR_NAMES
from agent_lbw.tools.git import is_runtime_artifact, run_git

DIFF_LIMIT = 16_000
MAX_LOCKFILE_ROWS = 80
# A new file is shown whole up to this size, so a reviewer reads a full
# AGENTS.md or README rather than its first page.
UNTRACKED_FILE_LIMIT = 12_000
# Where LBW cuts evidence short for a reviewer. It must not read as file text:
# a bare "... truncated" made reviewers fail goals for a file that was whole.
SHORTENED_MARK = "[LBW shortened this review copy"


def shortened_note(omitted: int | None = None) -> str:
    """The line that marks where LBW cut the review copy, not the file, short."""
    more = f"{omitted:,} more characters are" if omitted else "The rest is"
    return f"{SHORTENED_MARK} here. {more} in the workspace. The file itself is not cut off.]"


def clip_for_review(text: str, limit: int) -> str:
    """The text, or its first `limit` characters followed by the shortened note."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n" + shortened_note(len(text) - limit)


MANIFEST_BASENAMES = frozenset(
    {
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "pipfile",
        "package.json",
        "cargo.toml",
        "go.mod",
        "requirements.txt",
        "requirements.in",
        "gemfile",
        "composer.json",
    }
)

LOCKFILE_BASENAMES = frozenset(
    {
        "pipfile.lock",
        "poetry.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-lock.yml",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "uv.lock",
        "pdm.lock",
        "flake.lock",
        "go.sum",
    }
)

_META_KEYS = frozenset(
    {
        "checksum",
        "content-hash",
        "default",
        "dependencies",
        "develop",
        "dev-dependencies",
        "editable",
        "extras",
        "files",
        "git",
        "groups",
        "hashes",
        "index",
        "integrity",
        "lock-version",
        "markers",
        "meta",
        "metadata",
        "package",
        "packages",
        "path",
        "pipfile-spec",
        "python-versions",
        "python_version",
        "ref",
        "requires",
        "requires_python",
        "resolved",
        "source",
        "subdir",
        "version",
    }
)

_GIT_DIFF_SPLIT = re.compile(r"(?=^diff --git )", re.MULTILINE)
_DIFF_PATH = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)
_PLUS_PATH = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_PACKAGE_KEY = re.compile(r'^"([^"]+)"\s*:\s*\{')
_JSON_VERSION = re.compile(r'^"version"\s*:\s*"([^"]+)"')
_TOML_NAME = re.compile(r'^name\s*=\s*"([^"]+)"')
_TOML_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"')
_YARN_HEADER = re.compile(r'^"?([^"@\s]+)@')
_YARN_VERSION = re.compile(r'^version\s+"([^"]+)"')
_GOAL_TOKEN = re.compile(r"[A-Za-z][\w.\-]{2,}")
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".php",
        ".cs",
        ".gd",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".swift",
        ".kt",
        ".vue",
        ".svelte",
    }
)
_CATALOG_SUFFIXES = frozenset({".json", ".md", ".yaml", ".yml", ".txt", ".rst", ".adoc"})


def is_lockfile(path: str) -> bool:
    name = Path(path).name.lower()
    if name in LOCKFILE_BASENAMES:
        return True
    if name.endswith(("-lock.json", "-lock.yaml", "-lock.yml")):
        return True
    return name.endswith(".lock")


def nested_repos(root: Path) -> list[Path]:
    """Git repos inside the workspace, not the workspace itself."""
    found: list[Path] = []
    _walk_repos(root, root, 0, found)
    return found


def nested_projects_changed(workspace: Workspace) -> bool:
    """True when another project in this workspace has its own uncommitted work."""
    for repo in nested_repos(workspace.root):
        status = run_git(Workspace(repo), ["status", "--porcelain"])
        if (status.stdout or "").strip():
            return True
    return False


def nested_project_diff_text(workspace: Workspace, *, limit: int = 6_000) -> str:
    """Diffs from nested repos so a review can see changes git at the root hides."""
    parts: list[str] = []
    used = 0
    for repo in nested_repos(workspace.root):
        block = _nested_repo_block(workspace.root, repo)
        if not block:
            continue
        if used + len(block) > limit:
            remain = limit - used
            if remain > 80:
                parts.append(clip_for_review(block, remain))
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)


def collect_work_diff(
    workspace: Workspace,
    sha_before: str | None,
    *,
    goal: str | None = None,
) -> str:
    """Diff of this cycle's work: commits after sha_before plus the working tree.

    When LBW had to shorten it, a review index follows: each changed file's git
    blob id and an outline of line ranges a reviewer can read.
    """
    text = _work_diff_text(workspace, sha_before, goal=goal)
    if SHORTENED_MARK not in text:
        return text
    from agent_lbw.runtime.review_index import review_index

    index = review_index(workspace.root, _changed_paths(workspace, sha_before))
    return f"{text}\n\n{index}" if index else text


def _changed_paths(workspace: Workspace, sha_before: str | None) -> list[str]:
    """Changed and new text files at the root, largest first, without lockfiles."""
    range_args = [sha_before] if sha_before else ["HEAD"]
    names = run_git(workspace, ["diff", "--name-status", *range_args]).stdout
    listed = run_git(workspace, ["ls-files", "--others", "--exclude-standard"]).stdout
    paths: list[str] = []
    for line in names.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and not fields[0].startswith("D"):
            paths.append(fields[-1].strip())
    paths += [line.strip() for line in listed.splitlines() if line.strip()]
    kept = [
        path
        for path in dict.fromkeys(paths)
        if path and not is_lockfile(path) and not is_runtime_artifact(path)
    ]

    def size(path: str) -> int:
        try:
            return (workspace.root / path).stat().st_size
        except OSError:
            return 0

    return sorted(kept, key=size, reverse=True)


def _work_diff_text(
    workspace: Workspace,
    sha_before: str | None,
    *,
    goal: str | None = None,
) -> str:
    status = run_git(workspace, ["status", "--short", "--branch"])
    range_args = [sha_before] if sha_before else ["HEAD"]
    names = run_git(workspace, ["diff", "--name-status", *range_args])
    diff = run_git(workspace, ["diff", *range_args])
    lock_summaries = lockfile_summaries_from_status(
        workspace,
        names.stdout,
        sha_before,
        goal=goal,
    )
    untracked_parts: list[str] = []
    listed = run_git(workspace, ["ls-files", "--others", "--exclude-standard"])
    for rel in listed.stdout.splitlines():
        path = rel.strip()
        if not path or is_runtime_artifact(path):
            continue
        full = workspace.root / path
        if not full.is_file():
            untracked_parts.append(f"untracked: {path}")
            continue
        try:
            body = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            untracked_parts.append(f"untracked binary: {path}")
            continue
        untracked_parts.append(_format_untracked(path, body, goal=goal))
    body = format_work_diff(
        status=status.stdout,
        name_status=names.stdout,
        unified=diff.stdout,
        extra=untracked_parts,
        lock_summaries=lock_summaries,
        goal=goal,
    )
    nested = nested_project_diff_text(workspace)
    if not nested:
        return body
    if body == "(no diff)":
        return nested
    combined = f"{nested}\n\n{body}"
    if len(combined) > DIFF_LIMIT:
        return clip_for_review(combined, DIFF_LIMIT - 160)
    return combined


def _walk_repos(root: Path, directory: Path, depth: int, found: list[Path]) -> None:
    if depth > 3 or len(found) >= 8:
        return
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child.name in SKIP_DIR_NAMES:
            continue
        if (child / ".git").exists() and child != root:
            found.append(child)
            if len(found) >= 8:
                return
            continue
        _walk_repos(root, child, depth + 1, found)


def _nested_repo_block(workspace_root: Path, repo: Path) -> str:
    nested = Workspace(repo)
    status = run_git(nested, ["status", "--porcelain"])
    diff = run_git(nested, ["diff", "HEAD"])
    untracked = run_git(nested, ["ls-files", "--others", "--exclude-standard"])
    chunks: list[str] = []
    if (status.stdout or "").strip():
        chunks.append(status.stdout.strip())
    if (diff.stdout or "").strip():
        chunks.append(diff.stdout.strip())
    for rel in (untracked.stdout or "").splitlines():
        path = rel.strip()
        if not path or is_runtime_artifact(path):
            continue
        full = repo / path
        if not full.is_file():
            chunks.append(f"untracked: {path}")
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            chunks.append(f"untracked binary: {path}")
            continue
        text = clip_for_review(text, UNTRACKED_FILE_LIMIT)
        chunks.append(f"--- /dev/null\n+++ b/{path}\n{text}")
    if not chunks:
        return ""
    label = repo.relative_to(workspace_root).as_posix()
    return f"Other project {label}:\n" + "\n".join(chunks)


def collect_current_evidence(
    workspace: Workspace,
    goal: str,
    *,
    limit: int = DIFF_LIMIT,
) -> str:
    """Snapshot of the current tree so a reviewer can see if a goal is already met."""
    parts: list[str] = []
    head = run_git(workspace, ["log", "-1", "--oneline"])
    if head.stdout.strip():
        parts.append("Current HEAD:\n" + head.stdout.strip())
    status = run_git(workspace, ["status", "--short", "--branch"])
    if status.stdout.strip():
        parts.append(status.stdout.strip())
    for lock_path in _workspace_lockfiles(workspace.root):
        try:
            body = lock_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = lock_path.relative_to(workspace.root).as_posix()
        rows = _current_version_rows(extract_versions_from_lock_text(body), goal)
        if rows:
            parts.append(f"{rel} current versions:\n" + "\n".join(rows))
    for manifest in _workspace_manifests(workspace.root):
        try:
            body = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = manifest.relative_to(workspace.root).as_posix()
        body = clip_for_review(body, UNTRACKED_FILE_LIMIT)
        parts.append(f"{rel}:\n{body.strip()}")
    return _join_budget(parts, limit) or "(empty workspace)"


def _part_priority(part: str) -> int:
    """Rank a diff part so implementation files are kept ahead of large catalogs."""
    path = _diff_path(part) or ""
    suffix = Path(path).suffix.lower() if path else ""
    if suffix in _CODE_SUFFIXES:
        return 0
    if suffix in _CATALOG_SUFFIXES:
        return 2
    return 1


def format_work_diff(
    *,
    status: str = "",
    name_status: str = "",
    unified: str = "",
    extra: list[str] | None = None,
    lock_summaries: list[str] | None = None,
    goal: str | None = None,
    limit: int = DIFF_LIMIT,
) -> str:
    """Build a reviewer-sized diff: file list, source hunks, lockfile versions."""
    header_parts: list[str] = []
    if status.strip():
        header_parts.append(status.strip())
    if name_status.strip():
        header_parts.append("Changed files:\n" + name_status.strip())
    source_parts, lock_from_diff = partition_unified(unified, goal=goal)
    lock_parts = list(lock_summaries) if lock_summaries is not None else lock_from_diff
    body = source_parts + lock_parts + [part for part in extra or [] if part.strip()]
    # A long marketplace.json diff used to fill the review budget and hide the
    # Python files that actually implement the goal.
    body.sort(key=_part_priority)
    header = "\n\n".join(header_parts)
    if not header and not body:
        return "(no diff)"
    remaining = limit - len(header) - (2 if header and body else 0)
    packed = _join_budget(body, max(remaining, 0))
    if header and packed:
        return f"{header}\n\n{packed}"
    return header or packed or "(no diff)"


def summarize_unified_diff(
    text: str,
    *,
    goal: str | None = None,
    limit: int = DIFF_LIMIT,
) -> str:
    """Rewrite lockfile hunks as version changes; leave other files intact."""
    raw = (text or "").strip()
    if not raw:
        return raw
    source_parts, lock_parts = partition_unified(raw, goal=goal)
    if not lock_parts and len(raw) <= limit:
        return raw
    return _join_budget(source_parts + lock_parts, limit) or raw[:limit]


def lockfile_summaries_from_status(
    workspace: Workspace,
    name_status: str,
    sha_before: str | None,
    *,
    goal: str | None = None,
) -> list[str]:
    summaries: list[str] = []
    seen: set[str] = set()
    for line in (name_status or "").splitlines():
        path = _name_status_path(line)
        if not path or path in seen or not is_lockfile(path):
            continue
        seen.add(path)
        old = _file_at_revision(workspace, sha_before, path)
        new = _working_tree_text(workspace, path)
        summaries.append(summarize_lockfile_pair(path, old, new, goal=goal))
    return summaries


def summarize_lockfile_pair(
    path: str,
    old_text: str,
    new_text: str,
    *,
    goal: str | None = None,
) -> str:
    rows = version_change_rows(
        extract_versions_from_lock_text(old_text),
        extract_versions_from_lock_text(new_text),
        goal=goal,
    )
    return _format_lockfile_rows(path, rows)


def partition_unified(text: str, *, goal: str | None = None) -> tuple[list[str], list[str]]:
    source: list[str] = []
    lock: list[str] = []
    for chunk in _split_file_diffs(text):
        path = _diff_path(chunk)
        if path and is_lockfile(path):
            lock.append(summarize_lockfile_diff(path, chunk, goal=goal))
        else:
            source.append(chunk)
    return source, lock


def summarize_lockfile_diff(path: str, chunk: str, *, goal: str | None = None) -> str:
    return _format_lockfile_rows(path, extract_version_changes(chunk, goal=goal))


def extract_versions_from_lock_text(text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    name: str | None = None
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip().rstrip(",")
        package = _package_key(stripped)
        if package:
            name = package
            continue
        version = _version_value(stripped)
        if version and name:
            versions[name] = version
    return versions


def extract_version_changes(chunk: str, *, goal: str | None = None) -> list[str]:
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    name: str | None = None
    for raw_line in chunk.splitlines():
        if not raw_line or raw_line.startswith(("diff ", "index ", "+++", "---", "@@", "\\")):
            continue
        prefix = raw_line[0] if raw_line[0] in "+-" else " "
        line = raw_line[1:] if raw_line[0] in "+- " else raw_line
        stripped = line.strip().rstrip(",")
        package = _package_key(stripped)
        if package:
            name = package
            continue
        version = _version_value(stripped)
        if not version or not name:
            continue
        if prefix == "-":
            old[name] = version
        elif prefix == "+":
            new[name] = version
    return version_change_rows(old, new, goal=goal)


def version_change_rows(
    old: dict[str, str],
    new: dict[str, str],
    *,
    goal: str | None = None,
) -> list[str]:
    pairs: list[tuple[str, str, str]] = []
    for package in set(old) | set(new):
        before = old.get(package)
        after = new.get(package)
        if before == after:
            continue
        pairs.append((package, before or "(none)", after or "(removed)"))
    tokens = _goal_tokens(goal)
    pairs.sort(
        key=lambda item: (
            0 if _matches_goal(item[0], tokens) else 1,
            item[0].lower(),
        )
    )
    return [f"- {package}: {before} -> {after}" for package, before, after in pairs]


def _format_lockfile_rows(path: str, rows: list[str]) -> str:
    if not rows:
        return (
            f"{path} (lockfile): no package version changes in diff; "
            "hashes or metadata may have changed"
        )
    shown = rows[:MAX_LOCKFILE_ROWS]
    extra = len(rows) - len(shown)
    lines = [f"{path} (lockfile version changes):", *shown]
    if extra > 0:
        lines.append(f"... and {extra} more package version changes")
    return "\n".join(lines)


def _name_status_path(line: str) -> str | None:
    text = line.strip()
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    path = parts[1]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip() or None


def _file_at_revision(workspace: Workspace, sha: str | None, path: str) -> str:
    rev = sha or "HEAD"
    result = run_git(workspace, ["show", f"{rev}:{path}"])
    if result.returncode != 0:
        return ""
    return result.stdout


def _working_tree_text(workspace: Workspace, path: str) -> str:
    full = workspace.root / path
    if not full.is_file():
        return ""
    try:
        return full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _format_untracked(path: str, body: str, *, goal: str | None) -> str:
    if is_lockfile(path):
        return summarize_lockfile_pair(path, "", body, goal=goal)
    body = clip_for_review(body, UNTRACKED_FILE_LIMIT)
    return f"--- /dev/null\n+++ b/{path}\n{body}"


def _split_file_diffs(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("diff --git "):
        return [chunk.strip() for chunk in _GIT_DIFF_SPLIT.split(raw) if chunk.strip()]
    return [raw]


def _diff_path(chunk: str) -> str | None:
    match = _DIFF_PATH.search(chunk)
    if match:
        return match.group(1).strip()
    match = _PLUS_PATH.search(chunk)
    if match:
        return match.group(1).strip()
    return None


def _package_key(stripped: str) -> str | None:
    match = _PACKAGE_KEY.match(stripped)
    if match:
        name = match.group(1)
        return None if name.lower() in _META_KEYS else name
    match = _TOML_NAME.match(stripped)
    if match:
        return match.group(1)
    match = _YARN_HEADER.match(stripped)
    if match and stripped.endswith(":"):
        return match.group(1)
    return None


def _version_value(stripped: str) -> str | None:
    for pattern in (_JSON_VERSION, _TOML_VERSION, _YARN_VERSION):
        match = pattern.match(stripped)
        if match:
            value = match.group(1).strip()
            if value.startswith("=="):
                return value[2:]
            return value
    return None


def _goal_tokens(goal: str | None) -> set[str]:
    if not goal:
        return set()
    return {match.group(0).lower() for match in _GOAL_TOKEN.finditer(goal)}


def _matches_goal(name: str, tokens: set[str]) -> bool:
    lowered = name.lower()
    return any(token in lowered or lowered in token for token in tokens)


def _current_version_rows(versions: dict[str, str], goal: str) -> list[str]:
    tokens = _goal_tokens(goal)
    items = list(versions.items())
    if tokens:
        matched = [(name, version) for name, version in items if _matches_goal(name, tokens)]
        if matched:
            items = matched
    items.sort(key=lambda item: item[0].lower())
    return [f"- {name}: {version}" for name, version in items[:MAX_LOCKFILE_ROWS]]


def _workspace_lockfiles(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in _scan_dirs(root):
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_file() and is_lockfile(child.name):
                found.append(child)
                if len(found) >= 8:
                    return found
    return found


def _workspace_manifests(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in _scan_dirs(root):
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_file() and child.name.lower() in MANIFEST_BASENAMES:
                found.append(child)
                if len(found) >= 8:
                    return found
    return found


def _scan_dirs(root: Path) -> list[Path]:
    dirs = [root]
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return dirs
    for child in children:
        if child.is_dir() and child.name not in SKIP_DIR_NAMES:
            dirs.append(child)
        if len(dirs) >= 12:
            break
    return dirs


def _join_budget(parts: list[str], limit: int) -> str:
    if limit <= 0:
        return shortened_note()
    kept: list[str] = []
    used = 0
    for part in parts:
        extra = 2 if kept else 0
        size = extra + len(part)
        if used + size <= limit:
            kept.append(part)
            used += size
            continue
        remain = limit - used - extra - 160
        if remain > 80:
            kept.append(clip_for_review(part, remain))
        elif kept:
            kept.append(shortened_note())
        elif limit > 160:
            kept.append(clip_for_review(part, limit - 160))
        break
    return "\n\n".join(kept)
