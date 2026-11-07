import { describe, expect, it, vi } from 'vitest';
import type { ServiceStatus, SessionCounts, ServiceUnit } from './contracts';
import type { CrrClient, CrrResult } from './crrClient';
import {
  POLL_MS,
  Poller,
  reduce,
  sessionsRecheck,
  type PollerDeps,
  type PollInput,
  type PollState,
} from './poll';

const DISTRO = 'Ubuntu-24.04';

function unit(name: string, active: boolean): ServiceUnit {
  return { name, unit: `crr-${name}.service`, installed: true, enabled: true, active };
}

function service(over: Partial<ServiceStatus> = {}, units?: ServiceUnit[]): ServiceStatus {
  return {
    contract: 1,
    backend: 'systemd',
    reason: '',
    state: 'running',
    services: units ?? [unit('watchdog', true), unit('dashboard', true), unit('keep-awake', true)],
    dashboard_url: 'http://127.0.0.1:8765',
    dashboard_login: true,
    ...over,
  };
}

function counts(over: Partial<SessionCounts> = {}): SessionCounts {
  return { total: 3, live: 2, parked: 0, crashed: 1, ghost: 0, attention: 0, ...over };
}

const ok = <T>(value: T): CrrResult<T> => ({ ok: true, value });
const fail = (error: string, mismatch = false): CrrResult<never> => ({
  ok: false, error, exit: 1, mismatch,
});

const base: PollInput = { running: [DISTRO], distro: DISTRO, held: false };

describe('reduce', () => {
  // test_reduce_rules
  const rows: Array<[string, PollInput, string, string]> = [
    ['1 wsl.exe unavailable', { ...base, running: undefined }, 'wsl-down', 'WSL is not available'],
    ['2 no distro chosen', { ...base, distro: undefined }, 'wsl-down', 'WSL is stopped'],
    ['2 distro not running', { ...base, running: ['docker-desktop'] }, 'wsl-down', 'WSL is stopped'],
    ['3 held', { ...base, held: true }, 'wsl-down', 'crr holder is holding WSL down — not checking crr'],
    ['4 client error', { ...base, clientError: 'no crr' }, 'stopped', `crr not found in ${DISTRO}: no crr`],
    ['5 service call failed', { ...base, service: fail('boom') }, 'stopped', 'CRR unreachable: boom'],
    [
      '5 service mismatch',
      { ...base, service: fail('contract 2', true) },
      'stopped',
      'crr version mismatch — update crr or the tray app',
    ],
    [
      '6 backend unsupported',
      { ...base, service: ok(service({ backend: 'unsupported', reason: 'no systemd here', state: 'stopped' })) },
      'stopped',
      'no systemd here',
    ],
    [
      '7 not installed',
      { ...base, service: ok(service({ state: 'not-installed' })) },
      'stopped',
      'CRR is not installed',
    ],
    [
      '8 watchdog inactive',
      { ...base, service: ok(service({}, [unit('watchdog', false), unit('dashboard', true)])) },
      'stopped',
      'CRR stopped',
    ],
    [
      '8 dashboard inactive',
      { ...base, service: ok(service({}, [unit('watchdog', true), unit('dashboard', false)])) },
      'stopped',
      'CRR stopped',
    ],
    [
      '8 dashboard missing counts as inactive',
      { ...base, service: ok(service({}, [unit('watchdog', true)])) },
      'stopped',
      'CRR stopped',
    ],
    [
      '8 only keep-awake inactive is still running',
      {
        ...base,
        service: ok(service({}, [unit('watchdog', true), unit('dashboard', true), unit('keep-awake', false)])),
        sessions: ok(counts()),
      },
      'running',
      'CRR running · 3 sessions, 1 crashed',
    ],
    [
      '9 running, nothing needs attention',
      { ...base, service: ok(service()), sessions: ok(counts({ total: 0, crashed: 0 })) },
      'running',
      'CRR running · 0 sessions, 0 crashed',
    ],
    [
      '9 running, attention',
      { ...base, service: ok(service()), sessions: ok(counts({ attention: 2 })) },
      'attention',
      'CRR running · 3 sessions, 1 crashed',
    ],
    [
      '9 running, sessions call failed',
      { ...base, service: ok(service()), sessions: fail('bad status') },
      'running',
      'CRR running · sessions unknown',
    ],
    [
      '9 running, sessions not asked',
      { ...base, service: ok(service()) },
      'running',
      'CRR running · sessions unknown',
    ],
  ];

  it.each(rows)('test_reduce_rules: %s', (_name, input, icon, statusLine) => {
    const s = reduce(input);
    expect(s.icon).toBe(icon);
    expect(s.statusLine).toBe(statusLine);
  });

  it('reports the wsl gate for each rule', () => {
    expect(reduce({ ...base, running: undefined }).wsl).toBe('missing');
    expect(reduce({ ...base, running: [] }).wsl).toBe('stopped');
    expect(reduce({ ...base, held: true }).wsl).toBe('held');
    expect(reduce({ ...base, clientError: 'x' }).wsl).toBe('running');
    expect(reduce({ ...base, service: fail('x') }).wsl).toBe('running');
    expect(reduce({ ...base, service: ok(service()), sessions: ok(counts()) }).wsl).toBe('running');
  });

  it('carries error and mismatch from a failed service call', () => {
    const s = reduce({ ...base, service: fail('contract 2', true) });
    expect(s.error).toBe('contract 2');
    expect(s.mismatch).toBe(true);
  });
});

interface Fakes {
  deps: PollerDeps;
  states: PollState[];
  clientCalls: () => number;
  serviceCalls: () => number;
  sessionCalls: () => number;
  runningCalls: () => number;
  scheduled: Array<{ fn: () => void; ms: number; cancel: ReturnType<typeof vi.fn> }>;
}

function fakes(opts: {
  running?: string[] | undefined;
  held?: boolean;
  heldFn?: () => boolean;
  client?: CrrClient | { error: string };
  serviceStatus?: () => Promise<CrrResult<ServiceStatus>>;
  sessionCounts?: () => Promise<CrrResult<SessionCounts>>;
  runningDistros?: () => Promise<string[] | undefined>;
  clientFn?: () => Promise<CrrClient | { error: string }>;
} = {}): Fakes {
  let clientCalls = 0;
  let serviceCalls = 0;
  let sessionCalls = 0;
  let runningCalls = 0;
  const states: PollState[] = [];
  const scheduled: Fakes['scheduled'] = [];
  const crr = {
    serviceStatus: () => {
      serviceCalls += 1;
      return (opts.serviceStatus ?? (async () => ok(service())))();
    },
    sessionCounts: () => {
      sessionCalls += 1;
      return (opts.sessionCounts ?? (async () => ok(counts())))();
    },
  } as unknown as CrrClient;
  const listRunning = opts.runningDistros ?? (async () => ('running' in opts ? opts.running : [DISTRO]));
  const deps: PollerDeps = {
    runningDistros: () => {
      runningCalls += 1;
      return listRunning();
    },
    distro: () => DISTRO,
    held: opts.heldFn ?? (() => opts.held ?? false),
    client:
      opts.clientFn ??
      (async () => {
        clientCalls += 1;
        return opts.client ?? crr;
      }),
    onState: (s) => states.push(s),
    schedule: (fn, ms) => {
      const cancel = vi.fn();
      scheduled.push({ fn, ms, cancel });
      return cancel;
    },
  };
  return {
    deps, states, scheduled,
    clientCalls: () => clientCalls,
    serviceCalls: () => serviceCalls,
    sessionCalls: () => sessionCalls,
    runningCalls: () => runningCalls,
  };
}

describe('sessionsRecheck', () => {
  // test_sessions_recheck
  it('asks only when no hold has landed and the poller has not paused since the poll began', () => {
    expect(sessionsRecheck({ held: false, startedEpoch: 3, epoch: 3 })).toBe('ask');
  });

  it('reports the hold when one has landed, whether or not the poller paused', () => {
    expect(sessionsRecheck({ held: true, startedEpoch: 3, epoch: 3 })).toBe('held');
    expect(sessionsRecheck({ held: true, startedEpoch: 3, epoch: 4 })).toBe('held');
  });

  it('skips the call for a poll that began before a pause', () => {
    expect(sessionsRecheck({ held: false, startedEpoch: 3, epoch: 4 })).toBe('skip');
  });
});

describe('Poller', () => {
  it('test_no_distro_call_while_the_distro_is_stopped', async () => {
    const f = fakes({ running: ['docker-desktop'] });
    const s = await new Poller(f.deps).pollNow();
    expect(s.wsl).toBe('stopped');
    expect(f.clientCalls()).toBe(0);
    expect(f.serviceCalls()).toBe(0);
    expect(f.sessionCalls()).toBe(0);
  });

  it('test_no_distro_call_while_a_hold_is_in_place', async () => {
    const f = fakes({ held: true });
    const s = await new Poller(f.deps).pollNow();
    expect(s.wsl).toBe('held');
    expect(f.clientCalls()).toBe(0);
    expect(f.serviceCalls()).toBe(0);
    expect(f.sessionCalls()).toBe(0);
  });

  it('makes no distro call when wsl.exe is missing', async () => {
    const f = fakes({ running: undefined });
    const s = await new Poller(f.deps).pollNow();
    expect(s.wsl).toBe('missing');
    expect(f.clientCalls()).toBe(0);
  });

  it('test_client_error_is_reported', async () => {
    const f = fakes({ client: { error: 'no crr' } });
    const s = await new Poller(f.deps).pollNow();
    expect(s.icon).toBe('stopped');
    expect(s.statusLine).toBe(`crr not found in ${DISTRO}: no crr`);
    expect(f.serviceCalls()).toBe(0);
  });

  it('asks for sessions only when the service is running', async () => {
    const stopped = fakes({
      serviceStatus: async () => ok(service({}, [unit('watchdog', false), unit('dashboard', true)])),
    });
    await new Poller(stopped.deps).pollNow();
    expect(stopped.sessionCalls()).toBe(0);

    const running = fakes();
    const s = await new Poller(running.deps).pollNow();
    expect(running.sessionCalls()).toBe(1);
    expect(s.counts).toEqual(counts());
    expect(s.service).toEqual(service());
  });

  it('keeps running with unknown sessions when the sessions call fails', async () => {
    const f = fakes({ sessionCounts: async () => fail('nope') });
    const s = await new Poller(f.deps).pollNow();
    expect(s.icon).toBe('running');
    expect(s.statusLine).toBe('CRR running · sessions unknown');
    expect(s.counts).toBeUndefined();
  });

  it('test_polls_never_overlap', async () => {
    let release!: (r: CrrResult<ServiceStatus>) => void;
    const pending = new Promise<CrrResult<ServiceStatus>>((res) => { release = res; });
    const f = fakes({ serviceStatus: () => pending });
    const poller = new Poller(f.deps);
    const a = poller.pollNow();
    const b = poller.pollNow();
    expect(b).toBe(a);
    release(ok(service()));
    await a;
    expect(f.serviceCalls()).toBe(1);
    // After it finishes, a new poll is a new poll.
    const c = poller.pollNow();
    expect(c).not.toBe(a);
    await c;
    expect(f.serviceCalls()).toBe(2);
  });

  it('calls onState for every completed poll', async () => {
    const f = fakes();
    const poller = new Poller(f.deps);
    await poller.pollNow();
    await poller.pollNow();
    expect(f.states).toHaveLength(2);
  });

  it('maps a rejecting runningDistros to the wsl-missing state', async () => {
    const f = fakes({ runningDistros: async () => { throw new Error('spawn'); } });
    const s = await new Poller(f.deps).pollNow();
    expect(s.icon).toBe('wsl-down');
    expect(s.wsl).toBe('missing');
    expect(f.states).toHaveLength(1);
  });

  it('maps a rejecting client() to the client-error state', async () => {
    const f = fakes({ clientFn: async () => { throw new Error('resolve blew up'); } });
    const s = await new Poller(f.deps).pollNow();
    expect(s.statusLine).toBe(`crr not found in ${DISTRO}: resolve blew up`);
  });

  it('maps a rejecting serviceStatus to unreachable', async () => {
    const f = fakes({ serviceStatus: async () => { throw new Error('kaput'); } });
    const s = await new Poller(f.deps).pollNow();
    expect(s.icon).toBe('stopped');
    expect(s.statusLine).toBe('CRR unreachable: kaput');
  });

  it('schedules the next poll POLL_MS after a poll finishes', async () => {
    const f = fakes();
    const poller = new Poller(f.deps);
    poller.start();
    await vi.waitFor(() => expect(f.scheduled).toHaveLength(1));
    expect(f.scheduled[0].ms).toBe(POLL_MS);
    expect(f.serviceCalls()).toBe(1);
    f.scheduled[0].fn();
    await vi.waitFor(() => expect(f.scheduled).toHaveLength(2));
    expect(f.serviceCalls()).toBe(2);
  });

  it('test_pause_stops_scheduling', async () => {
    const f = fakes();
    const poller = new Poller(f.deps);
    poller.start();
    await vi.waitFor(() => expect(f.scheduled).toHaveLength(1));
    poller.pause();
    expect(f.scheduled[0].cancel).toHaveBeenCalledTimes(1);

    // A poll finishing while paused schedules nothing.
    await poller.pollNow();
    expect(f.scheduled).toHaveLength(1);

    // Resume polls right away and schedules again.
    poller.resume();
    await vi.waitFor(() => expect(f.scheduled).toHaveLength(2));
  });

  // test_no_sessions_call_after_a_hold_lands
  it('makes no sessions call when a hold lands between the service and sessions calls', async () => {
    let held = false;
    const f = fakes({
      heldFn: () => held,
      serviceStatus: async () => {
        held = true; // `crr holder pause` while `service status` was running
        return ok(service());
      },
    });
    const s = await new Poller(f.deps).pollNow();
    expect(f.serviceCalls()).toBe(1);
    expect(f.sessionCalls()).toBe(0);
    expect(s.wsl).toBe('held');
    expect(s.statusLine).toBe('crr holder is holding WSL down — not checking crr');
  });

  it('reads a hold check that throws on the second look as a hold', async () => {
    let calls = 0;
    const f = fakes({
      heldFn: () => {
        calls += 1;
        if (calls > 1) throw new Error('cannot read hold file');
        return false;
      },
    });
    const s = await new Poller(f.deps).pollNow();
    expect(f.sessionCalls()).toBe(0);
    expect(s.wsl).toBe('held');
  });

  // test_no_sessions_call_after_pause
  it('makes no sessions call for a poll that began before a pause (the machine went to sleep)', async () => {
    let release!: (r: CrrResult<ServiceStatus>) => void;
    const pending = new Promise<CrrResult<ServiceStatus>>((res) => { release = res; });
    const f = fakes({ serviceStatus: () => pending });
    const poller = new Poller(f.deps);
    const polled = poller.pollNow();
    await vi.waitFor(() => expect(f.serviceCalls()).toBe(1));
    poller.pause();
    release(ok(service()));
    const s = await polled;
    expect(f.sessionCalls()).toBe(0);
    // Rule 9 with the counts unknown.
    expect(s.icon).toBe('running');
    expect(s.statusLine).toBe('CRR running · sessions unknown');
    expect(s.counts).toBeUndefined();
  });

  it('asks for sessions again on the first poll after a pause and resume', async () => {
    const f = fakes();
    const poller = new Poller(f.deps);
    poller.pause();
    poller.resume();
    await vi.waitFor(() => expect(f.states).toHaveLength(1));
    expect(f.sessionCalls()).toBe(1);
  });

  // test_held_that_throws_is_a_hold
  it('reads a hold check that throws as a hold, and makes no distro call at all', async () => {
    const f = fakes({ heldFn: () => { throw new Error('cannot read hold file'); } });
    const s = await new Poller(f.deps).pollNow();
    expect(s.wsl).toBe('held');
    expect(f.clientCalls()).toBe(0);
    expect(f.serviceCalls()).toBe(0);
    expect(f.sessionCalls()).toBe(0);
  });

  // test_resume_joins_an_in_flight_poll
  it('resume() during an in-flight poll joins it rather than starting a second', async () => {
    let release!: (r: CrrResult<ServiceStatus>) => void;
    const pending = new Promise<CrrResult<ServiceStatus>>((res) => { release = res; });
    const f = fakes({ serviceStatus: () => pending });
    const poller = new Poller(f.deps);
    poller.start();
    await vi.waitFor(() => expect(f.serviceCalls()).toBe(1));
    poller.pause();
    poller.resume();
    expect(f.runningCalls()).toBe(1);
    release(ok(service()));
    await vi.waitFor(() => expect(f.states).toHaveLength(1));
    await Promise.resolve();
    expect(f.runningCalls()).toBe(1);
    expect(f.serviceCalls()).toBe(1);
    // Resumed, so the next poll is scheduled as usual.
    expect(f.scheduled).toHaveLength(1);
  });

  it('pause during an in-flight poll prevents rescheduling', async () => {
    let release!: (r: CrrResult<ServiceStatus>) => void;
    const pending = new Promise<CrrResult<ServiceStatus>>((res) => { release = res; });
    const f = fakes({ serviceStatus: () => pending });
    const poller = new Poller(f.deps);
    poller.start();
    await vi.waitFor(() => expect(f.serviceCalls()).toBe(1));
    poller.pause();
    release(ok(service()));
    await vi.waitFor(() => expect(f.states).toHaveLength(1));
    await Promise.resolve();
    expect(f.scheduled).toHaveLength(0);
  });
});
