"""Time every pass of the live send loop, and every wire event it causes.

The server's own logs could not answer "what is the loop doing" because they
report once a second and a struggling session keeps restarting, so the reports
land in the first fraction of a second and say nothing. This attaches to the
real sender and records the shape of the loop directly:

  * how long each pass took, and what it did in that pass;
  * how long each individual write blocked;
  * the longest gap between two audio packets, which is the number the device
    actually cares about -- it underruns at 300 ms.

Run it with the server paused, point the device at it, and read the summary.
It speaks the real protocol, so nothing on the device changes.

    python3 tools/loop_trace.py --bind 192.168.0.125 --channel ch000
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.live import CHANNELS, CHANNEL_AGENTS, LiveChannel
from server.protocol import (AUDIO_BYTES, IO_TIMEOUT, Packet, Kind, json_bytes,
                             receive_packet, send_packet)
from server import frames
from server.media import AUDIO_CHUNK_MS, AUDIO_LEAD_MS, FPS

AUDIO_MAX_LOOKAHEAD_MS = 240
# How long one frame may occupy the send loop before it is abandoned.
VIDEO_WRITE_BUDGET_S = 0.020


def config(session: int, channel: str) -> dict:
    return {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": FPS,
            "sample_rate": 16000, "channels": 1, "sample_bits": 16,
            "audio_chunk_ms": AUDIO_CHUNK_MS, "video_max_bytes": frames.VIDEO_MAX,
            "stripe_rows": frames.STRIPE_ROWS, "start_delay_ms": 200,
            "audio_lead_ms": AUDIO_LEAD_MS, "video_lead_ms": AUDIO_LEAD_MS,
            "session": session, "channel": channel, "duration_ms": 0}


class Trace:
    def __init__(self):
        self.started = time.monotonic()
        self.span = 0.0
        self.decisions = []
        self.lock = threading.Lock()
        self.passes = []
        self.audio_gaps = []
        self.write_ms = []
        self.last_audio = None

    def note_pass(self, ms, kind):
        with self.lock:
            self.passes.append((ms, kind))

    def note_write(self, ms):
        with self.lock:
            self.write_ms.append(ms)

    def note_audio(self, now):
        with self.lock:
            if self.last_audio is not None:
                self.audio_gaps.append((now - self.last_audio) * 1000)
            self.last_audio = now

    def report(self, seconds):
        seconds = max(0.001, getattr(self, 'span', seconds))
        with self.lock:
            if not self.passes:
                print("no passes recorded", flush=True)
                return
            slow = sorted(self.passes, key=lambda p: -p[0])[:8]
            kinds = {}
            for _, k in self.passes:
                kinds[k] = kinds.get(k, 0) + 1
            gaps = sorted(self.audio_gaps)
            writes = sorted(self.write_ms)
            print(f"--- {len(self.passes)} passes in {seconds:.0f}s "
                  f"({len(self.passes)/seconds:.0f}/s) ---", flush=True)
            print(f"  passes by kind: {kinds}", flush=True)
            print(f"  slowest passes (ms): "
                  f"{[(round(m, 1), k) for m, k in slow]}", flush=True)
            if gaps:
                print(f"  audio gaps ms: n={len(gaps)} max={gaps[-1]:.1f} "
                      f"median={gaps[len(gaps)//2]:.1f}", flush=True)
                # Every gap, in order. The shape says whether the sound ran
                # steadily and then stopped, or was never regular at all --
                # and the maximum alone cannot tell those apart.
                print(f"  gaps in order: "
                      f"{[round(g) for g in self.audio_gaps]}", flush=True)
            if self.decisions:
                print("  first passes:", flush=True)
                for line in self.decisions[:12]:
                    print(f"    {line}", flush=True)
            if writes:
                print(f"  writes ms: n={len(writes)} max={writes[-1]:.1f} "
                      f"p99={writes[int(len(writes)*0.99)]:.1f} "
                      f"median={writes[len(writes)//2]:.1f}", flush=True)


def timed_send(conn, packet, trace, deadline):
    start = time.monotonic()
    send_packet(conn, packet, deadline=deadline)
    trace.note_write((time.monotonic() - start) * 1000)


def serve(conn, channel_id, trace, seconds):
    conn.settimeout(10)
    receive_packet(conn, 10)
    session = 0x5EED0005
    send_packet(conn, Packet(Kind.CONFIG, session, 0, 0,
                             json_bytes(config(session, channel_id))))
    channel = LiveChannel(CHANNELS[channel_id], "ffmpeg",
                          CHANNEL_AGENTS.get(channel_id, ""))
    channel.build_palette()
    channel.start()
    send_packet(conn, Packet(Kind.PALETTE, session, 1, 0, channel.palette))

    # Wait for the transcode to fill its queues, exactly as the server does.
    # Without this the first seconds of a session have both queues empty, which
    # is not the condition under test and reads as a scheduling fault.
    waiting = time.monotonic()
    while not channel.prebuffered() and time.monotonic() - waiting < 30:
        if channel.failure():
            print(f"  transcode failed: {channel.failure()}", flush=True)
            return
        time.sleep(0.02)
    print(f"  prebuffered after {(time.monotonic()-waiting):.1f}s", flush=True)

    origin = time.monotonic() + max(0.2, AUDIO_LEAD_MS / 1000)
    seq = 2
    audio_pts = 0
    now = time.monotonic()
    last_video_pts = -1
    video_at = origin
    start = time.monotonic()
    try:
        while time.monotonic() - start < seconds:
            p0 = time.monotonic()
            now = p0
            # The picture's timestamp follows the sound's, and the sound is the
            # master clock. They used to be computed from different things --
            # video from the wall clock, audio from its own 20 ms per packet --
            # so they drifted apart and the device, which expects the two to
            # describe one timeline, ended up with the sound ahead of the
            # picture and refused the stream. Measured: audio_next_pts=220 while
            # video_pts=200.
            video_pts = max(audio_pts, last_video_pts + 1)
            audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            # The picture's slot is its own; deriving it from audio_at locks the
            # two to the same instant and the stricter audio priority then
            # starves the picture completely. See tv_server._pace_live.
            video_at = max(video_at, now - 1.0 / FPS)
            lookahead = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000

            readable, writables, _ = __import__("select").select(
                [conn], [conn], [], 0)
            writable = bool(writables)
            if conn in readable:
                trace.note_pass((time.monotonic() - p0) * 1000, "device-left")
                return
            audio_due = (now >= audio_at and channel.audio_pending()
                         and lookahead <= AUDIO_MAX_LOOKAHEAD_MS)
            if now - start < 2.0 and len(trace.decisions) < 30:
                trace.decisions.append(
                    f"t={(now-start)*1000:6.0f}ms a_due={int(now>=audio_at)} "
                    f"a_pend={int(channel.audio_pending())} "
                    f"look={lookahead:6.0f} wr={int(writable)} "
                    f"qa={len(channel.audio)} qv={len(channel.video)}")
            if audio_due and writable:
                block = channel.pop_audio()
                timed_send(conn, Packet(Kind.PCM, session, seq, audio_pts, block),
                           trace, now + 0.25)
                trace.note_audio(time.monotonic())
                audio_pts += AUDIO_CHUNK_MS
                seq += 1
                trace.note_pass((time.monotonic() - p0) * 1000, "audio")
                continue
            now = time.monotonic()
            if now >= video_at and channel.video_pending() and writable:
                frame = channel.pop_video()
                video_at = video_at + 1.0 / FPS
                # A picture may never hold the loop for longer than the sound
                # can go without. The device underruns at 300 ms, so the whole
                # frame gets a budget well inside that; a frame that cannot be
                # placed in that time is dropped and the next audio chunk goes
                # immediately. Measured before this: one video pass took 250 ms,
                # the audio gap behind it was 343 ms, and the session died.
                budget = now + VIDEO_WRITE_BUDGET_S
                try:
                    for n, part in enumerate(frame):
                        timed_send(conn, Packet(Kind.JPEG, session, seq, video_pts,
                                                part, 1 if n else 0), trace, budget)
                        seq += 1
                except TimeoutError:
                    pass
                last_video_pts = video_pts
                trace.note_pass((time.monotonic() - p0) * 1000, "video")
                continue
            trace.note_pass((time.monotonic() - p0) * 1000, "idle")
            time.sleep(0.002)
    finally:
        channel.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="192.168.0.125")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--channel", default="ch000")
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.port))
    listener.listen(8)
    print(f"listening on {args.bind}:{args.port}", flush=True)
    trace = Trace()
    while True:
        conn, peer = listener.accept()
        print(f"device {peer[0]} connected", flush=True)
        try:
            conn.setblocking(False)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
            serve(conn, args.channel, trace, args.seconds)
        except Exception as error:
            print(f"  session ended: {type(error).__name__}: {error}", flush=True)
        finally:
            conn.close()
        trace.span = time.monotonic() - trace.started
        trace.report(args.seconds)
        trace = Trace()


if __name__ == "__main__":
    raise SystemExit(main())
