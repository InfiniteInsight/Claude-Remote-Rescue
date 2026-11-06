/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { execFile } from 'node:child_process';

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

/**
 * Run an argv inside the WSL distro. `argv` is the crr path followed by its
 * arguments; it is never a shell string.
 */
export type RunArgv = (argv: readonly string[]) => Promise<CommandResult>;

/** The subset of `child_process.execFile` this module uses, injectable for tests. */
export type ExecFile = (
  file: string,
  args: readonly string[],
  opts: { encoding: 'utf8' | 'buffer'; timeout: number; maxBuffer: number },
  cb: (err: Error | null, stdout: string | Buffer, stderr: string | Buffer) => void,
) => void;

/** Upper bound on any single wsl.exe call (measured ~1.4 s; allow headroom). */
export const WSL_TIMEOUT_MS = 20_000;

const defaultExec = execFile as unknown as ExecFile;

/**
 * Real impl. Runs `wsl.exe -d <distro> -e <argv...>` with no shell, so a distro
 * name or crr path can never be interpreted. Never rejects: a missing wsl.exe
 * resolves code -1, a timeout resolves code -1 with stderr 'timed out', and any
 * other failure resolves its exit code (1 when none is available).
 */
export function realRunInDistro(
  distro: string,
  exec: ExecFile = defaultExec,
): RunArgv {
  return (argv) =>
    new Promise((resolve) => {
      exec(
        'wsl.exe',
        ['-d', distro, '-e', ...argv],
        { encoding: 'utf8', timeout: WSL_TIMEOUT_MS, maxBuffer: 16 * 1024 * 1024 },
        (err, stdout, stderr) => {
          const out = typeof stdout === 'string' ? stdout : '';
          const errText = typeof stderr === 'string' ? stderr : '';
          if (!err) {
            resolve({ code: 0, stdout: out, stderr: errText });
            return;
          }
          const e = err as { code?: unknown; killed?: unknown };
          if (e.code === 'ENOENT') {
            resolve({ code: -1, stdout: '', stderr: 'wsl.exe not found' });
            return;
          }
          if (e.killed === true) {
            resolve({ code: -1, stdout: out, stderr: 'timed out' });
            return;
          }
          const code = typeof e.code === 'number' ? e.code : 1;
          resolve({ code, stdout: out, stderr: errText });
        },
      );
    });
}

/**
 * Decode raw `wsl.exe` output. wsl.exe's own listings are UTF-16LE, so a NUL
 * byte marks that encoding; anything else is read as UTF-8.
 */
export function decodeWslOutput(buf: Buffer): string {
  return buf.includes(0) ? buf.toString('utf16le') : buf.toString('utf8');
}

/** Parse decoded `wsl.exe -l -q` output into a list of distro names. */
export function parseDistroList(raw: string): string[] {
  return raw
    .split('\n')
    .map((l) =>
      l
        .replace(/\r/g, '')
        .replace(/\s*\(Default\)\s*$/i, '')
        .trim(),
    )
    .filter((l) => l.length > 0);
}

/** Return the distro that `wsl.exe -l` marks `(Default)`, if any. */
export function parseDefaultDistro(raw: string): string | undefined {
  for (const line of raw.split('\n')) {
    const m = line.replace(/\r/g, '').match(/^(.*?)\s*\(Default\)\s*$/i);
    if (m && m[1].trim().length > 0) return m[1].trim();
  }
  return undefined;
}

/** Run a wsl.exe listing and return its decoded stdout (empty on failure). */
function runListing(
  args: readonly string[],
  exec: ExecFile,
): Promise<{ err: Error | null; text: string }> {
  return new Promise((resolve) => {
    exec(
      'wsl.exe',
      args,
      { encoding: 'buffer', timeout: WSL_TIMEOUT_MS, maxBuffer: 1024 * 1024 },
      (err, stdout) => {
        const buf = Buffer.isBuffer(stdout) ? stdout : Buffer.alloc(0);
        resolve({ err, text: decodeWslOutput(buf) });
      },
    );
  });
}

/** Enumerate installed WSL distros (`wsl.exe -l -q`). */
export async function listDistros(exec: ExecFile = defaultExec): Promise<string[]> {
  const { text } = await runListing(['-l', '-q'], exec);
  return parseDistroList(text);
}

/** The distro marked `(Default)` in `wsl.exe -l`, if wsl.exe can be run. */
export async function defaultDistro(
  exec: ExecFile = defaultExec,
): Promise<string | undefined> {
  const { err, text } = await runListing(['-l'], exec);
  if (err) return undefined;
  return parseDefaultDistro(text);
}

/**
 * Running distros (`wsl.exe -l --running -q`). Returns undefined only when
 * wsl.exe cannot be run at all. A non-zero exit ("There are no running
 * distributions") and any other failure return [].
 */
export async function runningDistros(
  exec: ExecFile = defaultExec,
): Promise<string[] | undefined> {
  const { err, text } = await runListing(['-l', '--running', '-q'], exec);
  if (err) {
    return (err as { code?: unknown }).code === 'ENOENT' ? undefined : [];
  }
  return parseDistroList(text);
}
