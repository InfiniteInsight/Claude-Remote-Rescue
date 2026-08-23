"""Reviver tests — revive crashed claude sessions into detached tmux.

Pure core driven by fakes (a fake tmux spawner, fake boot/probe, a real
JournalStore on a tmpdir). The behaviors that matter:

- Revival is gated on a LIVE session check (list_sessions), not on the
  persisted tmux_session field — so a reboot (fresh tmux server, name
  gone) re-revives instead of being blocked by a stale flag.
- The give-up guard is the safety valve for that gate: a session that
  keeps dying accrues strikes and is abandoned past the limit; a session
  observed alive resets to zero, so only *persistent* failures count.
- The revival command is word-form argv ([lesson: word-form exec]).
"""

import pytest

from crr.core.archive import ArchiveStore
from crr.core.journal import JournalStore, new_entry
from crr.core.reviver import (
    attach_argv,
    resolved_session_name,
    revival_argv,
    revive_crashed,
    session_name,
)

_ENTRY_BOOT = "entry-boot-0000"
_NOW = "2026-07-24T00:00:00Z"


def _claude(sid="8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"):
    return {"session_id": sid, "sid_source": "injected", "started": _NOW}


def _seed(store, pid, *, boot=_ENTRY_BOOT, claude=None, tmux_session=None, strikes=0):
    store.write(new_entry(
        pid=pid, cwd=f"/home/u/p{pid}", host="tmux", shell="zsh",
        boot_id=boot, now=_NOW, claude=claude, tmux_session=tmux_session,
        revive_strikes=strikes,
    ))


class FakeBoot:
    # current() never equals _ENTRY_BOOT, so seeded entries classify crashed.
    def current(self):
        return "current-boot-9999"


class FakeProbe:
    def is_alive(self, pid):
        return True

    def has_controlling_tty(self, pid):
        return True


class FakeTmux:
    def __init__(self, live=()):
        # live=None means "liveness is unknown" (F16 tri-state) — distinct
        # from live=() (genuinely no live sessions).
        self._live = None if live is None else set(live)
        self.created = []  # list of (name, cwd, argv)

    def list_sessions(self):
        return None if self._live is None else set(self._live)

    def new_detached_session(self, name, cwd, argv):
        if self._live is None:
            raise AssertionError("must not spawn while tmux liveness is unknown")
        self.created.append((name, cwd, list(argv)))
        self._live.add(name)

    def session_pid(self, name):
        # Unknown unless a test says otherwise (see _PidTmux) — matching the
        # adapter's "never guess a pid to re-key onto" contract.
        return None


def _run(entries_store, tmux, max_strikes=3, archive=None, remote_control_enabled=True):
    scan = entries_store.scan()
    return revive_crashed(
        scan.entries, FakeBoot(), FakeProbe(), tmux, entries_store,
        archive if archive is not None else ArchiveStore(entries_store._state_dir),
        max_strikes=max_strikes, now=_NOW,
        remote_control_enabled=remote_control_enabled,
    )


def test_attach_argv_is_word_form_tmux_attach():
    # A visible tab attaches to the detached session by name — word-form,
    # never a shell string (the name is crr-<8hex>, metacharacter-free).
    assert attach_argv("crr-8a1b2c3d") == ["tmux", "attach", "-t", "crr-8a1b2c3d"]


# --- Remote Control on revival (a revived session must stay phone-reachable) --

from crr.core.reviver import remote_control_flag_argv, remote_control_name


def test_remote_control_name_sanitizes_the_cwd_basename():
    # Spaces, parens, and punctuation collapse to single dashes; the token
    # is otherwise letters/digits/dash/underscore only.
    assert remote_control_name("/home/u/my project (v2)!") == "my-project-v2"


def test_remote_control_name_keeps_safe_characters_untouched():
    assert remote_control_name("/home/u/My_Project-2") == "My_Project-2"


def test_remote_control_name_falls_back_to_crr_for_empty_or_odd_cwd():
    assert remote_control_name("") == "crr"
    assert remote_control_name("/") == "crr"
    assert remote_control_name("///") == "crr"
    assert remote_control_name("/home/u/!!!") == "crr"


def test_remote_control_name_is_capped_at_forty_chars():
    long_dir = "x" * 500
    name = remote_control_name(f"/home/u/{long_dir}")
    assert len(name) <= 40


def test_remote_control_flag_argv_always_carries_an_explicit_name():
    # THE HAZARD: --remote-control takes an OPTIONAL value, so anything
    # bare risks swallowing whatever follows as the session name. An
    # explicit name is unambiguous regardless of what follows it.
    argv = remote_control_flag_argv("/home/u/proj")
    assert argv == ["--remote-control", "proj"]
    assert argv[1] and not argv[1].startswith("-")


def test_revival_argv_includes_remote_control_with_a_derived_name():
    entry = {"claude": _claude(), "cwd": "/home/u/my-proj"}
    argv = revival_argv(entry, remote_control=True)
    assert argv == ["claude", "--resume", _claude()["session_id"], "--remote-control", "my-proj"]


def test_revival_argv_omits_remote_control_when_disabled():
    entry = {"claude": _claude(), "cwd": "/home/u/my-proj"}
    argv = revival_argv(entry, remote_control=False)
    assert argv == ["claude", "--resume", _claude()["session_id"]]


def test_revival_argv_places_remote_control_last_so_nothing_follows_the_name():
    # Nothing can be swallowed as the name when the name is the final argv
    # element — this holds regardless of the sanitizer's output.
    entry = {"claude": _claude(), "cwd": "/home/u/my-proj"}
    argv = revival_argv(entry, remote_control=True)
    assert argv[-2] == "--remote-control"
    assert argv[-1] == "my-proj"


def test_crashed_claude_session_is_revived(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude())
    tmux = FakeTmux()
    outcome = _run(store, tmux)

    assert outcome.revived == [42]
    name = session_name({"claude": _claude()})
    assert tmux.created == [(
        name, "/home/u/p42",
        ["claude", "--resume", _claude()["session_id"], "--remote-control", "p42"],
    )]
    entry = store.read(42)
    assert entry["tmux_session"] == name
    assert entry["revive_strikes"] == 1


def test_revive_crashed_omits_remote_control_when_disabled(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude())
    tmux = FakeTmux()
    _run(store, tmux, remote_control_enabled=False)

    assert tmux.created == [(
        session_name({"claude": _claude()}), "/home/u/p42",
        ["claude", "--resume", _claude()["session_id"]],
    )]


def test_non_crashed_entries_are_left_alone(tmp_path):
    # Same-boot entry classifies live (probe alive+tty) -> not a candidate.
    store = JournalStore(tmp_path)
    store.write(new_entry(
        pid=7, cwd="/x", host="tmux", shell="zsh",
        boot_id="current-boot-9999", now=_NOW, claude=_claude(),
    ))
    tmux = FakeTmux()
    outcome = _run(store, tmux)
    assert outcome.revived == []
    assert tmux.created == []


def test_claude_less_crashed_shell_is_not_revived(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=None)  # crashed shell, nothing to resume
    tmux = FakeTmux()
    outcome = _run(store, tmux)
    assert outcome.revived == []
    assert tmux.created == []


def test_live_session_resets_strikes_and_does_not_respawn(tmp_path):
    store = JournalStore(tmp_path)
    name = session_name({"claude": _claude()})
    _seed(store, 42, claude=_claude(), tmux_session=name, strikes=2)
    tmux = FakeTmux(live={name})  # revival is up and running
    outcome = _run(store, tmux)

    assert outcome.revived == []
    assert outcome.reset == [42]
    assert tmux.created == []
    assert store.read(42)["revive_strikes"] == 0  # success clears the strikes


def test_reboot_gone_session_is_re_revived_despite_persisted_field(tmp_path):
    # The reboot case: tmux_session persisted but the server (and its
    # sessions) died. Gate on live sessions, so this re-revives.
    store = JournalStore(tmp_path)
    name = session_name({"claude": _claude()})
    _seed(store, 42, claude=_claude(), tmux_session=name, strikes=0)
    tmux = FakeTmux(live=set())  # no live sessions after reboot
    outcome = _run(store, tmux)
    assert outcome.revived == [42]
    assert store.read(42)["revive_strikes"] == 1


def test_persistent_failure_gives_up_to_the_archive(tmp_path):
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    _seed(store, 42, claude=_claude(), strikes=3)  # already at max
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, max_strikes=3, archive=archive)

    assert outcome.revived == []
    assert outcome.gave_up == [42]
    assert tmux.created == []
    # Give-up is terminal: dropped from active, preserved in the archive so
    # it stops re-reporting but isn't lost.
    with pytest.raises(KeyError):
        store.read(42)
    rec = archive.read(_claude()["session_id"])
    assert rec["reason"] == "gave-up"


def test_one_below_limit_still_revives(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude(), strikes=2)
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, max_strikes=3)
    assert outcome.revived == [42]
    assert store.read(42)["revive_strikes"] == 3


# --- reviving from the archive (reboot-recovery after pid reuse) ----------

def _archived_entry(store_dir, pid=99, strikes=0):
    # An entry preserved in the archive (as register-safety would do).
    entry = new_entry(
        pid=pid, cwd=f"/home/u/p{pid}", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(), revive_strikes=strikes,
    )
    archive = ArchiveStore(store_dir)
    archive.archive(entry, "superseded-on-register", _NOW)
    return archive


def test_archived_session_with_no_live_tmux_is_revived(tmp_path):
    store = JournalStore(tmp_path)
    archive = _archived_entry(tmp_path)
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, archive=archive)

    assert outcome.revived == [99]
    name = session_name({"claude": _claude()})
    assert tmux.created and tmux.created[0][0] == name
    rec = archive.read(_claude()["session_id"])
    assert rec["entry"]["revive_strikes"] == 1  # strike tracked in the archive


def test_archived_session_gives_up_in_place_past_limit(tmp_path):
    store = JournalStore(tmp_path)
    archive = _archived_entry(tmp_path, strikes=3)
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, max_strikes=3, archive=archive)

    assert outcome.gave_up == [99]
    assert tmux.created == []
    assert archive.read(_claude()["session_id"])["reason"] == "gave-up"  # terminal


def test_duplicate_sids_spawn_one_session_not_two(tmp_path):
    # Two entries journal the same sid (the documented 2026-07-21 case).
    # They collapse to one crr-<sid8> name; the reviver must spawn it once,
    # not try to create an already-created session (which real tmux rejects
    # and would abort the whole pass).
    store = JournalStore(tmp_path)
    _seed(store, 1, claude=_claude())
    _seed(store, 2, claude=_claude())  # same sid
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux)

    assert len(tmux.created) == 1  # spawned once, not twice
    assert 1 in outcome.revived
    assert 2 in outcome.reset  # second sees the just-spawned session as live


def test_gave_up_archive_record_is_not_re_revived(tmp_path):
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(),
    )
    archive.archive(entry, "gave-up", _NOW)  # already terminal
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == []
    assert tmux.created == []


def test_detmuxed_archive_record_is_not_re_revived(tmp_path):
    # A detmuxed record is also terminal: the user took manual ownership by
    # attaching a tab. Once that tab closes (claude exits, the tmux session
    # ends), the name drops out of `live` — without this skip, the archive
    # loop's fallthrough to 'revive' would spawn a fresh detached session
    # and `claude --resume` the very conversation the user just closed,
    # exactly the resurrection ops.detmux's delist is meant to prevent.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(), tmux_session="crr-8a1b2c3d",
    )
    archive.archive(entry, "detmuxed", _NOW)  # already terminal
    tmux = FakeTmux(live=set())  # the attached tab's tmux session is gone
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == []
    assert tmux.created == []


def test_untracked_archive_record_is_not_re_revived(tmp_path):
    # Terminology change: detmux -> untrack; ops.detmux now archives with
    # reason "untracked" rather than "detmuxed". Same terminal skip as
    # test_detmuxed_archive_record_is_not_re_revived above, exercised
    # against the current (not the deprecated) reason spelling.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(), tmux_session="crr-8a1b2c3d",
    )
    archive.archive(entry, "untracked", _NOW)  # already terminal
    tmux = FakeTmux(live=set())  # the attached tab's tmux session is gone
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == []
    assert tmux.created == []


def test_untmuxed_archive_record_is_not_re_revived(tmp_path):
    # [user request 2026-07-31] An untmuxed record is also terminal: the
    # user took manual ownership of a bare `claude --resume` in a visible
    # tab, with no crr-managed wrapper left behind. Without this skip, the
    # archive loop's fallthrough to 'revive' would spawn a fresh detached
    # tmux session for a conversation the user deliberately took out of
    # tmux — exactly the resurrection ops.untmux's delist is meant to
    # prevent, and it would make its "crr no longer manages it" message a
    # lie.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(), tmux_session="crr-8a1b2c3d",
    )
    archive.archive(entry, "untmuxed", _NOW)  # already terminal
    tmux = FakeTmux(live=set())  # the killed tmux session is gone
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == []
    assert tmux.created == []


def test_ghost_restored_archive_records_are_revival_candidates(tmp_path):
    # [user request 2026-07-30] ops.reopen's GHOST branch preserves a ghost
    # card's conversation to the archive as "ghost-restored" *before*
    # attempting the spawn — a spawn failure there must not strand the
    # conversation: the watchdog's next revive pass has to pick it up. The
    # skip tuple stays (gave-up, detmuxed, untracked, untmuxed, dismissed);
    # ghost-restored is not in it.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(), tmux_session="crr-8a1b2c3d",
    )
    archive.archive(entry, "ghost-restored", _NOW)
    tmux = FakeTmux(live=set())  # the tmux spawn never landed
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == [99]
    assert tmux.created and tmux.created[0][0] == "crr-8a1b2c3d"
    assert archive.read(_claude()["session_id"])["reason"] == "ghost-restored"


def test_shell_exited_archive_record_is_revived(tmp_path):
    # #99: shell-exited means the shell died (SIGHUP) but claude may still be
    # alive or resumable — deregister now archives instead of hard-deleting.
    # The reviver must treat it as a revival candidate, not a terminal reason.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(),
    )
    archive.archive(entry, "shell-exited", _NOW)
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == [99]
    assert tmux.created and tmux.created[0][0] == session_name({"claude": _claude()})


def test_dismissed_archive_record_is_not_re_revived(tmp_path):
    # [bug 2026-07-29] ops.dismiss archives with reason "dismissed" — the
    # user's explicit "clean up without restoring". Without this skip, the
    # archive loop's fallthrough to 'revive' would resurrect the very
    # conversation the user just dismissed, un-doing their choice.
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(),
    )
    archive.archive(entry, "dismissed", _NOW)  # already terminal
    tmux = FakeTmux(live=set())
    outcome = _run(store, tmux, archive=archive)
    assert outcome.revived == []
    assert tmux.created == []


# --- F16: tri-state tmux liveness — never act on "can't tell" ------------

def test_revive_skips_the_entire_pass_when_tmux_liveness_is_unknown(tmp_path):
    # spine (null-result expressibility): an unconfirmed "not live" must
    # never accumulate a strike or trigger a give-up archive against a
    # session that may in fact still be running. A transient tmux query
    # failure must be indistinguishable in effect from "revive wasn't
    # called this pass", not from "confirmed nothing to do".
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude())  # would ordinarily be revived
    outcome = _run(store, FakeTmux(live=None))
    assert outcome == ([], [], [], True)
    assert outcome.skipped is True
    entry = store.read(42)
    assert entry["revive_strikes"] == 0          # no strike accrued
    assert entry["tmux_session"] is None          # untouched


def test_revive_skips_archived_candidates_too_when_tmux_liveness_is_unknown(tmp_path):
    store = JournalStore(tmp_path)
    archive = ArchiveStore(tmp_path)
    entry = new_entry(
        pid=99, cwd="/x", host="tmux", shell="zsh",
        boot_id=_ENTRY_BOOT, now=_NOW, claude=_claude(),
    )
    archive.archive(entry, "superseded-on-register", _NOW)  # a revival candidate
    outcome = _run(store, FakeTmux(live=None), archive=archive)
    assert outcome == ([], [], [], True)
    assert outcome.skipped is True
    assert archive.read(_claude()["session_id"])["reason"] == "superseded-on-register"  # untouched


# --- session naming: the full sid is the identity (#51) -------------------
#
# `crr-<sid8>` used an 8-char DISPLAY abbreviation as crr's identity for a
# parked conversation. Two sids sharing those 8 chars collided, and Reopen
# then attached the user to the wrong conversation while reporting success.

_SID_A = "8a1b2c3d-1111-4a6b-8c7d-9e0f1a2b3c4d"
_SID_B = "8a1b2c3d-2222-4a6b-8c7d-9e0f1a2b3c4d"


def test_session_name_uses_the_whole_session_id():
    assert session_name({"claude": _claude(_SID_A)}) == f"crr-{_SID_A}"


def test_session_names_differ_for_sids_sharing_eight_characters():
    assert _SID_A[:8] == _SID_B[:8]  # the collision precondition
    assert session_name({"claude": _claude(_SID_A)}) != session_name({"claude": _claude(_SID_B)})


def test_resolved_name_prefers_a_name_already_recorded():
    # Migration: a conversation already parked under a legacy crr-<sid8>
    # must keep answering to that name. Recomputing it would make the
    # reviver think the session is gone and start a SECOND claude --resume
    # on the same conversation (the #48 hazard).
    entry = {"claude": _claude(_SID_A), "tmux_session": "crr-8a1b2c3d"}
    assert resolved_session_name(entry) == "crr-8a1b2c3d"


def test_resolved_name_computes_when_nothing_is_recorded():
    for recorded in (None, ""):
        entry = {"claude": _claude(_SID_A), "tmux_session": recorded}
        assert resolved_session_name(entry) == f"crr-{_SID_A}"


def test_revive_does_not_duplicate_a_legacy_named_session(tmp_path):
    # The live session is parked under the OLD short name. The reviver must
    # recognise it and reset strikes, not spawn a second one.
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude(_SID_A), tmux_session="crr-8a1b2c3d", strikes=2)
    tmux = FakeTmux(live={"crr-8a1b2c3d"})
    outcome = _run(store, tmux)
    assert tmux.created == [], "spawned a duplicate for an already-parked legacy session"
    assert outcome.revived == []
    assert outcome.reset == [42]


# --- revived sessions land in the journal under their live pid (#58) ------
#
# The reviver spawns `tmux new-session -- claude ...`, so the claude runs
# with no shim between it and tmux and never calls `crr register`. Every
# revived conversation was therefore invisible to crr: the only entry stayed
# keyed to the long-dead shell pid, so the card read "crashed" and offered
# no Kick while the conversation was alive and well.

class _PidTmux(FakeTmux):
    """FakeTmux that also reports a pane pid, like the real adapter."""

    def __init__(self, live=(), pane_pid=2016):
        super().__init__(live)
        self._pane_pid = pane_pid

    def session_pid(self, name):
        return self._pane_pid


def test_revival_rekeys_the_entry_onto_the_live_claude_pid(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 1311532, claude=_claude())
    tmux = _PidTmux(pane_pid=2016)
    _run(store, tmux)

    assert store.read(2016), "the live conversation has no card"
    with pytest.raises(KeyError):
        store.read(1311532)  # the dead shell pid is not a second card


def test_rekeyed_entry_carries_the_CURRENT_boot_so_it_classifies_live(tmp_path):
    # classify() short-circuits to CRASHED on a boot mismatch WITHOUT
    # consulting the pid, so keeping the old boot id would leave the card
    # crashed no matter how alive the process is.
    store = JournalStore(tmp_path)
    _seed(store, 1311532, claude=_claude())
    _run(store, _PidTmux(pane_pid=2016))
    assert store.read(2016)["boot_id"] == FakeBoot().current()


def test_an_already_parked_session_is_adopted_on_the_next_pass(tmp_path):
    # The five conversations already parked on the reporting host must not
    # stay invisible until something re-revives them.
    store = JournalStore(tmp_path)
    name = session_name({"claude": _claude()})
    _seed(store, 1311532, claude=_claude(), tmux_session=name)
    tmux = _PidTmux(live={name}, pane_pid=2016)
    _run(store, tmux)

    assert tmux.created == []          # already live: nothing respawned
    assert store.read(2016)
    with pytest.raises(KeyError):
        store.read(1311532)


def test_rekey_refuses_to_clobber_a_slot_owned_by_a_different_session(tmp_path):
    # Same discipline as adopt/retrack: a silent overwrite would destroy
    # another conversation's revival data.
    store = JournalStore(tmp_path)
    _seed(store, 1311532, claude=_claude(_SID_A))
    _seed(store, 2016, claude=_claude(_SID_B))   # the pane pid is taken
    _run(store, _PidTmux(pane_pid=2016))

    assert store.read(2016)["claude"]["session_id"] == _SID_B  # untouched
    assert store.read(1311532)                                  # original kept


def test_rekey_is_skipped_when_the_pane_pid_is_unknown(tmp_path):
    # None means "could not determine" — never guess a pid to re-key onto.
    store = JournalStore(tmp_path)
    _seed(store, 1311532, claude=_claude())
    _run(store, _PidTmux(pane_pid=None))
    assert store.read(1311532)  # left exactly as it was


# --- Close must stick for a parked session (#58) --------------------------
#
# Close arms a close flag that the SHIM's repair loop consumes; the shim then
# deregisters, which is what actually stops the watchdog. A tmux-revived
# claude has no shim, so nothing consumed the flag, the entry stayed, and the
# watchdog revived the very conversation the user just closed — within 30s.

class _Flags:
    def __init__(self, armed=None):
        self.armed = dict(armed or {})
        self.cleared = []

    def read(self, pid):
        return self.armed.get(pid)

    def clear(self, pid):
        self.cleared.append(pid)
        self.armed.pop(pid, None)


_CURRENT_BOOT = "current-boot-9999"  # matches FakeBoot().current()


def test_a_close_flagged_entry_is_not_revived(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    tmux = FakeTmux()
    outcome = revive_crashed(
        store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
        max_strikes=3, now=_NOW, remote_control_enabled=True,
        flags=_Flags({2016: ("close", None, _CURRENT_BOOT)}),
    )
    assert tmux.created == [], "resurrected a conversation the user closed"
    assert outcome.revived == []


def test_a_close_flagged_entry_is_archived_terminally_and_delisted(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    flags = _Flags({2016: ("close", None, _CURRENT_BOOT)})
    revive_crashed(store.scan().entries, FakeBoot(), FakeProbe(), FakeTmux(), store,
                   archive, max_strikes=3, now=_NOW, remote_control_enabled=True,
                   flags=flags)
    with pytest.raises(KeyError):
        store.read(2016)                       # gone from the active journal
    assert archive.read(_claude()["session_id"])["reason"] == "closed"
    assert flags.cleared == [2016]             # and the flag does not linger


def test_stale_boot_close_flag_is_ignored_not_honored(tmp_path):
    """A close flag from a previous boot must not archive a recycled-pid session."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    flags = _Flags({2016: ("close", None, "old-boot-dead")})
    tmux = FakeTmux()
    outcome = revive_crashed(
        store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
        max_strikes=3, now=_NOW, remote_control_enabled=True,
        flags=flags,
    )
    assert 2016 not in outcome.gave_up, "stale flag must not trigger give-up"
    assert flags.cleared == [2016], "stale flag must be cleared"
    assert len(tmux.created) == 1, "session should be revived normally"


def test_close_flag_with_no_boot_id_is_honored(tmp_path):
    """Legacy flags (no boot_id) are honored to avoid breaking existing state."""
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    flags = _Flags({2016: ("close", None, None)})
    revive_crashed(
        store.scan().entries, FakeBoot(), FakeProbe(), FakeTmux(), store, archive,
        max_strikes=3, now=_NOW, remote_control_enabled=True,
        flags=flags,
    )
    with pytest.raises(KeyError):
        store.read(2016)
    assert archive.read(_claude()["session_id"])["reason"] == "closed"


def test_a_closed_archive_record_is_never_revived(tmp_path):
    # Terminal, like gave-up/dismissed: the archive must not resurrect it.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    archive.archive(store.read(2016), "closed", _NOW)
    store.remove(2016)
    tmux = FakeTmux()
    revive_crashed(store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
                   max_strikes=3, now=_NOW, remote_control_enabled=True)
    assert tmux.created == []


def test_a_relaunch_flag_does_not_stop_a_revival(tmp_path):
    # Kick means "bring it back" — only close is terminal.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 2016, claude=_claude())
    tmux = FakeTmux()
    revive_crashed(store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
                   max_strikes=3, now=_NOW, remote_control_enabled=True,
                   flags=_Flags({2016: ("relaunch", _SID_A, _CURRENT_BOOT)}))
    assert len(tmux.created) == 1


# --- a kicked conversation comes back WITH a tab (#62) --------------------
#
# Kick signals and returns; the watchdog creates the replacement ~30s later,
# so the tab can only come from here. The relaunch flag is the signal that
# THIS revival was asked for: the shim consumes it for a shim-managed
# session, but a tmux-parked one has no shim (#58), so it survives to this
# sweep. Crash-driven revivals carry no flag and must stay tabless — the
# reporting host revives 13 conversations at boot.

class _Tab:
    def __init__(self, fail=False):
        self.opened = []
        self._fail = fail

    def available(self):
        return True

    def open_tab(self, argv, cwd=None):
        if self._fail:
            raise RuntimeError("wt boom")
        self.opened.append(list(argv))


def test_a_kicked_session_is_revived_with_a_tab(tmp_path):
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 899149, claude=_claude())
    tab = _Tab()
    flags = _Flags({899149: ("relaunch", _claude()["session_id"], _CURRENT_BOOT)})
    revive_crashed(store.scan().entries, FakeBoot(), FakeProbe(), FakeTmux(), store,
                   archive, max_strikes=3, now=_NOW, remote_control_enabled=True,
                   flags=flags, tab_spawner=tab)
    assert tab.opened, "kicked session came back with nothing pointing at it"
    assert tab.opened[0][-1] == session_name({"claude": _claude()})
    assert flags.cleared == [899149], "the flag must not re-open a tab next sweep"


def test_a_crash_driven_revival_opens_no_tab(tmp_path):
    # 13 conversations revive at boot on the reporting host. None asked for.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 42, claude=_claude())
    tab = _Tab()
    revive_crashed(store.scan().entries, FakeBoot(), FakeProbe(), FakeTmux(), store,
                   archive, max_strikes=3, now=_NOW, remote_control_enabled=True,
                   flags=_Flags(), tab_spawner=tab)
    assert tab.opened == []


def test_a_failed_tab_never_costs_the_revival(tmp_path):
    # The revival is durable by the time the tab is attempted; a spawner
    # failure is convenience lost, never the conversation.
    store, archive = JournalStore(tmp_path), ArchiveStore(tmp_path)
    _seed(store, 899149, claude=_claude())
    tmux = FakeTmux()
    outcome = revive_crashed(
        store.scan().entries, FakeBoot(), FakeProbe(), tmux, store, archive,
        max_strikes=3, now=_NOW, remote_control_enabled=True,
        flags=_Flags({899149: ("relaunch", _claude()["session_id"], _CURRENT_BOOT)}),
        tab_spawner=_Tab(fail=True),
    )
    assert outcome.revived == [899149]
    assert len(tmux.created) == 1
