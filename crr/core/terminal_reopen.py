"""Pure planner for reopening restored conversations into THIS terminal.

On a headless host (no GUI tab spawner) the terminal-native "tabs" are tmux
windows. Given the restored conversations' tmux session names, whether the
caller is inside tmux, and whether a tty is present, this decides the exact
tmux commands to run and whether to ``exec tmux attach`` — and does NO I/O, so
every branch is a pure function the cli can test without a tmux server.

``link-window`` SHARES a window between sessions (never ``move-window``): each
conversation stays in its own tracked ``crr-<sid>`` session AND shows up in the
aggregate/current session, so nothing is untracked and detaching kills nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from crr.core.reviver import attach_argv

AGGREGATE_NAME = "crr-restored"
# The aggregate's throwaway first window, named so it can be killed by name
# (index-independent — see the not-in-tmux branch). Double-underscored to not
# collide with any conversation's cwd-basename window label.
_PLACEHOLDER = "__crr_placeholder__"


@dataclass(frozen=True)
class TerminalReopenPlan:
    commands: tuple[tuple[str, ...], ...]
    exec_argv: tuple[str, ...] | None
    message: str


def plan_terminal_reopen(
    sessions: Sequence[tuple[str, str]],
    *,
    in_tmux: bool,
    has_tty: bool,
    current_session: str | None,
    aggregate_exists: bool = False,
) -> TerminalReopenPlan:
    sessions = list(sessions)
    if not sessions:
        return TerminalReopenPlan((), None, "")
    names = [name for name, _ in sessions]
    if not has_tty:
        joined = ", ".join(names)
        return TerminalReopenPlan(
            (), None,
            f"{len(sessions)} conversation(s) restored — attach with: "
            f"tmux attach -t <name> ({joined})",
        )

    def rename_and_link(dst: str) -> list[tuple[str, ...]]:
        out: list[tuple[str, ...]] = []
        for name, label in sessions:
            # Rename the SOURCE window (shared, so the name shows everywhere it
            # is linked, and the target is unambiguous without result indices).
            # Targeted by SESSION NAME ONLY (no ":0") — each crr-<sid> session
            # has exactly one window, created without a forced index, so it
            # lands wherever the user's tmux base-index puts it (crr never
            # sets base-index). Appending ":0" would miss under a non-default
            # base-index (e.g. `set -g base-index 1`); the bare name always
            # resolves to that single window regardless.
            out.append(("tmux", "rename-window", "-t", name, label))
            out.append(("tmux", "link-window", "-s", name, "-t", dst))
        return out

    if in_tmux:
        cmds = tuple(rename_and_link(current_session or ""))
        return TerminalReopenPlan(
            cmds, None,
            f"linked {len(sessions)} restored conversation(s) into this tmux "
            "session — Ctrl-b w to list",
        )

    if len(sessions) == 1:
        return TerminalReopenPlan(
            (), tuple(attach_argv(names[0])),
            f"attaching {names[0]} — Ctrl-b d to detach",
        )

    cmds: list[tuple[str, ...]] = []
    if aggregate_exists:
        # Rebuild so the aggregate reflects the current restored set. Safe:
        # its windows survive in their crr-<sid> sessions (a tmux window dies
        # only when its LAST linking session drops it).
        cmds.append(("tmux", "kill-session", "-t", AGGREGATE_NAME))
    # Give the placeholder shell a unique NAME so it can be killed by name,
    # not by index. `new-session` puts it at the user's base-index (0 by
    # default, but `set -g base-index 1` is common), so a `:0` kill would miss
    # under base-index 1 and leave a spare shell window (observed on real
    # tmux). Killing by name works at any base-index and can never hit a
    # conversation window (they carry their cwd-basename labels, not this).
    cmds.append(("tmux", "new-session", "-d", "-s", AGGREGATE_NAME, "-n", _PLACEHOLDER))
    cmds.extend(rename_and_link(AGGREGATE_NAME))
    cmds.append(("tmux", "kill-window", "-t", f"{AGGREGATE_NAME}:{_PLACEHOLDER}"))
    return TerminalReopenPlan(
        tuple(cmds), tuple(attach_argv(AGGREGATE_NAME)),
        f"attaching {len(sessions)} restored conversation(s) in "
        f"'{AGGREGATE_NAME}' — Ctrl-b w to list, Ctrl-b d to detach",
    )
