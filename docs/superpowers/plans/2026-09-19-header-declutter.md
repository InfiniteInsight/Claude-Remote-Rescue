# Dashboard header declutter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce clutter at the top of the CRR dashboard by moving two device-related buttons into Settings, and by making the `#key` legend show a compact one-line summary by default that expands to the full legend on tap.

**Architecture:** Both changes are pure `crr/core/page.html` HTML/CSS/JS edits — no backend, no API, no data model changes. Task 1 relocates existing markup (same element ids, same click handlers, which look elements up purely by `getElementById` with no DOM-position dependency) from the top-level `#tools` toolbar into a new section inside the existing `#admin-modal` (Settings). Task 2 replaces `#key`'s single always-visible block with two sibling blocks — a compact one-line summary and the existing full legend, verbatim — toggled by the same `el.hidden = true/false` idiom already used elsewhere on this page (e.g. `#diag-panel`, `#adddev-box`).

**Tech Stack:** Vanilla ES5 JavaScript embedded in `crr/core/page.html` (no build step, no runtime dependencies — stdlib-only per project constraints). Tests are Python string/substring assertions against the served page source (`crr/core/web.py`'s `load_page()`/`render_page()`), matching this codebase's existing convention — there is no browser/JS test runner in this repo.

**Design doc:** `docs/superpowers/specs/2026-09-19-header-declutter-design.md`

## Global Constraints

- **ES5-only JS in `page.html`**: no `const`/`let`/arrow functions — the whole file uses `var`/`function` throughout. Match it exactly.
- **Zero runtime dependencies**: stdlib-only web server, no new libraries, no build step.
- **No JS/backend behavior changes in Task 1**: `adddev-btn`/`machines-btn`'s existing click handlers, and the `renderMachines` function, must be left completely untouched — they already look up their targets purely by `getElementById`, so relocating the HTML around them is sufficient. Do not add, remove, or rewrite any JS in Task 1.
- **Task 2 must not touch the full legend's content or its per-term help behavior**: the exact same `.kgroup`/`.klabel`/`.kterm`/`data-help` markup that exists today moves, unchanged, into the new `#key-full` container. The existing tap-to-toast wiring (`querySelectorAll("#key .kterm")`, `page.html` ~2518-2522) is untouched — it still finds the same elements regardless of the new wrapper div around them.
- **Reuse existing CSS classes for the compact row's colors**: `.k-live`/`.k-ghost`/`.k-crashed`/`.k-parked`/`.k-attached` already render a colored dot via `#key .k-live::before` etc. (`page.html` ~45-59) — apply these classes directly to the new compact-row spans (without the `.kterm` class, since the compact row has no per-term tap-to-toast) rather than inventing a new dot mechanism.
- **`PAGE_VERSION` bump discipline**: every commit that changes `crr/core/page.html` MUST bump `PAGE_VERSION` in `crr/core/web.py` by 1 and append (never edit) a new entry to `PAGE_PINS` in `tests/test_page_version_guard.py`, computed from the actual post-edit file hash (`test_page_version_guard.py` prints the exact sha to use when it fails). **Starting point for this plan: `PAGE_VERSION` is currently 74.** Task 1 takes it to 75, Task 2 to 76.
- **TDD**: write the failing test against `page.html`'s source text first, watch it fail, then edit `page.html` to make it pass.
- **Use the repo's own venv for every test/lint command: `.venv/bin/pytest`, never the bare `pytest` on PATH.** A bare `pytest` invocation can silently resolve a globally editable-installed `crr` package from a *different* checkout instead of this one's own source. `.venv/bin/pytest` is what the pre-commit hook actually runs (`.git/hooks/pre-commit` → `.venv/bin/pytest -q`).
- **Frequent, small commits**: one commit per task, each ending with a full `.venv/bin/pytest` run (the repo's pre-commit hook runs the whole suite — ~2500 tests, ~1.5-3.5 minutes; budget for that, don't work around it).
- **Work on a feature branch, not `main` directly**: create a branch before Task 1 (this repo's convention for every non-docs change so far — v71 through v74 each shipped via its own branch + PR).

---

### Task 1: Move Add a device + Tailnet Members into a new "Devices" section in Settings

**Files:**
- Modify: `crr/core/page.html` — remove `adddev-btn`/`machines-btn` from `#tools` (currently ~line 443-444); remove `#adddev-box`/`#machines-panel` from their current top-level location (currently ~line 447-451); insert a new `#devices-section` inside `#admin-modal`, between `#login-section` and `#autokick-section` (currently ~line 481-482).
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 74 → 75.
- Modify: `tests/test_page_version_guard.py` — append a new `PAGE_PINS` entry.
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: nothing new — `adddev-btn`/`machines-btn`'s click handlers and `renderMachines()` (unmodified, already `getElementById`-based) still find their targets after the move.
- Produces: nothing later tasks depend on — Task 2 touches a completely separate part of the page (`#key`).

- [ ] **Step 1: Create a branch**

```bash
git checkout main
git pull
git checkout -b feat/header-declutter
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_web.py`, near the other Settings-modal tests (e.g. right after `test_settings_button_lives_in_the_header_as_a_gear_icon`):

```python
def test_devices_section_lives_in_settings_not_tools():
    # User feedback 2026-09-19: Add a device + Tailnet Members moved out of
    # the "Other views" toolbar into a new "Devices" section inside Settings,
    # grouped with Dashboard Login (both are about reaching this dashboard),
    # ahead of the more advanced Auto-kick/Tunnel/Excluded-directories config.
    page = web.render_page()
    login_i = page.index('id="login-section"')
    devices_i = page.index('id="devices-section"')
    autokick_i = page.index('id="autokick-section"')
    assert login_i < devices_i < autokick_i, (
        "Devices section must sit between Dashboard Login and Auto-kick"
    )
    devices = page[devices_i:autokick_i]
    for needle in ('id="adddev-btn"', 'id="adddev-box"', 'id="adddev-qr"',
                   'id="machines-btn"', 'id="machines-panel"'):
        assert needle in devices, f"{needle} must live in the Devices section"
    tools = page[page.index('id="tools"'):page.index("</div>", page.index('id="tools"'))]
    assert 'id="adddev-btn"' not in tools, "Add a device must no longer be in #tools"
    assert 'id="machines-btn"' not in tools, "Tailnet Members must no longer be in #tools"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_devices_section_lives_in_settings_not_tools -v`
Expected: FAIL — `id="devices-section"` doesn't exist yet, so `page.index(...)` raises `ValueError`.

- [ ] **Step 4: Remove the two buttons from `#tools`**

In `crr/core/page.html`, `#tools` currently reads:

```html
<div id="tools">
  <span class="tools-label">Other views</span>
  <button id="untracked-btn" type="button" title="Sessions you recently untracked — one tap to put them back under crr's management. Opens a searchable list.">Recently untracked</button>
  <button id="discoverable-btn" type="button" title="claude conversations on disk that crr never tracked — adopt one to make it recoverable. Opens a searchable list.">Discoverable</button>
  <button id="diag-btn" type="button" title="Plain-English verdict on why the previous boot or your sessions died (out-of-memory, kernel panic, unexpected shutdown, clean reboot), above the raw system evidence. Expands below.">Why did sessions die?</button>
  <button id="adddev-btn" type="button" title="Scan a QR code to open this dashboard on your phone, over your tailnet.">📱 Add a device</button>
  <button id="machines-btn" type="button" title="All crr dashboards on your tailnet.">Tailnet Members</button>
</div>
<div id="diag-panel" hidden></div>
<div id="adddev-box" hidden>
  <p>Scan to open this dashboard on another device:</p>
  <img id="adddev-qr" alt="dashboard QR code" width="220" height="220">
</div>
<div id="machines-panel" hidden></div>
```

Change it to (removing the last two buttons from `#tools` and removing the two panel divs entirely from this location — they move to Step 5):

```html
<div id="tools">
  <span class="tools-label">Other views</span>
  <button id="untracked-btn" type="button" title="Sessions you recently untracked — one tap to put them back under crr's management. Opens a searchable list.">Recently untracked</button>
  <button id="discoverable-btn" type="button" title="claude conversations on disk that crr never tracked — adopt one to make it recoverable. Opens a searchable list.">Discoverable</button>
  <button id="diag-btn" type="button" title="Plain-English verdict on why the previous boot or your sessions died (out-of-memory, kernel panic, unexpected shutdown, clean reboot), above the raw system evidence. Expands below.">Why did sessions die?</button>
</div>
<div id="diag-panel" hidden></div>
```

- [ ] **Step 5: Insert the new Devices section into Settings**

Still in `crr/core/page.html`, `#login-section` and `#autokick-section` currently read (only the boundary matters — do not change anything *inside* either section):

```html
    <div id="login-section" style="padding: 10px 16px 0;">
      ...
    </div>
    <div id="autokick-section" style="padding: 10px 16px 0;">
```

Insert a new section between that closing `</div>` and the `#autokick-section` opening tag:

```html
    <div id="login-section" style="padding: 10px 16px 0;">
      ...
    </div>
    <div id="devices-section" style="padding: 10px 16px 0;">
      <h3 style="margin:0 0 6px; font-size:12px; color:#8a93a2; font-weight:600;">Devices</h3>
      <button id="adddev-btn" type="button" title="Scan a QR code to open this dashboard on your phone, over your tailnet." style="padding:6px 12px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:13px;">📱 Add a device</button>
      <button id="machines-btn" type="button" title="All crr dashboards on your tailnet." style="padding:6px 12px; background:transparent; color:#8a93a2; border:1px solid #2f3745; border-radius:6px; cursor:pointer; font-size:13px; margin-left:6px;">Tailnet Members</button>
      <div id="adddev-box" hidden>
        <p>Scan to open this dashboard on another device:</p>
        <img id="adddev-qr" alt="dashboard QR code" width="220" height="220">
      </div>
      <div id="machines-panel" hidden></div>
    </div>
    <div id="autokick-section" style="padding: 10px 16px 0;">
```

(The button style matches this modal's existing secondary/"Cancel" button style — e.g. `cancelLoginSetup()`'s button — rather than inventing a new one or leaving them unstyled. `#adddev-box`/`#machines-panel` keep the exact same ids and their own existing CSS rules — `page.html` ~261-281 — which are id-selector-based and apply regardless of where in the DOM the elements live, so no CSS changes are needed for them.)

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_web.py::test_devices_section_lives_in_settings_not_tools -v`
Expected: PASS.

- [ ] **Step 7: Run the existing Add-a-device/machines tests to confirm no regression**

Run: `.venv/bin/pytest tests/test_web.py -k "add_a_device or machines or secondary_views" -v`
Expected: all PASS (these assert id presence and JS behavior text anywhere in the page — not position-dependent — so the move doesn't affect them).

- [ ] **Step 8: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`, the current line reads:

```python
PAGE_VERSION = 74  # v74: Settings moved to a header gear icon (was buried in the "Other views" toolbar); excluded-directories rows wrap instead of overflowing on narrow screens (user feedback 2026-09-18); v73: explanation toasts (badge long-press, #key legend tap) are sticky, not auto-dismissed — 3s wasn't enough to read a full sentence; v72: session-card status badges long-press to reveal an explanation, via the same showNotice toast the #key legend already uses on tap; v71: auth badge no longer treats "unknown" as healthy — visible muted badge + Reauth button, and the expired badge names its source
```

Change it to:

```python
PAGE_VERSION = 75  # v75: Add a device + Tailnet Members moved into a new Settings "Devices" section (were buried in the "Other views" toolbar, user feedback 2026-09-19); v74: Settings moved to a header gear icon (was buried in the "Other views" toolbar); excluded-directories rows wrap instead of overflowing on narrow screens (user feedback 2026-09-18); v73: explanation toasts (badge long-press, #key legend tap) are sticky, not auto-dismissed — 3s wasn't enough to read a full sentence; v72: session-card status badges long-press to reveal an explanation, via the same showNotice toast the #key legend already uses on tap; v71: auth badge no longer treats "unknown" as healthy — visible muted badge + Reauth button, and the expired badge names its source
```

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v`
Expected: FAIL, printing the exact sha256 to pin, e.g. `75: "<sha>",`.

Add that line at the **top** of `PAGE_PINS` in `tests/test_page_version_guard.py` (existing entries are never edited):

```python
PAGE_PINS: dict[int, str] = {
    75: "<sha from the failing test output>",
    74: "7f0e33da99bc34b15bb5d84345e71ed9a038bca49a80bb00cd55c6e35023bea3",
    ...
```

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v`
Expected: PASS.

- [ ] **Step 9: Update the version-history docstring test**

In `tests/test_web.py`, find this function (currently ~line 1090-1164 — its docstring is a chained changelog, each version's entry wrapping the previous one in parens):

```python
def test_page_version_is_74():
    """v74: Settings moved from the "Other views" toolbar to a bare gear
    icon in the header (top right) — user feedback 2026-09-18 that its old
    spot wasn't intuitive. Excluded-directories rows now wrap instead of
    overflowing on narrow screens (missing flex-wrap/min-width: 0 let a
    long config.toml path force the row wider than a phone screen).
    (v73: explanation toasts (badge long-press, #key legend tap) are
```

(...the rest of the docstring continues unchanged, all the way down to `(v46 gave parked cards Kick/Close, #58)."""` — copy the ENTIRE existing function body as-is, you are only changing the three things shown below.)

Change exactly three things:
1. Rename the function from `test_page_version_is_74` to `test_page_version_is_75`.
2. Prepend this new paragraph immediately after the opening `"""`, before the existing `v74: Settings moved...` text (so `v74:` becomes a parenthesized entry like all the others, matching the existing pattern exactly):

```
v75: Add a device + Tailnet Members moved from the "Other views" toolbar
into a new "Devices" section inside Settings, grouped with Dashboard
Login — both are about reaching this dashboard, not about the sessions on
screen (user feedback 2026-09-19). No JS/behavior change: same ids, same
click handlers, which already looked their targets up by getElementById,
never by DOM position.
(v74: Settings moved from the "Other views" toolbar to a bare gear
```
3. Change `assert web.PAGE_VERSION == 74` to `assert web.PAGE_VERSION == 75`.

Everything else in the docstring — the whole v73-down-to-v46 chain — is copied verbatim, unchanged.

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all pass (matches the pre-commit hook).

- [ ] **Step 11: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): move Add a device + Tailnet Members into Settings (v75)

User feedback 2026-09-19: the top of the page was cluttered. These two
buttons are about reaching this dashboard from another device/machine, not
about the sessions on screen, so they don't need to be one tap away from
the main view. Moved into a new "Devices" section inside Settings, grouped
with Dashboard Login (same "how do I reach this dashboard" concern), ahead
of the more advanced Auto-kick/Tunnel/Excluded-directories config below it.
Pure HTML relocation — same ids, same click handlers, same behavior; the
JS already looked its targets up by getElementById, never by DOM position.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

---

### Task 2: Compact, expandable `#key` legend

**Files:**
- Modify: `crr/core/page.html` — CSS (new rules for `#key-compact`/`#key-full`/`#key-less`, near the existing `#key`/`.k-live::before` rules ~line 38-59); HTML (`#key`'s content, currently lines ~399-404); JS (new toggle wiring, right after the existing `#key .kterm` tap-to-toast block, currently ending ~line 2522).
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 75 → 76.
- Modify: `tests/test_page_version_guard.py` — append a new `PAGE_PINS` entry.
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: existing `.k-live`/`.k-ghost`/`.k-crashed`/`.k-parked`/`.k-attached` CSS classes (`page.html` ~51-57) for the compact row's colored dots — these already render a dot via `::before` for any element with the class inside `#key`, with no dependency on the `.kterm` class also being present.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web.py`, right after `test_page_key_help_works_on_touch_not_just_hover`:

```python
def test_key_legend_has_a_compact_row_that_expands():
    # User feedback 2026-09-19: the 4-row legend was always fully visible,
    # ~90px of permanent vertical space before a single session card
    # renders. Now it starts as one compact row (5 colored state dots +
    # 3 bare group-name pills for context/remote control/sid) and expands
    # to the exact same full legend on click. Always starts collapsed —
    # no persistence (explicit user choice over remembering the state).
    page = web.render_page()
    assert 'id="key-compact"' in page
    assert 'id="key-full" hidden' in page
    compact = page[page.index('id="key-compact"'):page.index('id="key-full"')]
    # State dots reuse the existing colour classes, WITHOUT kterm — the
    # compact row has exactly one behaviour (click anywhere to expand), not
    # a second per-dot tap-to-explain competing with it.
    for cls in ("k-live", "k-ghost", "k-crashed", "k-parked", "k-attached"):
        assert f'class="{cls}"' in compact, f"{cls} dot missing from the compact row"
        assert f'{cls} kterm' not in compact, f"{cls} must not carry kterm in the compact row"
    # The other three groups collapse to bare labels, not their term lists.
    for label in (">context<", ">remote control<", ">sid<"):
        assert label in compact
    assert "phone: not connected" not in compact  # a term, not a group label

    full = page[page.index('id="key-full"'):]
    assert 'id="key-less"' in full
    # The exact same full legend content still lives here, unchanged.
    assert "kgroup" in full and "klabel" in full and "kterm" in full
    assert "A restored session you have already reopened" in full


def test_key_legend_toggle_wiring():
    page = web.render_page()
    assert 'getElementById("key-compact").addEventListener("click"' in page
    assert 'getElementById("key-less").addEventListener("click"' in page
    # Toggling flips both elements' hidden state in opposite directions.
    assert 'document.getElementById("key-full").hidden = false' in page
    assert 'document.getElementById("key-compact").hidden = false' in page
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_key_legend_has_a_compact_row_that_expands tests/test_web.py::test_key_legend_toggle_wiring -v`
Expected: FAIL — none of this exists yet.

- [ ] **Step 3: Add the CSS**

In `crr/core/page.html`, immediately after the existing block ending `.k-will-compact::before { background: #e05252; }` (currently ~line 59), insert:

```css
  /* Compact key legend (user feedback 2026-09-19): starts collapsed to one
     row, expands to the full legend below on click. Always starts
     collapsed — no persistence across reloads (explicit choice). */
  #key-compact {
    display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
    background: none; border: none; padding: 0; margin: 0; width: 100%;
    font: inherit; font-size: 12px; color: #8a93a2; cursor: pointer; text-align: left;
  }
  #key-compact:hover { color: #cdd3dd; }
  #key-full { display: flex; gap: 18px; flex-wrap: wrap; }
  #key-less {
    display: block; width: 100%; margin: 0 0 6px; padding: 0;
    background: none; border: none; font: inherit; font-size: 11px;
    color: #6b7280; cursor: pointer; text-align: left;
  }
  #key-less:hover { color: #cdd3dd; }
```

- [ ] **Step 4: Replace `#key`'s content**

In `crr/core/page.html`, `#key` currently reads:

```html
<div id="key">
  <span class="kgroup"><span class="klabel">state</span><span class="k-live kterm" data-help="The shell and its claude are running, with a terminal attached.">live</span><span class="k-ghost kterm" data-help="The shell is alive but has no terminal — its window was closed. Restore re-homes the conversation.">ghost</span><span class="k-crashed kterm" data-help="The process is gone, or the host rebooted. The conversation is preserved and revivable with Reopen.">crashed</span><span class="k-parked kterm" data-help="Running inside a detached tmux session — crr restored it after a reboot. The conversation is alive; Reopen brings it back into a terminal tab, or attach with: tmux attach -t &lt;name&gt;.">restored</span><span class="k-attached kterm" data-help="A restored session you have already reopened — a terminal (tmux client) is attached to it. Lets you tell, in a long restore list, which conversations are already back from those still parked.">attached</span></span>
  <span class="kgroup"><span class="klabel">context</span><span class="k-tight kterm" data-help="Estimated past 70% of this model's context window. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.">tight</span><span class="k-will-compact kterm" data-help="Estimated at or over the window, so resuming will compact and lose detail. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.">will compact on revive</span><span class="kterm" data-help="crr has no confirmed context window for this session's model — usually because no model could be read from the transcript at all (about 1 in 3 carry none). No badge would read as 'fine'; this says crr genuinely does not know.">context unknown</span></span>
  <span class="kgroup"><span class="klabel">remote control</span><span class="kterm" data-help="Claude Code itself reports no live Remote Control link for this session — the mobile app shows it as Disconnected while claude keeps working locally. crr reads Claude Code's own connection state, not a guess from transcript activity. Kick restarts claude so it reconnects, or turn on auto-kick in Settings to have crr do that for you.">phone: not connected</span><span class="kterm" data-help="crr could not establish whether the phone can reach this session: Claude Code's own connection state for it was missing, unreadable, or belonged to a process crr could not match. It is not a claim that the link is down — and crr never auto-kicks a session in this state. A session the phone can reach shows no badge at all.">phone: unknown</span><span class="kterm" data-help="Claude Code reports this session as blocked on YOU — a permission prompt, or a question it needs answered. It will sit there until you answer, at the keyboard or on the phone. If it also says the phone is not connected, answering on the phone is not an option until claude is restarted.">waiting on you</span></span>
  <span class="kgroup"><span class="klabel">sid</span><span class="kterm" data-help="How sure crr is that it identified the right conversation. injected = certain: crr generated the session id and passed it to claude.">injected</span><span class="kterm" data-help="Uncertain: the id was inferred from the newest transcript in that directory, so it may be the wrong conversation.">guessed</span><span class="kterm" data-help="Confirmed: a guessed id whose transcript has been written to since the session started.">verified</span></span>
</div>
```

Change it to (the four `.kgroup` lines inside `#key-full` are copied **verbatim, byte-for-byte** from above — do not retype them, copy-paste to avoid transcription errors in the long `data-help` strings):

```html
<div id="key">
  <button id="key-compact" type="button">
    <span class="k-live">live</span><span class="k-ghost">ghost</span><span class="k-crashed">crashed</span><span class="k-parked">restored</span><span class="k-attached">attached</span><span class="klabel">context</span><span class="klabel">remote control</span><span class="klabel">sid</span>
  </button>
  <div id="key-full" hidden>
    <button id="key-less" type="button">▴ less</button>
    <span class="kgroup"><span class="klabel">state</span><span class="k-live kterm" data-help="The shell and its claude are running, with a terminal attached.">live</span><span class="k-ghost kterm" data-help="The shell is alive but has no terminal — its window was closed. Restore re-homes the conversation.">ghost</span><span class="k-crashed kterm" data-help="The process is gone, or the host rebooted. The conversation is preserved and revivable with Reopen.">crashed</span><span class="k-parked kterm" data-help="Running inside a detached tmux session — crr restored it after a reboot. The conversation is alive; Reopen brings it back into a terminal tab, or attach with: tmux attach -t &lt;name&gt;.">restored</span><span class="k-attached kterm" data-help="A restored session you have already reopened — a terminal (tmux client) is attached to it. Lets you tell, in a long restore list, which conversations are already back from those still parked.">attached</span></span>
    <span class="kgroup"><span class="klabel">context</span><span class="k-tight kterm" data-help="Estimated past 70% of this model's context window. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.">tight</span><span class="k-will-compact kterm" data-help="Estimated at or over the window, so resuming will compact and lose detail. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.">will compact on revive</span><span class="kterm" data-help="crr has no confirmed context window for this session's model — usually because no model could be read from the transcript at all (about 1 in 3 carry none). No badge would read as 'fine'; this says crr genuinely does not know.">context unknown</span></span>
    <span class="kgroup"><span class="klabel">remote control</span><span class="kterm" data-help="Claude Code itself reports no live Remote Control link for this session — the mobile app shows it as Disconnected while claude keeps working locally. crr reads Claude Code's own connection state, not a guess from transcript activity. Kick restarts claude so it reconnects, or turn on auto-kick in Settings to have crr do that for you.">phone: not connected</span><span class="kterm" data-help="crr could not establish whether the phone can reach this session: Claude Code's own connection state for it was missing, unreadable, or belonged to a process crr could not match. It is not a claim that the link is down — and crr never auto-kicks a session in this state. A session the phone can reach shows no badge at all.">phone: unknown</span><span class="kterm" data-help="Claude Code reports this session as blocked on YOU — a permission prompt, or a question it needs answered. It will sit there until you answer, at the keyboard or on the phone. If it also says the phone is not connected, answering on the phone is not an option until claude is restarted.">waiting on you</span></span>
    <span class="kgroup"><span class="klabel">sid</span><span class="kterm" data-help="How sure crr is that it identified the right conversation. injected = certain: crr generated the session id and passed it to claude.">injected</span><span class="kterm" data-help="Uncertain: the id was inferred from the newest transcript in that directory, so it may be the wrong conversation.">guessed</span><span class="kterm" data-help="Confirmed: a guessed id whose transcript has been written to since the session started.">verified</span></span>
  </div>
</div>
```

- [ ] **Step 5: Add the toggle wiring**

In `crr/core/page.html`, the existing `#key .kterm` tap-to-toast block currently ends:

```js
Array.prototype.forEach.call(document.querySelectorAll("#key .kterm"), function (t) {
  var help = t.getAttribute("data-help") || "";
  t.title = help;
  t.addEventListener("click", function () { showNotice(t.textContent + ": " + help, "warn", { sticky: true }); });
});
```

Immediately after that block, insert:

```js
// Compact key legend (user feedback 2026-09-19): starts collapsed; click
// either the compact row or "less" to swap which one is hidden. Always
// starts collapsed on load — no persistence, by explicit user choice.
document.getElementById("key-compact").addEventListener("click", function () {
  document.getElementById("key-compact").hidden = true;
  document.getElementById("key-full").hidden = false;
});
document.getElementById("key-less").addEventListener("click", function () {
  document.getElementById("key-full").hidden = true;
  document.getElementById("key-compact").hidden = false;
});
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py::test_key_legend_has_a_compact_row_that_expands tests/test_web.py::test_key_legend_toggle_wiring -v`
Expected: PASS.

- [ ] **Step 7: Run the existing `#key`-related tests to confirm no regression**

Run: `.venv/bin/pytest tests/test_web.py -k "key" -v`
Expected: all PASS — `test_page_key_is_grouped_with_tap_and_hover_help`, `test_page_key_help_works_on_touch_not_just_hover`, and the `k-parked`/`k-attached` colour-dot tests (`page.html`'s `.k-parked::before`/`.k-attached::before` CSS rules are untouched) all check substring presence anywhere in the page, not position or exclusivity, so the restructure doesn't break them.

- [ ] **Step 8: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`, after Task 1 the line reads:

```python
PAGE_VERSION = 75  # v75: Add a device + Tailnet Members moved into a new Settings "Devices" section (were buried in the "Other views" toolbar, user feedback 2026-09-19); v74: Settings moved to a header gear icon (was buried in the "Other views" toolbar); excluded-directories rows wrap instead of overflowing on narrow screens (user feedback 2026-09-18); v73: explanation toasts (badge long-press, #key legend tap) are sticky, not auto-dismissed — 3s wasn't enough to read a full sentence; v72: session-card status badges long-press to reveal an explanation, via the same showNotice toast the #key legend already uses on tap; v71: auth badge no longer treats "unknown" as healthy — visible muted badge + Reauth button, and the expired badge names its source
```

Change it to:

```python
PAGE_VERSION = 76  # v76: #key legend starts as a compact row (5 state dots + 3 group pills), expands to the full legend on click (user feedback 2026-09-19); v75: Add a device + Tailnet Members moved into a new Settings "Devices" section (were buried in the "Other views" toolbar); v74: Settings moved to a header gear icon (was buried in the "Other views" toolbar); excluded-directories rows wrap instead of overflowing on narrow screens (user feedback 2026-09-18); v73: explanation toasts (badge long-press, #key legend tap) are sticky, not auto-dismissed — 3s wasn't enough to read a full sentence; v72: session-card status badges long-press to reveal an explanation, via the same showNotice toast the #key legend already uses on tap; v71: auth badge no longer treats "unknown" as healthy — visible muted badge + Reauth button, and the expired badge names its source
```

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v` (fails, prints the sha), add `76: "<sha>",` at the top of `PAGE_PINS`, re-run to confirm PASS.

- [ ] **Step 9: Update the version-history docstring test**

Task 1 renamed the docstring-chain test to `test_page_version_is_75` and prepended a `v75: ...` paragraph. Repeat the exact same pattern:

1. Rename the function from `test_page_version_is_75` to `test_page_version_is_76`.
2. Prepend this new paragraph immediately after the opening `"""`, before the `v75: Add a device...` text you added in Task 1 (so that becomes a parenthesized entry too):

```
v76: The #key legend starts as one compact row — 5 colored state dots
(live/ghost/crashed/restored/attached, reusing the existing .k-live etc.
colour classes) plus 3 bare group-name pills (context/remote control/sid)
— and expands to the exact same, unchanged full legend on click (user
feedback 2026-09-19: the always-visible 4-row legend took ~90px before a
single session card renders). Always starts collapsed on load; no
persistence, by explicit choice over remembering the expanded state.
(v75: Add a device + Tailnet Members moved from the "Other views" toolbar
```
3. Change `assert web.PAGE_VERSION == 75` to `assert web.PAGE_VERSION == 76`.

Everything else in the docstring is copied verbatim, unchanged.

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): compact, expandable key legend (v76)

User feedback 2026-09-19: the #key legend was a permanently-visible 4-row
wall of text (~90px) before a single session card renders. Now starts as
one compact row (5 colored state dots + 3 bare group-name pills) and
expands to the exact same, unchanged full legend on click — same terms,
same per-term tap-to-toast. Always starts collapsed on load; no
persistence, by explicit user choice over remembering the expanded state.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

- [ ] **Step 12: Push, open a PR, and report back**

```bash
git push -u origin feat/header-declutter
gh auth switch --hostname github.com --user InfiniteInsight
gh pr create --base main --head feat/header-declutter \
  --title "feat(dashboard): move devices into Settings, compact key legend" \
  --body "Two pieces of header-declutter feedback: Add a device + Tailnet Members moved into a new Settings \"Devices\" section; the #key legend now starts as a compact one-line summary and expands to the full legend on click. Page v74→v76. Full suite via the pre-commit hook.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR"
```

Report the PR URL back to the user and ask whether to merge + `crr deploy` it, matching the last three dashboard PRs' pattern.
