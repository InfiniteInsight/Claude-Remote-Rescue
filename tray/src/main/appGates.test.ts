import { describe, expect, it } from 'vitest';
import type { ServiceStatus } from './contracts.js';
import type { CrrResult } from './crrClient.js';
import type { PollState } from './poll.js';
import {
  dashboardActive,
  qrGate,
  qrStateAllows,
  shouldForgetCrrPath,
  stateIsFresh,
  type QrGateInput,
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
const DISTRO = 'Ubuntu-24.04';

/** Everything qrGate needs, all saying yes; each test changes one input. */
const OPEN: QrGateInput = {
  running: [DISTRO],
  distro: DISTRO,
  held: false,
  state: RUNNING,
  suspended: false,
};

describe('qrGate', () => {
  // test_qr_gate
  it('is true only for a freshly listed distro, no live hold, a running state with the dashboard up, awake', () => {
    expect(qrGate(OPEN)).toBe(true);
  });

  it('refuses when the last poll says running but a fresh listing no longer shows the distro (wsl --shutdown)', () => {
    expect(qrGate({ ...OPEN, running: [] })).toBe(false);
    expect(qrGate({ ...OPEN, running: ['docker-desktop'] })).toBe(false);
  });

  it('refuses when wsl.exe could not list running distros', () => {
    expect(qrGate({ ...OPEN, running: undefined })).toBe(false);
  });

  it('refuses with no distro chosen, even if something is running', () => {
    expect(qrGate({ ...OPEN, distro: undefined })).toBe(false);
  });

  it('refuses while a live read finds a hold, whatever the last poll said', () => {
    expect(qrGate({ ...OPEN, held: true })).toBe(false);
  });

  it('refuses with no state (before the first poll, or cleared on suspend)', () => {
    expect(qrGate({ ...OPEN, state: undefined })).toBe(false);
  });

  it('refuses while suspended, even with a pre-sleep state that says running', () => {
    expect(qrGate({ ...OPEN, suspended: true })).toBe(false);
  });

  it('refuses unless the last poll says wsl is running', () => {
    for (const wsl of ['missing', 'stopped', 'held'] as const) {
      expect(qrGate({ ...OPEN, state: { ...RUNNING, wsl } }), wsl).toBe(false);
    }
  });

  it('refuses when the dashboard unit is off or the service is unknown', () => {
    expect(qrGate({ ...OPEN, state: { ...RUNNING, service: service(false) } })).toBe(false);
    expect(qrGate({ ...OPEN, state: { ...RUNNING, service: undefined } })).toBe(false);
  });
});

describe('qrStateAllows', () => {
  it('is the part of qrGate that needs no wsl.exe call: the last poll and the sleep flag', () => {
    expect(qrStateAllows(RUNNING, false)).toBe(true);
    expect(qrStateAllows(undefined, false)).toBe(false);
    expect(qrStateAllows(RUNNING, true)).toBe(false);
    expect(qrStateAllows({ ...RUNNING, wsl: 'stopped' }, false)).toBe(false);
    expect(qrStateAllows({ ...RUNNING, service: service(false) }, false)).toBe(false);
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
