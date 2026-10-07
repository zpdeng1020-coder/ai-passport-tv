"""Give picture detail up perceptually when a frame does not fit its byte target.

The frame arrives as ffmpeg's own 3-3-2 indices, and that picture is the
reference: when it fits the target it is sent unchanged, byte for byte. When it
does not, `frames.encode_within` used to coarsen it by rounding whole colour
channels a step at a time, which lands far below the target and falls to noise at
the tightest ones. Here each pixel may instead keep the value the device already
shows, or its left neighbour's, when that palette colour is close enough to the
reference pixel's, so deflate sees more repeats and the picture loses detail
gradually. How close is a threshold searched per frame, starting from the level
the previous frame needed.

Needs numpy for speed; `AVAILABLE` says whether it imported.
"""

from __future__ import annotations

try:
    import numpy as np
except ImportError:  # the server stays usable without it, on the colour ladder
    np = None

from . import frames

AVAILABLE = np is not None

WIDTH, HEIGHT = frames.WIDTH, frames.HEIGHT

# Allowed distance between the reference colour and the one used instead, in
# weighted squared RGB (green counts most, blue least). The nearest two palette
# colours are one red step (3888) or one green step (7776) or one blue step (7225)
# apart, so the levels start there: below it nothing could be replaced. Level 0 is
# the reference itself.
LEVELS = (0, 3900, 7800, 11700, 15600, 23400, 31200, 46800, 70000, 110000)

if AVAILABLE:
    # The palette both ends implement: red and green in steps of 36, blue in steps
    # of 85 (frames.palette_bytes documents where those numbers come from), with
    # the weights folded into the coordinates so a distance is a plain one.
    _ROOT_WEIGHT = np.sqrt(np.array([3.0, 6.0, 1.0], np.float32))
    _PALETTE_W = np.array([((i >> 5) * 36, ((i >> 2) & 7) * 36, (i & 3) * 85)
                           for i in range(256)], np.float32) * _ROOT_WEIGHT


def _snap(reference: "np.ndarray", base: "np.ndarray", shown: "np.ndarray | None",
          held_distance: "np.ndarray | None", threshold: float) -> "np.ndarray":
    """`base` with pixels replaced by a cheaper index that is close enough.

    First the value the device already holds, wherever it is within `threshold`
    of the reference; then, column by column, the pixel to the left. Both make the
    stripe repeat itself, which is all deflate needs. The left pass is sequential
    on purpose: a pixel may copy a neighbour that itself just copied its own.
    """
    out = base.copy()
    if shown is not None:
        keep = held_distance <= threshold
        out[keep] = shown[keep]
    columns = np.ascontiguousarray(out.T)
    source = np.ascontiguousarray(reference.transpose(1, 0, 2))
    for x in range(1, WIDTH):
        left = columns[x - 1]
        gap = _PALETTE_W[left] - source[x]
        near = (np.einsum("ij,ij->i", gap, gap) <= threshold) & (columns[x] != left)
        np.copyto(columns[x], left, where=near)
    return np.ascontiguousarray(columns.T)


def encode_within(raw: bytes, shown: bytes | None, tick: int, target: int,
                  hint: int = 0,
                  min_diff: float = frames.DELTA_MAX_DIFF) -> tuple[list[bytes], bytes, int]:
    """Same contract as `frames.encode_within`: (stripes to send, what they draw, level).

    Level 0 is the frame as it came, and is always tried first, so a frame that
    fits costs what `frames.encode_within` costs and content that has become easy
    is back at full detail at once. Otherwise the search starts at `hint`, the level
    the previous frame needed, because frames that need one come in runs: if it
    fits, one level lower is tried, and if not, the levels above it are bisected.
    """
    if len(raw) != frames.FRAME_PIXELS:
        raise ValueError(f"frame is {len(raw)} bytes, expected {frames.FRAME_PIXELS}")
    top = len(LEVELS) - 1

    def attempt(level: int):
        drawn = raw if level == 0 else _snap(reference, base, held, held_distance,
                                             LEVELS[level]).tobytes()
        return frames.choose_stripes(drawn, shown, tick, 1 << 30, min_diff), drawn

    def fits(result) -> bool:
        return frames._wire_size(result[0]) <= target

    result = attempt(0)
    if fits(result):
        return result[0], result[1], 0

    base = np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH)
    reference = _PALETTE_W[base]
    held = held_distance = None
    if shown is not None:
        held = np.frombuffer(shown, np.uint8).reshape(HEIGHT, WIDTH)
        gap = _PALETTE_W[held] - reference
        held_distance = np.einsum("hwc,hwc->hw", gap, gap)

    start = min(max(hint, 1), top)
    result = attempt(start)
    if fits(result):
        if start > 1:
            lower = attempt(start - 1)
            if fits(lower):
                return lower[0], lower[1], start - 1
        return result[0], result[1], start
    low, high, best = start + 1, top, None
    while low <= high:
        probe = (low + high) // 2
        result = attempt(probe)
        if fits(result):
            best, high = (result[0], result[1], probe), probe - 1
        else:
            low = probe + 1
    if best is not None:
        return best
    drawn = attempt(top)[1]
    return frames.choose_stripes(drawn, shown, tick, target, min_diff), drawn, top
