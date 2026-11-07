import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { ServiceStatus } from './contracts.js';
import type { PollState } from './poll.js';
import {
  DASHBOARD_PARTITION,
  TAB_BAR_HEIGHT,
  dashboardOrigin,
  navigationDecision,
  navigationGuards,
  sessionsViewBounds,
  sessionsViewController,
  sessionsViewUrl,
  sessionsWebPreferences,
  type NavigationEvent,
  type SessionsViewPort,
} from './sessionsView.js';

const ORIGIN = 'http://127.0.0.1:8377';

describe('navigationDecision', () => {
  // test_navigation_decision
  it.each([
    // The dashboard's own origin stays in the view.
    ['http://127.0.0.1:8377/x?y', 'allow'],
    ['http://127.0.0.1:8377/', 'allow'],
    ['HTTP://127.0.0.1:8377/login', 'allow'],
    // Any other http(s) origin opens in the default browser.
    ['http://127.0.0.1:9000/', 'external'],
    ['https://github.com/', 'external'],
    ['http://localhost:8377/', 'external'],
    ['https://127.0.0.1:8377/', 'external'],
    ['http://127.0.0.1:8377@evil.example/', 'external'],
    // Every other scheme is refused outright, never handed to the OS.
    ['javascript:alert(1)', 'deny'],
    ['file:///C:/x', 'deny'],
    ['data:text/html,x', 'deny'],
    ['devtools://x', 'deny'],
    ['chrome://settings', 'deny'],
    ['about:blank', 'deny'],
    ['view-source:http://127.0.0.1:8377/', 'deny'],
    ['mailto:someone@example.com', 'deny'],
    ['ms-settings:privacy', 'deny'],
    // A blob URL carries the dashboard's origin inside it; the scheme still loses.
    ['blob:http://127.0.0.1:8377/0b6b3c1e-0000-4000-8000-000000000000', 'deny'],
    // Unparsable.
    ['not a url', 'deny'],
    ['', 'deny'],
  ] as const)('%s → %s', (target, expected) => {
    expect(navigationDecision(target, ORIGIN)).toBe(expected);
  });

  it('never allows an opaque origin, even when asked to match "null"', () => {
    expect(navigationDecision('data:text/html,x', 'null')).toBe('deny');
    expect(navigationDecision('about:blank', 'null')).toBe('deny');
  });
});

describe('dashboardOrigin', () => {
  // test_dashboard_origin_is_loopback_only
  it('gives the origin of crr\'s loopback URL', () => {
    expect(dashboardOrigin('http://127.0.0.1:8377/')).toBe(ORIGIN);
    expect(dashboardOrigin('http://127.0.0.1:9100/some/path?x#y')).toBe('http://127.0.0.1:9100');
  });

  it.each([
    'http://example.com:8377/',
    'https://127.0.0.1:8377/',
    'http://localhost:8377/',
    'http://[::1]:8377/',
    'http://127.0.0.2:8377/',
    'http://127.0.0.1.evil.example:8377/',
    'http://user:pw@127.0.0.1:8377/',
    // crr always names the port; without one the URL is not crr's.
    'http://127.0.0.1/',
    'file:///C:/x',
    'javascript:alert(1)',
    'not a url',
    '',
  ])('refuses %s', (url) => {
    expect(dashboardOrigin(url)).toBeUndefined();
  });
});

describe('sessionsWebPreferences', () => {
  // test_sessions_view_has_no_preload
  it('has no preload, and a persistent partition of its own', () => {
    const prefs = sessionsWebPreferences();
    expect('preload' in prefs).toBe(false);
    expect(prefs.partition).toBe(DASHBOARD_PARTITION);
    expect(DASHBOARD_PARTITION.startsWith('persist:')).toBe(true);
  });

  it('is sandboxed and isolated with no Node and no DevTools, with exactly those keys', () => {
    expect(sessionsWebPreferences()).toEqual({
      partition: 'persist:crr-dashboard',
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      devTools: false,
    });
  });
});

describe('sessionsViewBounds', () => {
  // test_sessions_view_sits_below_the_tab_bar
  it('fills the window below the tab bar', () => {
    expect(sessionsViewBounds({ width: 760, height: 640 })).toEqual({
      x: 0,
      y: TAB_BAR_HEIGHT,
      width: 760,
      height: 640 - TAB_BAR_HEIGHT,
    });
  });

  it('never has a negative height', () => {
    expect(sessionsViewBounds({ width: 300, height: 10 }).height).toBe(0);
  });

  it('matches the renderer\'s tab bar height', () => {
    const css = readFileSync(fileURLToPath(new URL('../renderer/styles.css', import.meta.url)), 'utf8');
    expect(TAB_BAR_HEIGHT).toBe(40);
    expect(css).toContain(`--tab-bar-height: ${TAB_BAR_HEIGHT}px;`);
  });
});

function service(dashboard: boolean, url = 'http://127.0.0.1:8377/'): ServiceStatus {
  return {
    contract: 1,
    backend: 'systemd',
    reason: '',
    state: 'running',
    services: [
      { name: 'watchdog', unit: 'w', installed: true, enabled: true, active: true },
      { name: 'dashboard', unit: 'd', installed: true, enabled: true, active: dashboard },
    ],
    dashboard_url: url,
    dashboard_login: true,
  };
}

const RUNNING: PollState = { icon: 'running', statusLine: 'CRR running', service: service(true), wsl: 'running' };
const SHOWN = { tab: 'sessions', state: RUNNING, suspended: false, windowShown: true } as const;

describe('sessionsViewUrl', () => {
  // test_sessions_view_shows_only_while_the_dashboard_is_up
  it('is crr\'s dashboard URL on the Sessions tab, with a fresh poll saying the dashboard is up', () => {
    expect(sessionsViewUrl(SHOWN)).toBe('http://127.0.0.1:8377/');
  });

  it('is nothing on the Control tab or with the window put away', () => {
    expect(sessionsViewUrl({ ...SHOWN, tab: 'control' })).toBeUndefined();
    expect(sessionsViewUrl({ ...SHOWN, windowShown: false })).toBeUndefined();
  });

  it('is nothing while asleep or just waking, or before the first poll', () => {
    expect(sessionsViewUrl({ ...SHOWN, suspended: true })).toBeUndefined();
    expect(sessionsViewUrl({ ...SHOWN, state: undefined })).toBeUndefined();
  });

  it('is nothing while the poller says WSL is down or held, whatever service it carries', () => {
    for (const wsl of ['missing', 'stopped', 'held'] as const) {
      expect(sessionsViewUrl({ ...SHOWN, state: { ...RUNNING, wsl } })).toBeUndefined();
    }
  });

  it('is nothing when the dashboard unit is off or unknown', () => {
    expect(sessionsViewUrl({ ...SHOWN, state: { ...RUNNING, service: service(false) } })).toBeUndefined();
    expect(sessionsViewUrl({ ...SHOWN, state: { ...RUNNING, service: undefined } })).toBeUndefined();
  });

  it('is nothing when crr reports a URL that is not its loopback dashboard', () => {
    const odd = { ...RUNNING, service: service(true, 'http://example.com:8377/') };
    expect(sessionsViewUrl({ ...SHOWN, state: odd })).toBeUndefined();
  });
});

type Call = ['open', string] | ['attach'] | ['detach'];

function fakePort(): { port: SessionsViewPort; calls: Call[] } {
  const calls: Call[] = [];
  return {
    calls,
    port: {
      open: (origin) => calls.push(['open', origin]),
      attach: () => calls.push(['attach']),
      detach: () => calls.push(['detach']),
    },
  };
}

describe('sessionsViewController', () => {
  // test_sessions_view_loads_once_per_origin
  it('loads the dashboard once, not on every poll', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.show('http://127.0.0.1:8377/');
    view.show('http://127.0.0.1:8377/');
    expect(calls).toEqual([['open', ORIGIN], ['attach']]);
  });

  it('keeps the page across a hide, so a tab switch does not reload it', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.hide();
    view.hide();
    view.show('http://127.0.0.1:8377/');
    expect(calls).toEqual([['open', ORIGIN], ['attach'], ['detach'], ['attach']]);
  });

  it('opens a fresh view when the port changes', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.show('http://127.0.0.1:9100/');
    expect(calls).toEqual([
      ['open', ORIGIN],
      ['attach'],
      ['detach'],
      ['open', 'http://127.0.0.1:9100'],
      ['attach'],
    ]);
  });

  it('takes a failed page off screen, and opens afresh on the next show, only once', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.loadFailed();
    expect(calls).toEqual([['open', ORIGIN], ['attach'], ['detach']]);
    view.show('http://127.0.0.1:8377/');
    view.show('http://127.0.0.1:8377/');
    expect(calls).toEqual([['open', ORIGIN], ['attach'], ['detach'], ['open', ORIGIN], ['attach']]);
  });

  it('remembers a failure that happened off screen', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.hide();
    view.loadFailed();
    view.show('http://127.0.0.1:8377/');
    expect(calls).toEqual([['open', ORIGIN], ['attach'], ['detach'], ['open', ORIGIN], ['attach']]);
  });

  it('hides for no URL, and never opens a URL that is not crr\'s loopback dashboard', () => {
    const { port, calls } = fakePort();
    const view = sessionsViewController(port);
    view.show('http://127.0.0.1:8377/');
    view.show(undefined);
    view.show('http://example.com:8377/');
    view.show('file:///C:/x');
    expect(calls).toEqual([['open', ORIGIN], ['attach'], ['detach']]);
  });
});

function navEvent(url: string, isMainFrame = true): NavigationEvent & { prevented: boolean } {
  const e = {
    url,
    isMainFrame,
    prevented: false,
    preventDefault: () => {
      e.prevented = true;
    },
  };
  return e;
}

function recordingHost(): { host: { openExternal(url: string): void }; opened: string[] } {
  const opened: string[] = [];
  return { opened, host: { openExternal: (url) => opened.push(url) } };
}

describe('navigationGuards', () => {
  // test_navigation_guards
  it('lets same-origin navigation through untouched', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    for (const guard of [guards.willNavigate, guards.willRedirect, guards.willFrameNavigate]) {
      const e = navEvent('http://127.0.0.1:8377/session/1', false);
      guard(e);
      expect(e.prevented).toBe(false);
    }
    const main = navEvent('http://127.0.0.1:8377/login');
    guards.willNavigate(main);
    expect(main.prevented).toBe(false);
    expect(opened).toEqual([]);
  });

  it('stops a main-frame navigation or redirect to another site and opens it in the browser', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    const nav = navEvent('https://github.com');
    guards.willNavigate(nav);
    const redirect = navEvent('http://127.0.0.1:9000/x');
    guards.willRedirect(redirect);
    expect(nav.prevented).toBe(true);
    expect(redirect.prevented).toBe(true);
    expect(opened).toEqual(['https://github.com/', 'http://127.0.0.1:9000/x']);
  });

  it('stops everything else without handing it to the OS', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    for (const url of ['file:///C:/x', 'javascript:alert(1)', 'ms-settings:privacy', 'not a url']) {
      for (const guard of [guards.willNavigate, guards.willRedirect]) {
        const e = navEvent(url);
        guard(e);
        expect(e.prevented).toBe(true);
      }
    }
    expect(opened).toEqual([]);
  });

  it('keeps subframes on the dashboard origin, but never opens the browser for one', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    const frame = navEvent('https://github.com/', false);
    guards.willFrameNavigate(frame);
    const redirect = navEvent('https://github.com/', false);
    guards.willRedirect(redirect);
    expect(frame.prevented).toBe(true);
    expect(redirect.prevented).toBe(true);
    expect(opened).toEqual([]);
  });

  it('leaves main-frame navigation to will-navigate, so a link is opened once', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    const e = navEvent('https://github.com/');
    guards.willFrameNavigate(e);
    expect(e.prevented).toBe(false);
    expect(opened).toEqual([]);
  });

  it('denies every new window, opening only other http(s) sites in the browser', () => {
    const { host, opened } = recordingHost();
    const guards = navigationGuards(ORIGIN, host);
    expect(guards.windowOpen({ url: 'https://claude.ai/oauth?x=1' })).toEqual({ action: 'deny' });
    expect(guards.windowOpen({ url: 'http://127.0.0.1:8377/other' })).toEqual({ action: 'deny' });
    expect(guards.windowOpen({ url: 'file:///C:/x' })).toEqual({ action: 'deny' });
    expect(guards.windowOpen({ url: 'javascript:alert(1)' })).toEqual({ action: 'deny' });
    expect(opened).toEqual(['https://claude.ai/oauth?x=1']);
  });
});
