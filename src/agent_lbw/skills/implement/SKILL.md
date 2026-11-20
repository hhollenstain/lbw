---
name: implement
description: >-
  The process for implementing a task: restate the goal, name files to change,
  use str_replace on existing files, and call run_tests and review_ui as needed.
---

# Implementation Process

This skill guides the agent on how to **start** a task cleanly and execute changes reliably.

## Restate the goal

Before any tool call, restate the goal in one sentence. This ensures alignment and prevents drift. The restate must appear in the text body (not just internal reasoning).

## Name files to change

Identify 1–5 files that will be edited. Call `glob_files` when you know the name and `search_text` when you know a symbol. Prefer `str_replace` on existing files rather than creating new ones. List these files explicitly before any edit.

## Follow the plan

The cycle breaks the goal into steps before the first edit. Do the current
step only. Confirm that step's check — the test, the error that must stop,
or what the screen shows — before the next step. Do not say the task is
complete while a step's check is still false. A null check, print, or log
is not a step.

## Look up unknowns

When the API, architecture, or a best practice is not already in this repo,
call `web_search` and then `fetch_url` on an official docs page before
inventing one. Do not guess library APIs or current recommendations.
That includes how this language or framework is tested, linted, and
previewed. Call `configure_project` with the commands the docs support.

Follow the other enabled process skills: **explore** before adding structure,
**debug** when the goal is a failure, **security** on input or secrets,
and **review** before you stop.

## Edit files

- Use `str_replace` for precision. Provide the exact `old_string` from the file.
- Use `delete_file` when this change removes a file. Do not delete a directory.
- Do not repeat the same `str_replace` if it fails; adjust the arguments instead.
- Do not add and remove the same small edit. A comment, blank line, or the same text put back and taken out is not the fix. Change the behavior the goal asks for.
- Do not add scaffolding, mocks, TODOs, or unused form fields that don't serve this goal.

## Verify

- After functional edits: call `run_tests` once. If no code has changed since the last run, including in another project in the workspace, LBW does not run the suite again. Do not call it again until you edit code.
- If there is no test command, look up the official test runner, add a test at the public seam, and call `configure_project` with that `test_command`, then call `run_tests`. The command must execute the suite and exit non-zero when a check fails.
- If `run_tests` fails, fix the code or the test that asserts the old behavior, then call `run_tests` again. Do not change the command so it exits 0 without running the failing check.
- After tests, call `run_lint` once. Do not call it again until code changes. If it says there is no lint command, look up
  the official linter and call `configure_project` with `lint_command`, then
  call `run_lint` again. A ruff command formats, then checks.
  Fix F401, E501, import order, and any files ruff would reformat before
  stopping.
- After HTML/CSS/JS/template edits: call `review_ui` once, click the new control, fix overlap/404/dead buttons. Do not call it again until code changes.
- If a tool fails, change arguments; do not repeat the same failed command.

## Stop when done

- If the tree already satisfies the goal, say so and stop. Do not continue editing.
- Do not leave the skill disabled or skip verification steps.
- Do not add process to `SYSTEM_PROMPT`. This skill contains the process.
