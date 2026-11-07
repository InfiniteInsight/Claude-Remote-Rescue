/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  buildLabel,
  buildStamp,
  formatBuiltAt,
  TOOLTIP_MAX,
  trayTooltip,
  windowTitle,
} from './buildInfo.js';

const STAMP = { commit: '84795e1', builtAt: '2026-11-03T19:40:07.000Z' };

describe('formatBuiltAt', () => {
  it('renders the ISO build time in UTC', () => {
    expect(formatBuiltAt('2026-11-03T19:40:07.000Z')).toBe(
      '2026-11-03 19:40 UTC',
    );
  });

  it('renders a non-UTC offset as UTC rather than relabelling the wall clock', () => {
    // 21:40 at +02:00 is 19:40Z: the title has to say the same instant
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
    // would be a ReferenceError at import.
    expect(buildStamp()).toEqual({ commit: 'unknown', builtAt: 'unknown' });
  });
});

describe('buildLabel and windowTitle', () => {
  // test_build_label
  it('names the version and the commit', () => {
    expect(buildLabel('0.1.0', STAMP)).toBe('CRR 0.1.0 (84795e1)');
  });

  it('the window title adds the build time', () => {
    expect(windowTitle('0.1.0', STAMP)).toBe('CRR 0.1.0 (84795e1) · built 2026-11-03 19:40 UTC');
  });

  it('says unknown when it cannot tell, rather than leaving the build out', () => {
    const unknown = { commit: 'unknown', builtAt: 'unknown' };
    expect(buildLabel('0.1.0', unknown)).toBe('CRR 0.1.0 (unknown)');
    expect(windowTitle('0.1.0', unknown)).toBe('CRR 0.1.0 (unknown) · built unknown');
  });
});

describe('trayTooltip', () => {
  // test_tray_tooltip
  it('is the status line, then the build', () => {
    expect(trayTooltip('CRR running · 3 sessions, 0 crashed', '0.1.0', STAMP)).toBe(
      'CRR running · 3 sessions, 0 crashed\nCRR 0.1.0 (84795e1)',
    );
  });

  it('fits Windows\' tooltip limit by shortening the status line, never the build', () => {
    const long = `CRR unreachable: ${'x'.repeat(200)}`;
    const tip = trayTooltip(long, '0.1.0', STAMP);
    expect(tip.length).toBeLessThanOrEqual(TOOLTIP_MAX);
    expect(tip.endsWith('…\nCRR 0.1.0 (84795e1)')).toBe(true);
    expect(tip.startsWith('CRR unreachable: xxx')).toBe(true);
  });

  it('is 127 characters at most, what a Windows notify icon holds', () => {
    expect(TOOLTIP_MAX).toBe(127);
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
    expect(define).toContain('__CRR_BUILD_COMMIT__');
    expect(define).toContain('__CRR_BUILD_AT__');
  });

  it('stamps the main context, which replaces rather than merges', () => {
    // nodeBuildOptions carries the stamp, and mainImportMetaShim (which defines
    // `import.meta.url` for main.ts) spreads AFTER it, so its `define` REPLACES
    // the shared one instead of adding to it. The stamp has to be restated
    // there: main.cjs is the only bundle that reads it, and dropping it there
    // builds green, runs fine, and shows "unknown" for every build.
    const shared = esbuildSrc.match(/const nodeBuildOptions = \{[^}]*\}/)?.[0];
    expect(shared).toContain('...BUILD_DEFINE');
    const mainShim = block('mainImportMetaShim');
    expect(mainShim).toContain("'import.meta.url'");
    expect(mainShim).toContain('...BUILD_DEFINE');
  });
});
