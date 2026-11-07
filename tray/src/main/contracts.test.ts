import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  ContractError,
  parseQr,
  parseServiceStatus,
  parseSessionCounts,
} from './contracts';

const fixture = (name: string): string =>
  readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), 'utf8');
const edited = (name: string, edit: (o: any) => void): string => {
  const o = JSON.parse(fixture(name));
  edit(o);
  return JSON.stringify(o);
};

describe('parseServiceStatus', () => {
  it('test_parse_service_status_fixture', () => {
    const s = parseServiceStatus(fixture('service'));
    expect(s.state).toBe('running');
    expect(s.services.map((u) => u.name)).toEqual([
      'watchdog',
      'dashboard',
      'keep-awake',
    ]);
    expect(s.dashboard_url).toBe('http://127.0.0.1:8377/');
  });

  it('rejects another contract version', () => {
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.contract = 2))),
    ).toThrow(ContractError);
  });

  it('rejects a boolean contract, as crr does', () => {
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.contract = true))),
    ).toThrow(ContractError);
  });

  it('rejects missing and unknown keys', () => {
    expect(() =>
      parseServiceStatus(edited('service', (o) => delete o.reason)),
    ).toThrow(/missing/);
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.extra = 1))),
    ).toThrow(/unknown/);
    expect(() =>
      parseServiceStatus(
        edited('service', (o) => (o.services[0].extra = 1)),
      ),
    ).toThrow(ContractError);
  });

  it('rejects bad enums and types', () => {
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.state = 'weird'))),
    ).toThrow(ContractError);
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.backend = 'launchd'))),
    ).toThrow(ContractError);
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.dashboard_login = 1))),
    ).toThrow(ContractError);
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.services = {}))),
    ).toThrow(ContractError);
    expect(() =>
      parseServiceStatus(
        edited('service', (o) => (o.services[1].active = 'yes')),
      ),
    ).toThrow(ContractError);
    expect(() =>
      parseServiceStatus(edited('service', (o) => (o.services[1] = null))),
    ).toThrow(ContractError);
  });

  it('rejects non-JSON and non-objects with a ContractError', () => {
    expect(() => parseServiceStatus('Traceback (most recent call last)')).toThrow(
      ContractError,
    );
    expect(() => parseServiceStatus('[]')).toThrow(ContractError);
    expect(() => parseServiceStatus('null')).toThrow(ContractError);
    expect(() => parseServiceStatus('')).toThrow(ContractError);
  });
});

describe('parseQr', () => {
  it('parses the fixture', () => {
    const q = parseQr(fixture('qr'));
    expect(q.provider).toBe('tailscale');
    expect(q.url).toBe('https://example-host.tailnet.ts.net/');
    expect(q.loopback_url).toBe('http://127.0.0.1:8377/');
  });

  it('accepts null provider, url and hint', () => {
    const q = parseQr(
      edited('qr', (o) => {
        o.provider = null;
        o.url = null;
        o.hint = null;
      }),
    );
    expect(q.url).toBeNull();
  });

  it('rejects wrong contract, keys and types', () => {
    expect(() => parseQr(edited('qr', (o) => (o.contract = 2)))).toThrow(
      ContractError,
    );
    expect(() => parseQr(edited('qr', (o) => delete o.hint))).toThrow(
      ContractError,
    );
    expect(() => parseQr(edited('qr', (o) => (o.extra = 1)))).toThrow(
      ContractError,
    );
    expect(() => parseQr(edited('qr', (o) => (o.url = 5)))).toThrow(
      ContractError,
    );
    expect(() => parseQr(edited('qr', (o) => (o.loopback_url = null)))).toThrow(
      ContractError,
    );
    expect(() => parseQr(edited('qr', (o) => (o.degraded = 'no')))).toThrow(
      ContractError,
    );
    expect(() => parseQr('not json')).toThrow(ContractError);
  });
});

describe('parseSessionCounts', () => {
  it('test_session_counts', () => {
    const fx = JSON.parse(fixture('status'));
    const count = (st: string) =>
      fx.sessions.filter((s: any) => s.state === st).length;
    const c = parseSessionCounts(fixture('status'));
    expect(c).toEqual({
      total: fx.sessions.length,
      live: count('live'),
      parked: count('parked'),
      crashed: 0,
      ghost: 0,
      attention: 0,
    });
    const elsewhere = parseSessionCounts(
      edited('status', (o) => (o.sessions[0].running_elsewhere = true)),
    );
    expect(elsewhere.attention).toBe(1);
  });

  it('counts crashed, ghost and conflict', () => {
    const c = parseSessionCounts(
      edited('status', (o) => {
        o.sessions[0].state = 'crashed';
        o.sessions[1].state = 'ghost';
        o.sessions[2].conflict = true;
      }),
    );
    expect(c.crashed).toBe(1);
    expect(c.ghost).toBe(1);
    expect(c.attention).toBe(2);
  });

  it('test_future_sessions_contract_with_extra_fields_parses', () => {
    const base = parseSessionCounts(fixture('status'));
    const c = parseSessionCounts(
      edited('status', (o) => {
        o.contract = 25;
        o.new_thing = { a: 1 };
        o.sessions[0].title = 'x';
      }),
    );
    expect(c).toEqual(base);
  });

  it('test_unknown_session_state_counts_in_total_only', () => {
    const base = parseSessionCounts(fixture('status'));
    const c = parseSessionCounts(
      edited('status', (o) => {
        o.sessions[0].state = 'hibernating';
        o.sessions[0].conflict = true;
      }),
    );
    expect(c.total).toBe(base.total);
    const named = (x: typeof c) => x.live + x.parked + x.crashed + x.ghost;
    expect(named(c)).toBe(named(base) - 1);
    expect(c.attention).toBe(1);
  });

  it('rejects an old or malformed payload', () => {
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.contract = 18))),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.contract = 19.5))),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.contract = true))),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.sessions = {}))),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.sessions[0].state = 5))),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(
        edited('status', (o) => (o.sessions[0].conflict = 'no')),
      ),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(
        edited('status', (o) => delete o.sessions[0].running_elsewhere),
      ),
    ).toThrow(ContractError);
    expect(() =>
      parseSessionCounts(edited('status', (o) => (o.sessions[0] = 3))),
    ).toThrow(ContractError);
    expect(() => parseSessionCounts('')).toThrow(ContractError);
  });
});
