/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
// The window is sandboxed, so this file may require only 'electron'. Anything
// else it imports must be a type (erased by the bundler).
import { contextBridge, ipcRenderer } from 'electron';
import type { ControlView } from '../main/controlView.js';

/** The `window.crr` surface the renderer consumes. */
export interface CrrApi {
  onView(cb: (v: ControlView) => void): void;
  start(): Promise<void>;
  stop(): Promise<void>;
  repair(): Promise<void>;
  setDistro(d: string): Promise<void>;
  refresh(): Promise<void>;
  showTab(tab: 'control' | 'sessions'): Promise<void>;
}

const api: CrrApi = {
  onView: (cb) => {
    ipcRenderer.on('view', (_event, view: ControlView) => cb(view));
  },
  start: () => ipcRenderer.invoke('start'),
  stop: () => ipcRenderer.invoke('stop'),
  repair: () => ipcRenderer.invoke('repair'),
  setDistro: (d) => ipcRenderer.invoke('setDistro', d),
  refresh: () => ipcRenderer.invoke('refresh'),
  showTab: (tab) => ipcRenderer.invoke('showTab', tab),
};

contextBridge.exposeInMainWorld('crr', api);

declare global {
  interface Window {
    crr: CrrApi;
  }
}
