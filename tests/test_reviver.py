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
from crr.core.reviver import attach_argv, revive_crashed, session_name

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
        self._live = set(live)
        self.created = []  # list of (name, cwd, argv)

    def list_sessions(self):
        return set(self._live)

    def new_detached_session(self, name, cwd, argv):
        self.created.append((name, cwd, list(argv)))
        self._live.add(name)


def _run(entries_store, tmux, max_strikes=3, archive=None):
    scan = entries_store.scan()
    return revive_crashed(
        scan.entries, FakeBoot(), FakeProbe(), tmux, entries_store,
        archive if archive is not None else ArchiveStore(entries_store._state_dir),
        max_strikes=max_strikes, now=_NOW,
    )


def test_attach_argv_is_word_form_tmux_attach():
    # A visible tab attaches to the detached session by name — word-form,
    # never a shell string (the name is crr-<8hex>, metacharacter-free).
    assert attach_argv("crr-8a1b2c3d") == ["tmux", "attach", "-t", "crr-8a1b2c3d"]


def test_crashed_claude_session_is_revived(tmp_path):
    store = JournalStore(tmp_path)
    _seed(store, 42, claude=_claude())
    tmux = FakeTmux()
    outcome = _run(store, tmux)

    assert outcome.revived == [42]
    name = session_name({"claude": _claude()})
    assert tmux.created == [(name, "/home/u/p42", ["claude", "--resume", _claude()["session_id"]])]
    entry = store.read(42)
    assert entry["tmux_session"] == name
    assert entry["revive_strikes"] == 1


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
