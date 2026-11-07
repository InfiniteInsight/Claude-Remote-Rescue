import type { Rectangle, WebPreferences } from 'electron';
import { dashboardActive } from './appGates.js';
import type { Tab } from './ipc.js';
import type { PollState } from './poll.js';

/**
 * The Sessions tab: crr's own dashboard in a `WebContentsView`, so every
 * session control behaves exactly as it does on the web.
 *
 * The page is crr's, but it is still web content, so it gets none of the
 * app's powers: no preload, sandboxed, isolated, no Node, no DevTools, every
 * permission denied, and navigation locked to the dashboard's own origin.
 * Other http(s) links go to the default browser; any other scheme is refused.
 *
 * Everything here is pure (Electron types only) so it is tested without
 * Electron; `sessionsViewElectron.ts` binds it to a real view.
 */

/** The dashboard's own session. `persist:` keeps its login cookie across restarts; the app never reads it. */
export const DASHBOARD_PARTITION = 'persist:crr-dashboard';

/** Height of the renderer's tab bar; must equal `--tab-bar-height` in styles.css (a test checks). */
export const TAB_BAR_HEIGHT = 40;

/** The dashboard view's renderer settings, exactly. There is deliberately no `preload`. */
export function sessionsWebPreferences(): WebPreferences {
  return {
    partition: DASHBOARD_PARTITION,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    // DevTools is on by default, and the default menu's Ctrl+Shift+I would open it on this page.
    devTools: false,
  };
}

function parse(url: string): URL | undefined {
  try {
    return new URL(url);
  } catch {
    return undefined;
  }
}

/**
 * The origin to load for crr's reported `dashboard_url`, or `undefined` for
 * anything that is not exactly what crr produces: `http://127.0.0.1:<port>/`.
 * A tampered or odd URL is never loaded.
 */
export function dashboardOrigin(dashboardUrl: string): string | undefined {
  const url = parse(dashboardUrl);
  if (url === undefined) return undefined;
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') return undefined;
  // crr always names the port (and `URL` drops a default :80, which crr cannot bind anyway).
  if (url.port === '') return undefined;
  if (url.username !== '' || url.password !== '') return undefined;
  return url.origin;
}

export type NavigationDecision = 'allow' | 'external' | 'deny';

/**
 * What to do with a navigation to `target` from a page on `origin`. The scheme
 * is checked first: only http(s) can ever be allowed or handed to the browser,
 * so a `blob:` URL (whose origin is the dashboard's) or an opaque origin
 * (`"null"`) can never match.
 */
export function navigationDecision(target: string, origin: string): NavigationDecision {
  const url = parse(target);
  if (url === undefined) return 'deny';
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return 'deny';
  return url.origin === origin ? 'allow' : 'external';
}

/** Where the view sits in the window's content area: everything below the tab bar. */
export function sessionsViewBounds(content: { width: number; height: number }): Rectangle {
  return {
    x: 0,
    y: TAB_BAR_HEIGHT,
    width: content.width,
    height: Math.max(0, content.height - TAB_BAR_HEIGHT),
  };
}

export interface SessionsGateInput {
  tab: Tab;
  /** The latest fresh poll (undefined before the first, and from suspend until a fresh one lands). */
  state: PollState | undefined;
  suspended: boolean;
  /** The window is visible and not minimized. */
  windowShown: boolean;
}

/**
 * The dashboard URL the view should show, or `undefined` to take it off
 * screen. Loading `http://127.0.0.1:<port>/` never runs `wsl.exe`, but the view
 * still shows only what the latest fresh poll vouches for: the distro running,
 * not held, the dashboard unit up, and a URL that is crr's loopback dashboard.
 */
export function sessionsViewUrl(input: SessionsGateInput): string | undefined {
  const { tab, state, suspended, windowShown } = input;
  if (tab !== 'sessions' || !windowShown || suspended) return undefined;
  if (state === undefined || state.wsl !== 'running' || !dashboardActive(state)) return undefined;
  const url = state.service?.dashboard_url;
  if (url === undefined || dashboardOrigin(url) === undefined) return undefined;
  return url;
}

/** What the renderer's Sessions panel shows under (or instead of) the dashboard view. */
export type SessionsPanel =
  | { kind: 'stopped' }
  | { kind: 'loading'; text: string }
  | { kind: 'refused'; text: string }
  | { kind: 'showing' };

/**
 * The Sessions panel for a poll state. `viewOnScreen` is whether the dashboard
 * view is currently drawn over the panel. A dashboard address the app refuses
 * is said plainly, so the tab never claims to be loading something it never
 * will. The URL is crr's own string, for the renderer to show as text only.
 */
export function sessionsPanel(state: PollState, viewOnScreen: boolean): SessionsPanel {
  if (state.wsl !== 'running' || !dashboardActive(state)) return { kind: 'stopped' };
  const url = state.service?.dashboard_url ?? '';
  if (dashboardOrigin(url) === undefined) {
    return { kind: 'refused', text: `crr reported a dashboard address the tray won't load: ${url}` };
  }
  return viewOnScreen ? { kind: 'showing' } : { kind: 'loading', text: 'Loading the dashboard…' };
}

/** The Electron side of the view, as the controller drives it. */
export interface SessionsViewPort {
  /** Replace any current view with a fresh one bound to `origin`, and load `${origin}/` in it. */
  open(origin: string): void;
  /** Put the current view on screen below the tab bar. */
  attach(): void;
  /** Take the current view off screen. Its page stays loaded. */
  detach(): void;
}

export interface SessionsView {
  /** Show the dashboard at `dashboardUrl`, or take the view off screen when it is undefined or not crr's. */
  show(dashboardUrl: string | undefined): void;
  /** Take the view off screen, keeping the page (a tab switch must not reload it). */
  hide(): void;
  /** The page failed to load or its renderer went away: off screen now, and the next `show()` opens a fresh view. */
  loadFailed(): void;
}

/**
 * When to (re)load the dashboard. A poll lands every few seconds, so `show()`
 * is called repeatedly with the same URL; it loads only when there is nothing
 * loaded yet, the origin changed (a port change), or the last load failed. A
 * fresh view per load means the back/forward history never holds another
 * origin, so `history.back()` cannot reach an old port something else now owns.
 */
export function sessionsViewController(
  port: SessionsViewPort,
  /** Told each time the view goes on or off screen, so the panel under it can follow. */
  onScreenChange: (onScreen: boolean) => void = () => {},
): SessionsView {
  let origin: string | undefined;
  let attached = false;
  let stale = false;

  const hide = (): void => {
    if (!attached) return;
    port.detach();
    attached = false;
    onScreenChange(false);
  };

  return {
    show(dashboardUrl) {
      const wanted = dashboardUrl === undefined ? undefined : dashboardOrigin(dashboardUrl);
      if (wanted === undefined) {
        hide();
        return;
      }
      if (wanted !== origin || stale) {
        hide();
        port.open(wanted);
        origin = wanted;
        stale = false;
      }
      if (!attached) {
        port.attach();
        attached = true;
        onScreenChange(true);
      }
    },
    hide,
    loadFailed() {
      // Off screen, so the renderer's panel shows instead of a blank page; the
      // next poll decides whether the dashboard is up to try again.
      stale = true;
      hide();
    },
  };
}

export interface SessionsHost {
  /** Open an http(s) URL in the default browser. */
  openExternal(url: string): void;
}

/** The parts of Electron's navigation events the guards read. */
export interface NavigationEvent {
  url: string;
  isMainFrame: boolean;
  preventDefault(): void;
}

/**
 * The navigation lock for one view bound to `origin`. Anything not allowed is
 * prevented. A main-frame navigation to another http(s) site opens in the
 * browser instead; a subframe's never does (nothing the user clicked). New
 * windows are always denied, with other sites sent to the browser.
 */
export function navigationGuards(
  origin: string,
  host: SessionsHost,
): {
  willNavigate(e: NavigationEvent): void;
  willRedirect(e: NavigationEvent): void;
  willFrameNavigate(e: NavigationEvent): void;
  windowOpen(details: { url: string }): { action: 'deny' };
} {
  const openExternal = (target: string): void => {
    // 'external' means it parsed as http(s); hand the browser the canonical form.
    host.openExternal(new URL(target).href);
  };
  const guard = (e: NavigationEvent): void => {
    const decision = navigationDecision(e.url, origin);
    if (decision === 'allow') return;
    e.preventDefault();
    if (decision === 'external' && e.isMainFrame) openExternal(e.url);
  };
  return {
    willNavigate: guard,
    willRedirect: guard,
    // `will-navigate` already covers the main frame; this keeps subframes in line too.
    willFrameNavigate: (e) => {
      if (!e.isMainFrame) guard(e);
    },
    windowOpen: ({ url }) => {
      if (navigationDecision(url, origin) === 'external') openExternal(url);
      return { action: 'deny' };
    },
  };
}
