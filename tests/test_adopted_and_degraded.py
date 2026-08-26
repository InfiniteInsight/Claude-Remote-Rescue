"""The two needs-review findings from audit run 3 (#40).

1. P4 — Quarantined fakery. `discovery.build_adopted_entry` writes
   host="tab" and shell="bash" into the journal for a session where neither
   was ever observed ("any enum member the schema accepts would be equally
   fabricated"). Those fields turn out to be DISPLAY-ONLY — nothing in crr
   makes a decision on them, they are copied straight onto the card. So the
   fabrication's only escape route is the dashboard, and that is where it
   gets closed: an adopted card reports what was actually observed.

   Deliberately NOT done: widening the journal schema to allow None. That
   means JOURNAL_SCHEMA_VERSION 1 -> 2 and migrating live journal files, for
   two fields nothing reads. Fixing the claim where the claim is made is the
   smaller true change.

2. P3 — Confidence + provenance. A degraded settings store maps to
   `effective_global_autokick() -> False`, so the card rendered
   "global-off". Honest about BEHAVIOUR (nothing is being kicked) but the
   REASON shown is wrong — the user never turned the global switch off.
"""

import pytest

from crr.core import contracts, settings
from crr.core.discovery import ADOPTED_BOOT_ID, build_adopted_entry
from crr.core.status import assemble_sessions

SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class FakeBoot:
    def current(self): return "boot-1"


class FakeProbe:
    def is_alive(self, pid): return True
    def has_controlling_tty(self, pid): return True
    def controlling_ttys(self, pids): return set(pids)


# --- 1. adopted entries stop presenting fabricated fields as fact ---------

def test_the_journal_entry_still_validates_unchanged():
    # The placeholders stay in the journal: they satisfy a v1 schema that
    # nothing reads them through. This pins that we did NOT migrate.
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    contracts.validate_journal_entry(entry)
    assert entry["host"] == "tab" and entry["shell"] == "bash"
    assert entry["boot_id"] == ADOPTED_BOOT_ID


def test_an_adopted_card_is_marked_as_adopted():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    assert card["adopted"] is True


def test_an_adopted_card_does_not_claim_a_host_or_shell():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    # Adoption never observed a shell registration; the journal's "tab"/"bash"
    # are schema filler, and the card must not repeat them as fact.
    assert card["host"] == ""
    assert card["shell"] == ""


def test_a_normally_registered_card_still_reports_its_real_host_and_shell():
    from crr.core.journal import new_entry
    entry = new_entry(pid=42, cwd="/home/u/p", host="tmux", shell="fish",
                      boot_id="boot-1", now="2026-01-01T00:00:00+00:00",
                      tmux_session="crr-abc",
                      claude={"session_id": SID, "sid_source": "injected",
                              "started": "2026-01-01T00:00:00+00:00",
                              "skip_permissions": False})
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    assert card["adopted"] is False
    assert card["host"] == "tmux" and card["shell"] == "fish"


def test_adopted_is_a_contracted_card_field():
    assert "adopted" in contracts.SESSION_CARD_KEYS


# --- 2. a degraded settings store says WHY, not just "off" ----------------

def test_degraded_is_a_distinct_autokick_state():
    assert "degraded" in contracts.AUTOKICK_STATES


def test_a_degraded_store_renders_degraded_not_global_off(tmp_path):
    (tmp_path / settings.FILENAME).write_text("{not json", encoding="utf-8")
    store = settings.SettingsStore(tmp_path)
    assert store.is_degraded() is True
    state = settings.autokick_card_state(
        config_default=True, global_override=store.effective_global_autokick(),
        session_override=None, degraded=True)
    assert state == "degraded"


def test_a_user_turned_off_switch_still_says_global_off():
    # The two must not be confused in either direction.
    state = settings.autokick_card_state(
        config_default=True, global_override=False, session_override=None,
        degraded=False)
    assert state == "global-off"


def test_degraded_still_means_nothing_is_kicked():
    # The BEHAVIOUR was already right and must not regress: whatever the
    # card says, a degraded store kicks nothing.
    assert settings.autokick_for(
        config_default=True, global_override=False, session_override=True) is False


@pytest.mark.parametrize("degraded,expected", [(True, "degraded"), (False, "on")])
def test_degraded_overrides_every_other_state(degraded, expected):
    assert settings.autokick_card_state(
        config_default=True, global_override=None, session_override=None,
        degraded=degraded) == expected


# --- the gap that let this ship broken -----------------------------------
# The tests above call assemble_sessions and inspect the dict. The live
# server VALIDATES the payload, and `shell: ""` failed the SHELLS enum —
# so /api/sessions 500'd on a real machine while every unit test passed.
# Validating the assembled payload is the check that was missing.

def test_an_adopted_payload_passes_its_own_contract():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    payload = assemble_sessions([entry], FakeBoot(), FakeProbe())
    contracts.validate_sessions_payload(payload)


def test_a_normal_payload_passes_its_own_contract():
    from crr.core.journal import new_entry
    entry = new_entry(pid=42, cwd="/home/u/p", host="tmux", shell="fish",
                      boot_id="boot-1", now="2026-01-01T00:00:00+00:00",
                      tmux_session="crr-abc",
                      claude={"session_id": SID, "sid_source": "injected",
                              "started": "2026-01-01T00:00:00+00:00",
                              "skip_permissions": False})
    contracts.validate_sessions_payload(
        assemble_sessions([entry], FakeBoot(), FakeProbe()))


def test_a_non_adopted_card_may_not_have_an_empty_shell():
    # The conditional must not become a blanket "" allowance: an empty
    # shell on a normally-registered card is still a real error.
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    card["adopted"] = False
    with pytest.raises(contracts.ContractError):
        contracts.validate_session_card(card)


def test_an_adopted_card_may_not_carry_a_fabricated_shell():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    card["shell"] = "bash"
    with pytest.raises(contracts.ContractError):
        contracts.validate_session_card(card)
