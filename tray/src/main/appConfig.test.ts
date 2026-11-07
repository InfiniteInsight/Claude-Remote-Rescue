import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { parseAppConfig, readAppConfig, writeAppConfig } from './appConfig.js';

describe('parseAppConfig', () => {
  // test_parse_app_config
  it('keeps the known fields', () => {
    expect(
      parseAppConfig({
        distro: 'Ubuntu',
        crrPath: '/home/e/.local/state/crr/app/bin/crr',
        windowBounds: { x: 1, y: 2, width: 800, height: 600 },
      }),
    ).toEqual({
      distro: 'Ubuntu',
      crrPath: '/home/e/.local/state/crr/app/bin/crr',
      windowBounds: { x: 1, y: 2, width: 800, height: 600 },
    });
  });

  it('drops unknown keys, so a secret written by mistake is never carried forward', () => {
    const out = parseAppConfig({ distro: 'Ubuntu', passphrase: 'hunter2', terminalCursor: 4 });
    expect(out).toEqual({ distro: 'Ubuntu' });
    expect(Object.keys(out)).not.toContain('passphrase');
  });

  it('drops a non-string distro or crrPath', () => {
    expect(parseAppConfig({ distro: 7, crrPath: 42 })).toEqual({});
    expect(parseAppConfig({ crrPath: ['/x'] })).toEqual({});
    expect(parseAppConfig({ crrPath: '' })).toEqual({});
  });

  it('needs numeric width and height for the bounds', () => {
    expect(parseAppConfig({ windowBounds: { width: 800 } })).toEqual({});
    expect(parseAppConfig({ windowBounds: { width: '800', height: 600 } })).toEqual({});
    expect(parseAppConfig({ windowBounds: null })).toEqual({});
    expect(parseAppConfig({ windowBounds: { width: 800, height: 600 } })).toEqual({
      windowBounds: { width: 800, height: 600 },
    });
  });

  it('drops a mistyped x or y but keeps the size', () => {
    expect(parseAppConfig({ windowBounds: { x: 'left', y: 3, width: 8, height: 6 } })).toEqual({
      windowBounds: { y: 3, width: 8, height: 6 },
    });
  });

  it('gives an empty config for anything that is not an object', () => {
    for (const raw of [undefined, null, 'x', 3, [], true]) {
      expect(parseAppConfig(raw)).toEqual({});
    }
  });
});

describe('read and write', () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  });
  const scratch = (): string => {
    const d = mkdtempSync(join(tmpdir(), 'crr-tray-config-'));
    dirs.push(d);
    return join(d, 'crr-tray.json');
  };

  it('round-trips, and a missing or corrupt file reads as empty', () => {
    const p = scratch();
    expect(readAppConfig(p)).toEqual({});
    writeAppConfig({ distro: 'Ubuntu', crrPath: '/c' }, p);
    expect(readAppConfig(p)).toEqual({ distro: 'Ubuntu', crrPath: '/c' });
    writeFileSync(p, '{not json', 'utf8');
    expect(readAppConfig(p)).toEqual({});
  });

  it('writes only the known fields', () => {
    const p = scratch();
    writeAppConfig({ distro: 'U', extra: 'secret' } as never, p);
    expect(JSON.parse(readFileSync(p, 'utf8'))).toEqual({ distro: 'U' });
  });
});
