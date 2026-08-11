"""cwd provenance for discovered sessions (#34 — audit run-3 P3).

`cli._discoverable_rows` did:

    cwd = transcript_source.read_cwd(sid) or t["cwd"]

which merged two values of very different standing into one string:

  read_cwd(...)  AUTHORITATIVE — the cwd Claude Code stamped on the
                 session's own records.
  t["cwd"]       the LOSSY project-dir decode. `_decode_project_dir_name`'s
                 own docstring: a literal `-` in a path component is
                 indistinguishable from an encoded `/`, so
                 `Claude-Remote-Rescue` decodes to
                 `/home/u/Claude/Remote/Rescue`. It calls itself "a
                 DISPLAY/fallback value, not authoritative" and warns that
                 "passing it to a real filesystem `cwd=` spawn" is what the
                 lossiness breaks.

That merged value reached `build_adopted_entry` -> the journal ->
`tmux.new_detached_session(name, entry["cwd"], ...)`, where a wrong
directory does not display wrong — it fails to revive.

AGENTS.md already declares the pattern for this class: `sid_source`
(injected|guessed|verified) exists so a guessed session id never presents
as truth. This is the same idea for cwd.
"""

import json
import os
import pathlib

import pytest

from crr import cli
from crr.core import contracts
from crr.core.journal import JournalStore


def _write_transcript(home, sid, project, records):
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _turn(cwd=None):
    r = {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hi"}}
    if cwd is not None:
        r["cwd"] = cwd
    return r


SID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_cwd_source_is_a_contracted_row_field():
    assert "cwd_source" in contracts.DISCOVERABLE_ROW_KEYS
    assert contracts.CWD_SOURCES == ("verified", "decoded")


def test_a_transcript_that_carries_its_own_cwd_is_verified(tmp_path, monkeypatch):
    real = tmp_path / "Claude-Remote-Rescue"
    real.mkdir()
    # The project dir name decodes LOSSILY to /home/u/Claude/Remote/Rescue;
    # the transcript's own records carry the truth.
    _write_transcript(tmp_path, SID, "-home-u-Claude-Remote-Rescue", [_turn(str(real))])
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    rows, _problems = cli._discoverable_rows(JournalStore(tmp_path))
    row = next(r for r in rows if r["session_id"] == SID)
    assert row["cwd"] == str(real)
    assert row["cwd_source"] == "verified"


def test_a_transcript_with_no_cwd_falls_back_and_says_so(tmp_path, monkeypatch):
    # No `cwd` on any record, so read_cwd comes up empty and the lossy
    # decode is all there is. It must be LABELLED, not passed off as fact.
    _write_transcript(tmp_path, SID, "-home-u-Claude-Remote-Rescue", [_turn()])
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    rows, _problems = cli._discoverable_rows(JournalStore(tmp_path))
    row = next(r for r in rows if r["session_id"] == SID)
    assert row["cwd"] == "/home/u/Claude/Remote/Rescue"
    assert row["cwd_source"] == "decoded"


# --- the invariant: a decoded cwd that isn't a real directory never
#     enters the journal, so nothing downstream can spawn into it --------

def test_adopt_refuses_a_decoded_cwd_that_is_not_a_real_directory(tmp_path, monkeypatch):
    _write_transcript(tmp_path, SID, "-home-u-Claude-Remote-Rescue", [_turn()])
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    store = JournalStore(tmp_path)
    ok, msg = cli._adopt(store, tmp_path, SID)
    assert ok is False
    assert "cwd" in msg.lower()
    # Nothing was written — the refusal is what keeps the journal clean.
    assert store.scan().entries == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="needs a real directory whose path round-trips through the "
           "'/'<->'-' project-dir codec; no Windows path does (drive "
           "letters and backslashes survive neither direction), so there "
           "is no decoded-cwd-that-exists for the accept case to use",
)
def test_adopt_accepts_a_decoded_cwd_that_does_exist(tmp_path, monkeypatch):
    # The project dir must decode to a path that really exists, which means
    # a path with NO hyphens in it. `/tmp` qualifies; pytest's own tmp_path
    # does not — its `pytest-of-<user>` / `test_..._0` components are full
    # of hyphens, so the decode mangles them. That is not a test artifact,
    # it is the bug: an ordinary directory name defeats the round-trip.
    _write_transcript(tmp_path, SID, "-tmp", [_turn()])
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    ok, msg = cli._adopt(JournalStore(tmp_path), tmp_path, SID)
    assert ok is True, msg


def test_adopt_accepts_a_verified_cwd_even_if_the_directory_is_gone(tmp_path, monkeypatch):
    # A verified cwd came from the session's own records. The directory may
    # legitimately have been deleted since, and that is not this guard's
    # business — the guard exists to catch a cwd we GUESSED, not one that
    # was observed and later removed.
    _write_transcript(tmp_path, SID, "-home-u-proj", [_turn("/gone/for/good")])
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    ok, msg = cli._adopt(JournalStore(tmp_path), tmp_path, SID)
    assert ok is True, msg
