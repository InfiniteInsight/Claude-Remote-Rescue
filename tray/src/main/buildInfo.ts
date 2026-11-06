/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */

/**
 * Which build is installed (issue #210).
 *
 * The tray app is installed by copying `release/win-unpacked` by hand over
 * `C:\Users\Infin\AppData\Local\Programs\CRR`, so nothing at the
 * install names the commit it came from. That cost an entire issue: on 2026-10-30
 * an app built on 10-12 was running under a fix the fork had landed on 10-27, the
 * phone's repeat "open this conversation" requests were swallowed by the dedup bug
 * that fix had already removed, and nothing on the host said the running code
 * predated the fix.
 *
 * So the identity is stamped at esbuild time and shown in the tray tooltip, which
 * `tray.ts` records as the only output a packaged build has: there is no console,
 * no --version path, and no log file to read it from.
 */

/**
 * Substituted by esbuild's `define` (see esbuild.js). Read through `typeof`
 * rather than bare: vitest loads this source unbundled, where the identifiers are
 * never declared, and `typeof` on an undeclared name yields 'undefined' where a
 * bare read would throw a ReferenceError and take the module down at import.
 */
declare const __LAUNCHER_BUILD_COMMIT__: string;
declare const __LAUNCHER_BUILD_AT__: string;

const UNKNOWN = 'unknown';

/** What this bundle knows about itself. */
export interface BuildStamp {
  commit: string;
  /** When the bundle was built, not when the commit was made. */
  builtAt: string;
}

/** The stamp, or `unknown` on both fields when the bundle was not stamped. */
export function buildStamp(): BuildStamp {
  return {
    commit:
      typeof __LAUNCHER_BUILD_COMMIT__ === 'string'
        ? __LAUNCHER_BUILD_COMMIT__
        : UNKNOWN,
    builtAt:
      typeof __LAUNCHER_BUILD_AT__ === 'string'
        ? __LAUNCHER_BUILD_AT__
        : UNKNOWN,
  };
}

/** `2026-11-03T19:40:07.000Z` to `2026-11-03 19:40 UTC`, in UTC whatever the offset. */
export function formatBuiltAt(builtAt: string): string {
  if (builtAt === UNKNOWN) return UNKNOWN;
  const at = new Date(builtAt);
  if (Number.isNaN(at.getTime())) return builtAt;
  return `${at.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

/**
 * Tray tooltip text: the name plus what build is running.
 *
 * An unstamped build says `unknown` in plain sight rather than showing only the
 * name. "We cannot tell which build this is" is the exact fact that must never be
 * silent again, so it is stated instead of omitted.
 */
export function trayTooltip(
  name: string,
  stamp: BuildStamp = buildStamp(),
): string {
  return `${name} · ${stamp.commit} · built ${formatBuiltAt(stamp.builtAt)}`;
}
