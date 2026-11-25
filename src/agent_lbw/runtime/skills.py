from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import yaml

SKILLS_DIRNAME = "skills"
SOURCES_DIRNAME = "sources"
LOCAL_DIRNAME = "local"
SKILL_FILENAMES = ("SKILL.md", "skill.md")
SKIP_WALK = {".git", "node_modules", "__pycache__", ".venv"}
MAX_SKILL_CHARS = 8_000
SUMMARY_LIMIT = 72
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z", re.DOTALL)
_MARKDOWN = None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    origin: str
    path: str
    source_url: str = ""
    enabled: bool = False

    def public_dict(self, *, include_body: bool = False) -> dict[str, str | bool]:
        payload = asdict(self)
        payload["summary"] = skill_summary(self.description or self.body)
        if include_body:
            payload["html"] = render_skill_html(self.body)
        else:
            payload.pop("body", None)
        return payload


@dataclass(frozen=True)
class SkillSource:
    url: str
    slug: str
    ref: str = ""
    path: str = ""

    def public_dict(self) -> dict[str, str]:
        return asdict(self)


def bundled_skills_root() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def skills_root(workspace: Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".lbw" / SKILLS_DIRNAME


def sources_root(workspace: Path) -> Path:
    return skills_root(workspace) / SOURCES_DIRNAME


def local_skills_root(workspace: Path) -> Path:
    return skills_root(workspace) / LOCAL_DIRNAME


def source_slug(url: str) -> str:
    from agent_lbw.runtime.workspaces import repo_name_from_url

    return repo_name_from_url(url) or "skills"


def skill_summary(text: str, *, limit: int = SUMMARY_LIMIT) -> str:
    """First sentence of a skill description, clipped for list rows."""
    compact = " ".join((text or "").split())
    if not compact:
        return "No description."
    for sep in (". ", "? ", "! "):
        index = compact.find(sep)
        if index != -1:
            compact = compact[: index + 1]
            break
    if len(compact) <= limit:
        return compact
    clipped = compact[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:")
    return f"{clipped or compact[: limit - 1]}…"


def get_skill(root: Path, name: str) -> Skill | None:
    wanted = _clean_name(name)
    if not wanted:
        return None
    return next((skill for skill in list_skills(root) if skill.name == wanted), None)


def render_skill_html(text: str) -> str:
    """Turn a SKILL.md body into HTML for the settings popover."""
    body = (text or "").strip()
    if not body:
        return ""
    return _markdown_renderer().render(body).strip()


def _markdown_renderer():
    from markdown_it import MarkdownIt

    global _MARKDOWN
    if _MARKDOWN is None:
        renderer = MarkdownIt("commonmark", {"html": False})
        renderer.enable(["table", "strikethrough"])
        _MARKDOWN = renderer
    return _MARKDOWN


_PLAIN_VALUE = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(.*)$")


def skill_markdown_for_kiwi(text: str) -> str:
    """Skill markdown whose frontmatter KiwiFS will store.

    An unquoted value containing a colon is read as a mapping. Quote that
    value and write the frontmatter back. The body stays as it was.
    """
    raw = (text or "").replace("\r\n", "\n")
    match = _FRONTMATTER_RE.match(raw.strip())
    if match is None:
        return text
    block = match.group(1) or ""
    if _frontmatter_mapping(block) is not None:
        return text
    loaded = _frontmatter_mapping(_quote_plain_colons(block))
    if loaded is None:
        problem = "could not be parsed"
        try:
            yaml.safe_load(_quote_plain_colons(block))
        except yaml.YAMLError as exc:
            problem = _yaml_problem(exc)
        raise ValueError(f"skill frontmatter is not valid YAML ({problem})")
    dumped = yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True, width=10**6).strip()
    body = (match.group(2) or "").strip("\n")
    if body:
        return f"---\n{dumped}\n---\n\n{body}\n"
    return f"---\n{dumped}\n---\n"


def _frontmatter_mapping(text: str) -> dict | None:
    try:
        loaded = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        return None
    return loaded


def _quote_plain_colons(text: str) -> str:
    """Quote an unquoted value that contains a colon, which YAML reads as a mapping."""
    lines: list[str] = []
    for line in text.splitlines():
        match = _PLAIN_VALUE.match(line)
        if match is None:
            lines.append(line)
            continue
        indent, key, rest = match.group(1), match.group(2), match.group(3)
        value = rest.strip()
        if not value or value[0] in "\"'|>[{&*!%#":
            lines.append(line)
            continue
        if ": " not in value:
            lines.append(line)
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{indent}{key}: "{escaped}"')
    return "\n".join(lines)


def _yaml_problem(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None) or "could not be parsed"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return str(problem)
    return f"{problem} (line {mark.line + 1})"


def parse_skill_markdown(text: str, *, fallback_name: str) -> tuple[str, str, str]:
    raw = (text or "").replace("\r\n", "\n")
    match = _FRONTMATTER_RE.match(raw.strip())
    meta: dict = {}
    body = raw.strip()
    if match:
        loaded = yaml.safe_load(match.group(1) or "") or {}
        if isinstance(loaded, dict):
            meta = loaded
        body = (match.group(2) or "").strip()
    name = _clean_name(str(meta.get("name") or fallback_name))
    description = str(meta.get("description") or "").strip()
    return name, description, body


def load_skill_settings(root: Path) -> dict:
    raw = _load_raw_config(root)
    block = raw.get("skills")
    if not isinstance(block, dict):
        block = {}
    enabled = _string_list(block.get("enabled"))
    sources: list[SkillSource] = []
    for item in block.get("sources") or []:
        parsed = _parse_source(item)
        if parsed is not None:
            sources.append(parsed)
    return {"enabled": enabled, "sources": sources}


def enabled_skill_names(root: Path) -> list[str]:
    return list(load_skill_settings(root)["enabled"])


def list_skill_sources(root: Path) -> list[SkillSource]:
    found: list[SkillSource] = []
    for source in load_skill_settings(root)["sources"]:
        dest = sources_root(root) / source.slug
        if dest.is_dir():
            source = replace(source, path=str(dest))
        found.append(source)
    return found


def list_skills(root: Path) -> list[Skill]:
    enabled = set(enabled_skill_names(root))
    by_name: dict[str, Skill] = {}
    for skill in _discover(bundled_skills_root(), origin="bundled"):
        by_name[skill.name] = skill
    for source in list_skill_sources(root):
        dest = Path(source.path) if source.path else sources_root(root) / source.slug
        if not dest.is_dir():
            continue
        for skill in _discover(dest, origin="source", source_url=source.url):
            by_name[skill.name] = skill
    local = local_skills_root(root)
    if local.is_dir():
        for skill in _discover(local, origin="local", skip_paths={}):
            by_name[skill.name] = skill
    repo_skills = _discover_repo_skills(root)
    for skill in repo_skills:
        by_name[skill.name] = skill
    return [
        replace(skill, enabled=skill.name in enabled)
        for skill in sorted(by_name.values(), key=lambda item: item.name)
    ]


def save_enabled_skills(
    root: Path,
    names: list[str] | None,
    extra_known: Sequence[str] = (),
) -> list[Skill]:
    known = {skill.name for skill in list_skills(root)} | set(extra_known)
    enabled = [name for name in _string_list(names) if name in known]
    _update_skills_config(root, enabled=enabled)
    return list_skills(root)


def add_skill_source(
    root: Path,
    url: str,
    *,
    ref: str | None = None,
) -> SkillSource:
    repo = (url or "").strip()
    if not repo:
        raise ValueError("repository URL is required")
    source = SkillSource(url=repo, slug=source_slug(repo), ref=(ref or "").strip())
    dest = _clone_or_pull(root, source)
    sources = [item for item in list_skill_sources(root) if item.slug != source.slug]
    sources.append(replace(source, path=str(dest)))
    _update_skills_config(root, sources=sources)
    return replace(source, path=str(dest))


def sync_skill_source(root: Path, slug: str, *, ref: str | None = None) -> SkillSource:
    wanted = (slug or "").strip()
    if not wanted:
        raise ValueError("skill source is required")
    match = next((item for item in list_skill_sources(root) if item.slug == wanted), None)
    if match is None:
        raise ValueError(f"unknown skill source: {wanted}")
    source = replace(match, ref=(ref or match.ref or "").strip())
    dest = _clone_or_pull(root, source)
    sources = [
        replace(source, path=str(dest)) if item.slug == source.slug else item
        for item in list_skill_sources(root)
    ]
    _update_skills_config(root, sources=sources)
    return replace(source, path=str(dest))


def selected_skills(
    root: Path,
    extra_names: Sequence[str] = (),
    included: Sequence[Skill] = (),
) -> list[Skill]:
    """The skills a run follows: enabled ones, the run's extras, and repo skills."""
    extra = {name for name in extra_names if name}
    by_name = {skill.name: skill for skill in list_skills(root)}
    for skill in included:
        current = by_name.get(skill.name)
        if current is None or current.origin in {"bundled", "source"}:
            by_name[skill.name] = skill
    return [
        skill
        for skill in sorted(by_name.values(), key=lambda item: item.name)
        if skill.enabled or skill.name in extra or skill.origin == "repo"
    ]


def format_enabled_skills(
    root: Path,
    extra_names: Sequence[str] = (),
    included: Sequence[Skill] = (),
) -> str:
    selected = selected_skills(root, extra_names, included)
    if not selected:
        return ""
    parts = [
        "## Enabled skills",
        "",
        "Follow these skills for this workspace. They are the process for this run.",
    ]
    for skill in selected:
        parts.extend(["", f"### {skill.name}"])
        if skill.description:
            parts.append(skill.description)
        body = skill.body.strip()
        if len(body) > MAX_SKILL_CHARS:
            body = body[:MAX_SKILL_CHARS].rstrip() + "\n... truncated"
        if body:
            parts.extend(["", body])
    return "\n".join(parts).strip()


def compose_system_prompt(
    root: Path,
    extra_names: Sequence[str] = (),
    included: Sequence[Skill] = (),
) -> str:
    from agent_lbw.runtime.project import load_guidelines

    base = load_guidelines(root).rstrip()
    skills = format_enabled_skills(root, extra_names, included)
    if not skills:
        return f"{base}\n"
    return f"{base}\n\n{skills}\n"


def skills_catalog(root: Path) -> dict[str, object]:
    return {
        "skills": [skill.public_dict() for skill in list_skills(root)],
        "sources": [source.public_dict() for source in list_skill_sources(root)],
        "enabled": enabled_skill_names(root),
    }


def _discover(
    root: Path,
    *,
    origin: str,
    source_url: str = "",
    skip_paths: set[str] | None = None,
) -> list[Skill]:
    if not root.is_dir():
        return []
    found: list[Skill] = []
    seen: set[Path] = set()
    skip = SKIP_WALK | (skip_paths or set())
    for filename in SKILL_FILENAMES:
        for path in root.rglob(filename):
            resolved = path.resolve()
            if resolved in seen:
                continue
            if any(part in skip for part in path.parts):
                continue
            seen.add(resolved)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            name, description, body = parse_skill_markdown(text, fallback_name=path.parent.name)
            if not name:
                continue
            found.append(
                Skill(
                    name=name,
                    description=description,
                    body=body,
                    origin=origin,
                    path=str(path),
                    source_url=source_url,
                )
            )
    return found


def _discover_repo_skills(root: Path) -> list[Skill]:
    """Discover skills that exist in the git repository root (outside .lbw)."""
    workspace = Path(root).expanduser().resolve()
    repo_root = _find_git_repo_root(workspace)
    if repo_root is None or not repo_root.is_dir():
        return []
    bundled = bundled_skills_root().resolve()
    known = _skill_files_git_knows(repo_root)
    found: list[Skill] = []
    for skill in _discover(repo_root, origin="repo", skip_paths={".lbw", ".loco"}):
        path = Path(skill.path).resolve()
        if path == bundled or bundled in path.parents:
            continue
        if known is not None and path not in known:
            continue
        found.append(skill)
    return found


def _skill_files_git_knows(repo_root: Path) -> set[Path] | None:
    """Skill files this repository tracks or would track, or None when git cannot say.

    An ignored folder, such as other projects' checkouts kept inside this one,
    and a repository nested inside it do not hold this repository's skills.
    """
    patterns = [f":(glob)**/{name}" for name in SKILL_FILENAMES]
    try:
        listed = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                *patterns,
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    return {(repo_root / name).resolve() for name in listed.stdout.split("\0") if name}


def _find_git_repo_root(start: Path) -> Path | None:
    """Find the root of the git repository containing start."""
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").is_dir():
            return current
        current = current.parent
    return None


def _clean_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")
    slug = slug[:64]
    if _NAME_RE.match(slug):
        return slug
    return ""


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = _clean_name(str(item))
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _parse_source(item: object) -> SkillSource | None:
    if isinstance(item, str):
        url = item.strip()
        if not url:
            return None
        return SkillSource(url=url, slug=source_slug(url))
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    ref = str(item.get("ref") or item.get("branch") or "").strip()
    slug = _clean_name(str(item.get("slug") or "")) or source_slug(url)
    path = str(item.get("path") or "").strip()
    return SkillSource(url=url, slug=slug, ref=ref, path=path)


def _load_raw_config(root: Path) -> dict:
    from agent_lbw.runtime.project import read_project_config

    try:
        return read_project_config(root)
    except (OSError, ValueError):
        return {}


def _write_raw_config(root: Path, raw: dict) -> None:
    from agent_lbw.runtime.project import dump_project_config

    path = Path(root).expanduser().resolve() / ".lbw" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_project_config(raw), encoding="utf-8")


def _update_skills_config(
    root: Path,
    *,
    enabled: list[str] | None = None,
    sources: list[SkillSource] | None = None,
) -> None:
    raw = _load_raw_config(root)
    block = raw.get("skills")
    if not isinstance(block, dict):
        block = {}
    if enabled is None:
        enabled = _string_list(block.get("enabled"))
    if sources is None:
        sources = [
            item
            for item in (_parse_source(raw_item) for raw_item in (block.get("sources") or []))
            if item is not None
        ]
    raw["skills"] = {
        "enabled": enabled,
        "sources": [
            {key: value for key, value in asdict(source).items() if key != "path" and value}
            for source in sources
        ],
    }
    if not raw["skills"]["sources"]:
        raw["skills"].pop("sources", None)
    _write_raw_config(root, raw)


def fetch_skill_checkout(cache: Path, url: str, dirname: str, *, ref: str = "") -> tuple[Path, str]:
    """Clone or update a skill repository and return its checkout and commit."""
    if not re.fullmatch(r"src_[a-f0-9]{12}", dirname):
        raise ValueError("unknown skill repository")
    repo = (url or "").strip()
    if not repo:
        raise ValueError("repository URL is required")
    source = SkillSource(url=repo, slug=dirname, ref=(ref or "").strip())
    dest = _clone_or_pull(cache, source)
    return dest, _head_sha(dest)


def skill_documents(checkout: Path) -> list[tuple[str, str]]:
    """SKILL.md files in a checkout, as relative path and text."""
    found: list[tuple[str, str]] = []
    for path in sorted(checkout.rglob("*")):
        if not path.is_file() or path.name not in SKILL_FILENAMES:
            continue
        if any(part in SKIP_WALK or part == ".git" for part in path.parts):
            continue
        relative = path.relative_to(checkout).as_posix()
        if ".." in relative.split("/") or not _SKILL_REL.fullmatch(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"could not read {relative}") from exc
        if not text.strip():
            continue
        if len(text) > 100_000:
            raise ValueError(f"{relative} is too large")
        try:
            text = skill_markdown_for_kiwi(text)
        except ValueError as exc:
            raise ValueError(f"{relative}: {exc}") from exc
        found.append((relative, text))
    if len(found) > 200:
        raise ValueError("that repository has too many skills")
    if not found:
        raise ValueError("that repository has no skills")
    return found


_SKILL_REL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,160}$")


def _head_sha(checkout: Path) -> str:
    from agent_lbw.runtime.workspaces import CLONE_TIMEOUT_SECONDS

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=False,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git timed out") from exc
    except OSError as exc:
        raise ValueError(f"git failed: {exc}") from exc
    sha = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("could not read the repository revision")
    return sha


def _clone_or_pull(root: Path, source: SkillSource) -> Path:
    dest = sources_root(root) / source.slug
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if not (dest / ".git").exists():
            raise ValueError(f"already exists: {dest}")
        _run_git(["git", "pull", "--ff-only"], cwd=dest)
        return dest
    command = ["git", "clone", "--depth", "1"]
    if source.ref:
        command.extend(["--branch", source.ref])
    command.extend([source.url, str(dest)])
    _run_git(command, cwd=dest.parent)
    return dest


def _run_git(command: list[str], *, cwd: Path) -> None:
    from agent_lbw.runtime.workspaces import CLONE_TIMEOUT_SECONDS

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git timed out") from exc
    except OSError as exc:
        raise ValueError(f"git failed: {exc}") from exc
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout or "git failed").strip())
