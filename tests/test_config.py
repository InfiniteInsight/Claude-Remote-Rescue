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
    assert cfg.CONFIG_DEFAULTS_VERSION == 11


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
