---
name: explore
description: >-
  Read how this repo already solves the problem before inventing a new
  module, pattern, or layer. Use on every change that adds behavior.
---

# Explore

Local models invent architecture. This repo already has one. Match it.

## Before you add a type or file

1. Read `AGENTS.md` if it exists.
2. `glob_files` for a filename. `list_dir` one directory when you need its
   contents. `search_text` for the names, routes, or errors in the goal.
   Pass `path` to stay in one area and `context` to see the lines around a hit.
3. Read one or two existing modules at the same seam (CLI, HTTP handler,
   template, test). Copy their shape: names, error objects, imports, tests.

## Match what is here

- Extend an existing function or route when it already owns the behavior.
- Reuse the project's error style (`{"error": "..."}`, `ToolResult`, raised
  types — whatever this repo uses). Do not introduce a new one.
- Put a new test next to the behavior it covers (`tests/test_<area>.py`).
- Do not add a framework, directory layout, service layer, or helper that
  nothing in this change calls.

## Stop exploring

Read at most a handful of files, then edit. Inspection is not a finish.
If the existing pattern is unclear, `web_search` official docs for the
library this repo already uses — do not switch libraries.
