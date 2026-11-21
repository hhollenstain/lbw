# Usage insights

LBW records what each run spends and changes, and shows it on three
dashboards: a project's Usage section, a person's Usage tab, and the
organization's Usage tab. They are one React component
(`web/src/sections/usage/UsageDashboard.tsx`) that reads everything it offers
from `GET /api/usage/meta`.

## How a run is recorded

A run is one stretch of work: from the start, or from a gate, to the next stop.
When it stops, `TaskManager._record_usage` calls `recording.record_usage`, which
calls `usage.record_run` once. It folds the run's progress events into daily
rollups and adds to them with an upsert:

| Table | One row per | Numbers |
| --- | --- | --- |
| `usage_daily` | day, project, person, service, model, step, operation | calls, failed calls, input, output, cache read, cache write tokens, model time, cost |
| `usage_files_daily` | day, project, person, file type | files, lines added, lines removed |
| `usage_skills_daily` | day, project, person, skill | runs using the skill, tokens in those runs |
| `usage_runs_daily` | day, project, person, outcome | runs, run time |

The person is the one whose credentials ran: the requester, or whoever acted at
the last gate. Files count only what changed since the job's previous run, so a
rework does not count lines twice. The audit trail gets one `task.usage` row per
run that called a model, naming whose credentials spent the tokens.

Prompts, responses, and logs are not stored here. Only counts.

## Standards

- Token names follow the OpenTelemetry GenAI semantic conventions. Input
  tokens include cached input (`gen_ai.usage.input_tokens`); cache reads and
  writes are also counted on their own
  (`gen_ai.usage.cache_read.input_tokens`,
  `gen_ai.usage.cache_creation.input_tokens`). `/api/usage/meta` lists the
  OpenTelemetry name next to each breakdown and number that has one.
- Cost is an estimate from the organization's price table (dollars per
  million tokens, by provider and model). It is fixed when the run is recorded,
  like a bill line; a later price change does not rewrite history.
- `GET /api/usage?format=csv` exports the same report for a spreadsheet or a
  FinOps tool.

## Keeping the database calm

- A report reads only rollup tables, never tasks or events.
- Every report has a date range, at most 366 days, and at most 50 groups.
- The same report for the same organization is answered from memory for 60
  seconds, and the response says `Cache-Control: private, max-age=60`.
- Each rollup has indexes on `(tenant_id, day)`, `(tenant_id, project_id, day)`,
  and `(tenant_id, identity_id, day)`.
- A dashboard fetches nothing until it is on screen.

## Who sees what

| Scope | Who | Shows |
| --- | --- | --- |
| `me` | everyone | their own usage |
| `project` | people who can use the project, and org usage readers | that project |
| `org` | owners, admins, and the `billing` duty on the organization | everything |

## Add a breakdown

1. Add the column to the rollup table in `runtime/store/schema.py`, with a
   default, and add it to that table's primary key. Bump `SCHEMA_VERSION`; add
   it with `_add_column` for existing databases.
2. Fill it in `usage.record_run`.
3. Add one `Dimension` to `DIMENSIONS` in `runtime/store/usage.py`, and its key
   to the dataset's `dimensions`.

The API and the dashboard pick it up. Add a test in `tests/test_usage_reports.py`.

## Add a number

Add one `Measure` to the dataset in `runtime/store/usage.py`: a key, a label, an
SQL aggregate over that table's columns, and a unit (`tokens`, `count`, `ms`, or
`usd_micros`). If it needs a new column, add the column first as above.

## Add a dataset

Create a rollup table keyed by `(tenant_id, day, project_id, identity_id, ...)`,
write it from `record_run`, and add a `Dataset` to `DATASETS`.
