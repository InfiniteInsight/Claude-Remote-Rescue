"""Injectable-priors guard (#37 — audit run-3 P5).

Every judgment-call constant is named config with a versioned default
(AGENTS.md), never a magic number in logic. This class has now regressed
TWICE — run-2 lifted the page's timing literals into `@PLACEHOLDER@`
injection and pinned two literal fallbacks to `config.DEFAULTS`, and both
patterns reappeared in new code. These tests are the pin, and the last one
is the guard that makes the page-timing case mechanically impossible to
reintroduce.
"""

import inspect
import re

from crr.adapters import transcript_source
from crr.core import context_pressure as cp
from crr.core import web
from crr.core.config import DEFAULTS
from crr.core.status import assemble_sessions


# --- the four literal fallbacks in assemble_sessions -----------------------
# Run 2b fixed exactly this shape for web_restart_seconds/model_tail_lines
# ("now reference config.DEFAULTS"); these four were introduced after.

def test_assemble_sessions_defaults_come_from_config():
    params = inspect.signature(assemble_sessions).parameters
    for name, key in (
        ("context_tight_fraction", "context_tight_fraction"),
        ("context_compact_fraction", "context_compact_fraction"),
        ("bridge_stale_records", "bridge_stale_records"),
        ("autokick_config_default", "remote_control_autokick"),
        ("context_bytes_per_token", "context_bytes_per_token"),
    ):
        assert params[name].default == DEFAULTS[key], name


# --- scan/display bounds that had config-keyed siblings --------------------

def test_cwd_scan_lines_is_the_named_config_default():
    # Every sibling scan bound (model_tail_lines, reply_tail_lines,
    # bridge_scan_lines) is a versioned config key with its measurement in
    # the comment; this one was a bare module constant.
    assert transcript_source.CWD_SCAN_LINES == DEFAULTS["cwd_scan_lines"]


def test_discoverable_page_is_the_named_config_default():
    # Every sibling display cap (diag_error_display_cap, recall_match_cap,
    # recall_snippet_cap, last_prompt_display_cap) is a config key.
    assert web.DISCOVERABLE_PAGE == DEFAULTS["discoverable_page_size"]


# --- the token-estimate divisor -------------------------------------------

def test_bytes_per_token_is_injectable_and_defaults_from_config():
    assert cp.estimate_tokens(4000) == 4000 // DEFAULTS["context_bytes_per_token"]
    # A different prior yields a different estimate — proving it is really
    # consulted, not just present.
    assert cp.estimate_tokens(4000, bytes_per_token=2) == 2000


def test_pressure_honors_an_injected_bytes_per_token():
    # haiku-4.5's window is 200_000 tokens. At 4 bytes/token, 200_000*4
    # bytes is exactly the window; at 8 bytes/token it is only half of it.
    at_window = 200_000 * 4
    assert cp.pressure(at_window, "claude-haiku-4-5-20251001",
                       tight=0.7, compact=1.0, bytes_per_token=4) == "will-compact"
    assert cp.pressure(at_window, "claude-haiku-4-5-20251001",
                       tight=0.7, compact=1.0, bytes_per_token=8) == "ok"


def test_window_for_returns_none_rather_than_a_fabricated_default():
    # DEFAULT_WINDOW is gone (#37 + #39): since `pressure` stopped consuming
    # the fallback, it influenced no decision — a dead prior, not an
    # injectable one. `window_for` now returns an honest null instead.
    assert cp.window_for("claude-haiku-4-5-20251001") == 200_000
    assert cp.window_for("some-model-nobody-heard-of") is None
    assert cp.window_for("") is None
    assert not hasattr(cp, "DEFAULT_WINDOW")


# --- the guard: no bare timing literals may return to the page ------------

def test_no_bare_numeric_delay_in_page_timers():
    """Every setTimeout/setInterval delay in page.html must be a NAMED
    constant, never a bare number.

    This is the regression guard. `FLASH_MS = 1400` and a `250`ms debounce
    were added straight into the page months after run 2 lifted every other
    timing literal into a placeholder — nothing in the suite noticed,
    because nothing was looking. Now something is.
    """
    page = web.load_page()
    # Two call shapes appear in this page: a single-line timer, and the far
    # more common `setTimeout(function () { ... }, DELAY)` whose body spans
    # lines (and contains semicolons, which is what defeated the first
    # version of this guard).
    inline = re.findall(r"set(?:Timeout|Interval)\s*\([^()\n]*,\s*(\d+)\s*\)", page)
    closure = re.findall(r"\}\s*,\s*(\d+)\s*\)\s*;", page)
    offenders = inline + closure
    assert offenders == [], (
        f"bare timing literal(s) {offenders} in page.html — add a DEFAULTS key "
        "and an @PLACEHOLDER@ (see CONFIRM_ARM_MS / NOTICE_MS / RELOAD_DELAY_MS)"
    )


def test_no_bare_millisecond_constant_in_page():
    """A `var SOMETHING_MS = <number>` is the same prior wearing a name.

    `FLASH_MS = 1400` passed the timer guard above precisely because it IS
    named — naming a magic number does not inject it. Every *_MS constant
    the page declares must be substituted from config at serve time.
    """
    page = web.load_page()
    bare = re.findall(r"var\s+([A-Z_]*MS)\s*=\s*(\d+)\s*;", page)
    assert bare == [], (
        f"bare millisecond constant(s) {bare} in page.html — source them from "
        "a DEFAULTS key via an @PLACEHOLDER@ instead"
    )


def test_every_page_placeholder_is_substituted_at_serve_time():
    # A raw @PLACEHOLDER@ that survives render is a JS syntax error and the
    # dashboard renders nothing ([lesson 2026-08-01: template/code skew]).
    rendered = web.render_page(1)
    leftovers = re.findall(r"@[A-Z_]+@", rendered)
    assert leftovers == [], leftovers
