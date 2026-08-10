"""Deploy decisions (#61) — which code the services are allowed to run.

The hazard: the watchdog and dashboard ran the development working tree via
an editable install, so unreviewed edits reached real session state within
one timer interval. Pure decisions here; git/pip live in the adapter.
"""

from pathlib import Path

from crr.core import deploy


def test_paths_sit_under_the_state_dir_the_services_already_use():
    sd = Path("/home/u/.local/state/crr")
    assert deploy.app_dir(sd) == sd / "app"
    assert deploy.marker_path(sd) == sd / "app" / "deployed.json"
    assert deploy.deployed_bin(sd) == sd / "app" / "bin" / "crr"


def test_a_clean_tree_deploys():
    assert deploy.refusal(dirty=False, force=False) is None


def test_a_dirty_tree_is_refused():
    msg = deploy.refusal(dirty=True, force=False)
    assert msg and "uncommitted" in msg
    assert "--force" in msg  # the override is discoverable from the refusal


def test_unknown_dirtiness_is_refused_not_assumed_clean():
    # "could not tell" is not "clean" — guessing here puts unreviewed code
    # on live session state, which is the whole hazard.
    msg = deploy.refusal(dirty=None, force=False)
    assert msg and "cannot tell" in msg


def test_force_overrides_every_refusal():
    for dirty in (True, False, None):
        assert deploy.refusal(dirty=dirty, force=True) is None


def test_no_deployed_copy_is_reported_as_running_the_working_tree():
    msg = deploy.drift(None, "abc1234")
    assert msg and "working tree" in msg


def test_matching_shas_are_silent():
    assert deploy.drift("abc1234", "abc1234") is None


def test_an_unknown_head_is_silent_rather_than_alarming():
    # Not a git checkout: nothing to compare against, so claiming drift
    # would be inventing a discrepancy.
    assert deploy.drift("abc1234", None) is None


def test_drift_names_both_shas_and_the_command_that_fixes_it():
    msg = deploy.drift("aaaaaaa1111", "bbbbbbb2222")
    assert "aaaaaaa" in msg and "bbbbbbb" in msg
    assert "crr deploy" in msg


# --- adapter: probes degrade to "unknown", never to a wrong answer --------

def test_is_dirty_ignores_untracked_files(tmp_path, monkeypatch):
    from crr.adapters import deploy as ad
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    assert ad.is_dirty(tmp_path) is False
    # A scratch file beside the source does not change what gets installed.
    assert "--untracked-files=no" in seen["cmd"]


def test_is_dirty_is_none_when_git_cannot_answer(tmp_path, monkeypatch):
    from crr.adapters import deploy as ad

    class Fail:
        returncode, stdout, stderr = 128, "", "not a git repository"

    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: Fail())
    assert ad.is_dirty(tmp_path) is None
    assert ad.head_sha(tmp_path) is None

    def boom(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(ad.subprocess, "run", boom)
    assert ad.is_dirty(tmp_path) is None


def test_marker_roundtrips_and_survives_junk(tmp_path):
    from crr.adapters import deploy as ad
    path = tmp_path / "app" / "deployed.json"
    ad.write_marker(path, "abc1234", "2026-08-10T00:00:00Z")
    assert ad.read_marker(path) == "abc1234"
    path.write_text("{not json", encoding="utf-8")
    assert ad.read_marker(path) is None                 # unreadable != a sha
    assert ad.read_marker(tmp_path / "nope.json") is None


def test_build_installs_non_editable_and_without_deps(tmp_path, monkeypatch):
    from crr.adapters import deploy as ad
    calls = []

    class Ok:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Ok())
    assert ad.build(tmp_path / "app", tmp_path / "repo", "abc") is None
    install = calls[-1]
    assert "--no-deps" in install
    assert "-e" not in install, "an editable install would recreate the hazard"


def test_build_reports_a_failure_instead_of_claiming_success(tmp_path, monkeypatch):
    from crr.adapters import deploy as ad

    class Fail:
        returncode, stdout, stderr = 1, "", "boom"

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Fail())
    assert "venv creation failed" in ad.build(tmp_path / "app", tmp_path / "repo", "abc")


# --- CLI wiring: services must not follow the working tree ----------------

def test_service_units_point_at_the_deployed_copy_when_one_exists(tmp_path, monkeypatch):
    from crr import cli
    from crr.adapters import state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    binary = tmp_path / "app" / "bin" / "crr"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    assert cli._resolve_service_bin(None) == str(binary)


def test_service_units_fall_back_when_nothing_is_deployed(tmp_path, monkeypatch):
    # A fresh checkout with no deploy yet must keep working exactly as before.
    from crr import cli
    from crr.adapters import state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_resolve_crr_bin", lambda x: "/fallback/crr")
    assert cli._resolve_service_bin(None) == "/fallback/crr"


def test_an_explicit_bin_still_wins(tmp_path, monkeypatch):
    from crr import cli
    from crr.adapters import state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    assert cli._resolve_service_bin("/opt/crr/bin/crr") == "/opt/crr/bin/crr"


def test_deploy_refuses_a_dirty_tree_and_builds_nothing(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    built = []
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: True)
    monkeypatch.setattr(ad, "build", lambda *a, **k: built.append(1))
    assert cli.main(["deploy"]) == 2
    assert built == [], "deployed uncommitted code"
    assert "uncommitted" in capsys.readouterr().err


def test_deploy_writes_the_marker_only_after_a_successful_build(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.core import deploy as core
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234def")
    monkeypatch.setattr(ad, "build", lambda *a, **k: "install failed: boom")
    assert cli.main(["deploy"]) == 1
    assert not core.marker_path(tmp_path).exists(), "recorded a deploy that failed"
    assert "install failed" in capsys.readouterr().err


def test_doctor_names_the_code_the_services_are_running(tmp_path, monkeypatch, capsys):
    # "My fix is committed" and "my fix is live" are otherwise
    # indistinguishable — a stale deploy is legitimate, just not invisible.
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "bbbbbbb2222")
    monkeypatch.setattr(ad, "read_marker", lambda path: "aaaaaaa1111")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "aaaaaaa" in out and "bbbbbbb" in out and "crr deploy" in out


def test_doctor_is_silent_when_the_deploy_matches_head(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "same111")
    monkeypatch.setattr(ad, "read_marker", lambda path: "same111")
    cli.main(["doctor"])
    assert "crr deploy" not in capsys.readouterr().out


# --- deploy puts crr on PATH (#61 follow-up) ------------------------------

def test_link_lands_in_the_conventional_user_bin_dir():
    assert deploy.link_path(Path("/home/u")) == Path("/home/u/.local/bin/crr")


def test_replacing_a_symlink_crr_owns_is_fine(tmp_path):
    link = tmp_path / "crr"
    link.symlink_to(tmp_path / "old-target")
    assert deploy.link_refusal(link) is None


def test_a_real_file_there_is_left_alone(tmp_path):
    # Someone else's install — a pip --user script, a hand-written wrapper.
    link = tmp_path / "crr"
    link.write_text("#!/bin/sh\necho not ours\n")
    msg = deploy.link_refusal(link)
    assert msg and "not a symlink" in msg


def test_a_missing_link_is_fine_to_create(tmp_path):
    assert deploy.link_refusal(tmp_path / "nothing-here") is None


def test_a_link_outside_PATH_is_called_out():
    msg = deploy.path_warning("/usr/bin:/bin", Path("/home/u/.local/bin/crr"))
    assert msg and "not on PATH" in msg


def test_a_link_on_PATH_is_silent():
    assert deploy.path_warning("/home/u/.local/bin:/usr/bin",
                               Path("/home/u/.local/bin/crr")) is None


def test_ensure_link_repoints_a_stale_symlink(tmp_path):
    from crr.adapters import deploy as ad
    link, old, new = tmp_path / "crr", tmp_path / "old", tmp_path / "new"
    new.write_text("")
    link.symlink_to(old)
    assert ad.ensure_link(link, new) is None
    assert link.resolve() == new.resolve()


def test_deploy_links_crr_onto_PATH_after_a_successful_build(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    home = tmp_path / "home"
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))
    assert cli.main(["deploy"]) == 0
    assert (home / ".local" / "bin" / "crr").is_symlink()


def test_deploy_does_not_link_when_the_build_failed(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    home = tmp_path / "home"
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: "install failed: boom")
    assert cli.main(["deploy"]) == 1
    assert not (home / ".local" / "bin" / "crr").exists(), "linked a failed deploy"
