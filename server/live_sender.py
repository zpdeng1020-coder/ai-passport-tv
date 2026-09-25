"""Packet-boundary live sender, selected with TV_LIVE_ENGINE=v2.

One socket has one writer and one pending FAV1 packet. Audio may preempt
video between complete packets, never inside a payload. LiveChannel owns
media/wire timestamps. Local pacing never claims device/acoustic progress.
"""
from __future__ import annotations

import collections
import os
import select
import time
from dataclasses import dataclass

from . import frames
from .media import AUDIO_CHUNK_MS, FPS
from .protocol import HEADER, MAGIC, VERSION, Packet, Kind, ProtocolError, VIDEO_CONTINUES
from . import rate
from .rate import FixedRate, RateController


@dataclass
class AudioSchedule:
    next_due: float
    lead_s: float = 0.28
    resets: int = 0

    def due(self, now: float) -> bool:
        return self.next_due - now <= self.lead_s

    def recover(self, now: float) -> bool:
        # Limit catch-up to one cushion; this changes no media timestamp.
        if now - self.next_due > 0.08:
            self.next_due = now
            self.resets += 1
            return True
        return False

    def sent(self) -> None:
        self.next_due += AUDIO_CHUNK_MS / 1000.0


@dataclass
class PendingWrite:
    packet: Packet
    started: float
    deadline: float
    last_part: bool = False
    stripes: int = 0

    def __post_init__(self):
        self.data = self.packet.encode()
        self.offset = 0

    def step(self, connection, now: float) -> bool:
        if now >= self.deadline:
            raise TimeoutError('live v2 partial packet deadline; close connection')
        try:
            count = connection.send(memoryview(self.data)[self.offset:self.offset + 4096])
        except BlockingIOError:
            return False
        if count == 0:
            raise EOFError('peer disconnected')
        self.offset += count
        return self.offset == len(self.data)


class EndReader:
    """Retain fragmented END bytes without blocking media or losing a prefix."""
    def __init__(self):
        self.data = bytearray()
        self.started = None

    def check_deadline(self, now: float) -> None:
        if self.started is not None and now - self.started >= 1:
            raise TimeoutError('live v2 incomplete control header; close connection')

    def step(self, connection, session: int, now: float) -> bool:
        self.check_deadline(now)
        try:
            block = connection.recv(HEADER.size - len(self.data))
        except BlockingIOError:
            return False
        if not block:
            raise EOFError('peer disconnected without END')
        if self.started is None:
            self.started = now
        self.data.extend(block)
        if len(self.data) < HEADER.size:
            return False
        magic, version, kind, flags, received_session, _, _, size = HEADER.unpack(self.data)
        if (magic, version, kind, flags, received_session, size) != (MAGIC, VERSION, Kind.END, 0, session, 0):
            raise ProtocolError('live v2 expected END for current session')
        return True


def repack(parts: list[bytes], target: int) -> list[bytes]:
    """Change FAV1 boundaries without decompressing or altering a stripe."""
    if not 4 <= target <= frames.VIDEO_MAX:
        raise ValueError('invalid live packet target')
    stripes, expected = [], 0
    for part in parts:
        if len(part) < 4:
            raise ValueError('short stripe packet')
        first, count = part[:2]
        table_end = 2 + 2 * count
        if first != expected or not count or first + count > frames.STRIPES or table_end > len(part):
            raise ValueError('noncontiguous stripe packet')
        lengths = [int.from_bytes(part[2 + 2*i:4 + 2*i], 'big') for i in range(count)]
        if not all(lengths) or table_end + sum(lengths) != len(part):
            raise ValueError('invalid stripe lengths')
        for size in lengths:
            stripes.append(part[table_end:table_end + size])
            table_end += size
        expected += count
    if expected != frames.STRIPES:
        raise ValueError('incomplete source frame')
    output, group, first, total = [], [], 0, 0
    for stripe in stripes:
        if len(stripe) + 4 > target:
            raise ValueError('one stripe exceeds packet target; cannot split this protocol unit')
        if group and 2 + 2 * (len(group) + 1) + total + len(stripe) > target:
            output.append(frames.packet(first, group))
            first += len(group)
            group, total = [], 0
        group.append(stripe)
        total += len(stripe)
    if group:
        output.append(frames.packet(first, group))
    return output


class LiveSender:
    def __init__(self, server, connection, channel, session):
        self.server, self.connection, self.channel, self.session = server, connection, channel, session
        self.packet_target = int(os.environ.get('TV_LIVE_PACKET_BYTES', '6144'))
        if not frames.STRIPE_PIXELS + 128 <= self.packet_target <= frames.VIDEO_MAX:
            raise ValueError('live packet target must hold one worst-case native stripe')
        self.packet_timeout = float(os.environ.get('TV_LIVE_PACKET_TIMEOUT_S', '1.0'))
        self.stripe_pace = float(os.environ.get('TV_STRIPE_PACE_MS', '5')) / 1000
        if not 0.05 <= self.packet_timeout <= 2.5 or not 0 <= self.stripe_pace <= 0.02:
            raise ValueError('live v2 timing setting outside candidate bounds')
        self.started = time.monotonic()
        self.audio = AudioSchedule(self.started)
        if os.environ.get('TV_FIXED_FPS'):
            fixed_fps = int(os.environ['TV_FIXED_FPS'])
            budget = int(os.environ.get('TV_VIDEO_BUDGET', '300000'))
            self.rate = FixedRate(fixed_fps, budget=budget)
            self.maximum_video_budget = float(budget)
            self.video_budget = float(budget)
        else:
            min_fps = max(1, int(os.environ.get('TV_MIN_FPS', '1')))
            max_fps = max(min_fps, int(os.environ.get('TV_MAX_FPS', str(FPS))))
            start_fps = int(os.environ.get('TV_START_FPS', '3'))
            start_fps = max(min_fps, min(max_fps, start_fps))
            self.rate = RateController(minimum=min_fps, maximum=max_fps, start=start_fps)
            self.maximum_video_budget = float(os.environ.get('TV_VIDEO_BUDGET', str(self.rate.budget)))
            if os.environ.get('TV_VIDEO_BUDGET'):
                self.video_budget = float(os.environ['TV_VIDEO_BUDGET'])
            else:
                self.video_budget = float(min(64000.0, self.maximum_video_budget))
        self.good_windows = 0
        self.server._last_controller = self.rate
        self.pending = None
        self.control = EndReader()
        self.parts = collections.deque()
        self.frame_pts = self.part_index = self.frame_wire_bytes = 0
        self.next_video = self.next_part = self.started
        self.seq = 2  # CONFIG=0, PALETTE=1
        self.frames_sent = self.video_packets = 0
        self.audio_starved_since = None
        self.last_report = self.last_rate = self.started
        self.rate_bytes = self.rate_frames = 0
        self.rate_slowest = 0.0
        self.last_audio = self.last_frames = self.last_packets = 0
        self.audio_max_gap = 0.0
        self.last_audio_write = None
        self.server.frames_sent = 0
        self.server.wire_bytes = 0

    def choose(self, now: float) -> None:
        s, ch = self.server, self.channel
        if ch.audio_pending() and self.audio.recover(now):
            s.logger(f'LIVE2_SCHEDULE_REBUFFER session={self.session} audio_packets={s.audio_sent} '
                     f'resets={self.audio.resets} device_clock=unmeasured')
        if ch.audio_pending() and self.audio.due(now):
            taken = ch.pop_audio()
            if taken is not None:
                block, stamp = taken
                self.pending = PendingWrite(Packet(Kind.PCM, self.session, self.seq, stamp, block),
                                            now, now + self.packet_timeout)
                return
        if s.media_filter == 'audio':
            return
        # Fill the audio cushion before admitting another video packet.
        if self.audio.due(now) or now < self.next_part:
            return
        if not self.parts:
            if now < self.next_video or not ch.video_pending():
                return
            chosen = ch.pop_video()
            if chosen is None:
                return
            frame, self.frame_pts = chosen
            self.parts.extend(repack(frame, self.packet_target))
            self.frame_wire_bytes = sum(len(part) + 24 for part in self.parts)
            self.part_index = 0
            self.next_video = now + 1 / self.rate.fps
        part = self.parts.popleft()
        self.pending = PendingWrite(Packet(Kind.JPEG, self.session, self.seq, self.frame_pts,
                                            part, VIDEO_CONTINUES if self.part_index else 0),
                                    now, now + self.packet_timeout,
                                    last_part=not self.parts, stripes=part[1])
        self.part_index += 1

    def complete(self, now: float) -> None:
        p, s = self.pending, self.server
        s.wire_bytes += len(p.data)
        self.rate_slowest = max(self.rate_slowest, now - p.started)
        self.seq += 1
        if p.packet.kind == Kind.PCM:
            self.audio.sent()
            s.audio_sent += 1
            if self.last_audio_write is not None:
                self.audio_max_gap = max(self.audio_max_gap, now - self.last_audio_write)
            self.last_audio_write = now
        else:
            self.video_packets += 1
            s.video_sent += 1  # historical field is explicitly a packet count
            # Admission is byte-paced as well as frame-paced. Socket writable
            # only means local buffer space; it does not prove downstream room.
            if os.environ.get('TV_INTRA_BURST', '1') == '1':
                if p.last_part:
                    self.frames_sent += 1
                    s.frames_sent = self.frames_sent
                    self.rate_frames += 1
                    self.rate_bytes += self.frame_wire_bytes
                    interval = max(p.stripes * self.stripe_pace, self.frame_wire_bytes / self.video_budget)
                    self.next_part = max(now, p.started + interval)
                    self.next_video = max(self.next_video, p.started + interval, now)
                else:
                    # Intra-frame: pace by stripe decode time (~14ms for dual 8/7 stripe split)
                    # to eliminate visible screen wipe while preventing nobuf packet overrun.
                    intra_max = float(os.environ.get('TV_INTRA_PACE_MAX_S', '0.014'))
                    intra_interval = min(max(0.010, p.stripes * self.stripe_pace), intra_max)
                    self.next_part = max(now, p.started + intra_interval)
            else:
                interval = max(p.stripes * self.stripe_pace, len(p.data) / self.video_budget)
                self.next_part = max(now, p.started + interval)
                if p.last_part:
                    self.frames_sent += 1
                    s.frames_sent = self.frames_sent
                    self.rate_frames += 1
                    self.rate_bytes += self.frame_wire_bytes
                    self.next_video = max(self.next_video, now)
        self.pending = None

    def report(self, now: float) -> None:
        if now - self.last_rate >= 1:
            if isinstance(self.rate, RateController):
                if self.rate_slowest > 0.15:
                    floor = 8000.0 if not os.environ.get('TV_MIN_VIDEO_BUDGET') else float(os.environ['TV_MIN_VIDEO_BUDGET'])
                    self.video_budget = max(floor, self.video_budget * .85)
                    self.good_windows = 0
                elif self.rate_frames and self.rate_slowest < 0.08:
                    self.good_windows += 1
                    if self.good_windows >= 2:
                        self.video_budget = min(self.maximum_video_budget, self.video_budget * 1.1)
                        self.good_windows = 0
                else:
                    self.good_windows = 0
                self.rate.budget = int(self.video_budget)
            self.rate.observe(self.rate_bytes, self.rate_slowest * 1000,
                              frames=self.rate_frames, window_s=now - self.last_rate)
            self.rate_bytes = self.rate_frames = 0
            self.rate_slowest = 0.0
            self.last_rate = now
        if now - self.last_report < 5:
            return
        dt = now - self.last_report
        flow = self.channel.flow_snapshot() if hasattr(self.channel, 'flow_snapshot') else {}
        self.server.logger(
            f'LIVE2 session={self.session} wall_s={now-self.started:.3f} '
            f'target_fps={self.rate.fps} frame_tx_fps={(self.frames_sent-self.last_frames)/dt:.3f} '
            f'video_budget_bps={self.video_budget:.0f} '
            f'video_packets_s={(self.video_packets-self.last_packets)/dt:.3f} '
            f'audio_packets_s={(self.server.audio_sent-self.last_audio)/dt:.3f} '
            f'frames_tx={self.frames_sent} video_packets={self.video_packets} '
            f'audio_packets={self.server.audio_sent} audio_gap_max_ms={self.audio_max_gap*1000:.1f} '
            f'source={flow} device_fps=unmeasured')
        self.last_report = now
        self.last_frames, self.last_packets = self.frames_sent, self.video_packets
        self.last_audio = self.server.audio_sent
        self.audio_max_gap = 0.0

    def run(self) -> None:
        s, ch, conn = self.server, self.channel, self.connection
        if s.media_filter == 'video':
            raise ValueError('video-only diagnosis requires TV_LIVE_ENGINE=legacy')
        s.logger(f'LIVE2_START session={self.session} packet_bytes={self.packet_target} '
                 f'stripe_pace_ms={self.stripe_pace*1000} start_fps={self.rate.fps} '
                 f'max_fps={FPS} adaptive={1 if isinstance(self.rate, RateController) else 0} '
                 f'device_feedback=unmeasured')
        while not s.stop.is_set():
            now = time.monotonic()
            self.control.check_deadline(now)
            if ch.failure():
                from .live import LiveError
                raise LiveError('transcode stopped')
            if s.session_limit_s is not None and now - self.started >= s.session_limit_s:
                return
            if self.pending is None:
                if s.fault_injector is not None:
                    ok, _ = s.fault_injector.check_pause(conn, self.session, s)
                    if not ok:
                        return
                    now = time.monotonic()
                if not self.parts and hasattr(ch, 'trim_backlog'):
                    trimmed = ch.trim_backlog()
                    if trimmed:
                        s.logger(f'LIVE2_BACKLOG_TRIM session={self.session} audio_chunks={trimmed}')
                if self.audio.due(now) and not ch.audio_pending():
                    if self.audio_starved_since is None:
                        self.audio_starved_since = now
                    elif now - self.audio_starved_since > 3.0:
                        raise TimeoutError('live v2 upstream audio starved for 3 s; reconnect')
                elif not self.audio.due(now):
                    self.audio_starved_since = None
                self.choose(now)
            readers = [conn]
            if s.listener is not None:
                readers.append(s.listener)
            ready_r, ready_w, _ = select.select(readers, [conn] if self.pending else [], [], 0.005)
            if s.listener in ready_r:
                s._reject_waiting()
            if conn in ready_r and self.control.step(conn, self.session, time.monotonic()):
                return
            now = time.monotonic()
            if self.pending:
                if now >= self.pending.deadline:
                    raise TimeoutError('live v2 packet deadline; close connection')
                s.phase = 'live_pcm_send' if self.pending.packet.kind == Kind.PCM else 'live_video_send'
                if ready_w and self.pending.step(conn, now):
                    self.complete(time.monotonic())
            self.report(time.monotonic())
