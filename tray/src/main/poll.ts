import type { ServiceStatus, SessionCounts } from './contracts';
import type { CrrClient, CrrResult } from './crrClient';

/**
 * The poller turns crr's answers into a tray state, and never wakes WSL.
 *
 * Every `wsl.exe -d <distro>` call boots that distro if it is stopped. So a poll
 * first checks, without starting anything, that the distro is running
 * (`wsl.exe -l --running -q`) and that crr's holder hold is not in place. If
 * either check fails it makes no `-d` call at all, and `client()` (the only
 * place the crr path is resolved, which is itself a `-d` call) is not invoked.
 */

/** Delay after a poll finishes (never an interval): one wsl.exe call is ~1.4 s. */
export const POLL_MS = 5_000;

export type TrayIcon = 'running' | 'attention' | 'stopped' | 'wsl-down';

export interface PollState {
  icon: TrayIcon;
  statusLine: string;
  service?: ServiceStatus;
  counts?: SessionCounts;
  error?: string;
  mismatch?: boolean;
  /** Whether later tasks may make `wsl.exe -d` calls: only on 'running'. */
  wsl: 'missing' | 'stopped' | 'held' | 'running';
}

export interface PollInput {
  running: string[] | undefined;
  distro: string | undefined;
  held: boolean;
  clientError?: string;
  service?: CrrResult<ServiceStatus>;
  sessions?: CrrResult<SessionCounts>;
}

/** The units whose state decides the icon. Keep-awake is deliberately absent. */
const ESSENTIAL_UNITS = ['watchdog', 'dashboard'] as const;

function isActive(service: ServiceStatus, name: string): boolean {
  return service.services.some((u) => u.name === name && u.active);
}

/** Rules 6-8 passed: the service is installed, supported and up. */
function isFullyRunning(service: ServiceStatus): boolean {
  return (
    service.backend !== 'unsupported' &&
    service.state !== 'not-installed' &&
    ESSENTIAL_UNITS.every((name) => isActive(service, name))
  );
}

export function reduce(input: PollInput): PollState {
  // 1-3: the never-wake gates.
  if (input.running === undefined) {
    return { icon: 'wsl-down', statusLine: 'WSL is not available', wsl: 'missing' };
  }
  if (input.distro === undefined || !input.running.includes(input.distro)) {
    return { icon: 'wsl-down', statusLine: 'WSL is stopped', wsl: 'stopped' };
  }
  if (input.held) {
    return {
      icon: 'wsl-down',
      statusLine: 'crr holder is holding WSL down — not checking crr',
      wsl: 'held',
    };
  }

  // 4: no usable client.
  if (input.clientError !== undefined) {
    return {
      icon: 'stopped',
      statusLine: `crr not found in ${input.distro}: ${input.clientError}`,
      error: input.clientError,
      wsl: 'running',
    };
  }

  // 5: the service call itself failed.
  const result = input.service;
  if (result === undefined || !result.ok) {
    const error = result === undefined ? 'no answer' : result.error;
    const mismatch = result !== undefined && result.mismatch;
    return {
      icon: 'stopped',
      statusLine: mismatch
        ? 'crr version mismatch — update crr or the tray app'
        : `CRR unreachable: ${error}`,
      error,
      mismatch,
      wsl: 'running',
    };
  }

  const service = result.value;
  // 6: no service manager to run crr under.
  if (service.backend === 'unsupported') {
    return { icon: 'stopped', statusLine: service.reason, service, wsl: 'running' };
  }
  // 7
  if (service.state === 'not-installed') {
    return { icon: 'stopped', statusLine: 'CRR is not installed', service, wsl: 'running' };
  }
  // 8: a missing unit counts as inactive.
  if (!ESSENTIAL_UNITS.every((name) => isActive(service, name))) {
    return { icon: 'stopped', statusLine: 'CRR stopped', service, wsl: 'running' };
  }

  // 9: running.
  const counts = input.sessions !== undefined && input.sessions.ok ? input.sessions.value : undefined;
  if (counts === undefined) {
    return { icon: 'running', statusLine: 'CRR running · sessions unknown', service, wsl: 'running' };
  }
  return {
    icon: counts.attention > 0 ? 'attention' : 'running',
    statusLine: `CRR running · ${counts.total} sessions, ${counts.crashed} crashed`,
    service,
    counts,
    wsl: 'running',
  };
}

export interface PollerDeps {
  runningDistros(): Promise<string[] | undefined>;
  distro(): string | undefined;
  held(): boolean;
  /**
   * The only place the crr path gets resolved. The poller calls it only after
   * the running and hold checks pass, so resolving can never boot WSL.
   */
  client(): Promise<CrrClient | { error: string }>;
  onState(s: PollState): void;
  schedule(fn: () => void, ms: number): () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * The second look before the sessions call. The poll's own gate was taken
 * at its start, a `wsl.exe` call or two earlier, and in that time a hold may
 * have landed or the machine may have gone to sleep (`pause()`). A hold is
 * reported as one; a poll that began before a pause skips the call and
 * reports the service with the sessions unknown.
 */
export function sessionsRecheck(input: {
  held: boolean;
  /** The pause epoch the poll began under. */
  startedEpoch: number;
  /** The pause epoch now. */
  epoch: number;
}): 'ask' | 'held' | 'skip' {
  if (input.held) return 'held';
  if (input.epoch !== input.startedEpoch) return 'skip';
  return 'ask';
}

export class Poller {
  private inFlight: Promise<PollState> | undefined;
  private cancelNext: (() => void) | undefined;
  private started = false;
  private paused = false;
  /** Bumped by every `pause()`, so a poll can tell it began before one. */
  private epoch = 0;

  constructor(private readonly deps: PollerDeps) {}

  /** Joins a poll already in flight instead of starting another. */
  pollNow(): Promise<PollState> {
    if (this.inFlight === undefined) {
      this.inFlight = this.run();
    }
    return this.inFlight;
  }

  start(): void {
    this.started = true;
    void this.pollNow();
  }

  /** Stop scheduling, and keep a poll already under way from making its sessions call. */
  pause(): void {
    this.paused = true;
    this.epoch += 1;
    this.clearNext();
  }

  /** Poll now, or join the poll already under way, and schedule again after it. */
  resume(): void {
    this.paused = false;
    void this.pollNow();
  }

  /** `held()`, reading a failure as a hold: when in doubt, do not touch WSL. */
  private heldSafely(): boolean {
    try {
      return this.deps.held();
    } catch {
      return true;
    }
  }

  private clearNext(): void {
    const cancel = this.cancelNext;
    this.cancelNext = undefined;
    cancel?.();
  }

  private async run(): Promise<PollState> {
    let state: PollState;
    try {
      state = await this.poll();
    } catch (e) {
      // poll() handles every dep failure itself; this is the last backstop.
      state = {
        icon: 'wsl-down',
        statusLine: 'WSL is not available',
        error: message(e),
        wsl: 'missing',
      };
    }
    this.inFlight = undefined;
    try {
      this.deps.onState(state);
    } catch {
      // A throwing listener must not stop polling.
    }
    if (this.started && !this.paused) {
      this.clearNext();
      this.cancelNext = this.deps.schedule(() => {
        this.cancelNext = undefined;
        void this.pollNow();
      }, POLL_MS);
    }
    return state;
  }

  private async poll(): Promise<PollState> {
    const startedEpoch = this.epoch;
    const distro = this.deps.distro();

    let running: string[] | undefined;
    try {
      running = await this.deps.runningDistros();
    } catch {
      running = undefined;
    }

    let held = false;
    if (running !== undefined && distro !== undefined && running.includes(distro)) {
      held = this.heldSafely();
    }

    const gate: PollInput = { running, distro, held };
    const gated = reduce(gate);
    if (gated.wsl !== 'running') return gated;

    let client: CrrClient | { error: string };
    try {
      client = await this.deps.client();
    } catch (e) {
      return reduce({ ...gate, clientError: message(e) });
    }
    if (!('serviceStatus' in client)) {
      return reduce({ ...gate, clientError: client.error });
    }

    let service: CrrResult<ServiceStatus>;
    try {
      service = await client.serviceStatus();
    } catch (e) {
      service = { ok: false, error: message(e), exit: -1, mismatch: false };
    }

    let sessions: CrrResult<SessionCounts> | undefined;
    if (service.ok && isFullyRunning(service.value)) {
      const next = sessionsRecheck({
        held: this.heldSafely(),
        startedEpoch,
        epoch: this.epoch,
      });
      if (next === 'held') return reduce({ ...gate, held: true });
      if (next === 'ask') {
        try {
          sessions = await client.sessionCounts();
        } catch (e) {
          sessions = { ok: false, error: message(e), exit: -1, mismatch: false };
        }
      }
    }
    return reduce({ ...gate, service, sessions });
  }
}
