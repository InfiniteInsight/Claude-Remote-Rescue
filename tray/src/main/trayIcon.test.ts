import { describe, expect, it } from 'vitest';
import { inflateSync } from 'node:zlib';
import type { TrayIcon } from './poll.js';
import { TRAY_ICON_COLORS, trayIconPng } from './trayIcon.js';

const ICONS: TrayIcon[] = ['running', 'attention', 'stopped', 'wsl-down'];
const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Split a PNG into its chunks. */
function chunks(png: Buffer): Array<{ type: string; data: Buffer }> {
  const out: Array<{ type: string; data: Buffer }> = [];
  let at = 8;
  while (at < png.length) {
    const len = png.readUInt32BE(at);
    out.push({
      type: png.subarray(at + 4, at + 8).toString('ascii'),
      data: png.subarray(at + 8, at + 8 + len),
    });
    at += 12 + len;
  }
  return out;
}

/** Decode the RGBA pixels (every scanline starts with a filter byte). */
function pixels(png: Buffer, size: number) {
  const idat = Buffer.concat(
    chunks(png).filter((c) => c.type === 'IDAT').map((c) => c.data),
  );
  const raw = inflateSync(idat);
  const stride = 1 + size * 4;
  expect(raw.length).toBe(stride * size);
  return (x: number, y: number): [number, number, number, number] => {
    expect(raw[y * stride]).toBe(0); // filter: none
    const o = y * stride + 1 + x * 4;
    return [raw[o], raw[o + 1], raw[o + 2], raw[o + 3]];
  };
}

describe('trayIconPng', () => {
  it('test_tray_icon_png', () => {
    const seen = new Set<string>();
    for (const icon of ICONS) {
      const png = trayIconPng(icon);
      expect(png.subarray(0, 8).equals(SIGNATURE)).toBe(true);
      const cs = chunks(png);
      expect(cs.map((c) => c.type)).toEqual(['IHDR', 'IDAT', 'IEND']);
      const ihdr = cs[0].data;
      expect(ihdr.readUInt32BE(0)).toBe(32);
      expect(ihdr.readUInt32BE(4)).toBe(32);
      expect(ihdr[8]).toBe(8); // bit depth
      expect(ihdr[9]).toBe(6); // colour type RGBA

      const px = pixels(png, 32);
      const [r, g, b] = TRAY_ICON_COLORS[icon];
      expect(px(16, 16)).toEqual([r, g, b, 255]);
      expect(px(0, 0)[3]).toBe(0);
      expect(px(31, 0)[3]).toBe(0);
      expect(px(0, 31)[3]).toBe(0);
      expect(px(31, 31)[3]).toBe(0);
      seen.add(png.toString('base64'));
    }
    expect(seen.size).toBe(4);
  });

  it('has the documented colours', () => {
    expect(TRAY_ICON_COLORS).toEqual({
      running: [22, 163, 74],
      attention: [217, 119, 6],
      stopped: [107, 114, 128],
      'wsl-down': [30, 64, 175],
    });
  });

  it('writes a valid CRC on every chunk', () => {
    // Bitwise reference CRC-32, independent of the table-based one under test.
    const reference = (buf: Buffer): number => {
      let crc = 0xffffffff;
      for (const byte of buf) {
        crc ^= byte;
        for (let k = 0; k < 8; k++) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
      }
      return (crc ^ 0xffffffff) >>> 0;
    };
    const png = trayIconPng('attention');
    let at = 8;
    while (at < png.length) {
      const len = png.readUInt32BE(at);
      const body = png.subarray(at + 4, at + 8 + len); // type + data
      expect(png.readUInt32BE(at + 8 + len)).toBe(reference(body));
      at += 12 + len;
    }
    expect(at).toBe(png.length);
  });

  it('honours a custom size', () => {
    const png = trayIconPng('stopped', 16);
    expect(chunks(png)[0].data.readUInt32BE(0)).toBe(16);
    expect(pixels(png, 16)(8, 8)[3]).toBe(255);
  });
});
