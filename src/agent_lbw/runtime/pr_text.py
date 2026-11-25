"""The pull request a build agent writes about its own change.

The agent that made the change knows it best, so its final reply names the pull
request and describes it for a reviewer. LBW reads that reply, adds what it
verified itself (tests, screenshots, the commit), and publishes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PR_TEXT_INSTRUCTIONS = """\
End with the pull request for this change, for a reviewer who has not seen the goal.
Use exactly this shape, with nothing after it:

PR title: <what the change does, in the imperative, under 72 characters>

## Summary
<two to four sentences: what changed and why>

## Changes
- <one line per area or file: what changed in it>

## How to test
1. <steps a reviewer follows: commands to run, pages to open, what to click>

## What to check
- <behavior and edge cases to confirm, and anything a reviewer must know:
  migrations, new settings, breaking changes, risks, or what is still open>

The title names the change, not the request: never repeat the goal's first line.
Describe the whole change on this branch, including earlier work on this goal,
not only your last fix. Keep each section short, and cover every part of it."""

_TITLE = re.compile(r"^\s*(?:[*_]{1,2})?PR title(?:[*_]{1,2})?\s*:\s*(.+?)\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{2,3}\s+\S", re.MULTILINE)
_FENCE = re.compile(r"^\s*```")
TITLE_LIMIT = 72


@dataclass(frozen=True)
class PullRequestText:
    title: str
    body: str


def _clip(text: str) -> str:
    clean = " ".join(text.split()).strip().strip("`*_\"'").strip()
    if len(clean) <= TITLE_LIMIT:
        return clean
    cut = clean[:TITLE_LIMIT].rsplit(" ", 1)[0].rstrip(".,;:-")
    return cut or clean[:TITLE_LIMIT].rstrip()


def written_pull_request(summary: str | None) -> PullRequestText | None:
    """The title and body the agent wrote, or None when its reply has none.

    The last ``PR title:`` line starts the pull request. The body is the
    sections after it and needs at least one ``##`` heading.
    """
    lines = (summary or "").splitlines()
    start = -1
    title = ""
    for index, line in enumerate(lines):
        found = _TITLE.match(line)
        if found:
            start, title = index, _clip(found.group(1))
    if start < 0 or not title:
        return None
    rest = [line for line in lines[start + 1 :] if not _FENCE.match(line)]
    body = "\n".join(rest).strip()
    if not _HEADING.search(body):
        return None
    return PullRequestText(title=title, body=body)
