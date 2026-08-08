"""crr command-line entry point — the composition root.

This is the ONE module allowed to import both ``crr.core`` and
``crr.adapters`` (the sole exception declared in .importlinter). Its job
is wiring: pick platform adapters, hand them to core, dispatch
subcommands. Business logic belongs in core, not here.

Phase 1 (headless Linux) is implemented: status, revive, session ops
(reopen/dismiss/remove/kick/close/untrack/untmux/retrack), discover
(surface + adopt untracked transcripts, T-C), adopt (`--takeover` safely
stops and adopts a still-live `claude --resume`), recall (print-only
transcript search), diagnose, gc, the web dashboard, the systemd watchdog,
and the shim-facing hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import select
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from crr import __version__
from crr.adapters import boot_identity  # composition root may import adapters
from crr.adapters import diagnostics as diag_source
from crr.adapters import diagnostics_macos
from crr.adapters import launchd, process_probe, state_dir, systemd, tab_spawn, tmux, transcript_source
from crr.adapters import diagnostics_windows, host, scheduled_task, tab_spawn_linux, tab_spawn_windows
from crr.adapters.locking import mutation_lock
from crr.core import config as cfg  # ...and core
from crr.core import bridge, bridge_kicks, classifier, contracts, discovery, exclusions, ops, ports, rescue, resume, reviver, settings, status, takeover, transcript, web, whoami
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
    """A tail_facts(entry)->{last_prompt, model, last_active, transcript_bytes,
    bridge_seen, bridge_since, ...} closure for assemble_sessions.

    One backward transcript read per card yields all these facts. Only
    called for claude-bearing entries (assemble_sessions filters the
    rest), so entry["claude"] is always present here.

    ``remote_control_watch`` (review fix-wave 2026-08-07, FIX 2 — IMPORTANT)
    gates the bridge scan itself, not just the watchdog's kick step: False
    passes ``bridge_scan_lines=0``, which short-circuits
    ``read_tail_facts``'s backward walk for the bridge marker, so no scan
    cost is paid on every poll and the card can never show a stale
    ``"dropped"``/``"ok"`` computed from a feature the user turned off.
    This is the single choke point every card-building call site
    (``_cmd_status``, ``_cmd_web``'s provider, ``_whoami_card``) reads
    through, so gating here covers detection + the badge + the scan cost at
    once.

    With watching off the card reads ``"unknown"``, not ``"off"`` (#33).
    ``off`` is a positive claim — *Remote Control was never enabled on this
    session* — and crr cannot support it while declining to look. The
    honest statement is that it does not know, which is exactly what
    turning the watch off asked for.
    """
    cap = config.get("last_prompt_display_cap")
    model_tail_lines = config.get("model_tail_lines")
    bridge_scan_lines = config.get("bridge_scan_lines") if config.get("remote_control_watch") else 0
    return lambda entry: transcript_source.read_tail_facts(
        entry["claude"]["session_id"], cap,
        model_tail_lines=model_tail_lines, bridge_scan_lines=bridge_scan_lines,
    )


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

    utk = sub.add_parser(
        "untrack", aliases=["detmux"],
        help="stop tracking a session — archive it and re-home into a visible tab",
    )
    utk.add_argument("pid", type=int)
    utk.set_defaults(func=_cmd_untrack)

    utm = sub.add_parser(
        "untmux", help="kill a parked tmux session and relaunch claude --resume in a visible tab"
    )
    utm.add_argument("pid", type=int)
    utm.set_defaults(func=_cmd_untmux)

    rtk = sub.add_parser(
        "retrack",
        help="restore untracked/detmuxed sessions back into crr's management",
    )
    rtk.add_argument("--last", type=int, default=None,
                      help="retrack the N most recently untracked sessions (default 10)")
    rtk.add_argument("--sid", default=None, help="retrack this specific archived session id")
    rtk.set_defaults(func=_cmd_retrack)

    dsc = sub.add_parser(
        "discover",
        help="list transcripts on disk crr hasn't journaled yet (T-C)",
    )
    dsc.add_argument(
        "--adopt", default=None, metavar="SID",
        help="adopt this discoverable session id into crr's journal (recoverable, not live)",
    )
    dsc.add_argument(
        "-n", dest="limit", type=int, default=20, metavar="N",
        help="show the N most recent (default 20; reading every transcript is slow)",
    )
    dsc.add_argument(
        "--all", action="store_true",
        help="list every discoverable transcript (slow on a machine with thousands)",
    )
    dsc.set_defaults(func=_cmd_discover)

    who = sub.add_parser(
        "whoami",
        help="which crr session is this shell/claude running inside?",
    )
    who.add_argument("--json", action="store_true", help="machine-readable output")
    who.set_defaults(func=_cmd_whoami)

    hk = sub.add_parser(
        "hook",
        help="[hooks] emit crr context for a Claude Code hook event",
    )
    hk.add_argument("event", choices=["session-start"])
    hk.set_defaults(func=_cmd_hook)

    adp = sub.add_parser(
        "adopt",
        help="adopt a discoverable session id (--takeover stops a still-live one first)",
    )
    adp.add_argument("sid", help="the session id to adopt")
    adp.add_argument(
        "--takeover", action="store_true",
        help="stop the live 'claude --resume SID' first (waits for a safe turn "
             "boundary), then adopt — destructive, default off",
    )
    adp.add_argument(
        "--wait", type=float, default=None,
        help="max seconds to wait for a safe boundary before refusing "
             "(default: config takeover_max_wait_seconds)",
    )
    adp.set_defaults(func=_cmd_adopt)

    rescued = sub.add_parser(
        "rescued",
        help="list conversations rescued from a previous boot (awaiting re-home)",
    )
    rescued.set_defaults(func=_cmd_rescued)

    resc_chk = sub.add_parser(
        "rescue-check",
        help="[shim] once per boot, offer to re-home rescued conversations",
    )
    resc_chk.set_defaults(func=_cmd_rescue_check)

    diag = sub.add_parser("diagnose", help="explain why the previous boot / sessions may have died")
    diag.add_argument("--json", action="store_true", help="emit the /api/diagnostics payload")
    diag.set_defaults(func=_cmd_diagnose)

    gc = sub.add_parser("gc", help="drop archive records past the retention window")
    gc.set_defaults(func=_cmd_gc)

    arch = sub.add_parser("archive", help="inspect archived (revival-preserved) sessions")
    arch.add_argument(
        "--list",
        action="store_true",
        help="print every archived record (reason, archived_at, sid8, cwd)",
    )
    arch.set_defaults(func=_cmd_archive)

    kicks = sub.add_parser(
        "kicks", help="inspect the watchdog's auto-kick history (why a session was restarted)")
    kicks.add_argument(
        "--list", action="store_true",
        help="print every recorded auto-kick attempt (when, why, thresholds, outcome)",
    )
    kicks.set_defaults(func=_cmd_kicks)

    w = sub.add_parser("web", help="serve the tailnet dashboard (loopback only)")
    w.add_argument("--port", type=int, default=None,
                   help="dashboard bind port (default: config dashboard_port = 8377)")
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
    sysd.add_argument("--port", type=int, default=None,
                      help="dashboard port to bake into crr-web.service "
                           "(default: config dashboard_port = 8377)")
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
    lncd.add_argument("--port", type=int, default=None,
                      help="dashboard port to bake into the web agent "
                           "(default: config dashboard_port = 8377)")
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
    sch.add_argument("--port", type=int, default=None,
                     help="dashboard port to bake into the web task "
                          "(default: config dashboard_port = 8377)")
    sch.set_defaults(func=_cmd_schtasks)

    rec = sub.add_parser(
        "recall",
        help="search a session's transcript for earlier conversation (print-only)",
    )
    rec.add_argument("--pid", type=int, default=None, help="resolve the sid from this pid's journal entry")
    rec.add_argument("--sid", default=None, help="search this session id's transcript directly")
    rec.add_argument("--all", action="store_true",
                      help="search every transcript in the cwd's project dir")
    rec.add_argument("--cwd", default=None,
                      help="cwd for --all when --pid isn't given (or to override --pid's cwd)")
    rec.add_argument("-n", type=int, default=None, dest="limit",
                      help="max matches to print, most-recent-first (default: config recall_match_cap)")
    rec.add_argument("query", help="case-insensitive substring to search for")
    rec.set_defaults(func=_cmd_recall)

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

    rca = sub.add_parser(
        "remote-control-args",
        help="[shim] print the args (one per line) that enable Remote Control on this "
             "launch, or nothing when disabled/untracked",
    )
    rca.add_argument("--pid", type=int, required=True, help="the shell's pid")
    rca.set_defaults(func=_cmd_remote_control_args)

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
    rendered = (template
                .replace("@CRR_BIN@", _resolve_crr_bin(args.crr_bin))
                .replace("@CRR_VERSION@", __version__)
                .replace("@CRR_DEFAULTS_V@", str(cfg.CONFIG_DEFAULTS_VERSION)))
    print(rendered, end="")
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
        f"diagnostics v{contracts.DIAGNOSTICS_CONTRACT_VERSION}, "
        f"archive v{contracts.ARCHIVE_CONTRACT_VERSION}, "
        f"config-defaults v{cfg.CONFIG_DEFAULTS_VERSION}, page v{web.PAGE_VERSION}"
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

    # Config. Doctor's own parse attempt doubles as the source of `config`
    # for the systemctl check below — a second, independent _load_config()
    # call here would print its own "ignoring bad config" line on top of
    # this section's structured [WARN], the same fact said twice.
    toml_path = sd / "config.toml"
    if toml_path.is_file():
        try:
            config = cfg.Config(cfg.load_toml_overrides(toml_path))
            _check("config.toml", True, str(toml_path))
        except (cfg.ConfigError, ValueError, OSError) as exc:
            config = cfg.Config()  # same fallback _load_config() would use
            _check("config.toml", False, f"{toml_path}: {exc}")
    else:
        config = cfg.Config()
        print(f"  [ok  ] config.toml — none (using defaults); crr config --effective to view")

    # systemd units (installed? enabled?).
    ud = systemd.unit_dir(Path.home())
    for unit in (systemd.TIMER_NAME, systemd.WEB_SERVICE_NAME):
        installed = (ud / unit).is_file()
        enabled = ""
        if installed and shutil.which("systemctl"):
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "is-enabled", unit],
                    capture_output=True, text=True,
                    timeout=config.get("interop_timeout_seconds"),
                )
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
    settings_store = settings.SettingsStore(sd)
    payload = status.assemble_sessions(
        scan.entries,
        boot,
        probe,
        tail_facts=_tail_facts_extractor(config),
        context_tight_fraction=config.get("context_tight_fraction"),
        context_compact_fraction=config.get("context_compact_fraction"),
        bridge_stale_records=config.get("bridge_stale_records"),
        autokick_config_default=config.get("remote_control_autokick"),
        autokick_global_override=settings_store.effective_global_autokick(),
        autokick_session_overrides=settings_store.read_session_overrides(),
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
        if card["duplicate_group"]:
            # A guessed duplicate is a weaker claim than a verified/injected
            # one — collapsing both into the same [dup] tag would hide that
            # difference (audit P3: confidence travels with the data).
            dup = " [dup? guessed]" if card["sid_source"] == "guessed" else " [dup]"
        else:
            dup = ""
        # injected is the certain norm; only a non-injected sid_source is
        # worth the extra characters on an otherwise compact line.
        sid_tag = f" sid:{card['sid_source']}" if card["sid_source"] != "injected" else ""
        model = f" {card['model']}" if card["model"] else ""  # omitted when unknown
        print(f"#{card['pid']} · {card['sid8']} [{card['state']}]{model} {card['cwd']}{dup}{sid_tag}")


def _cmd_recall(args: argparse.Namespace) -> int:
    """F1: query-scoped, capped transcript search — print-only, never
    re-injects into a live session (feeding recalled text back in would
    add tokens and could trigger the very compaction being worked around).
    """
    if args.pid is not None and args.sid is not None:
        print("crr recall: pass only one of --pid / --sid", file=sys.stderr)
        return 2
    if args.sid is not None and args.all:
        # --sid names one transcript; --all means "every transcript in the
        # cwd". Combined, --sid would be silently ignored and the scope
        # quietly widened to --all — reject rather than widen scope behind
        # the user's back.
        print("crr recall: --sid cannot be combined with --all", file=sys.stderr)
        return 2
    if args.pid is None and args.sid is None and not args.all:
        print("crr recall: specify --pid, --sid, or --all", file=sys.stderr)
        return 2
    if not args.query.strip():
        # An empty/whitespace-only query is a substring of every real turn
        # — reject it rather than silently matching the whole transcript.
        print("crr recall: query must not be empty", file=sys.stderr)
        return 2
    if args.sid is not None and not contracts.valid_session_id(args.sid):
        # A user-typed --sid may be junk (mirrors claude-launch's guard on a
        # user-typed --session-id): reject before it reaches find_transcript's
        # glob rather than treating an arbitrary string as a glob pattern.
        print(f"crr recall: {args.sid!r} is not a valid session id", file=sys.stderr)
        return 2

    config = _load_config()
    cap = config.get("recall_snippet_cap")
    limit = args.limit if args.limit is not None else config.get("recall_match_cap")

    sd = state_dir.state_dir()
    store = JournalStore(sd)

    entry = None
    if args.pid is not None:
        try:
            entry = store.read(args.pid)
        except (KeyError, contracts.ContractError):
            print(f"crr recall: no journal entry for pid {args.pid}", file=sys.stderr)
            return 2
        if not args.all and entry.get("claude") is None:
            print(f"crr recall: pid {args.pid} has no claude session", file=sys.stderr)
            return 2

    if args.all:
        cwd = args.cwd if args.cwd is not None else (entry["cwd"] if entry is not None else None)
        if cwd is None:
            print("crr recall: --all needs --cwd or --pid to derive one", file=sys.stderr)
            return 2
        matches = transcript_source.search_cwd(cwd, args.query, cap=cap)
    else:
        sid = args.sid if args.sid is not None else entry["claude"]["session_id"]
        matches = transcript_source.search_transcript(sid, args.query, cap=cap)

    if not matches:
        print(f"no matches for '{args.query}'")
        return 0

    # Most-recent-first, capped — shared with the dashboard recall provider
    # (transcript.rank_matches), so the CLI and web can't drift on ordering.
    ordered = transcript.rank_matches(matches, limit=limit)
    for m in ordered:
        ts = f" {m['timestamp']}" if m["timestamp"] else ""
        sid_tag = f" {m['session_id'][:8]}" if "session_id" in m else ""
        print(f"[{m['role']}]{ts}{sid_tag} {m['text']}")
    return 0


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


def _cmd_remote_control_args(args: argparse.Namespace) -> int:
    """[shim] The argv (one token per line, so an unquoted shell/fish
    command substitution splits it into the right words — see the shims)
    that enables Claude Code's Remote Control on this launch, or nothing.

    Called by the shim immediately before every launch/resume of claude
    (`--remote-control` is per-invocation, not sticky, so every launch has
    to ask fresh). Prints nothing when disabled or the shell is untracked
    (no cwd to derive a name from) — and on ANY error: a Remote Control
    label must never be able to break a claude launch.
    """
    try:
        config = _load_config()
        if not config.get("remote_control"):
            return 0
        entry = JournalStore(state_dir.state_dir()).read(args.pid)
        for token in reviver.remote_control_flag_argv(entry.get("cwd", "")):
            print(token)
    except Exception:
        return 0
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
            remote_control_enabled=config.get("remote_control"),
        )
    for name, reason in scan.problems:
        print(f"crr revive: skipped unreadable journal file {name}: {reason}", file=sys.stderr)
    if outcome.skipped:
        print(
            "crr revive: tmux state unknown — pass skipped (no strikes accrued)",
            file=sys.stderr,
        )
        # Exit 0, not nonzero: this runs unattended as a systemd oneshot, and
        # a transient tmux query failure flapping the unit into failed state
        # would spam systemd failure alerts for something that resolves
        # itself next pass. The stderr note above is the honest signal here,
        # not the exit code.
        return 0
    print(
        f"revived {len(outcome.revived)}, "
        f"gave up {len(outcome.gave_up)}, "
        f"already running {len(outcome.reset)}"
    )
    if outcome.gave_up:
        print(f"gave up: {outcome.gave_up}")

    # --- separate pass: reconnect LIVE sessions whose Remote Control
    # bridge has dropped (spec 2026-08-07, Slice 2). Deliberately AFTER and
    # APART from the crashed-session revival above — that pass raises the
    # dead; this one restarts something still running, a materially more
    # dangerous action that deserves its own gate, its own re-scan (the
    # revival above may have changed journal state), and its own reasoning
    # trail. See `_kick_dropped_bridges` for the full guard chain.
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    settings_store = settings.SettingsStore(sd)
    _kick_dropped_bridges(
        store.scan().entries, boot, probe, config, settings_store, store, sd,
        controller, flags,
    )
    return 0


def _kick_dropped_bridges(
    entries: Sequence[Mapping[str, Any]],
    boot,
    probe,
    config: cfg.Config,
    settings_store: "settings.SettingsStore",
    store: JournalStore,
    sd: Path,
    controller,
    flags: FlagStore,
    *,
    read_tail_facts=transcript_source.read_tail_facts,
    read_takeover_signal=transcript_source.read_takeover_signal,
    kick=ops.kick,
    clock=time.time,
    kick_store: "bridge_kicks.KickHistoryStore | None" = None,
) -> None:
    """Watchdog step (spec 2026-08-07, Slice 2): restart a LIVE session
    whose Remote Control bridge has dropped, so the relaunch (which always
    carries ``--remote-control``) reconnects it on the phone.

    THIS KILLS LIVE PROCESSES. Every guard below is load-bearing and
    checked in this exact order; every skip is printed with its reason —
    a watchdog that silently restarts things is unauditable:

      1. ``remote_control_watch`` must be on — the whole step's gate.
      2. the session must classify LIVE — never CRASHED (that is the
         reviver's job, above), never GHOST (no controlling terminal to
         reconnect on).
      3. its bridge state (``bridge.bridge_state`` over the same tail-facts
         walk Slice 1 added) must be ``"dropped"`` — not ``"off"`` (never
         enabled) and not ``"ok"``. A transition TO ``"ok"`` resets this
         sid's kick history (see ``kick_store`` below) — that is the only
         thing that resets it; nothing here does it on a timer.
      4. ``settings.autokick_for`` must resolve True for this sid, given
         the config default, the dashboard's global override, and this
         session's own override.
      5. ``takeover.ready_to_take_over`` must say the transcript is quiet
         at a completed assistant turn — a kick mid-turn destroys the
         in-flight turn, so a not-yet-ready session is simply left for the
         next pass rather than kicked now.
      6. ``bridge_kicks.kick_eligible`` (review fix-wave 2026-08-07, FIX 1
         — CRITICAL): this sid must be past its cooldown AND under its
         attempt cap. Without this the pass is stateless across sweeps —
         a FAILED reconnect (host briefly offline, auth expired, Remote
         Control unavailable) does not advance the bridge marker, so every
         guard above clears again next pass, re-kicking the same sid every
         ``watchdog_interval_seconds`` forever. ``kick_store`` persists the
         per-sid attempt count + last-kick time in the state dir
         (``crr.core.bridge_kicks.KickHistoryStore``) so this guard has
         memory across sweeps, not just within one.

    Only once every guard clears does ``kick`` run, and only then under
    ``mutation_lock`` — mirroring ``crr kick``'s own locking. Everything
    before that is lock-free, so a slow transcript scan on one session
    never stalls the others or the dashboard. ``kick_store.record_kick``
    happens inside the same lock, right after ``kick`` returns — it is
    part of the same mutating step, not a separate one.

    Fails CLOSED (mirrors the settings-store guard) when ``kick_store`` is
    degraded: a corrupt kick-history file must not silently read as "no
    history", which would erase the very protection FIX 1 exists to add.

    Two journal entries CAN carry the same session id (duplicate_group on
    the card; the crashed-session revival above has its own analogous
    guard — see ``test_duplicate_sids_spawn_one_session_not_two``). Such
    entries share a transcript, so they'd share every verdict above: if
    one qualifies, both would, deterministically double-kicking one live
    conversation. ``kicked_sids`` makes this pass kick each sid at most
    once per sweep.
    """
    if not config.get("remote_control_watch"):
        return

    # Fail CLOSED on an unreadable settings file. A corrupt store reads as
    # "no overrides", which would silently drop every per-session opt-out
    # and make a session the user explicitly excluded eligible again — and
    # the action here restarts a LIVE process. An absent file is not
    # degraded (never configured is the normal case).
    if settings_store.is_degraded():
        print("crr revive: settings file unreadable — not auto-kicking anything "
              "(per-session opt-outs cannot be honoured); fix or delete "
              f"{state_dir.state_dir() / settings.FILENAME}", file=sys.stderr)
        return

    kick_store = kick_store or bridge_kicks.KickHistoryStore(sd)
    if kick_store.is_degraded():
        print("crr revive: kick history file unreadable — not auto-kicking anything "
              "(the restart-loop cooldown/cap cannot be honoured); fix or delete "
              f"{state_dir.state_dir() / bridge_kicks.FILENAME}", file=sys.stderr)
        return

    global_override = settings_store.read_global_autokick()
    config_default = config.get("remote_control_autokick")
    bridge_stale_records = config.get("bridge_stale_records")
    bridge_scan_lines = config.get("bridge_scan_lines")
    model_tail_lines = config.get("model_tail_lines")
    cap = config.get("last_prompt_display_cap")
    idle_window = config.get("takeover_idle_seconds")
    grace = config.get("close_grace_seconds")
    cooldown_seconds = config.get("bridge_kick_cooldown_seconds")
    max_attempts = config.get("bridge_kick_max_attempts")
    kicked_sids: set[str] = set()

    for entry in entries:
        if entry.get("claude") is None:
            continue
        pid = entry["pid"]
        sid = entry["claude"]["session_id"]
        sid8 = sid[:8]
        if sid in kicked_sids:
            continue  # already kicked once this sweep via a duplicate entry

        if classifier.classify(entry, boot, probe) != classifier.LIVE:
            continue  # CRASHED is the reviver's job above; GHOST has no terminal to reconnect

        facts = read_tail_facts(
            sid, cap, model_tail_lines=model_tail_lines, bridge_scan_lines=bridge_scan_lines,
        )
        remote_control = bridge.bridge_state(
            facts["bridge_since"], facts["bridge_seen"], stale_after=bridge_stale_records,
        )
        # Only "dropped" is actionable. "unknown" (#33) in particular must
        # never be: it means the scan did not finish looking, so there is no
        # evidence of a drop to act on — and acting would SIGTERM a live
        # process on the strength of an absence. It also must not reset the
        # attempt counter: only a confirmed "ok" is evidence a reconnect
        # worked, and an unknown is by definition not a confirmation.
        if remote_control != "dropped":
            if remote_control == "ok":
                # The confirmed signal that a prior kick actually worked (or
                # the bridge never needed one) — the ONLY thing that resets
                # the attempt counter, per bridge_kicks's docstring.
                kick_store.reset(sid)
            continue  # "off" (never enabled) or "ok" — nothing to reconnect

        session_override = settings_store.read_session_autokick(sid)
        if not settings.autokick_for(
            config_default=config_default,
            global_override=global_override,
            session_override=session_override,
        ):
            global_resolved = config_default if global_override is None else global_override
            reason = "autokick globally off" if not global_resolved else "autokick opted out for this session"
            print(f"crr revive: skipping {sid8} (dropped bridge, {reason})")
            continue

        sig = read_takeover_signal(sid)
        seconds_idle = clock() - sig["mtime"]
        if not takeover.ready_to_take_over(seconds_idle, sig["tail_kind"], idle_window=idle_window):
            print(f"crr revive: skipping {sid8} (dropped bridge, mid-turn — waiting for a clean boundary)")
            continue

        now = clock()
        eligible, ineligible_reason = bridge_kicks.kick_eligible(
            attempts=kick_store.attempts(sid), last_kick_ts=kick_store.last_kick_ts(sid),
            now=now, cooldown_seconds=cooldown_seconds, max_attempts=max_attempts,
        )
        if not eligible:
            print(f"crr revive: skipping {sid8} (dropped bridge, {ineligible_reason})")
            continue

        with mutation_lock(sd):
            res = None
            try:
                res = kick(store, controller, flags, boot, probe, pid, grace=grace)
            finally:
                # Record the ATTEMPT, not the outcome — even if `kick` raises
                # (a subprocess/signal error, not just an OpResult(False,...)),
                # the attempt must count. Recording only on a normal return
                # would let an exception silently skip the counter, reopening
                # the exact restart-loop hole this guard exists to close: the
                # next pass, with no recorded attempt and no cooldown, would
                # retry immediately.
                #
                # `observation` is the lineage (#35): the state that justified
                # THIS kick, plus the thresholds in force. Without the
                # thresholds, changing bridge_stale_records later silently
                # rewrites the history of every decision taken under the old
                # one. Recorded here rather than after `kick` returns for the
                # same reason the counter is: it must survive an exception.
                kick_store.record_kick(sid, now, observation={
                    "pid": pid,
                    "bridge_since": facts["bridge_since"],
                    "bridge_seen": facts["bridge_seen"],
                    "stale_after": bridge_stale_records,
                    "scan_lines": bridge_scan_lines,
                    "cooldown_seconds": cooldown_seconds,
                    "max_attempts": max_attempts,
                    "config_defaults_version": cfg.CONFIG_DEFAULTS_VERSION,
                })
                if res is not None:
                    kick_store.record_outcome(sid, ok=res.ok, message=res.message)
        kicked_sids.add(sid)  # at most one kick attempt per sid per sweep, success or not
        outcome_word = "kicked" if res.ok else "kick failed for"
        print(f"crr revive: {outcome_word} {sid8} (dropped bridge): {res.message}")


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
                         remote_control=config.get("remote_control"),
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


def _cmd_untrack(args: argparse.Namespace) -> int:
    """Handler for both the primary ``untrack`` command and its deprecated
    ``detmux`` alias (terminology change: detmux -> untrack)."""
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr untrack: {exc}", file=sys.stderr)
        return 2
    config = _load_config()
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr untrack: tmux was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.detmux(JournalStore(sd), ArchiveStore(sd), tmux_spawner, boot, probe, args.pid, _now(),
                         tab_spawner=_tab_spawner(config))
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 1


def _cmd_untmux(args: argparse.Namespace) -> int:
    try:
        boot = boot_identity.detect()
    except NotImplementedError as exc:
        print(f"crr untmux: {exc}", file=sys.stderr)
        return 2
    config = _load_config()
    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    if not tmux_spawner.available():
        print("crr untmux: tmux was not found", file=sys.stderr)
        return 2
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    sd = state_dir.state_dir()
    with mutation_lock(sd):
        res = ops.untmux(JournalStore(sd), ArchiveStore(sd), tmux_spawner, boot, probe, args.pid, _now(),
                         remote_control=config.get("remote_control"),
                         tab_spawner=_tab_spawner(config))
    print(res.message, file=sys.stdout if res.ok else sys.stderr)
    return 0 if res.ok else 1


def _recent_untracked_records(records: list[dict], n: int | None) -> list[dict]:
    """The N most-recent untracked/detmuxed records out of ``records``
    (an ``ArchiveScan.records`` list), newest first. ``n=None`` returns all
    of them (the dashboard pages the full list itself).

    Shared by `crr retrack --last` and the web dashboard's /api/untracked
    provider — one implementation, so the two surfaces can't drift on what
    "recently untracked" means. Takes records rather than an ArchiveStore so
    the caller decides what to do with ``ArchiveScan.problems`` (a corrupt
    archive file must be surfaced, not silently dropped here).
    """
    candidates = [r for r in records if r["reason"] in ("untracked", "detmuxed")]
    ordered = sorted(candidates, key=lambda r: r["archived_at"], reverse=True)
    return ordered if n is None else ordered[:n]


def _untracked_view(record: dict, cap: int, model_tail_lines: int) -> dict:
    """The /api/untracked (and dashboard retrack panel) shape for one archive
    record, including a transcript-read ``last_prompt``.

    The wrapped journal entry never carries ``last_prompt`` itself (that's a
    status-CARD field, contracts.py), but the untracked session's transcript
    is still on disk, so we read the real last prompt from it — parity with
    the discoverable panel (``_discoverable_rows``), so the user can tell one
    retrack candidate from another. One ``read_tail_facts`` per record; safe
    here because ``/api/untracked`` is a lazy panel (opened on demand, capped
    at 10 records), never the poll path. A gone/unreadable transcript degrades
    to an honest ``""``, never an error.
    """
    entry = record["entry"]
    sid = entry["claude"]["session_id"]
    # This view has no use for bridge facts, so bridge_scan_lines=0 keeps
    # the walk's early exit exactly as cheap as before Slice 1 added the
    # bridge search (0 means the bridge window is never "in", so the break
    # clause is satisfied immediately and bridge_seen/bridge_since default).
    facts = transcript_source.read_tail_facts(
        sid, cap, model_tail_lines=model_tail_lines, bridge_scan_lines=0
    )
    return {
        "session_id": sid,
        "sid8": sid[:8],
        "cwd": entry["cwd"],
        "archived_at": record["archived_at"],
        "last_prompt": facts["last_prompt"],
    }


def _cmd_retrack(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    archive = ArchiveStore(sd)

    if args.sid is not None:
        if args.last is not None:
            # --sid names one record; --last means "the N most recent".
            # Combined, --last would be silently ignored — reject rather
            # than quietly narrow the scope behind the user's back (mirrors
            # recall's --sid/--all mutual-exclusion guard).
            print("crr retrack: --sid cannot be combined with --last", file=sys.stderr)
            return 2
        if not contracts.valid_session_id(args.sid):
            print(f"crr retrack: {args.sid!r} is not a valid session id", file=sys.stderr)
            return 2
        with mutation_lock(sd):
            res = ops.retrack(store, archive, args.sid, _now())
        print(res.message, file=sys.stdout if res.ok else sys.stderr)
        return 0 if res.ok else 1

    last = 10 if args.last is None else args.last
    if last < 0:
        print("crr retrack: --last must not be negative", file=sys.stderr)
        return 2

    with mutation_lock(sd):
        scan = archive.scan()
        records = _recent_untracked_records(scan.records, last)
        results = [
            ops.retrack(store, archive, record["entry"]["claude"]["session_id"], _now())
            for record in records
        ]
    # Corrupt files are surfaced on stderr, never silently dropped (mirrors
    # _cmd_status/_cmd_revive/_cmd_gc/_cmd_archive).
    for name, reason in scan.problems:
        print(f"crr retrack: skipped unreadable archive file {name}: {reason}", file=sys.stderr)
    if not results:
        print("no untracked sessions to retrack")
        return 0
    ok_all = True
    for res in results:
        print(res.message, file=sys.stdout if res.ok else sys.stderr)
        ok_all = ok_all and res.ok
    return 0 if ok_all else 1


def _discoverable_rows(
    store: JournalStore, config: cfg.Config | None = None,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Untracked transcripts (T-C): everything on disk crr's journal doesn't
    know about, enriched + recency-sorted (``crr.core.discovery.untracked``).

    Filters against the journal BEFORE reading any transcript content
    (``transcript_source.list_all_transcripts`` is glob+stat only), so a
    mostly-already-tracked ``~/.claude/projects`` doesn't pay a
    ``read_tail_facts``/``read_cwd`` read per already-journaled transcript
    — only the (usually much smaller) untracked subset does. Acceptable
    here (unlike the poll path) because every caller — `crr discover`, the
    lazy `/api/discoverable` panel, and adopt's cwd resolution — is
    on-demand, never the dashboard poll.

    Returns ``(rows, problems)``: ``problems`` is the journal scan's
    ``JournalScan.problems`` (corrupt/stale ``tabs/<pid>.json`` files) —
    NOT silently dropped, because a corrupt file means its session_id never
    entered the "journaled" exclusion set, so that already-tracked session
    would otherwise appear as falsely "discoverable" (and adoptable into a
    SECOND journal entry). The listing caller (`crr discover`) surfaces
    these on stderr, mirroring `_cmd_retrack`/`_cmd_status`; a single-sid
    caller (`_adopt`) and the web provider (which can't print) drop them —
    same precedent as `_cmd_retrack --sid` not consulting the archive
    scan's problems for a targeted lookup.
    """
    if config is None:
        config = _load_config()
    candidates, journaled, problems = _discoverable_candidates(store)
    return discovery.untracked(journaled, _enrich_discoverable(candidates, config)), problems


def _discoverable_candidates(store: JournalStore, config=None):
    """The CHEAP half of discovery: which transcripts are untracked, newest first.

    Glob + stat only (``list_all_transcripts``) plus the journal scan — NO
    transcript content is read here. That split is the whole point: on a
    machine with thousands of transcripts, reading every one to render a
    20-row page cost ~10s and 600KB (measured, 2856 rows). Callers that need
    only one row (``_adopt``) or one page (the dashboard modal) filter these
    candidates FIRST and enrich only what they will actually use.

    Returns ``(candidates, journaled_sids, scan_problems)``; candidates carry
    ``{session_id, cwd, mtime}`` where ``cwd`` is the LOSSY project-dir decode
    (see ``transcript_source._decode_project_dir_name``) — good enough to
    filter on, replaced with the authoritative stamped cwd during enrichment.
    """
    if config is None:
        config = _load_config()
    # config.toml's list is the user's hand-owned baseline; the dashboard's
    # admin section can add more without ever rewriting their TOML.
    excluded_dirs = exclusions.effective(
        config.get("discover_exclude_dirs"),
        exclusions.ExclusionStore(state_dir.state_dir()).read(),
    )
    scan = store.scan()
    journaled = {
        e["claude"]["session_id"] for e in scan.entries if e.get("claude") is not None
    }
    candidates = [
        t for t in transcript_source.list_all_transcripts()
        if t["session_id"] not in journaled
        # Tool-internal transcripts (claude-mem's observer sessions and the
        # like) are not the user's conversations — see discovery.is_excluded.
        and not discovery.is_excluded(t["cwd"], excluded_dirs)
    ]
    # Newest-first by file mtime: a cheap stand-in for conversation recency
    # (the accurate `last_active` needs the very read we're avoiding), so the
    # first page is the one a user most likely wants.
    candidates.sort(key=lambda t: t["mtime"], reverse=True)
    return candidates, journaled, scan.problems


def _enrich_discoverable(candidates, config) -> list[dict]:
    """The EXPENSIVE half: one tail read + one cwd read per candidate.

    Call this only on the subset actually being shown (see
    ``_discoverable_candidates``). Adds the fields that need transcript
    content: the authoritative stamped ``cwd`` (a revive spawn uses it; the
    project-dir decode is lossy), ``last_active``, ``transcript_bytes``, and
    ``last_prompt``.
    """
    cap = config.get("last_prompt_display_cap")
    model_tail_lines = config.get("model_tail_lines")
    enriched = []
    for t in candidates:
        # No use for bridge facts here either; bridge_scan_lines=0 keeps
        # this walk's early exit as cheap as before Slice 1 (see
        # _untracked_view's comment on the same pattern).
        facts = transcript_source.read_tail_facts(
            t["session_id"], cap, model_tail_lines=model_tail_lines, bridge_scan_lines=0
        )
        # (#34) Keep WHICH of the two cwds this is. `read_cwd` reads the
        # value Claude Code stamped on the session's own records —
        # authoritative. `t["cwd"]` is the project-dir decode, which cannot
        # tell an encoded `/` from a literal `-` (so `Claude-Remote-Rescue`
        # comes back as `/home/u/Claude/Remote/Rescue`). Collapsing them
        # with `or` lost exactly the distinction that decides whether the
        # value is safe to hand to a spawn.
        verified = transcript_source.read_cwd(t["session_id"])
        cwd = verified if verified is not None else t["cwd"]
        enriched.append({
            "session_id": t["session_id"],
            "cwd": cwd,
            "cwd_source": "verified" if verified is not None else "decoded",
            "last_active": facts["last_active"],
            "transcript_bytes": facts["transcript_bytes"],
            "last_prompt": facts["last_prompt"],
            "mtime": t["mtime"],
        })
    return enriched


def _discoverable_row(store: JournalStore, sid: str, config=None) -> dict | None:
    """One discoverable row by sid, enriching ONLY that transcript.

    ``_adopt``/takeover need a single row's authoritative cwd. Going through
    the full ``_discoverable_rows`` for that read every untracked transcript
    on the machine (~10s here) just to use one of them — and, once the
    dashboard paginates, would also have to be careful never to page. Filter
    the cheap candidate list first, then enrich exactly one.
    """
    if config is None:
        config = _load_config()
    candidates, _journaled, _problems = _discoverable_candidates(store)
    match = next((t for t in candidates if t["session_id"] == sid), None)
    if match is None:
        return None
    rows = _enrich_discoverable([match], config)
    return rows[0] if rows else None


def _adopt(store: JournalStore, sd: Path, sid: str, *, competing_note: bool = True) -> tuple[bool, str]:
    """Adopt one discoverable (untracked) transcript into the journal.

    Shared by ``crr discover --adopt`` and the web ``/api/sid-action
    {op:"adopt"}`` provider (which leave ``competing_note`` on — they have
    NOT stopped any live process, so the "a second ``claude --resume`` may
    start" hazard is real and must be disclosed) and by ``_takeover`` (which
    passes ``competing_note=False`` — it just stopped the live process, so
    that warning would be false there). The cwd resolution (``_discoverable_rows``)
    reads transcript content and runs OUTSIDE any lock; only the final
    re-check + write happens under ``mutation_lock`` — holding the lock
    across N transcript reads would stall every other op (the revive timer
    included) for the duration of a full enumeration.
    """
    # Targeted single-sid lookup: enriches ONLY this transcript rather than
    # every untracked one on the machine (see _discoverable_row). The caller
    # isn't listing, so a scan problem elsewhere in the journal isn't printed
    # here (mirrors `_cmd_retrack --sid`).
    row = _discoverable_row(store, sid)
    if row is None:
        return False, f"{sid[:8]} is not a discoverable (untracked) session"
    with mutation_lock(sd):
        # Re-check under the lock: another writer may have journaled this
        # sid since the read above (register(), retrack, or a concurrent
        # adopt of the same sid).
        journaled = {
            e["claude"]["session_id"] for e in store.scan().entries if e.get("claude") is not None
        }
        if sid in journaled:
            return False, f"{sid[:8]} is already tracked"
        # (#34) A DECODED cwd is a guess, and this is the last point before
        # it enters the journal — from there it reaches
        # `tmux.new_detached_session(name, entry["cwd"], ...)`, where a
        # wrong directory does not display wrong, it fails to revive.
        # Existing-directory is a weak check but a real one: the classic
        # lossy decode (`Claude-Remote-Rescue` -> `/home/u/Claude/Remote/
        # Rescue`) does not resolve, so this catches it. A VERIFIED cwd is
        # exempt deliberately — it was observed on the session's own
        # records, and a since-deleted project directory is not this
        # guard's business.
        if row.get("cwd_source") == "decoded" and not Path(row["cwd"]).is_dir():
            return False, (
                f"cannot adopt {sid[:8]}: its cwd was reconstructed from the "
                f"project directory name (lossy) and {row['cwd']!r} is not a "
                "directory — adopting it would journal a path nothing can "
                "revive into"
            )
        entry = discovery.build_adopted_entry(row["session_id"], row["cwd"], _now())
        pid = entry["pid"]
        try:
            existing = store.read(pid)
        except KeyError:
            existing = None  # slot is genuinely empty — safe to write
        except (contracts.ContractError, OSError):
            # Can't tell whose slot this is (a corrupt file or a read
            # error — JournalStore.scan hits the same two reading its
            # sibling files). Refuse rather than guess: silently treating
            # "unreadable" as "empty" risks clobbering a real entry, which
            # is exactly the kind of laundering this repo's archive/journal
            # discipline forbids elsewhere.
            return False, f"cannot adopt {sid[:8]}: pid slot {pid} is unreadable, refusing to guess"
        if existing is not None and (existing.get("claude") or {}).get("session_id") != sid:
            # The deterministic synthetic-pid slot (see discovery.adopted_pid)
            # already belongs to a DIFFERENT entry — refuse rather than
            # silently clobber it (a birthday-bound long shot, but a silent
            # overwrite is exactly the kind of laundering this repo's
            # archive/journal discipline forbids elsewhere).
            return False, f"cannot adopt {sid[:8]}: synthetic pid slot collision, refusing to overwrite"
        store.write(entry)
    msg = f"adopted {sid[:8]} — now tracked as recoverable (revive via `crr reopen`)"
    if competing_note:
        msg += (
            "; NOTE: this does NOT attach to a running process — and if the session is "
            "still alive elsewhere, the watchdog will start a second `claude --resume` "
            "on the same conversation."
        )
    return True, msg


def _takeover(
    store: JournalStore,
    sd: Path,
    config: cfg.Config,
    controller,
    flags: FlagStore,
    sid: str,
    *,
    max_wait: float,
    read_signal=transcript_source.read_takeover_signal,
    clock=time.time,
    sleep=time.sleep,
) -> tuple[bool, str]:
    """Safe adoption of a still-live ``claude --resume sid`` (`crr adopt
    --takeover`). See docs/superpowers/specs/2026-08-03-adopt-takeover.md.

    Ordering, each step load-bearing:
      1. Resolve the live process ONCE, up front. None -> refuse (the
         fresh-session / not-running home): no kill, no flag, no wait paid
         for. This early resolve only confirms a target exists before the
         wait loop — it is NOT the tuple that gets killed (see step 3):
         the wait loop below can run up to ``max_wait`` seconds lock-free,
         long enough for this process to exit and its pid/ppid/pgid to be
         recycled by the OS.
      2. Wait loop, refuse-fast, LOCK-FREE (mirrors ``_adopt``'s lock-free
         read phase — the wait can run up to ``max_wait`` seconds; holding
         ``mutation_lock`` here would stall every other op, the revive
         timer included, for that whole span). A quiet-but-not-clean tail
         refuses IMMEDIATELY, never waiting out the timeout; a busy
         transcript only refuses once ``max_wait`` elapses. Every
         non-ready outcome here is a refusal, never a kill.
      3. Kill under ``mutation_lock`` (bounded by ``grace``): re-check the
         sid is still untracked (closes the resolve->kill race), then
         RE-RESOLVE the live process (``find_resume_process`` again) —
         this is the tuple the kill actually uses, so it can never be the
         stale one from step 1. None on re-resolve -> refuse honestly (the
         process exited during the wait): no kill, no flag. Otherwise
         ``arm_close(ppid)`` STRICTLY BEFORE ``terminate_group(pgid)`` on
         the FRESH tuple — a flag survives only when a kill actually lands
         (mirrors ``_reopen_ghost``'s rollback rule); an undeliverable kill
         clears the flag and fails with nothing else touched.
      4. Adopt via ``_adopt`` (its own lock + re-check) AFTER the kill
         lock is released — the journal write only ever happens once a
         kill has actually landed. ``_adopt`` can still refuse past its own
         journal re-check (row no longer discoverable, a synthetic-pid
         slot collision, an unreadable slot) — a killed-but-not-adopted
         state is reachable and reported honestly; the close flag armed in
         step 3 still lets a later plain ``crr adopt`` recover it.
    """
    proc = controller.find_resume_process(sid)
    if proc is None:
        return False, (
            f"no live 'claude --resume {sid}' found; adopt without --takeover, "
            "or exit it in its own terminal first"
        )

    idle_window = config.get("takeover_idle_seconds")
    poll = config.get("takeover_poll_seconds")
    grace = config.get("close_grace_seconds")

    deadline = clock() + max_wait
    while True:
        sig = read_signal(sid)
        seconds_idle = clock() - sig["mtime"]
        if seconds_idle >= idle_window:
            # Quiet — decide now, an idle process won't advance its own tail.
            if takeover.ready_to_take_over(seconds_idle, sig["tail_kind"], idle_window=idle_window):
                break
            tail_kind = sig["tail_kind"] or "unknown"
            return False, (
                f"idle but parked at {tail_kind} — not a safe boundary to take over; "
                "finish or exit it manually"
            )
        # Still writing — keep polling unless the wait-for-quiet phase's
        # own deadline has elapsed.
        if clock() >= deadline:
            return False, f"still actively writing after {max_wait:g}s; not taking over"
        sleep(poll)

    with mutation_lock(sd):
        journaled = {
            e["claude"]["session_id"] for e in store.scan().entries if e.get("claude") is not None
        }
        if sid in journaled:
            return False, f"{sid[:8]} is now tracked — not taking over"
        # Re-resolve under the lock: the wait loop above ran lock-free for
        # up to max_wait seconds, long enough for the step-1 process to
        # exit and its pid/ppid/pgid to be recycled by the OS. The kill
        # below must use THIS tuple, never the stale one from the top of
        # the function.
        proc = controller.find_resume_process(sid)
        if proc is None:
            return False, (
                f"the live process for {sid[:8]} exited; adopt without --takeover"
            )
        flags.arm_close(proc.ppid)
        try:
            controller.terminate_group(proc.pgid, grace)
        except OSError as exc:
            flags.clear(proc.ppid)  # no kill landed -> the flag must not linger
            return False, f"takeover: failed to stop live pid {proc.pid}: {exc}"

    ok, msg = _adopt(store, sd, sid, competing_note=False)
    sid8 = sid[:8]
    prefix = f"took over {sid8} (stopped live pid {proc.pid})"
    if ok:
        return True, f"{prefix}; {msg}"
    return False, f"{prefix} but adoption failed: {msg}"


def _web_takeover(store, sd, config, controller, flags, sid, **kwargs) -> tuple[bool, str]:
    """Dashboard takeover (the phone-reachable ``/api/sid-action {op:"takeover"}``).

    ``max_wait=0.0`` so the request never blocks on the wait loop — the loop
    makes exactly ONE decision and returns (idle+boundary -> take over;
    parked/busy -> refuse), never sleeping. A mid-turn session therefore
    refuses immediately with ``_takeover``'s internal "still actively writing
    after 0s" wording, which reads like a bug on a phone; translate it to a
    retry-friendly message. (The dashboard runs on ThreadingHTTPServer, so the
    only real block is ``terminate_group``'s grace on a landed kill — the same
    bound the Kick/Close buttons already accept.) ``**kwargs`` forwards the
    injectable clock/sleep/read_signal seams for tests.
    """
    ok, msg = _takeover(store, sd, config, controller, flags, sid, max_wait=0.0, **kwargs)
    if not ok and "actively writing" in msg:
        return False, "session is mid-turn — try again in a moment"
    return ok, msg


def _cmd_adopt(args: argparse.Namespace) -> int:
    if not contracts.valid_session_id(args.sid):
        print(f"crr adopt: {args.sid!r} is not a valid session id", file=sys.stderr)
        return 2
    config = _load_config()
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    if not args.takeover:
        ok, msg = _adopt(store, sd, args.sid)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    controller = process_probe.PsProcessController(config.get("interop_timeout_seconds"))
    flags = FlagStore(sd)
    max_wait = args.wait if args.wait is not None else config.get("takeover_max_wait_seconds")
    idle_window = config.get("takeover_idle_seconds")
    if max_wait < idle_window:
        # Honest heads-up, not a block: a quiet/parked transcript can never
        # reach the idle-decision branch within a --wait shorter than the
        # idle window, so a later "still actively writing after Ns" refusal
        # would be misleading — it may not have been writing at all.
        print(
            f"note: --wait {max_wait:g}s is below the takeover idle window "
            f"({idle_window:g}s); a takeover can only proceed if the transcript "
            f"is already quiet — it cannot wait out an active turn in under "
            f"{idle_window:g}s",
            file=sys.stderr,
        )
    ok, msg = _takeover(store, sd, config, controller, flags, args.sid, max_wait=max_wait)
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _relative_age(iso: str, now_iso: str) -> str:
    """"Xm/h/d ago" (or "just now"/"" for unknown) — the CLI's server-side
    mirror of page.html's ``relTime()`` (T-A), for `crr discover`'s listing.
    """
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    # A transcript's stamped timestamp isn't guaranteed to carry a UTC
    # offset (fromisoformat happily parses "2026-01-01 00:00:00" as naive);
    # `_now()` always is. Subtracting an aware datetime from a naive one
    # raises TypeError, not ValueError — normalize instead of crashing the
    # whole `crr discover` listing over one honestly-timestamped-but-naive
    # transcript (mirrors archive.is_expired's ValueError/TypeError guard).
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    diff = (now - t).total_seconds()
    if diff < 60:
        return "just now"
    minutes = int(diff // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(diff // 3600)
    if hours < 24:
        return f"{hours}h ago"
    days = int(diff // 86400)
    return f"{days}d ago"


def _cmd_discover(args: argparse.Namespace) -> int:
    sd = state_dir.state_dir()
    store = JournalStore(sd)

    if args.adopt is not None:
        if not contracts.valid_session_id(args.adopt):
            print(f"crr discover: {args.adopt!r} is not a valid session id", file=sys.stderr)
            return 2
        ok, message = _adopt(store, sd, args.adopt)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    # Newest-first, and enrich only what we print: reading every untracked
    # transcript to list a screenful cost ~10s on a machine with a few
    # thousand of them. `--all` opts back into the full (slow) listing.
    config = _load_config()
    candidates, _journaled, problems = _discoverable_candidates(store)
    # Corrupt journal files are surfaced on stderr, never silently dropped
    # (mirrors _cmd_status/_cmd_retrack/_cmd_gc/_cmd_archive) — a session
    # this listing can't exclude because its journal file failed to parse
    # is a session that would otherwise look falsely "discoverable".
    for name, reason in problems:
        print(f"crr discover: skipped unreadable journal file {name}: {reason}", file=sys.stderr)
    if not candidates:
        print("no discoverable (untracked) transcripts")
        return 0
    total = len(candidates)
    shown = candidates if args.all else candidates[: args.limit]
    rows = _enrich_discoverable(shown, config)
    now_iso = _now()
    for r in rows:
        age = _relative_age(r["last_active"], now_iso)
        age_tag = f" {age}" if age else ""
        prompt = f" — {r['last_prompt']}" if r["last_prompt"] else ""
        print(f"{r['session_id'][:8]} {r['cwd']}{age_tag}{prompt}")
    # No silent caps: say what was withheld and how to see it.
    if len(rows) < total:
        print(f"\n… showing the {len(rows)} most recent of {total} "
              f"(use -n N for more, or --all for every one)")
    return 0


def _whoami_card(config=None) -> dict | None:
    """The session card this process is running inside, or None.

    Walks up the process tree to the nearest journaled shell (see
    crr.core.whoami), then assembles that one session so the answer carries
    the same title/slug/state the dashboard shows.
    """
    if config is None:
        config = _load_config()
    sd = state_dir.state_dir()
    store = JournalStore(sd)
    scan = store.scan()
    journaled = {e["pid"] for e in scan.entries}
    shell_pid = whoami.journaled_ancestor(
        os.getpid(), journaled,
        lambda pid: process_probe.parent_of(pid, config.get("interop_timeout_seconds")),
    )
    if shell_pid is None:
        return None
    try:
        boot = boot_identity.detect()
    except NotImplementedError:
        return None
    probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
    settings_store = settings.SettingsStore(sd)
    payload = status.assemble_sessions(
        [e for e in scan.entries if e["pid"] == shell_pid], boot, probe,
        tail_facts=_tail_facts_extractor(config),
        context_tight_fraction=config.get("context_tight_fraction"),
        context_compact_fraction=config.get("context_compact_fraction"),
        bridge_stale_records=config.get("bridge_stale_records"),
        autokick_config_default=config.get("remote_control_autokick"),
        autokick_global_override=settings_store.effective_global_autokick(),
        autokick_session_overrides=settings_store.read_session_overrides(),
    )
    sessions = payload.get("sessions") or []
    return sessions[0] if sessions else None


def _cmd_whoami(args: argparse.Namespace) -> int:
    """Identify this session — the bridge from a Claude mobile conversation
    to a crr dashboard card (the mobile list shows a title but no session
    id and no cwd, so the answer has to come from inside)."""
    card = _whoami_card()
    if card is None:
        print("crr whoami: this process has no crr-journaled shell in its "
              "ancestry — not a tracked session", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0
    label = card["title"] or card["slug"] or "(no title yet)"
    print(f"crr session: {label}")
    print(f"  state  {card['state']}")
    print(f"  sid    {card['session_id']}")
    print(f"  pid    {card['pid']}")
    print(f"  dir    {card['cwd']}")
    if card["slug"] and card["title"]:
        print(f"  slug   {card['slug']}")
    print(f"  find it on the dashboard by searching: {label}")
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    """[hooks] Emit crr identity for a Claude Code hook event.

    ``session-start`` prints one line that Claude Code injects into the
    session context, so claude always knows which crr session it is and can
    answer without running a command. Silent no-op when this shell isn't
    tracked — a hook must never break session startup.
    """
    if args.event != "session-start":
        return 0
    try:
        card = _whoami_card()
    except Exception:
        return 0  # a hook must never break startup
    if card is None:
        return 0
    label = card["title"] or card["slug"] or card["sid8"]
    print(f"crr: this session is tracked as \"{label}\" "
          f"(sid {card['sid8']}, pid {card['pid']}, {card['cwd']}). "
          "Find it on the crr dashboard by that name.")
    return 0


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
    if live is None:
        # F16 tri-state: an unconfirmed tmux state must never be read as
        # "definitely rescued" — degrade to the same "no rescued sessions"
        # an unavailable tmux already produces above, never a guess. Say so
        # on stderr (mirrors the sibling journal-problems pattern below) so
        # the degrade isn't silent undercounting.
        print(
            "crr rescued: tmux state unknown — rescued sessions may be undercounted",
            file=sys.stderr,
        )
        live = set()
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
    print("attach: tmux attach -t <name> · dashboard: Reopen/Untrack")
    return 0


def _cmd_rescue_check(args: argparse.Namespace) -> int:
    """[shim] Once per boot, on an interactive shell's first start, offer
    to re-home conversations `crr.core.rescue` found parked from a
    previous boot's crash into visible terminal tabs (Phase-3
    restore-prompt UX). Silent when there's nothing to offer, when this
    boot's marker already exists, or when stdin/stdout aren't a tty (the
    marker is deliberately NOT written in that case, so a later
    interactive shell in the same boot still gets offered). A timeout —
    an unattended prompt — is always treated as "not now"; only a typed
    empty line (Enter) defaults to yes. Headless (no tab spawner) degrades
    to a one-line notice instead of a prompt.

    Once-per-boot is enforced by `rescue.claim_prompt` — an atomic
    O_CREAT|O_EXCL marker claim taken AFTER candidates are found but
    BEFORE either visible outcome (the [Y/n] prompt or the headless
    notice) is printed. This closes a Task-3 review finding: two
    interactive shells starting together (e.g. a terminal app restoring
    several tabs) both used to pass the old check-then-act
    already_prompted() exists() check before either wrote the marker, so
    both could prompt and both detmux the same sessions. Now the winner
    claims first; a losing shell (claim_prompt returns False) exits
    silently without printing anything, and a mid-prompt Ctrl-C/crash no
    longer re-arms the prompt for the next shell (the marker is already
    down) — `crr rescued` remains the recovery path regardless.

    Called by the shims (crr.bash/zsh/fish) on every new interactive
    shell's startup, so the entire body is guarded: any unexpected
    exception must never break the shell it's sourced into — it exits 0
    silently rather than propagating.
    """
    try:
        return _rescue_check(args)
    except Exception:
        return 0


def _rescue_check(_args: argparse.Namespace) -> int:
    # Cheapest check first: a non-interactive caller (script sourcing the
    # shim, non-tty redirect) never gets a marker written, so it costs
    # nothing to bail before touching tmux/the journal/boot identity.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0
    config = _load_config()
    sd = state_dir.state_dir()
    try:
        boot = boot_identity.detect()
    except NotImplementedError:
        return 0
    boot_id = boot.current()
    if rescue.already_prompted(sd, boot_id):
        return 0

    tmux_spawner = tmux.RealTmux(config.get("interop_timeout_seconds"))
    live = tmux_spawner.list_sessions() if tmux_spawner.available() else set()
    if live is None:
        # F16 tri-state: never prompt on an unconfirmed tmux state. Same
        # stderr note as `crr rescued`'s sibling degrade: the interactive
        # shims redirect this command's stderr to /dev/null on shell
        # startup, so this stays quiet there; a manual `crr rescue-check`
        # still sees it.
        print(
            "crr rescue-check: tmux state unknown — rescued sessions may be undercounted",
            file=sys.stderr,
        )
        live = set()
    store = JournalStore(sd)
    found = rescue.rescued_sessions(store.scan().entries, boot_id, live)
    if not found:
        return 0

    # Atomic once-per-boot claim, taken BEFORE either visible outcome
    # below (prompt or headless notice) — the winner proceeds; a losing
    # shell (a concurrent shell already claimed this boot) exits silently
    # without printing anything (see claim_prompt's docstring).
    if not rescue.claim_prompt(sd, boot_id):
        return 0

    n = len(found)
    tab = _tab_spawner(config)
    if tab is None or not tab.available():
        print(f"crr: {n} conversation(s) rescued from the last reboot — "
              "'crr rescued' lists them; attach with: tmux attach -t <name>")
        return 0

    print(f"crr: {n} conversation(s) rescued from the last reboot. "
          "Open them in terminal tabs? [Y/n] ", end="", flush=True)
    timeout = config.get("rescue_prompt_timeout_seconds")
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        line = sys.stdin.readline() if ready else ""  # "" on timeout, or EOF with stdin closed
    except KeyboardInterrupt:
        # Ctrl-C at an unattended-or-not prompt must decline like a
        # timeout, not propagate (outer `except Exception` in
        # _cmd_rescue_check doesn't catch KeyboardInterrupt — a bare
        # widen there would silence the traceback). The claim above
        # already happened before this prompt was printed, so there is
        # no marker write left to skip here.
        print()
        print("not now — 'crr rescued' lists them")
        return 0
    if not line:
        print()  # nothing was typed/echoed by a terminal -> start the decline on its own line
    answer = line.strip().lower() if line else None

    if answer in ("", "y"):
        probe = process_probe.PsProcessProbe(config.get("interop_timeout_seconds"))
        with mutation_lock(sd):
            for e in found:
                res = ops.detmux(JournalStore(sd), ArchiveStore(sd), tmux_spawner, boot, probe,
                                 e["pid"], _now(), tab_spawner=tab)
                # All three shims invoke `crr rescue-check 2>/dev/null`, so
                # stderr is silenced here — the user already consented (typed
                # Y) and must see failures, not just successes. Both outcomes
                # go to stdout unconditionally.
                print(res.message)
    else:  # 'n'/'N', any other input, timeout, or EOF -> decline
        print("not now — 'crr rescued' lists them")
    return 0


def make_web_handler(
    sessions_provider: Callable[[], dict],
    allowed_hosts: set[str],
    allowed_suffixes: tuple[str, ...],
    action_provider: Callable[[str, int], tuple[bool, str]] | None = None,
    diagnostics_provider: Callable[[], dict] | None = None,
    untracked_provider: Callable[[str, int, int], dict] | None = None,
    discoverable_provider: Callable[[str, int, int], dict] | None = None,
    sid_action_provider: Callable[[str, str], tuple[bool, str]] | None = None,
    recall_provider: Callable[[str, str | None], dict] | None = None,
    exclusions_provider: Callable[[], dict] | None = None,
    exclusions_writer: Callable[[object], dict] | None = None,
    settings_provider: Callable[[], dict] | None = None,
    settings_writer: Callable[[object], dict] | None = None,
    poll_seconds: int | None = None,
    version_check_seconds: int | None = None,
    confirm_arm_seconds: int | None = None,
    notice_seconds: int | None = None,
    reload_delay_ms: int | None = None,
    flash_ms: int | None = None,
    filter_debounce_ms: int | None = None,
    diag_error_display_cap: int | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build an http.server handler bound to the given dependencies.

    Thin adapter: it only marshals bytes to/from ``web.handle_request``
    (the pure core handler that owns routing + the security gate).
    """

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            resp = web.handle_request(
                method, path, self.headers, body,
                sessions_provider=sessions_provider,
                action_provider=action_provider,
                diagnostics_provider=diagnostics_provider,
                untracked_provider=untracked_provider,
                discoverable_provider=discoverable_provider,
                sid_action_provider=sid_action_provider,
                recall_provider=recall_provider,
                exclusions_provider=exclusions_provider,
                exclusions_writer=exclusions_writer,
                settings_provider=settings_provider,
                settings_writer=settings_writer,
                query=query,
                allowed_hosts=allowed_hosts,
                allowed_suffixes=allowed_suffixes,
                poll_seconds=poll_seconds,
                version_check_seconds=version_check_seconds,
                confirm_arm_seconds=confirm_arm_seconds,
                notice_seconds=notice_seconds,
                reload_delay_ms=reload_delay_ms,
                diag_error_display_cap=diag_error_display_cap,
                flash_ms=flash_ms,
                filter_debounce_ms=filter_debounce_ms,
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


def _diagnostics_params(source, config: cfg.Config) -> dict:
    """The generating caps/lookback/timeout for ``source`` (audit P3/P5).

    Records only the config keys the selected source's ``collect`` actually
    reads — recording a sibling source's keys would be a lineage lie, not a
    lineage. Mirrors ``_select_diag_source``'s branching so the mapping
    can't silently drift from which source is actually wired.
    """
    if source is diagnostics_windows:
        return {
            "event_cap": config.get("diagnose_event_cap"),
            "timeout_seconds": config.get("interop_timeout_seconds"),
        }
    if source is diagnostics_macos:
        return {
            "lookback": config.get("diagnose_macos_lookback"),
            "event_cap": config.get("diagnose_event_cap"),
            "timeout_seconds": config.get("diagnose_macos_timeout_seconds"),
        }
    if source is diag_source:
        # journald (native Linux, or WSL with systemd) — diag_source's collect().
        return {
            "lookback_boots": config.get("diagnose_lookback_boots"),
            "event_cap": config.get("diagnose_event_cap"),
            "line_cap": config.get("diagnose_line_cap"),
            "timeout_seconds": config.get("interop_timeout_seconds"),
        }
    # No implicit fallthrough: a future source falling through to
    # journald's params would silently inherit journald's lineage claim.
    raise ValueError(f"unknown diagnostics source {getattr(source, 'SOURCE_NAME', source)!r}")


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
        params=_diagnostics_params(source, config),
    )


def _cmd_diagnose(args: argparse.Namespace) -> int:
    payload = gather_diagnostics(_load_config())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    # Source + boot identity first (F12: lineage before the verdict).
    print(f"source: {payload['source']}")
    boots = payload["boots"]
    if boots:
        b = boots[0]
        print(f"boot: {b.get('boot_id')} (start {b.get('start')}, stop {b.get('stop') or 'ongoing'})")
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
    removed_sid8s: list[str] = []
    with mutation_lock(sd):
        scan = archive.scan()
        for record in scan.records:
            if is_expired(record, now, retention):
                sid = record["entry"]["claude"]["session_id"]
                archive.remove(sid)
                removed_sid8s.append(sid[:8])
    for name, reason in scan.problems:
        print(f"crr gc: skipped unreadable archive file {name}: {reason}", file=sys.stderr)
    print(f"gc: removed {len(removed_sid8s)} archive record(s) older than {retention} days, "
          f"kept {len(scan.records) - len(removed_sid8s)}")
    if removed_sid8s:
        print(f"removed: {removed_sid8s}")
    return 0


def _cmd_kicks(args: argparse.Namespace) -> int:
    """#35: the watchdog's auto-kick lineage had no human read path.

    Same shape and reasoning as `crr archive --list` (run-2 F15): recording
    the conditions that produced an action is only half of P8 — an operator
    has to be able to ASK. This is the command that answers "why did crr
    restart that session, and under what thresholds?".

    Read-only, no mutation_lock (mirrors `archive --list`).
    """
    if not args.list:
        print("usage: crr kicks --list", file=sys.stderr)
        return 2
    sd = state_dir.state_dir()
    store = bridge_kicks.KickHistoryStore(sd)
    if store.is_degraded():
        print(f"crr kicks: {sd / bridge_kicks.FILENAME} is unreadable — "
              "the watchdog is auto-kicking nothing until it is fixed or removed",
              file=sys.stderr)
        return 2
    sids = store.session_ids()
    if not sids:
        print("no auto-kick attempts recorded")
        return 0
    for sid in sids:
        log = store.attempt_log(sid)
        print(f"{sid[:8]}  {store.attempts(sid)} attempt(s) since the last confirmed reconnect")
        if not log:
            # A counter-only file written before #35. Say so rather than
            # printing nothing, which would read as "no attempts".
            print("    (no lineage recorded — counters predate `crr kicks`)")
            continue
        for a in log:
            when = _iso_or_raw(a.get("at"))
            since = a.get("bridge_since", "?")
            stale = a.get("stale_after", "?")
            outcome = a.get("outcome", "outcome not recorded")
            mark = "ok" if a.get("outcome_ok") else "FAILED" if "outcome_ok" in a else "?"
            print(f"    {when}  pid {a.get('pid', '?')}  "
                  f"{since} records since the bridge marker (threshold {stale})  "
                  f"-> {mark}: {outcome}")
    return 0


def _iso_or_raw(ts) -> str:
    """A stored epoch float as a readable UTC stamp, or the raw value."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return str(ts)
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _cmd_archive(args: argparse.Namespace) -> int:
    """F15: archive lineage had no human read path — `crr archive --list`.

    Read-only (no mutation_lock — mirrors config --effective's no-lock
    reads, not gc's read-modify-write).
    """
    if not args.list:
        print("usage: crr archive --list", file=sys.stderr)
        return 2
    sd = state_dir.state_dir()
    scan = ArchiveStore(sd).scan()
    for name, reason in scan.problems:
        print(f"crr archive: skipped unreadable archive file {name}: {reason}", file=sys.stderr)
    if not scan.records:
        print("no archived sessions")
        return 0
    for record in sorted(scan.records, key=lambda r: r["archived_at"], reverse=True):
        sid8 = record["entry"]["claude"]["session_id"][:8]
        cwd = record["entry"]["cwd"]
        print(f"{record['reason']:<22} {record['archived_at']} {sid8} {cwd}")
    return 0


def _settings_payload(sd: Path, config: cfg.Config) -> dict:
    """The Settings modal's global auto-kick row (spec 2026-08-07, Slice 3),
    mirroring exclusions_provider's shape: the dashboard's own stored value
    plus enough of config.toml's baseline that the UI can show the resolved
    outcome without duplicating the global/session truth table client-side.

    ``resolved`` (review fix-wave 2026-08-07, FIX 4 — MINOR, same principle
    as commit b4fe3b6) uses ``effective_global_autokick()``, NOT the raw
    stored override: while the settings store is degraded, the watchdog
    auto-kicks NOTHING at all (fail-closed, FIX 1/Slice 2's
    ``_kick_dropped_bridges`` guard), so the checkbox this field drives
    (``page.html``'s ``cb.checked = !!data.resolved``) must not render
    CHECKED — a state the system is not honouring — while every session
    card (built from the same ``effective_global_autokick()``, per b4fe3b6)
    renders ``global-off``. ``degraded`` is still surfaced separately so the
    Settings modal states the reason, not just the honest-off outcome.
    """
    settings_store = settings.SettingsStore(sd)
    config_default = config.get("remote_control_autokick")
    resolved = settings_store.effective_global_autokick()
    if resolved is None:
        resolved = config_default
    return {
        "contract": contracts.SETTINGS_CONTRACT_VERSION,
        "autokick": settings_store.read_global_autokick(),
        "resolved": resolved,
        "config_default": config_default,
        "degraded": settings_store.is_degraded(),
    }


def _write_global_autokick_locked(sd: Path, value: bool | None) -> None:
    """Set (or clear) the dashboard's global auto-kick override, under
    ``mutation_lock`` (review fix-wave 2026-08-07, FIX 3 — IMPORTANT).

    ``SettingsStore.write_global_autokick``/``write_session_autokick`` are
    each individually atomic (tmp file + rename), but that only makes ONE
    call safe — it does not make two CONCURRENT calls safe together.
    ``ThreadingHTTPServer`` serves POSTs on separate threads, so without a
    shared lock around the whole read-modify-write, this interleaving is
    possible: a per-session write reads ``{"autokick": true}``, THEN the
    global switch flips to False, THEN the per-session write lands —
    carrying its stale ``autokick: true`` read forward and silently
    reverting the panic switch back on. See
    ``_write_session_autokick_locked`` (the other half of this pair) and
    ``test_cli.py``'s FIX 3 tests, which prove the lock is actually held
    for the FULL operation via a non-blocking probe.
    """
    with mutation_lock(sd):
        settings.SettingsStore(sd).write_global_autokick(value)


def _write_session_autokick_locked(sd: Path, sid: str, value: bool) -> None:
    """Set one session's auto-kick override, under ``mutation_lock`` — the
    other half of the FIX 3 pair; see ``_write_global_autokick_locked``'s
    docstring for the race this closes. Raises ``settings.SettingsError``
    on a non-UUID ``sid`` or a non-bool ``value``, same as the underlying
    store."""
    with mutation_lock(sd):
        settings.SettingsStore(sd).write_session_autokick(sid, value)


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
        settings_store = settings.SettingsStore(sd)
        payload = status.assemble_sessions(
            store.scan().entries,
            boot,
            probe,
            tail_facts=extract,
            context_tight_fraction=config.get("context_tight_fraction"),
            context_compact_fraction=config.get("context_compact_fraction"),
            bridge_stale_records=config.get("bridge_stale_records"),
            autokick_config_default=config.get("remote_control_autokick"),
            autokick_global_override=settings_store.effective_global_autokick(),
            autokick_session_overrides=settings_store.read_session_overrides(),
        )
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
                                  remote_control=config.get("remote_control"),
                                  tab_spawner=tab)
            elif op == "close":
                res = ops.close(store, controller, flags, boot, probe, pid,
                                 grace=config.get("close_grace_seconds"))
            elif op == "kick":
                res = ops.kick(store, controller, flags, boot, probe, pid,
                                grace=config.get("close_grace_seconds"))
            elif op in ("untrack", "detmux"):  # detmux: deprecated alias, same op
                res = ops.detmux(store, archive, tmux_spawner, boot, probe, pid, _now(), tab_spawner=tab)
            elif op == "untmux":
                res = ops.untmux(store, archive, tmux_spawner, boot, probe, pid, _now(),
                                  remote_control=config.get("remote_control"), tab_spawner=tab)
            else:
                return False, f"unknown op {op}"
        return res.ok, res.message

    def diagnostics_provider() -> dict:
        return gather_diagnostics(config)  # lazy: only on panel open, never on poll

    def untracked_provider(query: str = "", offset: int = 0, limit: int = 20) -> dict:
        # Lazy AND paged, mirroring discoverable_provider (they share one
        # modal, so they share the contract): filter/slice on the cheap
        # archive records, then read a transcript per row ONLY for the page.
        cap = config.get("last_prompt_display_cap")
        model_tail_lines = config.get("model_tail_lines")
        records = _recent_untracked_records(archive.scan().records, None)
        # Filter on the fields available without a transcript read.
        cheap = [
            {"session_id": r["entry"]["claude"]["session_id"],
             "cwd": r["entry"]["cwd"], "_record": r}
            for r in records
        ]
        page = discovery.filter_and_page(
            cheap, query=query, offset=offset, limit=limit,
            contract=contracts.UNTRACKED_CONTRACT_VERSION)
        page["rows"] = [
            _untracked_view(row["_record"], cap, model_tail_lines) for row in page["rows"]
        ]
        return page

    def discoverable_provider(query: str = "", offset: int = 0, limit: int = 20) -> dict:
        # Lazy (T-C) AND paged: filter/slice the CHEAP candidate list first,
        # then read transcript content for ONLY the page being shown. Reading
        # every untracked transcript cost ~10s / 600KB on a machine with a few
        # thousand of them. Scan problems are dropped here (a web handler
        # can't print to stderr); `crr discover` is where they're surfaced.
        candidates, _journaled, _problems = _discoverable_candidates(store)
        page = discovery.filter_and_page(
            candidates, query=query, offset=offset, limit=limit,
            contract=contracts.DISCOVERABLE_CONTRACT_VERSION)
        page["rows"] = _enrich_discoverable(page["rows"], config)
        # One ps snapshot for the whole page: which of these conversations is
        # ALREADY running? Plain Adopt on a live one starts a second claude on
        # the same transcript — the row is tagged so the user takes over
        # instead. Degrades to no tags if the probe is inconclusive.
        live = controller.resume_session_ids()
        for row in page["rows"]:
            row["sid8"] = row["session_id"][:8]
            row["running"] = row["session_id"] in live
        return page

    def sid_action_provider(op: str, sid: str) -> tuple[bool, str]:
        if op == "adopt":
            # _adopt takes its own mutation_lock scoped to just the
            # re-check + write (see its docstring) — the transcript reads
            # that resolve the cwd must NOT hold the lock the pid-keyed
            # action_provider and the revive timer also contend for.
            return _adopt(store, sd, sid)
        if op == "takeover":
            # _web_takeover uses max_wait=0.0 (non-blocking) and manages its
            # own mutation_lock for the kill, like _adopt — do NOT wrap it.
            return _web_takeover(store, sd, config, controller, flags, sid)
        if op in ("autokick-on", "autokick-off"):
            # Pins ONE session's auto-kick opt-in/opt-out (spec 2026-08-07,
            # Slice 3). `_write_session_autokick_locked` holds `mutation_lock`
            # for the WHOLE read-modify-write (review fix-wave 2026-08-07,
            # FIX 3 — per-call atomicity is not enough against a concurrent
            # global-switch write; see that function's docstring). The value
            # is written even when the global switch currently resolves off:
            # per-session overrides must SURVIVE a global off/on cycle
            # (spec's truth table), so this is not gated on the current
            # global state — only the dashboard's disabled-toggle rendering
            # is.
            value = op == "autokick-on"
            try:
                _write_session_autokick_locked(sd, sid, value)
            except settings.SettingsError as exc:
                return False, str(exc)
            return True, f"auto-kick {'enabled' if value else 'disabled'} for this session"
        # Same mutation lock as action_provider — a sid-keyed op racing the
        # pid-keyed ops or the revive timer must not interleave.
        with mutation_lock(sd):
            if op == "retrack":
                res = ops.retrack(store, archive, sid, _now())
            else:
                return False, f"unknown op {op}"
        return res.ok, res.message

    def exclusions_provider() -> dict:
        # Say WHERE the baseline came from, with the full path: on a machine
        # with no config.toml the "configured" entries are built-in defaults,
        # and labelling those "from config.toml" would be a lie.
        toml_path = sd / "config.toml"
        try:
            from_file = "discover_exclude_dirs" in cfg.load_toml_overrides(toml_path)
        except (cfg.ConfigError, ValueError, OSError):
            from_file = False
        return {
            "contract": contracts.EXCLUSIONS_CONTRACT_VERSION,
            "configured": list(config.get("discover_exclude_dirs")),
            "managed": exclusions.ExclusionStore(sd).read(),
            "config_path": str(toml_path),
            "config_from_file": from_file,
        }

    def exclusions_writer(dirs) -> dict:
        # ExclusionError is a ValueError, which handle_request turns into a
        # 400 carrying the message — bounds/type errors reach the user.
        managed = exclusions.ExclusionStore(sd).write(dirs)
        out = exclusions_provider()
        out["managed"] = managed
        return out

    def settings_provider() -> dict:
        return _settings_payload(sd, config)

    def settings_writer(value) -> dict:
        # write_global_autokick raises SettingsError (a ValueError) on
        # anything but a bool or None — handle_request turns that into a
        # 400 carrying the message, same contract as exclusions_writer.
        # `_write_global_autokick_locked` holds `mutation_lock` for the
        # WHOLE read-modify-write (review fix-wave 2026-08-07, FIX 3 — see
        # that function's docstring for the race this closes).
        _write_global_autokick_locked(sd, value)
        return settings_provider()

    def recall_provider(query: str, sid: str | None) -> dict:
        # Lazy GET (never the poll path): print-only transcript search, the
        # dashboard surface of `crr recall`. sid -> that one session; no sid ->
        # global (search_all bounds the newest-first whole-transcript sweep by
        # bytes and reports what it skipped). No lock: reads only.
        snippet_cap = config.get("recall_snippet_cap")
        match_cap = config.get("recall_match_cap")
        if sid is not None:
            matches = transcript_source.search_transcript(sid, query, cap=snippet_cap)
            for m in matches:
                m["session_id"] = sid
            return {"contract": contracts.RECALL_CONTRACT_VERSION,
                    "matches": transcript.rank_matches(matches, limit=match_cap),
                    "scanned": 1, "skipped": 0}
        out = transcript_source.search_all(
            query, snippet_cap=snippet_cap, match_cap=match_cap,
            byte_budget=config.get("recall_scan_byte_budget"),
            per_session_cap=config.get("recall_per_session_cap"),
            # Same exclusion list discovery uses: recall sweeps the same pool.
            exclude_dirs=exclusions.effective(
                config.get("discover_exclude_dirs"),
                exclusions.ExclusionStore(sd).read(),
            ),
        )
        # search_all returns the {matches, scanned, skipped} shape; stamp the
        # contract here so both arms of this provider emit the same payload.
        out["contract"] = contracts.RECALL_CONTRACT_VERSION
        return out

    # Host allowlist: loopback + this host's name + tailnet suffix + any
    # config.toml extras.
    allowed = {"127.0.0.1", "localhost", "[::1]", socket.gethostname().lower()}
    allowed.update(h.lower() for h in config.get("host_allowlist_extras"))
    handler = make_web_handler(
        provider, allowed, (".ts.net",),
        action_provider=action_provider,
        diagnostics_provider=diagnostics_provider,
        untracked_provider=untracked_provider,
        discoverable_provider=discoverable_provider,
        sid_action_provider=sid_action_provider,
        recall_provider=recall_provider,
        exclusions_provider=exclusions_provider,
        exclusions_writer=exclusions_writer,
        settings_provider=settings_provider,
        settings_writer=settings_writer,
        poll_seconds=config.get("dashboard_poll_seconds"),
        version_check_seconds=config.get("version_check_seconds"),
        confirm_arm_seconds=config.get("confirm_arm_seconds"),
        notice_seconds=config.get("notice_seconds"),
        reload_delay_ms=config.get("reload_delay_ms"),
        flash_ms=config.get("flash_ms"),
        filter_debounce_ms=config.get("filter_debounce_ms"),
        diag_error_display_cap=config.get("diag_error_display_cap"),
    )

    # Snapshot the page template NOW ([lesson: template/code skew]) — a lazy
    # first-request read would leave a window where a later checkout still
    # skews the served page against this process's loaded code.
    web.load_page()

    port = args.port if args.port is not None else config.get("dashboard_port")
    # Bind loopback ONLY; the tailnet (or a user proxy) is the auth boundary.
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"crr web: serving on http://127.0.0.1:{port}/ (loopback only)")
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
    # WSL tab-spawning (Untrack/Un-tmux/Reopen) shells out to wt.exe/wsl.exe, which
    # live under Windows dirs a service's PATH never inherits ([lesson:
    # interop PATH]) — baked as extras here, not in systemd.py, so native
    # Linux never warns about a "missing" wt.exe.
    is_wsl = host.is_wsl()
    extras = ("wt.exe", "wsl.exe") if is_wsl else ()
    path, missing = systemd.resolve_service_path(crr_bin, extra_binaries=extras)
    # XDG_STATE_HOME baked so the service watches the SAME state dir the shims
    # write to (state_dir() is <XDG_STATE_HOME>/crr; bake its parent).
    state_home = str(state_dir.state_dir().parent)
    # Same reason as XDG_STATE_HOME: the service won't see this install-time
    # shell's WSL_DISTRO_NAME either, and the tab spawner needs it to target
    # the right distro instead of silently falling back to the default one.
    wsl_distro = os.environ.get("WSL_DISTRO_NAME", "") if is_wsl else ""
    interval = config.get("watchdog_interval_seconds")
    port = args.port if args.port is not None else config.get("dashboard_port")
    units = {
        systemd.SERVICE_NAME: systemd.revive_service_unit(crr_bin, path, state_home),
        systemd.TIMER_NAME: systemd.revive_timer_unit(interval),
        systemd.WEB_SERVICE_NAME: systemd.web_service_unit(
            crr_bin, path, state_home, port, wsl_distro,
            restart_seconds=config.get("web_restart_seconds"),
        ),
    }

    # extras (wt.exe/wsl.exe) only degrade tab spawning, never revival —
    # reused verbatim, the revival wording below would overclaim for them.
    critical_missing = [m for m in missing if m not in extras]
    tab_missing = [m for m in missing if m in extras]
    if critical_missing:
        print(
            "crr systemd: WARNING — not found on PATH: "
            f"{', '.join(critical_missing)}; revived sessions will fail on exec until these resolve",
            file=sys.stderr,
        )
    if tab_missing:
        print(
            "crr systemd: WARNING — not found on PATH: "
            f"{', '.join(tab_missing)}; Untrack/Un-tmux/Reopen tab spawning will be unavailable "
            "until these resolve",
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
        if not _run_commands(systemd.critical_enable_commands(), "systemd"):
            print(f"crr systemd: units written to {ud} but enabling FAILED (see above); "
                  "the watchdog/dashboard are NOT running", file=sys.stderr)
            return 1
        # linger is judged separately: on WSL2 `loginctl enable-linger`
        # reliably exits 1 (a benign dbus quirk) even though the services
        # run fine, since the user manager starts with the session anyway —
        # failing it here would over-claim total install failure the same
        # way the exit-code-honesty fix over-claimed success before it.
        linger_cmd = systemd.linger_command()
        try:
            linger_ok = subprocess.run(linger_cmd, check=False).returncode == 0
        except OSError:
            linger_ok = False
        if not linger_ok:
            print(
                "crr systemd: warning — could not enable linger (common on WSL2); "
                "services will stop at logout unless linger is enabled another way",
                file=sys.stderr,
            )
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
    port = args.port if args.port is not None else config.get("dashboard_port")
    agents = {
        launchd.REVIVE_PLIST: launchd.revive_agent_plist(crr_bin, path, interval),
        launchd.WEB_PLIST: launchd.web_agent_plist(crr_bin, path, port),
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
    port = args.port if args.port is not None else config.get("dashboard_port")
    cmds = [
        scheduled_task.create_revive_task_command(crr_bin, interval, distro),
        scheduled_task.create_web_task_command(crr_bin, port, distro),
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
    print(f"# defaults version: {cfg.CONFIG_DEFAULTS_VERSION}")
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
