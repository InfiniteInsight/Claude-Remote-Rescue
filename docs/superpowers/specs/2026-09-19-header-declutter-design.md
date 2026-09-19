# Dashboard header declutter: Devices in Settings, compact key legend

**Date:** 2026-09-19
**Status:** approved (user-approved design; this doc is its record)

## Problem

User feedback: "The top of the page has a lot of important information and it
is very cluttered." Two concrete contributors, both above the fold before a
single session card renders:

1. **`#tools`** (the "Other views" toolbar, `page.html` ~438-445) carries five
   buttons — Recently untracked, Discoverable, Why did sessions die?, 📱 Add a
   device, Tailnet Members — of uneven importance. The last two are about
   *reaching the dashboard from elsewhere*, not about the sessions on screen,
   and don't need to be one tap away from the main view.
2. **`#key`** (the state/context/remote-control/sid legend, `page.html`
   ~399-404) is a permanently-visible, 4-row wall of text (~90px) rendered on
   every load, before the user has seen a single card. It's still valuable —
   the user was explicit it should stay — but its footprint is disproportionate
   to how often any one row is actually consulted.

## Decisions (user, 2026-09-19)

### Part 1 — Move Add a device + Tailnet Members into Settings

- Both move into the existing Settings modal (`#admin-modal`), as two new
  entries grouped under one new **"Devices"** heading — not two separate
  ungrouped sections, and not merged into one existing section. Both are
  fundamentally the same kind of thing (reaching this dashboard from another
  device/machine), so they read as one topic.
- Placement: right after the existing **Dashboard Login** section
  (`#login-section`, `page.html:458-481`) and before **Auto-kick**
  (`#autokick-section`, `page.html:482-490`) — grouped with the other
  "how do I get to/use this dashboard" concern, ahead of the more advanced
  Auto-kick/Tunnel/Excluded-directories configuration below it.
- **No JS or backend changes.** `adddev-btn`/`machines-btn`'s click handlers
  (`page.html:1781-1803`) and the `renderMachines` function look up
  `adddev-box`/`adddev-qr`/`machines-panel` purely by `getElementById` — never
  by DOM position or a sibling relationship — so relocating the buttons *and*
  their target boxes (`#adddev-box`, `#machines-panel`) into the new
  `#devices-section` is a pure HTML move. Same ids, same behavior, same
  `/api/machines` fetch, same QR `/qr.svg` re-fetch-on-open logic.
- `#tools` keeps Recently untracked / Discoverable / Why did sessions die? —
  unchanged.
- Styling: `#tools button` (`page.html:312-316`) styled these two buttons
  today (rounded, bordered, muted). Once moved out of `#tools` they lose that
  rule. Rather than invent a new button style or leave them looking like bare
  unstyled `<button>`s (which is what Tunnel's Save/Up/Down already look like
  — an existing inconsistency, not something this change is scoped to fix),
  give them the settings modal's existing **secondary-button** look already
  used by "Cancel" in the login sections: `padding:6px 12px;
  background:transparent; color:#8a93a2; border:1px solid #2f3745;
  border-radius:6px; cursor:pointer; font-size:13px;`.

### Part 2 — Key legend: compact row, expands to the full legend

Chosen from three mocked-up options (collapsed-by-default / always-visible
compact chips / hybrid) — the user picked the **hybrid**.

- **Compact row (default, every load, no persistence — user's explicit
  choice: "Always start collapsed").** One line:
  - The **state** group renders as 5 small colored dots + short labels
    (live/ghost/crashed/restored/attached), using the same colors as today's
    `.k-live`/`.k-ghost`/`.k-crashed`/`.k-parked`/`.k-attached` classes — this
    is the group people scan most, so it keeps visual identity even
    collapsed. Like the rest of `#key`, this is a **static vocabulary
    reference**, not a live summary of what's currently on screen — the same
    5 dots render every time, regardless of which states any card actually
    has right now (matching how the full legend behaves today).
  - The **context**, **remote control**, and **sid** groups collapse to
    bare group-name labels (just the label text, reusing the existing
    `.klabel` style — no background/border shape, no term list) in the
    same row. (Corrected 2026-09-19: the mocked-up options called these
    "pills," but the approved mockup's `.chip` class never actually
    rendered a pill shape either — this wording just matches what was
    shown and approved.)
  - No `data-help`/tap-to-toast on anything in the compact row — seeing an
    individual term's explanation is only available once expanded (see
    below), so the compact row has exactly one behavior, not two competing
    click targets.
- **Click anywhere on the compact row → expands** to reveal today's exact
  4-row legend (`page.html:399-404`, unchanged content, unchanged
  `data-help`/tap-to-toast behavior on each `.kterm`).
- **Expanded view carries a "▴ less" affordance** that collapses back to the
  compact row. Clicking it (or, for symmetry, the row itself again) toggles
  back.
- Nothing about the individual term explanations changes — this only changes
  what's visible by default and what one extra tap reveals.

## Testing

Same approach as the last three dashboard PRs (v72-v74): Python string
assertions against the served page source (`web.load_page()`/
`web.render_page()`), no browser/JS runner. New coverage needed:

- `#devices-section` exists between `#login-section` and `#autokick-section`
  in source order, contains `id="adddev-btn"` and `id="machines-btn"`, and
  neither id appears inside `#tools` anymore (mirrors the existing
  `test_settings_button_lives_in_the_header_as_a_gear_icon` pattern of
  slicing the source between markers and asserting presence/absence).
- `#adddev-box` and `#machines-panel` also moved into `#devices-section` (not
  left behind at their old top-level location) — assert their ids appear
  within that section's slice too.
- The compact-row markup exists (5 state dots/labels + 3 bare group-name
  labels) and the full legend's existing content/ids are unchanged.
- A click handler exists that toggles between compact and expanded (structural
  assertion on the toggle function/class, matching how existing toggle logic
  — e.g. the worktree-collapse expander (`#34`) — is tested elsewhere in this
  file: asserting the function and the class/attribute it flips, not runtime
  behavior).
- `PAGE_VERSION` bump + new `PAGE_PINS` entry, per the established discipline.

## Out of scope

- Any change to the Recently untracked / Discoverable / Why did sessions die?
  buttons or their panels.
- Any change to individual key-term wording or their `data-help` explanations
  — only their default visibility changes.
- A general pass over the Settings modal's inconsistent button styling
  (unstyled Tunnel buttons vs. styled Cancel/primary buttons) — noted above
  as a pre-existing inconsistency, not something this change fixes beyond
  giving the two newly-moved buttons a sensible existing style to reuse.
- Persisting the expanded/collapsed key-legend state across reloads — the
  user explicitly chose always-collapsed-on-load over remembering it.
