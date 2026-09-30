#!/usr/bin/env python3
"""Build main/demo_clip.bin, the clip the playback demo loops.

Each frame is a video payload exactly as the wire carries it (server/frames.py),
so the device parses it with av_video_decode(). Container:

    "DCL1" | u16 frames | u16 reserved | u32 length[frames] | frame 0 | frame 1 ...

    python tools/make_demo_clip.py [--frames 96]
"""
import argparse
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import frames  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--out", default=str(ROOT / "main" / "demo_clip.bin"))
    args = ap.parse_args()

    # Three complexity tiers, equal frame counts, so the demo can report the
    # frame rate per tier. The header's reserved field carries the tier count.
    per = args.frames // 3
    sources = [
        f"testsrc2=size={frames.WIDTH}x{frames.HEIGHT}:rate=24",
        f"mandelbrot=size={frames.WIDTH}x{frames.HEIGHT}:rate=24:end_pts={per}:end_scale=0.3",
        f"mandelbrot=size={frames.WIDTH}x{frames.HEIGHT}:rate=24:end_pts={per}:end_scale=0.003",
    ]
    payloads = []
    step = frames.FRAME_PIXELS
    for tier, source in enumerate(sources):
        cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source,
               "-frames:v", str(per), "-vf", f"{frames.FIT},format=rgb8",
               "-pix_fmt", "rgb8", "-sws_dither", "none", "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
        assert len(raw) == per * step, (tier, len(raw))
        tier_sizes = []
        for i in range(per):
            stripes = frames.compress_stripes(raw[i * step:(i + 1) * step])
            table = struct.pack(f">{len(stripes)}H", *(len(x) for x in stripes))
            payloads.append(bytes([0, len(stripes)]) + table + b"".join(stripes))
            tier_sizes.append(len(payloads[-1]))
        print(f"tier {tier}: avg {sum(tier_sizes)//per} max {max(tier_sizes)}")
    body = b"".join(payloads)
    head = b"DCL1" + struct.pack("<HH", len(payloads), 3)
    head += struct.pack(f"<{len(payloads)}I", *(len(p) for p in payloads))
    Path(args.out).write_bytes(head + body)
    sizes = [len(p) for p in payloads]
    print(f"{args.out}: {len(payloads)} frames, {len(head) + len(body)} bytes, "
          f"avg {sum(sizes) // len(sizes)} max {max(sizes)} (limit {frames.VIDEO_MAX})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
