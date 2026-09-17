# Long-press badge explanations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Long-pressing any status badge on a dashboard session card shows its explanation as a toast, without disturbing scrolling or normal taps.

**Architecture:** One delegated long-press detector (touch + mouse) attached once to the `#sessions` container reads a `data-help` attribute off whichever badge was pressed and calls the existing `showNotice()` toast — the same mechanism the `#key` legend already uses on tap. Every badge `renderCard` can create gets a `data-help` string wired at creation time; badges that already carry a `title=` reuse that exact string rather than duplicating it.

**Tech Stack:** Vanilla ES5 JavaScript embedded in `crr/core/page.html` (no build step, no runtime dependencies — stdlib-only per project constraints). Tests are Python string/substring assertions against the served page source (`crr/core/web.py`'s `load_page()`), matching this codebase's existing convention for testing `page.html` — there is no browser/JS test runner in this repo.

**Design doc:** `docs/superpowers/specs/2026-09-17-badge-longpress-help-design.md`

## Global Constraints

- **ES5-only JS in `page.html`**: no `const`/`let`/arrow functions — the whole file uses `var`/`function` throughout. Match it exactly.
- **Zero runtime dependencies**: stdlib-only web server, no new libraries, no build step.
- **`#key` legend gesture is unchanged**: it keeps single-tap-to-toast. Only the per-card badges (in `renderCard`) get long-press.
- **Every badge `renderCard` creates gets `data-help`**: state/parked/attached, worktree, duplicate (both variants), context-pressure (tight/will-compact/unknown), strike, remote-control (both variants), waiting, adopted, latest.
- **Reuse existing `title=` text verbatim** for `data-help` on badges that already have one (remote-control x2, waiting, adopted) — never a second, differently-worded copy of the same sentence.
- **`PAGE_VERSION` bump discipline**: every commit that changes `crr/core/page.html` MUST bump `PAGE_VERSION` in `crr/core/web.py` by 1 and append (never edit) a new entry to `PAGE_PINS` in `tests/test_page_version_guard.py`, computed from the actual post-edit file hash (`test_page_version_guard.py` prints the exact sha to use when it fails). **Starting point for this plan (this worktree, branched off `main`): `PAGE_VERSION` is currently 70.** This plan's tasks take it to 71, 72, 73, 74, then 75.
- **TDD**: write the failing test against `page.html`'s source text first, watch it fail, then edit `page.html` to make it pass.
- **Use the worktree-local venv for every test/lint command: `.venv/bin/pytest`, never the bare `pytest` on PATH.** This repo is edited from a git worktree; a bare `pytest` invocation does not add the current directory to `sys.path`, so it silently resolves the globally editable-installed `crr` package from a *different* checkout instead of this one's own source — passing or failing against the wrong code with no error. `.venv/bin/pytest` is a project-local virtualenv with `crr` installed editable from *this* worktree, and it's also exactly what the pre-commit hook runs (`.git/hooks/pre-commit` → `.venv/bin/pytest -q`), so using it directly matches what the commit gate checks.
- **Frequent, small commits**: one commit per task, each ending with a full `.venv/bin/pytest` run (the repo's pre-commit hook runs the whole suite — ~2500 tests, ~3 minutes; budget for that, don't work around it).

---

### Task 1: CSS touch-callout guard + delegated long-press detector

**Files:**
- Modify: `crr/core/page.html` — the `.badge {` CSS rule (currently lines 92-95); insert new JS constants/functions after the existing `SID_SOURCE_HELP` block (currently ends line 589); insert event-listener wiring right after the existing `#key` tap-to-toast wiring block (currently lines 2424-2431, ends `});`).
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION`.
- Modify: `tests/test_page_version_guard.py` — append a new `PAGE_PINS` entry.
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: existing `showNotice(text, kind, opts)` function (`page.html:1096`); existing `#sessions` container (`page.html:536`).
- Produces (for Tasks 2-5 to rely on): `lpBegin`, `lpMove`, `lpCancel` are internal to this detector — later tasks never call them directly. What later tasks rely on is the **contract**: any element matching `.badge[data-help]` inside `#sessions` will show `<textContent>: <data-help value>` as a toast on a ~500ms press. Later tasks only need to add `data-help` to their badges; the detector already exists after this task.

- [ ] **Step 1: Write the failing test for the detector**

```python
def test_page_has_longpress_badge_detector():
    # Long-press (not tap) on a session-card badge reveals its explanation,
    # via the same showNotice toast the #key legend already uses for tap.
    # Delegated on #sessions (not per-badge) because cards are rebuilt on
    # every poll — see
    # docs/superpowers/specs/2026-09-17-badge-longpress-help-design.md.
    page = web.load_page()
    assert "var LONGPRESS_MS" in page
    assert 'closest(".badge[data-help]")' in page
    assert 'showNotice(lpBadge.textContent + ": " + help, "warn")' in page
    assert 'getElementById("sessions")' in page
    assert 'addEventListener("touchstart"' in page
    assert 'addEventListener("mousedown"' in page
```

Add this to `tests/test_web.py` near the other page-source structural tests (e.g. right after `test_page_renders_the_not_connected_badge`).

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_page_has_longpress_badge_detector -v`
Expected: FAIL — none of those strings exist in `page.html` yet.

- [ ] **Step 3: Write the failing test for the CSS guard**

```python
def test_badges_suppress_native_touch_callout():
    # A long-press on text is exactly the gesture mobile browsers use for
    # their own selection/copy bubble — it must not fight with our toast.
    page = web.load_page()
    assert "-webkit-touch-callout: none" in page
```

- [ ] **Step 4: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_badges_suppress_native_touch_callout -v`
Expected: FAIL.

- [ ] **Step 5: Add the CSS guard**

In `crr/core/page.html`, the `.badge {` rule currently reads:

```css
  .badge {
    margin-left: auto; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
    padding: 2px 8px; border-radius: 999px; font-weight: 600;
  }
```

Change it to:

```css
  .badge {
    margin-left: auto; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
    padding: 2px 8px; border-radius: 999px; font-weight: 600;
    /* Long-press reveals this badge's explanation (see the detector further
       down the page). Without this, a long-press on text triggers the
       browser's own selection/copy bubble instead, which fights with our
       toast. */
    -webkit-touch-callout: none; user-select: none;
  }
```

- [ ] **Step 6: Add the long-press detector functions**

In `crr/core/page.html`, immediately after the existing `SID_SOURCE_HELP` block:

```js
var SID_SOURCE_HELP = {
  injected: "certain — crr generated this session id and passed it to claude",
  guessed: "uncertain — inferred from the newest transcript in this directory; may be the wrong conversation",
  verified: "confirmed — the guessed id's transcript has been written to since this session started"
};
```

insert:

```js
// Long-press (not tap) on a session-card badge shows its explanation as a
// toast — the same showNotice() mechanism the #key legend already uses on
// tap. Long-press, not tap, because a plain tap in a scrollable card list
// is at least as likely to be a mis-tap/scroll-release as deliberate; see
// docs/superpowers/specs/2026-09-17-badge-longpress-help-design.md.
//
// Delegated on #sessions rather than per-badge: cards (and their badges)
// are torn down and rebuilt on every poll, so per-element listeners would
// mean rewiring on every poll tick for no benefit.
var LONGPRESS_MS = 500;
var LONGPRESS_MOVE_PX = 10;
var lpTimer = null, lpStart = null, lpBadge = null;

function lpCancel() {
  clearTimeout(lpTimer);
  lpTimer = null; lpStart = null; lpBadge = null;
}

function lpBegin(target, x, y) {
  var badge = target.closest && target.closest(".badge[data-help]");
  if (!badge) return;
  lpBadge = badge;
  lpStart = { x: x, y: y };
  lpTimer = setTimeout(function () {
    var help = lpBadge.getAttribute("data-help") || "";
    showNotice(lpBadge.textContent + ": " + help, "warn");
    lpBadge = null;
  }, LONGPRESS_MS);
}

function lpMove(x, y) {
  if (!lpStart) return;
  if (Math.abs(x - lpStart.x) > LONGPRESS_MOVE_PX ||
      Math.abs(y - lpStart.y) > LONGPRESS_MOVE_PX) lpCancel();
}
```

- [ ] **Step 7: Wire the detector to `#sessions`**

In `crr/core/page.html`, the existing `#key` tap-to-toast wiring currently reads:

```js
// The key explains itself on hover (title) AND on tap (toast), because
// title tooltips never fire on a touch screen — and this dashboard is used
// from a phone at least as often as a desktop.
Array.prototype.forEach.call(document.querySelectorAll("#key .kterm"), function (t) {
  var help = t.getAttribute("data-help") || "";
  t.title = help;
  t.addEventListener("click", function () { showNotice(t.textContent + ": " + help, "warn"); });
});
```

Immediately after that block (still before `document.getElementById("filter")...`), insert:

```js
// Wiring for the long-press detector above: delegated on #sessions so it
// survives every poll's card rebuild. The #key legend above keeps its own
// tap gesture, untouched — this is deliberately separate.
var sessionsEl = document.getElementById("sessions");
sessionsEl.addEventListener("touchstart", function (e) {
  var t = e.touches[0];
  lpBegin(e.target, t.clientX, t.clientY);
}, { passive: true });
sessionsEl.addEventListener("touchmove", function (e) {
  var t = e.touches[0];
  lpMove(t.clientX, t.clientY);
}, { passive: true });
sessionsEl.addEventListener("touchend", lpCancel);
sessionsEl.addEventListener("touchcancel", lpCancel);
// Desktop click-and-hold, for parity — hover title already covers desktop
// (each badge keeps its own title= where it has one), so this is a
// nice-to-have there, not the primary path.
sessionsEl.addEventListener("mousedown", function (e) { lpBegin(e.target, e.clientX, e.clientY); });
sessionsEl.addEventListener("mousemove", function (e) { lpMove(e.clientX, e.clientY); });
sessionsEl.addEventListener("mouseup", lpCancel);
sessionsEl.addEventListener("mouseleave", lpCancel);
```

- [ ] **Step 8: Run both new tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py::test_page_has_longpress_badge_detector tests/test_web.py::test_badges_suppress_native_touch_callout -v`
Expected: PASS.

- [ ] **Step 9: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`, change:

```python
PAGE_VERSION = 70  # v70: Tunnel picker hides the config plumbing — effective provider shown; using-default tag + Reset link
```

to:

```python
PAGE_VERSION = 71  # v71: badges long-press to reveal an explanation (1/5 — CSS guard + delegated detector)
```

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v`
Expected: FAIL, with a message naming the exact sha256 to pin, e.g. `71: "<sha>",`.

Copy that exact line into `tests/test_page_version_guard.py`, added at the **top** of the `PAGE_PINS` dict (existing entries are never edited):

```python
PAGE_PINS: dict[int, str] = {
    71: "<sha from the failing test output>",
    70: "e1bcf105a0e864fc9de6f31c486c6d08ce0dcaca12706b7c33afc178cc612e83",
    ...
```

Run: `.venv/bin/pytest tests/test_page_version_guard.py -v`
Expected: PASS.

- [ ] **Step 10: Run the full suite and commit**

Run: `.venv/bin/pytest`
Expected: all pass (matches the pre-commit hook, so the commit itself won't need a second try).

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): long-press detector for badge explanations (v71)

One delegated touch/mouse long-press detector on #sessions, reading any
badge's data-help attribute and showing it via the existing showNotice
toast. No badge sets data-help yet (next tasks wire each badge family) —
this lands the mechanism and its CSS guard against the native
selection/copy bubble a long-press on text would otherwise trigger.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

---

### Task 2: State badge (`live`/`ghost`/`crashed`/`parked`/`attached`)

**Files:**
- Modify: `crr/core/page.html` — add a `STATE_HELP` map near `SID_SOURCE_HELP`; wire it onto the state badge in `renderCard` (currently the `parkedAttached`/`badge` block).
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 71 → 72.
- Modify: `tests/test_page_version_guard.py` — append the new pin.
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: the long-press detector from Task 1 (nothing to call directly — just needs `data-help` set).
- Produces: `STATE_HELP` object keyed by `"live" | "ghost" | "crashed" | "parked" | "attached"`.

- [ ] **Step 1: Write the failing test**

```python
def test_state_badge_has_longpress_help():
    # Reuses the #key legend's own wording for state, so the badge and the
    # legend never say two different things about the same state.
    page = web.load_page()
    assert "var STATE_HELP = {" in page
    assert 'badge.setAttribute("data-help", STATE_HELP[parkedAttached ? "attached" : s.state] || "")' in page
    assert "A restored session you have already reopened" in page
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_state_badge_has_longpress_help -v`
Expected: FAIL.

- [ ] **Step 3: Add `STATE_HELP`**

In `crr/core/page.html`, add near `SID_SOURCE_HELP` (either just before or after it):

```js
var STATE_HELP = {
  live: "The shell and its claude are running, with a terminal attached.",
  ghost: "The shell is alive but has no terminal — its window was closed. Restore re-homes the conversation.",
  crashed: "The process is gone, or the host rebooted. The conversation is preserved and revivable with Reopen.",
  parked: "Running inside a detached tmux session — crr restored it after a reboot. The conversation is alive; Reopen brings it back into a terminal tab, or attach with: tmux attach -t <name>.",
  attached: "A restored session you have already reopened — a terminal (tmux client) is attached to it. Lets you tell, in a long restore list, which conversations are already back from those still parked."
};
```

- [ ] **Step 4: Wire it onto the state badge**

In `renderCard`, the current code reads:

```js
  var parkedAttached = s.state === "parked" && s.attached;
  var badge = el("span", "badge " + (parkedAttached ? "attached" : s.state));
  badge.textContent = s.state === "parked"
    ? (s.attached ? "attached" : "restored")
    : s.state;
  top.appendChild(badge);
```

Change it to:

```js
  var parkedAttached = s.state === "parked" && s.attached;
  var badge = el("span", "badge " + (parkedAttached ? "attached" : s.state));
  badge.textContent = s.state === "parked"
    ? (s.attached ? "attached" : "restored")
    : s.state;
  badge.setAttribute("data-help", STATE_HELP[parkedAttached ? "attached" : s.state] || "");
  top.appendChild(badge);
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_web.py::test_state_badge_has_longpress_help -v`
Expected: PASS.

- [ ] **Step 6: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`: `PAGE_VERSION = 72  # v72: badges long-press to reveal an explanation (2/5 — state badge)`

Run `.venv/bin/pytest tests/test_page_version_guard.py -v` (fails, prints the sha), add `72: "<sha>",` at the top of `PAGE_PINS`, re-run to confirm PASS.

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/bin/pytest`
Expected: all pass.

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): long-press help for the state badge (v72)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

---

### Task 3: Context-pressure badges (`tight` / `will-compact` / `unknown`)

**Files:**
- Modify: `crr/core/page.html` — add a `CONTEXT_PRESSURE_HELP` map near `STATE_HELP`; wire it onto the three context-pressure badge branches in `renderCard`.
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 72 → 73.
- Modify: `tests/test_page_version_guard.py` — append the new pin.
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `CONTEXT_PRESSURE_HELP` object keyed by `"tight" | "will-compact" | "unknown"`.

- [ ] **Step 1: Write the failing test**

```python
def test_context_pressure_badges_have_longpress_help():
    page = web.load_page()
    assert "var CONTEXT_PRESSURE_HELP = {" in page
    assert 'pb.setAttribute("data-help", CONTEXT_PRESSURE_HELP.tight)' in page
    assert 'pb2.setAttribute("data-help", CONTEXT_PRESSURE_HELP["will-compact"])' in page
    assert 'pb3.setAttribute("data-help", CONTEXT_PRESSURE_HELP.unknown)' in page
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_context_pressure_badges_have_longpress_help -v`
Expected: FAIL.

- [ ] **Step 3: Add `CONTEXT_PRESSURE_HELP`**

In `crr/core/page.html`, add near `STATE_HELP`:

```js
var CONTEXT_PRESSURE_HELP = {
  tight: "Estimated past 70% of this model's context window. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.",
  "will-compact": "Estimated at or over the window, so resuming will compact and lose detail. An estimate: transcript bytes ÷ 4 against a known window size, not a real token count.",
  unknown: "crr has no confirmed context window for this session's model — usually because no model could be read from the transcript at all (about 1 in 3 carry none). No badge would read as 'fine'; this says crr genuinely does not know."
};
```

- [ ] **Step 4: Wire it onto the three badges**

In `renderCard`, the current code reads:

```js
  if (s.context_pressure === "tight") {
    var pb = el("span", "badge pressure-tight");
    pb.textContent = "context tight";
    top.appendChild(pb);
  } else if (s.context_pressure === "will-compact") {
    var pb2 = el("span", "badge pressure-will-compact");
    pb2.textContent = "will compact on revive";
    top.appendChild(pb2);
  } else if (s.context_pressure === "unknown") {
    // #39: no confirmed context window for this session's model (often
    // because no model could be extracted at all — measured at ~1 in 3
    // transcripts). Rendering nothing here would be read as "ok", which is
    // the fabricated reassurance this state exists to stop.
    var pb3 = el("span", "badge unknown-badge");
    pb3.textContent = "context unknown";
    top.appendChild(pb3);
  }
```

Change it to:

```js
  if (s.context_pressure === "tight") {
    var pb = el("span", "badge pressure-tight");
    pb.textContent = "context tight";
    pb.setAttribute("data-help", CONTEXT_PRESSURE_HELP.tight);
    top.appendChild(pb);
  } else if (s.context_pressure === "will-compact") {
    var pb2 = el("span", "badge pressure-will-compact");
    pb2.textContent = "will compact on revive";
    pb2.setAttribute("data-help", CONTEXT_PRESSURE_HELP["will-compact"]);
    top.appendChild(pb2);
  } else if (s.context_pressure === "unknown") {
    // #39: no confirmed context window for this session's model (often
    // because no model could be extracted at all — measured at ~1 in 3
    // transcripts). Rendering nothing here would be read as "ok", which is
    // the fabricated reassurance this state exists to stop.
    var pb3 = el("span", "badge unknown-badge");
    pb3.textContent = "context unknown";
    pb3.setAttribute("data-help", CONTEXT_PRESSURE_HELP.unknown);
    top.appendChild(pb3);
  }
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_web.py::test_context_pressure_badges_have_longpress_help -v`
Expected: PASS.

- [ ] **Step 6: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`: `PAGE_VERSION = 73  # v73: badges long-press to reveal an explanation (3/5 — context-pressure badges)`

Run `.venv/bin/pytest tests/test_page_version_guard.py -v` (fails, prints the sha), add `73: "<sha>",` at the top of `PAGE_PINS`, re-run to confirm PASS.

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/bin/pytest`
Expected: all pass.

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): long-press help for context-pressure badges (v73)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

---

### Task 4: Worktree, duplicate, strike, and latest badges (new copy)

**Files:**
- Modify: `crr/core/page.html` — `renderCard`'s worktree, duplicate, strike, and latest badge blocks.
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 73 → 74.
- Modify: `tests/test_page_version_guard.py` — append the new pin.
- Test: `tests/test_web.py`

**Interfaces:**
- None of these badges have an existing `title=` or `#key` entry — this is new copy, written inline (no shared map needed, since none of the four is a small fixed enum without dynamic content the way state/context-pressure are; duplicate and strike both interpolate per-card values).

- [ ] **Step 1: Write the failing test**

```python
def test_worktree_dup_strike_latest_badges_have_longpress_help():
    page = web.load_page()
    assert 'wtb.setAttribute("data-help"' in page
    assert 'dup.setAttribute("data-help"' in page
    assert 'stb.setAttribute("data-help"' in page
    assert 'lb.setAttribute("data-help"' in page
    # Duplicate's two variants (certain vs guessed-id match) get different
    # wording, not the same sentence copy-pasted onto both branches.
    assert "crr is certain of the match" in page
    assert "wasn't certain" in page or "isn't certain" in page
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web.py::test_worktree_dup_strike_latest_badges_have_longpress_help -v`
Expected: FAIL.

- [ ] **Step 3: Wire the worktree badge**

Current:

```js
  var wtInfo = worktreeInfo(s.cwd);
  if (wtInfo) {
    var wtb = el("span", "badge worktree-badge");
    wtb.textContent = "worktree: " + wtInfo.name;
    top.appendChild(wtb);
  }
```

Change to:

```js
  var wtInfo = worktreeInfo(s.cwd);
  if (wtInfo) {
    var wtb = el("span", "badge worktree-badge");
    wtb.textContent = "worktree: " + wtInfo.name;
    wtb.setAttribute("data-help", "A checkout of a git worktree, not the main branch — grouped under its parent repo below when there's more than one.");
    top.appendChild(wtb);
  }
```

- [ ] **Step 4: Wire the duplicate badge**

Current:

```js
  if (isDup) {
    var dup = el("span", "badge dup-badge" + (isUncertain ? " uncertain" : ""));
    dup.textContent = isUncertain ? "possible duplicate · guessed sid" : "duplicate";
    top.appendChild(dup);
  }
```

Change to:

```js
  if (isDup) {
    var dup = el("span", "badge dup-badge" + (isUncertain ? " uncertain" : ""));
    dup.textContent = isUncertain ? "possible duplicate · guessed sid" : "duplicate";
    dup.setAttribute("data-help", isUncertain
      ? "Another session may share this id, but it was guessed rather than injected, so the match isn't certain."
      : "Another tracked session shares this exact session id — crr is certain of the match.");
    top.appendChild(dup);
  }
```

- [ ] **Step 5: Wire the strike badge**

Current:

```js
  if (s.revive_strikes > 0) {
    var stb = el("span", "badge strike-badge");
    stb.textContent = "⚠ strike " + s.revive_strikes + "/" + STRIKE_MAX +
      " — process died upon revival";
    top.appendChild(stb);
  }
```

Change to:

```js
  if (s.revive_strikes > 0) {
    var stb = el("span", "badge strike-badge");
    stb.textContent = "⚠ strike " + s.revive_strikes + "/" + STRIKE_MAX +
      " — process died upon revival";
    stb.setAttribute("data-help", "This session died again shortly after crr revived it — strike " +
      s.revive_strikes + " of " + STRIKE_MAX + ". After " + STRIKE_MAX +
      " strikes crr stops trying to revive it automatically.");
    top.appendChild(stb);
  }
```

- [ ] **Step 6: Wire the latest badge**

Current:

```js
  var cwdInfo = latest && latest[s.cwd];
  if (cwdInfo && cwdInfo.count > 1 && recencyMs(s) === cwdInfo.max) {
    var lb = el("span", "badge latest-badge");
    lb.textContent = "latest";
    top.appendChild(lb);
  }
```

Change to:

```js
  var cwdInfo = latest && latest[s.cwd];
  if (cwdInfo && cwdInfo.count > 1 && recencyMs(s) === cwdInfo.max) {
    var lb = el("span", "badge latest-badge");
    lb.textContent = "latest";
    lb.setAttribute("data-help", "The most recently active session in this directory — shown when more than one session shares the same working directory.");
    top.appendChild(lb);
  }
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_web.py::test_worktree_dup_strike_latest_badges_have_longpress_help -v`
Expected: PASS.

- [ ] **Step 8: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`: `PAGE_VERSION = 74  # v74: badges long-press to reveal an explanation (4/5 — worktree/duplicate/strike/latest badges)`

Run `.venv/bin/pytest tests/test_page_version_guard.py -v` (fails, prints the sha), add `74: "<sha>",` at the top of `PAGE_PINS`, re-run to confirm PASS.

- [ ] **Step 9: Run the full suite and commit**

Run: `.venv/bin/pytest`
Expected: all pass.

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): long-press help for worktree/duplicate/strike/latest badges (v74)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```

---

### Task 5: Remote-control, waiting, and adopted badges (reuse existing title) + full-coverage regression test

**Files:**
- Modify: `crr/core/page.html` — `renderCard`'s remote-control (both branches), waiting, and adopted badge blocks.
- Modify: `crr/core/web.py:45` — bump `PAGE_VERSION` 74 → 75.
- Modify: `tests/test_page_version_guard.py` — append the new pin.
- Test: `tests/test_web.py`

**Interfaces:**
- None new — this closes out the badge list from the design spec and adds the regression test that pins the complete set.

- [ ] **Step 1: Write the failing tests**

```python
def test_remote_control_waiting_adopted_badges_have_longpress_help():
    # These three already carry a title= for desktop hover — data-help
    # reuses that exact string (rcb.title / rcu.title / wb.title / adb.title)
    # rather than a second, differently-worded copy.
    page = web.load_page()
    assert 'rcb.setAttribute("data-help", rcb.title)' in page
    assert 'rcu.setAttribute("data-help", rcu.title)' in page
    assert 'wb.setAttribute("data-help", wb.title)' in page
    assert 'adb.setAttribute("data-help", adb.title)' in page


def test_every_card_badge_has_longpress_help_wired():
    # Regression: every badge kind renderCard can create must be
    # long-press-able. A new badge kind added later without data-help would
    # pass every test above silently — this pins the full list from the
    # design spec (docs/superpowers/specs/2026-09-17-badge-longpress-help-design.md).
    page = web.load_page()
    badge_vars = ["badge", "wtb", "dup", "pb", "pb2", "pb3", "stb", "rcb", "rcu", "wb", "adb", "lb"]
    for var in badge_vars:
        assert f'{var}.setAttribute("data-help"' in page, f"{var} badge has no data-help wired"


def test_key_legend_tap_wiring_is_unchanged():
    # The #key legend keeps its existing single-tap-to-toast behavior —
    # only the per-card badges (above) gained long-press. Explicit decision
    # in the design spec; this is the regression check for it.
    page = web.load_page()
    assert 'querySelectorAll("#key .kterm")' in page
    assert 't.addEventListener("click", function () { showNotice(t.textContent + ": " + help, "warn"); });' in page
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py::test_remote_control_waiting_adopted_badges_have_longpress_help tests/test_web.py::test_every_card_badge_has_longpress_help_wired -v`
Expected: both FAIL (the third, `test_key_legend_tap_wiring_is_unchanged`, already PASSES today — run it too and confirm that; it's the regression guard, not new behavior).

- [ ] **Step 3: Wire the remote-control badges**

Current:

```js
  if (s.remote_control === "unreachable") {
    var rcb = el("span", "badge remote-control-badge");
    rcb.textContent = "phone: not connected";
    rcb.title = "Claude Code reports no live Remote Control link for this session. Kick restarts claude so it reconnects.";
    top.appendChild(rcb);
  } else if (s.remote_control === "unknown") {
    var rcu = el("span", "badge unknown-badge");
    rcu.textContent = "phone: unknown";
    rcu.title = "crr could not establish whether the phone can reach this session. It never auto-kicks a session in this state.";
    top.appendChild(rcu);
  }
```

Change to:

```js
  if (s.remote_control === "unreachable") {
    var rcb = el("span", "badge remote-control-badge");
    rcb.textContent = "phone: not connected";
    rcb.title = "Claude Code reports no live Remote Control link for this session. Kick restarts claude so it reconnects.";
    rcb.setAttribute("data-help", rcb.title);
    top.appendChild(rcb);
  } else if (s.remote_control === "unknown") {
    var rcu = el("span", "badge unknown-badge");
    rcu.textContent = "phone: unknown";
    rcu.title = "crr could not establish whether the phone can reach this session. It never auto-kicks a session in this state.";
    rcu.setAttribute("data-help", rcu.title);
    top.appendChild(rcu);
  }
```

- [ ] **Step 4: Wire the waiting badge**

Current:

```js
  if (s.waiting_for) {
    var wb = el("span", "badge waiting-badge");
    wb.textContent = "waiting on you";
    wb.title = "Claude Code reports this session as blocked on you: " + s.waiting_for;
    top.appendChild(wb);
  }
```

Change to:

```js
  if (s.waiting_for) {
    var wb = el("span", "badge waiting-badge");
    wb.textContent = "waiting on you";
    wb.title = "Claude Code reports this session as blocked on you: " + s.waiting_for;
    wb.setAttribute("data-help", wb.title);
    top.appendChild(wb);
  }
```

- [ ] **Step 5: Wire the adopted badge**

Current:

```js
  if (s.adopted) {
    var adb = el("span", "badge unknown-badge");
    adb.textContent = "adopted";
    adb.title = "crr found this conversation's transcript on disk and adopted it — it never watched the session start, so its host and shell were never observed.";
    top.appendChild(adb);
  }
```

Change to:

```js
  if (s.adopted) {
    var adb = el("span", "badge unknown-badge");
    adb.textContent = "adopted";
    adb.title = "crr found this conversation's transcript on disk and adopted it — it never watched the session start, so its host and shell were never observed.";
    adb.setAttribute("data-help", adb.title);
    top.appendChild(adb);
  }
```

- [ ] **Step 6: Run all three new tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py::test_remote_control_waiting_adopted_badges_have_longpress_help tests/test_web.py::test_every_card_badge_has_longpress_help_wired tests/test_web.py::test_key_legend_tap_wiring_is_unchanged -v`
Expected: all PASS.

- [ ] **Step 7: Bump `PAGE_VERSION` and re-pin**

In `crr/core/web.py:45`: `PAGE_VERSION = 75  # v75: badges long-press to reveal an explanation (5/5 — remote-control/waiting/adopted badges)`

Run `.venv/bin/pytest tests/test_page_version_guard.py -v` (fails, prints the sha), add `75: "<sha>",` at the top of `PAGE_PINS`, re-run to confirm PASS.

- [ ] **Step 8: Run the full suite and commit**

Run: `.venv/bin/pytest`
Expected: all pass.

```bash
git add crr/core/page.html crr/core/web.py tests/test_web.py tests/test_page_version_guard.py
git commit -m "$(cat <<'EOF'
feat(dashboard): long-press help for remote-control/waiting/adopted badges (v75)

Closes out the badge list from the design spec: every badge renderCard
creates now sets data-help, and a long-press on any of them (on a real
touch device) shows the explanation via showNotice — the same toast the
#key legend already uses on tap, which stays a tap, unchanged.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VZPsJyEbEN3g6PpV4ZumUR
EOF
)"
```
