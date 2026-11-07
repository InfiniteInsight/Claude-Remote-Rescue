/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import {
  app,
  BrowserWindow,
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
import { controlView, type ControlView } from './controlView.js';
import type { QrInfo } from './contracts.js';
import { CrrClient, resolveCrrPath, type CrrResult } from './crrClient.js';
import { holds, machineBootSeconds, readHold } from './hold.js';
import { Poller, type PollState, type TrayIcon } from './poll.js';
import { summarizeError } from './summarizeError.js';
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
/** When the latest poll began, to tell whether a joined poll predates an action. */
let lastPollStartedAt = 0;

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function isHeld(): boolean {
  return holds(readHold(), Date.now() / 1000, machineBootSeconds());
}

function dashboardUp(s: PollState | undefined): boolean {
  return s?.service?.services.some((u) => u.name === 'dashboard' && u.active) ?? false;
}

// ---- crr client ------------------------------------------------------------

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
    return new CrrClient(run, cachedCrrPath);
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
  return new CrrClient(run, found.path);
}

// ---- The poller, tray and view ---------------------------------------------

const poller = new Poller({
  runningDistros: () => {
    lastPollStartedAt = Date.now();
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

/** Poll, and if the poll joined was already under way before `since`, poll once more. */
async function pollFresh(since: number): Promise<void> {
  await poller.pollNow();
  if (lastPollStartedAt < since) await poller.pollNow();
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
  lastState = s;
  if (s.wsl !== 'running' || !dashboardUp(s)) {
    qr = undefined;
    qrImage = undefined;
  }
  refreshUi();
  if (windowOnControlTab() && qr === undefined) void refreshQr();
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
 * poll found the distro running with the dashboard up and no hold in place.
 */
async function refreshQr(): Promise<void> {
  if (qrBusy) return;
  const s = lastState;
  if (s === undefined || s.wsl !== 'running' || !dashboardUp(s) || isHeld()) return;
  const d = distro;
  qrBusy = true;
  try {
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
    await pollFresh(Date.now());
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
    await pollFresh(Date.now());
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
    void refreshDistros();
    await poller.pollNow();
    await refreshQr();
  },
  showTab: async (tab) => {
    // The Sessions tab's dashboard view is Task 8's; here the tab is only recorded.
    activeTab = tab;
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

  void win.loadFile(join(__dirname, '../renderer/index.html'));
  win.webContents.on('did-finish-load', () => pushView());

  registerIpc(win, ipcActions);

  const persistBounds = (): void => {
    if (win.isDestroyed()) return;
    writeAppConfig({ ...readAppConfig(), windowBounds: win.getBounds() });
  };
  win.on('resize', persistBounds);
  win.on('move', persistBounds);

  // The window is back on screen: refresh the distro list and the phone link.
  const onShown = (): void => {
    void refreshDistros();
    void refreshQr();
  };
  win.on('show', onShown);
  win.on('restore', onShown);

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
    if (mainWindow === win) mainWindow = undefined;
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

    powerMonitor.on('suspend', () => poller.pause());
    powerMonitor.on('resume', () => poller.resume());
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
