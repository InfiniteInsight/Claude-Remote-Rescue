/**
 * @license
 * Copyright 2025 Qwen Team
 * SPDX-License-Identifier: Apache-2.0
 * Modified for Claude-Remote-Rescue (crr), 2026.
 */
import { describe, it, expect } from 'vitest';
import {
  decodeWslOutput,
  defaultDistro,
  listDistros,
  parseDefaultDistro,
  parseDistroList,
  realRunInDistro,
  runningDistros,
  type ExecFile,
} from './wsl.js';

type Cb = (err: Error | null, stdout: string | Buffer, stderr: string | Buffer) => void;

/** A fake execFile that records its call and answers with the given result. */
function fakeExec(
  answer: { err?: Error & { code?: unknown; killed?: boolean }; stdout?: string | Buffer; stderr?: string } = {},
): { exec: ExecFile; calls: { file: string; args: readonly string[]; opts: Record<string, unknown> }[] } {
  const calls: { file: string; args: readonly string[]; opts: Record<string, unknown> }[] = [];
  const exec: ExecFile = (file, args, opts, cb) => {
    calls.push({ file, args, opts: opts as unknown as Record<string, unknown> });
    (cb as Cb)(answer.err ?? null, answer.stdout ?? '', answer.stderr ?? '');
  };
  return { exec, calls };
}

function execError(props: { code?: unknown; killed?: boolean }): Error & { code?: unknown; killed?: boolean } {
  return Object.assign(new Error('boom'), props);
}

describe('parseDistroList', () => {
  it('returns distro names, stripping CR / blanks / the default marker', () => {
    // wsl.exe -l -q output, already decoded from UTF-16 to a string
    const raw = 'Ubuntu\r\nUbuntu-22.04\r\ndocker-desktop\r\n\r\n';
    expect(parseDistroList(raw)).toEqual([
      'Ubuntu',
      'Ubuntu-22.04',
      'docker-desktop',
    ]);
  });
  it('handles an empty listing', () => {
    expect(parseDistroList('\r\n')).toEqual([]);
  });
});

describe('realRunInDistro', () => {
  it('test_run_in_distro_builds_argv_without_a_shell', async () => {
    const { exec, calls } = fakeExec({ stdout: 'ok' });
    const run = realRunInDistro('Ubuntu-24.04', exec);
    const result = await run(['/x/crr', 'service', 'status', '--json']);

    expect(calls).toHaveLength(1);
    expect(calls[0].file).toBe('wsl.exe');
    expect(calls[0].args).toEqual([
      '-d',
      'Ubuntu-24.04',
      '-e',
      '/x/crr',
      'service',
      'status',
      '--json',
    ]);
    expect('shell' in calls[0].opts).toBe(false);
    expect(calls[0].opts.windowsHide).toBe(true);
    expect(calls[0].opts.encoding).toBe('utf8');
    expect(typeof calls[0].opts.timeout).toBe('number');
    expect(calls[0].opts.timeout).toBeGreaterThan(0);
    expect(result).toEqual({ code: 0, stdout: 'ok', stderr: '' });
  });

  it('test_run_in_distro_never_rejects', async () => {
    const enoent = fakeExec({ err: execError({ code: 'ENOENT' }) });
    await expect(realRunInDistro('Ubuntu', enoent.exec)(['crr'])).resolves.toEqual({
      code: -1,
      stdout: '',
      stderr: 'wsl.exe not found',
    });

    const timeout = fakeExec({
      err: execError({ killed: true }),
      stdout: 'partial',
    });
    await expect(realRunInDistro('Ubuntu', timeout.exec)(['crr'])).resolves.toEqual({
      code: -1,
      stdout: 'partial',
      stderr: 'timed out',
    });
  });

  it('maps a numeric exit code through, and any other error to 1', async () => {
    const numeric = fakeExec({ err: execError({ code: 3 }), stderr: 'bad' });
    await expect(realRunInDistro('Ubuntu', numeric.exec)(['crr'])).resolves.toEqual({
      code: 3,
      stdout: '',
      stderr: 'bad',
    });

    const other = fakeExec({ err: new Error('odd') });
    await expect(realRunInDistro('Ubuntu', other.exec)(['crr'])).resolves.toMatchObject({
      code: 1,
    });
  });
});

describe('decodeWslOutput', () => {
  it('test_decode_wsl_output', () => {
    expect(decodeWslOutput(Buffer.from('Ubuntu\r\n', 'utf16le'))).toBe('Ubuntu\r\n');
    expect(decodeWslOutput(Buffer.from('Ubuntu\r\n', 'utf8'))).toBe('Ubuntu\r\n');
  });
});

describe('parseDefaultDistro', () => {
  it('test_parse_default_distro', () => {
    const raw =
      'Windows Subsystem for Linux Distributions:\r\nUbuntu-24.04 (Default)\r\ndocker-desktop\r\n';
    expect(parseDefaultDistro(raw)).toBe('Ubuntu-24.04');
  });

  it('returns undefined when no line is marked default', () => {
    expect(parseDefaultDistro('Ubuntu\r\ndocker-desktop\r\n')).toBeUndefined();
  });
});

describe('listDistros and defaultDistro', () => {
  it('lists distros from a UTF-16LE listing with the -l -q arguments', async () => {
    const { exec, calls } = fakeExec({
      stdout: Buffer.from('Ubuntu\r\ndocker-desktop\r\n', 'utf16le'),
    });
    await expect(listDistros(exec)).resolves.toEqual(['Ubuntu', 'docker-desktop']);
    expect(calls[0].file).toBe('wsl.exe');
    expect(calls[0].args).toEqual(['-l', '-q']);
    expect(calls[0].opts.encoding).toBe('buffer');
    expect('shell' in calls[0].opts).toBe(false);
    expect(calls[0].opts.windowsHide).toBe(true);
    expect(calls[0].opts.windowsHide).toBe(true);
  });

  it('reads the default distro with -l', async () => {
    const { exec, calls } = fakeExec({
      stdout: Buffer.from(
        'Windows Subsystem for Linux Distributions:\r\nUbuntu-24.04 (Default)\r\n',
        'utf16le',
      ),
    });
    await expect(defaultDistro(exec)).resolves.toBe('Ubuntu-24.04');
    expect(calls[0].args).toEqual(['-l']);
    expect(calls[0].opts.windowsHide).toBe(true);
  });
});

describe('runningDistros', () => {
  it('lists running distros with -l --running -q', async () => {
    const { exec, calls } = fakeExec({
      stdout: Buffer.from('Ubuntu-24.04\r\n', 'utf16le'),
    });
    await expect(runningDistros(exec)).resolves.toEqual(['Ubuntu-24.04']);
    expect(calls[0].args).toEqual(['-l', '--running', '-q']);
    expect(calls[0].opts.windowsHide).toBe(true);
  });

  it('test_running_distros_empty_on_nonzero_exit', async () => {
    // "There are no running distributions" exits non-zero.
    const { exec } = fakeExec({
      err: execError({ code: 1 }),
      stdout: Buffer.from('There are no running distributions.\r\n', 'utf16le'),
    });
    await expect(runningDistros(exec)).resolves.toEqual([]);
  });

  it('test_running_distros_undefined_without_wsl', async () => {
    const { exec } = fakeExec({ err: execError({ code: 'ENOENT' }) });
    await expect(runningDistros(exec)).resolves.toBeUndefined();
  });
});
