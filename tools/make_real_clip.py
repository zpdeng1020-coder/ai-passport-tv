#!/usr/bin/env python3
"""Make a clip of real channel footage for the network demo, in two variants
built from the SAME frames and the SAME quantiser, so the only difference is
how many bytes the frames carry:

  full    every frame carries all 15 stripes                      (baseline)
  delta   a stripe is sent only if it changed enough from what the panel is
          already showing; one stripe per frame is refreshed regardless, rotating,
          so a stripe lost on the network is repaired within 15 frames.

Wire (per frame) is the demo's own: u8 0 | u8 15 | u16 be len[15] | zlib stripe...
A length of 0 means "leave this stripe as it is" -- the demo's draw_frame skips it,
so a skipped stripe costs no inflate, no expand and no panel bus time either.

Quantised with ffmpeg's fixed 3-3-2 grid (rgb8, no dither) so the device's own
palette applies; change detection is done in that same coded domain, so the
quantiser's error is never mistaken for motion.

    python tools/make_real_clip.py SRC.ts --mode delta --out main/clip_delta.bin
"""
import argparse
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np

W, H, ROWS = 320, 180, 12
STRIPES = H // ROWS
SPX = W * ROWS

# The device's 3-3-2 palette: 36-step red and green, 85-step blue.
PAL = np.zeros((256, 3), np.int16)
for i in range(256):
    PAL[i] = ((i >> 5) * 36, ((i >> 2) & 7) * 36, (i & 3) * 85)


def read_indices(src: str, frames: int, fps: int, skip_s: float) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", str(skip_s), "-i", src,
           "-vf", f"fps={fps},scale={W}:{H}:flags=bicubic,format=rgb8",
           "-frames:v", str(frames), "-pix_fmt", "rgb8", "-sws_dither", "none",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (W * H)
    return np.frombuffer(raw[: n * W * H], np.uint8).reshape(n, H, W)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def encode(idx_frames: np.ndarray, mode: str, thr: float, px_frac: float):
    payloads, sent_stripes, psnrs = [], [], []
    shown = None  # what the panel shows, as indices
    for n, idx in enumerate(idx_frames):
        rgb = PAL[idx]
        send = [True] * STRIPES
        if mode == "delta" and shown is not None:
            srgb = PAL[shown]
            for s in range(STRIPES):
                a = rgb[s * ROWS:(s + 1) * ROWS]
                b = srgb[s * ROWS:(s + 1) * ROWS]
                err = np.abs(a - b).max(-1)
                send[s] = bool(err.mean() > thr or (err > 24).mean() > px_frac)
            send[n % STRIPES] = True          # rotating refresh
        comp, lens = [], []
        for s in range(STRIPES):
            if send[s]:
                z = zlib.compress(idx[s * ROWS:(s + 1) * ROWS].tobytes(), 6)
                comp.append(z); lens.append(len(z))
            else:
                lens.append(0)
        payloads.append(bytes([0, STRIPES]) + struct.pack(f">{STRIPES}H", *lens) + b"".join(comp))
        sent_stripes.append(sum(send))
        shown = idx.copy() if shown is None else shown
        for s in range(STRIPES):
            if send[s]:
                shown[s * ROWS:(s + 1) * ROWS] = idx[s * ROWS:(s + 1) * ROWS]
        psnrs.append(psnr(rgb, PAL[shown]))
    return payloads, sent_stripes, psnrs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--mode", choices=["full", "delta"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--frames", type=int, default=240)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--skip-s", type=float, default=2.0)
    ap.add_argument("--thr", type=float, default=6.0, help="stripe mean abs error that forces a resend")
    ap.add_argument("--px-frac", type=float, default=0.012, help="fraction of pixels >24 off that forces a resend")
    a = ap.parse_args()
    idx = read_indices(a.source, a.frames, a.fps, a.skip_s)
    payloads, sent, psnrs = encode(idx, a.mode, a.thr, a.px_frac)
    head = b"DCL1" + struct.pack("<HH", len(payloads), 1)
    head += struct.pack(f"<{len(payloads)}I", *(len(p) for p in payloads))
    Path(a.out).write_bytes(head + b"".join(payloads))
    sizes = np.array([len(p) for p in payloads])
    print(f"{a.mode}: {len(payloads)} frames @ {a.fps} fps  avg {sizes.mean():.0f} B  "
          f"p95 {np.percentile(sizes, 95):.0f}  max {sizes.max()}  "
          f"stripes sent avg {np.mean(sent):.1f}/{STRIPES}  "
          f"PSNR vs full-coded avg {np.mean(psnrs):.1f} min {np.min(psnrs):.1f} dB  -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
