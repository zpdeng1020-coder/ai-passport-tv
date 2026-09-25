"""Measure how well the device plays at each frame rate, one rate at a time.

Every measurement in this project so far has mixed three questions -- can the
server send it, can the link carry it, can the device draw it -- because the
only instrument was a log line read by hand while something else was being
changed. This runs one rate at a time, reads the device's own counters over a
fixed window, and prints one line per rate so the numbers can be compared.

The device is the judge: `interval_frames` is frames it completed, `rx_audio`
is sound chunks it read, `dropped` is frames it threw away, and `in_bps` is
bytes it took in. A rate is good when frames approach the target, audio sits
near 50 a second, and drops stay near zero.

    python3 tools/measure_rates.py --rates 2,3,4 --seconds 45
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[1]
INTERVAL = re.compile(
    r"interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+)"
    r".*?in_bps=(\d+)\s+rx_pkts=(\d+)\s+rx_bps=\d+\s+rx_audio=(\d+)")


def start_server(rate: int, channel: str, port: int, log: Path) -> subprocess.Popen:
    """Run the live server at one frame rate, on this machine.

    The rate reaches ffmpeg's filter graph through FPS in server/media.py, which
    is a constant rather than an argument -- so it is set in the environment
    instead of edited, and read back by the server through an override the
    server already understands. See the note in that module.
    """
    environment = dict(os.environ, TV_DATA_DIR=str(ROOT), TV_FPS=str(rate))
    handle = log.open("wb")
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "server.tv_server", "live",
         "--bind", "192.168.0.125", "--port", str(port), "--channel", channel],
        cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT)


def read_device(port: str, seconds: float) -> list[tuple[int, ...]]:
    device = serial.Serial(port, 115200, timeout=0.3)
    device.dtr = False
    device.rts = False
    rows: list[tuple[int, ...]] = []
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            line = device.readline().decode("utf-8", "replace")
            found = INTERVAL.search(line)
            if found:
                rows.append(tuple(int(value) for value in found.groups()))
    finally:
        device.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", default="2,3,4")
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--channel", default="ch013")
    parser.add_argument("--serial", default="/dev/cu.usbmodem101")
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args()

    print(f"{'fps':>4} {'frames/10s':>11} {'wanted':>7} {'audio/10s':>10} "
          f"{'dropped':>8} {'in_bps':>8} {'rx_pkts':>8} {'verdict':>10}")
    for rate in (int(value) for value in args.rates.split(",")):
        log = Path(f"/tmp/measure-{rate}fps.log")
        server = start_server(rate, args.channel, args.port, log)
        try:
            # Let the palette be sampled and the reserve fill before measuring;
            # those seconds are startup, not playback.
            time.sleep(14)
            rows = read_device(args.serial, args.seconds)
        finally:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            time.sleep(3)

        # Ignore the first interval: it spans the transition into playback.
        settled = rows[1:] if len(rows) > 1 else rows
        if not settled:
            print(f"{rate:>4} {'--':>11} {'--':>7} {'--':>10} {'--':>8} "
                  f"{'--':>8} {'--':>8} {'no data':>10}")
            continue
        frames = sum(row[0] for row in settled) / len(settled) / (settled[0][1] / 1000)
        audio = sum(row[5] for row in settled) / len(settled) / (settled[0][1] / 1000)
        dropped = sum(row[2] for row in settled) / len(settled)
        in_bps = sum(row[3] for row in settled) / len(settled)
        packets = sum(row[4] for row in settled) / len(settled)
        # A rate counts as reached when the picture is most of the way there and
        # almost nothing is being thrown away -- both, because either alone can
        # be bought by sacrificing the other.
        verdict = "reached" if frames >= rate * 0.9 and dropped <= 1 else "short"
        print(f"{rate:>4} {frames * 10:>11.1f} {rate * 10:>7} {audio * 10:>10.0f} "
              f"{dropped:>8.1f} {in_bps:>8} {packets:>8.0f} {verdict:>10}")

    # Put the frame rate back: it is a constant in a tracked file, and leaving
    # it changed would make the next reader's measurements meaningless.
    print("\nnote: server/media.py still holds FPS = 2 if this was edited by hand;"
          " TV_FPS is only an override for this script")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
