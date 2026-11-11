---
name: ui
description: >-
  Lay out web UI so new controls stay clickable and do not cover existing
  ones. Use when editing HTML, CSS, templates, or client layout, and after
  any change that adds a button, modal, tab, or overlay.
---

# UI layout

Organization Models, Skills, and Roles, and project Skills and Roles, live
in `web/src/sections/`. Other sections stay in `templates/index.html` until
they move. After a change under `web/src`, run `make web` before pytest and
before `review_ui`. Visual style stays the bundled `style` skill. The plan
is `docs/frontend-react.md`.

A UI change is done when the rendered page shows the new control, a click
reaches it, and it does not cover or get covered by something already there.
A template diff is not done. A screenshot of the closed main screen is not
done.

## Style

Color, type, and component look are the bundled `style` skill. Read it
before changing how the page looks. When an engineer asks to change the
styling, update that skill in the same change, then match the CSS to it.

## Place the control

- Read the CSS for the region before adding anything. Find what is already
  `position: absolute` or `fixed` and which corner it occupies.
- One pinned control per corner. If that corner is taken, put the new action
  in normal document flow and reserve padding so the text underneath still
  clears it.
- Do not add a second button at the same `top`/`right`/`bottom`/`left` as an
  existing one. Nudging it by a few pixels still overlaps.
- On a task or history card, `.pr-link` and `.rerun-btn` already own the
  bottom-right, and `.history-line-stats` owns the bottom-left. A new control
  in either corner overlaps them. Put a new card action in a footer row, or
  reuse the existing rerun button, and keep the card's bottom padding.
- Rules, Skills, Schedules, Roles, and Models are rail sections beside Proposals,
  Changes, and Releases. The main pane shows the selected section. A schedule
  runs in the current project. It starts from a base branch, which defaults
  to main, and uses a model turned on for this project. A fix opens a pull
  request. When nothing needs changing, the result says it found nothing.
  A job shows who started it, who accepted a gate, and who sent it back.
  The project list does not include the checkout folder or store files.
  It shows a repository an owner or admin turned on, and only when the
  signed-in person's GitHub account can use it. One repository appears
  once. The project field filters that list as you type. The address bar
  can open a section, an organization or personal tab, and a project, as
  in `/?section=changes&project=owner/repo`. The project bar does not
  add, remove, or archive a project. An organization with no projects says
  that none are listed yet and points to the agent integration. A project shows an
  organization or team rule, and an organization skill, only when it is
  assigned to that project. Rules has two tabs. LBW rules are the standards
  stored for the project. Repository lists instruction files such as
  AGENTS.md, CLAUDE.md, and CURSOR.md from the primary branch. Each file
  is shown as rendered markdown. Each file
  loads into project work until its switch is turned off. Update reads that
  branch again. Releases show open, merged, and closed counts as pills at
  the right of each heading, in that pull-request color. Skills from a cloned repository start collapsed
  under that repository, with a count and a switch for the whole repository.
  Each skill has a switch. Hovering the name shows its summary, and the info
  control reads the skill. An empty search keeps them collapsed. A search
  opens the matches. Roles lists the jobs for this project. A switch turns
  a role on. Builder and QA stay on. An enforced role stays on and its
  switch cannot turn it off. Hovering the name shows its summary. Models
  lists the organization models for this project. A switch turns one on.
  A model turned on for the organization starts on. An organization admin
  or a repository admin can change it. That choice stays when the
  organization turns the model off and back on. The live
  console is on the engineer dashboard, not this page. Settings opens a small
  menu for Personal and Organization. Personal and Organization fill the
  main window. Personal uses tabs for Profile, Rules, and GitHub. Profile
  chooses the editor used to open a changed file.
  The GitHub tab appears once the organization has a GitHub App, and LBW
  stores the login GitHub returns. Rules lists each name. The plus button
  opens a dialog to add one, and a listed rule can be viewed as markdown,
  edited, or deleted. Organization settings include People and
  Teams. A team belongs to the organization and has members and admins. Edit on a
  team selects people to change rank or remove. The add button opens a dialog
  that searches people and adds several at once. Close it from Close or a click
  outside it. Remove team appears when the team has no people. The sign-in page can create another organization. Crossing guard is the engineer dashboard, a separate service from the organization site. Locally the organization site is `https://www-local.lookbothways.dev` and the dashboard is `https://crossing-local.lookbothways.dev`. On `127.0.0.1` the path is `/crossingguard`. An organization admin does not open it. It lists every organization. A menu adds another LBW engineer. The page lists LBW job failures and can unlock a person. It does not remove a team, and it does not show code or a pull request. A connected GitHub App can add one of its
  repositories to the organization, a person, or a team, and can sync access
  for people who connected. Organization settings open in the main window and
  switch with tabs for
  People, Teams, Rules, Skills, Roles, Models, and GitHub. Skills stores
  repositories for the whole organization. Each repository starts collapsed
  and shows how many skills it loaded. Opening it lists those skills. A
  switch turns the whole repository or one skill on for every project, an
  info control shows that skill, and Remove takes the repository off the
  organization. Resync is on that repository. A project turns an available
  skill on. Models holds endpoints, models, and agents in tables. An endpoint row shows its
  name, URL, and how many model names it returned, and can resync. Removing
  an endpoint asks in a dialog first. A model row shows the endpoint and the
  model name, with a switch and a remove button. The switch makes that model
  available on every project until a project turns it off. An
  agent row shows its name and kind the same way. Plus opens a dialog to add
  a model or an agent. The proposal screen chooses a model turned on for the
  project. It does not add a host from that screen.
  Do not put Rules or Skills back in an overlay.
- A new dialog uses the existing `.modal-backdrop` and `.modal`, starts
  `hidden`, and closes from its button or a click on the backdrop. A closed
  dialog must not intercept clicks.

## Wire the control

- Give the control an `id` or a `data-*` attribute. The click listener added
  in the same change must mention that exact token.
- A class that is only used for color or position is not a handler. If the
  review says `button.<class> has no click handler`, the listener does not
  reference that class.
- A new route is wired when the added page contains that path. `fetch`,
  `EventSource`, and a template URL all count. Do not add a second route
  that the page never calls.
- Do not leave a button, route, or form field that nothing calls.

## Prove the layout

- After the edit, call `review_ui`. Open the pane that contains the control
  (Current, Past runs, Skills, Rules) and click the new control.
- The report is a failure when it says overlaps, unreadable, 0×0, dead
  control, JavaScript error, or 404. Move the control or reserve space, then
  call `review_ui` again.
- Check the resting state and the opened state. A panel that looks fine
  closed can cover the rerun button, the PR link, or the diff counts once
  it is open.
- Call `run_tests` after the layout is clean. If a test asserts the old
  markup this change replaces, update that test to the new public behavior.

## Show the screen

A visual change is not done until something renders it. If `review_ui` says
there is no preview server, add one before claiming the control works.

- For a web app, call `configure_project` with `preview_command`. The command
  runs with `{port}` replaced and must listen on that port. Example:
  `python3 -m http.server {port}`. Look up the framework's official dev
  server when this repo does not already name one.
- The page has to show the control this goal changes and perform the action
  (open the menu, press Start, change the setting). A paragraph that says the
  screen works is not a preview.
- A project with `project.godot` is previewed by LBW. It runs Godot, captures
  the main scene, and serves that frame on `{port}`. Do not set
  `preview_command` to `echo` or `sleep`. If the review says Godot was not
  found, install Godot 4 or set `GODOT_PATH`, then call `review_ui` again.
- If the capture is missing, blank, or does not show the action, the goal is
  not met. Fix the app or the preview and call `review_ui` again.

## UI Component Patterns

### Project sections
Rules, Skills, Schedules, and Roles sit in the project rail with Proposals, Changes, and Releases.
A cloned skill repository is stored in a KiwiFS space named from the organization,
team, and project ids. Resync pulls the latest commit into that space. Remove
on a project takes off a repository stored on that project. An organization
repository stays.
Selecting one shows that section in the main pane. Those pages list what is
assigned to the project. A collapsed sidebar keeps each rail item as its
icon. Settings opens a small menu. Personal and
Organization then fill the main window. Roles fills the main pane from its
rail item. A role enforced by the organization stays on and its checkbox
cannot turn it off.

### Modal Dialogs
Modal dialogs should follow this structure:
- Use existing `.modal-backdrop` and `.modal` classes for consistent styling
- Start with `hidden` attribute to ensure they're not visible initially
- Include a close mechanism (button or backdrop click)
- Do not intercept clicks when closed
- Maintain consistent z-index stacking with other UI elements

### Task Cards
A change in the list is a compact card: the goal on one line, and the
project with its step under that. Selecting a change opens it. Its steps
are one track. Selecting a step loads that step under the track. Log,
Changes, and Screenshots stay as tabs under that step. The log tab is
labeled Log.

Task cards follow specific positioning rules:
- Bottom-right corner owned by `.pr-link` and `.rerun-btn`
- Bottom-left corner owned by `.history-line-stats` 
- New controls in these corners will cause overlap issues
- Use footer rows for additional actions on task cards
- Maintain proper padding to prevent clipping

### Form Elements and Buttons
When adding form elements:
- All buttons must have `id` or `data-*` attributes for click handlers
- Click handlers must reference the exact token used for the element
- No unused form fields, routes, or buttons should be left in the code
- Maintain consistent styling with existing UI components

### Responsive Layout Guidelines
The UI must maintain proper behavior across different screen sizes:
- Use CSS Grid and Flexbox for responsive layouts
- Avoid fixed positioning that may conflict with other elements
- Test both desktop and mobile views during review process