"""Prepare synthetic or user-selected video into bounded JPEG/PCM media."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import frames
from .protocol import AUDIO_BYTES

# The picture geometry lives with the format it belongs to. Re-exported under
# the names this module's callers already use, so the scheduler and the tests
# keep reading one definition rather than a copy that can drift.
WIDTH, HEIGHT = frames.WIDTH, frames.HEIGHT
# Frames a second the server aims to send. Overridable from the environment so
# that finding the rate this device can actually hold does not mean editing a
# tracked file between every run -- the measurements are worth comparing, and
# they are not comparable if the thing being measured changes underneath them.
#
# Both ends read this figure for different purposes: the server paces by it, and
# the device only checks that it is sane (main/av_player.c, json_between) because
# every frame carries its own timestamp. So it can be raised or lowered freely
# from here without rebuilding the firmware.
#
# The rate the picture is PRODUCED at. **It is not the rate it is sent at, and
# this comment used to claim it was.**
#
# `FPS` follows `rate.MAX_FPS` and is what ffmpeg is asked for; the sender's
# rate is what the controller decides once a second, and under the shipped
# configuration it settles near 5 while ffmpeg produces 12. Measured from the
# live log: `frame=21678B` with `budget=120000` carries about 5.5 frames a
# second while 12 are produced, so `prod_drop` climbs by about 6.5 a second and
# the measured figure was 7.0. The surplus is discarded at the queue, which
# costs nothing that was going to be sent -- but the two rates are not the same
# number and must not be reasoned about as one.
#
# What the original comment described was a real fault, and it is worth keeping
# the shape of it: the two being independent is what put the queue permanently
# full. ffmpeg was asked for ten frames a second while the sender was held to
# five by the link, so five frames a second piled up in a queue that holds
# fifteen seconds of them: measured, `video_q` sat at 180 of 180 for the whole
# session, `prod_drop` climbed by five a second, and the picture on screen was
# some fifteen seconds behind the channel. Nothing was broken and nothing
# recovered -- the queue simply stopped draining.
#
# It is read from rate.MAX_FPS rather than repeated, so the two cannot drift
# apart again. Producing faster than the link carries buys nothing: the extra
# frames are discarded at the far end of a queue, at the cost of encoding them
# and of the delay it puts between the channel and the screen.
from .rate import MAX_FPS as _MAX_FPS

FPS = int(os.environ.get("TV_FPS", str(_MAX_FPS)))
DURATION_MS = 10000
AUDIO_CHUNK_MS = 40
# Send leads, defined here because both the pre-generated scheduler and the live
# sender must use the same pair and this module is the one they both import.
# They are equal by construction: the merge sends whichever stream's slot comes
# due first, and because audio may lead the wall clock by a lookahead, a shorter
# video lead placed the video slot behind the audio slot for every iteration and
# starved video for the entire session.
# How far ahead of the wall clock the sender runs. This, and not the lookahead,
# is what sets how much audio sits in the device's queue at a steady state: the
# sender releases a chunk when its due time arrives, so the queue holds this
# much and the lookahead only caps how far a catch-up may run past it.
#
# Two hundred was too near the edge. One task reads both streams, so a picture
# packet in flight is time in which nothing drains the queue, and a 240x180
# frame is one packet of about 13 KB that takes some 220 ms to cross -- longer
# than the 200 ms the queue held. Measured: every underrun was preceded by a
# ten-second interval with the sound full, and the gap at the failure was 305 ms
# every time, which is the device's threshold and not a variable quantity.
#
# Three hundred and twenty is where the sweep landed, and it is better than the
# figure it replaces on every count rather than merely no worse. Over 240
# seconds each: at 200 ms, ten underruns of the sound and frames dropped; at
# 260, none and a median of 10.0 frames; at 320, none, a median of 10.8 and not
# one dropped frame; at 400, none but 39 dropped and the median back to 9.0.
# Past a point the deeper lead is the picture's problem rather than the sound's,
# because a chunk released long before its due time occupies the socket that the
# frame needed.
#
# Overridable so the depth can be swept without a rebuild.
AUDIO_LEAD_MS = int(os.environ.get("TV_AUDIO_LEAD_MS", "320"))
# The picture keeps the lead it had, and separating the two is the point of
# this line. They were equal, and equal leads are what make the merge alternate
# evenly -- that property is wanted and is tested. But they do not have to be
# *this* value: the sound's lead is what sets the device's queue depth, while
# the picture's lead only decides how far ahead of its own slot a frame is
# released. Raising both with one number re-released the picture 200 ms earlier,
# which bunched it against the sound and cost more frames than it saved --
# measured, 58 dropped in 300 seconds against 9, while the underruns went to
# zero. The picture wants the old lead; the sound wants the deeper one.
VIDEO_LEAD_MS = int(os.environ.get("TV_VIDEO_LEAD_MS", "200"))
START_DELAY_MS = 200
# Derived, not written out: the counts must follow FPS. The pre-generated
# import path and the live path must agree on this number, because the device
# rejects a CONFIG whose fps field is not the one its geometry expects, and the
# live sender derives its timestamps from the same constant.
FRAME_COUNT = DURATION_MS * FPS // 1000
PCM_SIZE = 320000

# Seven-segment counters avoid a dependency on ffmpeg's optional drawtext/fonts.
DIGITS = ("abcedf", "bc", "abged", "abgcd", "fgbc", "afgcd", "afgecd",
          "abc", "abcdefg", "abfgcd")
SEGMENTS = {"a": (4, 0, 24, 4), "b": (28, 4, 4, 24),
            "c": (28, 32, 4, 24), "d": (4, 56, 24, 4),
            "e": (0, 32, 4, 24), "f": (0, 4, 4, 24), "g": (4, 28, 24, 4)}


def synthetic_frame(index: int) -> bytes:
    """One test frame as palette indices: counter, moving box, a flash a second.

    Indices rather than colour because that is what the device draws, so the
    generated media takes exactly the same path as a live channel and exercises
    the same code on both ends.
    """
    rgb = bytearray(bytes((18, 30, 48)) * WIDTH * HEIGHT)

    def rect(x, y, width, height, color):
        row = bytes(color) * width
        for yy in range(y, y + height):
            start = (yy * WIDTH + x) * 3
            rgb[start:start + len(row)] = row

    for position, digit in enumerate(f"{index:03d}"):
        for segment in DIGITS[int(digit)]:
            x, y, w, h = SEGMENTS[segment]
            rect(50 + position * 20 + x // 2, 32 + y // 2,
                 w // 2, h // 2, (240, 240, 240))
    rect(index % 140, 82, 20, 16, (50, 180, 220))
    if index % FPS == 0:
        rect(0, 0, WIDTH, 18, (255, 255, 255))
    # Back to indices in one pass. The rectangles above are drawn in colour
    # because that is readable; this is the single place the two meet.
    return bytes((((rgb[3 * i] >> 5) << 5) | ((rgb[3 * i + 1] >> 5) << 2) |
                  (rgb[3 * i + 2] >> 6)) for i in range(WIDTH * HEIGHT))


def validate_frame(raw: bytes) -> None:
    """An indexed frame is exactly one byte a pixel, and that is the whole check.

    There is no structure to walk and no marker to look for: a frame that is the
    right length is a frame, and one that is not would be drawn as a torn
    picture rather than rejected. Bounded here rather than trusted because these
    files are written by ffmpeg, and a truncated one would otherwise reach the
    panel.
    """
    if len(raw) != frames.FRAME_PIXELS:
        raise ValueError(f"frame is {len(raw)} bytes, not {frames.FRAME_PIXELS}")


def default_palette() -> bytes:
    """The 3-3-2 palette, which is the one synthetic_frame() quantises to.

    Generated media has to carry its own palette: a live channel gets one
    sampled from the source and sent before the first frame, and a generated
    clip has no source to sample. Without this the device would be handed
    indices with nothing to look them up in, and every frame would come out
    black -- a fault that looks like a broken device rather than a missing
    packet.

    The 36 and 85 steps are the ones ffmpeg uses for AV_PIX_FMT_RGB8, chosen so
    this and the live path agree. Both were checked against ffmpeg's own
    palette and both match it for all 256 entries once quantised to RGB565.
    """
    return frames.palette_bytes(bytes(
        value
        for k in range(256)
        for value in (36 * ((k >> 5) & 7), 36 * ((k >> 2) & 7), 85 * (k & 3))
    ))


@dataclass(frozen=True)
class Media:
    pcm: bytes
    frames: tuple[bytes, ...]

    @property
    def palette(self) -> bytes:
        """Generated media always uses the 3-3-2 palette; see default_palette."""
        return default_palette()

    @property
    def duration_ms(self) -> int:
        return DURATION_MS

    def audio_at(self, index: int) -> bytes:
        return self.pcm[index * AUDIO_BYTES:(index + 1) * AUDIO_BYTES]

    def frame_at(self, index: int) -> list[bytes]:
        """This frame as the packets that carry it, matching FileMedia.

        Built on demand from the stored frame: packets are derived from it and
        keeping both in memory would double what a prepared clip occupies.
        """
        return frames.frame_packets(self.frames[index])

    @classmethod
    def load(cls, directory: Path) -> "Media":
        # Fixed filenames and bounded reads: no paths or URLs from the peer.
        with (directory / "manifest.json").open("rb") as stream:
            manifest_raw = stream.read(1025)
        if len(manifest_raw) > 1024:
            raise ValueError("oversized media manifest")
        manifest = json.loads(manifest_raw)
        if manifest.get("format") == "FAV2-video":
            return FileMedia.load_video(directory, manifest)
        expected = {"format": "FAV2-indexed", "duration_ms": DURATION_MS,
                    "width": WIDTH, "height": HEIGHT, "fps": FPS,
                    "sample_rate": 16000, "channels": 1, "sample_bits": 16,
                    "audio_chunk_ms": AUDIO_CHUNK_MS, "frame_count": FRAME_COUNT}
        if manifest != expected:
            raise ValueError("unsupported media manifest")
        pcm_path = directory / "audio.s16le"
        with pcm_path.open("rb") as stream:
            pcm = stream.read(PCM_SIZE + 1)
        if len(pcm) != PCM_SIZE:
            raise ValueError("PCM must contain exactly ten seconds")
        stored = []
        for index in range(FRAME_COUNT):
            with (directory / f"frame-{index:03d}.idx").open("rb") as stream:
                raw = stream.read(frames.FRAME_PIXELS + 1)
            validate_frame(raw)
            stored.append(raw)
        return cls(pcm, tuple(stored))


def prepare(directory: Path, ffmpeg: str = "ffmpeg") -> Media:
    """Create a new directory holding ten seconds of generated media.

    Only the audio needs ffmpeg; the pictures are written directly. Each ffmpeg
    call has a 60-second deadline. Failed preparation stays local and has no
    manifest; run refuses it. Existing destinations are never replaced.
    """
    directory.mkdir(parents=True, exist_ok=False)
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]

    def invoke(args):
        subprocess.run(base + args, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

    # t=0 matches frame zero. 1 kHz, 50 ms per second, amplitude 0.08 (-22 dBFS).
    invoke(["-f", "lavfi", "-i",
            "aevalsrc=0.08*sin(2*PI*1000*t)*lt(mod(t\\,1)\\,0.05):s=16000:d=10",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "s16le",
            str(directory / "audio.s16le")])
    # Written straight out, no encoder in between: the generator already emits
    # the indices the device reads, so there is nothing to convert and no way
    # for the two to disagree. Re-quantising oversized frames disappeared with
    # the JPEG path -- an index frame is exactly one byte a pixel, always.
    for index in range(FRAME_COUNT):
        raw = synthetic_frame(index)
        validate_frame(raw)
        (directory / f"frame-{index:03d}.idx").write_bytes(raw)
    manifest = {"format": "FAV2-indexed", "duration_ms": DURATION_MS,
                "width": WIDTH, "height": HEIGHT, "fps": FPS,
                "sample_rate": 16000, "channels": 1, "sample_bits": 16,
                "audio_chunk_ms": AUDIO_CHUNK_MS, "frame_count": FRAME_COUNT}
    (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return Media.load(directory)


@dataclass(frozen=True)
class FileMedia:
    directory: Path
    duration_ms: int

    @classmethod
    def load_video(cls, directory: Path, manifest: dict) -> "FileMedia":
        duration = manifest.get("duration_ms")
        if type(duration) is not int or duration % 1000 or not 1000 <= duration <= 600000:
            raise ValueError("unsupported clip duration")
        for key, value in {"width": WIDTH, "height": HEIGHT, "fps": FPS,
                           "sample_rate": 16000, "channels": 1, "sample_bits": 16,
                           "audio_chunk_ms": AUDIO_CHUNK_MS,
                           "frame_count": duration * FPS // 1000}.items():
            if manifest.get(key) != value:
                raise ValueError("unsupported video format")
        if (directory / "audio.s16le").stat().st_size != duration * 32:
            raise ValueError("incorrect PCM length")
        if (directory / "palette.bin").stat().st_size != frames.PALETTE_BYTES:
            raise ValueError("incorrect palette size")
        for index in range(duration * FPS // 1000):
            with (directory / f"frame-{index:06d}.idx").open("rb") as stream:
                validate_frame(stream.read(frames.FRAME_PIXELS + 1))
        return cls(directory, duration)

    @property
    def palette(self) -> bytes:
        """The palette this clip was indexed with, read from beside it."""
        with (self.directory / "palette.bin").open("rb") as stream:
            raw = stream.read(frames.PALETTE_BYTES + 1)
        if len(raw) != frames.PALETTE_BYTES:
            raise ValueError("clip palette is the wrong size")
        return raw

    def audio_at(self, index: int) -> bytes:
        with (self.directory / "audio.s16le").open("rb") as stream:
            stream.seek(index * AUDIO_BYTES)
            raw = stream.read(AUDIO_BYTES)
        if len(raw) != AUDIO_BYTES:
            raise ValueError("truncated PCM")
        return raw

    def frame_at(self, index: int) -> bytes:
        """This frame, as the packets that carry it.

        Packets rather than a frame so the pre-generated path and the live path
        hand the sender the same kind of thing, and the sender needs no branch.
        They are built on demand rather than stored: a packet is a few kilobytes
        derived from a frame that is already on disk, and keeping both would
        double what a clip occupies.
        """
        with (self.directory / f"frame-{index:06d}.idx").open("rb") as stream:
            raw = stream.read(frames.FRAME_PIXELS + 1)
        validate_frame(raw)
        return frames.frame_packets(raw)


# An import is not interruptible: there is no session to abandon and no stop
# flag to honour, so the frame reader is handed a flag that is never set rather
# than being given a second code path.
_NEVER_STOP = threading.Event()


def import_video(source: Path, directory: Path, seconds: int = 60,
                 start: float = 0, ffmpeg: str = "ffmpeg") -> FileMedia:
    """Convert a local clip; cap duration and frame size without loading it into RAM."""
    source = source.resolve(strict=True)
    if not source.is_file() or not 1 <= seconds <= 600 or start < 0:
        raise ValueError("expected local video, 1..600 seconds, nonnegative start")
    directory.mkdir(parents=True, exist_ok=False)
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start), "-i", str(source)]
    def run(args):
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=max(60, seconds*4))

    # A clip needs a palette of its own before it can become indices: the
    # colours that suit one film are not the colours that suit another, and
    # there is no channel here to inherit one from. Sampled from the clip
    # itself, then used to index the whole of it.
    palette_png = directory / "palette.png"
    try:
        subprocess.run(frames.palette_command(str(source), ffmpeg, "", str(palette_png),
                                              sample_seconds=min(seconds, frames.SAMPLE_SECONDS)),
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=60)
    except subprocess.CalledProcessError as error:
        raise ValueError("could not sample a palette from this clip") from error
    # Kept beside the frames: the device is sent this exact palette before the
    # first frame, and reading it back from disk is what guarantees the colours
    # it uses are the colours the frames were indexed with.
    (directory / "palette.bin").write_bytes(frames.read_palette(str(palette_png), ffmpeg))

    # Fit the whole picture into the panel. No crop and no stretch: centred
    # black letterbox or pillarbox. The geometry itself comes from frames.FIT,
    # which is the same string the palette above was sampled with.
    filter_video = f"fps={FPS},{frames.FIT}"
    # Raw index bytes, one frame at a time, rather than a numbered image
    # sequence: the image2 muxer picks an encoder from the file extension and
    # writes a *picture of* the frame -- a 13287-byte PNG for a 76800-byte
    # frame, measured -- which is both larger and no longer the indices the
    # device reads. The frame written here is the frame as the device receives
    # it, with no encoder between the two to disagree with.
    #
    # Read a frame at a time instead of piping the whole span into memory: a
    # ten-minute import at twelve frames a second is half a gigabyte of
    # indices, and this runs on whatever machine is serving the television.
    # stderr is left as a pipe rather than sent to a file: it is only read when
    # ffmpeg fails, and a failing import is about to raise anyway.
    process = subprocess.Popen(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", str(start), "-i", str(source), "-i", str(palette_png),
         "-t", str(seconds), "-an", "-lavfi",
         f"[0:v]{filter_video}[s];[s][1:v]paletteuse=dither=none[v]",
         "-map", "[v]", "-pix_fmt", "pal8", "-f", "rawvideo", "pipe:1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    written: list[Path] = []
    try:
        while True:
            raw = frames.read_frame(process.stdout, _NEVER_STOP)
            # A partial tail is a clip that ends mid-frame, which cannot be
            # drawn; it is dropped rather than padded into a torn picture.
            if raw is None:
                break
            validate_frame(raw)
            (directory / f"frame-{len(written):06d}.idx").write_bytes(raw)
            written.append(directory / f"frame-{len(written):06d}.idx")
    finally:
        # Both pipes are closed here, not just the one that was read: stderr is
        # a pipe too, and an unclosed one is a descriptor leaked per import --
        # which on a long-lived server is a leak per clip, not per run.
        process.stdout.close()
        process.stderr.close()
        process.wait(timeout=60)
    if process.returncode:
        raise ValueError("could not read this clip as video")
    count = min(len(written) // FPS * FPS, seconds * FPS)
    if not count:
        raise ValueError("clip contains less than one second of video")
    duration = count // FPS
    # Drop any tail past a whole number of seconds, so the audio and the
    # pictures describe the same span.
    for extra in written[count:]:
        extra.unlink()
    # Optional map permits a silent source; synthesize silence only for no stream.
    probe = subprocess.run([ffmpeg,"-nostdin","-hide_banner","-i",str(source)],
                           capture_output=True, timeout=30)
    has_audio = b"Audio:" in probe.stderr
    output = str(directory / "audio.s16le")
    if has_audio:
        run(base + ["-map","0:a:0","-vn","-af", "apad",
                    "-t",str(duration),"-ac","1","-ar","16000",
                    "-c:a","pcm_s16le","-f","s16le",output])
    else:
        run([ffmpeg,"-nostdin","-v","error","-y","-f","lavfi","-i",
             "anullsrc=r=16000:cl=mono","-t",str(duration),"-c:a","pcm_s16le","-f","s16le",output])
    # Capture timestamps/encoder priming can shift the first audio frame.
    # Normalize the fixed-duration clip; this is not a live-stream clock correction.
    pcm_path = directory / "audio.s16le"
    expected_bytes = duration * 32000
    with pcm_path.open("r+b") as stream:
        stream.seek(0, 2)
        present = stream.tell()
        if present < expected_bytes:
            remaining = expected_bytes - present
            while remaining:
                chunk_bytes = min(remaining, 4096)
                stream.write(bytes(chunk_bytes))
                remaining -= chunk_bytes
        else:
            stream.truncate(expected_bytes)
    manifest={"format":"FAV2-video","duration_ms":duration*1000,
              "width":WIDTH,"height":HEIGHT,"fps":FPS,"sample_rate":16000,
              "channels":1,"sample_bits":16,"audio_chunk_ms":AUDIO_CHUNK_MS,
              "frame_count":count}
    (directory/"manifest.json").write_text(json.dumps(manifest)+"\n")
    return FileMedia.load_video(directory, manifest)


def schedule(duration_ms: int, media_duration_ms: int = DURATION_MS):
    """Lazy O(1) merge by send deadline, ordered PTS within each media stream.

    Audio/video have different lookahead, so cross-type wire PTS need not be
    ordered. Global seq is wire order; never re-sort these events by raw PTS.
    Integer arithmetic prevents drift through a 30-minute or longer run.

    The two leads are equal by construction. When video was given a shorter lead
    its slot sat behind the audio slot, and because audio may run a lookahead
    ahead of the wall clock, the merge picked audio at every step and starved
    video for the whole session.
    """
    audio, video = 0, 0
    while True:
        audio_pts = audio * AUDIO_CHUNK_MS
        video_pts = video * 1000 // FPS
        if min(audio_pts, video_pts) >= duration_ms:
            return
        if (audio_pts < duration_ms and
                (video_pts >= duration_ms or
                 audio_pts - AUDIO_LEAD_MS <= video_pts - VIDEO_LEAD_MS)):
            yield (audio_pts - AUDIO_LEAD_MS, 3, audio_pts,
                   audio % (media_duration_ms // AUDIO_CHUNK_MS))
            audio += 1
        else:
            yield (video_pts - VIDEO_LEAD_MS, 4, video_pts,
                   video % (media_duration_ms * FPS // 1000))
            video += 1
