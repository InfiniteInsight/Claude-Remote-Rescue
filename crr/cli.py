"""crr command-line entry point — the composition root.

This is the ONE module allowed to import both ``crr.core`` and
``crr.adapters`` (the sole exception declared in .importlinter). Its job
is wiring: pick platform adapters, hand them to core, dispatch
subcommands. Business logic belongs in core, not here.

Phase 1 (headless Linux) is implemented: status, revive, session ops
(reopen/dismiss/remove/kick/close/detmux), diagnose, gc, the web dashboard,
the systemd watchdog, and the shim-facing hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable, Sequence

from crr import __version__
from crr.adapters import boot_identity  # composition root may import adapters
from crr.adapters import diagnostics as diag_source
from crr.adapters import diagnostics_macos
from crr.adapters import launchd, process_probe, state_dir, systemd, tab_spawn, tmux, transcript_source
from crr.adapters import diagnostics_windows, host, scheduled_task, tab_spawn_linux, tab_spawn_windows
from crr.adapters.locking import mutation_lock
from crr.core import config as cfg  # ...and core
from crr.core import contracts, ops, ports, rescue, resume, reviver, status, web
from crr.core import diagnostics as diag_core
from crr.core.archive import ArchiveStore, is_expired
from crr.core.flags import FlagStore
from crr.core.journal import JournalStore, new_entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config() -> cfg.Config:
    """Load config.toml (co-located in the crr state dir) over the defaults.

    A malformed config warns on stderr and falls back to defaults rather
    than breaking every command (including the shim hot path).
    """
    toml_path = state_dir.state_dir() / "config.toml"
    try:
        return cfg.Config(cfg.load_toml_overrides(toml_path))
    except (cfg.ConfigError, ValueError, OSError) as exc:
        print(f"crr: ignoring bad config {toml_path}: {exc}", file=sys.stderr)
        return cfg.Config()


def _tail_facts_extractor(config: cfg.Config):
    """A tail_facts(entry)->{last_prompt, model} closure for assemble_sessions.

    One backward transcript read per card yields both facts. Only called for
    claude-bearing entries (assemble_sessions filters the rest), so
    entry["claude"] is always present here.
    """
    cap = config.get("last_prompt_display_cap")
    return lambda entry: transcript_source.read_tail_facts(entry["claude"]["session_id"], cap)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crr",
        description="Keep Claude Code sessions alive and remotely rescuable.",
    )
    parser.add_argument("--version", action="version", version=f"crr {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="report scaffold/environment status")
    doctor.set_defaults(func=_cmd_doctor)

    st = sub.add_parser("status", help="list journaled sessions and their state")
    st.add_argument("--json", action="store_true", help="emit the /api/sessions payload")
    st.set_defaults(func=_cmd_status)

    rev = sub.add_parser(
        "revive",
        help="revive crashed claude sessions into detached tmux (watchdog action)",
    )
    rev.set_defaults(func=_cmd_revive)

    # Session operations (classifier-gated, pid-keyed).
    rm = sub.add_parser("remove", help="delist a session (touches nothing else)")
    rm.add_argument("--pid", type=int, required=True)
    rm.set_defaults(func=_cmd_remove)

    dis = sub.add_parser("dismiss", help="clean up a crashed session without reviving")
    dis.add_argument("--pid", type=int, required=True)
    dis.set_defaults(func=_cmd_dismiss)

    reo = sub.add_parser(
        "reopen", aliases=["restore"],
        help="revive one crashed or ghost session now (alias: restore)",
    )
    reo.add_argument("--pid", type=int, required=True)
    reo.set_defaults(func=_cmd_reopen)

    kick = sub.add_parser("kick", help="restart claude in place on the same conversation")
    kick.add_argument("pid", type=int)
    kick.set_defaults(func=_cmd_kick)

    close = sub.add_parser("close", help="end a live session (remote exit); no revival")
    close.add_argument("pid", type=int)
    close.set_defaults(func=_cmd_close)

    dtm = sub.add_parser("detmux", help="re-home a revived tmux session into a visible tab")
    dtm.add_argument("pid", type=int)
    dtm.set_defaults(func=_cmd_detmux)

    rescued = sub.add_parser(
        "rescued",
        help="list conversations rescued from a previous boot (awaiting re-home)",
    )
    rescued.set_defaults(func=_cmd_rescued)

    diag = sub.add_parser("diagnose", help="explain why the previous boot / sessions may have died")
    diag.add_argument("--json", action="store_true", help="emit the /api/diagnostics payload")
    diag.set_defaults(func=_cmd_diagnose)

    gc = sub.add_parser("gc", help="drop archive records past the retention window")
    gc.set_defaults(func=_cmd_gc)

    w = sub.add_parser("web", help="serve the tailnet dashboard (loopback only)")
    w.add_argument("--port", type=int, default=8377)
    w.set_defaults(func=_cmd_web)

    sysd = sub.add_parser(
        "systemd",
        help="print (or --install) the systemd user watchdog timer + service",
    )
    sysd.add_argument("--install", action="store_true",
                      help="write units to ~/.config/systemd/user and enable the timer + web + linger")
    sysd.add_argument("--uninstall", action="store_true",
                      help="disable/remove the watchdog + dashboard integration")
    sysd.add_argument("--crr-bin", default=None,
                      help="absolute crr path to bake into the units (default: this crr binary)")
    sysd.add_argument("--port", type=int, default=8377,
                      help="dashboard port to bake into crr-web.service (default: 8377)")
    sysd.set_defaults(func=_cmd_systemd)

    lncd = sub.add_parser(
        "launchd",
        help="print (or --install) the macOS launchd user agents (watchdog + dashboard)",
    )
    lncd.add_argument("--install", action="store_true",
                      help="write agents to ~/Library/LaunchAgents and launchctl-load them")
    lncd.add_argument("--uninstall", action="store_true",
                      help="disable/remove the watchdog + dashboard integration")
    lncd.add_argument("--crr-bin", default=None,
                      help="absolute crr path to bake into the agents (default: this crr binary)")
    lncd.add_argument("--port", type=int, default=8377,
                      help="dashboard port to bake into the web agent (default: 8377)")
    lncd.set_defaults(func=_cmd_launchd)

    sch = sub.add_parser(
        "schtasks",
        help="print (or --install) the Windows/WSL Scheduled Tasks (watchdog + dashboard)",
    )
    sch.add_argument("--install", action="store_true",
                     help="run schtasks.exe to create the tasks (WSL host only)")
    sch.add_argument("--uninstall", action="store_true",
                     help="disable/remove the watchdog + dashboard integration")
    sch.add_argument("--crr-bin", default=None,
                     help="crr path inside WSL to bake into the tasks (default: this crr binary)")
    sch.add_argument("--port", type=int, default=8377,
                     help="dashboard port to bake into the web task (default: 8377)")
    sch.set_defaults(func=_cmd_schtasks)

    conf = sub.add_parser("config", help="inspect configuration")
    conf.add_argument(
        "--effective",
        action="store_true",
        help="print every key with its value and origin (configured|default)",
    )
    conf.set_defaults(func=_cmd_config)

    # Shim-facing commands (called by the shell hooks, by absolute path).
    reg = sub.add_parser("register", help="[shim] journal a shell at start")
    reg.add_argument("--pid", type=int, required=True)
    reg.add_argument("--cwd", required=True)
    reg.add_argument("--shell", required=True, choices=contracts.SHELLS)
    reg.add_argument("--host", required=True, choices=contracts.HOSTS)
    reg.set_defaults(func=_cmd_register)

    lc = sub.add_parser("last-cmd", help="[shim] update a shell's last command / cwd")
    lc.add_argument("--pid", type=int, required=True)
    lc.add_argument("--cmd", required=True)
    lc.add_argument("--cwd", default=None)
    lc.set_defaults(func=_cmd_last_cmd)

    dereg = sub.add_parser("deregister", help="[shim] remove a shell's journal entry")
    dereg.add_argument("--pid", type=int, required=True)
    dereg.set_defaults(func=_cmd_deregister)

    cl = sub.add_parser(
        "claude-launch",
        help="[shim] journal a fresh claude session and print its session-id",
    )
    cl.add_argument("--pid", type=int, required=True)
    cl.add_argument("--session-id", default=None, help="use this sid instead of generating one")
    cl.set_defaults(func=_cmd_claude_launch)

    cr = sub.add_parser(
        "claude-resume",
        help="[shim] journal a resumed/continued claude session (guessed/verified sid)",
    )
    cr.add_argument("--pid", type=int, required=True)
    cr.add_argument("--cwd", required=True, help="cwd, to locate the transcript(s) to guess from")
    cr.add_argument("--session-id", default=None, help="explicit sid from --resume <sid>, if any")
    cr.set_defaults(func=_cmd_claude_resume)

    ce = sub.add_parser(
        "claude-exit",
        help="[shim] mark a shell's claude session ended (clean exit)",
    )
    ce.add_argument("--pid", type=int, required=True)
    ce.set_defaults(func=_cmd_claude_exit)

    repair = sub.add_parser(
        "repair-check",
        help="[shim] read/clear a session's relaunch/close flag",
    )
    repair.add_argument("--pid", type=int, required=True)
    repair.add_argument("--clear", action="store_true")
    repair.set_defaults(func=_cmd_repair_check)

    shim = sub.add_parser(
        "shim",
        help="print the shell shim to source from your rc file",
    )
    shim.add_argument("shell", choices=SHIM_SHELLS)
    shim.add_argument(
        "--crr-bin",
        default=None,
        help="absolute path to bake into the shim (default: this crr binary)",
    )
    shim.set_defaults(func=_cmd_shim)

    return parser


# Shells that have a shim template. Keep in sync with the
# crr/shims/crr.<shell> files.
SHIM_SHELLS = ("bash", "zsh", "fish")


def _resolve_crr_bin(explicit: str | None) -> str:
    if explicit:
        return explicit
    # Prefer the absolute path of the crr that is generating the shim, so
    # the baked path points at exactly this install ([lesson: PATH
    # poisoning] — never a bare PATH lookup at shim runtime).
    argv0 = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.basename(argv0).startswith("crr") and os.path.exists(argv0):
        return argv0
    found = shutil.which("crr")
    return found or argv0 or "crr"


def _cmd_shim(args: argparse.Namespace) -> int:
    template = resources.files("crr.shims").joinpath(f"crr.{args.shell}").read_text(
        encoding="utf-8"
    )
    print(template.replace("@CRR_BIN@", _resolve_crr_bin(args.crr_bin)), end="")
    return 0


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "WARN"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Install-health checklist. Read-only; never changes anything."""
    print(f"crr {__version__}")
    print(
        "contracts: journal v"
        f"{contracts.JOURNAL_SCHEMA_VERSION}, sessions v{contracts.SESSIONS_CONTRACT_VERSION}, "
        f"diagnostics v{contracts.DIAGNOSTICS_CONTRACT_VERSION}"
    )

    # Platform integration.
    try:
        adapter = boot_identity.detect()
        _check("boot-identity adapter", True, type(adapter).__name__)
    except NotImplementedError as exc:
        _check("boot-identity adapter", False, str(exc))
    _check("tmux (revival substrate)", shutil.which("tmux") is not None,
           shutil.which("tmux") or "MISSING — revival unavailable")
    _check("journalctl (diagnose)", shutil.which("journalctl") is not None,
           "" if shutil.which("journalctl") else "absent — diagnose degrades")

    # State.
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)
    scan = store.scan()
    cards = [e for e in scan.entries if e.get("claude") is not None]
    _check("state dir", sd.exists(), str(sd))
    print(f"         {len(scan.entries)} shell(s) journaled, {len(cards)} with a claude "
          f"session, {len(archive.scan().records)} archived")
    for name, reason in scan.problems:
        _check(f"journal file {name}", False, reason)

    # Config.
    toml_path = sd / "config.toml"
    if toml_path.is_file():
        try:
            cfg.Config(cfg.load_toml_overrides(toml_path))
            _check("config.toml", True, str(toml_path))
        except (cfg.ConfigError, ValueError, OSError) as exc:
            _check("config.toml", False, f"{toml_path}: {exc}")
    else:
        print(f"  [ok  ] config.toml — none (using defaults); crr config --effective to view")

    # systemd units (installed? enabled?).
    ud = systemd.unit_dir(Path.home())
    for unit in (systemd.TIMER_NAME, systemd.WEB_SERVICE_NAME):
        installed = (ud / unit).is_file()
        enabled = ""
        if installed and shutil.which("systemctl"):
            try:
                r = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                                   capture_output=True, text=True, timeout=5)
                enabled = r.stdout.strip() or r.stderr.strip()
            except (subprocess.SubprocessError, OSError):
                enabled = "unknown"
        _check(f"unit {unit}", installed,
               f"{'enabled: ' + enabled if installed else 'not installed — run crr systemd --install'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr status: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))

    now = _now()
    if _guessed_upgradable(store, now):
        with mutation_lock(sd):
            _verify_guessed_sids(store, now)

    scan = store.scan()
    payload = status.assemble_sessions(
        scan.entries, boot, probe, tail_facts=_tail_facts_extractor(config)
    )
    # Validate our own output before emitting it (the P7 validator doubles
    # as a debug guard — both surfaces validate their own output; the web
    # provider below runs the same check independently on its payload).
    contracts.validate_sessions_payload(payload)

    # Corrupt files are surfaced on stderr, never silently dropped.
    for name, reason in scan.problems:
        print(f"crr status: skipped unreadable journal file {name}: {reason}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_status_human(payload)
    return 0


def _print_status_human(payload: dict) -> None:
    sessions = payload["sessions"]
    if not sessions:
        print("no journaled sessions")
        return
    for card in sessions:
        dup = " [dup]" if card["duplicate_group"] else ""
        model = f" {card['model']}" if card["model"] else ""  # omitted when unknown
        print(f"#{card['pid']} · {card['sid8']} [{card['state']}]{model} {card['cwd']}{dup}")


def _cmd_register(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr register: {exc}", file=sys.stderr)
        return 2
    current_boot = boot.current()
    sd = state_dir.state_dir()
    store = JournalStore(sd)

    # Register-safety (recycled-pid guard for revival data): if an entry
    # already exists for this pid and carries a claude session, do not blindly
    # clobber it.
    claude = None
    tmux_session = None
    revive_strikes = 0
    with mutation_lock(sd):
        try:
            existing = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            existing = None
        if existing is not None and existing.get("claude") is not None:
            if existing["boot_id"] != current_boot:
                # Different boot => the old process is unambiguously gone
                # (reboot or stale). Preserve its session in the archive so
                # the reviver can bring it back, then register fresh.
                ArchiveStore(sd).archive(existing, "superseded-on-register", _now())
            else:
                # Same boot => can't distinguish an rc re-source (this same
                # live shell) from pid reuse. Keep the claude field in place:
                # never wipe a possibly-live session, never risk a duplicate.
                claude = existing["claude"]
                tmux_session = existing["tmux_session"]
                revive_strikes = existing["revive_strikes"]

        store.write(new_entry(
            pid=args.pid,
            cwd=args.cwd,
            host=args.host,
            shell=args.shell,
            boot_id=current_boot,
            now=_now(),
            claude=claude,
            tmux_session=tmux_session,
            revive_strikes=revive_strikes,
        ))
    return 0


def _cmd_last_cmd(args: argparse.Namespace) -> int:
    # Hot-path prompt hook: if the entry is gone (never registered, already
    # deregistered), do nothing rather than error into the user's prompt.
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            return 0
        entry["last_cmd"] = args.cmd
        if args.cwd is not None:
            entry["cwd"] = args.cwd
        entry["updated"] = _now()
        store.write(entry)
    return 0


def _cmd_deregister(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        JournalStore(sd).remove(args.pid)
    return 0


def _attach_claude_session(sd: Path, pid: int, sid: str, sid_source: str) -> None:
    """Attach a claude session to a journaled shell, archiving a superseded one.

    Shared by claude-launch (injected) and claude-resume (guessed/verified).
    Under the mutation lock: the read-modify-write plus the superseded-archive
    must be atomic against the revive timer. A shell that was never registered
    has no entry to attach to — a no-op (claude-launch still prints its sid).
    """
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(pid)
        except (KeyError, contracts.ContractError):
            return
        # An existing claude field here is a dead one (the wrapper blocks while
        # claude runs; claude-exit clears it on any exit) — usually a reused
        # pid. Preserve it before overwriting or its revival data is lost.
        if entry.get("claude") is not None and entry["claude"]["session_id"] != sid:
            ArchiveStore(sd).archive(entry, "superseded-on-launch", _now())
        entry["claude"] = {"session_id": sid, "sid_source": sid_source, "started": _now()}
        entry["updated"] = _now()
        store.write(entry)


def _cmd_claude_launch(args: argparse.Namespace) -> int:
    # A wrapper-supplied or freshly generated sid is `injected` — certain,
    # never `guessed`. Print it (for the shim to pass to claude) even if the
    # shell was never registered, so claude still launches identifiably.
    sid = args.session_id or str(uuid.uuid4())
    if not contracts.valid_session_id(sid):
        # A user-typed --session-id may be junk (audit 2026-07-29): keep the
        # wrapper's contract of always printing a sid for claude to use, but
        # never journal it — claude itself will reject a non-UUID sid.
        print(sid)
        return 0
    _attach_claude_session(state_dir.state_dir(), args.pid, sid, "injected")
    print(sid)
    return 0


def _cmd_claude_resume(args: argparse.Namespace) -> int:
    # Resume / continue / picker launches carry no injected sid, so derive one
    # (audit P3): an explicit --resume <sid> is certain (verified if its
    # transcript exists), a --continue/picker launch is the newest transcript
    # (guessed). Journal it so a resumed session is revivable too — without
    # this it would be untracked. Prints nothing: claude already knows its
    # session from --resume/--continue.
    derived = resume.derive_resume_sid(
        args.session_id, transcript_source.list_transcripts(args.cwd)
    )
    if derived is None:
        return 0  # no explicit sid and no transcript to guess — leave untracked
    sid, sid_source = derived
    _attach_claude_session(state_dir.state_dir(), args.pid, sid, sid_source)
    return 0


def _cmd_claude_exit(args: argparse.Namespace) -> int:
    # Clean exit: clear the claude field. A crash skips this call, leaving
    # claude set so the reviver knows the shell died mid-session.
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    with mutation_lock(sd):
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            return 0
        entry["claude"] = None
        entry["updated"] = _now()
        store.write(entry)
    return 0


def _cmd_repair_check(args: argparse.Namespace) -> int:
    """[shim] Print the pid's relaunch/close flag for the repair loop, or
    clear it. Output: 'relaunch <sid>' | 'close' | '' (absent)."""
    flags = FlagStore(state_dir.state_dir())
    if args.clear:
        flags.clear(args.pid)
        return 0
    flag = flags.read(args.pid)
    if flag is None:
        return 0
    kind, sid = flag
    print(kind if sid is None else f"{kind} {sid}")
    return 0


def _verify_guessed_sids(store: JournalStore, now: str) -> None:
    """Upgrade guessed→verified where a transcript now confirms the sid.

    Called from the periodic, mutation-locked revive sweep and — guarded by
    `_guessed_upgradable`'s lock-free pre-scan — from status assembly (CLI
    and web), so the mutation lock is only ever taken here when an upgrade
    is actually about to be written. Each guessed entry is checked against
    its cwd's live transcript activity; unconfirmed guesses are left
    guessed (silence never confirms).
    """
    by_cwd: dict[str, list] = {}  # one transcript glob+stat per unique cwd
    for entry in store.scan().entries:
        claude = entry.get("claude")
        if not claude or claude.get("sid_source") != "guessed":
            continue  # only a guessed sid can be upgraded — skip the FS walk
        cwd = entry["cwd"]
        if cwd not in by_cwd:
            by_cwd[cwd] = transcript_source.list_transcripts(cwd)
        updated = resume.verify_guessed(entry, by_cwd[cwd], now)
        if updated is not None:
            store.write(updated)


def _guessed_upgradable(store: JournalStore, now: str) -> bool:
    """Lock-free pre-scan: would _verify_guessed_sids write anything?

    Keeps the poll path lock-free in the common case; the mutation lock is
    taken only when an upgrade is actually available to write.
    """
    by_cwd: dict[str, list] = {}
    for entry in store.scan().entries:
        claude = entry.get("claude")
        if not claude or claude.get("sid_source") != "guessed":
            continue
        cwd = entry["cwd"]
        if cwd not in by_cwd:
            by_cwd[cwd] = transcript_source.list_transcripts(cwd)
        if resume.verify_guessed(entry, by_cwd[cwd], now) is not None:
            return True
    return False


def _cmd_revive(_args: argparse.Namespace) -> int:
    config = _load_config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr revive: {exc}", file=sys.stderr)
        return 2
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr revive: tmux is required for revival but was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)

    with mutation_lock(sd):
        _verify_guessed_sids(store, _now())  # upgrade guessed sids before reviving
        scan = store.scan()                  # re-scan so revive sees the upgrades
        outcome = reviver.revive_crashed(
            scan.entries, boot, probe, tmux_spawner, store, archive,
            max_strikes=config.get("zombie_strikes"),
            now=_now(),
        )
    for name, reason in scan.problems:
        print(f"crr revive: skipped unreadable journal file {name}: {reason}", file=sys.stderr)
    print(
        f"revived {len(outcome.revived)}, "
        f"gave up {len(outcome.gave_up)}, "
        f"already running {len(outcome.reset)}"
    )
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.remove(JournalStore(sd), args.pid)
    print(res.message)
    return 0 if res.ok else 1


def _cmd_dismiss(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr dismiss: {exc}", file=sys.stderr)
        return 2
    config = _load_config()
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.dismiss(JournalStore(sd), ArchiveStore(sd), boot, probe, args.pid, _now())
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 2


def _tab_spawner(config: cfg.Config):
    """The visible-tab spawner for this host, or None where none applies.

    macOS → Terminal.app / iTerm2. WSL → Windows Terminal (wt.exe). Other
    Linux (desktop) → gnome-terminal / konsole / kitty / wezterm (None when
    headless or none installed). A None spawner makes reopen degrade to
    detached tmux rather than erroring — and the tab step is best-effort
    regardless, so an unverified wt.exe command can never cost the (already
    durable) revival.
    """
    timeout = config.get("interop_timeout_seconds")
    system = platform.system()
    if system == "Darwin":
        kind = tab_spawn.choose(config.get("terminal"), os.environ)
        spawner = tab_spawn.spawner_for(kind, timeout)
        return spawner if spawner.available() else None
    if system == "Linux":
        # WSL first: reach the Windows side via wt.exe (crr runs in the distro).
        if host.is_wsl():
            spawner = tab_spawn_windows.WindowsTerminalSpawner(
                timeout, config.get("wt_profile"), os.environ.get("WSL_DISTRO_NAME")
            )
            if spawner.available():
                return spawner
        # Otherwise a native Linux desktop terminal (None if headless/none).
        return tab_spawn_linux.detect(config.get("terminal"), os.environ, timeout)
    return None


def _cmd_reopen(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr reopen: {exc}", file=sys.stderr)
        return 2
    config = _load_config()
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr reopen: tmux is required for revival but was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    flags = FlagStore(sd)
    with mutation_lock(sd):
        res = ops.reopen(JournalStore(sd), ArchiveStore(sd), tmux_spawner, controller, flags,
                         boot, probe, args.pid, _now(),
                         grace=config.get("close_grace_seconds"),
                         tab_spawner=_tab_spawner(config))
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 2


def _cmd_kick(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr kick: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    with mutation_lock(sd):
        res = ops.kick(JournalStore(sd), controller, flags, boot, probe,
                       args.pid, grace=config.get("close_grace_seconds"))
    print(res.message)
    return 0 if res.ok else 1


def _cmd_close(args: argparse.Namespace) -> int:
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr close: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    with mutation_lock(sd):
        res = ops.close(JournalStore(sd), controller, flags, boot, probe,
                        args.pid, grace=config.get("close_grace_seconds"))
    print(res.message)
    return 0 if res.ok else 1


def _cmd_detmux(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr detmux: {exc}", file=sys.stderr)
        return 2
    config = _load_config()
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr detmux: tmux was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.detmux(JournalStore(sd), ArchiveStore(sd), tmux_spawner, boot, probe, args.pid, _now(),
                         tab_spawner=_tab_spawner(config))
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 1


def _cmd_rescued(_args: argparse.Namespace) -> int:
    """List prior-boot conversations the reviver parked in live tmux,
    awaiting re-homing (Phase-3 restore-prompt UX; see crr.core.rescue)."""
    config = _load_config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr rescued: {exc}", file=sys.stderr)
        return 2
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    live = tmux_spawner.list_sessions() if tmux_spawner.available() else set()
    store = JournalStore(state_dir.state_dir())
    scan = store.scan()
    found = rescue.rescued_sessions(scan.entries, boot.current(), live)
    # Corrupt files are surfaced on stderr, never silently dropped (mirrors
    # _cmd_status/_cmd_revive/_cmd_gc).
    for name, reason in scan.problems:
        print(f"crr rescued: skipped unreadable journal file {name}: {reason}", file=sys.stderr)
    if not found:
        print("no rescued sessions")
        return 0
    for e in found:
        sid8 = e["claude"]["session_id"][:8]
        print(f"#{e['pid']} · {sid8} {e['cwd']} → {e['tmux_session']}")
    print("attach: tmux attach -t <name> · dashboard: Reopen/De-tmux")
    return 0


def make_web_handler(
    sessions_provider: Callable[[], dict],
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    action_provider: Callable[[str, int], tuple[bool, str]] | None = None,
    diagnostics_provider: Callable[[], dict] | None = None,
    poll_seconds: int | None = None,
    version_check_seconds: int | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build an http.server handler bound to the given dependencies.

    Thin adapter: it only marshals bytes to/from ``web.handle_request``
    (the pure core handler that owns routing + the security gate).
    """

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            resp = web.handle_request(
                method, path, self.headers, body,
                sessions_provider=sessions_provider,
                action_provider=action_provider,
                diagnostics_provider=diagnostics_provider,
                allowed_hosts=allowed_hosts,
                allowed_suffixes=allowed_suffixes,
                poll_seconds=poll_seconds,
                version_check_seconds=version_check_seconds,
            )
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            self.wfile.write(resp.body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, *_args) -> None:  # keep the poll path quiet
            pass

    return _Handler


def _select_diag_source():
    """The diagnostics source adapter for this platform (composition root)."""
    if platform.system() == "Darwin":
        return diagnostics_macos
    # WSL without journald: use the Windows/WSL source (WinEvent + OOM
    # forensics). When journald IS present (systemd-in-WSL, or native Linux)
    # it stays the source — it already answers boots/prev_boot_errors that the
    # WSL source degrades.
    if (
        platform.system() == "Linux"
        and host.is_wsl()
        and not diag_source.available()
    ):
        return diagnostics_windows
    return diag_source  # journald (native Linux, or WSL with systemd)


def gather_diagnostics(config: cfg.Config, source: "ports.DiagnosticsSource | None" = None) -> dict:
    """Query the platform diagnostics source, degrading (never aborting).

    Timeout-guarded and lazy (never on the poll path). The per-source
    query/degrade lives in the adapter's ``collect``; this only selects the
    source and wraps its result in the contract-valid /api/diagnostics
    payload (failed sources listed in ``degraded``, never silently empty).
    """
    source = source or _select_diag_source()
    boots, prev, events, degraded = source.collect(config)
    return diag_core.build_payload(
        source=source.SOURCE_NAME, boots=boots, prev_boot_errors=prev,
        host_events=events, degraded=degraded,
    )


def _cmd_diagnose(args: argparse.Namespace) -> int:
    payload = gather_diagnostics(_load_config())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if payload["degraded"]:
        print(f"(degraded sources: {', '.join(payload['degraded'])})", file=sys.stderr)
    # Plain-English verdict first (the "why", before the raw evidence).
    for sentence in payload["summary"]:
        print(f"• {sentence}")
    print()
    print(f"boots on record: {len(payload['boots'])}")
    events = payload["host_events"]
    if events:
        print("host death / shutdown / OOM events in the previous boot:")
        for line in events:
            print(f"  {line}")
    else:
        print("no OOM / shutdown / watchdog events found in the previous boot")
    print(f"previous-boot errors: {len(payload['prev_boot_errors'])} (use --json to see them)")
    return 0


def _cmd_gc(_args: argparse.Namespace) -> int:
    config = _load_config()
    retention = config.get("archive_retention_days")
    sd = state_dir.state_dir()
    archive = ArchiveStore(sd)
    now = _now()
    removed = 0
    with mutation_lock(sd):
        scan = archive.scan()
        for record in scan.records:
            if is_expired(record, now, retention):
                archive.remove(record["entry"]["claude"]["session_id"])
                removed += 1
    for name, reason in scan.problems:
        print(f"crr gc: skipped unreadable archive file {name}: {reason}", file=sys.stderr)
    print(f"gc: removed {removed} archive record(s) older than {retention} days, "
          f"kept {len(scan.records) - removed}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    config = _load_config()
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr web: {exc}", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)
    flags = FlagStore(sd)
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    tab = _tab_spawner(config)

    extract = _tail_facts_extractor(config)

    def provider() -> dict:
        now = _now()
        if _guessed_upgradable(store, now):
            with mutation_lock(sd):
                _verify_guessed_sids(store, now)
        payload = status.assemble_sessions(store.scan().entries, boot, probe, tail_facts=extract)
        contracts.validate_sessions_payload(payload)
        return payload

    def action_provider(op: str, pid: int) -> tuple[bool, str]:
        # Same classifier-gated ops the CLI uses — one implementation — and
        # under the same mutation lock, so a double-tapped button (two
        # handler threads) or a race with the revive timer can't interleave.
        with mutation_lock(sd):
            if op == "remove":
                res = ops.remove(store, pid)
            elif op == "dismiss":
                res = ops.dismiss(store, archive, boot, probe, pid, _now())
            elif op == "reopen":
                res = ops.reopen(store, archive, tmux_spawner, controller, flags, boot, probe,
                                  pid, _now(), grace=config.get("close_grace_seconds"),
                                  tab_spawner=tab)
            elif op == "close":
                res = ops.close(store, controller, flags, boot, probe, pid,
                                 grace=config.get("close_grace_seconds"))
            elif op == "kick":
                res = ops.kick(store, controller, flags, boot, probe, pid,
                                grace=config.get("close_grace_seconds"))
            elif op == "detmux":
                res = ops.detmux(store, archive, tmux_spawner, boot, probe, pid, _now(), tab_spawner=tab)
            else:
                return False, f"unknown op {op}"
        return res.ok, res.message

    def diagnostics_provider() -> dict:
        return gather_diagnostics(config)  # lazy: only on panel open, never on poll

    # Host allowlist: loopback + this host's name + tailnet suffix + any
    # config.toml extras.
    allowed = {"127.0.0.1", "localhost", "[::1]", socket.gethostname().lower()}
    allowed.update(h.lower() for h in config.get("host_allowlist_extras"))
    handler = make_web_handler(
        provider, allowed, (".ts.net",), action_provider, diagnostics_provider,
        poll_seconds=config.get("dashboard_poll_seconds"),
        version_check_seconds=config.get("version_check_seconds"),
    )

    # Bind loopback ONLY; the tailnet (or a user proxy) is the auth boundary.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"crr web: serving on http://127.0.0.1:{args.port}/ (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_systemd(args: argparse.Namespace) -> int:
    if args.install and args.uninstall:
        print("crr systemd: --install and --uninstall are mutually exclusive", file=sys.stderr)
        return 2
    config = _load_config()
    crr_bin = _resolve_crr_bin(args.crr_bin)
    path, missing = systemd.resolve_service_path(crr_bin)
    # XDG_STATE_HOME baked so the service watches the SAME state dir the shims
    # write to (state_dir() is <XDG_STATE_HOME>/crr; bake its parent).
    state_home = str(state_dir.state_dir().parent)
    interval = config.get("watchdog_interval_seconds")
    units = {
        systemd.SERVICE_NAME: systemd.revive_service_unit(crr_bin, path, state_home),
        systemd.TIMER_NAME: systemd.revive_timer_unit(interval),
        systemd.WEB_SERVICE_NAME: systemd.web_service_unit(crr_bin, path, state_home, args.port),
    }

    if missing:
        print(
            "crr systemd: WARNING — not found on PATH: "
            f"{', '.join(missing)}; revived sessions will fail on exec until these resolve",
            file=sys.stderr,
        )

    if args.uninstall:
        ud = systemd.unit_dir(Path.home())
        ok = _run_commands(systemd.disable_commands(), "systemd")
        for name in (systemd.SERVICE_NAME, systemd.TIMER_NAME, systemd.WEB_SERVICE_NAME):
            (ud / name).unlink(missing_ok=True)
        if not ok:
            print("crr systemd: unit files removed, but disabling FAILED (see above)",
                  file=sys.stderr)
            return 1
        print(f"uninstalled watchdog + dashboard units from {ud}")
        return 0

    if args.install:
        ud = systemd.unit_dir(Path.home())
        systemd.write_units(ud, units)
        if not _run_commands(systemd.enable_commands(), "systemd"):
            print(f"crr systemd: units written to {ud} but enabling FAILED (see above); "
                  "the watchdog/dashboard are NOT running", file=sys.stderr)
            return 1
        print(f"installed watchdog + dashboard units to {ud} and enabled them")
        return 0

    # Default: print for inspection (no changes to the user manager).
    for name, text in units.items():
        print(f"# ---- {name} ----")
        print(text)
    print("# Install with:  crr systemd --install")
    print("# (writes the units above to ~/.config/systemd/user/ and runs:)")
    for cmd in systemd.enable_commands():
        print("#   " + " ".join(cmd))
    return 0


def _cmd_launchd(args: argparse.Namespace) -> int:
    if args.install and args.uninstall:
        print("crr launchd: --install and --uninstall are mutually exclusive", file=sys.stderr)
        return 2
    config = _load_config()
    crr_bin = _resolve_crr_bin(args.crr_bin)
    path, missing = launchd.resolve_service_path(crr_bin)
    # State dir is NOT baked: state_dir.resolve("Darwin", …) is env-independent,
    # so the agent resolves the same dir the shims write to via HOME alone.
    interval = config.get("watchdog_interval_seconds")
    agents = {
        launchd.REVIVE_PLIST: launchd.revive_agent_plist(crr_bin, path, interval),
        launchd.WEB_PLIST: launchd.web_agent_plist(crr_bin, path, args.port),
    }

    if missing:
        print(
            "crr launchd: WARNING — not found on PATH: "
            f"{', '.join(missing)}; revived sessions will fail on exec until these resolve",
            file=sys.stderr,
        )

    if args.uninstall:
        ad = launchd.agent_dir(Path.home())
        # Unload FIRST, then remove the plists — launchctl needs the plist
        # present on disk to unload it.
        ok = _run_commands(launchd.disable_commands(ad), "launchd")
        for name in (launchd.REVIVE_PLIST, launchd.WEB_PLIST):
            (ad / name).unlink(missing_ok=True)
        if not ok:
            print("crr launchd: agent files removed, but unloading FAILED (see above)",
                  file=sys.stderr)
            return 1
        print(f"uninstalled watchdog + dashboard agents from {ad}")
        return 0

    if args.install:
        ad = launchd.agent_dir(Path.home())
        launchd.write_agents(ad, agents)
        if not _run_commands(launchd.enable_commands(ad), "launchd"):
            print(f"crr launchd: agents written to {ad} but loading FAILED (see above); "
                  "the watchdog/dashboard are NOT running", file=sys.stderr)
            return 1
        print(f"installed watchdog + dashboard agents to {ad} and loaded them")
        return 0

    # Default: print for inspection (no changes to the launchd user domain).
    ad = launchd.agent_dir(Path.home())
    for name, text in agents.items():
        print(f"# ---- {name} ----")
        print(text)
    print("# Install with:  crr launchd --install")
    print(f"# (writes the plists above to {ad}/ and runs:)")
    for cmd in launchd.enable_commands(ad):
        print("#   " + " ".join(cmd))
    return 0


def _cmd_schtasks(args: argparse.Namespace) -> int:
    if args.install and args.uninstall:
        print("crr schtasks: --install and --uninstall are mutually exclusive", file=sys.stderr)
        return 2
    config = _load_config()
    crr_bin = _resolve_crr_bin(args.crr_bin)
    distro = os.environ.get("WSL_DISTRO_NAME")
    interval = config.get("watchdog_interval_seconds")
    cmds = [
        scheduled_task.create_revive_task_command(crr_bin, interval, distro),
        scheduled_task.create_web_task_command(crr_bin, args.port, distro),
    ]

    if args.uninstall:
        if shutil.which("schtasks.exe") is None:
            print("crr schtasks: schtasks.exe not found — not a Windows/WSL host; "
                  "nothing was removed", file=sys.stderr)
            return 2
        if not _run_commands(scheduled_task.delete_task_commands(), "schtasks"):
            print("crr schtasks: task removal FAILED (see above)", file=sys.stderr)
            return 1
        print("removed watchdog + dashboard Scheduled Tasks")
        return 0

    if args.install:
        if shutil.which("schtasks.exe") is None:
            print("crr schtasks: schtasks.exe not found — not a Windows/WSL host; "
                  "nothing was created", file=sys.stderr)
            return 2
        if not _run_commands(cmds, "schtasks"):
            print("crr schtasks: task creation FAILED (see above)", file=sys.stderr)
            return 1
        print("created watchdog + dashboard Scheduled Tasks")
        return 0

    # Default: print the schtasks commands for inspection (no changes made).
    for cmd in cmds:
        print(" ".join(_quote(part) for part in cmd))
    print("# Install with:  crr schtasks --install   (WSL host; runs the above)")
    print("# Remove with:")
    for cmd in scheduled_task.delete_task_commands():
        print("#   " + " ".join(_quote(part) for part in cmd))
    return 0


def _quote(part: str) -> str:
    """Quote a schtasks argv part for display when it contains spaces."""
    return f'"{part}"' if " " in part else part


def _run_commands(cmds: list[list[str]], label: str) -> bool:
    """Run each argv; surface every failure on stderr. True iff all exited 0.

    [lesson] a swallowed exit code turned hard failures into green
    checkmarks — install/uninstall must report what actually happened.
    """
    ok = True
    for cmd in cmds:
        shown = " ".join(cmd)
        try:
            result = subprocess.run(cmd, check=False)
        except OSError as exc:
            print(f"crr {label}: {shown} failed to run: {exc}", file=sys.stderr)
            ok = False
            continue
        if result.returncode != 0:
            print(f"crr {label}: {shown} exited {result.returncode}", file=sys.stderr)
            ok = False
    return ok


def _cmd_config(args: argparse.Namespace) -> int:
    if not args.effective:
        print("usage: crr config --effective", file=sys.stderr)
        return 2
    config = _load_config()
    for key, (value, origin) in sorted(config.effective().items()):
        print(f"{key} = {value!r}  ({origin})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
