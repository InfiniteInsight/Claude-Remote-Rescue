/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */

/**
 * Launch and close decisions, and the tray menu's contents.
 *
 * The app has to keep running after the window is shut — the tray icon is how
 * the user sees and controls crr. So the app lives in the tray and the window
 * is just a view. These are the pure decisions behind that; the Electron
 * wiring is in `tray.ts` (kept out of here so this stays unit-testable without
 * Electron).
 */
import type { ServiceStatus } from './contracts.js';
import type { PollState } from './poll.js';

/** Launch flags this app understands. */
export interface LaunchFlags {
  /** Start with no window — the login-item launch. */
  hidden: boolean;
}

/**
 * Args registered on the login item, so an autostart never pops a window over
 * the user's desktop. This is the only thing that launches the app hidden, so
 * the flag's whole purpose is visible from here.
 */
export const LOGIN_ITEM_ARGS: readonly string[] = ['--hidden'];

/**
 * Parse launch flags. `/hidden` and `-hidden` are accepted alongside
 * `--hidden` because a Windows Run-key or Task Scheduler entry is commonly
 * written in PowerShell style.
 */
export function parseLaunchFlags(argv: readonly string[]): LaunchFlags {
  const seen = new Set(argv.map((a) => a.toLowerCase()));
  return {
    hidden: ['--hidden', '/hidden', '-hidden', '--autostart'].some((f) =>
      seen.has(f),
    ),
  };
}

/**
 * Whether a window-close should be swallowed in favour of hiding.
 *
 * Only while there is a tray: without one, a hidden window is unrecoverable,
 * so closing has to mean leaving.
 */
export function shouldHideInsteadOfClose(opts: {
  trayAvailable: boolean;
  quitting: boolean;
}): boolean {
  return opts.trayAvailable && !opts.quitting;
}

/**
 * Whether the last window closing should end the app. The inverse case above:
 * no tray means no way back in. macOS keeps running on an empty dock by
 * convention, so it never quits here.
 */
export function shouldQuitOnWindowClosed(opts: {
  platform: NodeJS.Platform;
  trayAvailable: boolean;
}): boolean {
  if (opts.platform === 'darwin') return false;
  return !opts.trayAvailable;
}

/** Labels for the tray menu. */
export const TRAY_LABELS = {
  open: 'Open CRR',
  dashboard: 'Open dashboard in browser',
  start: 'Start CRR',
  stop: 'Stop CRR',
  loginItem: 'Start at login',
  quit: 'Quit tray app',
} as const;

export type MenuItemId =
  | 'status'
  | 'open'
  | 'dashboard'
  | 'startStop'
  | 'loginItem'
  | 'quit';

export interface MenuItemSpec {
  id: MenuItemId;
  label: string;
  enabled: boolean;
  /** Present only on the checkbox item. */
  checked?: boolean;
}

function unitActive(service: ServiceStatus | undefined, name: string): boolean {
  return service?.services.some((u) => u.name === name && u.active) ?? false;
}

/**
 * Decide the tray menu's contents for the current app state, top to bottom:
 * status line, Open CRR, Open dashboard in browser, Start/Stop CRR, Start at
 * login (packaged builds only), Quit tray app.
 */
export function trayMenuSpec(opts: {
  state: PollState;
  packaged: boolean;
  loginItemEnabled: boolean;
  busy: boolean;
}): MenuItemSpec[] {
  const { state, busy } = opts;
  const service = state.service;
  const dashboardUp = unitActive(service, 'dashboard');
  // Stop is offered only when both essential units are up; keep-awake alone
  // being down does not matter. Anything else reads Start.
  const running = dashboardUp && unitActive(service, 'watchdog');
  // Start stays enabled while WSL is down: it is an explicit user action, and
  // booting WSL is exactly what the user asked for.
  const startStopDisabled =
    busy ||
    service?.backend === 'unsupported' ||
    service?.state === 'not-installed';

  const items: MenuItemSpec[] = [
    { id: 'status', label: state.statusLine, enabled: false },
    { id: 'open', label: TRAY_LABELS.open, enabled: true },
    { id: 'dashboard', label: TRAY_LABELS.dashboard, enabled: dashboardUp },
    {
      id: 'startStop',
      label: running ? TRAY_LABELS.stop : TRAY_LABELS.start,
      enabled: !startStopDisabled,
    },
  ];
  // Omitted when unpackaged: a login item would register Electron itself, not
  // this app.
  if (opts.packaged) {
    items.push({
      id: 'loginItem',
      label: TRAY_LABELS.loginItem,
      enabled: true,
      checked: opts.loginItemEnabled,
    });
  }
  items.push({ id: 'quit', label: TRAY_LABELS.quit, enabled: true });
  return items;
}

/**
 * A stable string of everything visible in the menu: each item's id, label,
 * enabled and checked. The tray rebuilds the context menu only when this
 * changes, because on Windows `setContextMenu` while the menu is open can close
 * it under the user's cursor, and a poll lands every few seconds.
 */
export function menuSignature(items: ReturnType<typeof trayMenuSpec>): string {
  return JSON.stringify(
    items.map((i) => [i.id, i.label, i.enabled, i.checked ?? null]),
  );
}
