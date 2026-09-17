"""The `claude auth status --json` adapter — the second auth source.

Why this adapter exists: `crr.core.auth.auth_state` can only classify
`~/.claude/.credentials.json`, and that file does not exist on every
host Claude Code runs on (macOS keeps the tokens in the login Keychain;
`$CLAUDE_CONFIG_DIR` relocates the whole directory; an enterprise
gateway stores nothing locally). On those hosts CRR's expiry detection
was permanently blind, which is the bug this adapter closes.

Every test here fakes the subprocess. Nothing runs real `claude`.
"""

from __future__ import annotations

import json
import subprocess

from crr.adapters import claude_auth
from crr.core.ports import AuthStatus, AuthStatusSource


class _FakeRun:
    """Stand-in for subprocess.run recording the argv it was handed."""

    def __init__(self, *, stdout="", returncode=0, raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs = kwargs
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def _source(monkeypatch, fake):
    monkeypatch.setattr(claude_auth.subprocess, "run", fake)
    return claude_auth.ClaudeAuthStatus(timeout=3)


class TestPortConformance:
    def test_satisfies_the_port(self):
        assert isinstance(claude_auth.ClaudeAuthStatus(timeout=3), AuthStatusSource)


class TestLoggedIn:
    def test_logged_in_true(self, monkeypatch):
        fake = _FakeRun(stdout=json.dumps({"loggedIn": True, "authMethod": "oauth_token"}))
        status = _source(monkeypatch, fake).status()
        assert status.logged_in is True
        assert "oauth_token" in status.detail

    def test_logged_in_false(self, monkeypatch):
        fake = _FakeRun(stdout=json.dumps({"loggedIn": False}))
        status = _source(monkeypatch, fake).status()
        assert status.logged_in is False

    def test_invokes_claude_auth_status_json(self, monkeypatch):
        fake = _FakeRun(stdout=json.dumps({"loggedIn": True}))
        _source(monkeypatch, fake).status()
        assert fake.calls == [["claude", "auth", "status", "--json"]]
        # The probe sits on the dashboard poll path — an unbounded wait
        # would hang every card behind a wedged node process.
        assert fake.kwargs["timeout"] == 3

    def test_honours_a_configured_binary(self, monkeypatch):
        fake = _FakeRun(stdout=json.dumps({"loggedIn": True}))
        monkeypatch.setattr(claude_auth.subprocess, "run", fake)
        claude_auth.ClaudeAuthStatus(timeout=3, claude_bin="/opt/node/bin/claude").status()
        assert fake.calls[0][0] == "/opt/node/bin/claude"


class TestDegradesToUnknown:
    """Tri-state per the port (F16): None is "could not tell", and must
    never be collapsed into either confident answer. A None here leaves
    `resolve_auth_state` on whatever the credentials file said."""

    def test_missing_binary(self, monkeypatch):
        status = _source(monkeypatch, _FakeRun(raises=FileNotFoundError())).status()
        assert status.logged_in is None
        assert status.detail

    def test_timeout(self, monkeypatch):
        fake = _FakeRun(raises=subprocess.TimeoutExpired(["claude"], 3))
        status = _source(monkeypatch, fake).status()
        assert status.logged_in is None
        assert "timed out" in status.detail

    def test_nonzero_exit(self, monkeypatch):
        # A `claude` too old to have `auth status` exits nonzero. That is
        # not evidence of being logged out.
        fake = _FakeRun(stdout="", returncode=1)
        status = _source(monkeypatch, fake).status()
        assert status.logged_in is None

    def test_unparseable_output(self, monkeypatch):
        status = _source(monkeypatch, _FakeRun(stdout="not json at all")).status()
        assert status.logged_in is None

    def test_json_without_the_key(self, monkeypatch):
        status = _source(monkeypatch, _FakeRun(stdout=json.dumps({"authMethod": "x"}))).status()
        assert status.logged_in is None

    def test_non_bool_logged_in(self, monkeypatch):
        # A string "true" is not a bool. Coercing it would invent an answer.
        status = _source(monkeypatch, _FakeRun(stdout=json.dumps({"loggedIn": "true"}))).status()
        assert status.logged_in is None

    def test_json_that_is_not_an_object(self, monkeypatch):
        status = _source(monkeypatch, _FakeRun(stdout=json.dumps([1, 2, 3]))).status()
        assert status.logged_in is None

    def test_never_raises_on_an_unexpected_error(self, monkeypatch):
        status = _source(monkeypatch, _FakeRun(raises=RuntimeError("boom"))).status()
        assert status.logged_in is None


class TestAvailable:
    def test_available_when_the_binary_resolves(self, monkeypatch):
        monkeypatch.setattr(claude_auth.shutil, "which", lambda name: "/usr/bin/claude")
        assert claude_auth.ClaudeAuthStatus(timeout=3).available() is True

    def test_unavailable_when_it_does_not(self, monkeypatch):
        monkeypatch.setattr(claude_auth.shutil, "which", lambda name: None)
        assert claude_auth.ClaudeAuthStatus(timeout=3).available() is False


class TestAuthStatusShape:
    def test_named_tuple_fields(self):
        status = AuthStatus(logged_in=None, detail="nope")
        assert status.logged_in is None
        assert status.detail == "nope"
