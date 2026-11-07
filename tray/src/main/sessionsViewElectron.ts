import { WebContentsView, type BrowserWindow } from 'electron';
import {
  sessionsViewBounds,
  sessionsViewController,
  sessionsWebPreferences,
  type SessionsHost,
  type SessionsViewPort,
} from './sessionsView.js';
import { wireSessionsView } from './sessionsWiring.js';

/**
 * Binds the pure Sessions view (sessionsView.ts) to a real `WebContentsView`
 * in `win`. Kept apart from sessionsView.ts because importing Electron at
 * runtime fails outside the Electron binary, and the unit tests run without it.
 * Every guard on the view's contents and session lives in sessionsWiring.ts,
 * where it is tested.
 */

export interface SessionsViewHost extends SessionsHost {
  /** The view went on or off screen (including on its own, after a failed load). */
  onScreenChange(onScreen: boolean): void;
}

export function attachSessionsView(
  win: BrowserWindow,
  host: SessionsViewHost,
): { show(dashboardUrl: string | undefined): void; hide(): void } {
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
      view = v;
      if (old !== undefined) close(old);
      // A failure is left for the next poll to retry: it re-checks the
      // dashboard is up before anything loads again. Events from a view
      // already replaced are ignored.
      wireSessionsView(v.webContents, origin, host, () => {
        if (view === v) controller.loadFailed();
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

  const controller = sessionsViewController(port, (shown) => host.onScreenChange(shown));

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
