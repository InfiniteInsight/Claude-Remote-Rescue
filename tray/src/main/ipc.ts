/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { ipcMain, type BrowserWindow, type IpcMainInvokeEvent } from 'electron';

export type Tab = 'control' | 'sessions';

/** What the renderer may ask the app to do. Each is an explicit user action. */
export interface IpcActions {
  start(): Promise<void>;
  stop(): Promise<void>;
  repair(): Promise<void>;
  setDistro(distro: string): Promise<void>;
  refresh(): Promise<void>;
  showTab(tab: Tab): Promise<void>;
}

const CHANNELS = [
  'start',
  'stop',
  'repair',
  'setDistro',
  'refresh',
  'showTab',
] as const;

/** A WSL distro name is short and printable; refuse anything else before it nears an argv. */
const MAX_DISTRO_LENGTH = 256;

export function isTab(v: unknown): v is Tab {
  return v === 'control' || v === 'sessions';
}

export function isDistroName(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0 && v.length <= MAX_DISTRO_LENGTH;
}

/**
 * Wire the `window.crr` IPC surface (see preload.ts) for one BrowserWindow.
 * Every handler checks that the call came from this window's own renderer and
 * validates its argument; the handlers carry no payload back, because what the
 * user needs to see (success, failure, new state) arrives as a `ControlView`.
 */
export function registerIpc(win: BrowserWindow, actions: IpcActions): void {
  const guarded =
    <A extends unknown[]>(fn: (...args: A) => Promise<void>) =>
    async (event: IpcMainInvokeEvent, ...args: A): Promise<void> => {
      if (win.isDestroyed() || event.sender !== win.webContents) return;
      await fn(...args);
    };

  ipcMain.handle('start', guarded(() => actions.start()));
  ipcMain.handle('stop', guarded(() => actions.stop()));
  ipcMain.handle('repair', guarded(() => actions.repair()));
  ipcMain.handle('refresh', guarded(() => actions.refresh()));
  ipcMain.handle(
    'setDistro',
    guarded(async (distro: unknown) => {
      if (isDistroName(distro)) await actions.setDistro(distro);
    }),
  );
  ipcMain.handle(
    'showTab',
    guarded(async (tab: unknown) => {
      if (isTab(tab)) await actions.showTab(tab);
    }),
  );

  win.on('closed', () => {
    for (const channel of CHANNELS) ipcMain.removeHandler(channel);
  });
}
