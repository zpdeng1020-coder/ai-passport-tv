"""Carry an unmodified media server's session to the device over USB.

The server is run as it ships and listens on a local TCP port. This bridge plays
the part of the device towards it: it connects to that port, forwards what the
server sends down the USB serial port, and forwards the device's own packets
(HELLO, END) back up. Nothing about the encoder, the pacing or the protocol is
reimplemented here, so what the device receives is what it would receive over
Wi-Fi.

Why a bridge rather than handing the serial port to the server: the server
waits on its connection with select(), which on Windows only accepts sockets.

Back-pressure is kept honest. The bridge reads from the server's socket only as
fast as it can write to the cable, so a slow cable fills the server's send
buffer and the server sees "not writable" exactly as it would from a slow
network. --cap limits the cable rate for experiments; 0 means as fast as the
port accepts.

The device's USB driver has no flow control of its own: bytes that arrive while
its ring is full are lost. Anything the bridge writes in a burst larger than the
ring is therefore a risk the real server does not take on a socket, which is why
writes are cut to --chunk bytes.

    python tools/usb_bridge.py --port COM3 --seconds 120
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import threading
import time

import serial

MAGIC = b"FAV1"
HEADER = 24
KIND_HELLO, KIND_END = 1, 5

INTERVAL = re.compile(
    r"CLOCK_ESTIMATED interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+)"
    r".*?decode_max_ms=(\d+).*?in_bps=(\d+)")
WATCH = ("AUDIO_EMPTY", "Session reset", "RX_EXIT", "RX_IO", "rejected", "nobuf")


class Bridge:
    def __init__(self, args):
        self.args = args
        self.ser = serial.Serial()
        self.ser.port = args.port
        self.ser.baudrate = 921600
        self.ser.timeout = 0.0
        self.ser.write_timeout = 3.0
        # Held low before opening: the auto-reset circuit hangs off these lines.
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.stop = threading.Event()
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.sent_total = 0
        self.sent_window = 0
        self.sessions = 0
        self.intervals: list[dict] = []
        self.events: list[str] = []
        self.log = open(args.log, "w", encoding="utf-8", errors="replace") if args.log else None

    def say(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        if self.log:
            self.log.write(line + "\n")
            self.log.flush()

    def device_line(self, line: str) -> None:
        if self.log:
            self.log.write(f"{time.strftime('%H:%M:%S')} dev| {line}\n")
        found = INTERVAL.search(line)
        if found:
            frames, ms, dropped, decode, in_bps = (int(x) for x in found.groups())
            self.intervals.append({"frames": frames, "ms": ms, "dropped": dropped,
                                   "decode": decode, "in_bps": in_bps})
            self.say(f"device: {frames} frames in {ms} ms = {frames * 1000 / max(ms, 1):.1f} fps, "
                     f"dropped {dropped}, decode peak {decode} ms, in {in_bps / 1024:.0f} kB/s")
        elif any(word in line for word in WATCH):
            self.events.append(line)
            self.say(f"device: {line[:170]}")

    def connect_server(self) -> None:
        host, _, port = self.args.server.rpartition(":")
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            sock = socket.create_connection((host, int(port)), timeout=5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.settimeout(0.2)
            self.sock = sock
            self.sessions += 1

    # Device to server: the serial stream carries log text and FAV1 packets.
    def device_reader(self) -> None:
        buf = bytearray()
        while not self.stop.is_set():
            try:
                chunk = self.ser.read(4096)
            except (serial.SerialException, OSError):
                self.say("serial port lost")
                self.stop.set()
                return
            if not chunk:
                time.sleep(0.002)
                continue
            buf.extend(chunk)
            while True:
                at = buf.find(MAGIC)
                if at < 0:
                    if len(buf) > 3:
                        self.text(bytes(buf[:-3]))
                        del buf[:-3]
                    break
                if at:
                    self.text(bytes(buf[:at]))
                    del buf[:at]
                if len(buf) < HEADER:
                    break
                kind = buf[5]
                length = int.from_bytes(buf[20:24], "big")
                if buf[4] != 1 or kind not in (KIND_HELLO, KIND_END) or length > 4096:
                    self.text(bytes(buf[:1]))
                    del buf[:1]
                    continue
                if len(buf) < HEADER + length:
                    break
                packet = bytes(buf[:HEADER + length])
                del buf[:HEADER + length]
                self.forward_up(kind, packet)

    def text(self, raw: bytes) -> None:
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                self.device_line(line.strip())

    def forward_up(self, kind: int, packet: bytes) -> None:
        if kind == KIND_HELLO:
            self.say("device HELLO: opening a session with the server")
            try:
                self.connect_server()
            except OSError as error:
                self.say(f"cannot reach the server: {error}")
                return
        with self.lock:
            sock = self.sock
        if sock:
            try:
                sock.sendall(packet)
            except OSError:
                pass

    # Server to device.
    def server_reader(self) -> None:
        a = self.args
        next_at = time.monotonic()
        while not self.stop.is_set():
            with self.lock:
                sock = self.sock
            if not sock:
                time.sleep(0.01)
                continue
            try:
                data = sock.recv(a.chunk)
            except socket.timeout:
                continue
            except OSError:
                data = b""
            if not data:
                with self.lock:
                    if self.sock is sock:
                        self.sock = None
                self.say("server closed the session")
                continue
            if a.cap:
                now = time.monotonic()
                if next_at > now + 0.02:
                    time.sleep(next_at - now - 0.01)
                next_at = max(next_at, now) + len(data) / a.cap
            try:
                self.ser.write(data)
            except (serial.SerialException, OSError) as error:
                self.say(f"write to device failed: {type(error).__name__}")
                self.stop.set()
                return
            self.sent_total += len(data)
            self.sent_window += len(data)

    def run(self) -> None:
        threads = [threading.Thread(target=self.device_reader, daemon=True),
                   threading.Thread(target=self.server_reader, daemon=True)]
        for t in threads:
            t.start()
        # A session left over from an earlier run waits for a header and stays
        # silent; a header that is not one ends it at once.
        self.say("waiting for the device ... (sending a nudge in case an old session is open)")
        time.sleep(1.0)
        if not self.sessions:
            self.ser.write(bytes(24))
        started = time.monotonic()
        last = started
        try:
            while time.monotonic() - started < self.args.seconds and not self.stop.is_set():
                time.sleep(0.25)
                now = time.monotonic()
                if now - last >= 10:
                    self.say(f"bridge: {self.sent_window / (now - last) / 1024:.0f} kB/s down the cable "
                             f"({self.sessions} sessions so far)")
                    self.sent_window = 0
                    last = now
        finally:
            self.stop.set()
            try:
                self.ser.write(bytes(24))
            except Exception:
                pass
            self.summary(time.monotonic() - started)
            try:
                self.ser.close()
            except Exception:
                pass

    def summary(self, span: float) -> None:
        self.say("=" * 60)
        self.say(f"{span:.0f} s, {self.sessions} sessions, "
                 f"{self.sent_total / span / 1024:.0f} kB/s average down the cable")
        settled = self.intervals[1:] if len(self.intervals) > 1 else self.intervals
        if settled:
            fps = [i["frames"] * 1000 / max(i["ms"], 1) for i in settled]
            fps.sort()
            self.say(f"device fps over {len(settled)} intervals: min {fps[0]:.1f}, "
                     f"median {fps[len(fps) // 2]:.1f}, max {fps[-1]:.1f}; "
                     f"dropped {sum(i['dropped'] for i in settled)}; "
                     f"decode peak {max(i['decode'] for i in settled)} ms; "
                     f"in {sum(i['in_bps'] for i in settled) / len(settled) / 1024:.0f} kB/s mean")
        else:
            self.say("the device reported no frame intervals")
        resets = sum("Session reset" in e for e in self.events)
        empties = sum("AUDIO_EMPTY" in e for e in self.events)
        self.say(f"session resets {resets}, audio underruns {empties}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--server", default="127.0.0.1:8096")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--cap", type=float, default=0,
                        help="limit the cable to this many bytes a second (0 = none)")
    parser.add_argument("--chunk", type=int, default=2048,
                        help="largest single write to the device")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()
    Bridge(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
