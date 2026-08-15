"""Config-priors tests (audit P5 — Injectable priors, P3 — visible origin).

Every judgment-call constant is a named config key with a versioned
default; nothing is a magic number buried in logic. And every key reports
whether its value was `configured` or is a `default`, so a consumer can
tell an explicit choice from an assumed one (defaults drive kill
decisions — an invisible default is an invisible prior).

TOML file loading is a later increment; this covers the defaults registry
and the origin tracking, which is what the priors need right now.
"""

import pytest

from crr.core import config as cfg


def test_defaults_version_is_int():
    assert isinstance(cfg.CONFIG_DEFAULTS_VERSION, int)


def test_close_grace_seconds_default():
    from crr.core.config import Config
    assert Config().get("close_grace_seconds") == 5


def test_rescue_prompt_timeout_seconds_default():
    from crr.core.config import Config
    assert Config().get("rescue_prompt_timeout_seconds") == 15


def test_audit_floor_priors_are_present():
    # The DESIGN "config floor" — these MUST exist as named keys.
    floor = {
        "zombie_strikes",
        "close_grace_seconds",
        "diagnose_lookback_boots",
        "diagnose_event_cap",
        "diagnose_line_cap",
        "interop_timeout_seconds",
        "dashboard_poll_seconds",
        "version_check_seconds",
        "last_prompt_display_cap",
        "watchdog_interval_seconds",
        "archive_retention_days",
        "host_allowlist_extras",
        "rescue_prompt_timeout_seconds",
        # audit 2026-07-31 (P5): priors lifted from hardcoded literals.
        "dashboard_port",
        "web_restart_seconds",
        "confirm_arm_seconds",
        "notice_seconds",
        "reload_delay_ms",
        "diag_error_display_cap",
        "model_tail_lines",
    }
    assert floor <= set(cfg.DEFAULTS)


def test_dashboard_port_default():
    assert cfg.Config().get("dashboard_port") == 8377


def test_web_restart_seconds_default():
    assert cfg.Config().get("web_restart_seconds") == 2


def test_page_timing_and_cap_defaults():
    c = cfg.Config()
    assert c.get("confirm_arm_seconds") == 4
    assert c.get("notice_seconds") == 3
    assert c.get("reload_delay_ms") == 800
    assert c.get("diag_error_display_cap") == 20


def test_model_tail_lines_default():
    assert cfg.Config().get("model_tail_lines") == 200


def test_vestigial_keys_are_gone_and_version_bumped():
    """[audit 2026-07-29] these keys had zero consumers — a knob wired to
    nothing is an invisible lie, not a prior."""
    for gone in ("reopen_grace_seconds", "watcher_backoff_count", "watcher_cooldown_seconds"):
        assert gone not in cfg.DEFAULTS
        with pytest.raises(cfg.ConfigError):
            cfg.Config({gone: 1})   # now an unknown key: loud, not silent
    # v19 (2026-08-14): reachable-at-boot keys (boot_headless_window_seconds + boot_preferred_tailnet).
    assert cfg.CONFIG_DEFAULTS_VERSION == 19


def test_context_pressure_fraction_defaults():
    # Slice A / F2 (audit P5): context-pressure thresholds are named priors,
    # not magic numbers in crr.core.context_pressure.
    assert cfg.DEFAULTS["context_tight_fraction"] == 0.7
    assert cfg.DEFAULTS["context_compact_fraction"] == 1.0
    assert cfg.Config().get("context_tight_fraction") == 0.7
    assert cfg.Config().get("context_compact_fraction") == 1.0


def test_recall_caps_defaults():
    # Slice B / F1 (`crr recall`): print caps are named config, never a
    # magic number in cli.py or the search path.
    assert cfg.DEFAULTS["recall_match_cap"] == 10
    assert cfg.DEFAULTS["recall_snippet_cap"] == 500
    assert cfg.DEFAULTS["recall_scan_byte_budget"] == 0  # 0 = unlimited
    assert cfg.Config().get("recall_match_cap") == 10
    assert cfg.DEFAULTS["recall_per_session_cap"] == 2
    assert cfg.Config().get("recall_snippet_cap") == 500
    assert cfg.Config().get("recall_scan_byte_budget") == 0
    assert cfg.DEFAULTS["discover_exclude_dirs"] == [".claude-mem"]
    assert cfg.Config().get("discover_exclude_dirs") == [".claude-mem"]
    assert cfg.DEFAULTS["reply_tail_lines"] == 400


def test_remote_control_defaults_to_enabled():
    # A revived (or freshly launched) session should stay reachable from
    # the phone by default; set false to opt out everywhere crr starts or
    # resumes claude.
    assert cfg.DEFAULTS["remote_control"] is True
    assert cfg.Config().get("remote_control") is True


def test_terminal_prior_defaults_to_auto():
    # DESIGN: tab choice is auto-detected but config-overridable — so the
    # choice is a named prior (audit P5), not a magic default in logic.
    assert cfg.DEFAULTS["terminal"] == "auto"
    assert cfg.Config().get("terminal") == "auto"
    assert cfg.Config(overrides={"terminal": "iterm"}).get("terminal") == "iterm"


def test_takeover_defaults():
    # `crr adopt --takeover` (audit: destructive op, thresholds must be
    # named priors): idle window, refusal timeout, and poll cadence.
    assert cfg.DEFAULTS["takeover_idle_seconds"] == 20.0
    assert cfg.DEFAULTS["takeover_max_wait_seconds"] == 180.0
    assert cfg.DEFAULTS["takeover_poll_seconds"] == 2.0
    assert cfg.Config().get("takeover_idle_seconds") == 20.0
    assert cfg.Config().get("takeover_max_wait_seconds") == 180.0
    assert cfg.Config().get("takeover_poll_seconds") == 2.0


def test_remote_control_watchdog_defaults():
    # Dropped-Remote-Control watchdog: detection + kill-switch priors.
    assert cfg.DEFAULTS["remote_control_watch"] is True
    assert cfg.DEFAULTS["remote_control_autokick"] is True
    assert cfg.Config().get("remote_control_watch") is True
    assert cfg.Config().get("remote_control_autokick") is True


def test_the_record_counting_detectors_thresholds_are_gone(tmp_path):
    # v15 (spec 2026-08-09, Phases 1-3): the detector that counted
    # transcript records since the newest `bridge-session` marker is gone —
    # reachability comes from Claude Code's own `bridgeSessionId`. Both
    # thresholds were priors of a measurement no longer taken, so they must
    # not linger as knobs wired to nothing (the same rule
    # `test_vestigial_keys_are_gone_and_version_bumped` pins).
    for gone in ("bridge_stale_records", "bridge_scan_lines"):
        assert gone not in cfg.DEFAULTS
        with pytest.raises(cfg.ConfigError):
            cfg.Config({gone: 1})   # an unknown key: loud, not silent


def test_bridge_kick_restart_loop_guard_defaults():
    # Review fix-wave 2026-08-07, FIX 1 (CRITICAL): a failed reconnect must
    # not become an indefinite restart loop. See crr.core.bridge_kicks.
    assert cfg.DEFAULTS["bridge_kick_cooldown_seconds"] == 600
    assert cfg.DEFAULTS["bridge_kick_max_attempts"] == 3
    assert cfg.Config().get("bridge_kick_cooldown_seconds") == 600
    assert cfg.Config().get("bridge_kick_max_attempts") == 3


def test_no_overrides_all_default():
    c = cfg.Config()
    eff = c.effective()
    assert set(eff) == set(cfg.DEFAULTS)
    for key, (value, origin) in eff.items():
        assert origin == "default"
        assert value == cfg.DEFAULTS[key]


def test_override_marks_key_configured():
    c = cfg.Config(overrides={"zombie_strikes": 7})
    eff = c.effective()
    assert eff["zombie_strikes"] == (7, "configured")
    # An untouched key stays default.
    assert eff["interop_timeout_seconds"][1] == "default"


def test_get_returns_effective_value():
    c = cfg.Config(overrides={"interop_timeout_seconds": 9})
    assert c.get("interop_timeout_seconds") == 9
    assert c.get("zombie_strikes") == cfg.DEFAULTS["zombie_strikes"]


def test_unknown_override_key_rejected():
    # A typo'd config key must fail loudly, not silently do nothing.
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overrides={"zombie_strkes": 7})  # deliberate typo


def test_get_unknown_key_raises():
    c = cfg.Config()
    with pytest.raises(KeyError):
        c.get("no_such_key")


def test_override_type_mismatch_rejected():
    # A config.toml with the wrong type for a key must fail loudly, not
    # silently inject a string where logic expects an int.
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overrides={"zombie_strikes": "three"})


def test_list_valued_prior_round_trips():
    c = cfg.Config(overrides={"host_allowlist_extras": ["box.example.com"]})
    assert c.get("host_allowlist_extras") == ["box.example.com"]


def test_load_toml_overrides_reads_a_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('zombie_strikes = 7\nhost_allowlist_extras = ["a.example"]\n', encoding="utf-8")
    overrides = cfg.load_toml_overrides(p)
    assert overrides == {"zombie_strikes": 7, "host_allowlist_extras": ["a.example"]}
    c = cfg.Config(overrides=overrides)
    assert c.get("zombie_strikes") == 7
    assert c.effective()["zombie_strikes"] == (7, "configured")


def test_load_toml_overrides_missing_file_is_empty(tmp_path):
    assert cfg.load_toml_overrides(tmp_path / "nope.toml") == {}


def test_power_block_keys_exist_with_safe_defaults():
    # Off by default: a tool that silently stops your laptop sleeping is a
    # trust hazard, so it must be opted into.
    assert cfg.DEFAULTS["power_block"] == "off"
    assert cfg.DEFAULTS["power_block_requires_ac"] is True
    assert cfg.DEFAULTS["power_block_max_hours"] == 12
    assert cfg.DEFAULTS["power_poll_seconds"] == 30


def test_power_state_max_age_multiplier_default():
    # fix round 1 (2026-08-13): the staleness threshold for the awake
    # loop's cross-process state file — a named prior, not a bare `3` in
    # cli._power_report.
    assert cfg.DEFAULTS["power_state_max_age_multiplier"] == 3


def test_config_defaults_version_covers_the_power_keys():
    assert cfg.CONFIG_DEFAULTS_VERSION >= 17


def test_harden_keys_exist_with_a_legal_default_window():
    from crr.core.harden import valid_span
    start = cfg.DEFAULTS["harden_active_hours_start"]
    end = cfg.DEFAULTS["harden_active_hours_end"]
    assert (start, end) == (8, 2)
    # The default must itself be a window Windows would accept — a default
    # that fails validation would make `crr harden` unusable out of the box.
    assert valid_span(start, end) is None
    assert cfg.DEFAULTS["harden_restart_lookback_days"] == 14


def test_config_defaults_version_covers_the_harden_keys():
    assert cfg.CONFIG_DEFAULTS_VERSION >= 18


def test_reachable_at_boot_keys_exist():
    # A restart-came-up-headless window: WSL/systemd started within this many
    # seconds of MACHINE boot counts as headless (vs. only at login). 5 min is
    # generous slack for a slow cold boot; measured real gap was 39s.
    assert cfg.DEFAULTS["boot_headless_window_seconds"] == 300
    # Empty = "the tailnet active at install time"; crr never silently picks.
    assert cfg.DEFAULTS["boot_preferred_tailnet"] == ""


def test_config_defaults_version_covers_the_boot_keys():
    assert cfg.CONFIG_DEFAULTS_VERSION >= 19
