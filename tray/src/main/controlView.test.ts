import { describe, expect, it } from 'vitest';
import type { QrInfo, ServiceStatus, ServiceUnit } from './contracts.js';
import type { CrrResult } from './crrClient.js';
import { controlView, type ControlViewInput } from './controlView.js';
import type { PollState } from './poll.js';

function unit(name: string, active: boolean, installed = true): ServiceUnit {
  return { name, unit: `crr-${name}.service`, installed, enabled: installed, active };
}

function service(over: Partial<ServiceStatus> = {}, units?: ServiceUnit[]): ServiceStatus {
  return {
    contract: 1,
    backend: 'systemd',
    reason: '',
    state: 'running',
    services: units ?? [unit('watchdog', true), unit('dashboard', true), unit('keep-awake', true)],
    dashboard_url: 'http://127.0.0.1:8377/',
    dashboard_login: true,
    ...over,
  };
}

function state(over: Partial<PollState> = {}): PollState {
  return { icon: 'running', statusLine: 'CRR running · 2 sessions, 0 crashed', wsl: 'running', ...over };
}

const RUNNING = state({ service: service() });
const NOT_INSTALLED = state({
  icon: 'stopped',
  statusLine: 'CRR is not installed',
  service: service(
    { state: 'not-installed' },
    [unit('watchdog', false, false), unit('dashboard', false, false), unit('keep-awake', false, false)],
  ),
});
const UNSUPPORTED = state({
  icon: 'stopped',
  statusLine: 'no systemd in this distro',
  service: service({ backend: 'unsupported', reason: 'no systemd in this distro', state: 'stopped' }, []),
});

const qrInfo = (over: Partial<QrInfo> = {}): CrrResult<QrInfo> => ({
  ok: true,
  value: {
    contract: 1,
    provider: 'tailscale',
    url: 'https://host.tailnet.ts.net/',
    loopback_url: 'http://127.0.0.1:8377/',
    hint: null,
    dashboard_reachable: true,
    degraded: false,
    ...over,
  },
});

function view(over: Partial<ControlViewInput> = {}) {
  return controlView({ state: RUNNING, busy: false, distros: ['Ubuntu'], distro: 'Ubuntu', ...over });
}

describe('controlView rows and buttons', () => {
  // test_control_view_rows_and_buttons
  it('shows three on dots and Stop CRR when running', () => {
    const v = view();
    expect(v.rows).toEqual([
      { name: 'watchdog', label: 'Watchdog (revives sessions)', dot: 'on' },
      { name: 'dashboard', label: 'Dashboard', dot: 'on' },
      { name: 'keep-awake', label: 'Keep-awake', dot: 'on' },
    ]);
    expect(v.startStop).toEqual({ label: 'Stop CRR', enabled: true });
    expect(v.repair.visible).toBe(false);
  });

  it('reads Start CRR when a unit is off, and marks the off unit', () => {
    const v = view({
      state: state({
        icon: 'stopped',
        statusLine: 'CRR stopped',
        service: service({ state: 'partial' }, [unit('watchdog', false), unit('dashboard', true), unit('keep-awake', true)]),
      }),
    });
    expect(v.rows.map((r) => r.dot)).toEqual(['off', 'on', 'on']);
    expect(v.startStop).toEqual({ label: 'Start CRR', enabled: true });
  });

  it('keep-awake alone being off still reads Stop CRR', () => {
    const v = view({
      state: state({ service: service({}, [unit('watchdog', true), unit('dashboard', true), unit('keep-awake', false)]) }),
    });
    expect(v.rows.map((r) => r.dot)).toEqual(['on', 'on', 'off']);
    expect(v.startStop.label).toBe('Stop CRR');
  });

  it('shows Repair for a not-installed systemd backend, and no Start', () => {
    const v = view({ state: NOT_INSTALLED });
    expect(v.repair).toEqual({ visible: true, enabled: true });
    expect(v.rows.map((r) => r.dot)).toEqual(['missing', 'missing', 'missing']);
    expect(v.startStop.enabled).toBe(false);
  });

  it('shows no Repair for an unsupported backend and puts the reason in the banner', () => {
    const v = view({ state: UNSUPPORTED });
    expect(v.repair.visible).toBe(false);
    expect(v.banner).toBe('no systemd in this distro');
    expect(v.startStop.enabled).toBe(false);
  });

  it('disables Start/Stop and Repair while an action runs', () => {
    expect(view({ busy: true }).startStop.enabled).toBe(false);
    expect(view({ state: NOT_INSTALLED, busy: true }).repair).toEqual({ visible: true, enabled: false });
  });

  it('keeps Start enabled while WSL is down, since starting is an explicit boot', () => {
    const v = view({ state: state({ icon: 'wsl-down', statusLine: 'WSL is stopped', wsl: 'stopped' }) });
    expect(v.startStop).toEqual({ label: 'Start CRR', enabled: true });
    expect(v.rows.map((r) => r.dot)).toEqual(['missing', 'missing', 'missing']);
  });

  it('passes the distro choice and login state through', () => {
    expect(view({ distros: ['A', 'B'], distro: 'B' })).toMatchObject({ distros: ['A', 'B'], distro: 'B' });
    expect(view().login).toBe('unknown');
    expect(view({ login: true }).login).toBe('on');
    expect(view({ login: false }).login).toBe('off');
  });
});

describe('controlView phone', () => {
  // test_control_view_phone
  it('is a qr when the call gave a url and there is an image', () => {
    expect(view({ qr: qrInfo(), qrImage: 'data:image/png;base64,AAAA' }).phone).toEqual({
      kind: 'qr',
      url: 'https://host.tailnet.ts.net/',
      image: 'data:image/png;base64,AAAA',
    });
  });

  it('is the exact hint when the url is null', () => {
    const v = view({ qr: qrInfo({ url: null, provider: null, hint: 'run tailscale up' }) });
    expect(v.phone).toEqual({ kind: 'hint', text: 'To reach it from your phone: run tailscale up' });
  });

  it('is none on an error', () => {
    const v = view({ qr: { ok: false, error: 'boom', exit: 1, mismatch: false } });
    expect(v.phone.kind).toBe('none');
    expect(v.phone).toMatchObject({ text: expect.stringContaining('boom') });
  });

  it('is none before the first answer, and when a url has no image yet', () => {
    expect(view().phone.kind).toBe('none');
    expect(view({ qr: qrInfo() }).phone.kind).toBe('none');
  });

  it('is none without a hint or url', () => {
    expect(view({ qr: qrInfo({ url: null, hint: null }) }).phone.kind).toBe('none');
  });

  it('ignores a stale answer once WSL is no longer running or the dashboard is off', () => {
    const down = state({ icon: 'wsl-down', statusLine: 'WSL is stopped', wsl: 'stopped' });
    expect(view({ state: down, qr: qrInfo(), qrImage: 'data:x' }).phone.kind).toBe('none');
    expect(view({ state: NOT_INSTALLED, qr: qrInfo(), qrImage: 'data:x' }).phone.kind).toBe('none');
  });
});

describe('controlView banner', () => {
  // test_control_view_banner_priority
  const failed = { ok: false, message: 'crr service start failed: unit masked' };
  const noCrr = { ok: false, error: 'could not locate crr: exit 1' };
  const stopped = state({ icon: 'stopped', statusLine: 'CRR stopped', service: service({ state: 'stopped' }) });
  const degradedQr = qrInfo({ degraded: true });

  it('has no banner when all is well', () => {
    expect(view().banner).toBeUndefined();
    expect(view({ lastAction: { ok: true, message: 'started' } }).banner).toBeUndefined();
  });

  it('puts a failed action first, over the path error and the status line', () => {
    expect(view({ state: stopped, crrPath: noCrr, lastAction: failed }).banner).toBe(failed.message);
  });

  it('puts the crr path error second', () => {
    expect(view({ state: stopped, crrPath: noCrr }).banner).toBe(
      'crr not found in Ubuntu: could not locate crr: exit 1',
    );
  });

  it('puts the wsl-down and stopped status lines third', () => {
    expect(view({ state: stopped }).banner).toBe('CRR stopped');
    const down = state({ icon: 'wsl-down', statusLine: 'WSL is stopped', wsl: 'stopped' });
    expect(view({ state: down }).banner).toBe('WSL is stopped');
  });

  it('appends the degraded warning last', () => {
    expect(view({ qr: degradedQr, qrImage: 'data:x' }).banner).toBe('The tunnel answer may be out of date');
    expect(view({ qr: degradedQr, qrImage: 'data:x', lastAction: failed }).banner).toBe(
      `${failed.message}; the tunnel answer may be out of date`,
    );
  });

  it('does not report a running status line as a problem', () => {
    expect(view({ state: state({ icon: 'attention', statusLine: 'CRR running · 3 sessions, 1 crashed', service: service() }) }).banner).toBeUndefined();
  });
});

describe('controlView sessions panel', () => {
  // test_control_view_sessions_panel
  it('is stopped while the dashboard unit is off or WSL is not running', () => {
    const off = state({ service: service({}, [unit('watchdog', true), unit('dashboard', false)]) });
    expect(view({ state: off }).sessions).toEqual({ kind: 'stopped' });
    for (const wsl of ['missing', 'stopped', 'held'] as const) {
      expect(view({ state: state({ wsl }) }).sessions).toEqual({ kind: 'stopped' });
      // Even a state still carrying a service that says the dashboard is up.
      expect(view({ state: state({ wsl, service: service() }) }).sessions).toEqual({ kind: 'stopped' });
    }
    expect(view({ state: NOT_INSTALLED }).sessions).toEqual({ kind: 'stopped' });
  });

  it('says it is loading while the dashboard is up but its view is not on screen yet', () => {
    expect(view().sessions).toEqual({ kind: 'loading', text: 'Loading the dashboard…' });
    expect(view({ sessionsShown: false }).sessions).toEqual({
      kind: 'loading',
      text: 'Loading the dashboard…',
    });
  });

  it('is showing once the dashboard view is drawn over the panel', () => {
    expect(view({ sessionsShown: true }).sessions).toEqual({ kind: 'showing' });
  });

  it('says plainly when crr reports a dashboard address the tray will not load', () => {
    for (const url of ['http://example.com:8377/', 'file:///C:/x', '<b>not a url</b>']) {
      const odd = state({ service: service({ dashboard_url: url }) });
      for (const sessionsShown of [false, true]) {
        expect(view({ state: odd, sessionsShown }).sessions).toEqual({
          kind: 'refused',
          text: `crr reported a dashboard address the tray won't load: ${url}`,
        });
      }
    }
  });
});
