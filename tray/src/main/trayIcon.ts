/**
 * The tray icon image, drawn in code.
 *
 * A filled circle on a transparent background, one colour per state, encoded
 * as a PNG with nothing but `node:zlib`. No image library and no binary assets
 * to ship: Electron's `nativeImage.createFromBuffer` reads the bytes directly.
 */
import { deflateSync } from 'node:zlib';
import type { TrayIcon } from './poll.js';

export const TRAY_ICON_COLORS: Record<TrayIcon, [number, number, number]> = {
  running: [22, 163, 74],
  attention: [217, 119, 6],
  stopped: [107, 114, 128],
  'wsl-down': [30, 64, 175],
};

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const CRC_TABLE: Uint32Array = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of buf) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** One PNG chunk: length, type, data, then the CRC over type and data. */
function chunk(type: string, data: Buffer): Buffer {
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const out = Buffer.alloc(body.length + 8);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body), body.length + 4);
  return out;
}

/** A size x size RGBA PNG: a filled circle in the state's colour, transparent elsewhere. */
export function trayIconPng(icon: TrayIcon, size = 32): Buffer {
  const [r, g, b] = TRAY_ICON_COLORS[icon];
  const centre = (size - 1) / 2;
  const radius = size / 2 - 1;
  const stride = 1 + size * 4; // each scanline starts with a filter byte (0 = none)
  const raw = Buffer.alloc(stride * size); // zero-filled: transparent, filter none
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - centre;
      const dy = y - centre;
      if (dx * dx + dy * dy > radius * radius) continue;
      const o = y * stride + 1 + x * 4;
      raw[o] = r;
      raw[o + 1] = g;
      raw[o + 2] = b;
      raw[o + 3] = 255;
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type: RGBA
  // compression, filter and interlace methods stay 0
  return Buffer.concat([
    PNG_SIGNATURE,
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}
