"""Prepare synthetic or user-selected video into bounded JPEG/PCM media."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .protocol import AUDIO_BYTES, VIDEO_MAX

WIDTH, HEIGHT, FPS = 160, 120, 12
DURATION_MS = 10000
AUDIO_CHUNK_MS = 20
# Send leads, defined here because both the pre-generated scheduler and the live
# sender must use the same pair and this module is the one they both import.
# They are equal by construction: the merge sends whichever stream's slot comes
# due first, and because audio may lead the wall clock by a lookahead, a shorter
# video lead placed the video slot behind the audio slot for every iteration and
# starved video for the entire session.
AUDIO_LEAD_MS = 200
VIDEO_LEAD_MS = AUDIO_LEAD_MS
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
    """RGB source: frame counter, moving box, one-frame flash each second."""
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
    return bytes(rgb)


def validate_jpeg(raw: bytes) -> None:
    """Check bounded baseline, 8-bit, 160x120, three-component 4:2:0 JPEG.

    This is a metadata/structure guard, not an entropy decoder. Only locally
    generated files are supported; decoding validity is ffmpeg's responsibility.
    """
    if not 0 < len(raw) <= VIDEO_MAX:
        raise ValueError("JPEG exceeds 24 KiB or is empty")
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ValueError("invalid JPEG boundaries")
    offset, baseline = 2, False
    while offset < len(raw) - 2:
        if raw[offset] != 255:
            raise ValueError("invalid JPEG marker")
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        if offset + 2 > len(raw):
            break
        length = int.from_bytes(raw[offset:offset + 2], "big")
        if length < 2 or offset + length > len(raw):
            raise ValueError("invalid JPEG segment length")
        segment = raw[offset + 2:offset + length]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if (marker != 0xC0 or baseline or len(segment) != 15 or
                    segment[:6] != bytes((8, HEIGHT >> 8, HEIGHT & 255,
                                           WIDTH >> 8, WIDTH & 255, 3)) or
                    (segment[7], segment[10], segment[13]) != (0x22, 0x11, 0x11)):
                raise ValueError("JPEG must be baseline 160x120 yuv420")
            baseline = True
        if marker == 0xDA:
            if not baseline:
                raise ValueError("missing baseline JPEG frame header")
            return
        offset += length
    raise ValueError("missing JPEG scan")


@dataclass(frozen=True)
class Media:
    pcm: bytes
    frames: tuple[bytes, ...]

    @property
    def duration_ms(self) -> int:
        return DURATION_MS

    def audio_at(self, index: int) -> bytes:
        return self.pcm[index * AUDIO_BYTES:(index + 1) * AUDIO_BYTES]

    def frame_at(self, index: int) -> bytes:
        return self.frames[index]

    @classmethod
    def load(cls, directory: Path) -> "Media":
        # Fixed filenames and bounded reads: no paths or URLs from the peer.
        with (directory / "manifest.json").open("rb") as stream:
            manifest_raw = stream.read(1025)
        if len(manifest_raw) > 1024:
            raise ValueError("oversized media manifest")
        manifest = json.loads(manifest_raw)
        if manifest.get("format") == "FAV1-video":
            return FileMedia.load_video(directory, manifest)
        expected = {"format": "FAV1-synthetic", "duration_ms": DURATION_MS,
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
        frames = []
        for index in range(FRAME_COUNT):
            with (directory / f"frame-{index:03d}.jpg").open("rb") as stream:
                frame = stream.read(VIDEO_MAX + 1)
            validate_jpeg(frame)
            frames.append(frame)
        return cls(pcm, tuple(frames))


def prepare(directory: Path, ffmpeg: str = "ffmpeg") -> Media:
    """Create a new directory, retry oversized frames with lower JPEG quality.

    Each ffmpeg call has a 60-second deadline. Failed preparation stays local
    and has no manifest; run refuses it. Existing destinations are never replaced.
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
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=directory) as temporary:
        source = Path(temporary) / "frames.rgb"
        with source.open("wb") as stream:
            for index in range(FRAME_COUNT):
                stream.write(synthetic_frame(index))
        input_args = ["-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "160x120",
                      "-framerate", str(FPS), "-i", str(source)]
        invoke(input_args + ["-frames:v", str(FRAME_COUNT), "-c:v", "mjpeg", "-pix_fmt", "yuvj420p",
                             "-q:v", "5", "-threads", "1", "-start_number", "0",
                             str(directory / "frame-%03d.jpg")])
        for index in range(FRAME_COUNT):
            path = directory / f"frame-{index:03d}.jpg"
            for quality in (10, 18, 25, 31):
                if path.stat().st_size <= VIDEO_MAX:
                    break
                invoke(input_args + ["-vf", f"select=eq(n\\,{index})", "-frames:v", "1",
                                     "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v",
                                     str(quality), "-threads", "1", "-update", "1", str(path)])
            validate_jpeg(path.read_bytes())  # Never truncate or publish oversize media.
    manifest = {"format": "FAV1-synthetic", "duration_ms": DURATION_MS,
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
        for index in range(duration * FPS // 1000):
            with (directory / f"frame-{index:06d}.jpg").open("rb") as stream:
                validate_jpeg(stream.read(VIDEO_MAX + 1))
        return cls(directory, duration)

    def audio_at(self, index: int) -> bytes:
        with (self.directory / "audio.s16le").open("rb") as stream:
            stream.seek(index * AUDIO_BYTES)
            raw = stream.read(AUDIO_BYTES)
        if len(raw) != AUDIO_BYTES:
            raise ValueError("truncated PCM")
        return raw

    def frame_at(self, index: int) -> bytes:
        with (self.directory / f"frame-{index:06d}.jpg").open("rb") as stream:
            raw = stream.read(VIDEO_MAX + 1)
        validate_jpeg(raw)
        return raw


def import_video(source: Path, directory: Path, seconds: int = 60,
                 start: float = 0, ffmpeg: str = "ffmpeg") -> FileMedia:
    """Convert a local clip; cap duration and frame size without loading it into RAM."""
    source = source.resolve(strict=True)
    if not source.is_file() or not 1 <= seconds <= 600 or start < 0:
        raise ValueError("expected local video, 1..600 seconds, nonnegative start")
    directory.mkdir(parents=True, exist_ok=False)
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start), "-i", str(source)]
    pattern = str(directory / "frame-%06d.jpg")
    # Normalize source sample aspect ratio, then fit the full picture into 16:12
    # (4:3). No crop/stretch; centered black letterbox/pillarbox, even YUV420 size.
    filter_video = (f"fps={FPS},scale=iw*sar:ih,setsar=1,"
                    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2,"
                    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    def run(args):
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=max(60, seconds*4))
    run(base + ["-t", str(seconds), "-an", "-vf", filter_video,
                "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", "8",
                "-threads", "1", "-start_number", "0", pattern])
    frames = sorted(directory.glob("frame-*.jpg"))
    count = min(len(frames) // FPS * FPS, seconds * FPS)
    if not count:
        raise ValueError("clip contains less than one second of video")
    duration = count // FPS
    for frame in frames[:count]:
        if frame.stat().st_size > VIDEO_MAX:
            temporary = directory / "requantized.jpg"
            for quality in (16, 24, 31):
                run([ffmpeg,"-nostdin","-v","error","-y","-i",str(frame),
                     "-frames:v","1","-c:v","mjpeg","-pix_fmt","yuvj420p",
                     "-q:v",str(quality),"-update","1",str(temporary)])
                if temporary.stat().st_size <= VIDEO_MAX:
                    temporary.replace(frame)
                    break
        validate_jpeg(frame.read_bytes())
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
    manifest={"format":"FAV1-video","duration_ms":duration*1000,
              "width":WIDTH,"height":HEIGHT,"fps":FPS,"sample_rate":16000,
              "channels":1,"sample_bits":16,"audio_chunk_ms":20,"frame_count":count}
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
