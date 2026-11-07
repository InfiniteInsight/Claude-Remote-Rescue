import { describe, expect, it } from 'vitest';
import { summarizeError } from './summarizeError.js';

describe('summarizeError', () => {
  it('keeps the last non-empty line', () => {
    expect(summarizeError('Command failed: wsl.exe -d x\nreal reason\n\n')).toBe('real reason');
  });

  // test_summarize_error_strips_nul
  it('reads a UTF-16LE message that was decoded as UTF-8 (NUL-laced) as its text', () => {
    const laced = Buffer.from(
      'There is no distribution with the supplied name.\r\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n',
      'utf16le',
    ).toString('utf8');
    expect(laced).toContain('\u0000');
    expect(summarizeError(laced)).toBe('Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND');
  });

  it('turns a NUL-only answer into nothing, not a NUL', () => {
    expect(summarizeError('\u0000')).toBe('');
    expect(summarizeError('\u0000\r\u0000\n\u0000')).toBe('');
  });

  it('shortens a long line to fit the status line', () => {
    const s = summarizeError('x'.repeat(200));
    expect(s).toHaveLength(90);
    expect(s.endsWith('…')).toBe(true);
  });
});
