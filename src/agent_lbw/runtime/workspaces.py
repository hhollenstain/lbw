from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from agent_lbw.runtime.project import (
    ensure_run_gitignore,
    guidelines_are_custom,
    load_guidelines,
    write_default_project_files,
)
from agent_lbw.tools.files import SKIP_DIR_NAMES

MAX_WORKSPACES = 20
MAX_BROWSE_ENTRIES = 200
WORKSPACES_FILENAME = "workspaces.json"
CLONE_TIMEOUT_SECONDS = 180

_SKIP_BROWSE = SKIP_DIR_NAMES | {".DS_Store"}


def workspaces_path(root: Path) -> Path:
    return Path(root) / ".lbw" / WORKSPACES_FILENAME


def default_checkout_parent(store_root: Path, *, home: Path | None = None) -> Path:
    """Where a new checkout should live so a container rebuild keeps it.

    The workspace volume is outside the container home. A checkout under that
    home is discarded with the container, while the project row stays.
    """
    root = Path(store_root).expanduser().resolve()
    home_dir = (home or Path.home()).expanduser().resolve()
    if root == home_dir or home_dir in root.parents:
        return home_dir / "projects"
    return root


def durable_checkout_parent(
    store_root: Path,
    requested: str = "",
    *,
    home: Path | None = None,
) -> Path:
    """Use the requested folder unless it would disappear on the next rebuild."""
    root = Path(store_root).expanduser().resolve()
    text = (requested or "").strip()
    if not text:
        return default_checkout_parent(root, home=home)
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return default_checkout_parent(root, home=home)
    if resolved == root or root in resolved.parents or resolved in root.parents:
        return resolved
    home_dir = (home or Path.home()).expanduser().resolve()
    if resolved == home_dir or home_dir in resolved.parents:
        if root == home_dir or home_dir in root.parents:
            return resolved
        try:
            relative = resolved.relative_to(home_dir)
        except ValueError:
            return root
        return root / relative
    return resolved


def _read_payload(root: Path) -> dict:
    path = workspaces_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {"workspaces": payload}


def _normalize_dir(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"workspace is not a directory: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"workspace is not a directory: {resolved}")
    return resolved


def _canonical(path: str | Path) -> str:
    target = Path(path).expanduser()
    try:
        return str(target.resolve())
    except OSError:
        return str(target)


def _raw_paths(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        raw = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = _canonical(raw)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def repo_label(remote: str, name: str = "") -> str:
    """owner/repo from a remote URL, or the project name when there is no remote."""
    text = (remote or "").strip()
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@") and ":" in text:
        return text.split(":", 1)[1]
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        if parts:
            return parts[0]
    return text or name


def _git_remote_url(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def is_checkout_shelf(path: str | Path, store_root: str | Path) -> bool:
    """The checkout volume is not a project.

    A folder outside the home directory is where cloned repositories are
    kept. It shows up once for every organization when it is remembered as
    a workspace. A git repository, or a folder that already has LBW project
    files, stays a project.
    """
    root = Path(store_root).expanduser().resolve()
    try:
        place = Path(path).expanduser().resolve()
    except OSError:
        return False
    if place != root:
        return False
    if (place / ".git").exists() or (place / ".lbw" / "config.yaml").exists():
        return False
    return default_checkout_parent(place) == place


def is_store_bookkeeping(path: str | Path, store_root: str | Path) -> bool:
    """Schedule files and other store data are not projects."""
    root = Path(store_root).expanduser().resolve()
    try:
        place = Path(path).expanduser().resolve()
    except OSError:
        return False
    book = root / ".lbw"
    return place == book or book in place.parents


def describe_workspace(path: str | Path) -> dict[str, str | bool]:
    """One workspace entry. The path has to be a directory."""
    return _entry(_normalize_dir(path))


def _entry(path: Path) -> dict[str, str | bool]:
    return {
        "path": str(path),
        "name": path.name or str(path),
        "is_git": (path / ".git").exists(),
        "git_remote": _git_remote_url(path),
        "is_lbw": (path / ".lbw" / "config.yaml").exists(),
        "guidelines": load_guidelines(path),
        "custom_guidelines": guidelines_are_custom(path),
    }


def _entries_for(paths: list[str]) -> list[dict[str, str | bool]]:
    result: list[dict[str, str | bool]] = []
    seen: set[str] = set()
    for raw in paths:
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if not path.is_dir() or str(path) in seen:
            continue
        seen.add(str(path))
        result.append(_entry(path))
    return result


def _save_payload(
    store_root: Path,
    *,
    workspaces: list[str],
    archived: list[str],
    last: str,
) -> None:
    active = list(dict.fromkeys(workspaces))
    archived_only = [path for path in dict.fromkeys(archived) if path not in set(active)]
    payload = {
        "workspaces": [{"path": path} for path in active],
        "archived": [{"path": path} for path in archived_only],
        "last": last,
    }
    dest = workspaces_path(store_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ensure_run_gitignore(store_root)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_workspaces(root: Path, *, default: str | None = None) -> list[dict[str, str | bool]]:
    payload = _read_payload(root)
    result = _entries_for(_raw_paths(payload.get("workspaces")))
    seen = {str(item["path"]) for item in result}
    if default:
        try:
            fallback = _normalize_dir(default)
        except ValueError:
            fallback = None
        if fallback and str(fallback) not in seen:
            result.append(_entry(fallback))
    return result[:MAX_WORKSPACES]


def load_archived_workspaces(root: Path) -> list[dict[str, str | bool]]:
    payload = _read_payload(root)
    active = {str(item["path"]) for item in load_workspaces(root)}
    archived = [
        item
        for item in _entries_for(_raw_paths(payload.get("archived")))
        if str(item["path"]) not in active
    ]
    return archived


def last_workspace(root: Path, *, default: str | Path | None = None) -> str:
    payload = _read_payload(root)
    archived = set(_raw_paths(payload.get("archived")))
    last = payload.get("last")
    if isinstance(last, str) and last.strip():
        canonical = _canonical(last)
        if canonical not in archived:
            try:
                return str(_normalize_dir(last))
            except ValueError:
                pass
    active = _entries_for(_raw_paths(payload.get("workspaces")))
    if active:
        return str(active[-1]["path"])
    if default is not None:
        try:
            return str(_normalize_dir(default))
        except ValueError:
            pass
    return str(Path(root).expanduser().resolve())


def remember_workspace(store_root: Path, path: str | Path) -> list[dict[str, str | bool]]:
    resolved = _normalize_dir(path)
    payload = _read_payload(store_root)
    key = str(resolved)
    workspaces = _raw_paths(payload.get("workspaces"))
    archived = [item for item in _raw_paths(payload.get("archived")) if item != key]
    if key in workspaces:
        saved = workspaces
    else:
        saved = [*workspaces, key][-MAX_WORKSPACES:]
    _save_payload(store_root, workspaces=saved, archived=archived, last=key)
    return load_workspaces(store_root)


def _switch_last(active: list[str], removed: str, previous: object) -> str:
    if previous == removed or not isinstance(previous, str) or previous not in active:
        index = active.index(removed) if removed in active else len(active)
        remaining = [path for path in active if path != removed]
        if not remaining:
            return ""
        return remaining[min(index, len(remaining) - 1)]
    return previous


def archive_workspace(store_root: Path, path: str | Path) -> list[dict[str, str | bool]]:
    resolved = _normalize_dir(path)
    payload = _read_payload(store_root)
    key = str(resolved)
    workspaces = _raw_paths(payload.get("workspaces"))
    if key not in workspaces:
        raise ValueError("workspace is not in the session")
    if len(workspaces) < 2:
        raise ValueError("keep at least one workspace in the session")
    last = _switch_last(workspaces, key, payload.get("last"))
    remaining = [item for item in workspaces if item != key]
    archived = [item for item in _raw_paths(payload.get("archived")) if item != key]
    archived.append(key)
    _save_payload(store_root, workspaces=remaining, archived=archived, last=last)
    return load_workspaces(store_root)


def forget_workspace(store_root: Path, path: str | Path) -> list[dict[str, str | bool]]:
    key = _canonical(path)
    payload = _read_payload(store_root)
    workspaces = _raw_paths(payload.get("workspaces"))
    archived = _raw_paths(payload.get("archived"))
    if key not in workspaces and key not in archived:
        raise ValueError("workspace is not in the session")
    if key in workspaces and len(workspaces) < 2 and key not in archived:
        raise ValueError("keep at least one workspace in the session")
    last = payload.get("last")
    if key in workspaces:
        last = _switch_last(workspaces, key, last)
    remaining = [item for item in workspaces if item != key]
    kept_archived = [item for item in archived if item != key]
    if not remaining:
        raise ValueError("keep at least one workspace in the session")
    if not isinstance(last, str) or last not in remaining:
        last = remaining[-1]
    _save_payload(store_root, workspaces=remaining, archived=kept_archived, last=last)
    return load_workspaces(store_root)


def browse_directory(path: str | Path | None = None) -> dict[str, object]:
    target = Path(path).expanduser() if path else Path.home()
    target = target.resolve()
    if not target.exists() or not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    entries: list[dict[str, str | bool]] = []
    try:
        children = sorted(target.iterdir(), key=lambda child: child.name.lower())
    except OSError as exc:
        raise ValueError(f"cannot list {target}: {exc}") from exc
    for child in children:
        if len(entries) >= MAX_BROWSE_ENTRIES:
            break
        if not child.is_dir():
            continue
        if child.name in _SKIP_BROWSE or child.name.startswith("."):
            continue
        entries.append(_entry(child))
    parent = target.parent
    return {
        "path": str(target),
        "name": target.name or str(target),
        "parent": str(parent) if parent != target else None,
        "home": str(Path.home()),
        "is_git": (target / ".git").exists(),
        "is_lbw": (target / ".lbw" / "config.yaml").exists(),
        "entries": entries,
    }


def repo_name_from_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if ":" in cleaned and "://" not in cleaned:
        cleaned = cleaned.rsplit(":", 1)[-1]
    name = cleaned.rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "workspace"


def create_workspace(path: str | Path) -> dict[str, str | bool]:
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ValueError(f"workspace is not a directory: {target}")
    write_default_project_files(target)
    if not (target / ".git").exists():
        init = subprocess.run(
            ["git", "init"],
            cwd=target,
            check=False,
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            raise ValueError((init.stderr or init.stdout or "git init failed").strip())
    return _entry(target)


def clone_workspace(
    url: str,
    parent: str | Path,
    *,
    name: str | None = None,
    git_config: list[str] | None = None,
) -> dict[str, str | bool]:
    repo = url.strip()
    if not repo:
        raise ValueError("repository URL is required")
    dest_parent = Path(parent).expanduser().resolve()
    dest_parent.mkdir(parents=True, exist_ok=True)
    folder = (name or "").strip() or repo_name_from_url(repo)
    dest = dest_parent / folder
    if dest.exists():
        raise ValueError(f"already exists: {dest}")
    try:
        cloned = subprocess.run(
            ["git", *(git_config or []), "clone", repo, str(dest)],
            check=False,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git clone timed out") from exc
    if cloned.returncode != 0:
        raise ValueError((cloned.stderr or cloned.stdout or "git clone failed").strip())
    write_default_project_files(dest)
    return _entry(dest)
