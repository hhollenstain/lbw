"""Shared names, ranks, and id helpers for the directory tables."""

from __future__ import annotations

import re
import uuid

NAME_LIMIT = 80
RANKS = frozenset({"owner", "admin", "member"})
DUTIES = frozenset({"billing", "security", "rules"})
TENANT_KINDS = frozenset({"personal", "organization"})
PROJECT_KINDS = frozenset({"github", "git", "folder"})
ACCESS_LEVELS = frozenset({"use", "write", "manage"})
SCHEDULE_KINDS = frozenset({"change", "scan"})


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clean_name(value: str, label: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > NAME_LIMIT:
        raise ValueError(f"{label} must be {NAME_LIMIT} characters or fewer")
    return text


def slug_for(name: str, tenant_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "tenant"
    return f"{base}-{tenant_id[-6:]}"[:60]
