import { navigationGuards, type NavigationEvent, type SessionsHost } from './sessionsView.js';

/**
 * Every guard on the dashboard view's WebContents and Session, registered
 * before anything loads. It takes the parts of Electron's objects it uses as
 * plain structural types (both extend Node's EventEmitter), so the real
 * objects and test fakes both fit, and a deleted registration fails a test.
 */

// Node's EventEmitter listener type; each listener below declares its own arguments.
type Listener = (...args: any[]) => void;

interface CancellableEvent {
  preventDefault(): void;
}

/** The parts of Electron's `Session` the lock-down uses. */
export interface WiredSession {
  on(event: string, listener: Listener): unknown;
  setPermissionRequestHandler(
    handler: (webContents: unknown, permission: string, callback: (granted: boolean) => void) => void,
  ): void;
  setPermissionCheckHandler(handler: () => boolean): void;
  setDevicePermissionHandler(handler: () => boolean): void;
  setBluetoothPairingHandler(
    handler: (details: unknown, callback: (response: { confirmed: boolean }) => void) => void,
  ): void;
}

/** The parts of Electron's `WebContents` the wiring uses. */
export interface WiredContents {
  readonly session: WiredSession;
  on(event: string, listener: Listener): unknown;
  setWindowOpenHandler(handler: (details: { url: string }) => { action: 'deny' }): void;
  loadURL(url: string): Promise<unknown>;
}

/** Chromium's ERR_ABORTED: a load superseded or cancelled (by a guard or a newer load), not a failure. */
export const ERR_ABORTED = -3;

const locked = new WeakSet<object>();

/**
 * Deny everything the page could ask of the machine. Idempotent per session:
 * the select-* events are listeners, and a second one would answer a picker
 * twice. The session counts as locked only once every handler is in place, so
 * a registration that throws is redone in full next time (and, since the throw
 * reaches wireSessionsView, nothing loads meanwhile).
 */
function lockDownSession(ses: WiredSession): void {
  if (locked.has(ses)) return;
  // Camera, microphone, notifications, clipboard-read, external protocols (`openExternal`)...: all no.
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
  ses.setBluetoothPairingHandler((_details, callback) => callback({ confirmed: false }));
  // Device pickers: cancel, never let Electron choose. HID and USB document
  // "no arguments" as the cancel; serial documents the empty string.
  ses.on('select-hid-device', (e: CancellableEvent, _details: unknown, callback: () => void) => {
    e.preventDefault();
    callback();
  });
  ses.on('select-usb-device', (e: CancellableEvent, _details: unknown, callback: () => void) => {
    e.preventDefault();
    callback();
  });
  ses.on(
    'select-serial-port',
    (e: CancellableEvent, _ports: unknown, _wc: unknown, callback: (portId: string) => void) => {
      e.preventDefault();
      callback('');
    },
  );
  // A download would put a file of the page's choosing on disk. The dashboard
  // offers none, so every one is cancelled.
  ses.on('will-download', (e: CancellableEvent) => e.preventDefault());
  locked.add(ses);
}

/**
 * Lock `wc` (and its session) down, then load `${origin}/`. `onFailed` hears
 * of a failed main-frame load, a lost renderer, or the contents being destroyed.
 */
export function wireSessionsView(
  wc: WiredContents,
  origin: string,
  host: SessionsHost,
  onFailed: () => void,
): void {
  // The session the view really uses, not one looked up by partition name.
  lockDownSession(wc.session);

  const guards = navigationGuards(origin, host);
  wc.on('will-navigate', (e: NavigationEvent) => guards.willNavigate(e));
  wc.on('will-redirect', (e: NavigationEvent) => guards.willRedirect(e));
  wc.on('will-frame-navigate', (e: NavigationEvent) => guards.willFrameNavigate(e));
  wc.setWindowOpenHandler((details) => guards.windowOpen(details));
  // With no listener that prevents the default, Electron picks the first device found.
  wc.on('select-bluetooth-device', (e: CancellableEvent, _devices: unknown, callback: (id: string) => void) => {
    e.preventDefault();
    callback('');
  });
  // The page's https subresources (fetch, img, script) can reach any host, and
  // without preventDefault Electron presents the first certificate in the
  // store. The callback's certificate is optional: none is presented.
  wc.on(
    'select-client-certificate',
    (e: CancellableEvent, _url: string, _certificates: unknown, callback: () => void) => {
      e.preventDefault();
      callback();
    },
  );

  wc.on(
    'did-fail-load',
    (_e: unknown, errorCode: number, _description: string, _url: string, isMainFrame: boolean) => {
      if (isMainFrame && errorCode !== ERR_ABORTED) onFailed();
    },
  );
  wc.on('render-process-gone', () => onFailed());
  wc.on('destroyed', () => onFailed());

  // Only crr's loopback origin is ever loaded, never the raw reported URL.
  wc.loadURL(`${origin}/`).catch(() => {
    // Reported through did-fail-load; nothing to throw here.
  });
}
