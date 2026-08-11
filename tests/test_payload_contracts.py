"""Versioned contracts for the five lazy API payloads (#36 — run-3 P7).

/api/sessions and /api/diagnostics have carried a `contract` version, a
validator, and a canonical key list since early on. The five endpoints
added later — discoverable, untracked, recall, exclusions, settings — had
none, so any consumer of them silently depended on whatever shape they
happened to have that day. AGENTS.md calls an unversioned shape change
"the exact laundering the audit flagged".
"""

import pathlib

import pytest

from conftest import set_home  # tests/ is on sys.path (no __init__.py)
from crr.core import contracts


def _rows_payload(version, rows=None):
    return {"contract": version, "rows": rows if rows is not None else [],
            "total": 0, "filtered": 0, "offset": 0, "limit": 20}


def _disc_row():
    return {"session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "sid8": "8a1b2c3d",
            "cwd": "/home/u/p", "cwd_source": "verified", "last_active": "",
            "transcript_bytes": 0, "last_prompt": "", "mtime": 0.0, "running": False}


def _untracked_row():
    return {"session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "sid8": "8a1b2c3d",
            "cwd": "/home/u/p", "archived_at": "", "last_prompt": ""}


def test_every_lazy_payload_has_a_version_constant():
    for name in ("UNTRACKED_CONTRACT_VERSION", "RECALL_CONTRACT_VERSION",
                 "EXCLUSIONS_CONTRACT_VERSION", "SETTINGS_CONTRACT_VERSION"):
        assert getattr(contracts, name) == 1, name
    # discoverable moved to v2 when #34 added `cwd_source` to its rows.
    assert contracts.DISCOVERABLE_CONTRACT_VERSION == 2


def test_discoverable_payload_roundtrips():
    contracts.validate_discoverable_payload(
        _rows_payload(contracts.DISCOVERABLE_CONTRACT_VERSION, [_disc_row()]))


def test_untracked_payload_roundtrips():
    contracts.validate_untracked_payload(
        _rows_payload(contracts.UNTRACKED_CONTRACT_VERSION, [_untracked_row()]))


def test_recall_payload_roundtrips():
    contracts.validate_recall_payload({
        "contract": contracts.RECALL_CONTRACT_VERSION,
        "matches": [{"session_id": "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                     "role": "user", "text": "x", "index": 0, "timestamp": ""}],
        "scanned": 1, "skipped": 0,
    })


def test_exclusions_payload_roundtrips():
    contracts.validate_exclusions_payload({
        "contract": contracts.EXCLUSIONS_CONTRACT_VERSION,
        "configured": [".claude-mem"], "managed": [],
        "config_path": "/x/config.toml", "config_from_file": False,
    })


def test_settings_payload_roundtrips():
    contracts.validate_settings_payload({
        "contract": contracts.SETTINGS_CONTRACT_VERSION,
        "autokick": None, "resolved": True, "config_default": True, "degraded": False,
    })


@pytest.mark.parametrize("validator,payload", [
    ("validate_discoverable_payload", _rows_payload(99)),
    ("validate_untracked_payload", _rows_payload(99)),
    ("validate_recall_payload", {"contract": 99, "matches": [], "scanned": 0, "skipped": 0}),
])
def test_wrong_contract_version_is_rejected(validator, payload):
    with pytest.raises(contracts.ContractError):
        getattr(contracts, validator)(payload)


def test_a_dropped_row_field_is_caught():
    # The regression this exists to catch: a field silently disappearing
    # from one payload while every consumer still expects it.
    row = _disc_row()
    del row["running"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_discoverable_payload(
            _rows_payload(contracts.DISCOVERABLE_CONTRACT_VERSION, [row]))


def test_an_extra_row_field_is_caught():
    row = _disc_row(); row["surprise"] = 1
    with pytest.raises(contracts.ContractError):
        contracts.validate_discoverable_payload(
            _rows_payload(contracts.DISCOVERABLE_CONTRACT_VERSION, [row]))


# --- the endpoints actually serve what they now promise --------------------
# The validators above prove the SHAPES are declared. These prove the live
# providers emit them — a contract nothing is checked against is decoration.

def _seed_transcript(home, sid):
    """A real (tiny) transcript on disk, so the paged endpoints return a
    NON-EMPTY `rows` list.

    Without this the providers return `rows: []` and the row validator never
    executes — the test would pass while proving only that the envelope is
    right. Caught by mutation: deleting a row field from discovery.untracked
    did not fail this test until rows were seeded.
    """
    import json as _json
    d = home / ".claude" / "projects" / "-home-u-proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        _json.dumps({"type": "user", "cwd": "/home/u/proj",
                     "timestamp": "2026-01-01T00:00:00Z",
                     "message": {"role": "user", "content": "hello"}}) + "\n",
        encoding="utf-8",
    )


def test_live_endpoints_satisfy_their_contracts(tmp_path, monkeypatch):
    import json

    from crr import cli
    from crr.core import web

    home = tmp_path / "home"
    home.mkdir()
    sid = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    _seed_transcript(home, sid)
    set_home(monkeypatch, str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    handler_holder = {}

    real_make = cli.make_web_handler

    def capture(provider, allowed, suffixes, **kw):
        handler_holder.update(kw)
        return real_make(provider, allowed, suffixes, **kw)

    class _NoServer:
        def __init__(self, *a, **k): pass
        def serve_forever(self): raise KeyboardInterrupt   # return immediately
        def server_close(self): pass

    monkeypatch.setattr(cli, "make_web_handler", capture)
    monkeypatch.setattr(cli, "ThreadingHTTPServer", _NoServer)
    cli.main(["web", "--port", "0"])

    checks = [
        ("discoverable_provider", ("", 0, 20), contracts.validate_discoverable_payload),
        ("untracked_provider", ("", 0, 20), contracts.validate_untracked_payload),
        ("exclusions_provider", (), contracts.validate_exclusions_payload),
        ("settings_provider", (), contracts.validate_settings_payload),
        ("recall_provider", ("zzz-no-such-term-zzz", None), contracts.validate_recall_payload),
    ]
    saw_rows = False
    for name, args, validate in checks:
        provider = handler_holder.get(name)
        assert provider is not None, f"{name} was not wired into the handler"
        payload = provider(*args)
        if payload.get("rows"):
            saw_rows = True
        # Round-trips through JSON exactly as the server serialises it, so a
        # non-serialisable value fails here rather than at request time.
        validate(json.loads(json.dumps(payload)))

    # Guard the guard: if no endpoint returned a row, the row validators
    # above never ran and this test proved far less than it appears to.
    assert saw_rows, "no endpoint returned rows — the row contracts went unchecked"
