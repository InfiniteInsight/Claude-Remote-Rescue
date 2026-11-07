import { describe, expect, it } from 'vitest';
import { mainWindowWebPreferences } from './windowPrefs.js';

describe('mainWindowWebPreferences', () => {
  // test_main_window_is_sandboxed
  it('is sandboxed, isolated and has no node integration, with exactly those keys', () => {
    expect(mainWindowWebPreferences('C:\\app\\dist\\preload\\preload.cjs')).toEqual({
      preload: 'C:\\app\\dist\\preload\\preload.cjs',
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    });
  });
});
