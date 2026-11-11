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
`--card-lift`. The goal is a single line. The project and the step share
the muted line under it. Hover adds a short `--navy` shadow. The selected
card keeps an `--accent` border and a `3px` `--accent` bar on the left. Who
started the job and who accepted a gate stay on the open change. Merge,
Request changes, and Rerun sit inside the card only when that job needs them.

A change lists its steps in one track under that line. Each step is the
name only. The selected step shows its state and an `--accent` underline.
Its detail follows the track, without a second title card. Log, Changes,
and Screenshots stay as tabs under that detail. The log tab is labeled Log.
A second strip of Init, tests, lint, review, and publish squares is not
shown.

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
