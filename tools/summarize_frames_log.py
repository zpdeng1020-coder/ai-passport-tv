#!/usr/bin/env python3
"""Summarise the device's FRAMES lines from a serial_capture log.

Windows with no frames (fps_x10=0) and their neighbours are reconnect gaps between
server cycles, not performance, and are excluded. inc/nobuf are cumulative on the device, so they are
reported as a per-window rate from the first to the last kept window.

    python tools/summarize_frames_log.py dev.log [dev2.log ...]
"""
import re
import statistics as st
import sys

KV = re.compile(r"(\w+)=(\d+)")

for path in sys.argv[1:]:
    rows = []
    for line in open(path, encoding="utf-8", errors="replace"):
        if "FRAMES fps_x10" in line:
            d = {k: int(v) for k, v in KV.findall(line.split("FRAMES", 1)[1])}
            rows.append(d)
    # A window next to a fps=0 window is a server reconnect boundary and only
    # partly full; leaving it in reads as a slow device.
    keep = [r for i, r in enumerate(rows) if r["fps_x10"] > 0
            and (i == 0 or rows[i - 1]["fps_x10"] > 0)
            and (i + 1 == len(rows) or rows[i + 1]["fps_x10"] > 0)
            and r["rx_frames"] > 60]
    if len(keep) < 3:
        print(f"{path}: only {len(keep)} usable windows"); continue
    fps = [r["fps_x10"] / 10 for r in keep]
    span = len(keep) - 1
    inc = (keep[-1]["inc"] - keep[0]["inc"]) / span
    nb = (keep[-1]["nobuf"] - keep[0]["nobuf"]) / span
    print(f"{path}: {len(keep)} windows of 5 s  fps mean {st.mean(fps):.1f} min {min(fps):.1f} "
          f"max {max(fps):.1f}  inc/win {inc:.1f}  nobuf/win {nb:.1f}  "
          f"inflate {st.mean(r['inf_x100'] for r in keep)/100:.1f} ms  "
          f"expand {st.mean(r['exp_x100'] for r in keep)/100:.1f} ms  "
          f"submit {st.mean(r['sub_x100'] for r in keep)/100:.1f} ms  "
          f"skipped stripes/win {st.mean(r.get('skip_stripes',0) for r in keep):.0f}  "
          f"windows >=29 fps: {sum(f>=29 for f in fps)}/{len(fps)}")
