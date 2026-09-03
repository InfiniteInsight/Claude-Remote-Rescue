"""Dashboard-managed settings store: autokick toggles (spec 2026-08-07,
Slice 2 — the dropped-Remote-Control watchdog's settings store) plus tunnel
overrides (spec 2026-09-02).

A sibling to ``crr/core/exclusions.py``: same JSON-in-the-state-dir,
atomic-write, degrade-to-default-on-read discipline (the dashboard cannot
write ``config.toml`` — see exclusions.py's docstring for why). This one
holds three knobs, all under separate top-level keys in the same file so no
writer can clobber another's key when it rewrites the store:

- a GLOBAL ``autokick`` override, tri-state (unset / true / false) — the
  dashboard's override of ``config.toml``'s ``remote_control_autokick``
  default. Unset means "fall back to the config default".
- a per-**session-id** map of bool overrides, pinning one session in or out
  regardless of the resolved global value.
- a ``tunnel`` override (provider / cloudflare tunnel name / cloudflare
  hostname), each field independently unset-or-set — the dashboard's
  override of ``config.toml``'s tunnel keys. See ``read_tunnel``/
  ``write_tunnel``.

Keyed by session id, NEVER pid: a pid is recycled by the OS, and a
pid-keyed opt-out would silently transfer to an unrelated later session
that happens to reuse the same pid. ``write_session_autokick`` rejects
anything that isn't a session UUID (``contracts.valid_session_id``), and
the map is bounded (``MAX_SESSION_ENTRIES``) so a malformed or hostile POST
cannot grow the file without limit.

``autokick_for`` is the pure resolution helper implementing the spec's
two-level truth table: global OFF (whether via an explicit override or an
unset override falling back to a False config default) is a hard switch —
nothing overrides it, and per-session values are RETAINED rather than
discarded so flipping global back on restores them exactly.

Every writer (``write_global_autokick``, ``write_session_autokick``,
``write_tunnel``) rewrites the whole file, so each one carries the OTHER
keys forward via ``_carry_autokick``/``_carry_tunnel`` rather than setting
only the key it owns — otherwise, e.g., flipping the global autokick switch
would silently erase a stored tunnel override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from crr.core import contracts
from crr.core.journal import read_json_file, write_json_atomic
from crr.core.tunnel import TUNNEL_PROVIDERS

FILENAME = "settings.json"

# A malformed or hostile POST must not be able to grow the per-session map
# without limit (mirrors exclusions.py's MAX_ENTRIES).
MAX_SESSION_ENTRIES = 500

# The tunnel override's field names (spec 2026-09-02) — shared by
# SettingsStore.read_tunnel/write_tunnel and the carry-forward helper below.
_TUNNEL_FIELDS = ("provider", "cloudflare_tunnel_name", "cloudflare_hostname")


class SettingsError(ValueError):
    """A rejected settings payload (shape, type, or bounds)."""


def autokick_for(
    *, config_default: bool, global_override: bool | None, session_override: bool | None,
) -> bool:
    """Resolve the effective auto-kick decision for one session.

    Truth table (spec 2026-08-07 — the two-level toggle):

        global resolved False -> False, ALWAYS (per-session ignored, but
            still stored — this is the panic switch: nothing overrides it)
        global resolved True, session unset  -> True
        global resolved True, session False  -> False
        global resolved True, session True   -> True

    ``global_override`` is the dashboard's stored value, or ``None`` when
    it has never been set (falls back to ``config_default``, the
    ``config.toml`` value). The asymmetry is deliberate: global ON is
    permissive (the interesting control becomes "all except this one
    session"), global OFF is a hard switch nothing can quietly defeat.
    """
    global_resolved = config_default if global_override is None else global_override
    if not global_resolved:
        return False
    if session_override is None:
        return True
    return session_override


def autokick_card_state(
    *, config_default: bool, global_override: bool | None, session_override: bool | None,
    degraded: bool = False,
) -> str:
    """The session card's ``autokick`` field (spec 2026-08-07, Slice 3):
    ``"on"`` / ``"off"`` / ``"global-off"`` — see ``contracts.AUTOKICK_STATES``
    for what each means and why a bool is not enough.

    Layered directly on ``autokick_for``: the effective yes/no decision is
    unchanged, this only adds the distinction the per-session dashboard
    toggle needs to render disabled-with-reason instead of a lying "on" —
    when the GLOBAL switch resolves off, the reason is different (and more
    urgent — the panic switch itself is off) from a session that opted out
    while global stays on.
    """
    if degraded:
        # #40. The behaviour is identical to "global-off" — nothing is
        # kicked — but the REASON is not, and the reason is what the user
        # acts on. "global-off" points them at a switch they never touched
        # and that flipping will not fix; "degraded" points them at the
        # file. Checked first because it overrides every other state.
        return "degraded"
    global_resolved = config_default if global_override is None else global_override
    if not global_resolved:
        return "global-off"
    return "on" if autokick_for(
        config_default=config_default,
        global_override=global_override,
        session_override=session_override,
    ) else "off"


def _normalize_sessions(value: Any) -> dict[str, bool]:
    """Validate a stored/incoming sessions mapping, or raise ``SettingsError``.

    Every key must be a session UUID (never a pid-shaped string — see the
    module docstring) and every value a real bool. Bounded so a corrupt or
    hostile file can't be trusted past ``MAX_SESSION_ENTRIES``.
    """
    if not isinstance(value, dict):
        raise SettingsError("sessions must be a mapping of session id -> bool")
    if len(value) > MAX_SESSION_ENTRIES:
        raise SettingsError(f"too many session overrides (max {MAX_SESSION_ENTRIES})")
    out: dict[str, bool] = {}
    for sid, val in value.items():
        if not contracts.valid_session_id(sid):
            raise SettingsError(f"not a session id: {sid!r}")
        if not isinstance(val, bool):
            raise SettingsError(f"session override for {sid!r} must be a bool")
        out[sid] = val
    return out


def _normalize_tunnel(value: Any) -> dict[str, str]:
    """Filter a stored/incoming ``tunnel`` mapping down to recognized,
    non-empty string fields. Never raises — used only to CARRY existing
    state forward from ``_read_raw()``, where the value has already passed
    (or, if corrupt, simply won't survive) this filter; validation of a
    caller-supplied value belongs to ``SettingsStore.write_tunnel``."""
    if not isinstance(value, Mapping):
        return {}
    return {
        f: value[f] for f in _TUNNEL_FIELDS
        if isinstance(value.get(f), str) and value.get(f)
    }


def _carry_autokick(payload: dict[str, Any], raw: dict[str, Any]) -> None:
    """Carry the global autokick override forward unchanged. Shared by every
    writer that does NOT itself set autokick, so none of them can drop it —
    the "one writer must never discard another's state" requirement."""
    global_value = raw.get("autokick")
    if isinstance(global_value, bool):
        payload["autokick"] = global_value


def _carry_tunnel(payload: dict[str, Any], raw: dict[str, Any]) -> None:
    """Carry the tunnel override forward unchanged. Shared by every writer
    that does NOT itself set tunnel fields, for the same reason as
    ``_carry_autokick``: the autokick writers predate the tunnel key and
    must not silently drop it when they rewrite the store."""
    tunnel = _normalize_tunnel(raw.get("tunnel"))
    if tunnel:
        payload["tunnel"] = tunnel


class SettingsStore:
    """Read/write the dashboard-managed settings file (autokick toggles and
    tunnel overrides — see the module docstring)."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / FILENAME

    def _read_raw(self) -> dict[str, Any]:
        """The stored file as a dict, or ``{}`` on any read failure.

        Consulted on every watchdog pass (and every dashboard render), so a
        missing OR corrupt file must degrade rather than raise. Callers that
        gate a DESTRUCTIVE action must additionally consult ``is_degraded``
        — see its docstring for why an empty read is not safe on its own.
        """
        data, _degraded = self._read_checked()
        return data

    def _read_checked(self) -> tuple[dict[str, Any], bool]:
        """``(data, degraded)``. ``degraded`` is True only when the file
        EXISTS but could not be understood — never for a missing file."""
        if not self._path.exists():
            return {}, False          # never configured: the normal case
        try:
            data = read_json_file(self._path)
        except (OSError, ValueError):
            return {}, True
        if not isinstance(data, dict):
            return {}, True
        if not contracts.store_version_ok(data, contracts.SETTINGS_STORE_VERSION):
            # A version this build cannot read is DEGRADED, not empty (#36).
            # Reading it as "no overrides" would silently drop every
            # per-session opt-out and re-arm auto-kick against a session the
            # user excluded — the same fail-closed reasoning as a corrupt
            # file, and for the same reason: the action downstream SIGTERMs
            # live processes.
            return {}, True
        try:
            _normalize_sessions(data.get("sessions", {}))
        except SettingsError:
            return data, True
        return data, False

    def is_degraded(self) -> bool:
        """True when a stored settings file exists but cannot be understood.

        An absent file and a corrupt one both read as "no overrides", which
        is fine for display but NOT for licensing a destructive action: a
        corrupt file silently drops every per-session opt-out, so a session
        the user explicitly excluded would become eligible for auto-kick
        again. The watchdog therefore refuses to auto-kick anything while
        this is true — fail closed, because the cost of being wrong is
        restarting a live session the user asked crr to leave alone.
        """
        return self._read_checked()[1]

    def read_global_autokick(self) -> bool | None:
        """The dashboard's global override, or ``None`` (fall back to config)."""
        value = self._read_raw().get("autokick")
        return value if isinstance(value, bool) else None

    def effective_global_autokick(self) -> bool | None:
        """The global override AS THE SYSTEM WILL ACT ON IT.

        Identical to ``read_global_autokick`` except that a degraded store
        reports ``False``. While degraded the watchdog auto-kicks nothing
        (fail closed — see ``is_degraded``), so a card rendered from the raw
        read would claim "auto-kick on" for a system that is kicking
        nothing. Display must not promise behaviour that is not happening;
        the Settings modal surfaces the degraded reason itself.
        """
        if self.is_degraded():
            return False
        return self.read_global_autokick()

    def read_session_overrides(self) -> dict[str, bool]:
        """The full per-session map, or ``{}`` on a missing/corrupt entry."""
        try:
            return _normalize_sessions(self._read_raw().get("sessions", {}))
        except SettingsError:
            return {}

    def read_session_autokick(self, sid: str) -> bool | None:
        """One session's override, or ``None`` (unset -> resolve from global)."""
        return self.read_session_overrides().get(sid)

    def write_global_autokick(self, value: bool | None) -> None:
        """Set (or, with ``None``, clear back to unset) the global override.

        The per-session map is re-validated and carried forward unchanged —
        this is the operation that must NOT discard per-session state, per
        the spec's "survives a global off/on cycle" requirement.
        """
        if value is not None and not isinstance(value, bool):
            raise SettingsError("autokick must be a bool or None")
        raw = self._read_raw()
        sessions = _normalize_sessions(raw.get("sessions", {}))
        payload: dict[str, Any] = {
            "v": contracts.SETTINGS_STORE_VERSION, "sessions": sessions,
        }
        if value is not None:
            payload["autokick"] = value
        _carry_tunnel(payload, raw)
        write_json_atomic(self._path, payload)

    def write_session_autokick(self, sid: str, value: bool) -> None:
        """Set one session's override. Raises ``SettingsError`` on a
        non-UUID ``sid``, a non-bool ``value``, or past the entry bound."""
        if not contracts.valid_session_id(sid):
            raise SettingsError(f"not a session id: {sid!r}")
        if not isinstance(value, bool):
            raise SettingsError("session autokick must be a bool")
        raw = self._read_raw()
        sessions = _normalize_sessions(raw.get("sessions", {}))
        sessions[sid] = value
        if len(sessions) > MAX_SESSION_ENTRIES:
            raise SettingsError(f"too many session overrides (max {MAX_SESSION_ENTRIES})")
        payload: dict[str, Any] = {
            "v": contracts.SETTINGS_STORE_VERSION, "sessions": sessions,
        }
        _carry_autokick(payload, raw)
        _carry_tunnel(payload, raw)
        write_json_atomic(self._path, payload)

    # --- tunnel overrides (spec 2026-09-02) -----------------------------
    # GUI-writable overrides over config.toml's tunnel keys. None = "no
    # override; config.toml rules". Stored under one "tunnel" key so the
    # autokick writers and these can never clobber each other's state.

    def read_tunnel(self) -> dict[str, str | None]:
        """The dashboard's tunnel overrides, one entry per field in
        ``_TUNNEL_FIELDS``, each ``None`` when unset (fall back to
        ``config.toml``). A stored ``""`` reads back as ``None`` too —
        ``""`` means "unset" everywhere in the tunnel design (it's
        ``config.toml``'s own empty-string default), and letting a stored
        empty string survive to the caller would hand
        ``crr.core.tunnel.select``'s override-wins logic a value that is
        falsy but still "present", silently defeating the config fallback.
        Same degrade-to-unset-on-corruption behaviour as the autokick
        reads: a missing or corrupt file (``_read_raw`` returns ``{}``)
        reads as all-``None``, never raises.
        """
        raw = _normalize_tunnel(self._read_raw().get("tunnel"))
        return {f: raw.get(f) for f in _TUNNEL_FIELDS}

    def write_tunnel(
        self,
        provider: str | None = None,
        cloudflare_tunnel_name: str | None = None,
        cloudflare_hostname: str | None = None,
    ) -> None:
        """Replace the stored tunnel override with exactly these three
        fields — a full overwrite, not a merge: a field passed as ``None``
        clears it, so a caller that wants to change one field while
        keeping another must pass the other's current value back in (mirror
        it from a prior ``read_tunnel()``). Raises ``SettingsError`` on an
        unrecognized ``provider`` or a non-string field value; the
        autokick override and session map are re-validated and carried
        forward unchanged, same "must not discard the other writer's
        state" requirement ``write_global_autokick`` documents.
        """
        if provider is not None and provider not in TUNNEL_PROVIDERS:
            raise SettingsError(
                f"tunnel provider must be one of {TUNNEL_PROVIDERS}, got {provider!r}"
            )
        for label, value in (("cloudflare_tunnel_name", cloudflare_tunnel_name),
                             ("cloudflare_hostname", cloudflare_hostname)):
            if value is not None and not isinstance(value, str):
                raise SettingsError(f"{label} must be a string or None")
        raw = self._read_raw()
        sessions = _normalize_sessions(raw.get("sessions", {}))
        payload: dict[str, Any] = {
            "v": contracts.SETTINGS_STORE_VERSION, "sessions": sessions,
        }
        _carry_autokick(payload, raw)
        tunnel_payload = {
            k: v for k, v in (
                ("provider", provider),
                ("cloudflare_tunnel_name", cloudflare_tunnel_name),
                ("cloudflare_hostname", cloudflare_hostname),
            ) if v is not None
        }
        if tunnel_payload:
            payload["tunnel"] = tunnel_payload
        write_json_atomic(self._path, payload)
