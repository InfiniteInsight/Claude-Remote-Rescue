import { describe, expect, it } from 'vitest';
import type { ServiceStatus } from './contracts.js';
import type { CrrResult } from './crrClient.js';
import type { PollState } from './poll.js';
import {
  canRefreshQr,
  dashboardActive,
  shouldForgetCrrPath,
  stateIsFresh,
} from './appGates.js';

function service(dashboard: boolean): ServiceStatus {
  return {
    contract: 1,
    backend: 'systemd',
    reason: '',
    state: 'running',
    services: [
      { name: 'watchdog', unit: 'w', installed: true, enabled: true, active: true },
      { name: 'dashboard', unit: 'd', installed: true, enabled: true, active: dashboard },
    ],
    dashboard_url: 'http://127.0.0.1:8377/',
    dashboard_login: true,
  };
}

const RUNNING: PollState = { icon: 'running', statusLine: 'CRR running', service: service(true), wsl: 'running' };

describe('canRefreshQr', () => {
  it('is true only for a running distro with the dashboard up, not suspended, not held', () => {
    expect(canRefreshQr(RUNNING, false, false)).toBe(true);
  });

  it('is false with no state (before the first poll, or cleared on suspend)', () => {
    expect(canRefreshQr(undefined, false, false)).toBe(false);
  });

  it('is false while suspended, even with a pre-sleep state that says running', () => {
    expect(canRefreshQr(RUNNING, true, false)).toBe(false);
  });

  it('is false while a hold is in place', () => {
    expect(canRefreshQr(RUNNING, false, true)).toBe(false);
  });

  it('is false unless wsl is running', () => {
    for (const wsl of ['missing', 'stopped', 'held'] as const) {
      expect(canRefreshQr({ ...RUNNING, wsl }, false, false)).toBe(false);
    }
  });

  it('is false when the dashboard unit is off or the service is unknown', () => {
    expect(canRefreshQr({ ...RUNNING, service: service(false) }, false, false)).toBe(false);
    expect(canRefreshQr({ ...RUNNING, service: undefined }, false, false)).toBe(false);
  });
});

describe('dashboardActive', () => {
  it('reads the dashboard unit', () => {
    expect(dashboardActive(RUNNING)).toBe(true);
    expect(dashboardActive({ ...RUNNING, service: service(false) })).toBe(false);
    expect(dashboardActive(undefined)).toBe(false);
  });
});

describe('stateIsFresh', () => {
  it('trusts every state while not suspended', () => {
    expect(stateIsFresh(false, undefined, 5)).toBe(true);
  });

  it('distrusts a state that lands during sleep, before any resume', () => {
    expect(stateIsFresh(true, undefined, 5)).toBe(false);
  });

  it('distrusts a poll that began before the resume, trusts one that began at or after it', () => {
    expect(stateIsFresh(true, 100, 99)).toBe(false);
    expect(stateIsFresh(true, 100, 100)).toBe(true);
    expect(stateIsFresh(true, 100, 250)).toBe(true);
  });
});

describe('shouldForgetCrrPath', () => {
  const fail = (error: string, mismatch = false): CrrResult<never> => ({ ok: false, error, exit: 127, mismatch });

  it('forgets the path when the launcher is gone', () => {
    expect(shouldForgetCrrPath(fail('/home/e/.local/state/crr/app/bin/crr: No such file or directory'))).toBe(true);
    expect(shouldForgetCrrPath(fail('crr: command not found'))).toBe(true);
    expect(shouldForgetCrrPath(fail('exec: not found'))).toBe(true);
  });

  it('keeps the path for other failures', () => {
    expect(shouldForgetCrrPath(fail('boom'))).toBe(false);
    expect(shouldForgetCrrPath(fail('timed out'))).toBe(false);
  });

  it('keeps the path for a version mismatch, even one whose text says not found', () => {
    expect(shouldForgetCrrPath(fail('invalid choice: service (not found)', true))).toBe(false);
  });

  it('keeps the path after a success', () => {
    expect(shouldForgetCrrPath({ ok: true, value: 'not found' })).toBe(false);
  });
});
