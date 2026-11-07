import type { WebPreferences } from 'electron';

/**
 * The control window's renderer settings, exactly. The renderer is sandboxed
 * and isolated with no Node, and its only way to the app is the preload's
 * `window.crr`. Kept in a module that does not touch Electron's runtime so a
 * test can pin it; `main.ts` re-exports it.
 */
export function mainWindowWebPreferences(preloadPath: string): WebPreferences {
  return {
    preload: preloadPath,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
  };
}
