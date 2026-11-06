/**
 * crr's JSON contracts, as the tray app reads them.
 *
 * `service` and `qr` mirror `validate_service_payload` and `validate_qr_payload`
 * in crr/core/contracts.py exactly (exact key sets, exact contract version).
 * `status` is read loosely: only `contract`, `sessions[].state`,
 * `.running_elsewhere` and `.conflict` matter, every other key is ignored.
 *
 * Every parser throws ContractError (never anything else) on output it does not
 * understand, so callers can report a mismatch instead of crashing.
 */

export const SERVICE_CONTRACT = 1;
export const QR_CONTRACT = 1;
export const SESSIONS_MIN_CONTRACT = 19;

/** crr's output does not match what this app reads: wrong version, shape or type. */
export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ContractError';
  }
}

/**
 * The output was not JSON at all (a traceback, a shell banner, nothing). A
 * ContractError, but not evidence of a version skew, so callers can tell the
 * two apart.
 */
export class NotJsonError extends ContractError {
  constructor(message: string) {
    super(message);
    this.name = 'NotJsonError';
  }
}

export interface ServiceUnit {
  name: string; // crr validates it only as a string, so a new unit name must not break us
  unit: string;
  installed: boolean;
  enabled: boolean;
  active: boolean;
}

export interface ServiceStatus {
  contract: 1;
  backend: 'systemd' | 'unsupported';
  reason: string;
  state: 'not-installed' | 'running' | 'stopped' | 'partial';
  services: ServiceUnit[];
  dashboard_url: string;
  dashboard_login: boolean;
}

export interface QrInfo {
  contract: 1;
  provider: string | null;
  url: string | null;
  loopback_url: string;
  hint: string | null;
  dashboard_reachable: boolean;
  degraded: boolean;
}

export interface SessionCounts {
  total: number;
  live: number;
  parked: number;
  crashed: number;
  ghost: number;
  /** crashed, running elsewhere, or in conflict. */
  attention: number;
}

// Same tuples as crr/core/contracts.py.
const SERVICE_PAYLOAD_KEYS = [
  'contract', 'backend', 'reason', 'state', 'services', 'dashboard_url',
  'dashboard_login',
] as const;
const SERVICE_UNIT_FIELDS = ['name', 'unit', 'installed', 'enabled', 'active'] as const;
const SERVICE_BACKENDS = ['systemd', 'unsupported'] as const;
const SERVICE_STATES = ['not-installed', 'running', 'stopped', 'partial'] as const;
const QR_PAYLOAD_KEYS = [
  'contract', 'provider', 'url', 'loopback_url', 'hint', 'dashboard_reachable',
  'degraded',
] as const;
const SESSION_STATES = ['live', 'ghost', 'crashed', 'parked'] as const;

type Mapping = Record<string, unknown>;

function parseJson(stdout: string, what: string): unknown {
  try {
    return JSON.parse(stdout);
  } catch {
    throw new NotJsonError(`${what} output is not JSON`);
  }
}

function typeName(v: unknown): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'list';
  return typeof v;
}

function requireMapping(v: unknown, what: string): Mapping {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) {
    throw new ContractError(`${what} must be a mapping, got ${typeName(v)}`);
  }
  return v as Mapping;
}

function requireExactKeys(v: Mapping, keys: readonly string[], what: string): void {
  const missing = keys.filter((k) => !Object.hasOwn(v, k)).sort();
  const unknown = Object.keys(v).filter((k) => !keys.includes(k)).sort();
  if (missing.length > 0) {
    throw new ContractError(`${what} missing key(s): ${missing.join(', ')}`);
  }
  if (unknown.length > 0) {
    throw new ContractError(`${what} has unknown key(s): ${unknown.join(', ')}`);
  }
}

function requireString(v: unknown, what: string): void {
  if (typeof v !== 'string') {
    throw new ContractError(`${what} must be str, got ${typeName(v)}`);
  }
}

function requireNullableString(v: unknown, what: string): void {
  if (v !== null && typeof v !== 'string') {
    throw new ContractError(`${what} must be str/NoneType, got ${typeName(v)}`);
  }
}

function requireBool(v: unknown, what: string): void {
  if (typeof v !== 'boolean') {
    throw new ContractError(`${what} must be bool, got ${typeName(v)}`);
  }
}

function requireEnum(v: unknown, allowed: readonly string[], what: string): void {
  if (typeof v !== 'string' || !allowed.includes(v)) {
    throw new ContractError(
      `${what} must be one of ${allowed.join('/')}, got ${JSON.stringify(v)}`,
    );
  }
}

function requireInt(v: unknown, what: string): number {
  if (typeof v !== 'number' || !Number.isInteger(v)) {
    throw new ContractError(`${what} 'contract' must be an int, got ${typeName(v)}`);
  }
  return v;
}

function requireContract(payload: Mapping, expected: number, what: string): void {
  const got = requireInt(payload.contract, what);
  if (got !== expected) {
    throw new ContractError(
      `${what} 'contract' is ${got}, this app reads ${expected}`,
    );
  }
}

export function parseServiceStatus(stdout: string): ServiceStatus {
  const payload = requireMapping(parseJson(stdout, 'service status'), 'service payload');
  requireExactKeys(payload, SERVICE_PAYLOAD_KEYS, 'service payload');
  requireContract(payload, SERVICE_CONTRACT, 'service');
  requireEnum(payload.backend, SERVICE_BACKENDS, "service 'backend'");
  requireString(payload.reason, "service 'reason'");
  requireEnum(payload.state, SERVICE_STATES, "service 'state'");
  requireString(payload.dashboard_url, "service 'dashboard_url'");
  requireBool(payload.dashboard_login, "service 'dashboard_login'");
  if (!Array.isArray(payload.services)) {
    throw new ContractError(
      `service 'services' must be list, got ${typeName(payload.services)}`,
    );
  }
  payload.services.forEach((raw: unknown, i: number) => {
    const what = `service 'services'[${i}]`;
    const row = requireMapping(raw, what);
    requireExactKeys(row, SERVICE_UNIT_FIELDS, what);
    for (const f of ['name', 'unit'] as const) requireString(row[f], `${what} '${f}'`);
    for (const f of ['installed', 'enabled', 'active'] as const) {
      requireBool(row[f], `${what} '${f}'`);
    }
  });
  return payload as unknown as ServiceStatus;
}

export function parseQr(stdout: string): QrInfo {
  const payload = requireMapping(parseJson(stdout, 'qr'), 'qr payload');
  requireExactKeys(payload, QR_PAYLOAD_KEYS, 'qr payload');
  requireContract(payload, QR_CONTRACT, 'qr');
  for (const f of ['provider', 'url', 'hint'] as const) {
    requireNullableString(payload[f], `qr '${f}'`);
  }
  requireString(payload.loopback_url, "qr 'loopback_url'");
  requireBool(payload.dashboard_reachable, "qr 'dashboard_reachable'");
  requireBool(payload.degraded, "qr 'degraded'");
  return payload as unknown as QrInfo;
}

export function parseSessionCounts(stdout: string): SessionCounts {
  const payload = requireMapping(parseJson(stdout, 'status'), 'status payload');
  if (!Object.hasOwn(payload, 'contract')) {
    throw new ContractError("status payload missing key(s): contract");
  }
  const contract = requireInt(payload.contract, 'status');
  if (contract < SESSIONS_MIN_CONTRACT) {
    throw new ContractError(
      `status 'contract' is ${contract}, this app needs ${SESSIONS_MIN_CONTRACT} or newer`,
    );
  }
  if (!Array.isArray(payload.sessions)) {
    throw new ContractError(
      `status 'sessions' must be list, got ${typeName(payload.sessions)}`,
    );
  }
  const counts: SessionCounts = {
    total: 0, live: 0, parked: 0, crashed: 0, ghost: 0, attention: 0,
  };
  payload.sessions.forEach((raw: unknown, i: number) => {
    const what = `status 'sessions'[${i}]`;
    const s = requireMapping(raw, what);
    // Any string state is accepted: crr bumps the sessions contract often and a
    // new label must not read as a version mismatch. Unknown states count in
    // `total` only.
    requireString(s.state, `${what} 'state'`);
    requireBool(s.running_elsewhere, `${what} 'running_elsewhere'`);
    requireBool(s.conflict, `${what} 'conflict'`);
    const state = s.state as string;
    counts.total += 1;
    if ((SESSION_STATES as readonly string[]).includes(state)) {
      counts[state as (typeof SESSION_STATES)[number]] += 1;
    }
    if (state === 'crashed' || s.running_elsewhere === true || s.conflict === true) {
      counts.attention += 1;
    }
  });
  return counts;
}
