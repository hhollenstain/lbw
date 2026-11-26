---
name: style
description: >-
  Style the organization site from this guide. Use when editing CSS, themes,
  color, type, or component look, and when an engineer asks to change how
  the site is styled. Update this skill in that same change.
---

# Site style

Agents style the organization site from this guide. Color lives in
`static/themes.css`. Component look lives in `static/layout.css`. The page
is `templates/index.html`. Light is the default. Dark is
`html[data-theme="dark"]`.

## When an engineer changes the style

An engineer asking to change how the site is styled updates this skill in
that same change. Write the new color, type, spacing, shape, or component
rule here, then make the CSS match it. A CSS change that leaves this skill
describing the old look is not done. Do not add a second palette, type
scale, or button style beside the ones below.

## Color

Use the variables in `themes.css`. A component does not get its own hex
color. A new color is added to both `:root` and `[data-theme="dark"]`, and
to the list below.

| Variable | Use |
| --- | --- |
| `--bg`, `--bg-subtle` | Page, and the sidebar |
| `--panel`, `--panel-2` | Cards, fields, secondary buttons |
| `--ink`, `--muted` | Text, and labels, hints, kickers |
| `--line` | Borders and dividers |
| `--navy`, `--on-navy` | Primary buttons and page titles |
| `--accent`, `--on-accent`, `--ok` | Logo mark, success, open work |
| `--series-1` | The one data series in a chart: usage bars and breakdown bars. Checked for contrast on `--panel` in both themes |
| `--warn`, `--bad` | Caution, and failure |
| `--queued` | Queued work |
| `--pr-open`, `--pr-merged`, `--pr-closed` | Pull request state |
| `--code-bg` | Code |
| `--shadow`, `--card-lift`, `--overlay` | Raised panels, compact cards, and backdrops |

Status color uses those names. A new status reuses one of them unless the
engineer adds a token to both themes and to this table.

A repository instruction file is rendered markdown in a panel: `--panel`
background, `1px` `--line` border, `8px` radius, and room to scroll. Headings,
lists, and code use the same type as a skill. The file is not shown as a
monospace dump.

A release count sits at the right of its heading. It is a pill: `1px`
border, full radius, and a light fill in that pull-request color. Open uses
`--pr-open`, merged uses `--pr-merged`, and closed uses `--pr-closed`. The
heading words stay `--muted`.

The changes column is `--bg-subtle`. A change in that list is one compact
card on `--panel`, with a `1px` `--line` border, an `8px` radius, and
`--card-lift`. The goal is a single line. A status dot, the step, and the
project share the muted line under it. The dot takes the group color and
blinks while the change runs. A waiting change names the next action, such
as Review the plan, in `--warn`. Hover adds a short `--navy` shadow. The selected
card keeps an `--accent` border and a `3px` `--accent` bar on the left. Who
started the job and who accepted a gate stay on the open change. Merge,
Request changes, and Rerun sit inside the card only when that job needs them.

An open change shows its goal as the title, `--navy` at `1.2rem` weight
680, with the project, branch, model, and time in the muted line under it.
Its steps are one flow track: a round `1.6rem` dot per step joined by a
`2px` line, with the step name and its state under the dot. A done step is
a filled `--ok` dot with a check. The working step is a `--navy` ring that
pulses. A step waiting on a person is a filled `--warn` dot with a person
glyph and reads Your turn. A failed step is a filled `--bad` dot. A skipped
step has a dashed dot and a dashed line. A checkpoint not reached yet keeps
the person glyph on a `--warn` outline. The selected step has a light
`--navy` fill. In a narrow pane only the working, waiting, or failed step
keeps its state text.

A rerun keeps the change's title. An info banner between the title and the
track says how the run differs: Run #2 reruns this change starting at Build,
or from the start, and under This run, the request when it is not the goal,
such as fixing failed checks or merging the base. It is `--panel` tinted with
`--navy`, a `4px` `--navy` left border, an `8px` radius, and a round `--navy`
info mark.

Stop on a queued or running change is a secondary button with its text in
`--bad` and a `--bad` tinted border, inside the action card.

One action card sits under the track: `--panel`, `8px` radius, a `4px` left
border in the state color, a pill badge (Your turn, Working, Queued,
Failed, Done), and a title. Your turn tints the card with `--warn` and
Failed with `--bad`. A plan or spec in the card is on `--code-bg`. The
selected step's detail is a bordered panel under the card, and is left out
when the card already covers that step. Log, Changes, Screenshots, and
Audit stay as tabs under that. The log tab is labeled Log. A second strip
of Init, tests, lint, review, and publish squares is not shown. Pulsing
and blinking stop under `prefers-reduced-motion`.

An open pull request's card lists its CI checks in a bordered panel, with
a muted count line beside the title (2 failed · 1 running · 1 passed). Each
check is a row divided by a `1px` `--line` rule: a round `1.1rem` state icon,
the check name in `--ink` weight 600 with its summary muted under it, and a
muted Details link with an arrow at the right that turns `--navy` and
underlines on hover. Passed is a filled `--ok` dot with a check, failed a
filled `--bad` dot with an ×, and running a `--warn` ring that pulses. A
failed check has a checkbox before its icon for Investigate failures.
A merge conflict with the base branch is the first row, drawn as a failed
check without a checkbox, and Fix merge conflict sits above Investigate
failures with a hint under it.

The proposal form draws the path a change will take with the same flow
track, in a `Path` panel above its checkboxes. A checkpoint a person stops
at shows the person glyph. A skipped checkpoint is dashed and reads
Automatic. The checkboxes sit in Checkpoints and Result groups with
sentence-case `--ink` labels, not uppercase field labels.

The sidebar project list has no border of its own. A row is the project
name at `0.88rem` in `--ink`, with an `8px` radius and a `--panel` fill on
hover or while the keyboard is on it. The current project is `--navy`
weight 680 with a small `--accent` dot before it. Matched letters in a
search are bold and underlined, not tinted. A group label (Favorites,
Current, All projects) is uppercase `--muted` at `0.7rem`. The star is an
outline in `--muted` that shows on hover. A favorite is a filled `--warn`
star and always shows. All projects and Show fewer are a plain `--muted`
text button that turns `--navy` on hover. The list scrolls past `16rem`.

The model picker on the proposal form and the rerun dialog looks like a
field: `--panel`, `1px` `--line` border, `10px` radius, a muted chevron at
the right. It shows the chosen model's name in `--ink` weight 600 with its
group and endpoint muted under it. Opened, it is a `--panel` list with an
`8px` radius and `--shadow` under a search field, and opens in place inside
a dialog. Group labels (Claude, Claude Code · your machine, OpenAI, Local
models · your machine) are uppercase `--muted` at `0.7rem`. A row is the
name with its endpoint muted under it, and a `--panel-2` fill on hover or
while the keyboard is on it. The chosen model is `--navy` weight 680 with a
small `--accent` dot. Matched letters are bold and underlined, as in the
project list. A note such as Used last time is a muted outline pill.

A person's synced local models are an `.org-table` of Model and Tag under a
Synced models heading with a muted count pill. The model name is `--ink`
weight 600. The tag, such as `7b`, is a monospace pill on `--code-bg`. The
names are not a run of inline code.

## Type

Body text is `15px/1.5` system UI. The logo wordmark is Avenir Next, weight
600, letter-spacing `0.16em`. A section kicker is `.ask-kicker`: uppercase,
`--muted`, `0.72rem`. A page title is `.ask-heading`: `--navy`, `1.55rem`,
weight 680. Labels are uppercase `--muted` at `0.78rem`. Hints are `.hint`
at `0.78rem` `--muted`.

## Shape

Buttons, fields, and icon buttons use a `10px` radius. Cards, rail items,
and inbox rows use `8px`. Status chips use a full pill. Borders are `1px
solid var(--line)`. The page is `--bg`. The sidebar is `--bg-subtle`.
A collapsed sidebar is a narrow column. Each project section stays as its
icon, centered in a `2.15rem` button. The name stays available to the
reader and is not drawn.

## Controls

A primary button is `button`: `--navy` background and `--on-navy` text. A
secondary button is `button.secondary`: `--panel-2` background, `--ink`
text, and a `--line` border. An icon button is square, about `2.15rem`,
with a `10px` radius. A disabled button is `opacity: 0.5`. Put the class in
`layout.css`. Do not add an inline `style` attribute.

An organization table is `.org-table`. The header is uppercase `--muted` on
`--panel-2`. Rows sit on `--panel` with a `--line` divider and an `8px`
radius around the whole table. A switch is `.org-switch`: a pill, off on
`--panel-2` with a `--muted` knob, on with `--accent` and an `--on-accent`
knob. A remove control is a secondary icon button with a trash glyph. A
section plus is a secondary icon button. A skill loaded from a repository
is an `.org-skill-chip` pill. A project model uses the same row and switch
as a skill.

## Theme

The theme control sets `data-theme="dark"` on the document element. Light
removes that attribute. Every color variable exists in both themes.

## Charts

The usage dashboard draws one series at a time in `--series-1`. Bars are thin,
with a 4px rounded top on the baseline, and the axis is a `--line` rule. Values,
labels, and ticks stay in `--ink` and `--muted`, never the series color. A bar
has a hover tooltip on `--panel`. A breakdown is a table with a bar beside each
value, so a reader never depends on color. A chart never gets a second y-axis.
