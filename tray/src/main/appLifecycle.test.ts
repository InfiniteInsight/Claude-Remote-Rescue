/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { describe, expect, it } from 'vitest';
import type { ServiceStatus } from './contracts.js';
import type { PollState } from './poll.js';
import {
  LOGIN_ITEM_ARGS,
  parseLaunchFlags,
  shouldHideInsteadOfClose,
  shouldQuitOnWindowClosed,
  TRAY_LABELS,
  menuSignature,
  trayMenuSpec,
  type MenuItemId,
} from './appLifecycle.js';

describe('parseLaunchFlags', () => {
  it('is visible by default', () => {
    expect(parseLaunchFlags([])).toEqual({ hidden: false });
    expect(parseLaunchFlags(['.'])).toEqual({ hidden: false });
  });

  it('starts hidden for the login-item launch, in any of its spellings', () => {
    for (const flag of ['--hidden', '/hidden', '-hidden', '--autostart']) {
      expect(parseLaunchFlags(['C:\\app\\Launcher.exe', flag]).hidden).toBe(
        true,
      );
    }
  });

  it('ignores case, as Windows does', () => {
    expect(parseLaunchFlags(['--HIDDEN']).hidden).toBe(true);
  });

  it('registers the login item with the hidden flag, so a boot never pops a window', () => {
    expect(parseLaunchFlags([...LOGIN_ITEM_ARGS]).hidden).toBe(true);
  });
});

describe('shouldHideInsteadOfClose', () => {
  it('hides while a tray can bring the window back', () => {
    expect(
      shouldHideInsteadOfClose({ trayAvailable: true, quitting: false }),
    ).toBe(true);
  });

  it('lets the window close when there is no tray to show it again', () => {
    expect(
      shouldHideInsteadOfClose({ trayAvailable: false, quitting: false }),
    ).toBe(false);
  });

  it('lets it close during a real quit, or a hidden window is unrecoverable', () => {
    expect(
      shouldHideInsteadOfClose({ trayAvailable: true, quitting: true }),
    ).toBe(false);
  });
});

describe('shouldQuitOnWindowClosed', () => {
  it('quits with no tray, which is the pre-tray behaviour', () => {
    expect(
      shouldQuitOnWindowClosed({ platform: 'win32', trayAvailable: false }),
    ).toBe(true);
  });

  it('stays alive with a tray — the app must outlive the window', () => {
    expect(
      shouldQuitOnWindowClosed({ platform: 'win32', trayAvailable: true }),
    ).toBe(false);
  });

  it('never quits on an empty dock', () => {
    expect(
      shouldQuitOnWindowClosed({ platform: 'darwin', trayAvailable: false }),
    ).toBe(false);
  });
});

function service(
  active: { watchdog: boolean; dashboard: boolean },
  over: Partial<ServiceStatus> = {},
): ServiceStatus {
  const unit = (name: string, on: boolean) => ({
    name,
    unit: `crr-${name}.service`,
    installed: true,
    enabled: true,
    active: on,
  });
  return {
    contract: 1,
    backend: 'systemd',
    reason: '',
    state: 'running',
    services: [
      unit('watchdog', active.watchdog),
      unit('dashboard', active.dashboard),
      unit('keep-awake', false),
    ],
    dashboard_url: 'http://127.0.0.1:8765/',
    dashboard_login: false,
    ...over,
  };
}

const RUNNING: PollState = {
  icon: 'running',
  statusLine: 'crr is running',
  service: service({ watchdog: true, dashboard: true }),
  wsl: 'running',
};
const STOPPED: PollState = {
  icon: 'stopped',
  statusLine: 'crr is stopped',
  service: service({ watchdog: false, dashboard: false }, { state: 'stopped' }),
  wsl: 'running',
};
const WSL_DOWN: PollState = {
  icon: 'wsl-down',
  statusLine: 'WSL is stopped',
  wsl: 'stopped',
};

const spec = (
  state: PollState,
  over: Partial<{ packaged: boolean; loginItemEnabled: boolean; busy: boolean }> = {},
) =>
  trayMenuSpec({
    state,
    packaged: true,
    loginItemEnabled: false,
    busy: false,
    ...over,
  });
const item = (
  items: ReturnType<typeof trayMenuSpec>,
  id: MenuItemId,
) => items.find((i) => i.id === id);

describe('trayMenuSpec', () => {
  it('test_tray_menu_order_and_labels', () => {
    const items = spec(RUNNING);
    expect(items.map((i) => i.id)).toEqual([
      'status',
      'open',
      'dashboard',
      'startStop',
      'loginItem',
      'quit',
    ]);
    expect(item(items, 'status')).toMatchObject({
      label: 'crr is running',
      enabled: false,
    });
    expect(item(items, 'open')?.label).toBe(TRAY_LABELS.open);
    expect(item(items, 'dashboard')?.label).toBe(TRAY_LABELS.dashboard);
    expect(item(items, 'loginItem')?.label).toBe(TRAY_LABELS.loginItem);
    expect(item(items, 'quit')?.label).toBe(TRAY_LABELS.quit);
    expect(TRAY_LABELS).toEqual({
      open: 'Open CRR',
      dashboard: 'Open dashboard in browser',
      start: 'Start CRR',
      stop: 'Stop CRR',
      loginItem: 'Start at login',
      quit: 'Quit tray app',
    });
  });

  it('keeps the status line disabled whatever the state', () => {
    for (const s of [RUNNING, STOPPED, WSL_DOWN]) {
      expect(item(spec(s), 'status')?.enabled).toBe(false);
    }
  });

  it('test_start_stop_label_and_enablement', () => {
    expect(item(spec(RUNNING), 'startStop')).toMatchObject({
      label: 'Stop CRR',
      enabled: true,
    });
    expect(item(spec(STOPPED), 'startStop')).toMatchObject({
      label: 'Start CRR',
      enabled: true,
    });
    // WSL down: Start is an explicit action that may boot WSL.
    expect(item(spec(WSL_DOWN), 'startStop')).toMatchObject({
      label: 'Start CRR',
      enabled: true,
    });
    const notInstalled: PollState = {
      ...STOPPED,
      service: service(
        { watchdog: false, dashboard: false },
        { state: 'not-installed' },
      ),
    };
    expect(item(spec(notInstalled), 'startStop')).toMatchObject({
      label: 'Start CRR',
      enabled: false,
    });
    const unsupported: PollState = {
      ...STOPPED,
      service: service(
        { watchdog: false, dashboard: false },
        { backend: 'unsupported', state: 'not-installed' },
      ),
    };
    expect(item(spec(unsupported), 'startStop')?.enabled).toBe(false);
    expect(item(spec(RUNNING, { busy: true }), 'startStop')?.enabled).toBe(false);
  });

  it('reads Start CRR when only one of the two essential units is up', () => {
    const partial: PollState = {
      ...RUNNING,
      service: service({ watchdog: true, dashboard: false }, { state: 'partial' }),
    };
    expect(item(spec(partial), 'startStop')).toMatchObject({
      label: 'Start CRR',
      enabled: true,
    });
  });

  it('enables the dashboard item only when the dashboard unit is active', () => {
    expect(item(spec(RUNNING), 'dashboard')?.enabled).toBe(true);
    expect(item(spec(STOPPED), 'dashboard')?.enabled).toBe(false);
    expect(item(spec(WSL_DOWN), 'dashboard')?.enabled).toBe(false);
    const dashOnly: PollState = {
      ...RUNNING,
      service: service({ watchdog: false, dashboard: true }, { state: 'partial' }),
    };
    expect(item(spec(dashOnly), 'dashboard')?.enabled).toBe(true);
  });

  it('keeps Open CRR and Quit enabled, even while busy', () => {
    const items = spec(WSL_DOWN, { busy: true });
    expect(item(items, 'open')?.enabled).toBe(true);
    expect(item(items, 'quit')?.enabled).toBe(true);
  });

  it('test_login_item_only_when_packaged', () => {
    const packaged = spec(RUNNING, { packaged: true, loginItemEnabled: true });
    expect(item(packaged, 'loginItem')).toMatchObject({
      label: 'Start at login',
      enabled: true,
      checked: true,
    });
    const unpackaged = spec(RUNNING, { packaged: false, loginItemEnabled: true });
    expect(item(unpackaged, 'loginItem')).toBeUndefined();
    expect(unpackaged.map((i) => i.id)).toEqual([
      'status',
      'open',
      'dashboard',
      'startStop',
      'quit',
    ]);
  });

  it('reflects the OS state in the login checkbox', () => {
    expect(item(spec(RUNNING, { loginItemEnabled: false }), 'loginItem')?.checked).toBe(false);
  });
});

describe('menuSignature', () => {
  it('test_menu_signature_stable_for_equal_specs', () => {
    expect(menuSignature(spec(RUNNING))).toBe(menuSignature(spec(RUNNING)));
    // A different but equal-looking state object gives the same signature.
    const copy: PollState = JSON.parse(JSON.stringify(RUNNING));
    expect(menuSignature(spec(copy))).toBe(menuSignature(spec(RUNNING)));
  });

  it('test_menu_signature_changes_with_label_or_enablement', () => {
    const sigs = new Set([
      menuSignature(spec(RUNNING)),
      menuSignature(spec(STOPPED)), // Stop CRR vs Start CRR, dashboard disabled
      menuSignature(spec(RUNNING, { busy: true })), // startStop disabled
    ]);
    expect(sigs.size).toBe(3);
  });

  it('changes with the status line and the login checkbox', () => {
    expect(menuSignature(spec({ ...RUNNING, statusLine: 'other' }))).not.toBe(
      menuSignature(spec(RUNNING)),
    );
    expect(menuSignature(spec(RUNNING, { loginItemEnabled: true }))).not.toBe(
      menuSignature(spec(RUNNING, { loginItemEnabled: false })),
    );
  });

  it('does not run fields together into a false match', () => {
    const a = menuSignature([{ id: 'open', label: 'a|b', enabled: true }]);
    const b = menuSignature([{ id: 'open', label: 'a', enabled: true }]);
    expect(a).not.toBe(b);
  });
});
