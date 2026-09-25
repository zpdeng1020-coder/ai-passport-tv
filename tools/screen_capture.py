"""Read one screen capture off the device's console and write it as a PNG.

The device takes the picture itself, from the stripe it is about to hand the
panel, so what arrives here is the panel's input rather than a second
derivation of it. That matters for anything about colour: every check that
compares the server's output against itself cannot see a fault on the device's
side of the wire, and the screen cannot be read from a distance.

Double-click the third button on the device to ask for a capture; the picture
is taken from a whole frame a few dozen frames later and printed as hex
between two markers. Run this, then press:

    python3 tools/screen_capture.py --port /dev/cu.usbmodem101 --out screen.png
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib

import serial


def read_shot(port: str, timeout: float) -> tuple[int, int, bytes]:
    device = serial.Serial(port, 115200, timeout=0.2)
    device.dtr = False
    device.rts = False
    cols = rows = 0
    pixels: list[bytes] = []
    deadline = time.time() + timeout
    started = False
    try:
        while time.time() < deadline:
            line = device.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("SHOT begin"):
                _, _, c, r = line.split()
                cols, rows = int(c), int(r)
                pixels = []
                started = True
                print(f"capturing {cols}x{rows} ...", flush=True)
            elif started and line.startswith("SHOT end"):
                break
            elif started and line.startswith("SHOT"):
                raw = bytes.fromhex(line[4:])
                if len(raw) != cols * 2:
                    raise SystemExit(f"short row: {len(raw)} bytes, wanted {cols * 2}")
                pixels.append(raw)
    finally:
        device.close()
    if not started:
        raise SystemExit("no capture arrived; double-click the third button on the device")
    if len(pixels) != rows:
        raise SystemExit(f"got {len(pixels)} rows, wanted {rows}")
    return cols, rows, b"".join(pixels)


def write_png(path: str, cols: int, rows: int, raw: bytes) -> None:
    """RGB565 big-endian to a PNG, scaling 5/6/5 back to 8 bits per channel."""
    rgb = bytearray()
    for i in range(cols * rows):
        value = (raw[2 * i] << 8) | raw[2 * i + 1]
        r5, g6, b5 = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
        rgb += bytes(((r5 * 255) // 31, (g6 * 255) // 63, (b5 * 255) // 31))
    payload = b"".join(b"\x00" + bytes(rgb[y * cols * 3:(y + 1) * cols * 3])
                       for y in range(rows))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", cols, rows, 8, 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(payload, 6)) + chunk(b"IEND", b""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem101")
    parser.add_argument("--out", default="screen.png")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    cols, rows, raw = read_shot(args.port, args.timeout)
    write_png(args.out, cols, rows, raw)
    print(f"wrote {args.out} ({cols}x{rows})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
