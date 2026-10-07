#!/usr/bin/env python3
"""Offline lab: how small can a 320x180 frame get, and what does it cost in picture.

Deliberately independent of server/: it decodes real channel footage itself and
tries candidate codings from the device's constraints, not from the current wire
format. The constraints it takes are the measured ones:

  * the panel keeps its picture (GRAM), so a frame only has to carry what changed;
  * inflate costs ~18-21 cycles per OUTPUT byte, so fewer output bytes is cheaper
    for the CPU as well as for the link;
  * the receiver can hold about 4.5 frames, so bytes per frame is also memory.

Candidates (all lossy; quality is PSNR of what the panel would show vs the source):

  full332      whole frame, 3-3-2 indexed, deflate                (the yardstick)
  delta332     only blocks that changed enough vs what is on the panel
  pal16        whole frame, per-frame 16-colour palette, 4 bits/pixel, deflate
  delta_pal16  changed blocks only, 16-colour palette per frame

    python tools/frame_size_lab.py CCTV1.ts [--frames 240] [--fps 24]
"""
import argparse
import subprocess
import sys
import zlib

import numpy as np

W, H = 320, 180


def read_frames(path: str, count: int, fps: int, skip_s: float) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", str(skip_s), "-i", path,
           "-vf", f"fps={fps},scale={W}:{H}:flags=bicubic", "-frames:v", str(count),
           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (W * H * 3)
    return np.frombuffer(raw[: n * W * H * 3], np.uint8).reshape(n, H, W, 3)


def quant332(rgb: np.ndarray) -> np.ndarray:
    r = (rgb[..., 0].astype(np.uint16) * 7 + 127) // 255
    g = (rgb[..., 1].astype(np.uint16) * 7 + 127) // 255
    b = (rgb[..., 2].astype(np.uint16) * 3 + 127) // 255
    return ((r << 5) | (g << 2) | b).astype(np.uint8)


_P332 = np.zeros((256, 3), np.uint8)
for _i in range(256):
    _P332[_i] = ((_i >> 5) * 255 // 7, ((_i >> 2) & 7) * 255 // 7, (_i & 3) * 255 // 3)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def kmeans_palette(rgb: np.ndarray, k: int, iters: int = 6, sample: int = 4096):
    """Per-frame palette by a few k-means steps on a pixel sample."""
    px = rgb.reshape(-1, 3).astype(np.float32)
    rng = np.random.default_rng(1)
    pts = px[rng.choice(len(px), min(sample, len(px)), replace=False)]
    cent = pts[rng.choice(len(pts), k, replace=False)].copy()
    for _ in range(iters):
        d = ((pts[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            m = lab == j
            if m.any():
                cent[j] = pts[m].mean(0)
    return cent.round().clip(0, 255).astype(np.uint8)


def assign(rgb: np.ndarray, pal: np.ndarray) -> np.ndarray:
    px = rgb.reshape(-1, 3).astype(np.int32)
    out = np.empty(len(px), np.uint8)
    p = pal.astype(np.int32)
    for s in range(0, len(px), 8192):
        d = ((px[s:s + 8192, None, :] - p[None, :, :]) ** 2).sum(-1)
        out[s:s + 8192] = d.argmin(1)
    return out.reshape(H, W)


def pack_nibbles(idx: np.ndarray) -> bytes:
    f = idx.reshape(-1)
    return ((f[0::2] << 4) | f[1::2]).astype(np.uint8).tobytes()


BW = None  # block width; None means square blocks of `bs`


def pack_nibbles_flat(blocks: np.ndarray) -> bytes:
    f = blocks.reshape(-1)
    if len(f) % 2:
        f = np.append(f, 0)
    return ((f[0::2] << 4) | f[1::2]).astype(np.uint8).tobytes()


def block_view(a: np.ndarray, bs: int) -> np.ndarray:
    h, w = a.shape[:2]
    bw = BW or bs
    assert h % bs == 0 and w % bw == 0, (h, w, bs, bw)
    return a.reshape(h // bs, bs, w // bw, bw, *a.shape[2:]).swapaxes(1, 2)


def changed_mask(cand: np.ndarray, shown: np.ndarray, bs: int, thr: float) -> np.ndarray:
    """Blocks worth resending: what this frame would show there differs from what
    is on the panel. Compared in the coded domain -- source-vs-shown would count
    the quantiser's own error as change and resend a still picture forever."""
    err = np.abs(cand.astype(np.int16) - shown.astype(np.int16)).max(-1).astype(np.float32)
    return block_view(err, bs).mean((2, 3)) > thr


def pad_to_block(frames: np.ndarray, bs: int) -> np.ndarray:
    """Replicate the last rows so the height is a whole number of blocks (180 is
    not a multiple of 8). The pad is coded like real pixels, so it costs a little."""
    global H
    ph = (-frames.shape[1]) % bs
    pw = (-frames.shape[2]) % (BW or bs)
    if ph or pw:
        frames = np.pad(frames, ((0, 0), (0, ph), (0, pw), (0, 0)), mode="edge")
    H, W_ = frames.shape[1], frames.shape[2]
    globals()["W"] = W_
    return frames


def run(frames: np.ndarray, bs: int, thr: float, keyframe_every: int):
    frames = pad_to_block(frames, bs)
    n = len(frames)
    res = {k: {"bytes": [], "psnr": []} for k in ("full332", "delta332", "pal16", "delta_pal16")}
    skip = []
    shown332 = None
    shown16 = None
    for i, rgb in enumerate(frames):
        # -- full332
        idx = quant332(rgb)
        b = len(zlib.compress(idx.tobytes(), 6))
        res["full332"]["bytes"].append(b)
        res["full332"]["psnr"].append(psnr(rgb, _P332[idx]))

        # -- delta332 (keyframe is a full frame; the panel keeps the rest)
        key = shown332 is None or i % keyframe_every == 0
        if key:
            shown332 = _P332[idx].copy()
            b = len(zlib.compress(idx.tobytes(), 6)) + 4
            skip.append(0.0)
        else:
            m = changed_mask(_P332[idx], shown332, bs, thr)
            skip.append(1.0 - m.mean())
            blocks = block_view(idx, bs)[m]
            payload = np.packbits(m.reshape(-1)).tobytes() + blocks.tobytes()
            b = len(zlib.compress(payload, 6)) + 4
            new = shown332.copy()
            bi, bj = np.nonzero(m)
            bw = BW or bs
            for y, x in zip(bi, bj):
                new[y * bs:(y + 1) * bs, x * bw:(x + 1) * bw] = _P332[idx[y * bs:(y + 1) * bs, x * bw:(x + 1) * bw]]
            shown332 = new
        res["delta332"]["bytes"].append(b)
        res["delta332"]["psnr"].append(psnr(rgb, shown332))

        # -- pal16 (palette 16 x 3 bytes ahead of the nibbles)
        pal = kmeans_palette(rgb, 16)
        p16 = assign(rgb, pal)
        b = len(zlib.compress(pack_nibbles(p16), 6)) + 48
        res["pal16"]["bytes"].append(b)
        res["pal16"]["psnr"].append(psnr(rgb, pal[p16]))

        # -- delta_pal16: same palette, changed blocks only
        key = shown16 is None or i % keyframe_every == 0
        if key:
            shown16 = pal[p16].copy()
            b = len(zlib.compress(pack_nibbles(p16), 6)) + 52
        else:
            m = changed_mask(pal[p16], shown16, bs, thr)
            blocks = block_view(p16, bs)[m]
            payload = np.packbits(m.reshape(-1)).tobytes() + pack_nibbles_flat(blocks) if len(blocks) else np.packbits(m.reshape(-1)).tobytes()
            b = len(zlib.compress(payload, 6)) + 52
            new = shown16.copy()
            bi, bj = np.nonzero(m)
            bw = BW or bs
            for y, x in zip(bi, bj):
                new[y * bs:(y + 1) * bs, x * bw:(x + 1) * bw] = pal[p16[y * bs:(y + 1) * bs, x * bw:(x + 1) * bw]]
            shown16 = new
        res["delta_pal16"]["bytes"].append(b)
        res["delta_pal16"]["psnr"].append(psnr(rgb, shown16))
    return res, float(np.mean(skip[1:])) if n > 1 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--frames", type=int, default=240)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--skip-s", type=float, default=2.0)
    ap.add_argument("--block", type=int, default=8)
    ap.add_argument("--thr", type=float, default=10.0, help="mean abs error that makes a block worth resending")
    ap.add_argument("--keyframe-every", type=int, default=48)
    ap.add_argument("--stripe", action="store_true", help="blocks are whole 12-row stripes (what the panel path can write)")
    a = ap.parse_args()
    if a.stripe:
        global BW
        a.block, BW = 12, W
    frames = read_frames(a.source, a.frames, a.fps, a.skip_s)
    print(f"{a.source}: {len(frames)} frames at {a.fps} fps, block {a.block}, thr {a.thr}, "
          f"keyframe every {a.keyframe_every}")
    res, skipped = run(frames, a.block, a.thr, a.keyframe_every)
    print(f"blocks skipped on non-key frames: {skipped * 100:.0f}%")
    print(f"{'coding':<12}{'avg B':>8}{'p95 B':>8}{'max B':>8}{'PSNR dB':>9}{'min PSNR':>10}")
    for k, v in res.items():
        b = np.array(v["bytes"])
        p = np.array(v["psnr"])
        print(f"{k:<12}{b.mean():>8.0f}{np.percentile(b, 95):>8.0f}{b.max():>8.0f}{p.mean():>9.1f}{p.min():>10.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
