"""Dashboard-managed autokick toggles (Slice 2 — the settings store).

Two knobs, one store, same discipline as `exclusions.py`: atomic write,
degrade-to-default read, bounded validation. `autokick_for` is the pure
resolution helper implementing the spec's two-level truth table exactly —
global OFF is a hard switch nothing can override; per-session values are
retained (not discarded) while global is off, so flipping it back on
restores them.
"""

import pytest

from crr.core import settings

_SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_SID2 = "11112222-3333-4444-5555-666677778888"


# --------------------------------------------------------------------------
# autokick_for — the four truth-table rows (spec 2026-08-07).
# --------------------------------------------------------------------------

def test_global_resolved_false_wins_regardless_of_session_true():
    # global override False -> hard switch, per-session ignored.
    assert settings.autokick_for(
        config_default=True, global_override=False, session_override=True
    ) is False


def test_global_off_via_config_default_wins_too():
    # global_override unset (None) falls back to config_default; that
    # default being False is just as much a hard switch as an explicit one.
    assert settings.autokick_for(
        config_default=False, global_override=None, session_override=True
    ) is False


def test_global_true_session_unset_is_true():
    assert settings.autokick_for(
        config_default=True, global_override=None, session_override=None
    ) is True


def test_global_true_session_false_is_false():
    assert settings.autokick_for(
        config_default=True, global_override=None, session_override=False
    ) is False


def test_global_true_session_true_is_true():
    assert settings.autokick_for(
        config_default=True, global_override=True, session_override=True
    ) is True


def test_global_override_true_beats_a_false_config_default():
    # global_override, when set, always wins over config_default.
    assert settings.autokick_for(
        config_default=False, global_override=True, session_override=None
    ) is True


# --------------------------------------------------------------------------
# SettingsStore — round-trip, degrade-to-default, bounds.
# --------------------------------------------------------------------------

def test_store_absent_file_is_no_overrides(tmp_path):
    store = settings.SettingsStore(tmp_path)
    assert store.read_global_autokick() is None
    assert store.read_session_overrides() == {}


def test_store_global_autokick_roundtrips(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_global_autokick(False)
    assert store.read_global_autokick() is False
    assert settings.SettingsStore(tmp_path).read_global_autokick() is False
    store.write_global_autokick(True)
    assert store.read_global_autokick() is True


def test_store_global_autokick_can_be_cleared_back_to_unset(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_global_autokick(False)
    store.write_global_autokick(None)
    assert store.read_global_autokick() is None


def test_store_session_override_roundtrips(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_session_autokick(_SID, False)
    assert store.read_session_overrides() == {_SID: False}
    assert store.read_session_autokick(_SID) is False
    assert store.read_session_autokick(_SID2) is None  # untouched sid -> unset


def test_store_keyed_by_session_id_rejects_a_pid_shaped_key(tmp_path):
    # A pid ("12345") is NOT a session id — keying by pid would let a
    # recycled pid silently inherit an unrelated session's opt-out.
    store = settings.SettingsStore(tmp_path)
    with pytest.raises(settings.SettingsError):
        store.write_session_autokick("12345", True)


def test_store_session_overrides_survive_a_global_off_on_cycle(tmp_path):
    store = settings.SettingsStore(tmp_path)
    store.write_session_autokick(_SID, True)
    store.write_global_autokick(False)
    assert store.read_session_autokick(_SID) is True  # retained, not discarded
    store.write_global_autokick(True)
    assert store.read_session_autokick(_SID) is True


def test_store_missing_file_degrades_to_no_overrides(tmp_path):
    assert settings.SettingsStore(tmp_path / "nope").read_global_autokick() is None
    assert settings.SettingsStore(tmp_path / "nope").read_session_overrides() == {}


def test_store_corrupt_file_degrades_to_no_overrides(tmp_path):
    (tmp_path / settings.FILENAME).write_text("{not json", encoding="utf-8")
    store = settings.SettingsStore(tmp_path)
    assert store.read_global_autokick() is None
    assert store.read_session_overrides() == {}


def test_store_corrupt_sessions_shape_degrades_to_empty(tmp_path):
    (tmp_path / settings.FILENAME).write_text(
        '{"autokick": true, "sessions": "not-a-mapping"}', encoding="utf-8"
    )
    store = settings.SettingsStore(tmp_path)
    assert store.read_global_autokick() is True  # the sessions corruption is isolated
    assert store.read_session_overrides() == {}


def test_store_bad_type_in_sessions_map_degrades_to_empty(tmp_path):
    (tmp_path / settings.FILENAME).write_text(
        '{"sessions": {"%s": "not-a-bool"}}' % _SID, encoding="utf-8"
    )
    store = settings.SettingsStore(tmp_path)
    assert store.read_session_overrides() == {}


def test_store_enforces_max_session_entries(tmp_path):
    store = settings.SettingsStore(tmp_path)
    # Fill up to the bound via the raw file (bypassing the per-call bound
    # check) to prove read() degrades an over-bound file rather than raising.
    huge = {
        f"{i:08x}-0000-4000-8000-000000000000": True
        for i in range(settings.MAX_SESSION_ENTRIES + 1)
    }
    import json
    (tmp_path / settings.FILENAME).write_text(
        json.dumps({"sessions": huge}), encoding="utf-8"
    )
    assert store.read_session_overrides() == {}


def test_store_write_session_autokick_refuses_past_the_bound(tmp_path):
    store = settings.SettingsStore(tmp_path)
    for i in range(settings.MAX_SESSION_ENTRIES):
        store.write_session_autokick(f"{i:08x}-0000-4000-8000-000000000000", True)
    with pytest.raises(settings.SettingsError):
        store.write_session_autokick(_SID, True)
