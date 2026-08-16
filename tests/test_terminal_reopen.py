from crr.core import terminal_reopen as tr


def test_empty_sessions_is_a_noop():
    p = tr.plan_terminal_reopen([], in_tmux=False, has_tty=True, current_session=None)
    assert p.commands == () and p.exec_argv is None and p.message == ""


def test_no_tty_returns_a_notice_never_an_exec():
    p = tr.plan_terminal_reopen(
        [("crr-a", "proj")], in_tmux=False, has_tty=False, current_session=None)
    assert p.commands == () and p.exec_argv is None
    assert "tmux attach -t" in p.message and "crr-a" in p.message


def test_in_tmux_links_each_into_the_current_session_no_exec():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=True, has_tty=True, current_session="work")
    assert p.exec_argv is None
    assert p.commands == (
        ("tmux", "rename-window", "-t", "crr-a", "alpha"),
        ("tmux", "link-window", "-s", "crr-a", "-t", "work"),
        ("tmux", "rename-window", "-t", "crr-b", "beta"),
        ("tmux", "link-window", "-s", "crr-b", "-t", "work"),
    )
    assert "Ctrl-b w" in p.message
    # Regression guard: source targets are by session name only, so they
    # resolve to the session's single window at any tmux base-index.
    assert all(
        ":0" not in a
        for cmd in p.commands if cmd[1] in ("rename-window", "link-window")
        for a in cmd
    )


def test_not_in_tmux_single_attaches_directly_no_aggregate():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha")], in_tmux=False, has_tty=True, current_session=None)
    assert p.commands == ()
    assert p.exec_argv == ("tmux", "attach", "-t", "crr-a")


def test_not_in_tmux_multi_builds_aggregate_then_execs_attach():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None, aggregate_exists=False)
    assert p.commands == (
        ("tmux", "new-session", "-d", "-s", "crr-restored", "-n", "__crr_placeholder__"),
        ("tmux", "rename-window", "-t", "crr-a", "alpha"),
        ("tmux", "link-window", "-s", "crr-a", "-t", "crr-restored"),
        ("tmux", "rename-window", "-t", "crr-b", "beta"),
        ("tmux", "link-window", "-s", "crr-b", "-t", "crr-restored"),
        # Placeholder killed by NAME (not :0) so it works at any base-index.
        ("tmux", "kill-window", "-t", "crr-restored:__crr_placeholder__"),
    )
    assert p.exec_argv == ("tmux", "attach", "-t", "crr-restored")


def test_aggregate_is_killed_first_when_it_already_exists():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None, aggregate_exists=True)
    assert p.commands[0] == ("tmux", "kill-session", "-t", "crr-restored")
    assert p.commands[1] == ("tmux", "new-session", "-d", "-s", "crr-restored",
                             "-n", "__crr_placeholder__")


def test_placeholder_is_killed_by_name_not_index_for_base_index_safety():
    # The aggregate placeholder must be killed by its unique NAME, never by
    # `:0` — a user with `base-index 1` would otherwise keep a spare shell
    # window (verified on real tmux). No `:0` window target in the plan.
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None)
    kills = [c for c in p.commands if c[1] == "kill-window"]
    assert kills == [("tmux", "kill-window", "-t", "crr-restored:__crr_placeholder__")]
    assert all(not (c[1] == "kill-window" and c[-1].endswith(":0")) for c in p.commands)
