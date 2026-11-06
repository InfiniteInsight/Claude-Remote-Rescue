import { describe, expect, it } from 'vitest';
import { holds, readHold } from './hold.js';

const BOOT = 1_000_000;

describe('holds', () => {
  it('test_holds_matches_crr', () => {
    const now = 1000;
    const cases: Array<[string, unknown, number | undefined, boolean]> = [
      ['no record', undefined, BOOT, false],
      ['stop', { v: 1, mode: 'stop', set_at: 1 }, BOOT, true],
      ['pause in the future', { v: 1, mode: 'pause', set_at: 1, until: 2000 }, BOOT, true],
      ['pause in the past', { v: 1, mode: 'pause', set_at: 1, until: 500 }, BOOT, false],
      ['until-reboot, boot 3 s away', { v: 1, mode: 'until-reboot', set_at: 1, boot: BOOT + 3 }, BOOT, true],
      ['until-reboot, boot 600 s away', { v: 1, mode: 'until-reboot', set_at: 1, boot: BOOT + 600 }, BOOT, false],
      ['until-reboot, unknown machine boot', { v: 1, mode: 'until-reboot', set_at: 1, boot: BOOT + 600 }, undefined, true],
      ['v:2', { v: 2, mode: 'stop', set_at: 1 }, BOOT, false],
      ['extra key', { v: 1, mode: 'stop', set_at: 1, x: 1 }, BOOT, false],
      ['set_at is a boolean', { v: 1, mode: 'stop', set_at: true }, BOOT, false],
      ['a string record', 'stop', BOOT, false],
    ];
    for (const [name, record, boot, want] of cases) {
      expect(holds(record, now, boot), name).toBe(want);
    }
  });
});

describe('readHold', () => {
  it('test_read_hold_missing_file_is_undefined', () => {
    const missing = (): string => {
      throw Object.assign(new Error('no such file'), { code: 'ENOENT' });
    };
    expect(readHold('C:\\ProgramData\\crr\\holder-hold.json', missing)).toBeUndefined();
  });

  it('returns the parsed record from the injected reader', () => {
    const read = (): string => '{"v":1,"mode":"stop","set_at":1}';
    expect(readHold('x', read)).toEqual({ v: 1, mode: 'stop', set_at: 1 });
  });

  it('returns undefined for bad JSON', () => {
    expect(readHold('x', () => '{not json')).toBeUndefined();
  });
});
