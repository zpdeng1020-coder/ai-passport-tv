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

    python tools/usb_probe.py --port COM4 --seconds 90 --ramp 8,12,16,20,24,30

The device must be built with tools/sdkconfig.usb-transport as a third
SDKCONFIG_DEFAULTS entry, and only 0x10000 may be flashed. Pass --port on
Windows: discovery in usb_link only knows the macOS and Linux device names.

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

from server import frames, protocol, usb_link             # noqa: E402
from server.protocol import Kind, Packet, json_bytes      # noqa: E402

# One audio packet as the firmware now receives it: 40 ms of 16 kHz mono as IMA
# ADPCM, a 4-byte state header and 320 bytes of nibbles (AV_AUDIO_BYTES in
# main/av_protocol.h). server/protocol.py still describes the old 1280-byte PCM
# packet and is no longer maintained here, so the probe overrides the length for
# its own encode() calls rather than editing the server's copy.
AUDIO_CHUNK_MS = 40
AUDIO_BYTES = 324
protocol.AUDIO_BYTES = AUDIO_BYTES
# Zero header and zero nibbles are a valid ADPCM block and decode to silence.
SILENT_AUDIO = bytes(AUDIO_BYTES)

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
        "audio_codec": "ima_adpcm", "audio_chunk_ms": AUDIO_CHUNK_MS,
        "video_max_bytes": frames.VIDEO_MAX,
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
                        help="comma-separated fps steps held for an equal share "
                             "of --seconds each, e.g. 8,12,16,20,24")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--stripes-per-packet", type=int, default=7,
                        help="7 keeps a packet inside the device's ceiling, the "
                             "same packing the network path uses, so the only "
                             "variable this measures is the transport")
    parser.add_argument("--run", type=int, default=8)
    parser.add_argument("--pace", type=float, default=2.0,
                        help="cap the write rate at this multiple of what the "
                             "stream needs (0 = unlimited). The device's USB "
                             "driver has no back-pressure: it drops bytes once "
                             "its 16 KB ring is full, which desynchronises the "
                             "stream, so a burst larger than the ring is a "
                             "failure of the probe, not a measurement")
    parser.add_argument("--lead-ms", type=int, default=400,
                        help="audio sent ahead of real time, in one burst at the "
                             "start. The device holds the picture until its audio "
                             "clock exists, which takes five chunks, and while it "
                             "holds the picture it reads nothing: with no "
                             "back-pressure on this link that is how the ring "
                             "overflows")
    parser.add_argument("--video-delay", type=float, default=0.6,
                        help="seconds after the audio burst before the first frame")
    parser.add_argument("--clock-offset", type=float, default=0.3,
                        help="seconds between the audio burst and the device's "
                             "clock starting; frame timestamps are taken against "
                             "that clock")
    parser.add_argument("--settle", type=float, default=0.3,
                        help="seconds to wait after CONFIG before sending media")
    args = parser.parse_args()
    if args.ramp:
        # CONFIG announces the first step's rate, so the "needs" line below and
        # the device's own view of the stream agree with the first row.
        args.fps = int(args.ramp.split(",")[0])

    port = args.port or usb_link.wait_for_port(timeout=10)
    if not port:
        print("no USB serial port found; is the device plugged in?")
        return 1
    print(f"port {port}")

    connection = usb_link.SerialConnection(port)
    stream = DeviceStream(connection)
    saw: list[dict] = []
    flags = {"reset": False}

    def note(line: str) -> None:
        found = INTERVAL.search(line)
        if "Session reset" in line:
            flags["reset"] = True
        if not found and line[:2] in ("W ", "E "):
            print(f"  dev| {line}")
        if found:
            frames_done, ms, dropped, decode, in_bps = (int(x) for x in found.groups())
            saw.append({"frames": frames_done, "ms": ms, "dropped": dropped,
                        "decode": decode, "in_bps": in_bps})

    stream.on_log = note
    session = 0x5EED_0000

    def handshake() -> bool:
        nonlocal session
        session += 1
        # The device announces itself with HELLO every few seconds while it
        # waits for a server, and gives CONFIG only about two seconds after
        # that, so the wait is in short slices and CONFIG goes out the moment
        # HELLO is seen.
        hello_by = time.monotonic() + 15.0
        nudge_at = time.monotonic() + 3.0
        seen_hello = False
        while not seen_hello and time.monotonic() < hello_by:
            seen_hello = any(kind == Kind.HELLO for kind, _ in stream.pump(0.05))
            if not seen_hello and nudge_at and time.monotonic() > nudge_at:
                # A session left over from an earlier run is still waiting for
                # a header, for up to 30 s after its CONFIG, and says nothing
                # while it does. A header that is not one ends it at once.
                print("  no HELLO yet; ending the device's previous session")
                connection.send(bytes(24))
                nudge_at = 0.0
        if not seen_hello:
            print("no HELLO from the device in 15 s; is it running a build with "
                  "CONFIG_AV_USB_TRANSPORT?")
            return False
        connection.send(Packet(Kind.CONFIG, session, 0, 0,
                               config_payload(session, args.fps)).encode())
        connection.send(Packet(Kind.PALETTE, session, 1, 0,
                               bytes(frames.PALETTE_BYTES)).encode())
        stream.pump(args.settle)
        flags["reset"] = False
        return True

    pace_clock = [0.0]

    def send(data: bytes, rate: float) -> None:
        if not rate:
            connection.send(data)
            return
        for at in range(0, len(data), 1024):
            piece = data[at:at + 1024]
            if rate:
                now = time.monotonic()
                if pace_clock[0] < now:
                    pace_clock[0] = now          # never catch up with a burst
                # Sleep only once the schedule is 20 ms ahead. Windows sleeps
                # in steps of about 15 ms, so sleeping for every kilobyte held
                # this to a third of the rate it was asked for. 20 ms of data
                # at the highest rate is well inside the device's 16 KB ring.
                if pace_clock[0] - now > 0.020:
                    time.sleep(pace_clock[0] - now - 0.010)
                pace_clock[0] += len(piece) / rate
            connection.send(piece)

    try:
        print("waiting for the device's HELLO ...")
        if not handshake():
            return 1

        # Built up front and reused: synthesising and compressing a frame in
        # Python takes longer than a frame's slot at 12 fps, so building them in
        # the send loop capped the probe at about 7 fps and measured the host.
        pool = [frame_payloads(args.stripes_per_packet, i, args.run)
                for i in range(12)]
        sample = pool[0]
        frame_bytes = sum(len(p) + 24 for p in sample)
        audio_rate = (24 + AUDIO_BYTES) * 1000 / AUDIO_CHUNK_MS
        print(f"sending {args.fps} fps, {len(sample)} packets/frame, "
              f"{frame_bytes} B/frame, needs "
              f"{(frame_bytes * args.fps + audio_rate) / 1024:.0f} kB/s")

        steps = ([int(v) for v in args.ramp.split(",") if v] if args.ramp
                 else [args.fps])
        step_seconds = max(4.0, args.seconds / len(steps))
        print(f"ramp {steps} fps, {step_seconds:.0f}s each, pace x{args.pace}\n")
        print(f"{'ask':>4}{'sent':>8}{'fps':>7}   device frames/interval, dropped, "
              f"decode peak, resets")

        seq, fill = 2, 0
        start = time.monotonic()
        origin = start
        audio_at = start - args.lead_ms / 1000
        audio_pts = 0
        next_frame = start + args.video_delay
        sent_frames = 0
        step_index = 0
        step_begin = start
        step_frames = 0
        step_resets = 0
        total_resets = 0
        fps = steps[0]

        while time.monotonic() - start < args.seconds:
            now = time.monotonic()
            rate = (args.pace * (frame_bytes * fps + audio_rate)
                    if args.pace else 0.0)

            if flags["reset"]:
                total_resets += 1
                step_resets += 1
                print(f"  device reset the session ({total_resets}); reconnecting")
                if not handshake():
                    return 1
                seq, audio_pts = 2, 0
                origin = time.monotonic()
                audio_at = origin - args.lead_ms / 1000
                next_frame = origin + args.video_delay
                continue

            # Sound first and in full: the device's clock is its audio, and a
            # probe that underruns the queue measures a device that has stopped
            # rather than one that cannot keep up.
            while now >= audio_at:
                try:
                    send(Packet(Kind.PCM, session, seq, audio_pts,
                                SILENT_AUDIO).encode(), rate)
                except Exception as error:
                    print(f"  audio write failed: {type(error).__name__}")
                    return 1
                seq += 1
                audio_pts += AUDIO_CHUNK_MS
                audio_at += AUDIO_CHUNK_MS / 1000

            if now - step_begin >= step_seconds and step_index + 1 < len(steps):
                _report(steps[step_index], step_frames, now - step_begin, saw,
                        step_resets)
                saw.clear()
                step_index += 1
                fps = steps[step_index]
                step_begin = now
                step_frames = 0
                step_resets = 0

            if now >= next_frame:
                payloads = pool[fill % len(pool)]
                fill += 1
                # Against the device's clock, not the host's: it runs behind by
                # about the burst's start-up time. A frame that arrives early is
                # drawn at once; one more than 100 ms late is dropped, so the
                # stamp errs early by 120 ms.
                pts = max(0, int((now - origin - args.clock_offset) * 1000) + 120)
                for n, payload in enumerate(payloads):
                    try:
                        send(Packet(Kind.JPEG, session, seq, pts, payload,
                                    0x01 if n else 0).encode(), rate)
                    except Exception as error:
                        print(f"  video write failed: {type(error).__name__}")
                        return 1
                    seq += 1
                sent_frames += 1
                step_frames += 1
                next_frame += 1.0 / fps
                if next_frame < time.monotonic() - 0.5:
                    next_frame = time.monotonic()    # far behind: do not burst

            stream.pump(0.01)
    finally:
        # Tell the device the session is over, so the next run finds it saying
        # HELLO rather than waiting out the previous one.
        try:
            connection.send(Packet(Kind.END, session, 0, 0).encode())
        except Exception:
            pass
        connection.close()

    _report(steps[step_index], step_frames,
            time.monotonic() - step_begin, saw, step_resets)
    print(f"\nsent {sent_frames} frames in {args.seconds:.0f}s "
          f"= {sent_frames / args.seconds:.1f} fps, "
          f"{total_resets} session resets")
    return 0


def _report(asked: int, sent: int, span: float, saw: list[dict],
            resets: int = 0) -> None:
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
        print(f"{asked:>4}{sent:>8}{rate:>7.1f}   no device data   resets {resets}")
        return
    # Skip the first interval of each step: it straddles the change.
    settled = saw[1:] if len(saw) > 1 else saw
    frames10 = sum(s["frames"] for s in settled) / len(settled)
    dropped = sum(s["dropped"] for s in settled) / len(settled)
    decode = max(s["decode"] for s in settled)
    print(f"{asked:>4}{sent:>8}{rate:>7.1f}   {frames10:>6.0f} ({asked*10:>3} wanted)"
          f"  {dropped:>5.0f}  {decode:>4} ms  {resets:>3}")


if __name__ == "__main__":
    raise SystemExit(main())
