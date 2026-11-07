import type { CrrResult } from './crrClient.js';
import type { PollState } from './poll.js';

/**
 * Pure decisions behind main.ts's never-wake gates and its recovery from a
 * moved crr, kept out of main.ts so they can be tested without Electron.
 */

/** Whether the dashboard unit is active in this poll state. */
export function dashboardActive(state: PollState | undefined): boolean {
  return state?.service?.services.some((u) => u.name === 'dashboard' && u.active) ?? false;
}

/**
 * The part of `qrGate` that needs no `wsl.exe` call: the latest poll says the
 * distro is running with the dashboard up, and the machine is not asleep or
 * just waking (before a fresh poll has landed). Checked first, so a phone-link
 * refresh that cannot pass never even lists the running distros.
 */
export function qrStateAllows(state: PollState | undefined, suspended: boolean): boolean {
  return state !== undefined && state.wsl === 'running' && dashboardActive(state) && !suspended;
}

export interface QrGateInput {
  /** `wsl.exe -l --running -q`, read just before the call; undefined when wsl.exe could not run. */
  running: string[] | undefined;
  distro: string | undefined;
  /** crr's hold, read just before the call. */
  held: boolean;
  /** The latest poll state, which can be several seconds old. */
  state: PollState | undefined;
  suspended: boolean;
}

/**
 * May the phone-link refresh run `crr qr`? That is a `wsl.exe -d` call, and the
 * latest poll can be several seconds old: a `wsl --shutdown` since then would
 * be undone by it. So besides what the poll said, a fresh listing taken just
 * before the call must show the distro running and a live read must find no
 * hold. Listing running distros and reading the hold never boot anything.
 */
export function qrGate(input: QrGateInput): boolean {
  const { running, distro, held, state, suspended } = input;
  return (
    distro !== undefined &&
    running !== undefined &&
    running.includes(distro) &&
    !held &&
    qrStateAllows(state, suspended)
  );
}

/**
 * Whether a poll that just finished reflects the world after a sleep. Outside a
 * suspend every state counts. During one, a state counts only when the machine
 * has resumed and the poll began at or after the resume: a poll that was already
 * under way when the machine slept (and `resume()` merely joined) is stale.
 */
export function stateIsFresh(
  suspended: boolean,
  resumedAtMs: number | undefined,
  pollStartedAtMs: number,
): boolean {
  if (!suspended) return true;
  return resumedAtMs !== undefined && pollStartedAtMs >= resumedAtMs;
}

/**
 * Should a failed `service status` make the app forget its cached crr path?
 * When the launcher has moved or gone, the cached path fails with a not-found
 * error forever; forgetting it lets the next gated poll resolve it again. A
 * version mismatch is a different problem (crr runs, it just disagrees), and the
 * path is fine.
 */
export function shouldForgetCrrPath(result: CrrResult<unknown>): boolean {
  if (result.ok || result.mismatch) return false;
  return /no such file or directory|not found/i.test(result.error);
}
