import type { CrrResult } from './crrClient.js';
import type { PollState } from './poll.js';

/**
 * Pure decisions behind main.ts's never-wake gates, its sleep bookkeeping and
 * its recovery from a moved crr, kept out of main.ts so they can be tested
 * without Electron.
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
 * Sleep bookkeeping for main.ts, counted rather than timed. Every suspend and
 * resume bumps an epoch, and each poll records the epoch it began under. A
 * clock comparison went wrong when the clock stepped backwards after a resume
 * (an NTP correction on wake): every poll then looked older than the resume,
 * was dropped and re-polled at once, back to back. An epoch only goes up.
 *
 * `suspended` is set by a suspend and stays set until a poll that began at or
 * after the resume lands: nothing may act on what the app knew before the
 * machine slept, since WSL may have shut down meanwhile.
 */
export class SleepTracker {
  private epoch = 0;
  /** The epoch of the resume we are waiting on a fresh poll for. */
  private resumeEpoch: number | undefined;
  /** The epoch the latest poll began under. */
  private pollEpoch = 0;
  private asleep = false;

  /** Asleep, or awake with no poll begun since the resume having landed yet. */
  get suspended(): boolean {
    return this.asleep;
  }

  /** The machine is going to sleep. */
  suspend(): void {
    this.epoch += 1;
    this.asleep = true;
    this.resumeEpoch = undefined;
  }

  /** The machine woke. */
  resume(): void {
    this.epoch += 1;
    if (this.asleep) this.resumeEpoch = this.epoch;
  }

  /**
   * The user did something explicit (Refresh, Start, Stop, Repair, chose a
   * distro, opened the window). Nobody acts on a sleeping machine, so if we
   * still think it is asleep, Windows' resume was missed and the app would
   * stay frozen: count this as the resume. A poll already under way is still
   * not trusted; the next one ends the suspension. Returns whether it counted,
   * so the caller restarts the paused poller.
   */
  userActed(): boolean {
    if (!this.asleep) return false;
    this.resume();
    return true;
  }

  /** A poll is beginning (its first step, the running-distros check). */
  pollStarted(): void {
    this.pollEpoch = this.epoch;
  }

  /**
   * A poll landed. 'use' it (and stop being suspended, if we were); 'drop' it
   * while still asleep; or, for a poll that began before the resume, drop it
   * and 'repoll'. A poll that begins after the resume is always used, so this
   * asks for at most one repoll per resume.
   */
  pollLanded(): 'use' | 'drop' | 'repoll' {
    if (!this.asleep) return 'use';
    if (this.resumeEpoch === undefined) return 'drop';
    if (this.pollEpoch < this.resumeEpoch) return 'repoll';
    this.asleep = false;
    this.resumeEpoch = undefined;
    return 'use';
  }

  /** A mark that every poll beginning after this call reaches (see `polledSince`). */
  mark(): number {
    this.epoch += 1;
    return this.epoch;
  }

  /** Did the latest poll begin after `mark` was taken? */
  polledSince(mark: number): boolean {
    return this.pollEpoch >= mark;
  }
}

/**
 * The URL to hand `shell.openExternal`, or undefined to refuse. Only http and
 * https: the shell would pass any other scheme (`file:`, `ms-settings:`,
 * `search-ms:`, a UNC path) to whatever local handler claims it. Every
 * `shell.openExternal` in the app goes through this, whatever already checked
 * the URL upstream, and gets the re-serialized form, not the raw string.
 */
export function safeExternalUrl(raw: string): string | undefined {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return undefined;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined;
  return url.href;
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
