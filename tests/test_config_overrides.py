"""The crr-owned config overlay (``config_overrides.json``).

``config.toml`` is the user's hand-owned file and crr never writes it (see
``crr/core/exclusions.py`` for why). Settings changed through crr land in
this overlay instead, layered over the TOML and under nothing but the
defaults: overlay beats config.toml beats default.
"""

import json

import pytest

from crr.core import config as cfg
from crr.core import config_overrides
from crr.core.config_overrides import ConfigOverridesStore


def test_overlay_beats_config_toml_beats_default():
    c = cfg.Config({"zombie_strikes": 4}, overlay={"zombie_strikes": 7})
    assert c.get("zombie_strikes") == 7
    assert c.effective()["zombie_strikes"] == (7, "override")


def test_toml_beats_default_and_untouched_keys_stay_default():
    c = cfg.Config({"zombie_strikes": 4}, overlay={"dashboard_port": 9000})
    assert c.get("zombie_strikes") == 4
    assert c.effective()["zombie_strikes"] == (4, "configured")
    assert c.effective()["dashboard_port"] == (9000, "override")
    assert c.effective()["interop_timeout_seconds"][1] == "default"


def test_config_rejects_a_bad_overlay_with_the_same_rule_as_overrides():
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overlay={"nope": 1})
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overlay={"zombie_strikes": "7"})
    with pytest.raises(cfg.ConfigError):
        cfg.Config(overlay={"zombie_strikes": True})


def test_store_round_trips(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    store.set("zombie_strikes", 7)
    assert store.read() == {"zombie_strikes": 7}
    on_disk = json.loads((tmp_path / config_overrides.FILENAME).read_text("utf-8"))
    assert on_disk == {"v": 1, "values": {"zombie_strikes": 7}}
    assert store.is_degraded() is False


def test_missing_file_reads_empty_and_is_not_degraded(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    assert store.read() == {}
    assert store.is_degraded() is False


def test_set_refuses_unknown_or_mistyped_or_owned_keys(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    for key, value in (
        ("nope", 1),
        ("zombie_strikes", "7"),
        ("zombie_strikes", True),
        ("tunnel_provider", "tailscale"),
    ):
        with pytest.raises(cfg.ConfigError):
            store.set(key, value)
    assert not (tmp_path / config_overrides.FILENAME).exists()


def test_owned_elsewhere_names_the_dashboard_stores_keys():
    assert config_overrides.OWNED_ELSEWHERE == frozenset({
        "remote_control_autokick", "tunnel_provider", "cloudflare_tunnel_name",
        "cloudflare_hostname", "discover_exclude_dirs",
    })
    assert config_overrides.OWNED_ELSEWHERE <= set(cfg.DEFAULTS)


def test_read_drops_bad_values_one_at_a_time(tmp_path):
    (tmp_path / config_overrides.FILENAME).write_text(json.dumps({
        "v": 1,
        "values": {"zombie_strikes": "x", "dashboard_port": 9000, "nope": 1,
                   "tunnel_provider": "cloudflare"},
    }), encoding="utf-8")
    store = ConfigOverridesStore(tmp_path)
    assert store.read() == {"dashboard_port": 9000}
    assert store.is_degraded() is False


def test_read_drops_a_value_that_is_not_finite(tmp_path):
    # Python's json reads a hand-written NaN or Infinity; neither may reach
    # the strict-JSON payloads crr prints.
    (tmp_path / config_overrides.FILENAME).write_text(
        '{"v": 1, "values": {"context_tight_fraction": NaN, '
        '"takeover_idle_seconds": Infinity, "dashboard_port": 9000}}',
        encoding="utf-8")
    store = ConfigOverridesStore(tmp_path)
    assert store.read() == {"dashboard_port": 9000}
    assert store.is_degraded() is False


def test_corrupt_overrides_degrade_to_toml_and_defaults(tmp_path):
    (tmp_path / config_overrides.FILENAME).write_text("{not json", encoding="utf-8")
    store = ConfigOverridesStore(tmp_path)
    assert store.read() == {}
    assert store.is_degraded() is True


def test_unreadable_shapes_and_future_versions_are_degraded(tmp_path):
    path = tmp_path / config_overrides.FILENAME
    store = ConfigOverridesStore(tmp_path)
    for body in ('["a"]', '{"v": 1, "values": [1]}', '{"v": 99, "values": {}}'):
        path.write_text(body, encoding="utf-8")
        assert store.read() == {}, body
        assert store.is_degraded() is True, body


def test_set_keeps_other_values(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    store.set("zombie_strikes", 7)
    store.set("dashboard_port", 9000)
    store.set("zombie_strikes", 8)
    assert store.read() == {"zombie_strikes": 8, "dashboard_port": 9000}


def test_set_and_unset_keep_values_this_build_does_not_read(tmp_path):
    # A newer crr may have written keys this one doesn't know, and a hand edit
    # may hold a value this one refuses. One writer must never discard another's
    # state (the settings store's _carry_* rule), so writes go through the raw
    # file; only the key being written is validated, and read() still filters.
    path = tmp_path / config_overrides.FILENAME
    path.write_text(json.dumps({"v": 1, "values": {
        "key_from_a_newer_crr": 3, "zombie_strikes": 4, "rescue_auto_open": "maybe"}}),
        encoding="utf-8")
    store = ConfigOverridesStore(tmp_path)
    store.set("dashboard_port", 9000)
    assert json.loads(path.read_text())["values"] == {
        "key_from_a_newer_crr": 3, "zombie_strikes": 4, "rescue_auto_open": "maybe",
        "dashboard_port": 9000}
    assert store.unset("zombie_strikes") is True
    assert json.loads(path.read_text())["values"] == {
        "key_from_a_newer_crr": 3, "rescue_auto_open": "maybe", "dashboard_port": 9000}
    assert store.read() == {"dashboard_port": 9000}
    with pytest.raises(cfg.ConfigError):
        store.set("zombie_strikes", "x")        # the key being written is still checked


def test_unset_reports_whether_anything_was_removed(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    assert store.unset("zombie_strikes") is False
    store.set("zombie_strikes", 7)
    store.set("dashboard_port", 9000)
    assert store.unset("zombie_strikes") is True
    assert store.read() == {"dashboard_port": 9000}
    assert store.unset("zombie_strikes") is False


@pytest.mark.parametrize("body", [
    "{not json",
    '{"v": 99, "values": {"zombie_strikes": 7}}',
    '{"v": 1, "values": [1]}',
])
def test_set_refuses_to_overwrite_a_degraded_overlay(tmp_path, body):
    # Values a newer crr or a hand edit put there must never be destroyed.
    path = tmp_path / config_overrides.FILENAME
    path.write_text(body, encoding="utf-8")
    before = path.read_bytes()
    store = ConfigOverridesStore(tmp_path)
    with pytest.raises(cfg.ConfigError, match=config_overrides.FILENAME):
        store.set("dashboard_port", 9000)
    with pytest.raises(cfg.ConfigError, match=config_overrides.FILENAME):
        store.unset("zombie_strikes")
    assert path.read_bytes() == before
    # Reads still degrade, so commands keep working.
    assert store.read() == {}
