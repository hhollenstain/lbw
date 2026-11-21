# AGENTS.md

Practices for changing this repository. The cycle checklist for working on LBW
itself is also in `skills/agent-lbw/SKILL.md`.

## Layout

- Package code lives in `src/agent_lbw/`.
- Tests live in `tests/test_<area>.py`. Put a test next to the behavior it
  covers. Do not append a new feature's tests onto an unrelated file.
- Web UI is `src/agent_lbw/templates/index.html`, `static/`, and `web_ui.py`.
- The cycle is `runtime/improve.py`, `runtime/roles.py`, `runtime/review.py`, and `agent/loop.py`.
- Bundled skills ship in `src/agent_lbw/skills/<name>/SKILL.md`.
- Repo skills live at `skills/<name>/SKILL.md`.
- Prove a new behavior at a public seam: the `lbw` CLI, an `/api/...` route,
  or the rendered page. Do not add a helper, route, or control that nothing calls.

## Python

- Python 3.12+. Start modules that use annotations with
  `from __future__ import annotations`.
- A new or changed HTTP body starts in `openapi/lbw.yaml`. Run `make openapi`
  and use the generated model. The plan is `docs/openapi.md`.
- Line length is 100. Before finishing, run `uv run ruff format src tests`
  then `uv run ruff check src tests`. Ruff selects `E`, `F`, `I`, `UP`, and `B`.
- Use `pathlib.Path` for filesystem paths.
- Name caught exceptions `exc`. Tool and HTTP handlers keep failures in-band:
  tools return `ToolResult(False, message)`, and HTTP errors are
  `{"error": "..."}` with a 4xx status. Do not let a tool exception escape the
  tool runner.
- Prefer a public function over importing another module's `_private` helper.
- Keep imports at the top of the file, in the order ruff applies. A function
  may import a heavy or circular dependency locally when the top-level import
  would cycle.

## Web UI

- React sections live in `web/src/sections/`: organization Models, Skills,
  and Roles, and project Skills and Roles. Other sections stay in
  `index.html` until they move. A change under `web/src` needs `make web`
  before pytest and before `review_ui`. The plan is `docs/frontend-react.md`.
- JavaScript in `index.html` uses double quotes and `function` declarations for
  named behavior. Iterate with `for...of`. Sections that have not moved keep
  those rules.
- Rules, Skills, Schedules, Agent teams, and Models are rail sections beside Proposals,
  Changes, and Releases. A schedule runs in the current project from a base
  branch, which defaults to main. A fix opens a pull request. When the tree
  already meets the goal, the run reports that it found nothing. The main
  pane shows the selected section. Builder and QA stay on.
  An organization admin can enforce a role on every project.
- UI layout is the bundled `ui` skill. Visual style is the bundled `style`
  skill. New workspaces enable both with `tdd`.
  After a UI edit, exercise the control in a browser: open it, use it, and
  close it. A template diff is not done. `review_ui` must show the control
  without overlap, unreadable controls, dead buttons, or 404s.
## CSS

- Extract inline styles from templates into `static/layout.css`.
- Do not add inline `style=` attributes to HTML elements in templates.
- Define CSS classes in `layout.css` and apply them to HTML elements.
- Visual style is the bundled `style` skill. When an engineer asks to change
  how the site is styled, update that skill in the same change, then make
  `themes.css` and `layout.css` match it.
- After CSS changes, run `review_ui` to verify the page renders correctly.

## Skills and the cycle

- Agent process belongs in a skill, not in `SYSTEM_PROMPT` or the agent loop.
- If you add a bundled skill, keep `package-data` including `skills/*/SKILL.md`.
- A cycle is finished only when the stated goal is wired through. A later cycle
  is not a substitute for missing data or UI.
- After behavior changes, run `uv run pytest -q`. After Python edits, format
  with ruff, then lint.

## Directory database

Shared rows live in `runtime/store/`. Each file owns one concern: `schema` creates
the tables, `crypto` seals secrets, `install` is the local account, `teams`,
`duties`, and `projects` are access, `tasks` stores a request, `schedules`
indexes when one should run, and `recording` is what the task queue and the
scheduler call.

- Every shared row carries a tenant id. A tenant is `personal` or `organization`.
  A personal tenant has its owner and no teams. Promote it before adding a team.
  Promotion keeps the tenant id, so projects and tasks stay put.
- A person is an identity, separate from a membership. Rank is `owner`, `admin`,
  or `member`, once on the tenant and once on a team. The first password is
  the owner's, created on the setup page when no password exists. Later sign-in
  uses that email and password. The password is hashed. The session cookie is
  random and only its hash is stored. Sign-in proves an existing person and
  opens that session. A later SSO or SAML login, such as Entra or Auth0,
  matches the same person by email and opens the same session. An organization has members and admins.
  Teams belong to that organization and have members and admins. An empty team can be removed. A person can create another organization from the sign-in page. A crossing guard is an LBW engineer, marked on the person, and is not an organization admin. The engineer dashboard is its own service, `crossing`, in `services/crossing`. It shares `libs/lbw_shared` with the organization site. Compose publishes both through one port. A host whose first label is `crossing` or `crossing-` reaches that service. Locally the organization site is `https://www-local.lookbothways.dev` and the dashboard is `https://crossing-local.lookbothways.dev`. Nginx terminates the local certificate and sends each host to its service. On `127.0.0.1` the path `/crossingguard` is sent to the dashboard. It lists every organization. A menu there adds another LBW engineer. The page can unlock a person and store SSO settings. It does not remove a team, and it does not show a goal, a diff, or a pull request. A job is listed there only when LBW itself failed. Each person
  has a name and an email. Duties are additive
  grants, `billing`, `security`, or `rules`, on a tenant or a team. An owner
  already holds every duty, so do not write a duty row for an owner. A new duty
  is a new value, not a new kind of person.
- An organization owner or admin can use every project in the tenant. Everyone
 else needs a `use`, `write`, or `manage` grant on their team or on themselves.
 `manage` includes `write`. `write` includes `use`. A proposal pauses at a
 proposal group until a person with write access continues it. GitHub push is
 `write`. Pull is `use`. Admin and maintain are `manage`.
- Seal task goals and the GitHub App private key with `runtime/store/crypto.py`
  and the tenant key. The master key stays in `.lbw/master.key`. It does not go
  in the database, a prompt, or a commit. Do not add a second cipher. The
  `github_apps` row stores the app id, the sealed private key, the sealed
  client secret, and the chosen installation. Do not store an installation
  token or a user access token. A GitHub project's locator is `owner/repo`.
  The checkout is scratch for a job. When a database is configured, the project
  list and the run list come from that database. The memory palace is a cache
  outside the checkout, keyed by tenant and project, and it is rebuilt from
  the task rows. A developer keeps that app in gitignored
  `github.local.yaml` and restores it with `lbw hydrate`. That file is not
  under `.lbw/` and is not committed. Sync stores every repository the app
  can see. Each row has its own id, the GitHub repository id, and this
  organization's id. An owner or admin turns one on from Organization. A
  repository GitHub has archived stores that time and is not shown. The
  project list shows a repository when it is on and the person's linked
  GitHub account can use it. The same repository is stored once. The
  project bar does not add, remove, or archive a project. The checkout is the workspace. A person connects GitHub
  from the GitHub tab on their personal page once the organization has a
  GitHub App. Personal rules are
  listed by name. The plus button opens a dialog to add one, and a listed rule
  can be viewed as markdown, edited, or deleted. LBW
  reads their login from GitHub and stores it on `identities.github_login`.
  The client secret is sealed with the tenant key and is not returned. The
  user token is used once to read the login and is not stored. Sync copies that
  account's organization role, team membership, and repository access onto the
  existing grants. It does not create people.
- Do not store model context, prompts, tool traces, or logs in the database.
  Those stay in the run directory. A task row is the request and its outcome:
  tenant, project, requester, status, branch, pull request, `run_ref`,
  `goal_ciphertext`, and `result_ciphertext`. The result seals the plan, the
  steps, the outcome, and the final diff so history can show the file changes
  after the checkout is gone. Logs are not part of it.
- An audit row notes who changed the organization or a job: the person, the
  time, the action, the API path, and a short summary of what changed. Do not
  put a secret, a prompt, or a log line in it. A job records who started it.
  A gate records who accepted it, and who sent it back with a note.
- A schedule row is an index. The instruction stays in the schedule file.
  `scan` indexes a project schedule. `change` indexes a file schedule. Do not
  copy the instruction into the database. A project schedule stores its base
  branch and the granted model with that instruction. The default branch is
  main. An empty model does not fall back to a server that is not running.
- A rule's text lives in its own KiwiFS space: one for the tenant, one for a
  team, one for a project, and one for a person. The `rules` table indexes it.
  `rule_requirements` says which organization rules a project must follow. Do
  not store the rule body in the database. Derive the space from the scope id.
  Do not take a space name from the client. The `rules` duty lets a member edit
  rules for that tenant or team. Project rules follow `manage`. A repository
  instruction file such as AGENTS.md stays in git. LBW reads it from the
  primary branch. Turning one off is recorded in the workspace and does not
  copy the file into the database.
- A cloned skill repository is indexed in `skill_repos`. The files and the git
  commit stay in KiwiFS. The space is `sk-org-<id>`, `sk-org-<id>-team-<id>`,
  or `sk-org-<id>-team-<id>-proj-<id>`. Derive it from those ids. Do not put an
  organization, team, or project name in the space. A project reads the
  organization space, a team space only when that team can use the project,
  and its own project space.
- The hierarchy is organization, then teams, then projects. An organization
  rule or skill reaches a project only when it is assigned there: an
  organization rule through `rule_requirements`, a team rule through a project
  grant, a skill through `skill_grants` on the organization, a team, or that
  project. Organization settings stores skill repositories for the whole
  organization and resyncs them there. It does not assign a skill to a
  project. A project turns a loaded skill on. A project page shows the skills
  assigned to it. An organization can create many agent teams. A team has an
  instruction and a model, and the model's endpoint is the service it calls.
  An owner or admin can change all of them later. A team owns one or more
  steps in `agent_team_steps`. A step plans, builds, or reviews, can wait for
  other steps with `after`, and a build step can own plan steps by `paths`.
  Step names are unique in the organization. The graph rules live in
  `runtime/team_graph.py`: no loops, no wait on a later phase, and one plan
  step writes the plan. Steps that do not wait on each other run side by side,
  except steps on the same model and build steps, which take turns. A project
  turns a team on. In a phase a team covers, the teams replace the ROLE.md
  roles. A phase no team covers runs the roles, and enforced roles always run.
  "Turn roles into teams" (`import_roles`) makes a team for each role a project
  runs. The proposal pauses at a proposal group for a person with write
  access. `role_requirements` is the set of bundled tasks an organization
  owner or admin enforces on every project.
- A model is a bring-your-own endpoint on the organization: OpenAI, Claude,
  Google, Ollama, vLLM, LM Studio, Cursor, or another OpenAI-compatible server.
  Claude takes an Anthropic API key and a proposal runs it through the Messages
  API. Cursor takes an API key from the Cursor dashboard and lists that
  account's models, such as Grok. Cursor does not serve chat completions, so a
  proposal runs through a gateway that does. LiteLLM and other gateways speak the OpenAI
  API. A gateway takes a virtual key, or a sign-in token when it accepts this
  organization's Entra or Auth0 login. That token is sealed for the person who
  signed in. It is not the session. An endpoint can instead ask each person
  to check their own API key, or to sign in. A run uses that person's
  credential, so the provider can attribute the spend. A
  proposal runs the model chosen for the project. OpenAI, Claude, and the
  other endpoints are that model. There is no separate agent to add. The API key is sealed with
  the tenant key and is not returned. Turning a model on in
  organization settings makes it available on every project. A project turns
  one off from its Models page. That choice is kept when the organization
  turns the model off and back on. Rank does not include a model. A team
  grant applies on a project that team can use.
  An endpoint stores the model names it returned. Refresh replaces
  that list. Ids are `ep_`, `model_`, and `mgr_`.
- An integration is an endpoint an owner or admin sets to personal keys or
  sign-in. Each person links it from Integrations on their profile: a personal
  key, a gateway sign-in, or for Claude a linked Claude organization through
  Claude Platform OAuth (`runtime/store/claude_link.py`, beta). A Claude Pro or
  Max subscription is never used: Anthropic does not allow it in other apps.
  A job runs on the requester's link. Whoever continues it at a gate, asks for
  rework, or reruns it has their own link used from then on, and is refused
  with `409 link_required` until they link it. Never fall back to another
  person's credential. The worker runs as that person (`set_actor`), and each
  run reads the credential again. The audit trail notes `task.credentials` at a
  gate and `task.usage` per run.
- A runner is a person's own machine, linked with a device code
  (`runtime/store/runners.py`, design in `docs/runner.md`). Only the token's
  hash is stored. A job with `run_on: my_runner` (`agent_kind = claude_code`)
  goes to the acting person's online runner and never to a coworker's. The
  runner runs that person's Claude Code and sends commits back as a patch. LBW
  applies them on `lbw/runner-<id>`, runs the tests, and publishes. The goal,
  events, and patch pass through `runtime/runner_hub.py` in memory, not the
  database. The runner is Go in `runner/`, standard library only.
- Usage lives in daily rollups, never per model call: see `docs/usage.md`.
  Reports read only those rollups, inside a bounded range, behind a 60-second
  cache. Add a breakdown through the registry in `runtime/store/usage.py`.
- Extend these checks when adding access. Do not add a second permission table.

## Git

- Do not commit, stage, or push `.lbw/` or `history.json`. Config, cloned
  skills, run logs, and screenshots stay local.
- Publishing commits the project files, pushes that branch, and then opens the pull request. The pull request is not opened while those files are still uncommitted, or before the push succeeds.
- Do not force-push, skip hooks, or rewrite published history.
- Do not bind-mount this app's `.lbw` over `/workspaces/.lbw`.
- `lbw init` needs a directory that already exists. Do not document
  `docker clone`.
- Do not ship stubs, TODOs, unused form fields, or "this enables X later".
