"""The two needs-review findings from audit run 3 (#40).

1. P4 — Quarantined fakery. `discovery.build_adopted_entry` writes
   host="tab" and a shell into the journal for a session where neither was
   ever observed. The card must not present those as observations, so an
   adopted card reports what was actually seen.

   The shell is not filler (#152): `ops.tracked_resume_argv` reads it to
   choose the shell Un-tmux reopens in, so it is the user's login shell.

   Deliberately NOT done: widening the journal schema to allow None. That
   means JOURNAL_SCHEMA_VERSION 1 -> 2 and migrating live journal files.
   Fixing the claim where the claim is made is the smaller true change.

2. P3 — Confidence + provenance. A degraded settings store maps to
   `effective_global_autokick() -> False`, so the card rendered
   "global-off". Honest about BEHAVIOUR (nothing is being kicked) but the
   REASON shown is wrong — the user never turned the global switch off.
"""

import pytest

from crr.core import contracts, settings
from crr.core.discovery import ADOPTED_BOOT_ID, build_adopted_entry
from crr.core.journal import new_entry
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
    # The fields stay in the journal: this pins that we did NOT migrate.
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    contracts.validate_journal_entry(entry)
    assert entry["host"] == "tab" and entry["shell"] == "fish"
    assert entry["boot_id"] == ADOPTED_BOOT_ID


def test_an_adopted_card_is_marked_as_adopted():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    assert card["adopted"] is True


def test_an_adopted_card_does_not_claim_a_host_or_shell():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    # Adoption never observed a shell registration, so the card must not
    # repeat the journal's values as fact.
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
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
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
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    card["adopted"] = False
    with pytest.raises(contracts.ContractError):
        contracts.validate_session_card(card)


def test_an_adopted_card_may_not_carry_a_fabricated_shell():
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    card = assemble_sessions([entry], FakeBoot(), FakeProbe())["sessions"][0]
    card["shell"] = "bash"
    with pytest.raises(contracts.ContractError):
        contracts.validate_session_card(card)


# --- running_elsewhere: the card says why the watchdog is leaving it (#159) ------

def _adopted_card(**kwargs):
    entry = build_adopted_entry(SID, "/home/u/p", "2026-01-01T00:00:00+00:00", shell="fish")
    kwargs.setdefault("live_tmux_sessions", set())
    return assemble_sessions([entry], FakeBoot(), FakeProbe(), **kwargs)["sessions"][0]


def test_a_crashed_card_says_its_conversation_is_running_elsewhere():
    assert _adopted_card(running_sids={SID})["running_elsewhere"] is True


def test_a_live_session_is_not_running_elsewhere_from_itself():
    # Every healthy shim session names its own sid on the command line
    # (`claude --session-id <sid>`), so the probe sees all of them.
    entry = new_entry(pid=4242, cwd="/home/u/p", host="tab", shell="fish",
                      boot_id="boot-1", now="2026-01-01T00:00:00+00:00",
                      claude={"session_id": SID, "sid_source": "injected",
                              "started": "2026-01-01T00:00:00+00:00",
                              "skip_permissions": False})
    card = assemble_sessions([entry], FakeBoot(), FakeProbe(), running_sids={SID},
                             live_tmux_sessions=set())["sessions"][0]
    assert card["state"] == "live"
    assert card["running_elsewhere"] is False


def test_a_card_is_not_running_elsewhere_when_nothing_runs_it():
    assert _adopted_card(running_sids=set())["running_elsewhere"] is False


def test_an_unrelated_running_conversation_changes_nothing_on_the_card():
    other = "11111111-2222-4333-8444-555555555555"
    assert _adopted_card(running_sids={other})["running_elsewhere"] is False


def test_no_process_answer_claims_nothing():
    assert _adopted_card()["running_elsewhere"] is False


def test_unknown_tmux_state_claims_nothing():
    # The pane could be the copy `ps` sees, and the reviver skips this pass too.
    assert _adopted_card(running_sids={SID}, live_tmux_sessions=None)["running_elsewhere"] is False


def test_the_card_uses_the_watchdogs_own_tmux_name():
    # An entry with no recorded name whose default-named session is up. The
    # watchdog treats that as its own, so the card has to as well.
    assert _adopted_card(running_sids={SID}, live_tmux_sessions={f"crr-{SID}"})[
        "running_elsewhere"] is False
