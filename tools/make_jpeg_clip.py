#!/usr/bin/env python3
"""Build main/jpeg_test.bin: baseline 4:2:0 JPEG frames for the C3 decode benchmark.

    "JPG1" | u16 frames | u16 tiers | u32 length[frames] | frame 0 | frame 1 ...

Three content tiers (test pattern, two mandelbrot zooms), FRAMES_PER_TIER each,
at 320x180. The quality is searched per tier so the average frame lands near
--target bytes, because decode time depends on how much entropy-coded data there
is, and the question is the speed at the size the architecture note proposes.

    python tools/make_jpeg_clip.py [--target 5500] [--frames 8]
"""
import argparse
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
W, H = 320, 180


def encode(source: str, frames: int, q: int) -> list[bytes]:
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-frames:v", str(frames),
           "-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-q:v", str(q), "-f", "image2pipe", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    cuts = [i for i in range(len(data) - 1) if data[i] == 0xFF and data[i + 1] == 0xD8]
    cuts.append(len(data))
    return [data[a:b] for a, b in zip(cuts, cuts[1:])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=5500)
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "main" / "jpeg_test.bin"))
    args = ap.parse_args()
    per = args.frames
    sources = [
        f"testsrc2=size={W}x{H}:rate=24",
        f"mandelbrot=size={W}x{H}:rate=24:end_pts={per}:end_scale=0.3",
        f"mandelbrot=size={W}x{H}:rate=24:end_pts={per}:end_scale=0.003",
    ]
    out = []
    for tier, src in enumerate(sources):
        best = None
        for q in range(2, 32):
            fr = encode(src, per, q)
            avg = sum(map(len, fr)) / len(fr)
            if best is None or abs(avg - args.target) < abs(best[1] - args.target):
                best = (q, avg, fr)
        q, avg, fr = best
        print(f"tier {tier}: q={q} avg {avg:.0f} max {max(map(len, fr))} min {min(map(len, fr))}")
        out += fr
    head = b"JPG1" + struct.pack("<HH", len(out), 3) + struct.pack(f"<{len(out)}I", *map(len, out))
    Path(args.out).write_bytes(head + b"".join(out))
    print(f"{args.out}: {len(out)} frames, {len(head) + sum(map(len, out))} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
