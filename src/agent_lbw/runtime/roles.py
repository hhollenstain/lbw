"""Agent teams for one proposal: plan, build, and review.

A task is a ROLE.md file. Bundled tasks ship with lbw. A workspace adds or
replaces one by writing `.lbw/roles/<name>/ROLE.md` and turning it on for
its team. Plan pauses the proposal for a person with write access, then
Build writes the change, then Review pauses it again. Builder and QA stay
on; the rest of a team can be switched off.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_lbw.llm.client import LLMClient
from agent_lbw.progress import timed_complete
from agent_lbw.runtime.skills import skill_summary

log = logging.getLogger("lbw")

DEFAULT_ENABLED = ("architect", "builder", "qa")
REQUIRED_ROLES = ("builder", "qa")
ROLE_JOBS = frozenset({"plan", "build", "review"})
_ORDER = {"architect": 0, "builder": 1, "qa": 2, "security": 3, "quality": 4}
_TEAMS = {"plan": "Plan", "build": "Build", "review": "Review"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_VERDICT_ALIASES = {
    "pass": "pass",
    "approve": "pass",
    "approved": "pass",
    "ok": "pass",
    "send_back": "send_back",
    "send-back": "send_back",
    "fail": "send_back",
    "failed": "send_back",
    "block": "block",
    "reject": "block",
    "rejected": "block",
}


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    job: str
    description: str
    body: str
    origin: str
    path: str
    enabled: bool = False

    @property
    def team(self) -> str:
        return _TEAMS.get(self.job, "Review")

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "title": self.title,
            "job": self.job,
            "team": self.team,
            "description": self.description,
            "summary": skill_summary(self.description or self.body),
            "origin": self.origin,
            "enabled": self.enabled,
            "required": self.name in REQUIRED_ROLES,
        }


@dataclass(frozen=True)
class RoleVerdict:
    verdict: str
    summary: str
    parsed: bool = True


def bundled_roles_root() -> Path:
    return Path(__file__).resolve().parent.parent / "roles"


def local_roles_root(workspace: Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".lbw" / "roles"


def role_evidence(name: str, title: str, job: str, verdict: str, summary: str) -> dict[str, str]:
    return {
        "role": name,
        "title": title,
        "job": job,
        "verdict": verdict,
        "summary": (summary or "").strip()[:2000],
    }


def list_roles(root: Path) -> list[Role]:
    enabled = set(enabled_role_names(root))
    return [
        Role(
            name=role.name,
            title=role.title,
            job=role.job,
            description=role.description,
            body=role.body,
            origin=role.origin,
            path=role.path,
            enabled=role.name in enabled,
        )
        for role in _catalog(root)
    ]


def roles_catalog(root: Path) -> dict[str, object]:
    roles = list_roles(root)
    return {
        "roles": [role.public_dict() for role in roles],
        "enabled": [role.name for role in roles if role.enabled],
    }


def enabled_role_names(root: Path) -> list[str]:
    known = {role.name for role in _catalog(root)}
    raw = _load_raw_config(root)
    if "roles" not in raw:
        return [name for name in DEFAULT_ENABLED if name in known]
    block = raw.get("roles")
    if not isinstance(block, dict):
        block = {}
    enabled: list[str] = []
    for name in _string_list(block.get("enabled")):
        if name in known and name not in enabled:
            enabled.append(name)
    return enabled


def save_enabled_roles(root: Path, names: list[str] | None) -> list[Role]:
    known = {role.name for role in _catalog(root)}
    enabled: list[str] = []
    for name in list(REQUIRED_ROLES) + _string_list(names):
        if name in known and name not in enabled:
            enabled.append(name)
    raw = _load_raw_config(root)
    raw["roles"] = {"enabled": enabled}
    _write_raw_config(root, raw)
    return list_roles(root)


def plan_role(root: Path) -> Role | None:
    planners = [role for role in list_roles(root) if role.enabled and role.job == "plan"]
    for role in planners:
        if role.name == "architect":
            return role
    return planners[0] if planners else None


def optional_review_roles(root: Path) -> list[Role]:
    """Review roles the workspace turned on, besides the QA goal check."""
    return [
        role
        for role in list_roles(root)
        if role.enabled and role.job == "review" and role.name != "qa"
    ]


def parse_role_verdict(text: str | None) -> RoleVerdict:
    raw = (text or "").strip()
    blobs = [match.group(1).strip() for match in _FENCE.finditer(raw)]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        blobs.append(raw[start : end + 1])
    for blob in blobs:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        verdict = _VERDICT_ALIASES.get(str(data.get("verdict") or "").strip().lower())
        if verdict is None:
            continue
        summary = str(data.get("summary") or data.get("reason") or "").strip()
        if not summary:
            summary = {
                "pass": "passed",
                "send_back": "sent back",
                "block": "blocked",
            }[verdict]
        return RoleVerdict(verdict, summary[:500], parsed=True)
    return RoleVerdict("pass", "reviewer returned no verdict", parsed=False)


def review_with_role(
    llm: LLMClient,
    role: Role,
    *,
    goal: str,
    diff: str,
    summary: str,
    tests_passed: bool | None,
) -> RoleVerdict:
    """Ask one review role for a pass, send-back, or block verdict."""
    opinions = (role.body or "").strip()[:4000]
    system = (
        f"You are the {role.title}. One job: review this change.\n"
        "Reply with ONLY this JSON:\n"
        '{"verdict": "pass" | "send_back" | "block", "summary": "one or two sentences"}\n\n'
        "pass: the change meets your success criteria.\n"
        "send_back: the builder should fix a specific gap. summary must name "
        "what is missing or wrong, where it is, and what to change.\n"
        "block: the change must not ship. summary must name the exact problem.\n"
    )
    if opinions:
        system += f"\nOpinions:\n{opinions}\n"
    user = "\n".join(
        [
            "Goal:",
            (goal or "").strip(),
            "",
            "Agent summary:",
            (summary or "").strip() or "(none)",
            "",
            f"Tests passed: {tests_passed}",
            "",
            "Diff:",
            (diff or "").strip() or "(no diff)",
        ]
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        turn = timed_complete(llm, messages, [], purpose="role", role=role.name)
    except Exception as exc:  # noqa: BLE001 - a missed reviewer must not abort the cycle
        log.info("role %s review failed: %s", role.name, exc)
        return RoleVerdict("pass", "reviewer could not be reached", parsed=False)
    return parse_role_verdict(turn.text)


def _catalog(root: Path) -> list[Role]:
    by_name: dict[str, Role] = {}
    for role in _discover(bundled_roles_root(), origin="bundled"):
        by_name[role.name] = role
    local = local_roles_root(Path(root))
    for role in _discover(local, origin="local"):
        by_name[role.name] = role
    return sorted(by_name.values(), key=lambda role: (_ORDER.get(role.name, 50), role.name))


def _discover(root: Path, *, origin: str) -> list[Role]:
    directory = Path(root)
    if not directory.is_dir():
        return []
    found: list[Role] = []
    for path in sorted(directory.glob("*/ROLE.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        role = _parse_role(text, fallback_name=path.parent.name, origin=origin, path=str(path))
        if role is not None:
            found.append(role)
    return found


def _parse_role(text: str, *, fallback_name: str, origin: str, path: str) -> Role | None:
    raw = (text or "").replace("\r\n", "\n").strip()
    match = _FRONTMATTER_RE.match(raw)
    meta: dict = {}
    body = raw
    if match:
        loaded = yaml.safe_load(match.group(1) or "") or {}
        if isinstance(loaded, dict):
            meta = loaded
        body = (match.group(2) or "").strip()
    name = _clean_name(str(meta.get("name") or fallback_name))
    job = str(meta.get("job") or "").strip().lower()
    if not name or job not in ROLE_JOBS:
        return None
    title = str(meta.get("title") or name.replace("-", " ").title()).strip()
    description = str(meta.get("description") or "").strip()
    return Role(
        name=name,
        title=title[:80],
        job=job,
        description=description[:400],
        body=body[:8000],
        origin=origin,
        path=path,
    )


def _clean_name(value: str) -> str:
    name = (value or "").strip().lower()
    if _NAME_RE.match(name):
        return name
    return ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name = _clean_name(str(item or ""))
        if name and name not in names:
            names.append(name)
    return names


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
