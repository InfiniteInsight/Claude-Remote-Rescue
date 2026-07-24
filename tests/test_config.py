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


def test_audit_floor_priors_are_present():
    # The DESIGN "config floor" — these MUST exist as named keys.
    floor = {
        "zombie_strikes",
        "close_grace_seconds",
        "reopen_grace_seconds",
        "diagnose_lookback_boots",
        "diagnose_event_cap",
        "diagnose_line_cap",
        "interop_timeout_seconds",
        "dashboard_poll_seconds",
        "version_check_seconds",
        "last_prompt_display_cap",
        "watcher_backoff_count",
        "watcher_cooldown_seconds",
    }
    assert floor <= set(cfg.DEFAULTS)


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
