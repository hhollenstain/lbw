---
name: research
description: >-
  Look up official documentation and current best practices on the web
  before inventing an API, architecture, or recommendation. Use when the
  repo does not already contain the answer.
---

# Research

Local models guess stale APIs and weak architecture. Search and read
official docs before you invent a library call, framework pattern, or
"best practice" that is not already in this repo.

## When to look it up

Call `web_search`, then `fetch_url` on an official page, when you need:

- A library or framework API you have not seen in this workspace
- Current recommended architecture for a well-known problem
- Version-specific behavior, deprecations, or security guidance
- How an official tool, protocol, or language feature is supposed to work
- How this language or framework should be tested, linted, or previewed
  when `.lbw/config.yaml` has no command for it

Do not search the web for code that is already in the workspace. Call
`glob_files` and `search_text`, then `read_file`, first.

## How to look it up

1. Call `web_search` with the error line itself: the `ERROR:` text, the
   `res://` path, and the line number. Include an error code when the
   message has one. Do not replace that text with a general topic.
2. Prefer official sources in the results: project docs, MDN, language
   references, RFCs, and the library author's site. Skip random SEO blogs
   when an official page is present.
3. Call `fetch_url` on that official URL and use what it says.
4. If you already know the docs URL, skip search and `fetch_url` it directly.

## After you read

- Implement from the docs you fetched, not from memory that disagrees with them.
- When the docs say how to test, lint, or capture this stack, call
  `configure_project` with those commands. `preview_command` must listen on
  `{port}`. Do not leave LBW validating a Godot, native, or other non-web
  app with a command you invented.
- Write how to run, build, and test this project into `AGENTS.md` from the
  page you fetched. Name the real entry point and the commands. If that
  section is already there, correct it. Do not paste a long API guide.
- If the same failure comes back, look it up again before another edit of
  the same file. Do not repeat a guess the docs do not support.
- If search and fetch fail, say so and stop guessing at an invented API.
