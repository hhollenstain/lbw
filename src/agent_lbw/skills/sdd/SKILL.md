---
name: sdd
description: >-
  Spec-driven development. Use when there is a spec, requirements document, or
  detailed goal description, and to write the spec a proposal starts from.
  Prefer the spec throughout the cycle and verify work against it.
---

# Spec-Driven Development

Spec-driven development (SDD) means using specs as the primary source of truth
for what to build. This skill is especially useful when there is a requirements
document, design spec, or detailed specification that you should follow.

## Read the spec first

- Locate the spec in the issue body, comments, linked documents, or existing
  files in the workspace.
- Read the entire spec before making any changes.
- Note explicit requirements, constraints, acceptance criteria, and edge cases.
- If multiple specs exist, prefer the most recent or the one at the root of
  the workspace.

## Use the spec throughout the cycle

- Every change should map to an item in the spec. Do not implement behaviors
  that are not in the spec unless explicitly required.
- After each edit, verify against the spec. Ask: "Does this satisfy the
  current requirement?"
- When the agent runs are done, use the spec to form acceptance criteria.
  Each requirement should have a corresponding verification.

## Handle missing specs

- If the spec is unclear or missing, ask for clarification or propose one.
- Do not guess requirements. Document what you found and ask for confirmation.
- When a spec is ambiguous, prefer conservative behavior that can be adjusted.

## In this workspace

- Read issue comments and linked documents as part of the spec.
- Verify changes against both the stated goal and any additional spec that
  appears during the cycle.
- Use the `review` skill to check your work against the spec before finishing.

## Writing a spec

A spec-driven proposal starts by writing a spec. A person with write access
reads it, edits it or sends it back, and only then is the change planned and
built. The spec is committed beside the code in `specs/`.

- Write what the change must do and how anyone can tell it is done. Do not
  write the plan or the code.
- Read the files you are given. Name real modules, routes, commands, and
  screens; do not invent them.
- Every requirement is one sentence a reviewer can check, numbered `R1`,
  `R2`, and so on.
- Every acceptance criterion is observable: a test that passes, a command and
  its output, or what a screen shows. Each one names the requirement it proves.
- Keep it short: 3 to 8 requirements for one change.
- List what is out of scope, so the builder does not wander.
- When the request is unclear, say so under open questions instead of guessing.
- When you are sent back with a note, keep what the note does not touch and
  change what it asks for.

Reply with only the spec, in this shape:

```markdown
# <Title>

## Problem
<Why this change is needed, in two or three sentences.>

## Requirements
- R1: <one checkable sentence>

## Acceptance criteria
- [ ] <observable check> (R1)

## Out of scope
- <what this change does not do>

## Open questions
- <anything the reviewer should decide, or "None">
```
