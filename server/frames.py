"""The indexed picture: how a frame is cut, compressed and packed.

The device draws a 320x240 picture from 256-colour indices, one byte a pixel.
Indices rather than colours because the picture then compresses like a fax
rather than like a photograph, and because turning an index into a colour on
the device is a table lookup instead of an inverse transform. The device has no
JPEG decoder in this path at all.

A frame is cut into fifteen stripes of sixteen rows -- 320x16, so 5120 index
bytes each -- and each stripe is compressed on its own. Fifteen because 240
rows divided by sixteen is fifteen, and the panel takes its rows in sixteens:

  * the panel starts updating while the rest of the frame is still arriving,
    which matters on a link that takes tens of milliseconds per frame;
  * the device needs no scratch buffer larger than one stripe.

Splitting costs 2.2% more bytes than compressing the frame in one stream,
measured, which is a good price for both.

Packets carry a run of consecutive stripes:

    [u8 first stripe][u8 count][u16 length * count][compressed stripes]

The lengths are big-endian and count the compressed bytes that follow. A
constant number of stripes per packet is safe rather than a budget worked out
at run time: deflate cannot inflate incompressible input by more than a few
bytes per block, so the worst a stripe can be is its own size plus a handful.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import zlib

# What arrives on the wire, at the panel's own size. See the note at
# AV_VIDEO_WIDTH in main/av_protocol.h: the device enlarges this by the ratio in
# AV_ENLARGE_NUM/AV_ENLARGE_DEN on its way to the panel, and at 1/1 it enlarges
# nothing, which is what puts a source pixel in every screen pixel. Sending
# fewer columns and stretching them buys frame rate and spends sharpness; the
# viewer, shown that trade, asked for the sharpness.
# The two ends must agree exactly -- the device checks the size in CONFIG and
# refuses a stream that disagrees -- so these are the values in that header.
# The geometry the firmware was built with, named rather than specified as
# three numbers that have to agree.
#
# The device compiles its geometry in, so the two ends must match exactly or
# every session is refused -- and a mismatch does not announce itself as a
# configuration error, it looks like a broken link: the device connects,
# authenticates and drops. One name is one chance to get it right.
#
#     320x240   the default: every screen pixel is the source's own
#     280x210   seven eighths the source's own, 36% more content pixels
#     240x180   three quarters the source's own, the fastest of the three
#
# 320x240 is the default because every other ratio leaves an artefact. Spreading
# fewer columns across 320 copies some source pixels once and their neighbours
# twice, and the irregularity reads as a grid of dots over the whole picture --
# worse, not better, the closer the ratio is to one. The viewer rejected both
# 8/7 and 4/3 on sight for exactly that and asked for native.
#
# The cost is frame rate, paid in bytes rather than in computation: a native
# frame is 20.4 kB against 12.1 kB at 240x180, so the same link carries about
# half as many.
#
# tools/check_config_agreement.py prints which pane the firmware was built for,
# so the two can be compared without flashing anything.
GEOMETRIES = {"320x240": (320, 240, 16), "280x210": (280, 210, 14),
              "240x180": (240, 180, 12), "320x180": (320, 180, 12)}
_NAME = os.environ.get("TV_GEOMETRY", "320x180")
if _NAME not in GEOMETRIES:
    raise SystemExit(f"TV_GEOMETRY={_NAME} is not a geometry this firmware has: "
                     f"choose one of {', '.join(sorted(GEOMETRIES))}")
WIDTH, HEIGHT, STRIPE_ROWS = GEOMETRIES[_NAME]
STRIPES = HEIGHT // STRIPE_ROWS
STRIPE_PIXELS = WIDTH * STRIPE_ROWS
FRAME_PIXELS = WIDTH * HEIGHT

# How ffmpeg hands the picture over: one index byte a pixel and nothing else.
#
# This used to be pal8, which carries a palette and writes it once per frame
# after the pixels -- a stride of 77824, of which the last 1024 bytes were read
# and dropped. rgb8 carries no palette at all, so the trailer is gone and the
# frame is exactly its pixels.
#
# The two formats were once argued about in the other direction, and the history
# is worth keeping because the conclusion reversed. rgb8 is not "8-bit colour":
# it is ffmpeg's own fixed 3-3-2 grid, so asking for it quantises every pixel
# onto that grid. When the palette was adaptive that was fatal -- the indices
# were chosen against a palette-sampled image and would then be looked up in a
# different table. It is now the point: the grid is the palette, both ends know
# it without being told, and it cannot go stale. See FIXED_PALETTE in live.py.
TRAILER_BYTES = 0
FRAME_BYTES = FRAME_PIXELS + TRAILER_BYTES

# How large a packet may be, and how large the packer aims for.
#
# **The numbers in this paragraph are from an older geometry and the paragraph
# is kept only as the history of how the ceiling was set. Do not use them.**
#
# It was written when the picture was 240x180 with a 12288-byte packet ceiling,
# and it says the device is short of packets a second rather than bytes: its
# receive path tops out near 72 a second whatever their size, with the sound
# spending 50 of those at one 640-byte chunk every 20 ms. **The audio chunk is
# 40 ms and 1280 bytes now, not 20 ms and 640**, so that arithmetic describes a
# protocol this one is not; and at 320x240 with a 22528-byte ceiling, five
# frames a second costs five picture packets, not five of a scarce twenty.
#
# What was measured since does agree with the conclusion: splitting a frame into
# two packets made things distinctly worse (underruns 5 to 9, resets 11 to 24,
# twelve of thirteen sessions failed), so "packets are not the constraint, bytes
# are" holds. That is a measurement about this geometry; the 72 is not.
#
# The other direction is bounded too, and this is the part that took longest to
# see. One task reads the socket, so while a picture packet is arriving the sound
# is not being read at all. A packet that takes longer to cross than the sound
# can go without therefore costs the sound, and the device ends the session on
# that. Measured: a 23232-byte packet was still only 3790 bytes read after
# 563 ms, and the audio underran behind it.
#
# A frame of live television measures 29 to 43 KB. Packing to the protocol
# ceiling gives one or two packets of 20 to 42 kB, which is into that second
# limit; packing to 4 KB gives eleven small packets, which blows the packet
# budget and starves the picture. Twelve kilobytes is the middle: the same frame
# becomes three packets of 10 to 12 kB, each crossing in about 200 ms.
#
# VIDEO_MAX must equal AV_VIDEO_MAX in main/av_protocol.h. A packet over it ends
# the session rather than being trimmed, so it is a hard limit and not a target.
VIDEO_MAX = 22528

# What the packer aims for. Lower than VIDEO_MAX on purpose: the ceiling is what
# the device will accept, this is what keeps each packet short enough for the
# sound to keep flowing. See above for how the figure was arrived at.
#
# Overridable so the two sizes can be compared on one machine without editing a
# tracked file -- the same reason TV_FPS is. A measurement that needs a rebuild
# between its two halves is a measurement that cannot be repeated.
PACKET_TARGET_BYTES = int(os.environ.get("TV_PACKET_TARGET", "12288"))

# Must equal AV_PALETTE_ENTRIES in main/av_protocol.h.
PALETTE_ENTRIES = 256
PALETTE_BYTES = PALETTE_ENTRIES * 2

_HEADER = struct.Struct(">BB")

# Compression level 6, and the figure that chose it is worth re-measuring
# whenever the geometry changes -- it did, and it moved.
#
# Level 1 was chosen when a frame was 320x240 of JPEG-era data and the gap to
# level 6 measured under a fifth of a percent, which was not worth the processor
# time. At 240x180 indexed that is no longer true: measured over 24 frames of a
# live channel, level 1 costs 13412 bytes a frame, level 3 costs 12905, level 6
# costs 12147, and level 9 costs 12141. Six is 9.4% off every frame for 0.9 ms
# of compression instead of 0.4 -- under a hundredth of a frame's slot on a
# machine that is merely decoding one stream.
#
# What those bytes buy is frame rate directly, since the link's capacity is what
# it is: 9.4% fewer bytes per frame is 9.4% more frames for the same link.
_COMPRESS_LEVEL = 6


def compress_stripes(frame: bytes) -> list[bytes]:
    """Cut a frame into stripes and compress each one."""
    if len(frame) != FRAME_PIXELS:
        raise ValueError(f"frame is {len(frame)} bytes, expected {FRAME_PIXELS}")
    return [
        zlib.compress(frame[at * STRIPE_PIXELS:(at + 1) * STRIPE_PIXELS], _COMPRESS_LEVEL)
        for at in range(STRIPES)
    ]


def packet(first: int, compressed: list[bytes]) -> bytes:
    """Build one payload from consecutive compressed stripes."""
    if not compressed:
        raise ValueError("a packet carries at least one stripe")
    if first + len(compressed) > STRIPES:
        raise ValueError(f"stripes {first}+{len(compressed)} run past the frame")
    table = struct.pack(f">{len(compressed)}H", *(len(s) for s in compressed))
    payload = _HEADER.pack(first, len(compressed)) + table + b"".join(compressed)
    if len(payload) > VIDEO_MAX:
        raise ValueError(f"packet is {len(payload)} bytes, over the {VIDEO_MAX} limit")
    return payload


def frame_packets(frame: bytes) -> list[bytes]:
    """Everything a frame needs, in order.

    Packed to a byte budget rather than to a number of stripes, because what a
    packet costs the device is the time it takes to read, and that is bytes.
    A count of stripes is a different read time on every channel: measured from
    300 bytes to 3 KB for one stripe, so the same count is 100 ms on one channel
    and a second on another, and only the slow one is felt.

    A single stripe larger than the budget still goes out on its own -- there is
    nothing to split it into -- which is why the per-stripe sizes are checked
    against the protocol ceiling as well.
    """
    compressed = compress_stripes(frame)
    packets: list[bytes] = []
    run: list[bytes] = []
    run_bytes = 0
    at = 0
    for stripe in compressed:
        # The header and its length table grow by two bytes a stripe, so the
        # budget for the stripes themselves is what is left after them.
        overhead = _HEADER.size + 2 * (len(run) + 1)
        if run and overhead + run_bytes + len(stripe) > PACKET_TARGET_BYTES:
            packets.append(packet(at, run))
            at += len(run)
            run, run_bytes = [], 0
            overhead = _HEADER.size + 2
        if overhead + len(stripe) > VIDEO_MAX:
            raise ValueError(
                f"stripe {at + len(run)} is {len(stripe)} bytes and does not fit "
                f"in a {VIDEO_MAX}-byte packet even alone")
        run.append(stripe)
        run_bytes += len(stripe)
    if run:
        packets.append(packet(at, run))
    return packets


def read_exactly(stream, size: int) -> bytes:
    """Read `size` bytes from a pipe, or fewer if it ends first.

    A pipe hands back whatever is ready, so one read of a whole frame returns a
    short piece as a matter of course and a reader that trusted it would see
    endless truncated frames. The loop lives here rather than in the frame
    reader because that reader is about frames and this is about pipes.
    """
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_frame(stream, stop) -> bytes | None:
    """Read exactly one indexed frame from a raw pipe.

    The stream is rawvideo: no markers, no lengths, just one fixed number of
    bytes a frame. So this reads that number rather than scanning for anything,
    which is also why a short read means the pipe has ended rather than that a
    frame is malformed -- there is nothing to resynchronise to.

    Returns None when the stream ends, including a partial frame at the end: a
    half frame cannot be drawn and is better dropped than padded.
    """
    if stop.is_set():
        return None
    raw = read_exactly(stream, FRAME_PIXELS)
    if len(raw) != FRAME_PIXELS:
        return None
    if TRAILER_BYTES:
        # pal8 wrote a palette block here. rgb8 does not, so this is now dead
        # code kept only so that the two constants stay meaningful together;
        # it is reached the moment TRAILER_BYTES is given a value.
        if len(read_exactly(stream, TRAILER_BYTES)) != TRAILER_BYTES:
            return None
    return raw


# --- Reading a packet back, for tests and for anything that has to check ---

def unpack(payload: bytes) -> list[bytes]:
    """Split a payload into its decompressed stripes."""
    if len(payload) < _HEADER.size:
        raise ValueError("payload shorter than its own header")
    first, count = _HEADER.unpack_from(payload)
    if count == 0 or first + count > STRIPES:
        raise ValueError(f"stripe range {first}+{count} outside the frame")
    end = _HEADER.size + 2 * count
    if len(payload) < end:
        raise ValueError("payload truncated inside its length table")
    lengths = struct.unpack_from(f">{count}H", payload, _HEADER.size)
    if sum(lengths) != len(payload) - end:
        raise ValueError("length table does not match the bytes that follow")
    out = []
    at = end
    for n, length in enumerate(lengths):
        raw = zlib.decompress(payload[at:at + length])
        if len(raw) != STRIPE_PIXELS:
            raise ValueError(f"stripe {first + n} is {len(raw)} bytes, not {STRIPE_PIXELS}")
        out.append(raw)
        at += length
    return out


def palette_bytes(rgb24: bytes) -> bytes:
    """Turn 256 RGB triples into the big-endian RGB565 pairs the device wants.

    ffmpeg's 3-3-2 does not simply scale by 255/7; it multiplies by 36 and 85.
    The two disagree by one count on 220 of the 256 entries and then agree
    again once quantised to RGB565 -- checked against the palette read back out
    of ffmpeg's own rgb8 output, where all 256 matched. So the shift below is
    the same colour as ffmpeg's multiply, and cheaper.
    """
    if len(rgb24) < PALETTE_ENTRIES * 3:
        raise ValueError("a palette is 256 RGB triples")
    out = bytearray()
    for i in range(PALETTE_ENTRIES):
        r, g, b = rgb24[3 * i], rgb24[3 * i + 1], rgb24[3 * i + 2]
        out += struct.pack(">H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return bytes(out)


# --- Choosing the 256 colours -------------------------------------------------

# How much of a source to watch before choosing its palette, and how often to
# sample it while doing so. Long enough to cover more than one shot, short
# enough that changing channel does not feel like a wait: measured at about two
# seconds on a live channel, most of which is spent waiting for the source.
SAMPLE_SECONDS = 1.5
SAMPLE_FPS = 6
# The scale-and-letterbox both the sampler and the picture reader use, so the
# palette is chosen for what is actually shown rather than for the raw frame.
#
# Both, and by construction rather than by agreement. The sampler used to fit
# the frame without first squaring the pixels, while the picture reader did
# both: an anamorphic source was therefore sampled as a full white frame and
# then shown letterboxed, so the palette contained no black, and the black bars
# -- which are part of what the viewer sees -- were mapped to the nearest colour
# they could find, which was white. The bars above and below the picture came
# out white and the whole panel looked lit. Deriving both from one string is
# what stops that from being possible again.
SCALER_FLAGS = os.environ.get("TV_SCALER_FLAGS", "bicubic")
FIT = (f"scale=iw*sar:ih,setsar=1,"
       f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags={SCALER_FLAGS},"
       f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1")

DEFAULT_USER_AGENT = "AptvPlayer-UA"


def input_options(url: str, user_agent: str = "", paced: bool = False) -> list[str]:
    """Options for reading a source, network or local.

    The reconnect options are for a network source and are rejected outright by
    a local file -- "Option reconnect not found", after which the input never
    opens at all -- so they are added only when the source is remote. Tests run
    against local files, which makes the distinction load-bearing rather than
    theoretical.
    """
    options: list[str] = []
    if paced:
        # -re paces input at its native rate. Without it ffmpeg decoded a whole
        # HLS window at once, filled the queues within three seconds and then
        # went quiet, so the device drained them and stalled.
        options += ["-re", "-flags", "low_delay"]
    if url.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        options += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]
    if os.environ.get("TV_STREAM_LOOP") == "1":
        options += ["-stream_loop", "-1"]
    effective_ua = user_agent or (DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
    if effective_ua:
        options += ["-user_agent", effective_ua]
    return options


def palette_command(url: str, ffmpeg: str, user_agent: str, destination: str,
                    sample_seconds: float = SAMPLE_SECONDS) -> list[str]:
    """Ask ffmpeg for a palette suited to this source, written as a PNG.

    A second and a half is sampled rather than a single frame: one frame can be
    an outlier -- a title card, a fade, a graphic -- and the palette chosen from
    it would then be wrong for everything that follows.

    The duration is an INPUT option and has to stay before -i. As an output
    option it does nothing, and the process then reads a live stream for ever:
    palettegen emits one frame, at the very end, so anything waiting for its
    output to finish waits for the channel to stop broadcasting. Measured both
    ways -- 2.2 seconds before -i, hung indefinitely after it.
    """
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-t", str(sample_seconds),
        *input_options(url, user_agent),
        "-i", url,
        # reserve_transparent is on by default, which spends the last of the
        # 256 entries on a transparency colour the device has no use for --
        # and spends it on lime green, so if the encoder ever did emit that
        # index the picture would carry a bright green pixel that is nowhere
        # in the source. Measured on a white test card: with the default the
        # palette came back black + 254 greys + lime, and with it off, black
        # + 255 greys. Every entry is then a colour from the source.
        "-vf", f"fps={SAMPLE_FPS},{FIT},"
               f"palettegen=max_colors={PALETTE_ENTRIES}:stats_mode=single:"
               f"reserve_transparent=0",
        # -update 1 because a still-image muxer refuses to write a second file
        # over the first; palettegen emits one frame and it still has to be told.
        "-frames:v", "1", "-update", "1", "-y", destination,
    ]


def read_palette(png: str, ffmpeg: str) -> bytes:
    """The palette as 256 big-endian RGB565 pairs.

    Read back out of the PNG rather than generated a second time, so the
    palette the device is given and the one the picture is mapped onto are the
    same bytes by construction rather than because two calculations agree.
    """
    raw = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", png, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=30, check=True).stdout
    return palette_bytes(raw)


def build_palette(url: str, ffmpeg: str, user_agent: str, png: str,
                  timeout: float = 40) -> bytes:
    """Sample the source, write a palette PNG, and return the device's copy.

    The timeout is a backstop, not a schedule: this takes about two seconds on a
    live channel, and anything past the timeout is a source that is not
    answering. Without one, changing to a dead channel would hang instead of
    failing where the device can see it and reconnect.
    """
    if shutil.which(ffmpeg) is None and not ffmpeg.startswith("/"):
        raise RuntimeError(f"ffmpeg not found: {ffmpeg}")
    try:
        subprocess.run(palette_command(url, ffmpeg, user_agent, png),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout, check=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("palette generation timed out") from None
    return read_palette(png, ffmpeg)
