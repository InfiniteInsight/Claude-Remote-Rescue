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


def test_unset_reports_whether_anything_was_removed(tmp_path):
    store = ConfigOverridesStore(tmp_path)
    assert store.unset("zombie_strikes") is False
    store.set("zombie_strikes", 7)
    store.set("dashboard_port", 9000)
    assert store.unset("zombie_strikes") is True
    assert store.read() == {"dashboard_port": 9000}
    assert store.unset("zombie_strikes") is False
