/**
 * crr's Windows-side hold flag, read without touching WSL.
 *
 * `crr holder pause|stop` writes C:\ProgramData\crr\holder-hold.json so WSL
 * can stay down. While a hold is in place the tray must make no `wsl.exe -d`
 * call, because a poll would wake the distro. So the tray reads the flag
 * itself and applies the same rules as crr/core/holder_hold.py `holds()`:
 * an invalid or unreadable record means "no hold" (fail open).
 *
 * Validity mirrors crr/core/contracts.py `validate_holder_hold`. Keep the two
 * in step.
 */

import { readFileSync } from 'node:fs';
import os from 'node:os';

export const HOLD_FILE = 'C:\\ProgramData\\crr\\holder-hold.json';
export const HOLDER_HOLD_STORE_VERSION = 1;
// Windows' LastBootUpTime read twice can differ by rounding; a real reboot
// moves it by far more than this.
export const BOOT_MATCH_TOLERANCE_SECONDS = 5;

const HOLDER_HOLD_MODES = ['pause', 'stop', 'until-reboot'] as const;
type HoldMode = (typeof HOLDER_HOLD_MODES)[number];

/** The extra field each mode carries, beyond `v`, `mode` and `set_at`. */
const EXTRA_KEY: Record<HoldMode, 'until' | 'boot' | null> = {
  pause: 'until',
  stop: null,
  'until-reboot': 'boot',
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStrictInt(value: unknown): value is number {
  return Number.isInteger(value) && typeof value !== 'boolean';
}

function isValidRecord(record: unknown): record is Record<string, unknown> {
  if (!isPlainObject(record)) return false;
  if (record.v !== HOLDER_HOLD_STORE_VERSION) return false;
  const mode = record.mode;
  if (typeof mode !== 'string' || !(HOLDER_HOLD_MODES as readonly string[]).includes(mode)) {
    return false;
  }
  const extra = EXTRA_KEY[mode as HoldMode];
  const expectedKeys = new Set(['v', 'mode', 'set_at', ...(extra ? [extra] : [])]);
  const keys = Object.keys(record);
  if (keys.length !== expectedKeys.size || keys.some((k) => !expectedKeys.has(k))) {
    return false;
  }
  const intFields = extra ? ['set_at', extra] : ['set_at'];
  return intFields.every((key) => isStrictInt(record[key]));
}

/**
 * Should the holder stay down right now?
 *
 * `nowSec` and `machineBootSec` are epoch seconds. `undefined` for the boot
 * means the machine's boot time is unknown; an until-reboot hold then holds.
 */
export function holds(
  record: unknown,
  nowSec: number,
  machineBootSec: number | undefined,
): boolean {
  if (!isValidRecord(record)) return false;
  if (record.mode === 'stop') return true;
  if (record.mode === 'pause') return nowSec < (record.until as number);
  if (machineBootSec === undefined) return true;
  const sameBoot =
    Math.abs((record.boot as number) - machineBootSec) <= BOOT_MATCH_TOLERANCE_SECONDS;
  return sameBoot;
}

/**
 * Read and parse the hold flag. Returns `undefined` on any error (missing
 * file, bad JSON, read failure). Validation is `holds()`'s job.
 */
export function readHold(
  path: string = HOLD_FILE,
  read: (p: string) => string = (p) => readFileSync(p, 'utf8'),
): unknown {
  try {
    return JSON.parse(read(path));
  } catch {
    return undefined;
  }
}

/** This machine's boot time in epoch seconds, from the current uptime. */
export function machineBootSeconds(
  nowMs: number = Date.now(),
  uptimeSec: number = os.uptime(),
): number {
  return nowMs / 1000 - uptimeSec;
}
