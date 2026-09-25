"""Compare the integer geometry family without touching the device.

Every candidate geometry is "send WxH, enlarge to 320x240 by an exact integer
ratio", which is the whole family the firmware can draw without filtering. The
question is which member of it is best, and it cannot be answered by the byte
cost alone: sending fewer columns buys frames with sharpness, and the figure
that says how much sharpness was spent is the error against the panel-sized
picture -- not the error against the smaller one, which would flatter every
candidate equally.

So each geometry is decoded, letterboxed by its own aspect ratio, quantised onto
the fixed 3-3-2 grid the device uses, and then enlarged to 320x240 exactly the
way `enlarge_stripe()` in main/av_player.c does it: nearest neighbour, backwards,
by an integer ratio. Frames are cut into stripes and zlib-compressed the way
server/frames.py does, so the byte figures are the same quantity the link
carries.

Two things this cannot measure, and they are why it is a shortlist rather than
an answer: bytes-per-frame varies with content, so the achievable frame rate has
to be confirmed on a real channel; and the last row of the table is about
appearance, which should be looked at rather than inferred.

    python3 tools/geometry_sweep.py --source /tmp/clip.ts
"""

from __future__ import annotations

import argparse
import subprocess
import zlib

import numpy as np

PANEL_W, PANEL_H = 320, 240
PANEL_STRIPE_ROWS = 16


def decode(path: str, seconds: float, fps: int, width: int, height: int,
           rgb8: bool):
    """Decode a clip letterboxed into `width`x`height`, as raw bytes.

    The filter chain is the one server/frames.py builds: fit the source's own
    aspect ratio inside the box, centre it, and leave the rest black. `rgb8` is
    ffmpeg's fixed 3-3-2 grid, the same palette the device expands indices
    through, so quantising here measures the same loss the device would show.

    A frame's stride is asked of ffmpeg rather than assumed to be the row width.
    For an odd or awkward geometry it pads each row to an alignment, and reading
    the buffer at `width` bytes a row then shears the picture -- which would
    quietly corrupt every number below rather than fail.
    """
    fmt = "rgb8" if rgb8 else "rgb24"
    # The conversion happens before the padding, and that order is not cosmetic.
    # ffmpeg pads in whatever the frame's own format is, and this source is
    # 4:2:0 -- which has no notion of an odd row, so a pad target of 180x135
    # silently comes back 180x134. Converting first puts the padding in RGB,
    # where every row exists, and the geometry that was asked for is the
    # geometry that arrives.
    fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
           f"format={fmt},"
           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    chain = f"fps={fps},{fit}"
    # A count of frames rather than a duration: -t cuts wherever it lands, and
    # on a geometry whose rows do not divide the cut it returns a partial frame,
    # which then shears everything read after it.
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
         "-t", str(seconds), "-vf", chain, "-frames:v", str(int(seconds * fps)),
         "-pix_fmt", fmt, "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, check=True).stdout
    per_frame = width * height * 3
    if len(out) % per_frame:
        raise SystemExit(
            f"{width}x{height}: ffmpeg returned {len(out)} bytes, which is not "
            f"a whole number of {per_frame}-byte frames")
    return np.frombuffer(out, np.uint8).reshape(-1, height, width, 3)


def enlarge(sent: np.ndarray, width: int, height: int) -> np.ndarray:
    """The device's own enlargement: nearest neighbour, exact integer ratio.

    Written the same way round as the C, backwards in y and x, because the copy
    is in place there and the direction is load-bearing. Here the arrays are
    separate, so the direction cannot matter to the result -- but keeping it
    identical means a mistake in the C would show up as a difference here too
    rather than being quietly corrected.
    """
    out = np.empty((sent.shape[0], PANEL_H, PANEL_W, 3), np.uint8)
    for y in range(PANEL_H - 1, -1, -1):
        src_y = y * height // PANEL_H
        for x in range(PANEL_W):
            out[:, y, x] = sent[:, src_y, x * width // PANEL_W]
    return out


def frame_bytes(sent: np.ndarray, width: int, height: int, stripe: int) -> float:
    """Mean compressed bytes a frame, cut into stripes as the server cuts them."""
    total = 0
    pixels = width * stripe
    for frame in sent:
        flat = frame.reshape(-1)
        total += sum(len(zlib.compress(flat[i * pixels:(i + 1) * pixels], 1))
                     for i in range(height // stripe))
    return total / len(sent)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="a clip, ideally a recording "
                                                   "of a real channel")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fps", type=int, default=6)
    ap.add_argument("--columns", default=",".join(str(20 * s) for s in range(8, 17)))
    args = ap.parse_args()

    # Every geometry in the family. A panel stripe is sixteen rows, and the
    # picture supplies s of them, so a picture stripe is s rows and fifteen of
    # them make 15*s = h -- which is why every multiple of twenty columns is
    # geometrically legal, and why the family is not restricted to the ratios
    # that divide evenly. Those are the interesting cases precisely because the
    # ratio does not divide: 320/200 is 8/5, and 120 of the panel's 320 columns
    # are then copies of a neighbour.
    geometries = []
    for s in (int(c) // 20 for c in args.columns.split(",")):
        w, h, stripe = 20 * s, 15 * s, s
        # The panel's 240 rows are fifteen stripes of sixteen, and each picture
        # stripe becomes one of them, so a frame has to be fifteen picture
        # stripes tall. 15*s/s is fifteen for every s, which is what makes the
        # whole family legal rather than a handful of round ratios.
        if stripe > 0 and h // stripe == PANEL_H // PANEL_STRIPE_ROWS:
            geometries.append((w, h, stripe))

    print(f"{args.source}  {args.seconds:g}s @ {args.fps}fps "
          f"→ panel {PANEL_W}x{PANEL_H}")
    print(f"{'sends':<10}{'enlarge':>8}{'B/frame':>9}{'B/pixel':>9}"
          f"{'@145kB/s':>10}{'MAE':>7}{'cols':>6}{'cols/320':>9}{'uniform':>9}")

    for w, h, stripe in geometries:
        sent = decode(args.source, args.seconds, args.fps, w, h, rgb8=True)
        truth = decode(args.source, args.seconds, args.fps, PANEL_W, PANEL_H,
                       rgb8=True)
        shown = enlarge(sent, w, h)
        frame_b = frame_bytes(sent, w, h, stripe)
        mae = float(np.abs(shown.astype(np.int16) - truth.astype(np.int16)).mean())
        # Distinct columns actually on the panel: how many source columns
        # survived, which is what reads as softness. Counted from a frame in
        # the middle rather than the first, which may be a title card.
        #
        # The count is capped at the number that could possibly be there. Two
        # source columns that happen to carry the same colour are one distinct
        # column of picture, and counting distinct byte patterns would report
        # fewer than were sent and call it a rendering fault.
        probe = shown[len(shown) // 2]
        patterns = {c.tobytes() for c in probe.transpose(1, 0, 2)}
        cols = min(len(patterns), w)
        # Whether every source column got the same number of screen columns.
        # An uneven ratio is what makes straight edges look ragged.
        counts = [sum(1 for x in range(PANEL_W) if x * w // PANEL_W == c)
                  for c in range(w)]
        uniform = "yes" if max(counts) == min(counts) else f"{min(counts)}-{max(counts)}"
        print(f"{w}x{h:<6}{PANEL_W // w if w else 1:>8}{frame_b:>9.0f}"
              f"{frame_b / (w * h):>9.3f}{145000 / frame_b:>10.1f}"
              f"{mae:>7.1f}{cols:>6}{cols / PANEL_W:>9.2f}{uniform:>9}")

    print("\nB/pixel rises as the picture shrinks, so enlarging costs more bytes "
          "per pixel\nthan it saves. 'cols' is how many distinct source columns "
          "reach the panel;\n'uniform' is how evenly they are spread -- a range "
          "means some columns are\ndoubled and others are not, which is what "
          "ragged edges look like.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
