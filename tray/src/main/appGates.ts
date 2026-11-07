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
 * May the phone-link refresh run? `crr qr` is a `wsl.exe -d` call, so it must
 * never act on a state that could be old: there has to be a state, it has to say
 * the distro is running with the dashboard up, no hold may be in place, and the
 * machine must not be asleep or just waking (before a fresh poll has landed).
 */
export function canRefreshQr(
  state: PollState | undefined,
  suspended: boolean,
  held: boolean,
): boolean {
  return (
    state !== undefined &&
    state.wsl === 'running' &&
    dashboardActive(state) &&
    !suspended &&
    !held
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
