import { session, WebContentsView, type BrowserWindow } from 'electron';
import {
  DASHBOARD_PARTITION,
  navigationGuards,
  sessionsViewBounds,
  sessionsViewController,
  sessionsWebPreferences,
  type SessionsHost,
  type SessionsViewPort,
} from './sessionsView.js';

/**
 * Binds the pure Sessions view (sessionsView.ts) to a real `WebContentsView`
 * in `win`. Kept apart from sessionsView.ts because importing Electron at
 * runtime fails outside the Electron binary, and the unit tests run without it.
 */

/** Chromium's ERR_ABORTED: a load superseded or cancelled (by us or by a newer load), not a failure. */
const ERR_ABORTED = -3;

/** Lock the dashboard's partition down. Idempotent; must run before anything loads in it. */
function lockDownSession(): void {
  const ses = session.fromPartition(DASHBOARD_PARTITION);
  // Camera, microphone, notifications, clipboard-read, external protocols (`openExternal`)...: all no.
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
}

export function attachSessionsView(
  win: BrowserWindow,
  host: SessionsHost,
): { show(dashboardUrl: string | undefined): void; hide(): void } {
  lockDownSession();

  let view: WebContentsView | undefined;
  let onScreen = false;

  const layout = (): void => {
    if (view === undefined || win.isDestroyed()) return;
    view.setBounds(sessionsViewBounds(win.getContentBounds()));
  };

  const close = (v: WebContentsView): void => {
    if (!v.webContents.isDestroyed()) v.webContents.close();
  };

  const port: SessionsViewPort = {
    open(origin) {
      const old = view;
      const v = new WebContentsView({ webPreferences: sessionsWebPreferences() });
      const wc = v.webContents;
      const guards = navigationGuards(origin, host);
      wc.on('will-navigate', (e) => guards.willNavigate(e));
      wc.on('will-redirect', (e) => guards.willRedirect(e));
      wc.on('will-frame-navigate', (e) => guards.willFrameNavigate(e));
      wc.setWindowOpenHandler((details) => guards.windowOpen(details));

      // A failure is left for the next poll to retry: it re-checks the
      // dashboard is up before anything loads again. Events from a view
      // already replaced are ignored.
      const failed = (): void => {
        if (view === v) controller.loadFailed();
      };
      wc.on('did-fail-load', (_e, errorCode, _description, _url, isMainFrame) => {
        if (isMainFrame && errorCode !== ERR_ABORTED) failed();
      });
      wc.on('render-process-gone', failed);
      wc.on('destroyed', failed);

      view = v;
      if (old !== undefined) close(old);
      // Only crr's loopback origin is ever loaded, never the raw reported URL.
      wc.loadURL(`${origin}/`).catch(() => {
        // Reported through did-fail-load; nothing to throw here.
      });
    },
    attach() {
      if (view === undefined || win.isDestroyed()) return;
      win.contentView.addChildView(view);
      onScreen = true;
      layout();
    },
    detach() {
      onScreen = false;
      if (view === undefined || win.isDestroyed()) return;
      // Reached from webContents events too (a renderer gone, teardown at
      // quit): a throw here would surface as an uncaught main-process error.
      try {
        win.contentView.removeChildView(view);
      } catch (e) {
        console.warn(`could not take the dashboard view off screen: ${String(e)}`);
      }
    },
  };

  const controller = sessionsViewController(port);

  const relayout = (): void => {
    if (onScreen) layout();
  };
  win.on('resize', relayout);
  win.on('maximize', relayout);
  win.on('unmaximize', relayout);
  win.on('restore', relayout);

  win.on('closed', () => {
    const v = view;
    view = undefined;
    onScreen = false;
    if (v !== undefined) close(v);
  });

  return { show: controller.show, hide: controller.hide };
}
