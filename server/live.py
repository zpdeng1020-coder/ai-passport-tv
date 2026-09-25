"""Live HLS transcoding into the indexed picture the device draws.

Two ffmpeg processes per channel, because they have different jobs and the
picture one has to be restarted when the palette changes:

  * one decodes the source, scales it to the panel, maps it onto a 256-colour
    palette and writes 320x240 index bytes to a pipe, a frame at a time;
  * one decodes the audio to 16 kHz mono PCM and writes it to another.

Two reader threads frame the bytes; the session sender paces them against one
monotonic origin so audio PTS stays contiguous and video is never allowed to
stall the sound.

The picture is full resolution now and the device does no scaling: it inflates
each stripe and looks each index up in the palette. That is why the palette is
built per channel and sent to the device before the first frame.

This is a LAN test service, not a hardened transcoder: URLs come only from the
fixed CHANNELS table, the stream is not recorded, and no credentials are used.
"""

from __future__ import annotations

import collections
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import frames, pts
from .timeline import (BASIS_COMMON_DECODE, BASIS_LAUNCH, ContentTimeline,
                       SessionClock, SourceState, VERDICT_IN_HOLE,
                       VERDICT_NOT_YET, VERDICT_PLACED)
from .media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, FPS, START_DELAY_MS,
                    VIDEO_LEAD_MS, WIDTH as VIDEO_WIDTH)
from .protocol import AUDIO_BYTES, VIDEO_MAX
# Device-side limits, mirroring main/av_protocol.h. A channel id the firmware
# would skip must be rejected here instead, where it can be reported; the
# tests cross-check these against the header so the two cannot drift.
TV_CONTROL_MAX = 7168
# Must match main/av_protocol.h. The device rejects a list longer than its own
# limit, and a server willing to send more turns that difference into a session
# that dies at the boundary instead of a channel that is simply not offered.
TV_CHANNEL_MAX = 128
TV_CHANNEL_ID_MAX = 16
# Ceiling on the bytes one channel table line may occupy, used only to bound the
# read of channels.txt. It is generous next to a real line (a long name plus a
# long URL) so a legal file is never truncated, and the four-field form with a
# User-Agent fits inside it too.
MAX_CHANNEL_LINE_BYTES = 512
# NOT defined here. It is imported from media.py at the top of this file, and a
# second definition used to sit on this line saying 20 while the imported one
# says 40 -- which quietly doubled every quantity in this module derived from a
# number of chunks.
#
# What it cost, measured: `PREBUFFER_CHUNKS` came out at 400 instead of 200, so
# a session waited for 400 chunks of 40 ms -- sixteen seconds -- where eight was
# intended, and the wait is the whole of a channel change. That is the "channel
# changes are slow" the viewer reported. The same shadowing also overstated the
# audio queue's capacity in the depth calculation and understated the sound's
# queue depth in `audio_depth_ms`, which is what the picture is aligned to.
#
# The lesson is the one this project keeps relearning in a new place: a name
# defined twice does not fail, it just picks one, and the one it picks is the
# nearer. The import stays and nothing here may redefine it.
AUDIO_RATE = 16000
# The send leads come from media.py so the pre-generated scheduler and this live
# sender cannot disagree about them.
VIDEO_LATE_DROP_MS = 200
# Kept below the device's AUDIO_UNDERRUN_MS (300 ms) so the server gives up on a
# starved origin before the device concludes its stream is broken: otherwise the
# device logs "audio underrun" while this side still believes it is mid-stream.
# The device is the one that reconnects, so if it gives up first the server is
# left holding a socket that nobody is reading.
AUDIO_LATE_RESET_MS = 250
# How far the sound may lead the wall clock, and therefore how much of the
# device's buffer is actually kept full. It matters more than it looks: the
# sender is one loop, so the sound waits while a picture packet is written, and
# that wait comes out of this cushion.
#
# Sized from the device's own figures rather than from the 400 ms this comment
# used to name, which was true of an older firmware. The device now queues
# PCM_QUEUE (48) chunks of 20 ms -- 960 ms -- and its audio task tolerates a
# further AUDIO_UNDERRUN_MS (300) of silence before ending the session, with
# about 90 ms of DMA behind that.
#
# The failure this prevents, measured: a picture packet is 12 to 17 kB and takes
# roughly 300 ms to write at the rate this link manages. With the lead at 240 ms
# the cushion was smaller than one packet, so every frame began by draining it,
# and the session ended about nine seconds in -- long enough for the deficit to
# accumulate rather than be ridden out.
#
# pocket-tv, for comparison, sends 450 ms ahead into a 512 ms FIFO, and closes
# the loop by reading the device's reported queue depth. This sender has no such
# feedback, so the figure has to be chosen to leave room rather than to fill
# what is there.
#
# The device stops reading the socket once its audio queue passes PCM_QUEUE-4,
# which is 44 of 48 chunks -- 880 ms (main/av_player.c). Filling to that line is
# therefore not free even though the buffer would hold it: every chunk above it
# is time the receiver spends in a ten-millisecond sleep with the socket
# untouched, and a picture packet cannot be read during that sleep. Measured
# with the lead at 480 ms: 2799 ms of a ten-second window spent in exactly that
# wait, and the device completed 14 frames where 20 were sent.
#
# The figure to keep in mind is not this one on its own but the depth it puts in
# the device's queue, which is this plus AUDIO_LEAD_MS -- the lead is how far
# ahead the sender is, and the lookahead is how much further it may run.
#
#     queue depth = AUDIO_LEAD_MS + AUDIO_MAX_LOOKAHEAD_MS
#
# The device stops reading the socket once its audio queue passes PCM_QUEUE-4,
# which is 26 chunks of 20 ms. A depth of 200 + 320 = 520 ms is 26 chunks -- on
# the line exactly -- so the queue arrived at the flow-control threshold and
# stayed there: measured `queue_high=27` and 2799 ms of a ten-second window
# spent sleeping in that wait, during which no picture packet could be read
# either.
#
# The depth above is what matters, and 360 ms was too near the line for the
# reason the picture's packets create. One task reads both streams, so a picture
# packet in flight is 200 ms or so in which nothing drains the device's queue;
# the device calls an underrun after 300 ms of silence, so a depth of 360 ms
# left only 60 ms of real slack. Measured: every underrun was preceded by a
# ten-second interval with the sound full, and the gap at failure was 305 ms
# every time.
#
# Twelve chunks of lookahead on top of the lead puts the depth at 480 ms, which
# is twelve of the device's eighteen chunks. The queue is shallower in count
# than it was (PCM_QUEUE is 18) but each chunk is 40 ms rather than 20, so it
# holds 720 ms -- more audio than the thirty 20 ms chunks it replaced.
# Overridable so the depth can be swept without a rebuild, the same reason
# TV_FPS is: a measurement that needs a rebuild between its two halves is one
# nobody runs twice.
AUDIO_MAX_LOOKAHEAD_MS = int(os.environ.get("TV_AUDIO_LOOKAHEAD_MS", "280"))
# These queues are the whole reason playback can be steady. An HLS origin does
# not deliver a smooth stream: each segment arrives as a burst, so ffmpeg emits
# several seconds of audio at once and then waits for the next segment. With a
# buffer only a few seconds deep the queue hit its ceiling during a burst (audio
# was discarded) and then drained to nothing before the next segment, which the
# device saw as an underrun and reset the session. A deep buffer absorbs the
# burst and rides out the wait, at the cost of starting some seconds behind the
# live edge, which is the trade this prototype wants.
PCM_QUEUE_CHUNKS = int(os.environ.get("TV_PCM_QUEUE_CHUNKS", "400"))  # 16 s at 40 ms/chunk
# A frame is now a whole 320x240 indexed picture, 76800 bytes, where a JPEG of
# the same moment was a few kilobytes. The depth is chosen for the burst an HLS
# origin delivers -- several seconds of pictures at once, then a wait -- so this
# is about 15 seconds' worth, or 14 MB. The bound matters: unbounded, a stalled
# reader would grow the queue until the machine ran out of memory.
# How much picture may be held ready, in frames. This is a LATENCY budget as
# much as a buffer, and that is what it was getting wrong.
#
# It was 180, described as fifteen seconds of pictures to ride out the burst an
# HLS origin delivers. Two things make that the wrong size now.
#
# The sender takes the OLDEST frame in the queue (`pop_video` is a popleft), so
# whatever the queue holds is how far behind live the picture is. At 180 frames
# and twelve a second, that is fifteen seconds of delay on a live channel.
#
# And the queue only fills at all when production outruns transmission, which is
# now the normal state: the picture rate is chosen per channel by the rate
# controller, and ffmpeg produces at the ceiling. Measured with the ceiling at
# twelve and a channel the controller settles at four: `video_q` sat at 180 of
# 180 and `prod_drop` climbed to 358 -- every frame produced above what the link
# carries is discarded at the far end of a fifteen-second queue.
#
# Thirty-six frames is about three seconds at the ceiling, which is still more
# than the second or two of jitter a paced HLS origin produces, and it bounds
# the delay at a few seconds instead of fifteen. The sound's own queue is
# untouched: it is the one that has to survive a segment arriving late.
# Sized to hold VIDEO_QUEUE_SECONDS of content at the rate the producer runs,
# with margin. It has to exceed what the picture is held to, or the queue fills
# and discards -- and discarding from the front is exactly the thing that pulls
# the picture out of step with the sound. At twelve frames a second and sixteen
# seconds that is 192 frames, against the 180 this used to be: the old figure
# would have silently truncated the hold on any channel the producer ran fast
# for.
VIDEO_QUEUE_SECONDS = float(os.environ.get("TV_VIDEO_QUEUE_S", "16"))

# How far apart the sound's arrival and the picture's may be and still count as
# the same moment. Wide enough to cover the two files arriving a frame apart
# from two separate ffmpeg processes, narrow enough that a mismatch shows up as
# a dropped frame rather than as a drift.
VIDEO_SYNC_TOLERANCE_S = float(os.environ.get("TV_SYNC_TOLERANCE_S", "0.15"))

VIDEO_QUEUE_FRAMES = int(os.environ.get("TV_VIDEO_QUEUE",
                                        str(int(VIDEO_QUEUE_SECONDS * FPS) + 32)))
# How deep the picture's queue is held, in SECONDS OF CONTENT -- and it is not a
# latency setting, which is what it was first written as.
#
# The two queues are fed by the same source at the same real-time rate and
# drained by the same sender, so their depths are not independent: whatever the
# sound is behind by, the picture has to be behind by too, or the two describe
# different moments. The sound plays as it arrives and cannot wait; the picture
# can, and the depth of its queue is the only thing that makes it.
#
# Measured, and this is how the mistake was caught. With the queue capped at
# eight frames (0.7 seconds) against a sound queue holding 15.7 seconds of
# content, the viewer reported severe desynchronisation and the two figures say
# why: the picture was running fifteen seconds ahead of the sound. In the
# configuration before that cap -- no cap at all, so the queue sat at 84 frames,
# 16.9 seconds against the sound's 16.0 -- the same arithmetic gives 0.9 seconds
# and the viewer had reported it in sync.
#
# So the depth is taken from the sound's own queue rather than being a constant.
# Capping the picture below the sound does not reduce the delay -- the sound's
# delay is set by its own queue and is unaffected -- it only pulls the picture
# out of step with it.
# How much playback is held ready before the first packet goes out, in seconds.
# Depth and startup delay are separate decisions and this is the second one: the
# queues above are capacity, this is what is kept full.
#
# It is also the only cushion against a source that stalls. The sender paces at
# real time and a live source produces at real time, so the level this reaches
# here is the level it holds: the queue is not fed faster than it is drained,
# and the reserve is not rebuilt once spent. A source that stops for longer than
# this empties the queue, and the session then ends at AUDIO_LATE_RESET_MS --
# which is why raising this is the way to tolerate an unreliable channel, and
# why raising the queue depths above is not.
#
# What it costs is the wait on a channel change, and that is the whole of the
# trade: the reserve has to fill at real time before a picture appears. So the
# figure is a setting rather than a constant, because two viewers will weigh it
# differently.
#
#     TV_PREBUFFER_S=4   the default: two seconds either side of a channel change
#     TV_PREBUFFER_S=8   a slower change, more tolerance of a source that stalls
#
# Four rather than eight, and the reason is what the wait is actually for. The
# reserve exists so a source that stops for a moment does not empty the device's
# queue, and an HLS segment arrives every two to six seconds -- so four seconds
# covers one whole segment arriving late, which is the shape of the lapse this
# protects against. Eight was chosen before the sound's queue was understood to
# be the thing that rides out a stall, and it doubles the wait a viewer sees on
# every channel change for margin that the audio queue already provides.
#
# It was 3 before. An older version wrote the reserve as 36 frames, which was
# three seconds at the 12 fps of the day and became eighteen seconds when the
# rate came down -- a device asked to wait that long reported nothing at all.
# Both halves are derived from the one number now, so that cannot recur, and
# they are capped together rather than separately: `prebuffered` waits for the
# sound and the picture at once and starts when the shallower is full, so a
# reserve deeper than either queue would never be reached and every session
# would end at PREBUFFER_TIMEOUT_S reporting "no media from source" -- which
# reads as a dead channel rather than as a setting that is too ambitious.
PREBUFFER_SECONDS = min(float(os.environ.get("TV_PREBUFFER_S", "4")),
                        PCM_QUEUE_CHUNKS * AUDIO_CHUNK_MS / 1000,
                        VIDEO_QUEUE_FRAMES / FPS)
PREBUFFER_CHUNKS = int(PREBUFFER_SECONDS * 1000 / AUDIO_CHUNK_MS)   # 20 ms each
# Frames needed to fill `PREBUFFER_SECONDS`, counted at the rate the SENDER will
# actually draw on, not at the rate the producer was asked for.
#
# These were the same number until the ceiling was raised, and making them differ
# is a correction with three symptoms behind it. `FPS` follows the rate ceiling
# (`media.FPS` is read from `rate.MAX_FPS`), so raising the ceiling from five to
# twelve raised this target from 40 frames to 96 while the source kept producing
# about six a second -- sixteen seconds of waiting on a channel change instead of
# eight. Measured on the device: channel changes took visibly longer, the device
# gave up on a channel before it appeared and moved to the next one, and the
# picture ran twelve or more seconds behind the sound, because the reserve is
# filled before the first frame is sent and the sound starts playing into it.
#
# The reserve is a DURATION -- enough picture and sound to ride out a stalled
# source -- and a duration is what it should be counted in. The rate to count it
# at is the one the link was measured carrying, which is what `START_FPS` is:
# where the median channel lands, and where a session begins.
from .rate import START_FPS as _START_FPS

PREBUFFER_FRAMES = int(PREBUFFER_SECONDS * _START_FPS)
# Only the backstop for an origin that never produces anything at all: the
# prebuffer target, not this, decides when playback normally starts.
PREBUFFER_TIMEOUT_S = 60

# A backstop, not a schedule: the palette takes about two seconds on a live
# channel, and anything past this is a source that is not answering. Without
# it a channel change would hang instead of failing and letting the device
# reconnect.
PALETTE_TIMEOUT_S = 40

# The 256 colours every live picture is drawn in, and the reason they are fixed.
#
# An adaptive palette was the obvious design and it was wrong for this medium.
# It has to be chosen before the first frame and it cannot afterwards change
# without re-indexing every picture and telling the device -- so it is taken
# from about a second and a half at the moment a channel opens and then frozen
# for the whole session. Whatever the viewer is watching later is drawn in the
# colours of whatever happened to be on screen in those first seconds: a fade,
# a title card, a studio caption. Nothing about that is stable, and the symptom
# is exactly what a viewer described as "sometimes it is fine, sometimes it goes
# grey, sometimes green, sometimes brown".
#
# Measured, with a palette sampled from a dim opening scene and applied to a
# colour-bar frame from the same clip: RMSE 275 out of 255. The same frame
# quantised against its own palette: RMSE 0. Against the fixed grid: RMSE 57.
# A stale adaptive palette is not a slight tint, it is a different picture.
#
# The fixed grid cannot go stale, because it does not depend on the content at
# all. It is also cheaper: no second ffmpeg pass, no PNG written and read back,
# no two-second wait before a channel can open, and no way for the two ends to
# disagree -- the colours are a rule, not a message.
#
# What it gives up is the roughly 2x smaller frames an adaptive palette buys on
# content with few colours, and section 4 of docs/development/state-20260915.md
# measures bytes as the real ceiling here. That trade is worth stating plainly:
# this buys a picture that is always the right colour, and pays for it in frame
# rate. It is the right way round -- a wrong-coloured picture at 8 frames a
# second is worse than a correct one at 4.
FIXED_PALETTE = True

# The channel table can be replaced without touching this file: point
# TV_CHANNELS_FILE at a text file, or drop a channels.txt beside the working
# directory, and it is read at startup. Each line is
#
#     id | Display name | https://...
#
# with blank lines and '#' comments ignored. Source addresses change and expire,
# so editing a text file is the expected way to maintain them, not a code change.
CHANNELS_FILE = "channels.txt"
CHANNELS_ENV = "TV_CHANNELS_FILE"
# The built-in fallback uses broadcasters' own public streams. They are more
# dependable than community relays and their terms are clearer, which matters
# because the alternative sources are unverified re-streams of unknown standing.
BUILTIN_CHANNELS = {
    "cgtn": ("CGTN", "https://english-livebkali.cgtn.com/live/encgtn.m3u8"),
    "france24": ("France 24",
                 "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_500.m3u8"),
    "dw": ("DW English",
           "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/master.m3u8"),
    "tagesschau": ("Tagesschau24",
                   "https://tagesschau.akamaized.net/hls/live/2020115/tagesschau/tagesschau_1/master.m3u8"),
}

# Populated by load_channels(); kept as module-level names because the server,
# the CONFIG payload and the tests all read them directly.
CHANNELS: dict[str, str] = {}
CHANNEL_LABELS: dict[str, str] = {}
# User-Agent per channel id; empty means ffmpeg's default.
CHANNEL_AGENTS: dict[str, str] = {}
DEFAULT_CHANNEL = ""


def parse_channels(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse the channel table, rejecting anything the device would discard.

    The firmware skips an id that is empty, non-printable or 16 bytes or longer,
    and stops reading the list at 16 entries. Catching that here turns a silently
    missing channel on the device into an error the operator can see.
    """
    channels: dict[str, str] = {}
    labels: dict[str, str] = {}
    agents: dict[str, str] = {}
    dropped = 0
    global CHANNEL_AGENTS
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        # Four fields when the source needs a specific User-Agent. Many community
        # mirrors answer only to the player they were captured for and return 403
        # to anything else, so the agent is part of the source, not a detail.
        if len(parts) == 3:
            key, label, url = parts
            agent = ""
        elif len(parts) == 4:
            key, label, url, agent = parts
        else:
            raise ValueError(f"line {number}: expected 'id | name | url [| user-agent]', "
                             f"got {len(parts)} fields")
        if not key or len(key) >= TV_CHANNEL_ID_MAX:
            raise ValueError(f"line {number}: id must be 1..{TV_CHANNEL_ID_MAX - 1} characters")
        if not all("!" <= char <= "~" for char in key):
            raise ValueError(f"line {number}: id must be printable ASCII")
        if key in channels:
            raise ValueError(f"line {number}: duplicate id {key!r}")
        # Past the device's limit the extra entries are dropped rather than
        # treated as a fatal fault.
        #
        # The limit is the device's; so is the consequence of exceeding it, and
        # the consequence is that the later channels never appear on screen. It
        # is not a reason for the server to refuse to run: a table that is too
        # long is a table with channels the device cannot show, not a broken
        # server, and stopping means nothing plays at all -- including the
        # hundred-odd channels that are perfectly fine. Refusing also put the
        # failure at start-up, in a traceback, at the one moment the operator is
        # least able to tell which of several possible causes it was.
        #
        # Reported once, below, with the numbers, so the operator is told rather
        # than left to notice a channel missing.
        if len(channels) >= TV_CHANNEL_MAX:
            dropped += 1
            continue
        if not label:
            raise ValueError(f"line {number}: missing display name")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"line {number}: url must start with http:// or https://")
        channels[key] = url
        labels[key] = label
        if agent:
            agents[key] = agent
        elif url.startswith(("http://", "https://")):
            agents[key] = frames.DEFAULT_USER_AGENT
    if not channels:
        raise ValueError("channel table contains no channels")
    # A list that fits the count but not the packet is the other way to lose
    # channels, and it is the quieter one: nothing is dropped, so the count looks
    # right, but the CONFIG packet is refused on the wire and the device shows no
    # channels at all. Names are what make it long -- each Chinese character is
    # six bytes once JSON-escaped -- so this is the check that catches a hundred
    # long names where the count check catches a thousand short ones.
    #
    # Reported, not refused, for the same reason as the count: the table is
    # usable, the packet limit is the device's, and stopping the server would
    # take away the channels that do fit.
    packet = len(json.dumps({"channel_list": [{"id": k, "name": labels[k]}
                                              for k in channels]},
                            ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    if packet > TV_CONTROL_MAX or dropped:
        total = len(channels) + dropped
        # Two separate faults, said separately. Reporting the count when the
        # count is fine sends the reader to cut channels they do not need to cut,
        # and the real cause -- names too long -- goes unfixed.
        if dropped:
            print(f"警告：channels.txt 有 {total} 个频道，设备最多接收 "
                  f"{TV_CHANNEL_MAX} 个。", flush=True)
            print(f"      超出的 {dropped} 个已被忽略，设备只会显示前 "
                  f"{len(channels)} 个。", flush=True)
        if packet > TV_CONTROL_MAX:
            if not dropped:
                print(f"警告：{total} 个频道，数量没有超，但频道名太长。", flush=True)
            print(f"      下发给设备的列表有 {packet} 字节，超过设备的 "
                  f"{TV_CONTROL_MAX} 字节上限，设备可能一个频道都收不到。", flush=True)
            print(f"      每个频道名平均 {packet // max(1, len(channels))} 字节，"
                  f"缩短频道名（尤其是中文名）是有效的办法。", flush=True)
        print("      用 python3 tools/channel_config.py 调整，或直接编辑 channels.txt。",
              flush=True)
    return channels, labels, agents


def load_channels(path: Path | None = None) -> None:
    """Install the channel table, from a file if one is given or found.

    Called at import so every existing reader of CHANNELS keeps working, and
    again from main() when --channels-file is passed.
    """
    global CHANNELS, CHANNEL_LABELS, CHANNEL_AGENTS, DEFAULT_CHANNEL
    source = path
    if source is None:
        environment = os.environ.get(CHANNELS_ENV)
        if environment:
            source = Path(environment)
        elif Path(CHANNELS_FILE).is_file():
            source = Path(CHANNELS_FILE)
        elif (Path(__file__).resolve().parent.parent / CHANNELS_FILE).is_file():
            source = Path(__file__).resolve().parent.parent / CHANNELS_FILE
    if source is None:
        channels = {key: url for key, (_, url) in BUILTIN_CHANNELS.items()}
        labels = {key: label for key, (label, _) in BUILTIN_CHANNELS.items()}
        agents = {}
    else:
        if not source.is_file():
            raise ValueError(f"channel file not found: {source}")
        # Bounded by the channel limit, never by TV_CONTROL_MAX or VIDEO_MAX.
        # Those are wire limits for a single packet, and using one of them here
        # silently truncated the file mid-line: the parser then saw a half line
        # with one field and refused the whole table, so the server exited at
        # import while the file itself was fine.
        #
        # The bound is what the largest legal table can occupy: every entry is at
        # most this many bytes (id, name, url and agent), so nothing a valid file
        # can contain is ever cut, while a stray multi-megabyte file still stops
        # at a known size.
        limit = TV_CHANNEL_MAX * MAX_CHANNEL_LINE_BYTES
        with source.open(encoding="utf-8", errors="strict") as handle:
            text = handle.read(limit + 1)
        if len(text) > limit:
            raise ValueError(f"{source} is larger than {limit} bytes; the device "
                             f"cannot hold more than {TV_CHANNEL_MAX} channels")
        channels, labels, agents = parse_channels(text)
    CHANNELS.clear()
    CHANNELS.update(channels)
    CHANNEL_LABELS.clear()
    CHANNEL_LABELS.update(labels)
    CHANNEL_AGENTS.clear()
    CHANNEL_AGENTS.update(agents)
    # Keep the previous default when it is still offered, otherwise start at the
    # top of the list: a default that is not in the table would fail every
    # connection that named no channel.
    if DEFAULT_CHANNEL not in channels:
        DEFAULT_CHANNEL = next(iter(channels))


def channel_list() -> list[dict]:
    return [{"id": key, "name": CHANNEL_LABELS.get(key, key)} for key in CHANNELS]


load_channels()


class LiveError(RuntimeError):
    """Transcoding or framing failure; the session ends and the device reconnects."""


def read_frames(stream, on_frame, stop) -> None:
    """Read indexed frames from a raw pipe, one at a time.

    Rawvideo means no markers and no lengths -- just one byte a pixel -- so this
    reads a fixed number of bytes instead of scanning for anything. That is also
    why a short read means the pipe has ended, not that a frame was malformed:
    there is nothing to resynchronise to, and a half frame cannot be drawn, so
    the tail is dropped rather than padded.
    """
    while not stop.is_set():
        frame = frames.read_frame(stream, stop)
        if frame is None:
            raise LiveError("transcode video pipe closed")
        on_frame(frame)


def chunk_pcm(stream, on_audio, stop) -> None:
    """Re-block the PCM pipe into exact device chunks (1280 bytes / 40 ms)."""
    buffer = bytearray()
    while not stop.is_set():
        chunk = stream.read(AUDIO_BYTES * 8)
        if not chunk:
            raise LiveError("transcode audio pipe closed")
        buffer.extend(chunk)
        while len(buffer) >= AUDIO_BYTES:
            block = bytes(buffer[:AUDIO_BYTES])
            del buffer[:AUDIO_BYTES]
            on_audio(block)


# Samples in one device block. The device takes 1280 bytes of s16 mono, so the
# audio frame size is fixed to that rather than left to the resampler: it is
# what makes one `ashowinfo` line describe exactly one block, and the timestamps
# are read one line per block.
AUDIO_SAMPLES = AUDIO_BYTES // 2

# One graph, two outputs, and the timestamps read out of the same graph.
#
# Two processes used to decode this source, each counting its own frames, with
# the relationship between the two clocks measured once from `ffprobe`. Counting
# is a rate and not a clock, and it cannot see a gap at all, so this is now one
# decode carrying the decoder's own `pts_time` for every item it produces.
#
# `format=rgb8` sits before `showinfo` so the logged frame is the frame that is
# written. `aformat=...mono` before `asetnsamples` is load-bearing and was got
# wrong first time round: a stereo frame carries two device blocks, so one
# `ashowinfo` line would cover two blocks and every timestamp after the first
# would be paired with the wrong payload.
PRE_FILTER = os.environ.get("TV_PRE_FILTER", "").strip()
_VIDEO_CHAIN = f"{frames.FIT},{PRE_FILTER}" if PRE_FILTER else frames.FIT
SOURCE_GRAPH = (
    f"[0:v]setpts=PTS-STARTPTS,fps={FPS},{_VIDEO_CHAIN},format=rgb8,showinfo[v];"
    f"[0:a]asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},aformat=channel_layouts=mono,"
    f"asetnsamples=n={AUDIO_SAMPLES},ashowinfo[a]"
)


def source_command(url: str, video_fd: int, audio_fd: int, ffmpeg: str,
                   user_agent: str = "") -> list[str]:
    """Read the source once and produce both payloads and both timestamps.

    The picture is decoded, scaled, and quantised onto ffmpeg's own fixed 3-3-2
    palette. That palette is the same 256 colours `main/av_protocol.c` builds in
    `av_palette_rgb565` -- 36 and 85 steps, replicated to fill each slot -- and
    it is fixed rather than generated, because an adaptive palette sampled from
    the opening seconds of a channel goes stale for every later picture whose
    colours were not in that sample.

    `-loglevel info` is required and not a debugging leftover: `showinfo` and
    `ashowinfo` log at INFO, and the timestamps are what this process is here to
    produce. `-nostats` keeps ffmpeg's progress line out of the same stream.

    The graph's outputs are named because ffmpeg rejects a filter graph whose
    output label is also its input label. Dithering is off, and that is not a
    detail: swscale dithers by default, and dithering is noise by construction,
    so it roughly halves how well a frame compresses -- measured across five
    channels, worst-case frames fell from about 31 KB to about 23 KB once it was
    off. It has to be given here, after the conversion, rather than before `-i`,
    where it parses without complaint and does nothing.
    """
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
        *frames.input_options(url, user_agent, paced=True),
        "-i", url,
        "-filter_complex", SOURCE_GRAPH,
        "-map", "[v]", "-pix_fmt", "rgb8", "-sws_dither", "none",
        "-threads", "1", "-f", "rawvideo", f"pipe:{video_fd}",
        "-map", "[a]", "-f", "s16le", f"pipe:{audio_fd}",
    ]


# How long ffprobe may take on a source before the answer is treated as absent.
# A live HLS playlist is the slow case and it is seconds, not minutes; a source
# that cannot answer inside this is one this server will start without.
PROBE_TIMEOUT_S = float(os.environ.get("TV_PROBE_TIMEOUT_S", "20"))


def stream_start_times(url: str, ffmpeg: str, user_agent: str = "",
                       diagnostic: list | None = None) -> tuple[float, float] | None:
    """Ask the media where each stream begins. (video_start_s, audio_start_s)

    This is the **only** thing that can relate the two content clocks, and this
    function is the only place that answer comes from. `ffprobe` reports each
    stream's `start_time`, which is a property of the programme: two streams
    that begin together are simultaneous, and one that begins 600 ms later has
    600 ms less content.

    Neither of the two things that were tried instead can answer it. The arrival
    time is about the network, and the process launch time is about the
    scheduler. An external review disproved the second with real ffmpeg: the
    same file, with only the audio process launched 600 ms later, produced an
    offset of +600 ms and discarded the picture's first four frames, although
    the media had not changed at all. And a file whose audio genuinely starts
    600 ms in produced an offset of +0.2 ms, erasing a real difference.

    Returns None when the answer is not available -- ffprobe missing, the source
    unreadable, or a live format that does not report start times. **A caller
    that gets None must not substitute a guess**: an unexplained offset is worse
    than an admitted absence of one, which is the whole lesson of the two
    attempts above.

    `ffmpeg` is the binary path this server already resolved, so ffprobe is
    looked for beside it before falling back to PATH.
    """
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(probe):
        probe = "ffprobe"

    def failed(reason: str) -> None:
        # Recorded rather than returned silently. An external review showed the
        # difference is not academic: a source that answers ffmpeg with a
        # User-Agent and refuses ffprobe without one looks exactly like a source
        # that reports no start times, and the code then chose a basis that is
        # only valid for the other kind of source. "No answer" and "this source
        # has no start times" are different findings and only one of them is
        # about the media.
        if diagnostic is not None:
            diagnostic.append(reason)

    command = [probe, "-v", "error"]
    # The same User-Agent the two decoders use. Many sources in the channel
    # table answer only the player they were captured for and 403 anything
    # else, which is why the agent travels with the channel in the first place.
    # Asking without it made the probe fail on sources the picture and sound
    # decoders could read perfectly well.
    effective_ua = user_agent or (frames.DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
    if effective_ua:
        command += ["-user_agent", effective_ua]
    command += ["-show_entries", "stream=codec_type,start_time",
                "-of", "json", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        failed("ffprobe is not installed")
        return None
    except subprocess.TimeoutExpired:
        failed(f"ffprobe timed out after {PROBE_TIMEOUT_S:g} s")
        return None
    except OSError as error:
        failed(f"ffprobe could not be run: {type(error).__name__}")
        return None
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        failed("ffprobe failed: " + (first[0] if first else
                                     f"exit {done.returncode}"))
        return None
    try:
        streams = json.loads(done.stdout).get("streams", [])
    except ValueError:
        failed("ffprobe returned something that is not JSON")
        return None
    found: dict[str, float] = {}
    for stream in streams:
        kind = stream.get("codec_type")
        if kind not in ("video", "audio") or kind in found:
            continue
        try:
            found[kind] = float(stream["start_time"])
        except (KeyError, TypeError, ValueError):
            # A live HLS playlist often has no start_time. Absent is absent.
            failed(f"the source reports no start_time for its {kind} stream")
            return None
    if "video" not in found or "audio" not in found:
        failed("the source does not report both a video and an audio stream")
        return None
    return found["video"], found["audio"]


class LiveChannel:
    """Own one channel: a palette, a picture process, an audio process, and the
    bounded queues between them and the sender."""

    def __init__(self, url: str, ffmpeg: str = "ffmpeg", user_agent: str = "",
                 palette_dir: Path | None = None):
        if url not in CHANNELS.values():
            raise ValueError("channel URL is not in the local allowlist")
        self.url = url
        self.ffmpeg = ffmpeg
        self.user_agent = user_agent
        self.stop = threading.Event()
        self.audio = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        self.video = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        # When each queued item ARRIVED, in step with the two queues above.
        #
        # This is what the picture is aligned by, and the reason is that it needs
        # no knowledge of any frame rate. The sound and the picture come from one
        # source in real time, so the block and the frame carrying the same
        # moment of content arrive at the same instant; if the sender emits the
        # frame whose arrival matches the sound it is emitting, the two describe
        # the same moment whatever their queues are doing.
        #
        # Every earlier attempt here worked from queue DEPTH converted to seconds
        # by a frame rate, and that conversion is the flaw: the producer's rate,
        # the ceiling and the controller's rate are three different numbers and
        # the wrong one was used each time. Measured with the depth method, the
        # log read `a_lag=15.7s v_lag=15.8s` -- apparently in step, and wrong,
        # because `v_lag` divided by the SENDING rate (five) while the queue had
        # been filled at the PRODUCING rate (twelve). The picture was in fact
        # about nine seconds ahead of the sound.
        # Each item's CONTENT time: when it arrived, less when its own decoder
        # was started. NOT the arrival instant, which is a different number for
        # the two streams by however long they took to launch.
        self.audio_at: collections.deque = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        self.video_at: collections.deque = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        self.video_epoch = time.monotonic()
        self.audio_epoch = time.monotonic()
        # The content clock, which is what alignment is now done in. The
        # arrival deques above remain for exactly one purpose: the single
        # calibration that relates the two decoders' origins. After that they
        # are diagnostics.
        #
        # See server/timeline.py for why this exists. In one line: arrival time
        # is a property of the network, content time is a property of the
        # programme, and only the second can carry a lip-sync relationship.
        self.timeline = ContentTimeline(
            video_interval_ms=1000.0 / FPS, audio_interval_ms=AUDIO_CHUNK_MS)
        # Content time carried into this session's wire clock. One per
        # channel, because a channel is a session and the device requires the
        # sound to start at zero in each one.
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        self.video_content: collections.deque = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        self.audio_content: collections.deque = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        # Whether the source is still producing, as distinct from whether the
        # link is keeping up. The controller is told the difference; see
        # SourceState for why an empty window is not one reading.
        self.source = SourceState()
        self._video_advanced = False
        self._audio_advanced = False
        # The rate the link is currently taking pictures at, and when the last
        # frame was accepted. See set_target_fps and _push_video.
        self._target_fps = FPS
        self._last_push: float | None = None
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.dropped_video = 0
        self.skipped_audio = 0
        self.produced_audio = 0
        self.produced_video = 0
        self.encoded_video_bytes = 0
        self.encode_seconds = 0.0
        #: The one decoder. It produces both payloads and both timestamps.
        self.decoder: subprocess.Popen | None = None
        self.timestamps: pts.Timestamps | None = None
        self.pts_reader: pts.Reader | None = None
        self.threads: list[threading.Thread] = []
        self._raw_video: queue.Queue[bytes | None] = queue.Queue(maxsize=120)
        self._raw_audio: queue.Queue[bytes | None] = queue.Queue(maxsize=300)
        # The colours the device needs before it can draw anything. Filled in by
        # build_palette(), and sent by the session before the first frame.
        self.palette: bytes | None = None
        self.palette_path: Path | None = None
        self._palette_dir = palette_dir or Path(tempfile.gettempdir())

    @staticmethod
    def has_media_start_times(probe_note: list) -> bool:
        """Whether the probe's failure was about the media or about the probe.

        Only one failure means "this source has no start times": the media
        answered and its streams do not report a `start_time`. Every other
        failure -- no program, a timeout, a refused request, output that is not
        JSON -- is a failure of the *probe*, and none of them says anything
        about the source's time semantics.

        Collapsing the two is a defect an external review found twice. The first
        version read any probe failure as "no start times" and used launch times
        for a local file; the second read the URL's suffix and was defeated by
        the same file served from `/watch?id=1` instead of `/source.mkv`.
        """
        return any("reports no start_time" in note for note in probe_note)

    @staticmethod
    def _content_looks_live(url: str) -> bool | None:
        """Whether the path names a stored file. None means the URL does not say.

        A last resort, and deliberately a weak one: it is consulted only when
        the probe cannot answer, and its negative answer is what stops the
        fallback. The positive answer is never sufficient on its own -- see
        `calibrate_from_source`.
        """
        suffix = Path(urlparse(url).path).suffix.lower()
        if not suffix:
            return None
        return suffix not in (".mp4", ".mkv", ".mov", ".ts", ".webm", ".avi",
                              ".flv", ".m4v")

    def calibrate_from_source(self) -> bool:
        """Relate the two content clocks.

        One decoder produces both streams and both timestamps, so there is no
        offset left to measure: the picture and the sound are already on one
        clock and the number this sets is zero by construction. What the call
        still does, and why it is still a call, is record WHICH basis related
        them. A session that cannot say where its times came from cannot be
        accepted or rejected on the strength of them.

        Kept as a method rather than inlined so the decision stays exercisable
        without starting a decoder.

        The bases this replaced are worth naming here, because each was wrong in
        its own way. Counting each stream's items and relating the two counts
        once from `ffprobe` cannot see a gap: a count closes a hole rather than
        recording it. Using the two processes' launch times folds the scheduler
        into the programme, which an external review disproved twice. Both
        remain defined in `timeline.py`, where the tests that disproved them
        live, and `BASIS_LAUNCH` now survives as a *state* rather than a session
        path -- no session reaches it, and it is kept because "approximate" has
        to stay distinguishable from "measured" for either word to mean
        anything.
        """
        return self.timeline.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)

    def build_palette(self) -> bytes:
        """The colours this channel's indices are drawn in.

        With FIXED_PALETTE this is a constant and there is nothing to build, no
        sample to wait for, and nothing to go stale. The method keeps its name
        because the session calls it before the first frame and the contract --
        "the device has a palette before it is sent an index" -- is unchanged.

        The adaptive version sampled the source here, which is the bug written
        up at FIXED_PALETTE above.
        """
        if FIXED_PALETTE:
            # Imported here rather than at module scope: media imports live.
            from .media import default_palette
            self.palette = default_palette()
            self.palette_path = None
            return self.palette
        self.palette_path = self._palette_dir / f"palette-{abs(hash(self.url)):x}.png"
        try:
            self.palette = frames.build_palette(
                self.url, self.ffmpeg, self.user_agent, str(self.palette_path),
                timeout=PALETTE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            raise LiveError(f"palette generation failed: {type(error).__name__}") from None
        return self.palette

    def start(self) -> None:
        if not FIXED_PALETTE and self.palette_path is None:
            raise LiveError("start() before build_palette()")
        if self.palette is None:
            raise LiveError("start() before build_palette()")
        # Picture and sound come from a single decoder process with separate pipes.
        # Every resource is closed on failure: the device reconnects on error, so a
        # leaking start() would exhaust descriptors after a few attempts.
        video_r = audio_r = video_w = audio_w = -1
        success = False
        try:
            video_r, video_w = os.pipe()
            audio_r, audio_w = os.pipe()
            # On Linux, default anonymous pipe buffer is only 64KB (holding only
            # 1.1 frames of 320x180 RGB8 at 57.6KB). Expand to 1MB if OS supports it.
            if fcntl is not None:
                for fd in (video_r, video_w, audio_r, audio_w):
                    try:
                        fcntl.fcntl(fd, 1031, 1048576)
                    except (AttributeError, OSError):
                        pass
            for fd in (video_w, audio_w):
                os.set_inheritable(fd, True)
            # stderr is a pipe rather than a file now, because the decoder's
            # timestamps arrive on it. It is drained by a thread of its own: an
            # unread pipe blocks ffmpeg at the next log line, and a blocked
            # ffmpeg is indistinguishable from a stalled source.
            self.timestamps = pts.Timestamps()
            self.decoder = subprocess.Popen(
                source_command(self.url, video_w, audio_w, self.ffmpeg,
                               self.user_agent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, pass_fds=(video_w, audio_w))
            self.pts_reader = pts.Reader(self.decoder.stderr, self.timestamps)
            self.pts_reader.start()
            # One process, so there is one launch instant. Kept because the
            # approximate basis still names it; see `calibrate_from_source`.
            self.video_epoch = self.audio_epoch = time.monotonic()
            self.calibrate_from_source()

            # Close write ends in the parent as child now owns inherited write ends
            for fd in (video_w, audio_w):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            video_w = audio_w = -1

            self._video = os.fdopen(video_r, "rb", buffering=0)
            video_r = -1
            self._audio = os.fdopen(audio_r, "rb", buffering=0)
            audio_r = -1

            self.threads = [
                threading.Thread(target=self._drain_video, daemon=True, name="live-video-drain"),
                threading.Thread(target=self._process_video, daemon=True, name="live-video-process"),
                threading.Thread(target=self._drain_audio, daemon=True, name="live-audio-drain"),
                threading.Thread(target=self._process_audio, daemon=True, name="live-audio-process"),
            ]
            for thread in self.threads:
                thread.start()
            success = True
        finally:
            for fd in (video_w, audio_w, video_r, audio_r):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if not success:
                self.close()

    def _note(self, error: Exception) -> None:
        with self.lock:
            if self.error is None:
                self.error = error
        self.stop.set()

    def _decoder_time(self, kind: str) -> float:
        """The timestamp belonging to the item just read, in milliseconds.

        Raising rather than substituting is the point. A stream stamped from a
        substitute clock looks exactly like a stream stamped from a real one,
        and this project has spent several rounds on the consequences of not
        being able to tell them apart.
        """
        seconds = self.timestamps.take(kind) if self.timestamps else None
        if seconds is None:
            if self.stop.is_set():
                return 0.0
            raise LiveError(
                f"the decoder produced a {kind} item without logging its "
                f"timestamp; the session cannot be timed and is being ended")
        return seconds * 1000.0

    def _drain_video(self) -> None:
        """Drain raw frames from ffmpeg video pipe immediately into memory queue.

        Decoupling pipe draining from timestamp synchronization is critical on
        Linux: a 64KB pipe buffer only holds 1.1 frames of 320x180 raw video. If
        the pipe reader pauses while waiting for a timestamp log line, ffmpeg
        blocks on write(), freezing the entire transcode process and deadlocking.
        """
        try:
            while not self.stop.is_set():
                frame = frames.read_frame(self._video, self.stop)
                if frame is None:
                    if not self.stop.is_set():
                        self._raw_video.put(None)
                    break
                while not self.stop.is_set():
                    try:
                        self._raw_video.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError) as error:
            self._note(error)

    def _process_video(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    frame = self._raw_video.get(timeout=0.1)
                except queue.Empty:
                    continue
                if frame is None:
                    if self.stop.is_set():
                        break
                    raise LiveError("transcode video pipe closed")
                self._push_video(frame, self._decoder_time("video"))
        except (LiveError, OSError, ValueError, pts.Misaligned) as error:
            self._note(error)

    def _drain_audio(self) -> None:
        """Drain raw audio blocks immediately from ffmpeg audio pipe."""
        try:
            while not self.stop.is_set():
                block = frames.read_exactly(self._audio, AUDIO_BYTES)
                if len(block) != AUDIO_BYTES:
                    if not self.stop.is_set():
                        self._raw_audio.put(None)
                    break
                while not self.stop.is_set():
                    try:
                        self._raw_audio.put(block, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError) as error:
            self._note(error)

    def _process_audio(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    block = self._raw_audio.get(timeout=0.1)
                except queue.Empty:
                    continue
                if block is None:
                    if self.stop.is_set():
                        break
                    raise LiveError("transcode audio pipe closed")
                self._push_audio(block, self._decoder_time("audio"))
        except (LiveError, OSError, ValueError, pts.Misaligned) as error:
            self._note(error)

    def _read_video(self) -> None:
        """Alias for compatibility with external or test invocations."""
        self._process_video()

    def _read_audio(self) -> None:
        """Alias for compatibility with external or test invocations."""
        self._process_audio()

    def _push_video(self, frame: bytes, content_ms: float) -> None:
        """Cut and compress a frame here, not on the sending thread.

        `content_ms` is the decoder's own timestamp for this frame, which is
        where it sits on the source's timeline. It is carried through the queue
        beside the payload and never recomputed; see `_push_audio` for what it
        replaces.

        The queue holds packets where it used to hold a frame, and the whole
        frame is compressed before any of it is queued, so the sender never
        sees half a picture. Compressing here rather than in the sender keeps
        about a millisecond of work out of the loop that also has to keep the
        sound fed -- small, but that loop's budget is what the picture is
        already counted against.
        """
        encode_started = time.monotonic()
        packets = frames.frame_packets(frame)
        with self.lock:
            self.produced_video = getattr(self, "produced_video", 0) + 1
            self.encoded_video_bytes = getattr(self, "encoded_video_bytes", 0) + sum(map(len, packets))
            self.encode_seconds = getattr(self, "encode_seconds", 0.0) + time.monotonic() - encode_started
            # Frames the link is not going to take are given up here, before
            # they are queued, rather than kept and dropped at the other end.
            #
            # The test is the QUEUE's depth and not the interval since the last
            # frame, and the difference is not a detail. Measured with an
            # interval test: a source delivering six frames a second into a
            # target of twelve had every frame rejected as "too soon", so the
            # picture ran at 6.2 packets a second with the queue empty and
            # `prod_drop` climbing by 1900 -- the throttle was discarding the
            # source rather than the surplus. A source slower than the target has
            # no surplus to discard and must never be throttled.
            #
            # Queue depth is the right test because depth is what this is for:
            # the producer runs at the ceiling and the sender runs at whatever
            # the channel's frame size allows, so on any channel held below the
            # ceiling the queue fills and stays full -- and a full queue is the
            # picture's distance behind live, because the frame sent is the
            # oldest one in it.
            if len(self.video) == self.video.maxlen:
                self.video.popleft()
                if self.video_at:
                    self.video_at.popleft()
                self.dropped_video += 1
            self.video.append(packets)
            arrival = time.monotonic() - self.video_epoch
            self.video_at.append(arrival)
            self.video_content.append(content_ms)
            self._video_advanced = True

    def _push_audio(self, block: bytes, content_ms: float) -> None:
        """Queue one sound block, beside the decoder's timestamp for it.

        The timestamp is the decoder's, not a count of blocks. Counting gave a
        block the position `blocks_received * 40 ms`, which is the right rate
        and the wrong clock: a decoder that skips 300 ms of sound still emits
        one block per 40 ms, so the count closed the hole and moved everything
        after it, and the sender had no way to see that it had happened.
        """
        with self.lock:
            if len(self.audio) == self.audio.maxlen:
                self.audio.popleft()
                if self.audio_at:
                    self.audio_at.popleft()
                if self.audio_content:
                    self.audio_content.popleft()
                self.skipped_audio += 1
            self.audio.append(block)
            self.produced_audio += 1
            arrival = time.monotonic() - self.audio_epoch
            self.audio_at.append(arrival)
            self.audio_content.append(content_ms)
            self._audio_advanced = True

    def pop_audio(self) -> tuple[bytes, int] | None:
        """The next sound block, with the session timestamp it is to be sent at.

        The timestamp **is** the count of blocks taken, and that is not the
        defect it looks like. The device's clock counts sound it has received
        (`submitted_samples / 16000`) and `av_stream_accept()` requires each
        block's timestamp to be exactly the previous one plus one chunk, so a
        block's session position is its index by definition.

        What the count may not do is *stand in for the picture's position*, and
        that was the earlier defect: the frame's stamp was derived from this same
        counter, so it described this loop's progress instead of the frame's
        place in the programme. The picture is stamped from its own content now;
        see `SessionClock.video`.

        The block's content time is not discarded -- `SessionClock.audio` uses it
        to detect and measure a source discontinuity, which a count cannot see.
        The arrival deque is popped alongside because it is the queue's own
        bookkeeping and leaving it behind would pair one queue's entries with
        another's.
        """
        with self.lock:
            if not self.audio or not self.audio_content:
                # The two deques are pushed together and popped together, so an
                # empty content deque beside a full payload deque cannot arise
                # from _push_audio. Refusing rather than stamping the block with
                # a counter: a counter here is the defect this replaced.
                return None
            content = self.audio_content.popleft()
            if self.audio_at:
                self.audio_at.popleft()
            return self.audio.popleft(), self.session_clock.audio(content)

    def picture_lag_s(self) -> float:
        """How far behind the sound's head the picture's head is, in seconds.

        Both are CONTENT times, so the difference is a real lip-sync error and
        needs no frame rate to interpret. Positive means the picture is behind
        the sound.

        This used to subtract two arrival times, which is a delivery
        measurement wearing a sync measurement's name: it read -0.3 s through a
        session in which the picture was seconds out of step, because both
        queues had been filled by the same reader and their arrivals therefore
        agreed while their content did not.
        """
        with self.lock:
            if not self.video_content or not self.audio_content:
                return 0.0
            if not self.timeline.calibrated:
                return 0.0
            sound_at = self.timeline.audio_in_video_units(self.audio_content[0])
            return (self.video_content[0] - sound_at) / 1000.0

    def audio_depth_ms(self) -> int:
        """How much sound is queued, in milliseconds of content.

        The picture's queue is held to this, because the two have to describe the
        same moment. See VIDEO_QUEUE_SECONDS.
        """
        with self.lock:
            return len(self.audio) * AUDIO_CHUNK_MS

    def set_target_fps(self, fps: int) -> None:
        """Tell the producer how fast the link is taking pictures.

        ffmpeg's own rate is fixed when the process starts, and restarting it to
        change that would cost the session its stream and its palette. So the
        producer keeps running at the ceiling and the excess is dropped here
        instead, which is cheaper than it sounds: ffmpeg is decoding and
        compressing on the host, and the frame is discarded before it is
        queued rather than after it has crossed the link.

        Without this the queue is fed faster than it is drained on every channel
        the controller holds below the ceiling -- which, after the ceiling became
        a per-channel derivation, is most of them.
        """
        with self.lock:
            self._target_fps = max(1, fps)

    def pop_video(self, keep: int = 0) -> tuple[list[bytes], int] | None:
        """The frame whose CONTENT matches the sound the sender is about to send.

        Alignment is by content time, not by arrival time. Both queues carry,
        beside each item, the moment of the programme that item belongs to --
        counted from the stream's own production rate, not read off a clock --
        and this returns the frame whose content is the sound's, within one
        audio chunk.

        **What this replaced, and why.** The rule used to be `arrival minus that
        decoder's own start`, which is still an arrival time: it moves when the
        network stalls, when the reader is slow, when a process is descheduled.
        None of those are properties of the programme. An external review built
        the counterexample that this module now carries as a test -- the same
        picture, delivered 600 ms late, stopped matching its sound, because
        600 ms is past `VIDEO_SYNC_TOLERANCE_S`. The two decoders' origins are
        related once at the start and are constant for the session; treating a
        later delay as a fresh observation of that relationship is exactly the
        defect.

        A frame whose content is older than the sound being sent has already
        been heard by the viewer and is given up. A frame that is newer has not
        reached its moment, and nothing is returned so the caller waits -- the
        sound keeps the timeline, which is the rule everywhere else here.

        The answer is the frame's packets **and the session timestamp they are
        to be sent under**, decided here rather than by the sender. The two must
        come out of one decision: the packets of a frame all share a timestamp,
        and a sender free to derive that separately is free to derive it from
        something else -- which is exactly what the sender used to do, stamping
        the picture with its own progress instead of the frame's place against
        the sound. See `SessionClock`.
        """
        with self.lock:
            if not self.video:
                return None
            if not self.audio_content and self.session_clock._prev_audio_content is None:
                return None
            if not self.timeline.usable:
                # Nothing has related the two clocks yet, so there is no basis
                # for saying which frame and which chunk are the same moment.
                # Refusing is the answer; the first version of this guessed, and
                # the startup gap became a lip-sync error.
                #
                # `usable` and not `calibrated`, and the difference is a black
                # screen. An approximate mapping may be used to pair; what it
                # may not do is pass as a measurement. Gating pairing on the
                # stricter question left `video_sent` at zero for two minutes
                # while the sound climbed to 2634 packets.
                return None
            # The moment of the programme the sound belongs to, expressed on
            # the picture's clock so the comparison is direct.
            ref_audio = self.audio_content[0] if self.audio_content else self.session_clock._prev_audio_content
            head_audio = self.timeline.audio_in_video_units(ref_audio)
            # A loop rather than a single decision, because a frame that turns
            # out to be unplaceable is discarded and the next one tried. The
            # alternative -- returning after the first refusal -- drops at most
            # one frame per tick, so a run of frames inside one hole would take
            # as many ticks to clear as it has frames.
            while self.video and self.video_content:
                content = self.video_content[0]
                # Anything the picture holds that is older than that sound is
                # content the viewer has already heard, so it goes.
                if content < head_audio - VIDEO_SYNC_TOLERANCE_S * 1000:
                    self._give_up_frame()
                    continue
                # Not yet due: the frame that matches this sound has not arrived.
                if content > head_audio + VIDEO_SYNC_TOLERANCE_S * 1000:
                    return None
                # The frame's OWN content position is what decides its wire
                # timestamp, not the position of the sound it was paired with.
                # Using the sound's position was the defect an external review
                # demonstrated by construction: a frame 333 ms older than that
                # sound was stamped with the sound's moment, so the device drew
                # it 333 ms late, and the constant -292 ms residual this
                # produced over five minutes was that tolerance being spent and
                # then hidden.
                frame_at = self.timeline.video_in_audio_units(content)
                # The queue's head is passed in as well as being paired with.
                # It is the only way the clock can know that a gap has opened
                # BEFORE the sound after it is taken; without it the open
                # segment extends across the hole and stamps frames that belong
                # inside it.
                verdict, stamp = self.session_clock.placement(
                    frame_at, ref_audio)
                if verdict == VERDICT_IN_HOLE:
                    # Sound the sender dropped. The viewer never heard this
                    # moment, so there is none to draw the frame at.
                    self._give_up_frame()
                    continue
                if verdict != VERDICT_PLACED:
                    # Not due yet, or the session has no anchor. Both are
                    # patience rather than loss, so the frame stays queued.
                    return None
                self.video_content.popleft()
                if self.video_at:
                    self.video_at.popleft()
                packets = self.video.popleft()
                self.session_clock.note_pair(frame_at, ref_audio)
                return packets, stamp
            return None

    def _give_up_frame(self) -> None:
        """Discard the frame at the head of the picture queue. Caller holds the lock."""
        self.video.popleft()
        self.video_content.popleft()
        if self.video_at:
            self.video_at.popleft()
        self.dropped_video += 1

    def source_state(self, queue_over_bound: bool = False) -> str:
        """Classify the window that just ended and clear the per-window flags.

        Called once per decision window by the sender, before it tells the
        controller anything. The flags are per-window and are consumed here, so
        a stale True from a window three seconds ago cannot make a quiet window
        look busy.

        The three answers are not decoration: `starved` is what stops an empty
        window being read as spare capacity, and `congested` is the case the
        rate is supposed to answer. Before this existed the controller had one
        number for both.
        """
        with self.lock:
            state = self.source.observe(self._video_advanced, self._audio_advanced,
                                        queue_over_bound)
            self._video_advanced = False
            self._audio_advanced = False
            return state

    def flow_snapshot(self) -> dict:
        """Producer totals and queue depth; these are not device counters."""
        with self.lock:
            return dict(audio_produced=self.produced_audio, video_produced=self.produced_video,
                        video_encoded_bytes=self.encoded_video_bytes,
                        video_encode_wall_s=round(self.encode_seconds, 4),
                        audio_queue_ms=len(self.audio) * AUDIO_CHUNK_MS,
                        video_queue_frames=len(self.video), audio_skipped=self.skipped_audio,
                        video_discarded=self.dropped_video)

    def trim_backlog(self, maximum_ms: int = 8000, retain_ms: int = 4000) -> int:
        """Trim both streams at one content edge; call between complete frames.

        SessionClock observes the next audio content jump and retains
        contiguous wire PCM stamps. This can skip content when overloaded.
        """
        if not 0 < retain_ms < maximum_ms:
            raise ValueError("invalid live backlog bounds")
        with self.lock:
            if len(self.audio) * AUDIO_CHUNK_MS <= maximum_ms:
                return 0
            count = len(self.audio) - max(1, retain_ms // AUDIO_CHUNK_MS)
            for _ in range(count):
                self.audio.popleft()
                self.audio_content.popleft()
                if self.audio_at:
                    self.audio_at.popleft()
            self.skipped_audio += count
            if self.audio_content and self.timeline.usable:
                edge = self.timeline.audio_in_video_units(self.audio_content[0])
                while self.video_content and self.video_content[0] < edge:
                    self._give_up_frame()
            return count

    def has_data(self) -> bool:
        with self.lock:
            # Video can be paired if audio queue has data or if previous audio was already anchored.
            return bool(self.audio or (self.video and self.session_clock._prev_audio_content is not None))

    def audio_pending(self) -> bool:
        with self.lock:
            return bool(self.audio)

    def video_pending(self) -> bool:
        with self.lock:
            return bool(self.video)

    def failure(self) -> Exception | None:
        with self.lock:
            return self.error

    def prebuffered(self) -> bool:
        with self.lock:
            return len(self.audio) >= PREBUFFER_CHUNKS and len(self.video) >= PREBUFFER_FRAMES

    def wait_for_failure(self) -> None:
        """Block until the transcode stops, for a supervisor thread."""
        self.stop.wait()

    @staticmethod
    def _sanitize_diagnostics(text: str) -> str:
        """Strip tokens, passwords, and sensitive parameters from diagnostic text."""
        # Redact URL query parameters (e.g. ?token=... or &key=...)
        text = re.sub(r"([?&][a-zA-Z0-9_.-]+=)[^\s&'\"<>]+", r"\1<redacted>", text)
        # Redact user:pass in URLs
        text = re.sub(r"(https?://)([^:@\s/]+:[^:@\s/]+@)", r"\1<auth>@", text)
        return text


    def diagnostics(self) -> str:
        """Transcode error text for logs: ffmpeg stderr only, never media."""
        parts = []
        error = self.failure()
        if error is not None:
            parts.append(type(error).__name__)
        proc = getattr(self, "decoder", None)
        if proc is not None and proc.poll() is not None:
            parts.append(f"decoder_exit={proc.returncode}")
        pts_reader = getattr(self, "pts_reader", None)
        if pts_reader is not None and pts_reader.error is not None:
            parts.append(f"pts_reader_error={type(pts_reader.error).__name__}")
        ts = getattr(self, "timestamps", None)
        if ts is not None and ts.tail:
            tail_lines = [line.decode("utf-8", errors="replace").strip() for line in ts.tail]
            tail_text = " ".join(line for line in tail_lines if line)
            if tail_text:
                tail_text = self._sanitize_diagnostics(tail_text)
                parts.append(f"decoder: {tail_text.replace(chr(10), ' ')[-300:]}")
        return " | ".join(parts)

    def _processes(self):
        """Single decoder process, kept for compatibility if needed."""
        proc = getattr(self, "decoder", None)
        return (("decoder", proc, None),)

    def close(self) -> None:
        self.stop.set()
        ts = getattr(self, "timestamps", None)
        if ts is not None:
            try:
                ts.close()
            except Exception:
                pass
        # Close pipe streams first so decoder is not blocked in write() and reader threads see EOF
        for stream in (getattr(self, "_video", None), getattr(self, "_audio", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._video = None
        self._audio = None
        proc = getattr(self, "decoder", None)
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except OSError:
                    pass
            except OSError:
                pass
        all_threads = list(getattr(self, "threads", []))
        pts_reader = getattr(self, "pts_reader", None)
        if pts_reader is not None:
            all_threads.append(pts_reader)
        for thread in all_threads:
            if thread.is_alive():
                try:
                    thread.join(timeout=1.0)
                except Exception:
                    pass
        if proc is not None and getattr(proc, "stderr", None) is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass
        palette_path = getattr(self, "palette_path", None)
        if palette_path is not None:
            try:
                palette_path.unlink()
            except OSError:
                pass
            self.palette_path = None
