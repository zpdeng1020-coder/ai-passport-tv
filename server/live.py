"""Live HLS transcoding into the bounded FAV1 stream the device already accepts.

One ffmpeg process per channel pulls an allowlisted public HLS URL and writes
MJPEG frames and 16 kHz mono PCM into two pipes. Two reader threads frame the
bytes; the session sender paces them against one monotonic origin so audio PTS
stays contiguous and video is never allowed to stall audio.

This is a LAN test service, not a hardened transcoder: URLs come only from the
fixed CHANNELS table, the stream is not recorded, and no credentials are used.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .media import (AUDIO_LEAD_MS, FPS, HEIGHT as VIDEO_HEIGHT, START_DELAY_MS,
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
AUDIO_CHUNK_MS = 20
AUDIO_RATE = 16000
# The send leads come from media.py so the pre-generated scheduler and this live
# sender cannot disagree about them.
VIDEO_LATE_DROP_MS = 200
# Kept below the device's AUDIO_UNDERRUN_MS (300 ms) so the server gives up on a
# starved origin before the device concludes its stream is broken: otherwise the
# device logs "audio underrun" while this side still believes it is mid-stream.
AUDIO_LATE_RESET_MS = 250
# The device holds 400 ms of PCM plus ~90 ms of DMA. Keep some distance from
# that ceiling, but leave enough room that the device queue stays topped up
# between bursts: too small a lookahead leaves it empty and a brief Wi-Fi gap
# then looks like an underrun.
# The cap is on how far audio may lead the wall clock, so a pause of S seconds
# lets this sender flush (S*1000 + cap)/20 chunks while the device drains only
# ~S*1000/20 of them: the queue grows by roughly cap/20 + 1 chunks, i.e. 17 at
# 320 ms against a 20-chunk device queue. 240 ms keeps that margin at 13 and
# still leaves more than the ~90 ms DMA cover, so a burst cannot overrun it.
AUDIO_MAX_LOOKAHEAD_MS = 240
# These queues are the whole reason playback can be steady. An HLS origin does
# not deliver a smooth stream: each segment arrives as a burst, so ffmpeg emits
# several seconds of audio at once and then waits for the next segment. With a
# buffer only a few seconds deep the queue hit its ceiling during a burst (audio
# was discarded) and then drained to nothing before the next segment, which the
# device saw as an underrun and reset the session. A deep buffer absorbs the
# burst and rides out the wait, at the cost of starting some seconds behind the
# live edge, which is the trade this prototype wants.
PCM_QUEUE_CHUNKS = 3000        # 60 s of PCM (~1.9 MB)
VIDEO_QUEUE_FRAMES = 720       # 60 s of JPEG at 12 fps (~17 MB worst case)
# Depth and startup delay are separate decisions and must stay that way. The
# queue depth above absorbs a segment burst without discarding anything; this
# threshold is only "how much is enough to start". Raising it to match the queue
# made every channel change wait half a minute for a reserve it did not need.
PREBUFFER_CHUNKS = 150         # 3 s of audio
PREBUFFER_FRAMES = 36          # 3 s of video at 12 fps
# Only the backstop for an origin that never produces anything at all: the
# prebuffer target, not this, decides when playback normally starts.
PREBUFFER_TIMEOUT_S = 60
JPEG_QUALITY = "8"

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
    CHANNELS, CHANNEL_LABELS, CHANNEL_AGENTS = channels, labels, agents
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


def frame_jpeg(stream, on_frame, stop) -> None:
    """Split a byte stream of concatenated JPEGs on SOI/EOI markers.

    ffmpeg's image2pipe MJPEG output contains no EXIF thumbnails, so a nested
    SOI cannot occur. A frame is only emitted once its EOI marker arrived; an
    unterminated tail is discarded rather than sent truncated.
    """
    buffer = bytearray()
    while not stop.is_set():
        chunk = stream.read(8192)
        if not chunk:
            raise LiveError("transcode video pipe closed")
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                if len(buffer) > VIDEO_MAX:
                    del buffer[:-1]  # keep one byte: SOI may straddle chunks
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buffer[:start]
                if len(buffer) > VIDEO_MAX:
                    raise LiveError("JPEG frame exceeds the 24 KiB device limit")
                break
            frame = bytes(buffer[start:end + 2])
            del buffer[:end + 2]
            if len(frame) > VIDEO_MAX:
                raise LiveError("JPEG frame exceeds the 24 KiB device limit")
            on_frame(frame)


def chunk_pcm(stream, on_audio, stop) -> None:
    """Re-block the PCM pipe into exact 640-byte / 20 ms device chunks."""
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


def ffmpeg_command(url: str, video_fd: int, audio_fd: int, ffmpeg: str,
                   user_agent: str = "") -> list[str]:
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
        # -re paces input at its native rate and stays. Without it ffmpeg decoded
        # a whole HLS window at once, filled both queues to their limit within
        # three seconds and then went quiet, so the device drained them and
        # stalled. Do not add "-fflags +nobuffer" back either: it looks like a
        # latency win, but on an HLS input it suppresses segment prefetch and
        # pushed the first video frame from 2 s out to 11 s, against a device
        # deadline of 12 s.
        "-re",
        "-flags", "low_delay",
        # HLS edges drop connections routinely; without these ffmpeg exits and
        # the device sees a dead session instead of a brief rebuffer.
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]
    # Some mirrors only answer the player they were captured for and return 403
    # to anything else, so the agent travels with the source.
    if user_agent:
        command += ["-user_agent", user_agent]
    command += [
        "-i", url,
        "-map", "0:v:0", "-vf",
        f"fps={FPS},scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", JPEG_QUALITY,
        "-threads", "1", "-f", "image2pipe", f"pipe:{video_fd}",
        "-map", "0:a:0", "-ac", "1", "-ar", str(AUDIO_RATE),
        "-c:a", "pcm_s16le", "-f", "s16le", f"pipe:{audio_fd}",
    ]
    return command


class LiveChannel:
    """Own one ffmpeg process and expose bounded audio/video queues."""

    def __init__(self, url: str, ffmpeg: str = "ffmpeg", user_agent: str = ""):
        if url not in CHANNELS.values():
            raise ValueError("channel URL is not in the local allowlist")
        self.url = url
        self.ffmpeg = ffmpeg
        self.user_agent = user_agent
        self.stop = threading.Event()
        self.audio = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        self.video = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.dropped_video = 0
        self.skipped_audio = 0
        self.process: subprocess.Popen | None = None
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        # Every resource is closed on failure: the device reconnects on error, so
        # a leaking start() would exhaust descriptors after a few attempts. Both
        # pipes are opened inside the try because the second one can fail too, and
        # then the first pair would never be closed.
        video_r = audio_r = video_w = audio_w = -1
        opened = False
        try:
            video_r, video_w = os.pipe()
            audio_r, audio_w = os.pipe()
            for fd in (video_w, audio_w):
                os.set_inheritable(fd, True)
            # stderr goes to a file, never a pipe: an unread pipe eventually
            # blocks ffmpeg and looks like a stalled stream, not a warning.
            self._stderr_file = tempfile.TemporaryFile()
            self.process = subprocess.Popen(
                ffmpeg_command(self.url, video_w, audio_w, self.ffmpeg, self.user_agent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=self._stderr_file, pass_fds=(video_w, audio_w))
            opened = True
        finally:
            # Each descriptor is closed only if it was actually opened: a failure
            # between the two os.pipe() calls leaves the later names unbound, and
            # closing -1 would raise a second error from the cleanup itself.
            for fd in (video_w, audio_w):
                if fd >= 0:
                    os.close(fd)
            if not opened:
                for fd in (video_r, audio_r):
                    if fd >= 0:
                        os.close(fd)
                stderr_file = getattr(self, "_stderr_file", None)
                if stderr_file is not None:
                    stderr_file.close()
                    self._stderr_file = None
        self._video = os.fdopen(video_r, "rb", buffering=0)
        self._audio = os.fdopen(audio_r, "rb", buffering=0)
        self.threads = [
            threading.Thread(target=self._read_video, daemon=True),
            threading.Thread(target=self._read_audio, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _note(self, error: Exception) -> None:
        with self.lock:
            if self.error is None:
                self.error = error
        self.stop.set()

    def _read_video(self) -> None:
        try:
            frame_jpeg(self._video, self._push_video, self.stop)
        except (LiveError, OSError, ValueError) as error:
            self._note(error)

    def _read_audio(self) -> None:
        try:
            chunk_pcm(self._audio, self._push_audio, self.stop)
        except (LiveError, OSError, ValueError) as error:
            self._note(error)

    def _push_video(self, frame: bytes) -> None:
        with self.lock:
            if len(self.video) == self.video.maxlen:
                self.video.popleft()
                self.dropped_video += 1
            self.video.append(frame)

    def _push_audio(self, block: bytes) -> None:
        with self.lock:
            if len(self.audio) == self.audio.maxlen:
                self.audio.popleft()
                self.skipped_audio += 1
            self.audio.append(block)

    def pop_audio(self) -> bytes | None:
        with self.lock:
            return self.audio.popleft() if self.audio else None

    def pop_video(self) -> bytes | None:
        with self.lock:
            return self.video.popleft() if self.video else None

    def has_data(self) -> bool:
        with self.lock:
            return bool(self.audio or self.video)

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

    def diagnostics(self) -> str:
        """Transcode error text for logs: ffmpeg stderr only, never media."""
        parts = []
        error = self.failure()
        if error is not None:
            parts.append(type(error).__name__)
        if self.process is not None and self.process.poll() is not None:
            parts.append(f"ffmpeg_exit={self.process.returncode}")
        try:
            self._stderr_file.seek(0)
            tail = self._stderr_file.read(4096).decode("utf-8", errors="replace").strip()
        except (OSError, ValueError, AttributeError):
            tail = ""
        if tail:
            parts.append(tail.replace("\n", " ")[-400:])
        return " | ".join(parts)

    def close(self) -> None:
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (getattr(self, "_video", None), getattr(self, "_audio", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        stderr_file = getattr(self, "_stderr_file", None)
        if stderr_file is not None:
            try:
                stderr_file.close()
            except OSError:
                pass
