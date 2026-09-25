"""A device, in software, that the real server can talk to.

Why this exists: the interesting failures on this link happen over minutes, and
the only way to see one is to leave the television on. Changing a controller
parameter then costs a session's worth of the viewer's time, and the link's
mood changes between runs, so two runs are never comparable.

This is a fake AI Passport. It dials the real server, speaks the real FAV1
protocol, and applies the rules the firmware applies -- with their line numbers
in the constants below so each can be checked against the source rather than
remembered. The LINK is simulated by reading from the socket slowly and in
stalls, which is not a shortcut: not reading is exactly what makes the sender's
writes block, so the backpressure this produces is the same mechanism the real
device produces when its receive window closes.

WHAT IT CAN ANSWER: which of two controllers keeps a session alive on a link
that stalls, how long each takes to recover, whether frames go late against the
audio clock, and how much of the picture survives. Those are comparisons, and a
comparison between two runs on the same simulated link is worth more than two
runs on a real one, because the simulated link is identical both times.

WHAT IT CANNOT ANSWER: anything about the device's own timing. On hardware,
inflating a stripe takes up to 146 ms, painting a frame 34.7 ms, and the
receiver outranks the decoder on a single core -- and those are what decide how
long a packet takes to read. Here a "read" is a socket call that costs nothing.
So a configuration that survives here is a candidate, not a finding, and the
final judgement has to come from the device. This project has paid for that
lesson more than once.

    python3 tools/device_sim.py --bind 192.168.0.125 --port 8096 --seconds 300

Then point nothing anywhere: this is the device, so it connects out. The server
must already be running and must accept the channel this asks for.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.protocol import Kind  # noqa: E402
from tools.console import use_utf8  # noqa: E402

# --- The firmware's rules, each with the line it was read from -------------
#
# main/av_player.c:445-453. The audio clock is whichever is behind: the wall
# clock since the origin, or the time represented by the samples actually
# submitted to the codec. The second term is what makes silence during a
# starvation honest rather than a lie about the timeline.
CLOCK_ORIGIN_OFFSET_MS = 90      # main/av_player.c:1266, DMA_ESTIMATE_MS
# main/av_protocol.h. Silence is played and the session carried on until this.
AUDIO_SILENCE_MAX_MS = 3000      # AUDIO_SILENCE_MAX_MS
# main/av_protocol.h. The gap between two consecutive I2S writes, which is a
# SEPARATE failure path from the queue running dry: the sound task also fails if
# it goes this long without feeding the codec at all. It is larger than the
# underrun tolerance by one chunk because the interval it measures covers one
# extra write.
AUDIO_FEED_GAP_MS = 340          # AUDIO_FEED_GAP_MS = 300 + 40
# main/av_player.c:2085. A frame more than this far behind the audio clock is
# dropped rather than drawn -- it would show a moment the viewer has heard.
VIDEO_LATE_DROP_MS = 100
# main/av_player.c:2184-2185. Two slots for video, and a pool of two receive
# buffers. A packet that finds the queue full, or no buffer free, is discarded
# and the frame it belonged to is left incomplete.
VIDEO_QUEUE_DEPTH = 2
VIDEO_BUFFERS = 2
# main/av_player.c:2117. A frame counts as drawn only when all fifteen stripes
# have arrived; anything less is discarded and the panel keeps the last picture.
STRIPES = 15
# The device's inactivity timeout, after which it drops the session by itself.
RX_INACTIVE_MS = 5000            # main/av_player.c


class Device:
    """The receiving half: queues, the clock, and the rules above."""

    def __init__(self, sample_rate=16000, chunk_ms=40, verbose=False):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.verbose = verbose
        # Re-entrant, because clock_ms() is called from inside sections that
        # already hold it. A plain Lock deadlocked on the first new frame:
        # video_task took the lock to apply the lateness rule, called
        # clock_ms(), and blocked for ever on its own lock -- so not one
        # frame was ever read and the server timed out writing to a
        # device that had stopped listening. The fault was here, not in
        # the thing being measured.
        self.lock = threading.RLock()

        self.audio = collections.deque()      # (pts, bytes)
        self.video = collections.deque()      # (pts, payload)
        self.free_buffers = VIDEO_BUFFERS
        self.stripes = [None] * STRIPES
        self.frame_pts = None
        self.frame_started = False

        # The clock, as the firmware computes it.
        self.clock_origin = None
        self.submitted_samples = 0
        self.last_audio_feed = None

        # What happened.
        self.drawn = 0
        self.dropped_late = 0
        self.dropped_incomplete = 0
        self.dropped_nobuf = 0
        self.dropped_queue_full = 0
        self.silence_episodes = 0
        self.silence_ms_total = 0
        self.failed = None
        self.last_packet_at = None
        self.first_media_at = None
        self.audio_newest_pts = None
        # What the sound actually playing belongs to; set when a chunk is taken.
        self.audio_playing_pts = None
        self.audio_playing_at = None
        # Frames and the sound that was current when each was drawn, so the
        # offset is between two streams rather than between a stream and a clock.
        self.frame_marks = []
        # The offset between the picture being drawn and the sound being played,
        # in milliseconds: positive means the picture is behind the sound.
        self.av_offsets = []
        self.start = time.monotonic()

    # --- the clock --------------------------------------------------------

    def clock_ms(self) -> float:
        with self.lock:
            return self._clock_ms_locked()

    def _clock_ms_locked(self) -> float:
        """The clock, assuming the lock is already held."""
        if self.clock_origin is None:
            return 0.0
        wall = (time.monotonic() - self.clock_origin) * 1000.0
        submitted = self.submitted_samples * 1000.0 / self.sample_rate
        return min(wall, submitted)

    # --- receiving --------------------------------------------------------

    def feed_audio(self, pts, payload):
        with self.lock:
            if len(self.audio) >= 960:        # PCM_QUEUE, 24 x 40 ms
                self.audio.popleft()
            self.audio.append((pts, len(payload), time.monotonic()))
            # What the sound at the head of the queue is, and when it got here.
            # The offset is measured against THIS rather than against the clock,
            # because the clock and the timestamps do not share an origin: the
            # clock starts at the first audio write plus the DMA estimate, while
            # the timestamps start at the server's own origin a lead-time
            # earlier. Subtracting one from the other gave a constant -2486 ms
            # that said nothing about synchronisation.
            #
            # Two packets that arrived together carry the same moment of
            # content, so comparing their timestamps compares the two streams
            # directly and needs no origin at all.
            # NOT the head of the queue: the head is what is about to play and
            # is only known when it is popped. This is the newest arrival, and
            # it was used as though it were the head -- which happens to be
            # close while the queue is being drained at the rate it fills, and
            # is wildly wrong the moment anything is discarded. Measured on a
            # fast link it reported a median offset of over a second and a P90
            # of twenty, which is not a synchronisation error at all.
            self.audio_newest_pts = pts
            self.last_packet_at = time.monotonic()
            if self.first_media_at is None:
                self.first_media_at = self.last_packet_at

    def feed_video(self, pts, payload, continues):
        """One picture packet, with the receiver's buffer rules applied."""
        with self.lock:
            self.last_packet_at = time.monotonic()
            if self.first_media_at is None:
                self.first_media_at = self.last_packet_at
            # A buffer is needed to receive into. None free means the packet is
            # lost and the frame it belongs to cannot complete.
            if self.free_buffers <= 0:
                self.dropped_nobuf += 1
                return
            # The queue is depth two. A third packet with nowhere to go is
            # discarded the same way, which is how a split frame loses its
            # second half.
            if len(self.video) >= VIDEO_QUEUE_DEPTH:
                self.dropped_queue_full += 1
                return
            self.free_buffers -= 1
            self.video.append((pts, payload, continues, time.monotonic()))

    # --- the two tasks ----------------------------------------------------

    def audio_task(self, stop: threading.Event):
        """Pop one chunk every chunk_ms, play silence when there is none."""
        while not stop.is_set():
            time.sleep(self.chunk_ms / 1000.0)
            now = time.monotonic()
            got = None
            with self.lock:
                if self.audio:
                    got = self.audio.popleft()
                    self.audio_playing_pts = got[0]
                    self.audio_playing_at = got[2]
                else:
                    # The queue is empty. This is where the firmware used to end
                    # the session; it now plays silence and keeps the clock
                    # honest by advancing it only by what was really played.
                    if self.clock_origin is None:
                        continue
                    if not hasattr(self, "_starved_since") or self._starved_since is None:
                        self._starved_since = now
                    starved_ms = (now - self._starved_since) * 1000.0
                    if starved_ms > AUDIO_SILENCE_MAX_MS:
                        self.failed = f"audio starved beyond budget ({starved_ms:.0f} ms)"
                        return
            if got is not None:
                with self.lock:
                    if hasattr(self, "_starved_since") and self._starved_since is not None:
                        self.silence_episodes += 1
                        self.silence_ms_total += (now - self._starved_since) * 1000.0
                        self._starved_since = None
            # Feeding the codec, real or silent, advances the submitted count
            # either way -- that is what makes the clock describe what played.
            with self.lock:
                if self.clock_origin is None:
                    self.clock_origin = now + CLOCK_ORIGIN_OFFSET_MS / 1000.0
                self.submitted_samples += int(self.sample_rate * self.chunk_ms / 1000)
                if self.last_audio_feed is not None:
                    gap_ms = (now - self.last_audio_feed) * 1000.0
                    if gap_ms > AUDIO_FEED_GAP_MS:
                        self.failed = f"audio feed gap {gap_ms:.0f} ms exceeds budget"
                        return
                self.last_audio_feed = now

    def video_task(self, stop: threading.Event):
        """Take one packet at a time and apply the frame rules."""
        while not stop.is_set():
            with self.lock:
                item = self.video.popleft() if self.video else None
            if item is None:
                time.sleep(0.005)
                continue
            pts, payload, continues, arrived = item
            offset = None
            # The critical section is kept to bookkeeping only. Everything that
            # can be computed from `payload` alone is done OUTSIDE it, because
            # the read loop needs this lock to hand packets over and a slow
            # section here delays the socket -- which is what timed out the
            # server in the first run.
            first, count = payload[0], payload[1]
            with self.lock:
                if not continues:
                    # A new frame's opening packet. If the previous frame never
                    # completed, this is where its loss is counted.
                    if self.frame_started and any(s is None for s in self.stripes):
                        self.dropped_incomplete += 1
                    self.stripes = [None] * STRIPES
                    self.frame_pts = pts
                    self.frame_started = True
                # The lateness rule, applied once per frame at its first packet.
                if not continues:
                    behind = self._clock_ms_locked() - pts
                    if behind > VIDEO_LATE_DROP_MS:
                        self.dropped_late += 1
                        self.stripes = [None] * STRIPES
                        self.free_buffers += 1
                        continue
                for i in range(count):
                    self.stripes[first + i] = True
                self.free_buffers += 1
                if all(s is not None for s in self.stripes):
                    self.drawn += 1
                    if self.audio_playing_pts is not None:
                        # Positive means the picture is showing a LATER moment
                        # than the sound is playing, i.e. the picture leads.
                        # The same quantity the server reports as v_lag: how far
                        # the picture being drawn is behind the sound being
                        # played, measured in ARRIVAL times rather than in
                        # timestamps. Two packets that arrived together carry
                        # the same moment of content, so the difference is a
                        # real lip-sync error and needs no origin.
                        #
                        # It replaces a subtraction of timestamps that reported
                        # +3.0 s on a link where the server said -0.2 s: the two
                        # were measuring different things and only this one
                        # answers "are they in step".
                        self.frame_marks.append((arrived - self.audio_playing_at) * 1000.0)
                    self.stripes = [None] * STRIPES
                    self.frame_started = False

    # --- the summary ------------------------------------------------------

    def report(self, seconds):
        with self.lock:
            offs = list(self.frame_marks)
        n = len(offs)
        offs_sorted = sorted(offs)
        line = [
            f"时长 {seconds:.0f}s",
            f"会话{'失败: ' + self.failed if self.failed else '存活'}",
            f"成帧 {self.drawn}",
            f"实得帧率 {self.drawn / max(1.0, seconds):.1f}",
            f"迟到丢弃 {self.dropped_late}",
            f"不完整丢弃 {self.dropped_incomplete}",
            f"无缓冲 {self.dropped_nobuf}",
            f"队列满 {self.dropped_queue_full}",
            f"静音 {self.silence_episodes} 次共 {self.silence_ms_total:.0f} ms",
        ]
        if n:
            # Against the sound being played, not against the clock. Positive
            # means the picture is ahead of the sound.
            line.append(f"音画(帧pts-声pts) 中位 {offs_sorted[n // 2]:+.0f} ms "
                        f"P90 {offs_sorted[int(n * 0.9)]:+.0f} ms "
                        f"最大 {offs_sorted[-1]:+.0f} ms")
        return "  ".join(line)


class Link:
    """A simulated Wi-Fi link: a bandwidth, and stalls drawn from a distribution.

    Reading slowly is the whole mechanism. The server's loop writes into a
    socket; when this stops taking bytes the socket buffer fills, the write
    blocks, and the sender behaves exactly as it does on a real link that has
    gone busy. Nothing is faked about the backpressure.

    The stall lengths default to the distribution measured on the real link --
    round-trip times between 5 and 334 ms on 1200-byte pings, with a mean near
    40 and occasional excursions past 300. A lost TCP segment does not appear
    here as lost bytes, because TCP would retransmit them; it appears as a
    stall, which is what the device actually experiences.
    """

    def __init__(self, bandwidth, stall_every_ms, stall_ms, seed):
        self.bandwidth = float(bandwidth)
        self.stall_every_ms = float(stall_every_ms)
        self.stall_ms = float(stall_ms)
        self.rng = random.Random(seed)
        self.allowance = 0.0
        self.last = time.monotonic()
        self.next_stall = self.last + self.rng.uniform(0, stall_every_ms) / 1000.0
        self.stall_until = 0.0
        self.stalls = 0

    def wait_for(self, nbytes):
        """Block until the link can carry `nbytes`, then release them."""
        while True:
            now = time.monotonic()
            if now < self.stall_until:
                time.sleep(min(0.01, self.stall_until - now))
                continue
            if now >= self.next_stall and self.stall_ms > 0:
                # Not every excursion is the same size: the measured tail runs
                # to several times the mean, so the length is drawn per stall.
                length = self.rng.expovariate(1.0 / (self.stall_ms / 1000.0))
                self.stall_until = now + min(length, 1.5)
                self.next_stall = self.stall_until + self.rng.uniform(0, self.stall_every_ms) / 1000.0
                self.stalls += 1
                continue
            self.allowance += (now - self.last) * self.bandwidth
            self.last = now
            if self.allowance >= nbytes:
                self.allowance -= nbytes
                return
            need = (nbytes - self.allowance) / self.bandwidth
            time.sleep(min(0.005, max(0.0005, need)))


class Simulator:
    """Talks FAV1 to the server, and reports what the device would have seen."""

    def __init__(self, host, port, channel, seconds, link, device, token=None):
        self.host, self.port = host, port
        self.channel = channel
        self.seconds = seconds
        self.link = link
        self.device = device
        self.token = token
        self.sock = None

    # --- wire -------------------------------------------------------------

    def read_exact(self, n):
        """Read n bytes, paying the link for them as a whole.

        The link is charged per RECEIVE rather than per byte, and that is not a
        detail: charging per byte here meant 4096 wait_for calls for a 4 KB
        packet, each with its own sleep, and the read loop fell so far behind
        that the server's writes timed out -- the simulator failing at the thing
        it was built to measure. The bandwidth accounting inside wait_for is
        already continuous, so asking once for the whole block is both correct
        and fast.
        """
        buf = bytearray()
        while len(buf) < n:
            self.link.wait_for(n - len(buf))
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("peer closed")
            buf.extend(chunk)
        return bytes(buf)

    def read_header(self):
        wire = self.read_exact(24)
        magic, version, kind, flags = struct.unpack(">4sBBH", wire[:8])
        if magic != b"FAV1":
            raise ValueError(f"bad magic {magic!r}")
        session, seq, pts, length = struct.unpack(">IIII", wire[8:24])
        return kind, flags, session, seq, pts, length

    def send(self, kind, session, seq, pts, payload):
        wire = struct.pack(">4sBBHIIII", b"FAV1", 1, kind, 0,
                           session, seq, pts, len(payload)) + payload
        self.sock.sendall(wire)

    # --- the session ------------------------------------------------------

    def run(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        hello = json.dumps({"channel": self.channel, "version": 1}).encode()
        if self.token:
            hello = json.dumps({"channel": self.channel, "version": 1,
                                "token": self.token}).encode()
        self.send(Kind.HELLO, 0, 0, 0, hello)
        print(f"→ HELLO channel={self.channel}", flush=True)

        stop = threading.Event()
        threading.Thread(target=self.device.audio_task, args=(stop,), daemon=True).start()
        threading.Thread(target=self.device.video_task, args=(stop,), daemon=True).start()

        session = None
        deadline = time.monotonic() + self.seconds
        try:
            while time.monotonic() < deadline:
                kind, flags, s, seq, pts, length = self.read_header()
                payload = self.read_exact(length) if length else b""
                if kind == Kind.CONFIG:
                    session = s
                    cfg = json.loads(payload)
                    print(f"← CONFIG {cfg.get('width')}x{cfg.get('height')} "
                          f"fps={cfg.get('fps')} video_max={cfg.get('video_max_bytes')}",
                          flush=True)
                elif kind == Kind.PALETTE:
                    pass
                elif kind == Kind.PCM:
                    self.device.feed_audio(pts, payload)
                elif kind == Kind.JPEG:
                    self.device.feed_video(pts, payload, bool(flags & 0x01))
                elif kind == Kind.END:
                    print("← END", flush=True)
                    break
                elif kind == Kind.ERROR:
                    print(f"← ERROR {payload.decode('utf-8', 'replace')}", flush=True)
                    break
                if self.device.failed:
                    break
        except (EOFError, ConnectionResetError, socket.timeout) as e:
            print(f"连接结束：{type(e).__name__} {e}", flush=True)
        finally:
            stop.set()
            try:
                self.sock.close()
            except OSError:
                pass


def main() -> int:
    # Before anything is printed: on Windows the first Chinese message raises
    # UnicodeEncodeError and ends the process. Every entry point in this repo
    # does this, and this one did not -- caught by test_console.py, which exists
    # for exactly that.
    use_utf8()
    ap = argparse.ArgumentParser(description="一台用软件写的设备，连真实服务端")
    ap.add_argument("--bind", default="192.168.0.125")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--channel", default="ch013")
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--token", default=None)
    # The link, defaulting to what the real one measured.
    ap.add_argument("--bandwidth", type=float, default=160_000,
                    help="字节/秒；真机实测 110000–160000")
    ap.add_argument("--stall-every-ms", type=float, default=2500,
                    help="平均多久停顿一次")
    ap.add_argument("--stall-ms", type=float, default=120,
                    help="停顿的平均时长（按指数分布抽样）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--quiet-picture", action="store_true",
                    help="只收音频，不看画面（用于分离故障）")
    args = ap.parse_args()

    link = Link(args.bandwidth, args.stall_every_ms, args.stall_ms, args.seed)
    device = Device()
    sim = Simulator(args.bind, args.port, args.channel, args.seconds, link,
                    device, args.token)
    started = time.monotonic()
    try:
        sim.run()
    except KeyboardInterrupt:
        pass
    elapsed = time.monotonic() - started

    print()
    print("=" * 72)
    print(f"链路  带宽 {args.bandwidth/1000:.0f} kB/s  "
          f"每 {args.stall_every_ms:.0f} ms 停顿一次、平均 {args.stall_ms:.0f} ms"
          f"（实际停顿 {link.stalls} 次）")
    print(f"设备  {device.report(elapsed)}")
    if device.failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
