# Keychain-Blind Reauth Design

**Date:** 2026-09-17
**Status:** Implemented
**Supersedes nothing.** Fixes a gap in
[2026-08-21-dashboard-reauth-design.md](2026-08-21-dashboard-reauth-design.md).

**Motivation:** The dashboard reauth feature shipped working — and
invisible on any host that does not keep its OAuth tokens in
`~/.claude/.credentials.json`. Reported from the field: a login expired
on a laptop while its owner was away from it, and the dashboard showed a
clean header with no Reauth button. That is the *only* case the feature
exists to serve; a user sitting at the machine can just run `claude auth
login`.

## Problem

The 2026-08-21 design named exactly one source:

> Claude Code authenticates via OAuth tokens stored in
> `~/.claude/.credentials.json`.

That is true on some hosts and false on others. Claude Code also stores
credentials:

- in the **macOS login Keychain**, with no credentials file on disk at
  all;
- under **`$CLAUDE_CONFIG_DIR`** when that env var relocates the config
  directory — which `_credentials_path` ignored outright, its docstring
  saying only "the location itself is not currently configurable";
- **nowhere local** behind an enterprise auth gateway (`claude gateway`).

Verified during this work on a plain Linux host: `claude auth status
--json` reports `{"loggedIn": true, "authMethod": "oauth_token"}` while
`~/.claude/.credentials.json` does not exist. The file's absence carries
no information about the login at all.

On every such host the chain degraded like this:

1. `_read_credentials` returns None (file missing).
2. `auth.auth_state` returns `("unknown", None)` — correct and honest.
3. `renderAuthBadge` handled `unknown` **in the same branch as `valid`**:
   blank badge, blank class, early return.

So "crr cannot tell" rendered pixel-for-pixel identically to "your login
is fine", and the Reauth button — reachable only from the `expired`
branch — never appeared. The one screen a remote user had left showed
nothing wrong.

Two further consequences, both silent:

- **`crr revive`'s guard.** It suppresses the revival pass on `expired`,
  because a revived session launches under the same dead token, dies, and
  burns a give-up strike. `unknown` is not `expired`, so the guard never
  fired and every sweep spent a strike.
- **`_kick_dropped_bridges` guard 0.** Same test, same outcome: the
  watchdog kept restarting live sessions that could not authenticate,
  burning the capped attempts that exist to stop exactly that loop.

Both are worse on these hosts than the missing badge, because they
consume budgets the user cannot see.

## Changes

### 1. Honor `$CLAUDE_CONFIG_DIR` (`crr/cli.py`)

`_credentials_path` reads `$CLAUDE_CONFIG_DIR` and falls back to
`~/.claude`. A blank-but-exported value is ignored rather than resolving
the file to `/.credentials.json`.

This is the cheap third of the fix and the only one a path change can
buy. The Keychain and gateway cases cannot be fixed by any list of paths,
which is why the rest of this design adds a source rather than more
places to look.

### 2. A second source: `AuthStatusSource`

| Piece | Layer | Role |
|-------|-------|------|
| `AuthStatus` / `AuthStatusSource` | `crr/core/ports.py` | The port. `logged_in` is tri-state per F16. |
| `ClaudeAuthStatus` | `crr/adapters/claude_auth.py` | Runs `claude auth status --json`, parses `loggedIn`. |
| `_auth_status_source` | `crr/cli.py` | Selection at the composition root. |

`logged_in` is `True` / `False` / `None`, and `None` is load-bearing: a
missing binary, a timeout, a nonzero exit (a `claude` too old for the
subcommand), unparseable output, or a `loggedIn` that is not literally a
bool all degrade to "could not tell". None of them is evidence of being
signed out, and a `None` that became a `False` would put "Login expired"
on a healthy host.

The probe is coarser than the file **by nature**: `loggedIn` is a boolean
with no timestamps, so it can confirm an expiry but can never justify the
3-day `expiring` warning. That asymmetry drives the precedence below.

### 3. Precedence, in pure core (`crr/core/auth.py`)

`resolve_auth_state(credentials, *, now, logged_in) -> AuthResolution`,
returning `(state, expires_in_seconds, source)`.

| File says | Probe says | Result | Source |
|-----------|-----------|--------|--------|
| `valid` / `expiring` | *(not consulted)* | as the file says, with countdown | `credentials_file` |
| `expired` | `True` | `valid` | `cli_probe` |
| `expired` | `False` or `None` | `expired`, with countdown | `credentials_file` |
| `unknown` | `True` | `valid` | `cli_probe` |
| `unknown` | `False` | `expired`, **no** countdown | `cli_probe` |
| `unknown` | `None` | `unknown` | `none` |

Why the probe gets a veto over an `expired` file: a stale
`.credentials.json` left behind by a host that has since moved its tokens
to the Keychain reads as long-expired forever. Believing it would pin a
false "Login expired" badge on a perfectly healthy host and suppress the
kick watchdog permanently — the mirror image of the bug being fixed. A
*silent* probe cannot veto: absence of evidence does not rescue an
expired file.

`auth_state` (the file-only classifier) is unchanged and still exported.
It remains the whole answer on the common path.

### 4. Cost: the probe runs only when the file cannot answer

`_resolve_auth` (and `_resolve_auth_at`, for the watchdog's injected
path) spends the subprocess only when the file classification is
`unknown` or `expired` — the two rows above where the probe can change
the answer. A readable, healthy file costs nothing extra, which matters
because this sits on the dashboard poll path.

`claude_auth_probe_timeout_seconds` (default 15, config defaults v26)
gets its own key rather than borrowing `interop_timeout_seconds`: this
spawns node, which is nothing like a `ps`/tmux read, and a 5s borrow
times out on a cold start and reports "could not tell" on a host that
knew the answer. Same reasoning that gave `tab_spawn_timeout_seconds` its
own budget.

### 5. Provenance on the wire (sessions contract v18)

The payload gains `auth_source` (`credentials_file` | `cli_probe` |
`none`). P3 — confidence travels with data. The two sources are not
interchangeable: only `credentials_file` can populate
`auth_expires_in_seconds`, and a `cli_probe` verdict must not be dressed
up as a countdown crr never read. A v17 consumer would do exactly that,
so the version moves.

### 6. `unknown` becomes visible (`page.html`, PAGE_VERSION 71)

- `valid` keeps the blank badge and the modal auto-close.
- `unknown` gets its own muted (grey, **not** red) badge — "Login state
  unknown" — plus the Reauth button, and a tooltip saying crr could not
  read the credentials file and `claude auth status` did not answer.
  Deliberately not styled as an expiry: `unknown` is not a confirmed
  anything, and claiming otherwise would be the mirror of drawing it as
  nothing.
- `expired` keeps its red badge and gains a source-dependent tooltip, so
  a probe-sourced verdict says plainly that there is no expiry time to
  show.

The Reauth button is shared by both branches (`reauthButton()`). Giving
it to `unknown` is the point of the change: a user who cannot reach the
machine needs the escape hatch *more* when detection cannot name the
problem, and `POST /api/reauth` never depended on `auth_state` anyway.

### 7. Both watchdog guards route through the resolved state

`crr revive`'s revival gate and `_kick_dropped_bridges`' guard 0 both
call the resolver instead of reading the file. The watchdog takes
`auth_status_source` as a new keyword beside its existing
`credentials_path`; `None` keeps the pre-2026-09-17 file-only behaviour,
and the real caller in `_cmd_revive` arms it.

## What doesn't change

- The reauth flow itself — `claude auth login` in the `crr-reauth` tmux
  pane, capture-pane URL scraping, `send_keys` for the code,
  `_post_reauth_recovery` on the expired → valid transition. All of it
  was correct; it was only ever unreachable.
- `auth.auth_state`'s classification, the 3-day window, the `AUTH_STATES`
  enum.
- Both API endpoints, their CSRF/Content-Type posture, and the
  single-flight `_reauth_active` guard.
- Session cards. `auth_state` and `auth_source` are payload-level.

## Layering

- `crr/core/auth.py`, `crr/core/ports.py`, `crr/core/contracts.py`,
  `crr/core/status.py`, `crr/core/page.html` — core. Still pure: the
  resolver takes the probe's *answer*, never the probe.
- `crr/adapters/claude_auth.py` — adapter, imports only
  `crr.core.ports`.
- `crr/cli.py` — selection and both halves of the I/O.

`lint-imports`: 1 kept, 0 broken.

## Testing

- `tests/test_auth.py::TestResolveAuthState` — every row of the
  precedence table, plus the stale-file veto and the "every resolved
  state is a declared member" sweep.
- `tests/test_claude_auth.py` — the adapter, with `subprocess.run`
  faked: both bools, and each degrade-to-None path (missing binary,
  timeout, nonzero exit, non-JSON, no key, non-bool, non-object,
  unexpected raise). Port conformance via `isinstance`.
- `tests/test_cli.py::TestCredentialsPath` — default, `$CLAUDE_CONFIG_DIR`,
  blank-var.
- `tests/test_cli.py::TestResolveAuth` — including
  `test_healthy_file_does_not_spend_a_subprocess` (the cost gate is
  behaviour, not a comment) and a probe that raises anyway.
- `tests/test_cli.py::TestReviveUsesBothAuthSources` and
  `TestKickSuppressionUsesBothAuthSources` — both guards on a
  no-credentials-file host, both directions.
- `tests/test_cli.py` — served-payload tests for `cli_probe`-sourced
  `expired` (with `auth_expires_in_seconds is None`) and
  `credentials_file`-sourced `valid`.
- `tests/test_web.py` — the page half, including
  `test_unknown_auth_state_is_no_longer_folded_into_valid` pinned
  directly against the regression.
- `tests/test_contracts.py` — v18 `auth_source` enum, every member, and
  a v17-shaped payload now failing as incomplete.

### Safety

- No test reads a real credentials file or runs real `claude`. A new
  autouse `conftest.py` fixture,
  `_forbid_unstubbed_claude_auth_probe`, defaults the adapter's
  `subprocess` to a loud raise — same pattern as
  `_forbid_unstubbed_tab_spawn`. It was needed immediately: three
  existing revive/reauth tests write an expired fixture file, and with
  the seam live they consulted the developer's real login (green on a
  signed-in laptop, red in CI, for reasons nothing in the test named)
  and nearly doubled the suite's runtime spawning node.

## Deployment note

A host whose `crr` snapshot predates this change keeps the old blindness,
and the dashboard footer is how to tell from a phone: the reauth badge
and modal shipped at PAGE_VERSION 60, the build footer itself at 64, and
this change is 71. No footer at all means the deploy predates 64. See
`crr doctor`'s deploy-drift check (PR #110).
