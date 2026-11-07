import {
  ContractError,
  NotJsonError,
  parseQr,
  parseServiceStatus,
  parseSessionCounts,
  type QrInfo,
  type ServiceStatus,
  type SessionCounts,
} from './contracts';
import { summarizeError } from './summarizeError';
import type { CommandResult, RunArgv } from './wsl';

/**
 * Prints where crr's deployed launcher lives. A fixed constant, never
 * interpolated: it is the one `sh -c` the app runs, because `wsl.exe -e` has no
 * PATH and no shell to expand $HOME.
 */
export const CRR_PATH_SCRIPT =
  'printf "%s" "${XDG_STATE_HOME:-$HOME/.local/state}/crr/app/bin/crr"';

export type CrrResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string; exit: number; mismatch: boolean };

/** An older crr rejecting a subcommand or flag this app uses. */
function isUsageError(r: CommandResult): boolean {
  return (
    r.code === 2 &&
    (r.stderr.includes('invalid choice') ||
      r.stderr.includes('unrecognized arguments'))
  );
}

async function runSafely(run: RunArgv, argv: readonly string[]): Promise<CommandResult> {
  try {
    return await run(argv);
  } catch (e) {
    // A RunArgv should never reject; if one does, report it like a wsl.exe failure.
    return { code: -1, stdout: '', stderr: e instanceof Error ? e.message : String(e) };
  }
}

export async function resolveCrrPath(
  run: RunArgv,
): Promise<{ ok: true; path: string } | { ok: false; error: string }> {
  const found = await runSafely(run, ['sh', '-c', CRR_PATH_SCRIPT]);
  const path = found.stdout.trim();
  if (found.code !== 0 || path === '') {
    const why = summarizeError(found.stderr || found.stdout) || `exit ${found.code}`;
    return { ok: false, error: `could not locate crr: ${why}` };
  }
  const version = await runSafely(run, [path, '--version']);
  if (version.code !== 0 || !version.stdout.startsWith('crr ')) {
    const why =
      version.code !== 0
        ? summarizeError(version.stderr || version.stdout) || `exit ${version.code}`
        : 'unexpected --version output';
    return { ok: false, error: `crr is not runnable at ${path}: ${why}` };
  }
  return { ok: true, path };
}

export class CrrClient {
  constructor(
    private readonly run: RunArgv,
    private readonly crrPath: string,
  ) {}

  serviceStatus(): Promise<CrrResult<ServiceStatus>> {
    return this.json(['service', 'status', '--json'], parseServiceStatus);
  }

  sessionCounts(): Promise<CrrResult<SessionCounts>> {
    return this.json(['status', '--json'], parseSessionCounts);
  }

  qr(): Promise<CrrResult<QrInfo>> {
    return this.json(['qr', '--json'], parseQr);
  }

  start(): Promise<CrrResult<string>> {
    return this.message(['service', 'start']);
  }

  stop(): Promise<CrrResult<string>> {
    return this.message(['service', 'stop']);
  }

  /** Re-install the units; this is also what the refusal messages point to. */
  repair(): Promise<CrrResult<string>> {
    return this.message(['systemd', '--install']);
  }

  private failure(r: CommandResult): CrrResult<never> {
    return {
      ok: false,
      // wsl.exe's own errors arrive on stdout with stderr empty.
      error: summarizeError(r.stderr || r.stdout) || `exit ${r.code}`,
      exit: r.code,
      mismatch: isUsageError(r),
    };
  }

  private async json<T>(
    args: readonly string[],
    parse: (stdout: string) => T,
  ): Promise<CrrResult<T>> {
    const r = await runSafely(this.run, [this.crrPath, ...args]);
    if (r.code !== 0) return this.failure(r);
    try {
      return { ok: true, value: parse(r.stdout) };
    } catch (e) {
      // A parser only throws ContractError; anything else would be our own bug,
      // and still must not crash the poller.
      const message = e instanceof Error ? e.message : String(e);
      return {
        ok: false,
        error: summarizeError(message),
        exit: r.code,
        mismatch: e instanceof ContractError && !(e instanceof NotJsonError),
      };
    }
  }

  private async message(args: readonly string[]): Promise<CrrResult<string>> {
    const r = await runSafely(this.run, [this.crrPath, ...args]);
    if (r.code !== 0) return this.failure(r);
    return { ok: true, value: r.stdout.trim() };
  }
}
