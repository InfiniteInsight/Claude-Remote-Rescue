import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { CRR_PATH_SCRIPT, CrrClient, resolveCrrPath } from './crrClient';
import { realRunInDistro, type CommandResult, type RunArgv } from './wsl';

const PATH = '/home/u/.local/state/crr/app/bin/crr';
const fixture = (name: string): string =>
  readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), 'utf8');
const ok = (stdout: string): CommandResult => ({ code: 0, stdout, stderr: '' });

function fake(reply: (argv: readonly string[]) => CommandResult) {
  const calls: string[][] = [];
  const run: RunArgv = async (argv) => {
    calls.push([...argv]);
    return reply(argv);
  };
  return { run, calls };
}

describe('CrrClient', () => {
  it('test_client_argv', async () => {
    const { run, calls } = fake((argv) =>
      ok(
        argv.includes('--json')
          ? fixture(
              argv[1] === 'service' ? 'service' : argv[1] === 'qr' ? 'qr' : 'status',
            )
          : 'done\n',
      ),
    );
    const c = new CrrClient(run, PATH);
    await c.serviceStatus();
    await c.sessionCounts();
    await c.qr();
    await c.start();
    await c.stop();
    await c.repair();
    expect(calls).toEqual([
      [PATH, 'service', 'status', '--json'],
      [PATH, 'status', '--json'],
      [PATH, 'qr', '--json'],
      [PATH, 'service', 'start'],
      [PATH, 'service', 'stop'],
      [PATH, 'systemd', '--install'],
    ]);
  });

  it('returns parsed values on success', async () => {
    const c = new CrrClient(async () => ok(fixture('service')), PATH);
    const r = await c.serviceStatus();
    expect(r.ok && r.value.state).toBe('running');
  });

  it('start, stop and repair return the trimmed stdout on exit 0', async () => {
    const c = new CrrClient(async () => ok('crr services started\n'), PATH);
    expect(await c.start()).toEqual({ ok: true, value: 'crr services started' });
    expect(await c.repair()).toEqual({ ok: true, value: 'crr services started' });
  });

  it('test_unknown_contract_version_is_a_mismatch_not_a_crash', async () => {
    const o = JSON.parse(fixture('service'));
    o.contract = 2;
    const c = new CrrClient(async () => ok(JSON.stringify(o)), PATH);
    const r = await c.serviceStatus();
    expect(r).toMatchObject({ ok: false, mismatch: true });
  });

  it('a too-old status contract is a mismatch', async () => {
    const o = JSON.parse(fixture('status'));
    o.contract = 3;
    const c = new CrrClient(async () => ok(JSON.stringify(o)), PATH);
    expect(await c.sessionCounts()).toMatchObject({ ok: false, mismatch: true });
  });

  it('test_non_json_stdout_is_an_error', async () => {
    const c = new CrrClient(async () => ok('Traceback (most recent call last)'), PATH);
    for (const call of [() => c.serviceStatus(), () => c.sessionCounts(), () => c.qr()]) {
      const r = await call();
      expect(r).toMatchObject({ ok: false, mismatch: false });
    }
  });

  it('test_stop_refusal_surfaces_crrs_message', async () => {
    const msg = 'crr: the installed unit is old; run crr systemd --install first';
    const c = new CrrClient(
      async () => ({ code: 1, stdout: '', stderr: `warning: x\n${msg}\n` }),
      PATH,
    );
    expect(await c.stop()).toEqual({ ok: false, error: msg, exit: 1, mismatch: false });
  });

  it('falls back to stdout when stderr is empty', async () => {
    const c = new CrrClient(
      async () => ({ code: 1, stdout: 'not running\n', stderr: '' }),
      PATH,
    );
    expect(await c.start()).toMatchObject({ ok: false, error: 'not running', exit: 1 });
  });

  it('test_old_crr_is_a_mismatch', async () => {
    const c = new CrrClient(
      async () => ({
        code: 2,
        stdout: '',
        stderr: "usage: crr ...\ncrr: error: argument cmd: invalid choice: 'service'",
      }),
      PATH,
    );
    expect(await c.serviceStatus()).toMatchObject({ ok: false, mismatch: true, exit: 2 });
    expect(await c.start()).toMatchObject({ ok: false, mismatch: true });
    const u = new CrrClient(
      async () => ({ code: 2, stdout: '', stderr: 'crr: error: unrecognized arguments: --json' }),
      PATH,
    );
    expect(await u.qr()).toMatchObject({ ok: false, mismatch: true });
  });

  it('other failures are not mismatches', async () => {
    const other = new CrrClient(
      async () => ({ code: 2, stdout: '', stderr: 'something else' }),
      PATH,
    );
    expect(await other.qr()).toMatchObject({ ok: false, mismatch: false });
    const noWsl = new CrrClient(
      async () => ({ code: -1, stdout: '', stderr: 'spawn wsl.exe ENOENT' }),
      PATH,
    );
    expect(await noWsl.serviceStatus()).toMatchObject({
      ok: false,
      mismatch: false,
      exit: -1,
    });
    expect(await noWsl.stop()).toMatchObject({ ok: false, mismatch: false, exit: -1 });
  });

  it('a run that throws is an error, not a crash', async () => {
    const c = new CrrClient(async () => {
      throw new Error('boom');
    }, PATH);
    expect(await c.serviceStatus()).toMatchObject({ ok: false, mismatch: false });
  });
});

describe('resolveCrrPath', () => {
  it('test_resolve_crr_path', async () => {
    const good = fake((argv) =>
      argv[0] === 'sh' ? ok(PATH) : ok('crr 0.9.0\n'),
    );
    expect(await resolveCrrPath(good.run)).toEqual({ ok: true, path: PATH });
    expect(good.calls).toEqual([
      ['sh', '-c', CRR_PATH_SCRIPT],
      [PATH, '--version'],
    ]);

    const bad = fake((argv) =>
      argv[0] === 'sh' ? ok(PATH) : { code: 127, stdout: '', stderr: 'no such file' },
    );
    const r = await resolveCrrPath(bad.run);
    expect(r.ok).toBe(false);
    expect(!r.ok && r.error).toContain(PATH);
  });

  it('rejects a version output that does not start with "crr "', async () => {
    const run = fake((argv) => (argv[0] === 'sh' ? ok(PATH) : ok('python 3.12\n'))).run;
    const r = await resolveCrrPath(run);
    expect(r.ok).toBe(false);
    expect(!r.ok && r.error).toContain(PATH);
  });

  it('fails cleanly when the path script fails or prints nothing', async () => {
    const a = await resolveCrrPath(async () => ({ code: -1, stdout: '', stderr: 'timed out' }));
    expect(a.ok).toBe(false);
    const b = await resolveCrrPath(async () => ok(''));
    expect(b.ok).toBe(false);
  });
});

describe("wsl.exe's own errors, through the real runner", () => {
  // What `wsl.exe -d NoSuchDistro -e true` prints on this host: exit 255,
  // UTF-16LE, on stdout, with nothing on stderr.
  const notFound = (): RunArgv =>
    realRunInDistro('NoSuchDistro', (_file, _args, _opts, cb) => {
      cb(
        Object.assign(new Error('Command failed'), { code: 255 }),
        Buffer.from(
          'There is no distribution with the supplied name.\r\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n',
          'utf16le',
        ),
        Buffer.alloc(0),
      );
    });

  // test_wsl_error_reaches_the_status_line
  it('a crr call reports the readable reason, falling back to stdout', async () => {
    const r = await new CrrClient(notFound(), PATH).serviceStatus();
    expect(r).toEqual({
      ok: false,
      error: 'Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND',
      exit: 255,
      mismatch: false,
    });
  });

  it('resolving the crr path reports it too', async () => {
    const r = await resolveCrrPath(notFound());
    expect(r).toEqual({
      ok: false,
      error: 'could not locate crr: Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND',
    });
  });

  it('a failure with nothing readable on either stream names the exit code', async () => {
    const c = new CrrClient(async () => ({ code: 255, stdout: '\u0000', stderr: '' }), PATH);
    const r = await c.serviceStatus();
    expect(!r.ok && r.error).toBe('exit 255');
  });
});

describe('CRR_PATH_SCRIPT', () => {
  it('is the fixed script', () => {
    expect(CRR_PATH_SCRIPT).toBe(
      'printf "%s" "${XDG_STATE_HOME:-$HOME/.local/state}/crr/app/bin/crr"',
    );
  });
});
