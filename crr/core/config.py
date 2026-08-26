"""Config priors (audit P5 — Injectable priors, P3 — visible origin).

Every constant that encodes a judgment call lives here as a named key
with a versioned default — never a magic number in logic. New timing or
threshold decisions join ``DEFAULTS`` at introduction time, not at audit
time (that rule is why this module exists before most of its consumers
do).

``effective()`` reports each key's value AND its origin (``configured``
vs ``default``) so a consumer can always distinguish an explicit choice
from an assumed one — defaults drive kill decisions, and an invisible
default is an invisible prior. It backs ``crr config --effective``.

TOML loading lives here too (``load_toml_overrides``); ``Config`` still
takes a plain overrides mapping so tests need no files.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

# Bump when a default value changes meaning, so a consumer pinning
# behavior can detect the shift.
# v2: dropped watcher_backoff_count / watcher_cooldown_seconds / reopen_grace_seconds
# (no crr mechanism consumes them; see DESIGN)
# v3: added context_tight_fraction / context_compact_fraction (Slice A, F2 —
# compaction badge thresholds; see crr.core.context_pressure)
# v4: added recall_match_cap / recall_snippet_cap (Slice B, F1 — `crr recall`
# print caps; see crr.core.transcript.search)
# v5: added takeover_idle_seconds / takeover_max_wait_seconds /
# takeover_poll_seconds (`crr adopt --takeover`; see crr.core.takeover)
# v6: added recall_scan_byte_budget (dashboard global recall — bounds the
# newest-first whole-transcript sweep; see transcript_source.search_all)
# v7: added discover_exclude_dirs (keep tool-internal transcript dirs out of
# discovery; see crr.core.discovery.is_excluded)
# v8: added reply_tail_lines (bounds the card's "claude said" lookback)
# v9: SKIPPED — never shipped. Reconstructed 2026-08-08 (#38): commit
# 553134e bumped the constant 8 -> 10 in a single step while labelling its
# entry "v9", so from that point every comment sat one number behind the
# version it described. The number is burned rather than reused: a v9 that
# means two different things is worse than a v9 that never existed.
# v10: recall_scan_byte_budget defaults to 0 (unlimited) — the raw-bytes
# prefilter makes a full sweep cheap, so the budget no longer caps coverage;
# recall_match_cap 5 -> 10 and new recall_per_session_cap so one chatty
# session can't fill every result slot. (This is the entry 553134e wrote as
# "v9"; it has always described v10.)
# v11: added remote_control (every claude launch/revival enables Claude
# Code's Remote Control by default, so a session stays reachable from the
# phone; see crr.core.reviver.revival_argv and the shims)
# v12: added remote_control_watch / remote_control_autokick /
# bridge_stale_records / bridge_scan_lines (spec 2026-08-07 — dropped-
# Remote-Control watchdog, Slice 1: detection + the global kill switch; see
# crr.core.bridge and crr.adapters.transcript_source's bridge_seen/
# bridge_since)
# v13: added bridge_kick_cooldown_seconds / bridge_kick_max_attempts (review
# fix-wave 2026-08-07, FIX 1 — a failed reconnect must not become an
# indefinite restart loop; see crr.core.bridge_kicks)
# v14: added flash_ms / filter_debounce_ms / cwd_scan_lines /
# discoverable_page_size / context_bytes_per_token (#37 — six priors the
# run-3 audit caught, three of them a regression of a class run 2 had
# already remediated; tests/test_priors.py is the guard that keeps them
# from coming back a third time)
# v15: REMOVED bridge_stale_records / bridge_scan_lines (spec 2026-08-09,
# Phases 1-3 — the record-counting dropped-Remote-Control detector is gone,
# replaced by Claude Code's own per-process bridgeSessionId; see
# crr.adapters.session_state and crr.core.reachability). Both keys were
# priors of a measurement that is no longer taken, so leaving them would
# have been a config surface that decides nothing. `remote_control_watch`
# and the two bridge_kick_* guards survive — only the thresholds of the
# deleted detector went. The v12 entry above still names them: it is
# history, and history is not edited.
# v16: added power_block / power_block_requires_ac / power_block_max_hours /
# power_poll_seconds (spec 2026-08-12 — keep the machine up while a session
# is live; see crr.core.power). Off by default: a tool that silently stops a
# laptop sleeping is a trust hazard, so it is opted into, not out of.
# v17: added power_state_max_age_multiplier (fix round 1, 2026-08-13 —
# `crr power`/`crr doctor` read the awake loop's cross-process state file
# instead of asking a freshly-constructed holder to `held()`, which could
# only ever answer about its own unrelated child; see
# crr.core.power.interpret / crr.adapters.power_state). The multiplier
# turns `power_poll_seconds` into a staleness threshold for that file: a
# report older than this many polls is read as UNKNOWN, not as "nothing
# held" — a wedged or crashed loop's last snapshot must not be trusted
# forever.
# v18: added harden_active_hours_start / harden_active_hours_end /
# harden_restart_lookback_days (spec 2026-08-12 — Windows Update hardening:
# the active-hours window widens to cover overnight work; see crr.core.harden).
# v19: added boot_headless_window_seconds / boot_preferred_tailnet (spec
# 2026-08-14 — reachable-at-boot, making the dashboard come up at boot
# without a login so a reboot is survivable; see crr.core.reachable_at_boot).
# v20: added launcher_tag (spec 2026-08-18 — Phase 3 Launcher: the Tailscale
# tag used to discover peer machines for the Machines panel; default tag:crr)
# v21: added rescue_auto_open (spec 2026-08-20 — reopen tab reliability:
# auto-open tabs for restored sessions on boot, skipping the [Y/n] prompt)
# v22: added reauth_success_display_ms (dashboard reauth Task 5 — how long
# the reauth modal shows "Login refreshed!" before auto-closing once
# auth_state flips back to valid; same family as flash_ms/notice_seconds,
# a page timing prior injected via @PLACEHOLDER@, never a bare literal in
# page.html; see tests/test_priors.py::test_no_bare_numeric_delay_in_page_timers)
# v23: added dashboard_session_hours (dashboard login — optional passphrase
# auth gate; see crr.core.dashboard_auth)
CONFIG_DEFAULTS_VERSION = 23

# The audit "config floor": each of these was a hardcoded prior the audit
# caught (or a peer of one). Value is the versioned default.
DEFAULTS: dict[str, Any] = {
    # watchdog / revival
    "zombie_strikes": 3,             # strikes before a re-dying session is archived
    "watchdog_interval_seconds": 30, # how often the systemd timer sweeps for revivals
    # session operations
    "close_grace_seconds": 5,     # wait after a polite close before force
    # diagnostics
    "diagnose_lookback_boots": 1,  # how many prior boots to inspect
    "diagnose_event_cap": 50,      # max events returned per source
    "diagnose_line_cap": 200,      # max log lines scanned per source
    "diagnose_macos_lookback": "1d",  # macOS `log show --last` window (no boot index on macOS)
    "diagnose_macos_timeout_seconds": 30,  # macOS `log show` is slow to start; own timeout
    "interop_timeout_seconds": 5,  # per external-command guard in diagnostics/probes
    # Tab spawning gets its OWN budget: launching a cold Windows Terminal is
    # nothing like a ps/tmux probe, and borrowing the 5s above produced false
    # "no tab" reports while the tab opened anyway (#53). Costs nothing warm —
    # the call returns in milliseconds and only a genuine cold start nears it.
    "tab_spawn_timeout_seconds": 30,
    # dashboard
    "dashboard_poll_seconds": 5,   # session-list poll cadence
    "version_check_seconds": 30,   # page self-heal version poll cadence
    "last_prompt_display_cap": 280,  # chars of last prompt shown on a card
    "host_allowlist_extras": [],   # extra Host values the dashboard accepts
    # retention
    "archive_retention_days": 14,  # gc drops archive records older than this
    # tab spawn (macOS/desktop): which terminal a visible reopen uses.
    # "auto" = $TERM_PROGRAM then a sensible default; or force one — macOS:
    # "terminal"/"iterm"; Linux: "gnome-terminal"/"konsole"/"kitty"/"wezterm"
    # (a named prior, not a magic default).
    "terminal": "auto",
    "wt_profile": "",  # Windows Terminal profile for a WSL reopen ("" = default)
    # restore prompt
    "rescue_prompt_timeout_seconds": 15,  # [Y/n] wait before defaulting to "not now"
    "rescue_auto_open": True,        # skip [Y/n] and auto-open tabs for restored sessions
    # dashboard port + service restart cadence (audit 2026-07-31, P5)
    "dashboard_port": 8377,        # `crr web`'s bind port; baked into systemd/launchd/schtasks units
    "web_restart_seconds": 2,      # systemd RestartSec for crr-web.service after a crash
    # page timing/caps injected into page.html at serve time (audit 2026-07-31, P5)
    "confirm_arm_seconds": 4,      # danger-button confirm-arm window before it disarms
    "notice_seconds": 3,           # transient notice banner display duration
    "reload_delay_ms": 800,        # delay before a stale-page self-heal reload
    "diag_error_display_cap": 20,  # max previous-boot error lines rendered client-side
    # (#37) Two more page priors, added after run 2 lifted the four above and
    # missed by nothing until the run-3 audit. `flash_ms` is how long a card
    # highlights after a search result jumps to it — long enough for the eye
    # to land on it, short enough not to linger as if it were a state.
    # `filter_debounce_ms` is the pause after the last keystroke before the
    # discoverable modal re-queries the server; that request enriches
    # transcripts, so firing it per keystroke is real work.
    "flash_ms": 1400,
    "filter_debounce_ms": 250,
    # Rows per page in the dashboard's discoverable modal. Enriching every
    # untracked transcript to render one page cost ~10s on a machine with a
    # few thousand of them, which is why the panel pages server-side at all.
    "discoverable_page_size": 20,
    # transcript scan bound (audit 2026-07-31, P5): a real model id always sits
    # within a few lines of the tail (measured on 3243 live transcripts:
    # p50=3, p99=37 lines back), but ~1 in 3 transcripts carry NO model at all
    # — so the model search stops here rather than reading a model-less
    # transcript in full on every poll. See transcript_source.read_tail_facts.
    "model_tail_lines": 200,
    # How far back past the last prompt to look for claude's preceding reply
    # (card "claude" line). Measured on real transcripts here: the reply sat
    # 4-65 records before the prompt, and the prompt itself up to ~124 from
    # the tail — 400 covers both with headroom, and an honest "" beyond it.
    "reply_tail_lines": 400,
    # How far into a transcript to look for the AUTHORITATIVE cwd its own
    # records carry (transcript_source.read_cwd). Observed: the cwd appears
    # within the first handful of records in every transcript seen — the
    # session-start/snapshot header lines don't carry it, the first real
    # turn does. Bounded so a cwd-less transcript is never read end to end.
    # Same shape as model_tail_lines / reply_tail_lines.
    "cwd_scan_lines": 200,
    # Every claude launch/resume/revival crr is involved in passes
    # `--remote-control <name>` (an explicit name — never a bare flag; see
    # the reviver/shim comments on why) so the session is always reachable
    # from Claude Code's mobile Remote Control. False disables it
    # everywhere at once (reviver.py + all three shims ask this key).
    "remote_control": True,
    # context-pressure badge (Slice A, F2; audit P5): fraction of a model's
    # context window at which a session is "tight" vs. expected to compact
    # on revive. Every window in MODEL_CONTEXT_WINDOWS is confirmed against
    # published model docs (each entry carries its source); a model NOT in
    # that map yields context_pressure "unknown" rather than borrowing a
    # number (#39). Corrected 2026-08-08 (#38): this comment used to say
    # "unverified for most models", which stopped being true when the map
    # was confirmed and contradicted context_pressure.py's own docstring.
    # See crr.core.context_pressure.pressure.
    "context_tight_fraction": 0.7,
    "context_compact_fraction": 1.0,
    # Bytes per token — the divisor behind the context estimate (#37). A
    # rough rule of thumb, NOT a tokenizer: it varies with content (code and
    # JSON pack denser than prose), so it is the single most load-bearing
    # assumption behind every compaction badge, and belongs here where it
    # can be seen and changed rather than buried in a `// 4`.
    "context_bytes_per_token": 4,
    # `crr recall` (Slice B, F1): query-scoped, capped transcript search — a
    # grep for your own history, not a transcript dump (never uncapped).
    "recall_match_cap": 10,     # max matches printed, most-recent-first (-n)
    # Max matches ONE session may contribute to a multi-session search. Found
    # live: a "dokploy" search returned 5 matches all from the newest (very
    # chatty) session, so the session actually being looked for never
    # appeared. Recency still orders results; this only stops one transcript
    # taking every slot. 0 = no per-session cap.
    "recall_per_session_cap": 2,
    "recall_snippet_cap": 500,  # chars of matched text shown per match
    # Backstop only: cap the cumulative transcript bytes one global recall
    # sweep may read. 0 = unlimited (the default). A raw-bytes prefilter
    # skips files that cannot match before any parsing, so a FULL sweep of
    # this author's 411 MB / 48-transcript corpus takes ~0.75s — a budget
    # small enough to matter would cost coverage (50 MB reached only 5 of
    # 48 transcripts here, hiding older sessions from search) for a saving
    # measured in fractions of a second. Raise above 0 only for a corpus
    # large enough that a full read is genuinely too slow.
    "recall_scan_byte_budget": 0,
    # Directory names whose transcripts are NOT the user's own conversations
    # and so never appear as "discoverable". Default: claude-mem's observer
    # sessions, which on a busy machine can be >98% of all transcripts.
    "discover_exclude_dirs": [".claude-mem"],
    # `crr adopt --takeover` (destructive: SIGTERMs a live process) — the
    # idle window + refusal timeout + poll cadence for the cli-owned wait
    # loop. See crr.core.takeover.ready_to_take_over. idle_window doubles
    # as a decision threshold, not just a "feels idle" gate: a transcript
    # quiet for less than this refuses fast as "still mid-turn", so the
    # window must exceed the longest expected no-write gap during ACTIVE
    # generation (extended thinking / a slow non-streaming API turn) or
    # it produces false "parked mid-turn" refusals — 20s is the safer
    # floor (12s measured too short against that gap).
    "takeover_idle_seconds": 20.0,       # transcript quiet + clean tail this long
    "takeover_max_wait_seconds": 180.0,  # give up (refuse, never kill) after this
    "takeover_poll_seconds": 2.0,        # wait-loop poll cadence
    # Dropped-Remote-Control watchdog (spec 2026-08-09, Phases 1-3). Detects
    # a session whose mobile Remote Control link has gone down, by reading
    # Claude Code's OWN per-process `bridgeSessionId` state file — see
    # crr.adapters.session_state and crr.core.reachability. (It used to count
    # transcript records since the newest `bridge-session` marker; that
    # needed a median of 8 minutes of ACTIVE work to fire and never fired at
    # all on an idle session, which is the case the feature exists for.)
    # False turns off detection, the card badge, AND the per-poll cost, not
    # just the watchdog's kick step: cli._reachability_by_sid skips both the
    # `~/.claude/sessions` scan and the process-table snapshot, which reads
    # as an honest "unknown" card state (#33 — never a positive claim about
    # a session nothing looked at). The kick step (cli._kick_dropped_bridges)
    # also gates on this directly.
    "remote_control_watch": True,
    "remote_control_autokick": True,   # GLOBAL hard switch for auto-kicking a dropped session (consumed from Slice 2's watchdog step)
    # Watchdog restart-loop guards (review fix-wave 2026-08-07, FIX 1 —
    # CRITICAL). Without these, a failed reconnect (host briefly offline,
    # auth expired, Remote Control unavailable) re-qualifies for a kick on
    # every 30s `crr revive` pass forever: a kick does not itself advance
    # the bridge marker, and the session stays LIVE at a clean turn
    # boundary, so every guard clears again next pass. See
    # crr.core.bridge_kicks for the per-sid history this reads/writes.
    "bridge_kick_cooldown_seconds": 600,  # never re-kick the same sid within this many seconds
    "bridge_kick_max_attempts": 3,        # consecutive attempts before giving up; resets only on a confirmed "ok"
    # Power blocking (spec 2026-08-12). "off" | "sleep" | "sleep+shutdown".
    # "sleep" means AUTOMATIC/idle sleep only — lid close is never blocked
    # on any platform, which is a hard requirement, not a default.
    "power_block": "off",
    # A forgotten session must not flatten an unplugged laptop.
    "power_block_requires_ac": True,
    # Belt-and-braces against a holder that outlives crr and blocks
    # restarts with nothing left to explain why.
    "power_block_max_hours": 12,
    "power_poll_seconds": 30,        # how often crr-awake re-decides
    # How many missed polls before the awake loop's cross-process state
    # file (fix round 1, 2026-08-13) is read as UNKNOWN rather than
    # trusted. A judgment call, not a measurement: 3 misses tolerates one
    # slow poll without flapping to "unknown" on every run of `crr power`,
    # while still catching a genuinely wedged loop within a few intervals.
    "power_state_max_age_multiplier": 3,
    # Windows Update hardening (spec 2026-08-12). Active hours are the window
    # in which Windows will NOT auto-restart; it may wrap midnight and
    # Windows caps the span at 18 hours (crr.core.harden.MAX_ACTIVE_HOURS_SPAN).
    # 08:00-02:00 is the maximum span anchored on a late working day, chosen
    # because sessions that run past midnight are exactly the ones a forced
    # restart destroys.
    "harden_active_hours_start": 8,
    "harden_active_hours_end": 2,
    # How far back to look for restarts when reporting whether hardening held.
    "harden_restart_lookback_days": 14,
    # reachable-at-boot (spec 2026-08-14). The window, in seconds, within which
    # the control surface coming up after MACHINE boot counts as "headless"
    # rather than "only at login". Generous slack for a slow cold boot; the
    # measured real gap on the reference host was 39s.
    "boot_headless_window_seconds": 300,
    # Which Tailscale account the boot task re-selects. Empty means "whatever
    # is active at install time" — crr never silently picks a tailnet.
    "boot_preferred_tailnet": "",
    # launcher (Phase 3)
    "launcher_tag": "tag:crr",
    # dashboard reauth (Task 5): how long the reauth modal shows "Login
    # refreshed!" before auto-closing, once a poll reports auth_state back
    # to "valid". Same family as flash_ms — long enough to read, short
    # enough not to linger as if it were a state.
    "reauth_success_display_ms": 2000,
    # dashboard login (spec 2026-08-26): how long a login session cookie
    # stays valid. 720 hours = 30 days. Changing this does not invalidate
    # existing sessions — it only affects the Max-Age on NEW cookies and
    # the server-side expiry check.
    "dashboard_session_hours": 720,
}


class ConfigError(ValueError):
    """A config override is invalid (unknown key or wrong value type)."""


def load_toml_overrides(path: Path | str) -> dict[str, Any]:
    """Read a config.toml into an overrides dict ({} if the file is absent)."""
    path = Path(path)
    if not path.is_file():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


class Config:
    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        overrides = dict(overrides or {})
        unknown = set(overrides) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown config key(s): {sorted(unknown)}")
        # Type-check each override against its default so a mistyped config.toml
        # fails loudly rather than injecting e.g. a str where logic wants an int.
        for key, value in overrides.items():
            default = DEFAULTS[key]
            # bool is an int subclass; keep them from cross-matching.
            if isinstance(value, bool) != isinstance(default, bool) or not isinstance(value, type(default)):
                raise ConfigError(
                    f"config '{key}' must be {type(default).__name__}, got {type(value).__name__}"
                )
        self._overrides = overrides

    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(key)
        return self._overrides.get(key, DEFAULTS[key])

    def effective(self) -> dict[str, tuple[Any, str]]:
        """Return ``{key: (value, origin)}`` for every known key."""
        return {
            key: (
                (self._overrides[key], "configured")
                if key in self._overrides
                else (default, "default")
            )
            for key, default in DEFAULTS.items()
        }
