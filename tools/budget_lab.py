#!/usr/bin/env python3
"""Offline lab for a fixed frame rate with a per-frame byte budget.

Feeds real channel footage through the server's own quantiser settings and the
server's own stripe compressor, and compares ways of choosing which stripes of a
frame to send:

  full      every stripe, every frame                     (what the wire carried before delta)
  delta3    changed stripes only, 3% tolerance, no budget  (what TV_DELTA=1 did before)
  budget    frames.choose_stripes: stripes ranked by how many pixels differ from what
            the panel shows, sent in that order until the frame's byte budget is
            spent; the rotating refresh stripe always goes
  bkt<N>k   frames.encode_within at N kB/s: fit rate / fps, coarser colour if needed

Reported per channel and budget: bytes per frame, encode time per frame, how much
of the panel is stale after each frame (share of pixels that differ from the frame
just decoded) and PSNR of the panel against the decoded frame. Stdlib for the
encode path being timed, numpy only for the picture metrics outside the timer.

    python tools/budget_lab.py /tmp/avbench/CCTV5.ts --budgets 6000,9000,12000
"""
import argparse
import subprocess
import sys
import time

import numpy as np

from server import frames

PAL = np.zeros((256, 3), np.int16)
for _i in range(256):
    PAL[_i] = ((_i >> 5) * 36, ((_i >> 2) & 7) * 36, (_i & 3) * 85)


def read_frames(src: str, count: int, skip_s: float) -> tuple[list[bytes], float]:
    """Decoded index frames at the source's own rate, as the server's graph makes them."""
    fps = source_fps(src)
    cmd = ["ffmpeg", "-v", "error", "-ss", str(skip_s), "-i", src,
           "-vf", f"fps={fps},{frames.FIT},format=rgb8", "-frames:v", str(count),
           "-pix_fmt", "rgb8", "-sws_dither", "none", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // frames.FRAME_PIXELS
    return [raw[i * frames.FRAME_PIXELS:(i + 1) * frames.FRAME_PIXELS] for i in range(n)], fps


def source_fps(src: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=avg_frame_rate", "-of", "default=nw=1:nk=1", src],
                         capture_output=True, text=True, check=True).stdout.split()[0]
    num, den = out.split("/")
    return float(num) / float(den)


def stripe(frame: bytes, at: int) -> bytes:
    return frame[at * frames.STRIPE_PIXELS:(at + 1) * frames.STRIPE_PIXELS]


def make_fit(rate: float, fps: float):
    """The product's rule: each frame fits rate / fps, coarser colour if it must."""
    target = int(rate / fps)

    def choose(raw, shown, tick):
        return frames.encode_within(raw, shown, tick, target)[0]
    return choose


def psnr(a: bytes, b: bytes) -> float:
    ra = PAL[np.frombuffer(a, np.uint8)].astype(np.float64)
    rb = PAL[np.frombuffer(b, np.uint8)].astype(np.float64)
    mse = np.mean((ra - rb) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def run(name: str, video: list[bytes], choose) -> dict:
    shown = None
    size, secs, stale, snr, age_max = [], [], [], [], []
    age = [0] * frames.STRIPES
    for tick, raw in enumerate(video):
        t0 = time.perf_counter()
        if shown is None:
            sent = frames.compress_stripes(raw)
        else:
            sent = choose(raw, shown, tick)
        payload = frames.pack_stripes(sent)
        secs.append(time.perf_counter() - t0)
        size.append(sum(map(len, payload)))
        base = raw if shown is None else shown
        shown = frames.apply_stripes(raw, base, sent)
        stale.append(frames.differing_pixels(shown, raw) / frames.FRAME_PIXELS)
        snr.append(psnr(shown, raw))
        for at in range(frames.STRIPES):
            age[at] = 0 if sent[at] or shown[at * frames.STRIPE_PIXELS:(at + 1) * frames.STRIPE_PIXELS] == stripe(raw, at) else age[at] + 1
        age_max.append(max(age))
    b, t = np.array(size), np.array(secs) * 1000
    return {"name": name, "avg": b.mean(), "p95": np.percentile(b, 95), "max": b.max(),
            "ms": t.mean(), "ms95": np.percentile(t, 95), "msmax": t.max(),
            "stale": np.mean(stale) * 100, "stale95": np.percentile(stale, 95) * 100,
            "snr": np.mean(snr), "snrmin": np.min(snr), "age": max(age_max)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--frames", type=int, default=200)
    ap.add_argument("--skip-s", type=float, default=2.0)
    ap.add_argument("--budgets", default="6000,9000,12000")
    ap.add_argument("--rates", default="", help="bytes/s for a token bucket, comma separated")
    ap.add_argument("--min-diff", type=float, default=0.005,
                    help="share of a stripe's pixels that must differ before it is worth sending")
    a = ap.parse_args()
    video, fps = read_frames(a.source, a.frames, a.skip_s)
    print(f"{a.source}: {len(video)} frames, source {fps:g} fps")
    rows = [run("full", video, lambda raw, shown, tick: frames.compress_stripes(raw)),
            run("delta3", video, lambda raw, shown, tick:
                frames.choose_stripes(raw, shown, tick, 10 ** 9, 0.03))]
    for rate in (int(x) for x in a.rates.split(",") if x):
        rows.append(run(f"bkt{rate // 1000}k", video, make_fit(rate, fps)))
    for budget in (int(x) for x in a.budgets.split(",") if x):
        rows.append(run(f"budget{budget // 1000}k", video,
                        lambda raw, shown, tick, b=budget: frames.choose_stripes(raw, shown, tick, b, a.min_diff)))
    print(f"{'coding':<10}{'avg B':>7}{'p95':>7}{'max':>7} | {'ms avg':>7}{'p95':>7}{'max':>7} | "
          f"{'stale%':>7}{'p95':>6} | {'PSNR':>6}{'min':>6} | {'max age':>7}")
    for r in rows:
        print(f"{r['name']:<10}{r['avg']:>7.0f}{r['p95']:>7.0f}{r['max']:>7.0f} | "
              f"{r['ms']:>7.1f}{r['ms95']:>7.1f}{r['msmax']:>7.1f} | "
              f"{r['stale']:>7.2f}{r['stale95']:>6.1f} | {r['snr']:>6.1f}{r['snrmin']:>6.1f} | {r['age']:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
