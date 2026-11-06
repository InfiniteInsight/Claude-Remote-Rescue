import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildStamp, formatBuiltAt, trayTooltip } from './buildInfo.js';

describe('formatBuiltAt', () => {
  it('renders the ISO build time in UTC', () => {
    expect(formatBuiltAt('2026-11-03T19:40:07.000Z')).toBe(
      '2026-11-03 19:40 UTC',
    );
  });

  it('renders a non-UTC offset as UTC rather than relabelling the wall clock', () => {
    // 21:40 at +02:00 is 19:40Z: the tooltip has to say the same instant
    // regardless of the timezone of whoever ran the build.
    expect(formatBuiltAt('2026-11-03T21:40:07+02:00')).toBe(
      '2026-11-03 19:40 UTC',
    );
  });

  it('passes an unknown or unparseable stamp through', () => {
    expect(formatBuiltAt('unknown')).toBe('unknown');
    expect(formatBuiltAt('not a date')).toBe('not a date');
  });
});

describe('buildStamp', () => {
  it('falls back to unknown instead of throwing when the bundle is unstamped', () => {
    // This is the unbundled case: vitest loads buildInfo.ts directly, so the
    // esbuild `define` never ran and the identifiers do not exist. A bare read
    // would be a ReferenceError at import and every tray call would throw.
    expect(buildStamp()).toEqual({ commit: 'unknown', builtAt: 'unknown' });
  });
});

describe('trayTooltip', () => {
  it('names the commit and the build time', () => {
    const tip = trayTooltip('CRR', {
      commit: '84795e1',
      builtAt: '2026-11-03T19:40:07.000Z',
    });
    expect(tip).toContain('CRR');
    expect(tip).toContain('84795e1');
    expect(tip).toContain('2026-11-03 19:40 UTC');
  });

  it('says unknown when it cannot tell, rather than showing only the name', () => {
    // The whole point of the stamp is that "which build is this" was once
    // unanswerable on an installed app. Silence would recreate the bug.
    expect(
      trayTooltip('CRR', { commit: 'unknown', builtAt: 'unknown' }),
    ).toBe('CRR · unknown · built unknown');
  });
});

describe('the build stamps the bundle that reads it', () => {
  const esbuildSrc = readFileSync(
    fileURLToPath(new URL('../../esbuild.js', import.meta.url)),
    'utf8',
  );

  const block = (start: string): string | undefined =>
    esbuildSrc.match(new RegExp(`const ${start} = \\{[\\s\\S]*?\\n\\};`))?.[0];

  it('defines the stamp once, naming both fields', () => {
    const define = block('BUILD_DEFINE');
    expect(define).toContain('__LAUNCHER_BUILD_COMMIT__');
    expect(define).toContain('__LAUNCHER_BUILD_AT__');
  });

  it('stamps the main context, which replaces rather than merges', () => {
    // nodeBuildOptions carries the stamp, and mainImportMetaShim (which defines
    // `import.meta.url` for main.ts) spreads AFTER it, so its `define` REPLACES
    // the shared one instead of adding to it. The stamp has to be restated there:
    // main.cjs is the only bundle that calls trayTooltip, and dropping it there
    // builds green, runs fine, and tells you nothing about which build is
    // installed, which is the #210 blind spot exactly as it was.
    const shared = esbuildSrc.match(/const nodeBuildOptions = \{[^}]*\}/)?.[0];
    expect(shared).toContain('...BUILD_DEFINE');
    const mainShim = block('mainImportMetaShim');
    expect(mainShim).toContain("'import.meta.url'");
    expect(mainShim).toContain('...BUILD_DEFINE');
  });
});
