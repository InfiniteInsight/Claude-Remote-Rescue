# Tracking dialect — Claude-Remote-Rescue

Stamped by recursive-spine bootstrap on 2026-07-31. Maps this repo's local
vocabulary onto the [recursive-spine principles](https://github.com/slopstopper).

## Unit of work

A **Task** — matching existing practice: implementation plans under
`docs/superpowers/plans/` are decomposed into numbered Tasks, each TDD'd and
reviewed. A Task maps to an **issue**; a plan (a narrative of Tasks) maps to
a **milestone**.

## Modules stamped

| Module | Label | Meaning here |
|---|---|---|
| Deferral (mandatory) | `deferred` | Postponed with a record — nothing is deferred without an issue |
| Gap | `gap` | Findings from audits/assessments (e.g. the 2026-07-29 bug-hunt audit); work issues cite the gaps they close |
| Debt | `inherited-debt` | Known-incomplete edges filed before a Task's issue closes |

Lane module: not stamped (no model-routing labels).

## Branch / PR convention

Branches: `<prefix>/<issue>-<slug>` (e.g. `fix/42-reviver-dismissed`).
PRs close their issue (`Closes #N`). In-flight work is a query
(`gh issue list --assignee @me`), never a prose file.

## Cross-project board

Owner: **InfiniteInsight** (user-level "Spine" Projects board).
`SPINE_BOARD_NUMBER`: **2** (hooked up 2026-07-31 after `gh auth refresh -s project`;
open issues added via `gh project item-add 2 --owner InfiniteInsight`). For
whole-repo aggregation, enable the board's auto-add workflow for this repo in
the board settings: https://github.com/users/InfiniteInsight/projects/2/settings
