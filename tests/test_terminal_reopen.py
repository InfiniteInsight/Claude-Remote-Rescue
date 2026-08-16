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
        ("tmux", "rename-window", "-t", "crr-a:0", "alpha"),
        ("tmux", "link-window", "-s", "crr-a:0", "-t", "work"),
        ("tmux", "rename-window", "-t", "crr-b:0", "beta"),
        ("tmux", "link-window", "-s", "crr-b:0", "-t", "work"),
    )
    assert "Ctrl-b w" in p.message


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
        ("tmux", "new-session", "-d", "-s", "crr-restored"),
        ("tmux", "rename-window", "-t", "crr-a:0", "alpha"),
        ("tmux", "link-window", "-s", "crr-a:0", "-t", "crr-restored"),
        ("tmux", "rename-window", "-t", "crr-b:0", "beta"),
        ("tmux", "link-window", "-s", "crr-b:0", "-t", "crr-restored"),
        ("tmux", "kill-window", "-t", "crr-restored:0"),
    )
    assert p.exec_argv == ("tmux", "attach", "-t", "crr-restored")


def test_aggregate_is_killed_first_when_it_already_exists():
    p = tr.plan_terminal_reopen(
        [("crr-a", "alpha"), ("crr-b", "beta")],
        in_tmux=False, has_tty=True, current_session=None, aggregate_exists=True)
    assert p.commands[0] == ("tmux", "kill-session", "-t", "crr-restored")
    assert p.commands[1] == ("tmux", "new-session", "-d", "-s", "crr-restored")
