/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */

/**
 * Which build of the tray app is running.
 *
 * The app is installed by copying `release/win-unpacked` into a folder by hand,
 * so nothing in the install names the commit it was built from. esbuild stamps
 * the commit and the build time into the bundle (see esbuild.js), and the app
 * shows them in the window title and the tray tooltip: a packaged build has no
 * console and no log to read them from. Adapted from the Qwen launcher.
 */

/**
 * Substituted by esbuild's `define` (see esbuild.js). Read through `typeof`
 * rather than bare: vitest loads this source unbundled, where the identifiers are
 * never declared, and `typeof` on an undeclared name yields 'undefined' where a
 * bare read would throw a ReferenceError and take the module down at import.
 */
declare const __CRR_BUILD_COMMIT__: string;
declare const __CRR_BUILD_AT__: string;

const UNKNOWN = 'unknown';

/** The most a Windows tray tooltip holds (a notify icon's 128-char buffer, less its NUL). */
export const TOOLTIP_MAX = 127;

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
      typeof __CRR_BUILD_COMMIT__ === 'string' ? __CRR_BUILD_COMMIT__ : UNKNOWN,
    builtAt: typeof __CRR_BUILD_AT__ === 'string' ? __CRR_BUILD_AT__ : UNKNOWN,
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
 * `CRR <version> (<commit>)`. An unstamped build says `unknown` in plain sight
 * rather than leaving the build out: "which build is this" must always have
 * an answer, even when the answer is that we can't tell.
 */
export function buildLabel(version: string, stamp: BuildStamp = buildStamp()): string {
  return `CRR ${version} (${stamp.commit})`;
}

/** The window title: the build label and when it was built. */
export function windowTitle(version: string, stamp: BuildStamp = buildStamp()): string {
  return `${buildLabel(version, stamp)} · built ${formatBuiltAt(stamp.builtAt)}`;
}

/**
 * The tray tooltip: the status line, then the build label on its own line.
 * Windows cuts a tooltip off at `TOOLTIP_MAX`, so a long status line (crr's
 * error text) is shortened here instead, and the build label always shows.
 */
export function trayTooltip(
  statusLine: string,
  version: string,
  stamp: BuildStamp = buildStamp(),
): string {
  const label = buildLabel(version, stamp);
  const room = TOOLTIP_MAX - label.length - 1;
  const status =
    statusLine.length > room ? `${statusLine.slice(0, Math.max(0, room - 1))}…` : statusLine;
  return `${status}\n${label}`;
}
