"""A review index: what a reviewer can read when a file is too long to show.

LBW shortens long files and diffs before a reviewer sees them. When it does,
the review evidence ends with an index of the changed files, built from
conventions agents already use instead of a new format:

- each file is named by its git blob id (`git hash-object`), so the reviewer
  reads exactly the bytes under review, and a later edit is caught;
- each file has an outline of line ranges, like a ctags index or an LSP
  document outline: Markdown headings, Python classes and functions, or
  definitions in other languages, else fixed blocks of lines;
- the reviewer reads a range with `read_file(path, start_line, end_line)`, the
  same shape as the builder's tool and common agent file tools.

The reader opens only the files in the index, read-only, for a few calls.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_lbw.llm.client import AssistantTurn, LLMClient
from agent_lbw.llm.toolparse import parse_tool_calls
from agent_lbw.progress import timed_complete
from agent_lbw.tools.base import ToolResult, ToolSpec, object_schema

INDEX_HEADING = "Review index"
MAX_FILES = 20
MAX_ENTRIES = 40
MAX_READS = 6
MAX_READ_LINES = 300
MAX_READ_CHARS = 24_000
INDEX_LIMIT = 6_000
BLOCK_LINES = 100
_SHA_CHARS = 12
_ENTRY_LABEL = 90
_FILE_LINE = re.compile(r"^(?P<path>\S.*?) · blob (?P<blob>[0-9a-f]{7,40}) · (?P<lines>\d+) lines$")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:function|class|interface|type|enum|struct|trait|impl|func|fn|def|module)\s+"
    r"([A-Za-z_][\w.]*)"
)
_FINAL_NUDGE = "You have read enough. Reply now with only the JSON your instructions ask for."


def blob_sha(data: bytes) -> str:
    """The git object id of these bytes, the same as `git hash-object`."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git's id


def outline(path: str, text: str) -> list[tuple[int, int, str]]:
    """Line ranges for the file's parts: (first line, last line, label)."""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return []
    starts: list[tuple[int, str]] = []
    suffix = Path(path).suffix.lower()
    if suffix in {".md", ".markdown", ".mdx", ".rst", ".txt"}:
        fenced = False
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            heading = None if fenced else _MD_HEADING.match(line)
            if heading:
                starts.append((number, f"{heading.group(1)} {heading.group(2)}"))
    elif suffix == ".py":
        starts = _python_starts(text)
    else:
        for number, line in enumerate(lines, start=1):
            found = _DEFINITION.match(line)
            if found:
                starts.append((number, line.strip()))
    if not starts:
        return _cover(
            [
                (first, min(first + BLOCK_LINES - 1, total), "lines")
                for first in range(1, total + 1, BLOCK_LINES)
            ]
        )
    if starts[0][0] > 1:
        starts.insert(0, (1, "(top of file)"))
    ranges = []
    for spot, (first, label) in enumerate(starts):
        last = starts[spot + 1][0] - 1 if spot + 1 < len(starts) else total
        ranges.append((first, last, label[:_ENTRY_LABEL]))
    return _cover(ranges)


def _cover(ranges: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """At most MAX_ENTRIES ranges that still cover the whole file, end included."""
    if len(ranges) <= MAX_ENTRIES:
        return ranges
    size = -(-len(ranges) // MAX_ENTRIES)
    merged = []
    for spot in range(0, len(ranges), size):
        group = ranges[spot : spot + size]
        label = group[0][2] if len(group) == 1 else f"{group[0][2]} … {group[-1][2].strip()}"
        merged.append((group[0][0], group[-1][1], label[: _ENTRY_LABEL * 2]))
    return merged


def _python_starts(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    starts: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            starts.append((node.lineno, f"class {node.name}"))
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    starts.append((item.lineno, f"  def {node.name}.{item.name}"))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            starts.append((node.lineno, f"def {node.name}"))
    return sorted(starts)


def review_index(root: Path, paths: list[str]) -> str:
    """The index block for these changed files, or "" when none can be read."""
    blocks: list[str] = []
    used = 0
    for path in paths[:MAX_FILES]:
        full = root / path
        if not full.is_file():
            continue
        try:
            data = full.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        rows = [f"{path} · blob {blob_sha(data)[:_SHA_CHARS]} · {len(lines)} lines"]
        rows += [f"  L{first}-L{last}  {label}" for first, last, label in outline(path, text)]
        block = "\n".join(rows)
        if used + len(block) > INDEX_LIMIT:
            break
        blocks.append(block)
        used += len(block) + 1
    if not blocks:
        return ""
    head = (
        f"{INDEX_HEADING}: LBW shortened the evidence above. These changed files are whole "
        "in the workspace. Read any part with read_file(path, start_line, end_line) "
        f"before you decide; you have {MAX_READS} reads."
    )
    return head + "\n" + "\n".join(blocks)


@dataclass
class ReviewReader:
    """Read-only access to the files a review index lists, for a few calls."""

    root: Path
    files: dict[str, str]
    reads: int = 0
    seen: list[str] = field(default_factory=list)

    @classmethod
    def from_evidence(cls, root: Path, evidence: str) -> ReviewReader | None:
        """The reader for the index in this evidence, or None when it has none."""
        start = evidence.rfind(f"\n{INDEX_HEADING}: ")
        if start < 0:
            return None
        files: dict[str, str] = {}
        for line in evidence[start:].splitlines():
            found = _FILE_LINE.match(line)
            if found:
                files[found.group("path")] = found.group("blob")
        return cls(Path(root), files) if files else None

    @property
    def spent(self) -> bool:
        return self.reads >= MAX_READS

    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description=(
                "Read lines of a file listed in the review index. Lines are 1-based and "
                f"at most {MAX_READ_LINES} per read. The `N|` prefix is the line number."
            ),
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "A path from the review index."},
                    "start_line": {"type": "integer", "description": "First line to read."},
                    "end_line": {"type": "integer", "description": "Last line to read."},
                },
                ["path"],
            ),
            handler=self.read,
        )

    def read(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ToolResult:
        clean = str(path or "").strip().removeprefix("./")
        blob = self.files.get(clean)
        if blob is None:
            listed = ", ".join(sorted(self.files))
            return ToolResult(False, f"{path} is not in the review index. You can read: {listed}")
        if self.spent:
            return ToolResult(False, "No reads left. Decide from what you have read.")
        self.reads += 1
        full = (self.root / clean).resolve()
        if not full.is_relative_to(self.root.resolve()) or not full.is_file():
            return ToolResult(False, f"{clean} is gone from the workspace")
        data = full.read_bytes()
        if not blob_sha(data).startswith(blob):
            return ToolResult(False, f"{clean} changed after the review began; it was not read")
        lines = data.decode("utf-8", errors="replace").splitlines()
        first = max(1, _line(start_line, 1))
        last = min(len(lines), _line(end_line, first + MAX_READ_LINES - 1))
        last = min(last, first + MAX_READ_LINES - 1)
        if first > last:
            return ToolResult(False, f"{clean} has {len(lines)} lines")
        numbered = [
            f"{first + spot:>5}|{line}" for spot, line in enumerate(lines[first - 1 : last])
        ]
        body = f"{clean} lines {first}-{last} of {len(lines)} · blob {blob}\n" + "\n".join(numbered)
        self.seen.append(f"{clean}:{first}-{last}")
        return ToolResult(True, body[:MAX_READ_CHARS])


def _line(value: object, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def complete_review(
    llm: LLMClient,
    messages: list[dict],
    reader: ReviewReader | None,
    *,
    purpose: str,
    role: str = "",
) -> AssistantTurn:
    """One review answer. With a reader, the reviewer may read indexed files first."""
    if reader is None:
        return timed_complete(llm, messages, [], purpose=purpose, role=role)
    tool = reader.tool()
    for _ in range(MAX_READS + 1):
        if reader.spent:
            break
        turn = timed_complete(llm, messages, [tool.openai_schema()], purpose=purpose, role=role)
        native = bool(turn.tool_calls)
        calls = turn.tool_calls or parse_tool_calls(turn.text, {tool.name})
        if not calls:
            return turn
        if native:
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in calls
                    ],
                }
            )
        else:
            messages.append({"role": "assistant", "content": turn.text or ""})
        for call in calls:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            if call.name != tool.name:
                output = f"Only {tool.name} is available during review."
            else:
                result = reader.read(
                    str(args.get("path") or ""), args.get("start_line"), args.get("end_line")
                )
                output = result.output
            if native:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output}
                )
            else:
                messages.append(
                    {"role": "user", "content": f"Tool {call.name} returned:\n{output}"}
                )
    messages.append({"role": "user", "content": _FINAL_NUDGE})
    return timed_complete(llm, messages, [], purpose=purpose, role=role)
