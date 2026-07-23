"""Shim installer: copies the shell shims into the state dir and wires up
an idempotent, guarded source line in each shell's rc file.

[lesson: PATH poisoning] The installer bakes the running ``crr``
executable's absolute path into each shim's ``CRR_BIN`` placeholder --
shims must invoke ``crr`` by absolute path, resolved once here at install
time, never rediscovered via PATH inside the hot hook path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import journal

SHELLS = ("zsh", "bash", "fish")

_RC_FILES = {
    "zsh": ".zshrc",
    "bash": ".bashrc",
    "fish": os.path.join(".config", "fish", "config.fish"),
}

_PLACEHOLDER = "__CRR_BIN__"

_MARK_BEGIN = "# >>> crr shim (managed by `crr install-shims`) >>>"
_MARK_END = "# <<< crr shim (managed by `crr install-shims`) <<<"


# ---------------------------------------------------------------------------
# Locating things


def shims_source_dir() -> Path:
    """Where the shipped shim templates live: ``<repo>/shims/`` next to the
    installed ``crr`` package (source / editable-install layout, which is
    what Phase 1 targets)."""
    here = Path(__file__).resolve().parent  # .../crr
    return here.parent / "shims"


def shims_install_dir() -> Path:
    return journal.state_dir() / "shims"


def crr_bin_path() -> str:
    """Absolute path of the currently running ``crr`` entry point.

    ``$CRR_BIN`` overrides for tests. Otherwise resolves ``sys.argv[0]``
    (the console-script path when run as the installed ``crr`` command)
    to an absolute, canonical path.
    """
    override = os.environ.get("CRR_BIN")
    if override:
        return override
    exe = sys.argv[0] or "crr"
    resolved = shutil.which(exe) or exe
    candidate = Path(resolved).resolve()
    if candidate.is_file():
        return str(candidate)
    # sys.argv[0] wasn't a real, resolvable file (e.g. "-c" from
    # `python -c ...`, or a bare module name during development) --
    # fall back to a PATH lookup for the installed `crr` console script.
    found = shutil.which("crr")
    if found:
        return str(Path(found).resolve())
    return str(candidate)


def detected_shells() -> List[str]:
    """Shells actually present on this host (``which`` finds a binary)."""
    return [s for s in SHELLS if shutil.which(s)]


# ---------------------------------------------------------------------------
# rc-file guarded block


def _guarded_block(shell: str, dest: Path) -> str:
    dest_str = str(dest)
    if shell == "fish":
        body = 'test -f "%s"; and source "%s"' % (dest_str, dest_str)
    else:
        body = '[ -f "%s" ] && source "%s"' % (dest_str, dest_str)
    return "\n%s\n%s\n%s\n" % (_MARK_BEGIN, body, _MARK_END)


def _ensure_source_line(rc_path: Path, dest: Path, shell: str) -> bool:
    """Idempotently append the guarded source block to *rc_path*.

    Returns True when the file was modified. A second call is a no-op
    (returns False) -- installing twice must not duplicate the block.
    """
    try:
        existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    except OSError:
        existing = ""
    if _MARK_BEGIN in existing:
        return False
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rc_path, "a", encoding="utf-8") as fh:
        fh.write(_guarded_block(shell, dest))
    return True


def _strip_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out = []
    skipping = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == _MARK_BEGIN:
            skipping = True
            continue
        if stripped == _MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# install / uninstall


def install(shells: Optional[List[str]] = None, home: Optional[Path] = None) -> Dict[str, Dict]:
    """Install shims for *shells* (default: autodetected shells present on
    this host). Returns a per-shell report dict."""
    home = home or Path.home()
    src_dir = shims_source_dir()
    dest_dir = shims_install_dir()
    crr_bin = crr_bin_path()
    target_shells = shells if shells else detected_shells()

    report: Dict[str, Dict] = {}
    for shell in target_shells:
        entry: Dict = {
            "copied": False,
            "rc_updated": False,
            "rc_path": None,
            "shim_path": None,
            "error": None,
        }
        src = src_dir / ("crr.%s" % shell)
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as exc:
            entry["error"] = "shim source missing (%s): %s" % (src, exc)
            report[shell] = entry
            continue

        text = text.replace(_PLACEHOLDER, crr_bin)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ("crr.%s" % shell)
        try:
            dest.write_text(text, encoding="utf-8")
            dest.chmod(0o755)
        except OSError as exc:
            entry["error"] = "could not write %s: %s" % (dest, exc)
            report[shell] = entry
            continue
        entry["copied"] = True
        entry["shim_path"] = str(dest)

        rc_path = home / _RC_FILES[shell]
        entry["rc_path"] = str(rc_path)
        try:
            entry["rc_updated"] = _ensure_source_line(rc_path, dest, shell)
        except OSError as exc:
            entry["error"] = "could not update %s: %s" % (rc_path, exc)
        report[shell] = entry
    return report


def uninstall(shells: Optional[List[str]] = None, home: Optional[Path] = None) -> Dict[str, Dict]:
    """Remove the guarded source line from each shell's rc file.

    The installed shim scripts under the state dir are left in place
    (harmless once nothing sources them); only the rc-file wiring this
    installer added is removed.
    """
    home = home or Path.home()
    target_shells = shells if shells else list(SHELLS)

    report: Dict[str, Dict] = {}
    for shell in target_shells:
        entry: Dict = {"rc_cleaned": False, "rc_path": None, "error": None}
        rc_path = home / _RC_FILES[shell]
        entry["rc_path"] = str(rc_path)
        if rc_path.exists():
            try:
                text = rc_path.read_text(encoding="utf-8")
            except OSError as exc:
                entry["error"] = str(exc)
                report[shell] = entry
                continue
            new_text = _strip_block(text)
            if new_text != text:
                try:
                    rc_path.write_text(new_text, encoding="utf-8")
                    entry["rc_cleaned"] = True
                except OSError as exc:
                    entry["error"] = str(exc)
        report[shell] = entry
    return report
