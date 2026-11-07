import type { QrInfo, ServiceStatus } from './contracts.js';
import type { CrrResult } from './crrClient.js';
import type { PollState } from './poll.js';
import { TRAY_LABELS, trayMenuSpec } from './appLifecycle.js';
import { sessionsPanel, type SessionsPanel } from './sessionsView.js';

/**
 * The Control tab, as data. The renderer is a dumb DOM render of this: every
 * decision (labels, which buttons exist, what the banner says) is made here so
 * it can be tested without a window.
 */

export type RowName = 'watchdog' | 'dashboard' | 'keep-awake';
export type Dot = 'on' | 'off' | 'missing';

export type Phone =
  | { kind: 'qr'; url: string; image: string }
  | { kind: 'hint'; text: string }
  | { kind: 'none'; text: string };

export interface ControlView {
  rows: Array<{ name: RowName; label: string; dot: Dot }>;
  startStop: { label: string; enabled: boolean };
  repair: { visible: boolean; enabled: boolean };
  phone: Phone;
  /** Whether the dashboard asks for a passphrase (crr's `dashboard_login`); unknown without a service answer. */
  passphrase: 'set' | 'not set' | 'unknown';
  login: 'on' | 'off' | 'unknown';
  /** The Sessions tab's panel, under (or instead of) the dashboard view. */
  sessions: SessionsPanel;
  banner?: string;
  distros: string[];
  distro?: string;
}

export interface ControlViewInput {
  state: PollState;
  qr?: CrrResult<QrInfo>;
  qrImage?: string;
  busy: boolean;
  lastAction?: { ok: boolean; message: string };
  distros: string[];
  distro?: string;
  crrPath?: { ok: boolean; error?: string };
  /** Whether the OS login item is on; omitted (unpackaged run) reads 'unknown'. */
  login?: boolean;
  /** Whether the dashboard view is on screen over the Sessions panel; omitted reads false. */
  sessionsShown?: boolean;
}

const ROWS: ReadonlyArray<{ name: RowName; label: string }> = [
  { name: 'watchdog', label: 'Watchdog (revives sessions)' },
  { name: 'dashboard', label: 'Dashboard' },
  { name: 'keep-awake', label: 'Keep-awake' },
];

const DEGRADED = 'the tunnel answer may be out of date';

/** The banner while crr's holder holds WSL down: Start stays enabled, so say what it does. */
export const HELD_BANNER =
  'crr holder is holding WSL down. Start CRR boots WSL anyway, but the hold stays — run crr holder resume to lift it.';

function dotFor(service: ServiceStatus | undefined, name: string): Dot {
  const unit = service?.services.find((u) => u.name === name);
  if (unit === undefined || !unit.installed) return 'missing';
  return unit.active ? 'on' : 'off';
}

function phoneFor(input: ControlViewInput, dashboardOn: boolean): Phone {
  const { state, qr, qrImage } = input;
  // A QR fetched while crr was up must not outlive it.
  if (state.wsl !== 'running' || !dashboardOn) {
    return { kind: 'none', text: 'The phone link appears when CRR is running' };
  }
  if (qr === undefined) return { kind: 'none', text: 'Checking the phone link…' };
  if (!qr.ok) return { kind: 'none', text: `Phone link unavailable: ${qr.error}` };
  const info = qr.value;
  if (info.url !== null) {
    return qrImage !== undefined
      ? { kind: 'qr', url: info.url, image: qrImage }
      : { kind: 'none', text: 'Preparing the QR code…' };
  }
  if (info.hint !== null) {
    return { kind: 'hint', text: `To reach it from your phone: ${info.hint}` };
  }
  return { kind: 'none', text: 'No phone link is available' };
}

/**
 * Failed action, then the crr path error, then the hold (what Start does
 * under it), then the wsl-down / stopped line; degraded is appended.
 */
function bannerFor(input: ControlViewInput): string | undefined {
  const { state, lastAction, crrPath } = input;
  let main: string | undefined;
  if (lastAction !== undefined && !lastAction.ok && lastAction.message !== '') {
    main = lastAction.message;
  } else if (crrPath !== undefined && !crrPath.ok) {
    main = `crr not found in ${input.distro ?? 'WSL'}: ${crrPath.error ?? 'unknown error'}`;
  } else if (state.wsl === 'held') {
    main = HELD_BANNER;
  } else if (state.icon === 'wsl-down' || state.icon === 'stopped') {
    main = state.statusLine;
  }
  const degraded =
    state.wsl === 'running' && input.qr !== undefined && input.qr.ok && input.qr.value.degraded;
  if (!degraded) return main;
  return main === undefined
    ? `${DEGRADED.charAt(0).toUpperCase()}${DEGRADED.slice(1)}`
    : `${main}; ${DEGRADED}`;
}

export function controlView(input: ControlViewInput): ControlView {
  const { state, busy } = input;
  const service = state.service;

  const rows = ROWS.map((r) => ({ ...r, dot: dotFor(service, r.name) }));
  const dashboardOn = rows.find((r) => r.name === 'dashboard')?.dot === 'on';

  // Same rule as the tray menu, so the two never disagree about Start/Stop.
  const item = trayMenuSpec({
    state,
    packaged: false,
    loginItemEnabled: false,
    busy,
  }).find((i) => i.id === 'startStop');
  const startStop = { label: item?.label ?? TRAY_LABELS.start, enabled: item?.enabled ?? false };

  const view: ControlView = {
    rows,
    startStop,
    repair: {
      visible: service?.backend === 'systemd' && service.state === 'not-installed',
      enabled: !busy,
    },
    phone: phoneFor(input, dashboardOn),
    passphrase: service === undefined ? 'unknown' : service.dashboard_login ? 'set' : 'not set',
    login: input.login === undefined ? 'unknown' : input.login ? 'on' : 'off',
    sessions: sessionsPanel(state, input.sessionsShown ?? false),
    distros: input.distros,
    distro: input.distro,
  };
  const banner = bannerFor(input);
  if (banner !== undefined) view.banner = banner;
  return view;
}
