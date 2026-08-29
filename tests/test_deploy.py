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


# `deploy.drift()` (a bare sha == sha comparison) was superseded by
# `deploy.deploy_status()` below, which also validates ancestry before
# claiming "behind" — see the deploy_status tests further down.


# --- repo resolution (the fix: deploy can't resolve itself from the ------
# --- deployed, PATH-linked copy — #<issue>) -------------------------------
#
# `crr deploy` re-invoked through the symlink it just created has `__file__`
# in site-packages, not a git checkout, so `_repo_root()` alone leaves it
# with nothing to build from. `resolve_repo` is the precedence pure
# decision; the CLI supplies the "is this path a checkout" answers from
# `crr.adapters.deploy.is_checkout`.

def test_resolve_repo_prefers_an_explicit_flag_that_is_a_checkout():
    got = deploy.resolve_repo(
        explicit="/explicit/repo", explicit_is_checkout=True,
        repo_root=Path("/repo/root"), repo_root_is_checkout=True,
        marker_repo="/marker/repo", marker_repo_is_checkout=True,
    )
    assert got == Path("/explicit/repo")


def test_resolve_repo_refuses_a_bad_explicit_path_rather_than_falling_back():
    # The operator named a specific path; silently substituting another one
    # would build from code they didn't point at.
    got = deploy.resolve_repo(
        explicit="/typo/repo", explicit_is_checkout=False,
        repo_root=Path("/repo/root"), repo_root_is_checkout=True,
        marker_repo="/marker/repo", marker_repo_is_checkout=True,
    )
    assert got is None


def test_resolve_repo_falls_back_to_repo_root_without_an_explicit_flag():
    got = deploy.resolve_repo(
        explicit=None, explicit_is_checkout=False,
        repo_root=Path("/repo/root"), repo_root_is_checkout=True,
        marker_repo="/marker/repo", marker_repo_is_checkout=True,
    )
    assert got == Path("/repo/root")


def test_resolve_repo_falls_back_to_the_marker_repo_when_repo_root_is_not_a_checkout():
    # The deployed-copy case: `_repo_root()` lands in site-packages, but the
    # marker recorded where a real checkout deployed from last time.
    got = deploy.resolve_repo(
        explicit=None, explicit_is_checkout=False,
        repo_root=Path("/opt/venv/site-packages/crr"), repo_root_is_checkout=False,
        marker_repo="/home/u/src/crr", marker_repo_is_checkout=True,
    )
    assert got == Path("/home/u/src/crr")


def test_resolve_repo_refuses_when_nothing_is_a_checkout():
    got = deploy.resolve_repo(
        explicit=None, explicit_is_checkout=False,
        repo_root=Path("/opt/venv/site-packages/crr"), repo_root_is_checkout=False,
        marker_repo=None, marker_repo_is_checkout=False,
    )
    assert got is None


def test_no_checkout_refusal_names_the_bad_explicit_path():
    msg = deploy.no_checkout_refusal("/typo/repo")
    assert "/typo/repo" in msg and "checkout" in msg


def test_no_checkout_refusal_without_explicit_says_what_to_do():
    # The message it replaces ("is this a git checkout?") sent the reporter
    # down the wrong path; this one must name both ways out.
    msg = deploy.no_checkout_refusal(None)
    assert "source checkout" in msg
    assert "--repo" in msg


# --- deploy_status: what doctor renders about the deployed snapshot ------

def test_deploy_status_nothing_deployed_is_informational():
    ok, detail = deploy.deploy_status(
        deployed_sha=None, head_sha=None, is_ancestor=None, commits_behind=None)
    assert ok is True
    assert "nothing deployed" in detail


def test_deploy_status_caveats_when_head_is_unknown():
    # Marker present, but the repo it was deployed from can't be found or
    # isn't a checkout: name the deployed sha, don't claim a comparison.
    ok, detail = deploy.deploy_status(
        deployed_sha="aaaaaaa1111", head_sha=None, is_ancestor=None, commits_behind=None)
    assert ok is True
    assert "aaaaaaa" in detail
    assert "cannot compare" in detail


def test_deploy_status_matching_shas_are_up_to_date():
    ok, detail = deploy.deploy_status(
        deployed_sha="abc1234", head_sha="abc1234", is_ancestor=None, commits_behind=None)
    assert ok is True
    assert "up to date" in detail


def test_deploy_status_an_ancestor_deploy_warns_with_the_count():
    ok, detail = deploy.deploy_status(
        deployed_sha="aaaaaaa1111", head_sha="bbbbbbb2222",
        is_ancestor=True, commits_behind=3)
    assert ok is False
    assert "aaaaaaa" in detail and "bbbbbbb" in detail
    assert "3 commit" in detail
    assert "crr deploy" in detail


def test_deploy_status_an_unknown_sha_is_informational_not_a_guess():
    # Rebased/squashed away: git can't place it, so this must not claim
    # "behind" (nor "up to date") for a sha it can't locate at all.
    ok, detail = deploy.deploy_status(
        deployed_sha="aaaaaaa1111", head_sha="bbbbbbb2222",
        is_ancestor=None, commits_behind=None)
    assert ok is True
    assert "aaaaaaa" in detail and "bbbbbbb" in detail
    assert "cannot be compared" in detail


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


def test_marker_records_the_source_repo_when_given(tmp_path):
    from crr.adapters import deploy as ad
    path = tmp_path / "app" / "deployed.json"
    ad.write_marker(path, "abc1234", "2026-08-10T00:00:00Z", repo="/home/u/src/crr")
    assert ad.read_marker_repo(path) == "/home/u/src/crr"
    assert ad.read_marker(path) == "abc1234"  # sha read is unaffected


def test_read_marker_repo_is_none_for_an_older_marker_without_the_key(tmp_path):
    # Backward compatibility: markers written before this field existed
    # must not crash anything that now looks for it.
    from crr.adapters import deploy as ad
    path = tmp_path / "deployed.json"
    path.write_text('{"sha": "abc1234", "deployed_at": "x"}', encoding="utf-8")
    assert ad.read_marker_repo(path) is None
    assert ad.read_marker(path) == "abc1234"


def test_write_marker_omits_the_repo_key_when_not_given(tmp_path):
    from crr.adapters import deploy as ad
    path = tmp_path / "deployed.json"
    ad.write_marker(path, "abc1234", "x")
    assert ad.read_marker_repo(path) is None


def test_is_checkout_true_for_a_directory_with_a_dot_git_dir(tmp_path):
    from crr.adapters import deploy as ad
    (tmp_path / ".git").mkdir()
    assert ad.is_checkout(tmp_path) is True


def test_is_checkout_true_for_a_dot_git_file_worktree(tmp_path):
    # A git worktree's `.git` is a file pointing at the real gitdir, not a
    # directory — still a checkout.
    from crr.adapters import deploy as ad
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert ad.is_checkout(tmp_path) is True


def test_is_checkout_false_without_a_dot_git_entry(tmp_path):
    from crr.adapters import deploy as ad
    assert ad.is_checkout(tmp_path) is False


def test_is_ancestor_true_when_git_says_so(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class Ok:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Ok())
    assert ad.is_ancestor(tmp_path, "a", "b") is True


def test_is_ancestor_false_when_git_says_not_an_ancestor(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class NotAncestor:
        returncode, stdout, stderr = 1, "", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: NotAncestor())
    assert ad.is_ancestor(tmp_path, "a", "b") is False


def test_is_ancestor_unknown_when_the_sha_is_not_a_known_object(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class BadObject:
        returncode, stdout, stderr = 128, "", "fatal: not a valid object name"

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: BadObject())
    assert ad.is_ancestor(tmp_path, "gone", "b") is None


def test_is_ancestor_degrades_on_exceptions(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    def boom(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(ad.subprocess, "run", boom)
    assert ad.is_ancestor(tmp_path, "a", "b") is None


def test_commits_behind_counts_from_git(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class Ok:
        returncode, stdout, stderr = 0, "3\n", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Ok())
    assert ad.commits_behind(tmp_path, "a", "b") == 3


def test_commits_behind_degrades_on_non_integer_output(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class Weird:
        returncode, stdout, stderr = 0, "not a number\n", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Weird())
    assert ad.commits_behind(tmp_path, "a", "b") is None


def test_commits_behind_degrades_on_git_failure(monkeypatch, tmp_path):
    from crr.adapters import deploy as ad

    class Fail:
        returncode, stdout, stderr = 128, "", "bad revision"

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Fail())
    assert ad.commits_behind(tmp_path, "a", "b") is None


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


# --- the bug: deploy re-invoked through the copy it made can't resolve ---
# --- its own source checkout -----------------------------------------------
#
# `_repo_root()` returns `__file__`'s grandparent. Run the deployed,
# PATH-linked `crr` and that lands in a venv's site-packages — not a git
# checkout — so `is_dirty()` returns None ("could not tell") and deploy
# refused every time, even though nothing was actually dirty.

def test_deploy_from_a_non_checkout_repo_root_refuses_the_old_unhelpful_way_without_a_fallback(
        tmp_path, monkeypatch, capsys):
    # RED: reproduces the reported bug on unfixed code — `_repo_root()`
    # pointed at a plain directory (no marker to fall back to either), and
    # deploy had nothing usable and no actionable way to say so.
    from crr import cli
    from crr.adapters import state_dir
    fake_root = tmp_path / "site-packages" / "crr"
    fake_root.mkdir(parents=True)
    monkeypatch.setattr(cli, "_repo_root", lambda: fake_root)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    assert cli.main(["deploy"]) == 2
    err = capsys.readouterr().err
    assert "could not find a git checkout" in err
    assert "--repo" in err
    # The old message is gone — it sent the reporter down the wrong path.
    assert "is this a git checkout?" not in err


def test_deploy_force_does_not_override_a_missing_checkout(tmp_path, monkeypatch, capsys):
    # `--force` overrides the DIRTY-tree safety gate (refusal()). Having no
    # checkout at all to build from is a missing input, not a gate to
    # override — there's still nothing to build, force or not.
    from crr import cli
    from crr.adapters import state_dir
    fake_root = tmp_path / "site-packages" / "crr"
    fake_root.mkdir(parents=True)
    monkeypatch.setattr(cli, "_repo_root", lambda: fake_root)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    assert cli.main(["deploy", "--force"]) == 2
    assert "could not find a git checkout" in capsys.readouterr().err


def test_deploy_falls_back_to_the_repo_recorded_in_the_marker(tmp_path, monkeypatch, capsys):
    # GREEN: the fix. `_repo_root()` is not a checkout (the deployed copy's
    # own case), but a previous successful deploy recorded a real checkout
    # in the marker — deploy must use it rather than refuse.
    from crr import cli
    from crr.core import deploy as core
    from crr.adapters import deploy as ad, state_dir
    fake_root = tmp_path / "site-packages" / "crr"
    fake_root.mkdir(parents=True)
    real_repo = tmp_path / "src" / "crr"
    (real_repo / ".git").mkdir(parents=True)
    sd = tmp_path / "state"
    monkeypatch.setattr(cli, "_repo_root", lambda: fake_root)
    monkeypatch.setattr(state_dir, "state_dir", lambda: sd)
    ad.write_marker(core.marker_path(sd), "old1234", "2026-08-01T00:00:00Z", repo=str(real_repo))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "new1234")
    built = []
    monkeypatch.setattr(ad, "build", lambda *a, **k: built.append(a) or None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert cli.main(["deploy", "--no-restart"]) == 0
    assert built, "did not fall back to the marker-recorded checkout"
    assert built[0][1] == real_repo


def test_deploy_prefers_an_explicit_repo_flag(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    explicit_repo = tmp_path / "explicit"
    (explicit_repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    built = []
    monkeypatch.setattr(ad, "build", lambda *a, **k: built.append(a) or None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert cli.main(["deploy", "--repo", str(explicit_repo), "--no-restart"]) == 0
    assert built[0][1] == explicit_repo


def test_deploy_refuses_actionably_when_the_explicit_repo_is_not_a_checkout(
        tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir
    bad = tmp_path / "not-a-repo"
    bad.mkdir()
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    assert cli.main(["deploy", "--repo", str(bad)]) == 2
    err = capsys.readouterr().err
    assert str(bad) in err and "checkout" in err


def test_deploy_prints_which_repo_it_deployed_from(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    repo = tmp_path / "src"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(ad, "is_dirty", lambda r, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda r, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert cli.main(["deploy", "--no-restart"]) == 0
    out = capsys.readouterr().out
    assert str(repo) in out


def test_deploy_records_the_repo_path_in_the_marker(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.core import deploy as core
    from crr.adapters import deploy as ad, state_dir
    repo = tmp_path / "src"
    (repo / ".git").mkdir(parents=True)
    sd = tmp_path / "state"
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(state_dir, "state_dir", lambda: sd)
    monkeypatch.setattr(ad, "is_dirty", lambda r, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda r, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert cli.main(["deploy", "--no-restart"]) == 0
    assert ad.read_marker_repo(core.marker_path(sd)) == str(repo)


def test_doctor_reports_nothing_deployed(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)  # no marker written
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "nothing deployed" in out


def test_doctor_warns_when_the_deployed_sha_is_behind_head(tmp_path, monkeypatch, capsys):
    # "My fix is committed" and "my fix is live" are otherwise
    # indistinguishable — a stale deploy is legitimate, just not invisible.
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "read_marker", lambda path: "aaaaaaa1111")
    monkeypatch.setattr(ad, "read_marker_repo", lambda path: "/src/crr")
    monkeypatch.setattr(ad, "is_checkout", lambda repo: True)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "bbbbbbb2222")
    monkeypatch.setattr(ad, "is_ancestor", lambda repo, a, b, timeout=5: True)
    monkeypatch.setattr(ad, "commits_behind", lambda repo, a, b, timeout=5: 5)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "aaaaaaa" in out and "bbbbbbb" in out and "crr deploy" in out
    assert "[WARN]" in out


def test_doctor_reports_up_to_date_when_the_deploy_matches_head(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "read_marker", lambda path: "same111")
    monkeypatch.setattr(ad, "read_marker_repo", lambda path: "/src/crr")
    monkeypatch.setattr(ad, "is_checkout", lambda repo: True)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "same111")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "up to date" in out
    assert "[WARN]" not in out


def test_doctor_caveats_when_the_deployed_repo_is_unknown(tmp_path, monkeypatch, capsys):
    # Marker present, but nothing usable to compare HEAD against — must not
    # claim drift (or "up to date") it cannot measure.
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "read_marker", lambda path: "aaaaaaa1111")
    monkeypatch.setattr(ad, "read_marker_repo", lambda path: None)
    monkeypatch.setattr(ad, "is_checkout", lambda repo: False)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "aaaaaaa" in out
    assert "cannot compare" in out
    assert "[WARN]" not in out


def test_doctor_is_informational_when_the_deployed_sha_is_unknown_to_the_repo(
        tmp_path, monkeypatch, capsys):
    # Rebased/squashed away: git can't place it. Must not guess "behind".
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "read_marker", lambda path: "aaaaaaa1111")
    monkeypatch.setattr(ad, "read_marker_repo", lambda path: "/src/crr")
    monkeypatch.setattr(ad, "is_checkout", lambda repo: True)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "bbbbbbb2222")
    monkeypatch.setattr(ad, "is_ancestor", lambda repo, a, b, timeout=5: None)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "cannot be compared" in out
    assert "[WARN]" not in out


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


# These two pass a POSIX-shaped PATH, so they say so with `sep=":"` rather
# than leaning on the host's separator. Without it they were reading a
# colon-joined string on Windows, where PATH is joined with ";" — one
# entry, never matching, warning fired. That looked like the #70
# `path_warning` defect resurfacing; it is the mirror image, a test feeding
# an input its own platform would never produce.
def test_a_link_outside_PATH_is_called_out():
    msg = deploy.path_warning("/usr/bin:/bin", Path("/home/u/.local/bin/crr"),
                              sep=":")
    assert msg and "not on PATH" in msg


def test_a_link_on_PATH_is_silent():
    assert deploy.path_warning("/home/u/.local/bin:/usr/bin",
                               Path("/home/u/.local/bin/crr"), sep=":") is None


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
    monkeypatch.setattr(ad, "restart_service", lambda **k: None)
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


def test_path_warning_uses_the_platform_separator():
    # [#70] Hardcoded ":" made a Windows PATH parse as ONE entry, so the
    # warning fired for a directory that was on it. Only the separator is
    # testable off-platform — Windows path SHAPE (backslashes, drive-letter
    # case) is normalised by os.path.normcase, which is identity here, so
    # that half is proved by the Windows CI job rather than pretended at.
    link = Path("/home/u/.local/bin/crr")
    multi = "/home/u/.local/bin;/usr/bin"
    assert deploy.path_warning(multi, link, sep=";") is None
    assert deploy.path_warning(multi, link, sep=":") is not None  # the old bug
    assert deploy.path_warning("/home/u/.local/bin:/usr/bin", link, sep=":") is None


# --- deploy auto-restart (#101) -----------------------------------------------
#
# `crr deploy` printed "restart the services to pick it up" and left the
# operator to remember. Forgotten restarts leave the dashboard running
# stale code — the exact landmine the deploy was built to prevent.

def test_restart_service_calls_systemctl_restart(monkeypatch):
    from crr.adapters import deploy as ad
    calls = []

    class Ok:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Ok())
    assert ad.restart_service() is None
    assert calls == [["systemctl", "--user", "restart", "crr-web.service"]]


def test_restart_service_reports_failure(monkeypatch):
    from crr.adapters import deploy as ad

    class Fail:
        returncode, stdout, stderr = 1, "", "unit not found"

    monkeypatch.setattr(ad.subprocess, "run", lambda cmd, **k: Fail())
    err = ad.restart_service()
    assert err and "unit not found" in err


def test_restart_service_degrades_on_exceptions(monkeypatch):
    from crr.adapters import deploy as ad

    def boom(*a, **k):
        raise OSError("no systemctl")

    monkeypatch.setattr(ad.subprocess, "run", boom)
    err = ad.restart_service()
    assert err and "no systemctl" in err


def test_deploy_restarts_the_service_after_a_successful_build(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    restarted = []
    home = tmp_path / "home"
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: restarted.append(1) or None)
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))
    assert cli.main(["deploy"]) == 0
    assert restarted == [1], "service was not restarted after a successful deploy"
    assert "restarted crr-web" in capsys.readouterr().out


def test_deploy_does_not_restart_when_the_build_failed(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    restarted = []
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: "install failed: boom")
    monkeypatch.setattr(ad, "restart_service", lambda **k: restarted.append(1) or None)
    assert cli.main(["deploy"]) == 1
    assert restarted == [], "restarted the service after a failed build"


def test_deploy_reports_restart_failure_but_still_succeeds(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    home = tmp_path / "home"
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: "unit not found")
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))
    assert cli.main(["deploy"]) == 0, "a restart failure must not fail the whole deploy"
    out, err = capsys.readouterr()
    assert "unit not found" in err


def test_deploy_no_restart_skips_the_service_restart(tmp_path, monkeypatch, capsys):
    from crr import cli
    from crr.adapters import deploy as ad, state_dir
    restarted = []
    home = tmp_path / "home"
    monkeypatch.setattr(state_dir, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ad, "is_dirty", lambda repo, timeout=5: False)
    monkeypatch.setattr(ad, "head_sha", lambda repo, timeout=5: "abc1234")
    monkeypatch.setattr(ad, "build", lambda *a, **k: None)
    monkeypatch.setattr(ad, "restart_service", lambda **k: restarted.append(1) or None)
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))
    assert cli.main(["deploy", "--no-restart"]) == 0
    assert restarted == [], "--no-restart did not skip the restart"
