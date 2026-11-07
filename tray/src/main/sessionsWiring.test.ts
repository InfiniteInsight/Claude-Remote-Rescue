import { describe, expect, it } from 'vitest';
import { wireSessionsView, type WiredContents, type WiredSession } from './sessionsWiring.js';

const ORIGIN = 'http://127.0.0.1:8377';

type Handler = (...args: any[]) => any;

/** Fake Session and WebContents that record, in one shared log, what was registered and when. */
function fakes(opts: { load?: () => Promise<unknown>; session?: FakeSession } = {}) {
  const session = opts.session ?? fakeSession();
  const log = session.log;
  const wcListeners = new Map<string, Handler[]>();
  let windowOpen: Handler | undefined;
  const wc: WiredContents = {
    session: session.ses,
    on(event, listener) {
      log.push(`wc.on:${event}`);
      wcListeners.set(event, [...(wcListeners.get(event) ?? []), listener]);
      return wc;
    },
    setWindowOpenHandler(handler) {
      log.push('wc.setWindowOpenHandler');
      windowOpen = handler;
    },
    loadURL(url) {
      log.push(`wc.loadURL:${url}`);
      return opts.load ? opts.load() : Promise.resolve();
    },
  };
  const emit = (event: string, ...args: unknown[]): void => {
    for (const l of wcListeners.get(event) ?? []) l(...args);
  };
  return {
    log,
    wc,
    session,
    emit,
    windowOpen: (url: string) => windowOpen?.({ url }),
    wcListeners,
  };
}

interface FakeSession {
  ses: WiredSession;
  log: string[];
  listeners: Map<string, Handler[]>;
  handlers: Map<string, Handler>;
}

function fakeSession(log: string[] = []): FakeSession {
  const listeners = new Map<string, Handler[]>();
  const handlers = new Map<string, Handler>();
  const set = (name: string) => (handler: Handler) => {
    log.push(`ses.${name}`);
    handlers.set(name, handler);
  };
  const ses: WiredSession = {
    on(event, listener) {
      log.push(`ses.on:${event}`);
      listeners.set(event, [...(listeners.get(event) ?? []), listener]);
      return ses;
    },
    setPermissionRequestHandler: set('setPermissionRequestHandler'),
    setPermissionCheckHandler: set('setPermissionCheckHandler'),
    setDevicePermissionHandler: set('setDevicePermissionHandler'),
    setBluetoothPairingHandler: set('setBluetoothPairingHandler'),
  };
  return { ses, log, listeners, handlers };
}

function recorder() {
  const opened: string[] = [];
  let failures = 0;
  return {
    opened,
    failures: () => failures,
    host: { openExternal: (url: string) => opened.push(url) },
    onFailed: () => {
      failures += 1;
    },
  };
}

function navEvent(url: string, isMainFrame = true) {
  const e = { url, isMainFrame, prevented: false, preventDefault: () => (e.prevented = true) };
  return e;
}

function pickerEvent() {
  const e = { prevented: false, preventDefault: () => (e.prevented = true) };
  return e;
}

const EVERY_GUARD = [
  'ses.setPermissionRequestHandler',
  'ses.setPermissionCheckHandler',
  'ses.setDevicePermissionHandler',
  'ses.setBluetoothPairingHandler',
  'ses.on:select-hid-device',
  'ses.on:select-serial-port',
  'ses.on:select-usb-device',
  'ses.on:will-download',
  'wc.on:will-navigate',
  'wc.on:will-redirect',
  'wc.on:will-frame-navigate',
  'wc.setWindowOpenHandler',
  'wc.on:select-bluetooth-device',
  'wc.on:select-client-certificate',
];

describe('wireSessionsView', () => {
  // test_wiring_registers_every_guard_before_loading
  it('registers every guard and handler before it loads anything, then loads the origin once', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);
    const load = f.log.indexOf(`wc.loadURL:${ORIGIN}/`);
    expect(load).toBe(f.log.length - 1);
    expect(f.log.filter((l) => l.startsWith('wc.loadURL'))).toEqual([`wc.loadURL:${ORIGIN}/`]);
    for (const guard of EVERY_GUARD) {
      const at = f.log.indexOf(guard);
      expect(at, guard).toBeGreaterThanOrEqual(0);
      expect(at, guard).toBeLessThan(load);
    }
    for (const failure of ['wc.on:did-fail-load', 'wc.on:render-process-gone', 'wc.on:destroyed']) {
      expect(f.log.indexOf(failure), failure).toBeLessThan(load);
    }
  });

  // test_wiring_navigation_handlers
  it('locks navigation, redirects and subframes to the origin through the registered listeners', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);

    const same = navEvent(`${ORIGIN}/session/1`);
    f.emit('will-navigate', same);
    expect(same.prevented).toBe(false);

    const away = navEvent('https://github.com/x');
    f.emit('will-navigate', away);
    expect(away.prevented).toBe(true);

    const file = navEvent('file:///C:/x');
    f.emit('will-navigate', file);
    expect(file.prevented).toBe(true);

    const redirect = navEvent('http://127.0.0.1:9000/');
    f.emit('will-redirect', redirect);
    expect(redirect.prevented).toBe(true);

    const frame = navEvent('https://evil.example/', false);
    f.emit('will-frame-navigate', frame);
    expect(frame.prevented).toBe(true);

    expect(r.opened).toEqual(['https://github.com/x', 'http://127.0.0.1:9000/']);
  });

  it('denies every new window, opening only other http(s) sites in the browser', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);
    expect(f.windowOpen('https://claude.ai/oauth')).toEqual({ action: 'deny' });
    expect(f.windowOpen(`${ORIGIN}/other`)).toEqual({ action: 'deny' });
    expect(f.windowOpen('file:///C:/x')).toEqual({ action: 'deny' });
    expect(f.windowOpen('ms-settings:privacy')).toEqual({ action: 'deny' });
    expect(r.opened).toEqual(['https://claude.ai/oauth']);
  });

  // test_wiring_denies_device_pickers
  it('cancels every device picker instead of letting Electron pick a device', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);

    // Bluetooth: with no handled listener Electron picks the first device found.
    const bt = pickerEvent();
    const btCalls: unknown[][] = [];
    f.emit('select-bluetooth-device', bt, [{ deviceId: 'a', deviceName: 'Headset' }], (...a: unknown[]) => btCalls.push(a));
    expect(bt.prevented).toBe(true);
    expect(btCalls).toEqual([['']]);

    const serial = pickerEvent();
    const serialCalls: unknown[][] = [];
    for (const l of f.session.listeners.get('select-serial-port') ?? []) {
      l(serial, [{ portId: 'COM3' }], f.wc, (...a: unknown[]) => serialCalls.push(a));
    }
    expect(serial.prevented).toBe(true);
    expect(serialCalls).toEqual([['']]);

    // HID and USB document "no arguments" as the cancel.
    for (const event of ['select-hid-device', 'select-usb-device']) {
      const e = pickerEvent();
      const calls: unknown[][] = [];
      for (const l of f.session.listeners.get(event) ?? []) {
        l(e, { deviceList: [{ deviceId: 'd' }], frame: null }, (...a: unknown[]) => calls.push(a));
      }
      expect(e.prevented, event).toBe(true);
      expect(calls, event).toEqual([[]]);
    }

    // A client certificate: the page's https subresources can reach any host,
    // and Electron would otherwise present the first certificate in the store.
    const cert = pickerEvent();
    const certCalls: unknown[][] = [];
    f.emit('select-client-certificate', cert, 'https://mtls.example/', [{ subjectName: 'me' }], (...a: unknown[]) =>
      certCalls.push(a),
    );
    expect(cert.prevented).toBe(true);
    expect(certCalls).toEqual([[]]);

    const pairing: unknown[] = [];
    f.session.handlers.get('setBluetoothPairingHandler')?.({ deviceId: 'a', pairingKind: 'confirm' }, (resp: unknown) =>
      pairing.push(resp),
    );
    expect(pairing).toEqual([{ confirmed: false }]);
  });

  // test_wiring_cancels_downloads
  it('cancels every download the page starts', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);
    const listeners = f.session.listeners.get('will-download') ?? [];
    expect(listeners).toHaveLength(1);
    const e = pickerEvent();
    for (const l of listeners) l(e, { getFilename: () => 'x.exe' }, f.wc);
    expect(e.prevented).toBe(true);
  });

  // test_wiring_denies_permissions
  it('denies every permission request and check, and every device permission', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);
    const granted: boolean[] = [];
    for (const permission of ['media', 'notifications', 'openExternal', 'clipboard-sanitized-write', 'geolocation']) {
      f.session.handlers.get('setPermissionRequestHandler')?.(f.wc, permission, (g: boolean) => granted.push(g), {});
    }
    expect(granted).toEqual([false, false, false, false, false]);
    expect(f.session.handlers.get('setPermissionCheckHandler')?.(f.wc, 'hid', ORIGIN, {})).toBe(false);
    expect(f.session.handlers.get('setDevicePermissionHandler')?.({ deviceType: 'usb', origin: ORIGIN })).toBe(false);
  });

  // test_wiring_reports_failures
  it('reports a failed main-frame load or a lost renderer, but not an aborted load or a subframe', () => {
    const f = fakes();
    const r = recorder();
    wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed);
    f.emit('did-fail-load', {}, -3, 'ERR_ABORTED', `${ORIGIN}/`, true);
    f.emit('did-fail-load', {}, -102, 'ERR_CONNECTION_REFUSED', `${ORIGIN}/frame`, false);
    expect(r.failures()).toBe(0);
    f.emit('did-fail-load', {}, -102, 'ERR_CONNECTION_REFUSED', `${ORIGIN}/`, true);
    expect(r.failures()).toBe(1);
    f.emit('render-process-gone', {}, { reason: 'crashed' });
    expect(r.failures()).toBe(2);
    f.emit('destroyed');
    expect(r.failures()).toBe(3);
  });

  it('does not throw or leave a rejection when the load fails', async () => {
    const f = fakes({ load: () => Promise.reject(new Error('ERR_CONNECTION_REFUSED')) });
    const r = recorder();
    expect(() => wireSessionsView(f.wc, ORIGIN, r.host, r.onFailed)).not.toThrow();
    // An unhandled rejection would fail the run.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  // test_wiring_retries_a_failed_lockdown
  it('never loads on a session it failed to lock, and locks it fully on the next try', () => {
    const shared = fakeSession();
    const setPairing = shared.ses.setBluetoothPairingHandler;
    let failOnce = true;
    shared.ses.setBluetoothPairingHandler = (handler) => {
      if (failOnce) {
        failOnce = false;
        throw new Error('not supported');
      }
      setPairing(handler);
    };
    const r = recorder();
    const first = fakes({ session: shared });
    expect(() => wireSessionsView(first.wc, ORIGIN, r.host, r.onFailed)).toThrow('not supported');
    expect(first.log.some((l) => l.startsWith('wc.loadURL'))).toBe(false);

    const second = fakes({ session: shared });
    wireSessionsView(second.wc, ORIGIN, r.host, r.onFailed);
    expect(shared.handlers.has('setBluetoothPairingHandler')).toBe(true);
    for (const event of ['select-hid-device', 'select-serial-port', 'select-usb-device']) {
      expect(shared.listeners.get(event)?.length, event).toBe(1);
    }
  });

  // test_wiring_locks_a_session_once
  it('locks a shared session once, so a picker is never answered twice', () => {
    const shared = fakeSession();
    const r = recorder();
    wireSessionsView(fakes({ session: shared }).wc, ORIGIN, r.host, r.onFailed);
    wireSessionsView(fakes({ session: shared }).wc, 'http://127.0.0.1:9100', r.host, r.onFailed);
    for (const event of ['select-hid-device', 'select-serial-port', 'select-usb-device']) {
      expect(shared.listeners.get(event)?.length, event).toBe(1);
    }
  });
});
