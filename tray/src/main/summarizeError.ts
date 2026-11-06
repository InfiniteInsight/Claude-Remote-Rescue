/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */

/**
 * Shorten a child-process failure for the tray's single status line.
 *
 * Node prefixes these with `Command failed: <full argv>` and repeats the argv
 * inside stderr, which would push the actual reason off the menu.
 */
export function summarizeError(raw: string): string {
  const lines = raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  const last = lines[lines.length - 1] ?? raw.trim();
  return last.length > 90 ? `${last.slice(0, 89)}…` : last;
}
