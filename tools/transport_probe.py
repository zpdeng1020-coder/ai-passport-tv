"""Measure how fast the device can actually receive, without ffmpeg in the way.

This speaks the same wire protocol as the real server and sends valid indexed
frames -- real stripes, real zlib, real lengths -- but it makes them itself, at
a rate and packet size chosen on the command line. It exists because every
measurement taken so far has had a live transcode and a scheduler between the
question and the answer, and the question is now only about the link: how many
bytes a second reach the device, and how does that change with packet size.

The device needs no changes. It renders whatever arrives, and its interval log
reports rx_pkts, rx_bps, io_ms and iters, which is the whole measurement.

    python3 tools/transport_probe.py --port 8096 --bind 192.168.0.114 \
        --fps 4 --stripes-per-packet 7 --seconds 60 --audio

Diagnostic only. This is not the server; it serves one connection and stops.

**--audio is required, not optional.** The device's audio task waits for a
reserve of sound after CONFIG and fails the session outright without it, so a
picture-only run measures the handshake rather than the link.

**The sound is interleaved between the picture's packets, not only between
frames.** A 17 kB frame occupies the link for 80-90 ms, which is the whole of
the device's DMA buffer, and the device's drawing clock IS its sound clock
(`submitted_samples / 16000`). Feeding the sound only between frames starves
that clock and the device renders 1.1-2.5 fps while receiving everything --
the exact figure the firmware's own source records for a starved clock
(main/av_player.c:2294). Five defects in this probe produced readings that
looked like device limits; each is written up at the place it was fixed,
because an instrument that is wrong in the flattering direction is the one
that ends an investigation early.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.protocol import (AUDIO_BYTES, Kind, Packet, json_bytes,
                             receive_packet, send_packet)
from server.media import AUDIO_CHUNK_MS
from server import frames

# A stripe that inflates to exactly the right size, with its compressed size set
# by how often a value repeats. Raw noise would be 5131 bytes a stripe and would
# not fit in a packet at all; flat colour would compress to nothing and measure
# a packet size no real channel ever sends. `run` is the knob between them:
# measured on real channels, a stripe of live television compresses to between
# 300 bytes and 3 KB, so run=8 or so is the realistic range.
def stripe(fill: int, run: int) -> bytes:
    state = 0x1234_5678 ^ (fill * 2_654_435_761)
    out = bytearray()
    while len(out) < frames.STRIPE_PIXELS:
        state = (state * 1_103_515_245 + 12_345) & 0xFFFF_FFFF
        out += bytes(((state >> 16) & 0xFF,)) * run
    return bytes(out[:frames.STRIPE_PIXELS])


def frame_payloads(stripes_per_packet: int, fill: int, run: int) -> list[bytes]:
    raw = [zlib.compress(stripe(i + fill, run), 1) for i in range(frames.STRIPES)]
    payloads = []
    at = 0
    while at < frames.STRIPES:
        chunk = raw[at:at + stripes_per_packet]
        payloads.append(frames.packet(at, chunk))
        at += len(chunk)
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="192.168.0.114")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--fps", type=int, default=4)
    parser.add_argument("--stripes-per-packet", type=int, default=7)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--token", default="")
    parser.add_argument("--run", type=int, default=8, help="how often a value "
                        "repeats inside a stripe; 8 is about what live "
                        "television compresses to, 64 is a near-flat picture")
    parser.add_argument("--audio", action="store_true",
                        help="also send PCM, at the real rate, to match the "
                             "condition the picture failed under. Without it the "
                             "device refuses the session outright: its audio task "
                             "waits for a reserve of sound after CONFIG and fails "
                             "with \"no audio after CONFIG\" if none comes. "
                             "Measured: 102 sessions in 120 seconds, each about a "
                             "second long. A picture-only run against this "
                             "firmware does not measure the link, it measures the "
                             "handshake.")
    parser.add_argument("--tick-ms", type=float, default=5.0,
                        help="scheduler tick; audio and picture are interleaved "
                             "on it rather than sent in bursts")
    parser.add_argument("--audio-lead-ms", type=float, default=300.0,
                        help="how far the sound is kept ahead of the wall "
                             "clock, which is the cushion the device plays from")
    parser.add_argument("--write-timeout", type=float, default=0.5)
    parser.add_argument("--ramp", default="",
                        help="comma-separated fps steps, e.g. 1,2,4,8; each is "
                             "held for --step-seconds and its achieved rate "
                             "printed, which is the whole curve in one run")
    parser.add_argument("--step-seconds", type=float, default=12)
    args = parser.parse_args()
    if args.ramp:
        args.steps = [int(x) for x in args.ramp.split(",") if x]
    else:
        args.steps = [args.fps]
    if not args.audio:
        print("refusing: this firmware ends any session that sends no sound "
              "(see --audio). A run without it would measure the handshake, "
              "not the link.", file=sys.stderr)
        return 2

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.port))
    listener.listen(1)
    print(f"listening on {args.bind}:{args.port}; device should connect", flush=True)

    threads = []
    while True:
        connection, peer = listener.accept()
        print(f"connection from {peer[0]}", flush=True)
        thread = threading.Thread(target=serve, args=(connection, args), daemon=True)
        thread.start()
        threads.append(thread)


def serve(connection: socket.socket, args) -> None:
    try:
        run(connection, args)
    except (OSError, EOFError) as error:
        print(f"  session ended: {type(error).__name__}: {error}", flush=True)
    finally:
        connection.close()


def run(connection: socket.socket, args) -> None:
    connection.settimeout(10)
    hello = receive_packet(connection, 10)
    print(f"  HELLO {hello.kind.name} len={len(hello.payload)}", flush=True)
    session = 0x5EED0001
    config = dict(frames_config())
    config["session"] = session
    config["channel"] = "probe"
    config["duration_ms"] = 0
    send_packet(connection, Packet(Kind.CONFIG, session, 0, 0, json_bytes(config)))
    send_packet(connection, Packet(Kind.PALETTE, session, 1, 0,
                                   bytes(frames.PALETTE_BYTES)))
    sample = frame_payloads(args.stripes_per_packet, 0, args.run)
    frame_bytes = sum(len(p) for p in sample)
    print(f"  CONFIG + PALETTE sent; {args.fps} fps, "
          f"{args.stripes_per_packet} stripes a packet, "
          f"{len(sample)} packets/frame, {frame_bytes} B/frame, "
          f"needs {frame_bytes*args.fps/1024:.1f} kB/s", flush=True)

    seq = 2
    pts = 0
    fill = 0
    start = time.monotonic()
    audio_pts = 0
    audio_at = start
    audio_bytes = 0
    step_begin = start
    step_index = 0
    step_frames = 0
    step_bytes = 0
    print(f"  ramp {args.steps} fps, {args.step_seconds}s each, "
          f"audio {'on, lead %.0f ms' % args.audio_lead_ms if args.audio else 'OFF'}"
          f", pt {args.stripes_per_packet} stripes/packet", flush=True)

    # One tick for both streams, and neither is ever sent in a burst.
    #
    # The shape this replaces sent, once per frame: every audio chunk that had
    # come due, and then a whole picture. At 4 fps that is twelve chunks of
    # 1280 bytes in one go followed by 17 kB of picture, twenty-five times a
    # second's worth of sound pushed out in four bursts -- which is not how any
    # real sender behaves and is not a condition the device was designed for.
    # The device's audio queue is 24 chunks with flow control at 20, so a burst
    # walks straight into the flow-control wait, during which it reads nothing
    # at all, and the picture is what goes unread.
    #
    # Measured with the burst: every session ended in `packet deadline expired`
    # on this side and `video-read`/`video-discard` on the device, twelve
    # sessions in 110 seconds. Those resets were the instrument, and reading
    # them as a property of the link would have been the same mistake this
    # project has made repeatedly.
    tick = args.tick_ms / 1000.0
    next_frame_at = start

    def send_audio(now: float) -> None:
        """Every chunk whose moment has passed, one packet each, never a burst."""
        nonlocal seq, audio_pts, audio_bytes
        while audio_pts <= (now - start) * 1000 + args.audio_lead_ms:
            try:
                send_packet(connection, Packet(Kind.PCM, session, seq,
                                               audio_pts, bytes(AUDIO_BYTES)),
                            deadline=now + args.write_timeout)
            except TimeoutError:
                raise
            seq += 1
            audio_pts += AUDIO_CHUNK_MS
            audio_bytes += AUDIO_BYTES

    while time.monotonic() - start < args.seconds:
        now = time.monotonic()
        fps = args.steps[min(step_index, len(args.steps) - 1)]
        if now - step_begin >= args.step_seconds:
            span = now - step_begin
            print(f"  STEP fps={fps:3d}  frames={step_frames:5d} "
                  f"video_kBps={step_bytes/span/1024:7.1f} "
                  f"audio_kBps={audio_bytes/span/1024:6.1f} "
                  f"total_kBps={(step_bytes+audio_bytes)/span/1024:7.1f}",
                  flush=True)
            step_index += 1
            step_begin = now
            step_frames = step_bytes = audio_bytes = 0
            if step_index >= len(args.steps):
                break
            fps = args.steps[step_index]
            next_frame_at = now

        if args.audio:
            send_audio(now)

        if now >= next_frame_at:
            payloads = frame_payloads(args.stripes_per_packet, fill, args.run)
            fill = (fill + 1) % 251
            # The picture is stamped with the SOUND's position, not the wall
            # clock, because that is the clock the device draws against.
            #
            # `estimated_pts()` in main/av_player.c is
            # `submitted_samples / 16000` -- how much sound the device has
            # actually taken -- and `main/av_player.c:2324` discards a frame
            # whose `pts` is more than 100 ms behind it. This probe keeps the
            # sound `--audio-lead-ms` ahead of the wall clock, so a picture
            # stamped with the wall clock is *by construction* that far behind
            # the sound, and every single frame was discarded as stale.
            #
            # Measured: the device received 97.4 kB/s and 134 packets while
            # rendering 1.4 complete frames a second, with `stray` climbing and
            # one `dropped` -- which reads as a device that cannot keep up and
            # was in fact a picture stamped on the wrong clock.
            #
            # This is the same defect, in the same shape, that the server's
            # `SessionClock` was written to remove. The probe reproduced it,
            # which is a point in favour of the server-side fix.
            pts = audio_pts
            frame_start = time.monotonic()
            sent = 0
            for n, payload in enumerate(payloads):
                # The sound goes out BETWEEN the picture's packets, not only
                # before the frame. This is what the real sender does, and
                # leaving it out is not a simplification: a 17 kB frame occupies
                # the link for 80 to 90 ms, which is the whole of the device's
                # DMA buffer, so a frame written in one uninterrupted run starves
                # the sound for exactly as long as it takes -- and the device's
                # drawing clock IS its sound clock (`estimated_pts()` is
                # `submitted_samples / 16000`), so starving the sound stalls the
                # clock the picture is scheduled against.
                #
                # Measured with the sound fed only between frames: the device
                # received 190 kB/s and rendered 1.1 to 2.5 complete frames a
                # second, with `AUDIO_EMPTY` every few seconds -- and 1.4-2.5 fps
                # is the exact figure the firmware's own source records for a
                # starved clock at main/av_player.c:2294. Reading that as "the
                # device cannot draw faster" would have been wrong by an order of
                # magnitude, and it is the same mistake this project has made
                # before.
                if args.audio:
                    send_audio(time.monotonic())
                # FATAL, not a skipped frame: `send_packet` may raise after
                # writing part of the packet, and there is then no way to
                # resume on a packet boundary.
                send_packet(connection, Packet(
                    Kind.JPEG, session, seq, pts, payload,
                    0x01 if n else 0), deadline=frame_start + args.write_timeout)
                seq += 1
                sent += len(payload)
            step_frames += 1
            step_bytes += sent
            next_frame_at += 1.0 / fps
            if next_frame_at < time.monotonic():
                # This step asked for more than the link gives. Run late by
                # resetting the next slot rather than accumulating a debt the
                # step can never pay, which would send every remaining frame
                # back to back at the end.
                next_frame_at = time.monotonic() + 1.0 / fps

        slack = tick - (time.monotonic() - now)
        if slack > 0:
            time.sleep(slack)

    if step_begin < time.monotonic():
        span = time.monotonic() - step_begin
        print(f"  STEP fps={args.steps[min(step_index,len(args.steps)-1)]:3d}  "
              f"frames={step_frames:5d} video_kBps={step_bytes/span/1024:7.1f} "
              f"audio_kBps={audio_bytes/span/1024:6.1f} "
              f"total_kBps={(step_bytes+audio_bytes)/span/1024:7.1f}",
              flush=True)


def frames_config() -> dict:
    return {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": 4,
            "sample_rate": 16000, "channels": 1, "sample_bits": 16,
            "audio_chunk_ms": AUDIO_CHUNK_MS, "video_max_bytes": frames.VIDEO_MAX,
            "stripe_rows": frames.STRIPE_ROWS, "start_delay_ms": 200,
            "audio_lead_ms": 200, "video_lead_ms": 200}


if __name__ == "__main__":
    raise SystemExit(main())
