/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */

/**
 * Tray presence.
 *
 * The app has to outlive its window, so it needs a surface that exists without
 * one. On Windows a left click on the tray icon fires `click` and a right click
 * opens the context menu, which gives the usual arrangement: click to bring the
 * window back, right-click for the menu.
 *
 * The icon's colour shows crr's state, and the menu's first line says it in
 * words. What each menu item says and whether it is enabled is decided by the
 * pure `trayMenuSpec`; this file only turns that into Electron objects.
 */
import { Menu, Tray } from 'electron';
import { escapeMenuLabel, menuClick, menuSignature, trayMenuSpec, type MenuHost } from './appLifecycle.js';
import type { PollState, TrayIcon } from './poll.js';

/** What the tray can ask the app to do (Start/Stop carries the action its item showed). */
export type TrayHost = MenuHost;

/** Login-item state, read from and written to the OS. */
export interface LoginItemControl {
  get: () => boolean;
  set: (enabled: boolean) => void;
}

export interface TrayOptions {
  /** The image for each icon state (a real image: an empty tray icon is invisible). */
  image: (icon: TrayIcon) => Electron.NativeImage;
  host: TrayHost;
  /** The tooltip for a status line (the status line plus which build this is). */
  tooltip: (statusLine: string) => string;
  /** Omit when a login item cannot be registered (an unpackaged run). */
  loginItem?: LoginItemControl;
}

/** Re-renders the tray for a new poll state. */
export interface TrayHandle {
  update: (state: PollState, busy: boolean) => void;
}

/**
 * Create the tray. The handle keeps the `Tray` alive by closing over it — an
 * unreferenced Tray would be collected and the icon would vanish.
 */
export function createTray(opts: TrayOptions): TrayHandle {
  const initial: PollState = {
    icon: 'stopped',
    statusLine: 'Checking crr…',
    wsl: 'missing',
  };
  let shownIcon: TrayIcon = initial.icon;
  const tray = new Tray(opts.image(shownIcon));
  let last: { state: PollState; busy: boolean } = { state: initial, busy: false };
  // What the tray currently shows. The menu and tooltip are touched only when
  // these change: on Windows, setContextMenu while the menu is open can close
  // it under the user's cursor, and a poll lands every few seconds.
  let shownSignature: string | undefined;
  let shownTooltip: string | undefined;

  const render = (): void => {
    const { state, busy } = last;
    const items = trayMenuSpec({
      state,
      packaged: !!opts.loginItem,
      loginItemEnabled: opts.loginItem?.get() ?? false,
      busy,
    });
    const tooltip = opts.tooltip(state.statusLine);
    if (tooltip !== shownTooltip) {
      shownTooltip = tooltip;
      tray.setToolTip(tooltip);
    }
    const signature = menuSignature(items);
    if (signature === shownSignature) return;
    shownSignature = signature;
    const template: Electron.MenuItemConstructorOptions[] = items.map((item) => {
      if (item.id === 'status') {
        return { label: escapeMenuLabel(item.label), enabled: false };
      }
      if (item.id === 'loginItem') {
        const control = opts.loginItem;
        return {
          label: item.label,
          type: 'checkbox',
          checked: item.checked ?? false,
          enabled: item.enabled,
          click: (menuItem) => {
            control?.set(menuItem.checked);
            // Reflect what the OS actually holds now, in case the write was
            // refused (group policy, non-writable Run key). Electron has
            // already flipped the checkbox, so force a rebuild even when the
            // OS state (and so the signature) is unchanged. The menu closed
            // on this click, so rebuilding it is safe.
            shownSignature = undefined;
            render();
          },
        };
      }
      // Bound to what this item shows: the menu is rebuilt whenever that changes.
      return {
        label: item.label,
        enabled: item.enabled,
        click: menuClick(item, opts.host),
      };
    });
    tray.setContextMenu(Menu.buildFromTemplate(template));
  };

  render();
  tray.on('click', () => opts.host.show());

  return {
    update(state, busy) {
      last = { state, busy };
      if (state.icon !== shownIcon) {
        shownIcon = state.icon;
        tray.setImage(opts.image(state.icon));
      }
      render();
    },
  };
}
