"""Versioned contracts for the dashboard-managed stores (#36 — run-3 P7).

AGENTS.md: "Contract shapes are versioned. Change a stored/served shape ->
bump its version constant." Three JSON files in the state dir had none:
exclusions.json, settings.json, bridge_kicks.json.

These are the more urgent half of #36. A served payload breaks visibly
against a page of a known version; a STORED file is read back by whatever
crr is installed later — possibly an older one, after a rollback — and a
shape it half-understands is worse than one it refuses.

The rule these tests pin, for all three:

  write   always stamps the current version
  read    accepts an unstamped file as legacy v1 (every file already on
          disk predates this change and is otherwise valid)
  read    REFUSES a version from the future — degraded, never a partial
          read of a shape this build does not know
"""

import json

import pytest

from crr.core import bridge_kicks, contracts, exclusions, settings


def _write_raw(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- version constants exist and are declared centrally -------------------

def test_store_version_constants_are_declared_in_contracts():
    assert contracts.EXCLUSIONS_STORE_VERSION == 1
    assert contracts.SETTINGS_STORE_VERSION == 1
    assert contracts.KICKS_STORE_VERSION == 1


# --- exclusions.json ------------------------------------------------------

def test_exclusions_write_stamps_the_version(tmp_path):
    store = exclusions.ExclusionStore(tmp_path)
    store.write([".claude-mem"])
    raw = json.loads((tmp_path / exclusions.FILENAME).read_text())
    assert raw["v"] == contracts.EXCLUSIONS_STORE_VERSION
    assert raw["dirs"] == [".claude-mem"]


def test_exclusions_reads_an_unstamped_legacy_file(tmp_path):
    # Every exclusions.json already on disk looks exactly like this.
    _write_raw(tmp_path / exclusions.FILENAME, {"dirs": [".claude-mem"]})
    assert exclusions.ExclusionStore(tmp_path).read() == [".claude-mem"]


def test_exclusions_refuses_a_future_version(tmp_path):
    _write_raw(tmp_path / exclusions.FILENAME, {"v": 99, "dirs": [".claude-mem"]})
    # Not a silent partial read: this build cannot know what v99 means.
    assert exclusions.ExclusionStore(tmp_path).read() == []


# --- settings.json --------------------------------------------------------

def test_settings_write_stamps_the_version(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_global_autokick(False)
    raw = json.loads((tmp_path / settings.FILENAME).read_text())
    assert raw["v"] == contracts.SETTINGS_STORE_VERSION
    assert raw["autokick"] is False


def test_settings_reads_an_unstamped_legacy_file(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_raw(tmp_path / settings.FILENAME, {"autokick": False, "sessions": {sid: True}})
    store = settings.SettingsStore(tmp_path)
    assert store.is_degraded() is False
    assert store.read_global_autokick() is False
    assert store.read_session_autokick(sid) is True


def test_settings_future_version_is_degraded_so_the_watchdog_fails_closed(tmp_path):
    # This is the one that matters most: a settings file this build cannot
    # understand must NOT read as "no overrides", because that silently
    # drops every per-session opt-out and re-arms auto-kick for a session
    # the user excluded — against a code path that SIGTERMs live processes.
    _write_raw(tmp_path / settings.FILENAME, {"v": 99, "autokick": True, "sessions": {}})
    store = settings.SettingsStore(tmp_path)
    assert store.is_degraded() is True
    assert store.effective_global_autokick() is False


def test_settings_version_survives_a_session_write(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = settings.SettingsStore(tmp_path)
    store.write_global_autokick(True)
    store.write_session_autokick(sid, False)
    raw = json.loads((tmp_path / settings.FILENAME).read_text())
    assert raw["v"] == contracts.SETTINGS_STORE_VERSION
    assert raw["autokick"] is True and raw["sessions"][sid] is False


# --- bridge_kicks.json ----------------------------------------------------

def test_kicks_write_stamps_the_version(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    store = bridge_kicks.KickHistoryStore(tmp_path)
    store.record_kick(sid, 1000.0)
    raw = json.loads((tmp_path / bridge_kicks.FILENAME).read_text())
    assert raw["v"] == contracts.KICKS_STORE_VERSION
    assert raw["sessions"][sid]["attempts"] == 1


def test_kicks_reads_an_unstamped_legacy_file(tmp_path):
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _write_raw(tmp_path / bridge_kicks.FILENAME,
               {"sessions": {sid: {"attempts": 2, "last_kick_ts": 500.0}}})
    store = bridge_kicks.KickHistoryStore(tmp_path)
    assert store.is_degraded() is False
    assert store.attempts(sid) == 2
    assert store.last_kick_ts(sid) == 500.0


def test_kicks_future_version_is_degraded_so_the_cooldown_is_not_erased(tmp_path):
    # A kick-history file this build cannot read must fail CLOSED: reading
    # it as "no history" erases the cooldown and attempt cap, which is the
    # restart-loop protection itself.
    _write_raw(tmp_path / bridge_kicks.FILENAME, {"v": 99, "sessions": {}})
    assert bridge_kicks.KickHistoryStore(tmp_path).is_degraded() is True


@pytest.mark.parametrize("bad", [{"v": "1"}, {"v": 1.5}, {"v": True}, {"v": None}])
def test_stores_reject_a_non_integer_version(tmp_path, bad):
    payload = dict(bad); payload["sessions"] = {}
    _write_raw(tmp_path / settings.FILENAME, payload)
    assert settings.SettingsStore(tmp_path).is_degraded() is True
