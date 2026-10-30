"""Host predicate tests (crr.adapters.host)."""

from types import SimpleNamespace

import pytest

from crr.adapters import host


def test_is_wsl_reads_proc_version(tmp_path):
    micro = tmp_path / "version_wsl"
    micro.write_text("Linux version 5.15.0-microsoft-standard-WSL2 ...", encoding="utf-8")
    assert host.is_wsl(str(micro)) is True

    native = tmp_path / "version_native"
    native.write_text("Linux version 6.8.0-generic ...", encoding="utf-8")
    assert host.is_wsl(str(native)) is False

    assert host.is_wsl(str(tmp_path / "missing")) is False  # absent -> not WSL


# --- login shell (#152) ------------------------------------------------------
#
# An adopted session has no registration to read a shell from, so Un-tmux
# reopens it in the user's login shell, the one that carries the shim.


def _passwd_shell(monkeypatch, shell):
    monkeypatch.setattr(host.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_shell=shell))


@pytest.mark.parametrize("path, expected", [
    ("/usr/bin/fish", "fish"),
    ("/bin/zsh", "zsh"),
    ("/bin/bash", "bash"),
])
def test_login_shell_comes_from_the_passwd_entry(monkeypatch, path, expected):
    _passwd_shell(monkeypatch, path)
    assert host.login_shell() == expected


def test_login_shell_ignores_a_shell_variable_that_disagrees(monkeypatch):
    # $SHELL describes whatever started crr, which need not be the user.
    _passwd_shell(monkeypatch, "/usr/bin/fish")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert host.login_shell() == "fish"


@pytest.mark.parametrize("path", ["/usr/sbin/nologin", "/bin/dash", ""])
def test_login_shell_falls_back_to_bash_for_a_shell_the_journal_cannot_hold(monkeypatch, path):
    _passwd_shell(monkeypatch, path)
    assert host.login_shell() == "bash"


def test_login_shell_falls_back_to_bash_without_a_passwd_entry(monkeypatch):
    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(host.pwd, "getpwuid", missing)
    assert host.login_shell() == "bash"


# --- distro name at call time (#54) ---------------------------------------
#
# crr systemd bakes WSL_DISTRO_NAME into the unit because a service inherits
# no such variable. Baked values go stale silently: rename the distro and the
# tab spawner targets a distro that no longer exists. `wslpath -w /` reports
# the CURRENT registered name and needs no interop, so it survives a rename.

def test_distro_name_is_parsed_from_wslpath():
    assert host.distro_name_from_wslpath("\\\\wsl.localhost\\Ubuntu-24.04\\") == "Ubuntu-24.04"


def test_distro_name_handles_the_older_wsl_dollar_form():
    assert host.distro_name_from_wslpath("\\\\wsl$\\Ubuntu-22.04\\") == "Ubuntu-22.04"


def test_distro_name_returns_none_for_anything_unrecognised():
    for junk in ("", "C:\\Users\\Infin", "/home/evan", "\\\\wsl.localhost\\", "garbage"):
        assert host.distro_name_from_wslpath(junk) is None


def test_distro_name_prefers_wslpath_then_env_then_nothing(monkeypatch):
    # wslpath wins: it reflects a rename the baked env var would not.
    monkeypatch.setattr(host, "_wslpath_root", lambda timeout=None: "\\\\wsl.localhost\\Renamed\\")
    assert host.distro_name({"WSL_DISTRO_NAME": "Stale-Name"}) == "Renamed"
    # wslpath unavailable (missing binary, odd output) -> fall back to the env.
    monkeypatch.setattr(host, "_wslpath_root", lambda timeout=None: None)
    assert host.distro_name({"WSL_DISTRO_NAME": "Baked-Name"}) == "Baked-Name"
    # Neither -> None, and the caller omits --distribution as it does today.
    assert host.distro_name({}) is None
