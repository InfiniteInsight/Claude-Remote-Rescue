from pathlib import Path

from crr import install_shims


def test_crr_bin_path_prefers_env_override(monkeypatch):
    monkeypatch.setenv("CRR_BIN", "/opt/wherever/crr")
    assert install_shims.crr_bin_path() == "/opt/wherever/crr"


def test_crr_bin_path_falls_back_to_path_lookup_when_argv0_unresolvable(
    monkeypatch, tmp_path
):
    """`python -c ...` leaves sys.argv[0] == "-c", which isn't a real
    file; crr_bin_path must not blindly resolve that against cwd."""
    monkeypatch.delenv("CRR_BIN", raising=False)
    monkeypatch.setattr(install_shims.sys, "argv", ["-c"])
    monkeypatch.setattr(install_shims.shutil, "which", lambda name: None)
    real_crr = tmp_path / "crr"
    real_crr.write_text("#!/bin/sh\n")

    def fake_which(name):
        return str(real_crr) if name == "crr" else None

    monkeypatch.setattr(install_shims.shutil, "which", fake_which)
    assert install_shims.crr_bin_path() == str(real_crr.resolve())


def test_install_writes_shim_with_placeholder_rewritten(tmp_path, monkeypatch, crr_state):
    monkeypatch.setenv("CRR_BIN", "/abs/path/to/crr")
    home = tmp_path / "home"
    home.mkdir()

    report = install_shims.install(["bash"], home=home)
    assert report["bash"]["error"] is None
    assert report["bash"]["copied"] is True

    dest = Path(report["bash"]["shim_path"])
    assert dest.exists()
    text = dest.read_text()
    assert "__CRR_BIN__" not in text
    assert "/abs/path/to/crr" in text
    # Executable bit set.
    assert dest.stat().st_mode & 0o111


def test_install_appends_guarded_rc_block(tmp_path, monkeypatch, crr_state):
    monkeypatch.setenv("CRR_BIN", "/abs/path/to/crr")
    home = tmp_path / "home"
    home.mkdir()

    report = install_shims.install(["bash"], home=home)
    rc_path = Path(report["bash"]["rc_path"])
    assert rc_path == home / ".bashrc"
    assert rc_path.exists()
    text = rc_path.read_text()
    assert install_shims._MARK_BEGIN in text
    assert install_shims._MARK_END in text
    assert report["bash"]["rc_updated"] is True


def test_install_is_idempotent(tmp_path, monkeypatch, crr_state):
    monkeypatch.setenv("CRR_BIN", "/abs/path/to/crr")
    home = tmp_path / "home"
    home.mkdir()

    install_shims.install(["bash"], home=home)
    rc_path = home / ".bashrc"
    first_text = rc_path.read_text()

    report2 = install_shims.install(["bash"], home=home)
    second_text = rc_path.read_text()

    assert first_text == second_text  # no duplicate block
    assert report2["bash"]["rc_updated"] is False  # already wired


def test_install_reports_error_for_unknown_shell_source(tmp_path, monkeypatch, crr_state):
    # Force the source dir lookup to somewhere with no shim files.
    monkeypatch.setattr(install_shims, "shims_source_dir", lambda: tmp_path / "nope")
    monkeypatch.setenv("CRR_BIN", "/abs/path/to/crr")
    home = tmp_path / "home"
    home.mkdir()

    report = install_shims.install(["zsh"], home=home)
    assert report["zsh"]["error"] is not None
    assert report["zsh"]["copied"] is False


def test_uninstall_removes_source_line_only(tmp_path, monkeypatch, crr_state):
    monkeypatch.setenv("CRR_BIN", "/abs/path/to/crr")
    home = tmp_path / "home"
    home.mkdir()

    install_shims.install(["bash"], home=home)
    rc_path = home / ".bashrc"
    assert install_shims._MARK_BEGIN in rc_path.read_text()

    report = install_shims.uninstall(["bash"], home=home)
    assert report["bash"]["rc_cleaned"] is True
    text = rc_path.read_text()
    assert install_shims._MARK_BEGIN not in text
    assert install_shims._MARK_END not in text

    # The installed shim file itself is left in place.
    shim_dir = install_shims.shims_install_dir()
    assert (shim_dir / "crr.bash").exists()


def test_uninstall_on_untouched_rc_is_a_noop(tmp_path, crr_state):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# user's own stuff\n")

    report = install_shims.uninstall(["bash"], home=home)
    assert report["bash"]["rc_cleaned"] is False
    assert (home / ".bashrc").read_text() == "# user's own stuff\n"


def test_uninstall_missing_rc_file_is_fine(tmp_path, crr_state):
    home = tmp_path / "home"
    home.mkdir()
    report = install_shims.uninstall(["fish"], home=home)
    assert report["fish"]["rc_cleaned"] is False
    assert report["fish"]["error"] is None


def test_shims_install_dir_under_state_dir(monkeypatch, crr_state):
    assert install_shims.shims_install_dir() == crr_state / "shims"


def test_detected_shells_only_lists_present_binaries(monkeypatch):
    monkeypatch.setattr(
        install_shims.shutil, "which", lambda name: "/usr/bin/%s" % name if name == "bash" else None
    )
    assert install_shims.detected_shells() == ["bash"]
