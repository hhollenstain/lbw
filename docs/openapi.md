# OpenAPI first

New HTTP behavior starts in `openapi/lbw.yaml`. The spec names the path, the
request, and the response. `make openapi` reads that file and writes
`src/agent_lbw/openapi_models.py`. A handler imports those models. It does
not grow a second copy of the same fields by hand.

`make openapi-check` fails when the generated file does not match the spec.
CI runs that check. The running image does not run the generator. It imports
the file that was committed with the spec.

Routes that are not in the spec yet stay as they are. When a group moves,
add its paths and schemas, regenerate, and point the handler at the generated
model in that same change. The first groups in the spec are schedules, the
audit log, and repository instruction files.

## Conditional GET

Every JSON `GET` under `/api/` is conditional, on the organization site and
on crossing guard. `lbw_shared.conditional.ConditionalGetMiddleware` does it
for both apps, so a handler does nothing to opt in.

- A `200` carries an `ETag` made from the exact body, `Cache-Control:
  private, no-cache`, and `Vary: Cookie`. A handler's own `Cache-Control`
  is kept.
- A request whose `If-None-Match` names that ETag gets `304 Not Modified`
  with no body. A browser sends the header on its own and hands `fetch` its
  stored copy, so page code reads a `200` either way.
- Errors, streams (`text/event-stream`), files and other non-JSON bodies,
  and a response that sets `no-store` or its own `ETag` are sent as they
  are. Use `no-store` for a body that must never be kept, such as a secret.
- The spec lists `304` on each `GET` through `#/components/responses/NotModified`.
  A new `GET` in the spec lists it too.

The handler still runs on each request. A poll saves the transfer and the
browser's parsing, not the handler's work. An endpoint whose work is costly
keeps its own check before building the body, as `/api/history` does with
its `token`.

The page polls at a pace that matches the work: every 2 seconds while a job
runs or waits in the queue, every 10 seconds when nothing moves, and not at
all while the tab is hidden. A person's own action refreshes at once.
