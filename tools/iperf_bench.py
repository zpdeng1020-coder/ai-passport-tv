#!/usr/bin/env python3
"""Drive the ESP-IDF iperf example's console to measure this board's Wi-Fi.

Step 1 of the handoff's benchmark plan: the board's own link ceiling, with no
application code in the way. The example is console-driven (esp-qa/wifi-cmd and
iperf-cmd), so every step here is a line typed at its REPL rather than a
compile-time setting.

    python3 tools/iperf_bench.py --port COM3 --ssid Link --password ... --listening

The device runs `iperf -s`; this script then measures from the PC with iperf2
(`iperf -c <device> -i 3 -t 30`), because the handoff specifies iperf **2.x** and
the example's README says so too: "It's compatible with iperf version 2.x."
iperf3 speaks a different control protocol and will not interoperate.

Credentials are taken on the command line and never written to a file. The
example stores them in NVS, which is on the device and not in this repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover - environment problem, not logic
    print("pyserial is required: pip install pyserial", file=sys.stderr)
    raise SystemExit(2)

IP_RE = re.compile(r"got ip[:\s]+(\d+\.\d+\.\d+\.\d+)", re.I)
READY_RE = re.compile(r"iperf>")
CONNECTED_RE = re.compile(r"(connected|WIFI_CONNECTED)", re.I)

# The console prints its prompt after each command, so command/reply pairing is
# done by waiting for the prompt rather than by sleeping a fixed time. A fixed
# sleep is how a command gets typed into the middle of the previous reply.
PROMPT = b"iperf>"


def open_port(port: str, baud: int) -> "serial.Serial":
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    # Do not reset the board on attach: it is already running the example and
    # has been reset by the flash that put it there.
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def read_until(ser: "serial.Serial", needle: bytes, timeout: float) -> bytes:
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if needle in buf:
                return buf
    return buf


def send_command(ser: "serial.Serial", command: str, timeout: float = 15.0) -> bytes:
    ser.write((command + "\r\n").encode())
    ser.flush()
    return read_until(ser, PROMPT, timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--listening",
        action="store_true",
        help="leave the device running `iperf -s` and exit, so the PC can drive it",
    )
    args = parser.parse_args()

    ser = open_port(args.port, args.baud)
    try:
        # The board has been reset by the flash and is sitting at the prompt.
        # Say hello and drain whatever it printed on the way up.
        banner = read_until(ser, PROMPT, 8.0)
        print("--- device banner ---")
        print(banner.decode("utf-8", errors="replace")[-1200:])

        # `sta_connect` is the command this version of esp-qa/wifi-cmd exposes;
        # read out of the device's own `help` output rather than guessed. An
        # earlier attempt used `wifi connect`, which that component rejected
        # with "excess option <ssid>".
        reply = send_command(ser, f"sta_connect {args.ssid} {args.password}", 25.0)
        print("--- sta_connect ---")
        print(reply.decode("utf-8", errors="replace")[-800:])

        ip_match = IP_RE.search(reply.decode("utf-8", errors="replace"))
        if not ip_match:
            # Association can finish after the command returns; ask again.
            time.sleep(4)
            reply += send_command(ser, "sta_connect", 10.0)
            ip_match = IP_RE.search(reply.decode("utf-8", errors="replace"))
        if ip_match:
            print(f"IP={ip_match.group(1)}")
        else:
            print("no IP parsed; check the console output above", file=sys.stderr)

        if args.listening:
            started = send_command(ser, "iperf -s -i 3", 10.0)
            print("--- iperf server started on device ---")
            print(started.decode("utf-8", errors="replace")[-600:])
            print(f"\nDevice address for the PC: {ip_match.group(1) if ip_match else '<unknown>'}")
            print("Leave this process running; drive it with iperf2 from the PC.")
            # Hold the port open so the server keeps running and its output can
            # be read back as the PC drives it.
            try:
                while True:
                    chunk = ser.read(4096)
                    if chunk:
                        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
            except KeyboardInterrupt:
                pass
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
