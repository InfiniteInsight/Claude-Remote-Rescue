# Long-press explanations for session-card status badges

**Date:** 2026-09-17
**Status:** approved (user-approved design; this doc is its record)

## Problem

Every session card renders a row of status badges (state/parked/attached,
worktree, duplicate, context-pressure, strike, remote-control, waiting,
adopted, latest — `page.html` `renderCard`, ~lines 780-895). Some carry a
`title=` attribute (desktop hover only); most carry none. On a touch
device — and this dashboard is used from a phone at least as often as a
desktop (existing comment, `page.html:2425`) — `title=` never fires, so
touch users have no way to learn what a badge means short of scrolling up
to the `#key` legend and finding the matching term.

The `#key` legend already solves this for itself: each term gets a
`data-help` string, a `title` for desktop hover, and a `click` handler that
shows the explanation as a toast via `showNotice()` (`page.html:2424-2431`,
covered by `test_web.py::test_page_renders_the_not_connected_badge` and the
`#key`-specific tests around line 1750). That mechanism is not wired to the
badges on the cards themselves.

## Decision

Add the explanation to the badges directly, triggered by **long-press**
(not a plain tap):

- A plain tap on a card, in a scrollable list, is at least as likely to be
  a mis-tap or a scroll-release as a deliberate one. Long-press is a
  deliberate, unambiguous gesture, and matches how iOS/Android already
  reveal tooltips/previews elsewhere.
- The `#key` legend's existing tap-to-toast behavior is **unchanged** — it
  already works, and switching it to long-press would be a pure regression
  for anyone used to it today. Only the per-card badges get the new
  gesture.
- **Every** badge gets this treatment — state/parked/attached, worktree,
  duplicate, context-pressure, strike, remote-control, waiting, adopted,
  latest — not just the ones that already have a `title=`. Consistency
  beats saving a few lines: a user who long-presses one badge and gets
  nothing, then long-presses another and gets a toast, would reasonably
  read the silent one as broken.

## Design

### Delegated long-press detector, not per-badge listeners

Cards are torn down and rebuilt on every poll (`setInterval(pollSessions,
POLL_MS)`), so badges are fresh DOM nodes each cycle. Attaching a listener
per badge would mean rewiring on every poll tick for no benefit. Instead,
attach **one** listener set to the sessions container (delegation),
matching how the rest of the page already avoids per-item wiring:

```js
// One shared long-press detector for every badge, delegated on the
// sessions container so re-rendering cards on each poll never needs to
// rewire anything. 500ms mirrors the native long-press threshold on
// iOS/Android; movement beyond a small radius, or an early release,
// cancels it — this must never fire off a scroll gesture.
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
// Desktop click-and-hold, for parity — hover title already covers desktop,
// so this is a nice-to-have, not the primary path there.
sessionsEl.addEventListener("mousedown", function (e) { lpBegin(e.target, e.clientX, e.clientY); });
sessionsEl.addEventListener("mousemove", function (e) { lpMove(e.clientX, e.clientY); });
sessionsEl.addEventListener("mouseup", lpCancel);
sessionsEl.addEventListener("mouseleave", lpCancel);
```

`#sessions` (`page.html:536`) is the real container id — confirmed against
the current markup, not illustrative.

### Preventing native interference

A long-press on text is exactly the gesture mobile browsers use for their
own text-selection/copy bubble, which would visually fight with our toast.
Badges get:

```css
.badge { -webkit-touch-callout: none; user-select: none; }
```

### Help text source

Follow the existing `SID_SOURCE_HELP` precedent (`page.html:585-589`): a
plain JS object mapping badge kind → help string, set as `data-help` on the
badge element at creation time, alongside any existing `title=` (title is
left in place, unaffected, for desktop hover).

- Badges that already carry a `title=` (remote-control, waiting, adopted)
  reuse that exact string for `data-help` — no second copy to drift out of
  sync.
- Badges with an equivalent term in the `#key` legend (state, context-
  pressure) get a `data-help` conveying the same meaning.
- Badges with no explanation anywhere today (worktree, duplicate, strike,
  latest) get new short copy, e.g.:
  - worktree: `"A checkout of a git worktree, not the main branch — grouped under its parent repo below."`
  - duplicate: `"Another card shares this session id. 'possible duplicate' means the id was guessed, not certain."`
  - strike: `"This session died again shortly after being revived. crr gives up reviving it after N/M strikes."`
  - latest: `"The most recently active session in this directory, when more than one is open here."`

  (Final wording is an implementation detail, kept consistent in voice with
  existing badge/legend copy; not pinned further by this spec.)

### Versioning

`page.html` changes are guarded by `PAGE_VERSION` (`crr/core/web.py:45`)
and `test_page_version_guard.py`. Every commit that touches `page.html`
bumps it by one and appends a new pin, following the same pattern as the
v69/v70/v71 tunnel/auth-badge commits — see the implementation plan for the
exact sequence of numbers against this branch's actual starting point.

## Testing

This codebase tests `page.html` as served text/JS source (string
assertions against `web.render_page()` / `web.load_page()`), not with a
real browser — see `test_web.py` around `test_page_renders_the_not_connected_badge`
and the `#key` tests near line 1750. New tests follow the same style:

- Every badge-creation call site sets `data-help` (grep for
  `data-help="` count matching the number of badge kinds, or per-kind
  assertions).
- The long-press detector function(s) exist in the page source
  (`assert "LONGPRESS_MS" in page`, `assert "touchstart" in page`, etc.)
  and use `showNotice` the same way the `#key` handler does.
- `.badge { -webkit-touch-callout: none` (or equivalent) is present in the
  CSS.
- `PAGE_VERSION` bumped and re-pinned per the existing guard test.
- The `#key` legend's own tap-to-toast wiring is untouched (existing tests
  for it keep passing unmodified — a regression check that this change
  didn't touch that code path).

## Out of scope

- Changing the `#key` legend's gesture (stays single-tap).
- A real anchored tooltip bubble UI — reusing the existing shared toast
  (`showNotice`) keeps this consistent with the legend and avoids a new UI
  component for one feature.
- Any change to what information is shown — this only makes existing (and
  a few newly-written) explanations reachable on touch.
