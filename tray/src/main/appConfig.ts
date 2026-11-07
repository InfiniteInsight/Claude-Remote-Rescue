/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export interface WindowBounds {
  x?: number;
  y?: number;
  width: number;
  height: number;
}

/**
 * Persisted app config. This MUST NEVER contain secrets: the app never handles
 * the dashboard passphrase or its cookie. It only remembers which distro crr
 * lives in, where crr is inside it, and where the window was.
 */
export interface AppConfig {
  distro?: string;
  /** Absolute path of crr's launcher inside `distro`; cleared when the distro changes. */
  crrPath?: string;
  windowBounds?: WindowBounds;
}

const CONFIG_FILE = 'crr-tray.json';

let configDir: string | undefined;

/**
 * Point the config at Electron's per-user data directory. Set once by `main.ts`
 * at startup. It is a setter rather than an `electron` import so this module
 * (and its tests) load without Electron present.
 */
export function setConfigDir(dir: string): void {
  configDir = dir;
}

/** `<userData>/crr-tray.json`. */
export function configPath(): string {
  if (configDir === undefined) {
    throw new Error('app config directory is not set');
  }
  return join(configDir, CONFIG_FILE);
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function parseBounds(v: unknown): WindowBounds | undefined {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined;
  const b = v as Record<string, unknown>;
  if (!isFiniteNumber(b['width']) || !isFiniteNumber(b['height'])) return undefined;
  const out: WindowBounds = { width: b['width'], height: b['height'] };
  if (isFiniteNumber(b['x'])) out.x = b['x'];
  if (isFiniteNumber(b['y'])) out.y = b['y'];
  return out;
}

/** Keep only the known, correctly typed fields. Never throws. */
export function parseAppConfig(raw: unknown): AppConfig {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const r = raw as Record<string, unknown>;
  const out: AppConfig = {};
  if (typeof r['distro'] === 'string' && r['distro'] !== '') out.distro = r['distro'];
  if (typeof r['crrPath'] === 'string' && r['crrPath'] !== '') out.crrPath = r['crrPath'];
  const bounds = parseBounds(r['windowBounds']);
  if (bounds) out.windowBounds = bounds;
  return out;
}

/** Read the persisted config. Missing/corrupt file gives an empty config (never throws). */
export function readAppConfig(path?: string): AppConfig {
  try {
    return parseAppConfig(JSON.parse(readFileSync(path ?? configPath(), 'utf8')));
  } catch {
    return {};
  }
}

/** Persist the config (distro, crr path and window bounds only; never secrets). */
export function writeAppConfig(config: AppConfig, path?: string): void {
  try {
    const p = path ?? configPath();
    const dir = dirname(p);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    writeFileSync(p, JSON.stringify(parseAppConfig(config), null, 2), 'utf8');
  } catch {
    // Best-effort persistence; a failed write just falls back to defaults
    // next launch.
  }
}
