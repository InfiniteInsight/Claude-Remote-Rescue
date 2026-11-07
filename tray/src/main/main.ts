/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import {
  app,
  BrowserWindow,
  Menu,
  nativeImage,
  powerMonitor,
  shell,
  type NativeImage,
} from 'electron';
import QRCode from 'qrcode';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { registerIpc, type IpcActions, type Tab } from './ipc.js';
import {
  readAppConfig,
  setConfigDir,
  writeAppConfig,
} from './appConfig.js';
import {
  LOGIN_ITEM_ARGS,
  parseLaunchFlags,
  shouldHideInsteadOfClose,
  shouldQuitOnWindowClosed,
  TRAY_LABELS,
} from './appLifecycle.js';
import {
  dashboardActive,
  qrGate,
  qrStateAllows,
  shouldForgetCrrPath,
  SleepTracker,
} from './appGates.js';
import { controlView, type ControlView } from './controlView.js';
import type { QrInfo } from './contracts.js';
import { CrrClient, resolveCrrPath, type CrrResult } from './crrClient.js';
import { holds, machineBootSeconds, readHold } from './hold.js';
import { Poller, type PollState, type TrayIcon } from './poll.js';
import { summarizeError } from './summarizeError.js';
import { sessionsViewUrl } from './sessionsView.js';
import { attachSessionsView } from './sessionsViewElectron.js';
import { createTray, type TrayHandle } from './tray.js';
import { trayIconPng } from './trayIcon.js';
import { mainWindowWebPreferences } from './windowPrefs.js';
import {
  defaultDistro,
  listDistros,
  realRunInDistro,
  runningDistros,
} from './wsl.js';

export { mainWindowWebPreferences };

const __dirname = dirname(fileURLToPath(import.meta.url));

/** Must match `appId` in electron-builder.yml (Windows keys the taskbar group and toasts off it). */
const APP_USER_MODEL_ID = 'io.github.infiniteinsight.crr-tray';

/** While the Control tab is on screen, refresh the phone link this often. */
const QR_REFRESH_MS = 60_000;

const PLACEHOLDER: PollState = {
  icon: 'stopped',
  statusLine: 'Checking crr…',
  wsl: 'missing',
};

const launchFlags = parseLaunchFlags(process.argv.slice(1));

// The tray handle must stay referenced: it closes over the Tray, and an
// unreferenced one would be collected, taking the icon with it.
let tray: TrayHandle | undefined;
let mainWindow: BrowserWindow | undefined;
/** The Sessions tab's dashboard view in `mainWindow`. */
let sessionsView: ReturnType<typeof attachSessionsView> | undefined;
/** Whether that view is drawn over the Sessions panel right now. */
let sessionsOnScreen = false;
let isQuitting = false;

// ---- App state -------------------------------------------------------------

let distro: string | undefined;
let distros: string[] = [];
/** crr's launcher path inside `distro`; cleared whenever the distro changes. */
let cachedCrrPath: string | undefined;
/** Why the crr path could not be resolved, while that is true. */
let crrPathError: string | undefined;
let lastState: PollState | undefined;
let busy = false;
let lastAction: { ok: boolean; message: string } | undefined;
let qr: CrrResult<QrInfo> | undefined;
let qrImage: string | undefined;
let qrBusy = false;
let activeTab: Tab = 'control';
/**
 * Sleep bookkeeping, by epoch rather than clock (appGates.ts). While
 * `sleep.suspended`, nothing may act on what the app last knew about WSL: that
 * is pre-sleep state, and WSL may have shut down since.
 */
const sleep = new SleepTracker();

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function isHeld(): boolean {
  return holds(readHold(), Date.now() / 1000, machineBootSeconds());
}

/** `isHeld()`, reading a failure as a hold: when in doubt, do not touch WSL. */
function heldNow(): boolean {
  try {
    return isHeld();
  } catch {
    return true;
  }
}

// ---- crr client ------------------------------------------------------------

/** Drop the cached crr path (and its error), in memory and on disk, so the next gated lookup resolves it afresh. */
function forgetCrrPath(): void {
  cachedCrrPath = undefined;
  crrPathError = undefined;
  writeAppConfig({ ...readAppConfig(), crrPath: undefined });
}

/** A client that forgets the cached path when `service status` says the launcher is gone. */
function watchedClient(run: ReturnType<typeof realRunInDistro>, path: string): CrrClient {
  const client = new CrrClient(run, path);
  const serviceStatus = client.serviceStatus.bind(client);
  client.serviceStatus = async () => {
    const result = await serviceStatus();
    if (shouldForgetCrrPath(result) && cachedCrrPath === path) forgetCrrPath();
    return result;
  };
  return client;
}

let resolving: Promise<Awaited<ReturnType<typeof resolveCrrPath>>> | undefined;

/**
 * The only place the crr path is resolved. Resolving is a `wsl.exe -d` call, so
 * the poller reaches this only after its never-wake gates pass; explicit user
 * actions (Start, Stop, Repair) call it directly and may boot the distro.
 */
async function getClient(): Promise<CrrClient | { error: string }> {
  const d = distro;
  if (d === undefined) return { error: 'no WSL distro selected' };
  const run = realRunInDistro(d);
  if (cachedCrrPath !== undefined) {
    crrPathError = undefined;
    return watchedClient(run, cachedCrrPath);
  }
  resolving ??= resolveCrrPath(run).finally(() => {
    resolving = undefined;
  });
  const found = await resolving;
  if (distro !== d) return { error: 'the WSL distro changed while looking for crr' };
  if (!found.ok) {
    crrPathError = found.error;
    return { error: found.error };
  }
  crrPathError = undefined;
  cachedCrrPath = found.path;
  writeAppConfig({ ...readAppConfig(), crrPath: found.path });
  return watchedClient(run, found.path);
}

// ---- The poller, tray and view ---------------------------------------------

const poller = new Poller({
  runningDistros: () => {
    sleep.pollStarted();
    return runningDistros();
  },
  distro: () => distro,
  held: isHeld,
  client: getClient,
  onState,
  schedule: (fn, ms) => {
    const timer = setTimeout(fn, ms);
    return () => clearTimeout(timer);
  },
});

/** Poll, and if the poll joined was already under way before this call, poll once more. */
async function pollFresh(): Promise<void> {
  const mark = sleep.mark();
  await poller.pollNow();
  if (!sleep.polledSince(mark)) await poller.pollNow();
}

function loginEnabled(): boolean | undefined {
  return app.isPackaged ? app.getLoginItemSettings().openAtLogin : undefined;
}

function buildView(): ControlView {
  return controlView({
    state: lastState ?? PLACEHOLDER,
    qr,
    qrImage,
    busy,
    lastAction,
    distros,
    distro,
    crrPath: crrPathError === undefined ? undefined : { ok: false, error: crrPathError },
    login: loginEnabled(),
    sessionsShown: sessionsOnScreen,
  });
}

function pushView(): void {
  const win = mainWindow;
  if (!win || win.isDestroyed() || win.webContents.isDestroyed()) return;
  win.webContents.send('view', buildView());
}

/** Everything that shows state: the tray (once there is a first poll) and the window. */
function refreshUi(): void {
  if (lastState) tray?.update(lastState, busy);
  pushView();
}

function onState(s: PollState): void {
  const verdict = sleep.pollLanded();
  if (verdict !== 'use') {
    // A poll that began before the machine slept: its answer is old. Drop it, and
    // once we have resumed, ask for one that is not.
    if (verdict === 'repoll') void poller.pollNow();
    return;
  }
  lastState = s;
  // A "crr not found" error is only meaningful while we could look for crr; it
  // must not outrank "WSL is stopped" once the distro is down.
  if (s.wsl !== 'running') crrPathError = undefined;
  if (s.wsl !== 'running' || !dashboardActive(s)) {
    qr = undefined;
    qrImage = undefined;
  }
  refreshUi();
  updateSessionsView();
  if (windowOnControlTab() && qr === undefined) void refreshQr();
}

// ---- The Sessions tab ------------------------------------------------------

/**
 * Put the dashboard on screen, or take it off. It shows only on the Sessions
 * tab of a visible window, and only when the latest fresh poll found the
 * distro running, not held, with the dashboard up (sessionsViewUrl). Loading
 * the loopback page never runs wsl.exe.
 */
function updateSessionsView(): void {
  const win = mainWindow;
  const view = sessionsView;
  if (!win || win.isDestroyed() || !view) return;
  view.show(
    sessionsViewUrl({
      tab: activeTab,
      state: lastState,
      suspended: sleep.suspended,
      windowShown: win.isVisible() && !win.isMinimized(),
    }),
  );
}

/** The panel under the view says "Loading…" or nothing, so it must follow the view. */
function onSessionsScreenChange(onScreen: boolean): void {
  sessionsOnScreen = onScreen;
  pushView();
}

/** A link out of the dashboard: http(s) only (navigationGuards), to the default browser. */
function openExternalFromDashboard(url: string): void {
  shell.openExternal(url).catch((e: unknown) => {
    console.warn(`could not open ${url}: ${errorMessage(e)}`);
  });
}

// ---- The phone link --------------------------------------------------------

function windowOnControlTab(): boolean {
  const win = mainWindow;
  return (
    !!win && !win.isDestroyed() && win.isVisible() && !win.isMinimized() && activeTab === 'control'
  );
}

/**
 * Ask crr for the phone link. Never wakes WSL: it runs only when the latest
 * poll found the distro running with the dashboard up, the machine is not
 * (just back from) sleeping, and, read just before the call, the distro is
 * still listed as running and no hold is in place (qrGate).
 */
async function refreshQr(): Promise<void> {
  if (qrBusy) return;
  // Without a running state there is nothing to ask, so skip even the listing.
  if (!qrStateAllows(lastState, sleep.suspended)) return;
  const d = distro;
  qrBusy = true;
  try {
    // The latest poll can be seconds old, and `crr qr` is a `-d` call: a
    // `wsl --shutdown` since then would be undone by it. Listing running
    // distros and reading the hold never boot anything.
    const running = await runningDistros();
    if (distro !== d) return;
    if (!qrGate({ running, distro: d, held: heldNow(), state: lastState, suspended: sleep.suspended })) {
      return;
    }
    const client = await getClient();
    if (!(client instanceof CrrClient)) return;
    const result = await client.qr();
    if (distro !== d) return;
    qr = result;
    qrImage =
      result.ok && result.value.url !== null
        ? await QRCode.toDataURL(result.value.url, { margin: 1, width: 240 })
        : undefined;
  } catch (e) {
    qr = { ok: false, error: summarizeError(errorMessage(e)), exit: -1, mismatch: false };
    qrImage = undefined;
  } finally {
    qrBusy = false;
    pushView();
  }
}

// ---- Distros ---------------------------------------------------------------

/** `wsl.exe -l` lists distros without booting anything. */
async function refreshDistros(): Promise<void> {
  distros = await listDistros();
  if (distro === undefined) {
    const found = await defaultDistro();
    if (found !== undefined) {
      distro = found;
      writeAppConfig({ ...readAppConfig(), distro: found });
    }
  }
  pushView();
}

// ---- Actions (explicit user choices: these may boot WSL) --------------------

async function runAction(name: 'start' | 'stop' | 'repair'): Promise<void> {
  if (busy) return;
  busy = true;
  lastAction = undefined;
  refreshUi();
  try {
    const client = await getClient();
    if (!(client instanceof CrrClient)) {
      lastAction = {
        ok: false,
        message: `crr not found in ${distro ?? 'WSL'}: ${client.error}`,
      };
      return;
    }
    const result = await client[name]();
    lastAction = result.ok
      ? { ok: true, message: result.value }
      : { ok: false, message: result.error };
  } catch (e) {
    lastAction = { ok: false, message: summarizeError(errorMessage(e)) };
  } finally {
    await pollFresh();
    busy = false;
    refreshUi();
  }
}

/** Save the choice, forget the old distro's crr path, boot the new one once, and poll. */
async function chooseDistro(d: string): Promise<void> {
  if (busy) return;
  busy = true;
  lastAction = undefined;
  refreshUi();
  try {
    distros = await listDistros();
    if (!distros.includes(d)) {
      lastAction = { ok: false, message: `${d} is not an installed WSL distro` };
      return;
    }
    distro = d;
    cachedCrrPath = undefined;
    crrPathError = undefined;
    qr = undefined;
    qrImage = undefined;
    writeAppConfig({ ...readAppConfig(), distro: d, crrPath: undefined });
    // An explicit boot: the user chose this distro, so starting it is expected.
    const boot = await realRunInDistro(d)(['true']);
    if (boot.code !== 0) {
      const why = summarizeError(boot.stderr || boot.stdout) || `exit ${boot.code}`;
      lastAction = { ok: false, message: `could not start ${d}: ${why}` };
    }
  } finally {
    await pollFresh();
    busy = false;
    refreshUi();
  }
}

const ipcActions: IpcActions = {
  start: () => runAction('start'),
  stop: () => runAction('stop'),
  repair: () => runAction('repair'),
  setDistro: chooseDistro,
  refresh: async () => {
    lastAction = undefined;
    // The user may have moved or reinstalled crr: forget where it was. The
    // poller resolves it again through client(), after its never-wake gates.
    forgetCrrPath();
    void refreshDistros();
    await pollFresh();
    await refreshQr();
  },
  showTab: async (tab) => {
    activeTab = tab;
    updateSessionsView();
    if (tab === 'control') void refreshQr();
  },
};

// ---- Tray host -------------------------------------------------------------

function openDashboardInBrowser(): void {
  const raw = lastState?.service?.dashboard_url;
  if (!raw) return;
  try {
    const url = new URL(raw);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
    void shell.openExternal(url.toString());
  } catch {
    // crr gave something that is not a URL; there is nothing to open.
  }
}

function startStopFromTray(): void {
  const stopping = buildView().startStop.label === TRAY_LABELS.stop;
  void runAction(stopping ? 'stop' : 'start');
}

const trayImages = new Map<TrayIcon, NativeImage>();

function trayImage(icon: TrayIcon): NativeImage {
  let image = trayImages.get(icon);
  if (!image) {
    image = nativeImage.createFromBuffer(trayIconPng(icon));
    // An empty image is an invisible tray icon, and hiding the window to an
    // invisible icon would strand the app. Fail so the window stays reachable.
    if (image.isEmpty()) throw new Error(`tray icon image for '${icon}' is empty`);
    trayImages.set(icon, image);
  }
  return image;
}

/** Create the tray if Windows lets us. Without one, closing the window must mean leaving. */
function installTray(): void {
  try {
    tray = createTray({
      image: trayImage,
      host: {
        show: showWindow,
        openDashboard: openDashboardInBrowser,
        startStop: startStopFromTray,
        quit,
      },
      // Only a packaged build can register itself; `electron .` would register
      // Electron rather than this app.
      loginItem: app.isPackaged
        ? {
            get: () => app.getLoginItemSettings().openAtLogin,
            set: (enabled) =>
              app.setLoginItemSettings({
                openAtLogin: enabled,
                args: [...LOGIN_ITEM_ARGS],
              }),
          }
        : undefined,
    });
  } catch (e) {
    tray = undefined;
    console.warn(`tray unavailable: ${errorMessage(e)}`);
  }
}

// ---- Window ----------------------------------------------------------------

function createWindow(): BrowserWindow {
  const bounds = readAppConfig().windowBounds;

  const win = new BrowserWindow({
    width: bounds?.width ?? 760,
    height: bounds?.height ?? 640,
    x: bounds?.x,
    y: bounds?.y,
    title: 'CRR',
    autoHideMenuBar: true,
    // The login-item launch carries `--hidden`: an autostart must not drop a
    // window on the desktop every time the machine boots.
    show: false,
    webPreferences: mainWindowWebPreferences(join(__dirname, '../preload/preload.cjs')),
  });

  // The control window shows one local page and nothing else.
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event) => event.preventDefault());

  sessionsOnScreen = false;
  sessionsView = attachSessionsView(win, {
    openExternal: openExternalFromDashboard,
    onScreenChange: onSessionsScreenChange,
  });

  void win.loadFile(join(__dirname, '../renderer/index.html'));
  win.webContents.on('did-finish-load', () => {
    // A (re)loaded page starts on the Control tab, so the dashboard must not
    // stay drawn over it.
    activeTab = 'control';
    updateSessionsView();
    pushView();
  });

  registerIpc(win, ipcActions);

  const persistBounds = (): void => {
    if (win.isDestroyed()) return;
    writeAppConfig({ ...readAppConfig(), windowBounds: win.getBounds() });
  };
  win.on('resize', persistBounds);
  win.on('move', persistBounds);

  // The window is back on screen: refresh the distro list and the phone link.
  const onShown = (): void => {
    updateSessionsView();
    void refreshDistros();
    void refreshQr();
  };
  win.on('show', onShown);
  win.on('restore', onShown);
  // Put away: take the dashboard off screen with it.
  win.on('hide', updateSessionsView);
  win.on('minimize', updateSessionsView);

  // With a tray, closing the window is "put it away", not "leave".
  win.on('close', (event) => {
    if (!shouldHideInsteadOfClose({ trayAvailable: tray !== undefined, quitting: isQuitting })) {
      return;
    }
    event.preventDefault();
    persistBounds();
    win.hide();
  });
  win.on('closed', () => {
    if (mainWindow === win) {
      mainWindow = undefined;
      sessionsView = undefined;
      sessionsOnScreen = false;
    }
  });

  win.show();
  return win;
}

/** Show the window, creating it on first use (the app may have started hidden). */
function showWindow(): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = createWindow();
}

function quit(): void {
  isQuitting = true;
  app.quit();
}

// ---- Startup ---------------------------------------------------------------

// Two tray apps would run two pollers against the same crr, so a second launch
// only brings the first one's window forward.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  // No default menu, before any window exists. Its accelerators would open
  // DevTools on the app's own page (Ctrl+Shift+I), reload it (Ctrl+R) and zoom
  // it (Ctrl+=), and a zoomed page has a tab bar taller than TAB_BAR_HEIGHT,
  // so the dashboard view would overlap it. Windows needs no Edit menu for
  // copy and paste.
  Menu.setApplicationMenu(null);

  app.on('second-instance', () => showWindow());

  if (process.platform === 'win32') app.setAppUserModelId(APP_USER_MODEL_ID);

  void app.whenReady().then(async () => {
    setConfigDir(app.getPath('userData'));
    const config = readAppConfig();
    distro = config.distro;
    cachedCrrPath = config.crrPath;

    installTray();
    // Without a tray a hidden window cannot be recovered, so show it regardless.
    if (!launchFlags.hidden || tray === undefined) showWindow();

    if (distro === undefined) {
      // First run: the Windows default distro. `wsl.exe -l` lists distros
      // without booting any of them.
      distro = await defaultDistro();
      if (distro !== undefined) writeAppConfig({ ...readAppConfig(), distro });
    }

    powerMonitor.on('suspend', () => {
      // Pre-sleep state must not be acted on after waking: WSL may have shut
      // down meanwhile. Only a poll that begins after the resume clears this.
      sleep.suspend();
      lastState = undefined;
      poller.pause();
      updateSessionsView();
    });
    powerMonitor.on('resume', () => {
      sleep.resume();
      poller.resume();
    });
    setInterval(() => {
      if (windowOnControlTab()) void refreshQr();
    }, QR_REFRESH_MS);

    void refreshDistros();
    poller.start();

    app.on('activate', () => showWindow());
  });
}

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('window-all-closed', () => {
  if (
    shouldQuitOnWindowClosed({
      platform: process.platform,
      trayAvailable: tray !== undefined,
    })
  ) {
    app.quit();
  }
});
