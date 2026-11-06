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


def test_rescue_auto_open_default():
    assert cfg.DEFAULTS["rescue_auto_open"] is True
    assert cfg.Config().get("rescue_auto_open") is True


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
    assert c.get("longpress_ms") == 500
    assert c.get("longpress_move_px") == 10


def test_model_tail_lines_default():
    assert cfg.Config().get("model_tail_lines") == 200


def test_vestigial_keys_are_gone_and_version_bumped():
    """[audit 2026-07-29] these keys had zero consumers — a knob wired to
    nothing is an invisible lie, not a prior."""
    for gone in ("reopen_grace_seconds", "watcher_backoff_count", "watcher_cooldown_seconds"):
        assert gone not in cfg.DEFAULTS
        with pytest.raises(cfg.ConfigError):
            cfg.Config({gone: 1})   # now an unknown key: loud, not silent
    # v21 (2026-08-20): rescue_auto_open (spec — reopen tab reliability: auto-open on boot).
    # v22 (2026-08-21): reauth_success_display_ms (dashboard reauth Task 5 —
    # reauth modal auto-close delay, sourced from config like every other
    # page timing prior).
    # v23 (2026-08-26): dashboard_session_hours (dashboard login — session
    # cookie Max-Age; see crr.core.dashboard_auth).
    # v24 (2026-08-29): zombie_strikes default 3 -> 5 (reviver hardening —
    # activity-keyed strikes now accumulate against alive-but-frozen
    # sessions, so healthy-but-idle ones deserve more runway before
    # give-up; user decision on the 2026-08-29 spec).
    # v25 (2026-09-02): tunnel_provider + cloudflare_tunnel_name + cloudflare_hostname (pluggable tunnel spec).
    # v26 (2026-09-17): claude_auth_probe_timeout_seconds (keychain-blind
    # reauth — the `claude auth status --json` fallback probe's own budget,
    # named rather than borrowing interop_timeout_seconds, because it
    # spawns node and a 5s borrow times out on a cold start).
    # v27 (2026-09-17): longpress_ms + longpress_move_px (badge long-press
    # explanation toast delay and movement-cancel radius, page timing/
    # geometry priors).
    # v28 (2026-09-28): boot_holder_rearm_minutes (#138 — the WSL holder
    # task repeats so a distro-only restart re-arms it).
    # v29 (2026-10-09): diagnose_scan_cap (#148 — journald host events are
    # classified from up to this many scanned lines, then capped).
    assert cfg.CONFIG_DEFAULTS_VERSION == 29
    assert cfg.DEFAULTS["diagnose_scan_cap"] == 5000


def test_zombie_strikes_default_is_five():
    # Reviver hardening (spec 2026-08-29): strikes now accumulate against
    # alive-but-frozen sessions too, so the default gives an idle-but-
    # healthy session five reboots of runway instead of three.
    assert cfg.DEFAULTS["zombie_strikes"] == 5


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


def test_launcher_tag_default():
    assert cfg.DEFAULTS["launcher_tag"] == "tag:crr"
    assert cfg.Config().get("launcher_tag") == "tag:crr"


def test_tunnel_provider_defaults():
    # Tunnel spec 2026-09-02: default preserves current behavior exactly
    # (tailscale, nothing else configured).
    assert cfg.DEFAULTS["tunnel_provider"] == "tailscale"
    assert cfg.DEFAULTS["cloudflare_tunnel_name"] == ""
    assert cfg.DEFAULTS["cloudflare_hostname"] == ""


def test_every_config_key_has_an_apply_rule():
    # A new DEFAULTS key with no rule fails here: the tray app must be able
    # to say what makes any setting take effect.
    assert set(cfg.APPLY_RULES) == set(cfg.DEFAULTS)
    assert all(kind in cfg.APPLY_KINDS for kind in cfg.APPLY_RULES.values())


def test_known_apply_rules():
    assert cfg.APPLY_RULES["dashboard_port"] == "reinstall"
    assert cfg.APPLY_RULES["boot_preferred_tailnet"] == "windows-installer"
    assert cfg.APPLY_RULES["power_block"] == "live"
    assert cfg.APPLY_RULES["dashboard_poll_seconds"] == "restart-web"
    # Read once into the dashboard's page at startup (web.make_web_handler),
    # so an edit needs a dashboard restart; the watchdog re-reads it per pass.
    assert cfg.APPLY_RULES["zombie_strikes"] == "restart-web"
    # The keep-awake holder is built once; its other keys are re-read per poll.
    assert cfg.APPLY_RULES["power_block_max_hours"] == "restart-awake"
    # Diagnostic thresholds no installer bakes in: the boot_/harden_ prefix
    # alone does not make a key windows-installer.
    assert cfg.APPLY_RULES["boot_headless_window_seconds"] == "live"
    assert cfg.APPLY_RULES["harden_restart_lookback_days"] == "live"
    # An import-time injectable prior: crr reads no Config for it.
    assert cfg.APPLY_RULES["reply_tail_lines"] == "fixed"
    # The five dashboard-owned keys: `crr config set` routes them to the
    # dashboard's own stores (settings.json, exclusions.json), which the
    # watchdog, poll and discovery re-read on every pass, not to config.toml.
    assert cfg.APPLY_RULES["remote_control_autokick"] == "live"
    assert cfg.APPLY_RULES["tunnel_provider"] == "live"
    assert cfg.APPLY_RULES["discover_exclude_dirs"] == "live"


@pytest.mark.parametrize("text", ["False", "off", "NO", "0"])
def test_parse_config_value_bools_false(text):
    assert cfg.parse_config_value("rescue_auto_open", text) is False


@pytest.mark.parametrize("text", ["True", "on", "Yes", "1"])
def test_parse_config_value_bools_true(text):
    assert cfg.parse_config_value("rescue_auto_open", text) is True


def test_parse_config_value_bools():
    assert cfg.parse_config_value("remote_control_autokick", "False") is False
    assert cfg.parse_config_value("remote_control_autokick", "off") is False
    assert cfg.parse_config_value("remote_control_autokick", "1") is True


def test_parse_config_value_refuses_a_word_for_a_bool():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("rescue_auto_open", "maybe")


def test_parse_config_value_int_float_str_list():
    assert cfg.parse_config_value("zombie_strikes", "7") == 7
    assert cfg.parse_config_value("context_tight_fraction", "0.5") == 0.5
    assert cfg.parse_config_value("terminal", " wt ") == " wt "
    assert cfg.parse_config_value("host_allowlist_extras", '["a.example", "b"]') == [
        "a.example", "b"]


def test_parse_config_value_refuses_a_float_for_an_int():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("zombie_strikes", "5.0")


def test_parse_config_value_refuses_text_for_an_int_or_float():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("zombie_strikes", "many")
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("context_tight_fraction", "high")
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("context_tight_fraction", "nan")


def test_parse_config_value_refuses_a_list_with_a_number():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("host_allowlist_extras", '["a", 1]')


def test_parse_config_value_refuses_bad_json_and_non_lists():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("host_allowlist_extras", "a, b")
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("host_allowlist_extras", '"a"')


def test_parse_config_value_refuses_an_unknown_key():
    with pytest.raises(cfg.ConfigError):
        cfg.parse_config_value("no_such_key", "1")


# --- effective_payload: the contracted shape `crr config --effective --json` prints

def _payload(config=None, owned=None, degraded=False):
    return cfg.effective_payload(config or cfg.Config(), owned=owned or {}, degraded=degraded)


def test_effective_payload_validates():
    from crr.core import contracts
    config = cfg.Config({"zombie_strikes": 7}, overlay={"power_block": "sleep"})
    payload = _payload(config, owned={"remote_control_autokick": False})
    contracts.validate_config_payload(payload)   # must not raise
    assert payload["contract"] == contracts.CONFIG_CONTRACT_VERSION
    assert payload["defaults_version"] == cfg.CONFIG_DEFAULTS_VERSION
    assert payload["degraded"] is False
    assert set(payload["keys"]) == set(cfg.DEFAULTS)
    assert tuple(payload["keys"]["zombie_strikes"]) == contracts.CONFIG_KEY_FIELDS
    assert list(payload["keys"]) == sorted(cfg.DEFAULTS)


def test_effective_payload_reports_value_default_origin_type_and_apply():
    config = cfg.Config({"zombie_strikes": 7, "terminal": "kitty"},
                        overlay={"power_block": "sleep", "context_tight_fraction": 0.5})
    keys = _payload(config)["keys"]
    assert keys["zombie_strikes"] == {
        "value": 7, "default": 5, "origin": "configured", "type": "int",
        "apply": cfg.APPLY_RULES["zombie_strikes"]}
    assert keys["power_block"] == {
        "value": "sleep", "default": "off", "origin": "override", "type": "str",
        "apply": "live"}
    assert keys["close_grace_seconds"]["origin"] == "default"
    assert keys["close_grace_seconds"]["value"] == keys["close_grace_seconds"]["default"]
    assert keys["context_tight_fraction"]["type"] == "float"
    assert keys["rescue_auto_open"]["type"] == "bool"      # not "int"
    assert keys["host_allowlist_extras"]["type"] == "list"
    assert keys["reply_tail_lines"]["apply"] == "fixed"
    for key, fields in keys.items():
        assert fields["apply"] == cfg.APPLY_RULES[key]


def test_dashboard_overrides_show_as_override():
    config = cfg.Config({"remote_control_autokick": True})
    owned = {"remote_control_autokick": False, "tunnel_provider": "cloudflare"}
    keys = _payload(config, owned=owned)["keys"]
    assert keys["tunnel_provider"]["value"] == "cloudflare"
    assert keys["tunnel_provider"]["origin"] == "override"
    assert keys["tunnel_provider"]["default"] == "tailscale"
    assert keys["remote_control_autokick"]["value"] is False
    assert keys["remote_control_autokick"]["origin"] == "override"
    assert keys["remote_control_autokick"]["default"] is True
    # a dashboard key with nothing stored keeps the config.toml/default answer
    assert keys["cloudflare_hostname"]["origin"] == "default"


def test_effective_payload_does_not_alias_the_defaults():
    keys = _payload()["keys"]
    keys["host_allowlist_extras"]["default"].append("x")
    keys["host_allowlist_extras"]["value"].append("y")
    assert cfg.DEFAULTS["host_allowlist_extras"] == []


def test_effective_payload_refuses_an_owned_value_for_an_unknown_key():
    with pytest.raises(cfg.ConfigError):
        _payload(owned={"no_such_key": 1})


def test_every_list_default_holds_strings():
    # check_value relies on this: a list setting is a list of strings.
    for key, default in cfg.DEFAULTS.items():
        if isinstance(default, list):
            assert all(isinstance(v, str) for v in default), key


def test_check_value_refuses_a_list_with_a_non_string():
    for bad in ([1], ["a", 2], [None], [["a"]], [True]):
        with pytest.raises(cfg.ConfigError, match="host_allowlist_extras"):
            cfg.check_value("host_allowlist_extras", bad)
    with pytest.raises(cfg.ConfigError):
        cfg.Config({"host_allowlist_extras": [1]})
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overlay={"discover_exclude_dirs": [1]})
    cfg.check_value("host_allowlist_extras", ["a", "b"])   # must not raise
    cfg.check_value("host_allowlist_extras", [])
