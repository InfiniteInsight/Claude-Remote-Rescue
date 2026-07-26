"""Host predicate tests (crr.adapters.host)."""

from crr.adapters import host


def test_is_wsl_reads_proc_version(tmp_path):
    micro = tmp_path / "version_wsl"
    micro.write_text("Linux version 5.15.0-microsoft-standard-WSL2 ...", encoding="utf-8")
    assert host.is_wsl(str(micro)) is True

    native = tmp_path / "version_native"
    native.write_text("Linux version 6.8.0-generic ...", encoding="utf-8")
    assert host.is_wsl(str(native)) is False

    assert host.is_wsl(str(tmp_path / "missing")) is False  # absent -> not WSL
