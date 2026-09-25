"""Prove the USB transport works, before the server depends on it.

Sends the same FAV1 stream the live server sends, but down a cable instead of
over the network, and reports what the device did with it. The device needs no
changes to be tested this way -- it renders whatever arrives and logs what it
rendered -- so this is the smallest experiment that can settle whether the USB
path is real.

Why a separate tool rather than a flag on transport_probe.py: that one answers
"how fast can the link go", which needs a rate ramp and a fixed frame size. This
answers "does this transport carry the protocol at all", which is a yes or no
and does not need the ramp. Merging them would make both harder to read.

    python3 tools/usb_probe.py --seconds 30 --fps 20

Reads the device's own log from the same port to report what it saw. The log and
the device's control packets share that direction, so the reader has to tell them
apart; see scan_for_packets.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import frames, usb_link                       # noqa: E402
from server.protocol import Kind, Packet, json_bytes      # noqa: E402

# What the device writes back. Read from its own log so the number being judged
# is the device's, not a guess made from what was sent.
INTERVAL = re.compile(
    r"CLOCK_ESTIMATED interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+)"
    r".*?decode_max_ms=(\d+).*?in_bps=(\d+)")


def frame_payloads(stripes_per_packet: int, fill: int, run: int) -> list[bytes]:
    """One frame, built the way the server builds it.

    Content that compresses like live television rather than like noise: `run`
    is how often a byte value repeats inside a stripe, and 8 is about what a
    real channel measures. Noise would be a frame no channel ever sends and
    would measure a speed nothing can use.
    """
    compressed = []
    for index in range(frames.STRIPES):
        state = 0x1234_5678 ^ ((index + fill) * 2_654_435_761)
        out = bytearray()
        while len(out) < frames.STRIPE_PIXELS:
            state = (state * 1_103_515_245 + 12_345) & 0xFFFF_FFFF
            out += bytes(((state >> 16) & 0xFF,)) * run
        compressed.append(zlib.compress(bytes(out[:frames.STRIPE_PIXELS]), 1))
    payloads, at = [], 0
    while at < frames.STRIPES:
        chunk = compressed[at:at + stripes_per_packet]
        payloads.append(frames.packet(at, chunk))
        at += len(chunk)
    return payloads


def config_payload(session: int, fps: int) -> bytes:
    return json_bytes({
        "width": frames.WIDTH, "height": frames.HEIGHT, "fps": fps,
        "sample_rate": 16000, "channels": 1, "sample_bits": 16,
        "audio_chunk_ms": 20, "video_max_bytes": frames.VIDEO_MAX,
        "stripe_rows": frames.STRIPE_ROWS, "start_delay_ms": 200,
        "audio_lead_ms": 200, "video_lead_ms": 200, "session": session,
    })


class DeviceStream:
    """The device's side of the port, with its log filtered out.

    Both directions of a USB serial connection are byte streams, and on the way
    back the device writes two quite different things into one of them: its log,
    which is lines of text, and its control packets, which are FAV1 frames. A
    reader that wants the packets has to step over the text.

    It does that by looking for the magic, which is four bytes that a log line
    has no reason to contain, and then checking the header that follows. A false
    positive would need the text to spell FAV1 *and* the four bytes after it to
    decode as a plausible type, version and length -- and even then the length
    would have to match what actually follows. The check is cheap and the
    alternative, turning the log off, costs the only window into the device that
    exists when the screen is across the room.

    Log lines that are not packets are handed to `on_log`, so the caller can
    still read them.
    """

    def __init__(self, connection, on_log=None):
        self.connection = connection
        self.on_log = on_log
        self.buffer = bytearray()

    def pump(self, seconds: float) -> list[tuple[int, bytes]]:
        """Read for a while and return whatever packets turned up."""
        packets: list[tuple[int, bytes]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = self.connection.recv(4096)
            if not chunk:
                time.sleep(0.002)
                continue
            self.buffer.extend(chunk)
            packets.extend(self._extract())
        return packets

    def _extract(self) -> list[tuple[int, bytes]]:
        found: list[tuple[int, bytes]] = []
        buffer = self.buffer
        while True:
            at = buffer.find(b"FAV1")
            if at < 0:
                # Keep the tail: a magic split across two reads must survive to
                # the next one, and three bytes is the most of it that can be
                # missing.
                if len(buffer) > 3:
                    self._as_log(bytes(buffer[:-3]))
                    del buffer[:-3]
                break
            if at:
                self._as_log(bytes(buffer[:at]))
                del buffer[:at]
            if len(buffer) < 24:
                break                      # header not all here yet
            version, kind = buffer[4], buffer[5]
            length = int.from_bytes(buffer[20:24], "big")
            if version != 1 or kind not in range(1, 8) or length > 16384:
                # Not a header after all. Drop one byte and look again, so a
                # magic inside a log line cannot stall the stream for ever.
                self._as_log(bytes(buffer[:1]))
                del buffer[:1]
                continue
            if len(buffer) < 24 + length:
                break                      # payload not all here yet
            found.append((kind, bytes(buffer[24:24 + length])))
            del buffer[:24 + length]
        return found

    def _as_log(self, raw: bytes) -> None:
        if self.on_log and raw:
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.strip():
                    self.on_log(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None,
                        help="USB serial port (default: discover)")
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--ramp", default="",
                        help="comma-separated fps steps held for --step-seconds "
                             "each, e.g. 8,12,16,20,24")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--stripes-per-packet", type=int, default=7,
                        help="7 keeps a packet inside the device's ceiling, the "
                             "same packing the network path uses, so the only "
                             "variable this measures is the transport")
    parser.add_argument("--run", type=int, default=8)
    parser.add_argument("--audio", action="store_true", default=True)
    args = parser.parse_args()

    port = args.port or usb_link.wait_for_port(timeout=10)
    if not port:
        print("no USB serial port found; is the device plugged in?")
        return 1
    print(f"port {port}")

    connection = usb_link.SerialConnection(port)
    session = 0x5EED_0001
    stream = DeviceStream(connection, on_log=lambda line: print(f"  dev| {line}"))

    try:
        connection.send(Packet(Kind.CONFIG, session, 0, 0,
                               config_payload(session, args.fps)).encode())
        connection.send(Packet(Kind.PALETTE, session, 1, 0,
                               bytes(frames.PALETTE_BYTES)).encode())

        sample = frame_payloads(args.stripes_per_packet, 0, args.run)
        frame_bytes = sum(len(p) for p in sample)
        print(f"sending {args.fps} fps, {len(sample)} packets/frame, "
              f"{frame_bytes} B/frame, needs {frame_bytes * args.fps / 1024:.0f} kB/s")

        steps = ([int(v) for v in args.ramp.split(",") if v] if args.ramp
                 else [args.fps])
        step_seconds = max(4.0, args.seconds / len(steps))
        print(f"ramp {steps} fps, {step_seconds:.0f}s each\n")
        print(f"{'ask':>4}{'sent':>8}{'fps':>7}   device frames/10s, dropped, decode peak")

        seq, pts, fill = 2, 0, 0
        start = time.monotonic()
        audio_at = start
        audio_pts = 0
        next_frame = start
        sent_frames = 0
        step_index = 0
        step_begin = start
        step_frames = 0
        fps = steps[0]
        # One reader, reused: the device's log is a stream and a second reader
        # would see only what arrived after it opened.
        saw: list[dict] = []

        def note(line: str) -> None:
            found = INTERVAL.search(line)
            if found:
                frames_done, ms, dropped, decode, in_bps = (int(x) for x in found.groups())
                saw.append({"frames": frames_done, "ms": ms, "dropped": dropped,
                            "decode": decode, "in_bps": in_bps})

        stream.on_log = note

        while time.monotonic() - start < args.seconds:
            now = time.monotonic()
            # Sound first and in full: the device's clock is its audio, and a
            # probe that underruns the queue measures a device that has stopped
            # rather than one that cannot keep up.
            while args.audio and now >= audio_at:
                try:
                    connection.send(Packet(Kind.PCM, session, seq, audio_pts,
                                           bytes(640)).encode())
                except Exception as error:
                    print(f"  audio write failed: {type(error).__name__}")
                    return 1
                seq += 1
                audio_pts += 20
                audio_at += 0.020

            if now - step_begin >= step_seconds and step_index + 1 < len(steps):
                _report(steps[step_index], step_frames, now - step_begin, saw)
                saw.clear()
                step_index += 1
                fps = steps[step_index]
                step_begin = now
                step_frames = 0

            if now >= next_frame:
                payloads = frame_payloads(args.stripes_per_packet, fill, args.run)
                fill = (fill + 1) % 251
                pts = int((now - start) * 1000)
                for n, payload in enumerate(payloads):
                    flags = 0x01 if n else 0
                    try:
                        connection.send(Packet(Kind.JPEG, session, seq, pts,
                                               payload, flags).encode())
                    except Exception as error:
                        print(f"  video write failed: {type(error).__name__}")
                        return 1
                    seq += 1
                sent_frames += 1
                step_frames += 1
                next_frame += 1.0 / fps

            stream.pump(0.01)
    finally:
        connection.close()

    _report(steps[step_index], step_frames,
            time.monotonic() - step_begin, saw)
    print(f"\nsent {sent_frames} frames in {args.seconds:.0f}s "
          f"= {sent_frames / args.seconds:.1f} fps")
    return 0


def _report(asked: int, sent: int, span: float, saw: list[dict]) -> None:
    """One line per rate, from what the device said it did.

    The device is the judge, not the sender: `sent` says what was offered and
    the device's own counters say what arrived and what was drawn, and a rate
    that looks fine from the sending side while the device reports nothing is
    the failure this exists to catch.
    """
    if not span:
        return
    rate = sent / span
    if not saw:
        print(f"{asked:>4}{sent:>8}{rate:>7.1f}   no device data")
        return
    # Skip the first interval of each step: it straddles the change.
    settled = saw[1:] if len(saw) > 1 else saw
    frames10 = sum(s["frames"] for s in settled) / len(settled)
    dropped = sum(s["dropped"] for s in settled) / len(settled)
    decode = max(s["decode"] for s in settled)
    print(f"{asked:>4}{sent:>8}{rate:>7.1f}   {frames10:>6.0f} ({asked*10:>3} wanted)"
          f"  {dropped:>5.0f}  {decode:>4} ms")


if __name__ == "__main__":
    raise SystemExit(main())
