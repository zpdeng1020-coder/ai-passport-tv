"""What each channel can actually carry, measured by letting the rate find out.

The controller already probes for the ceiling: it raises the rate while the link
accepts the bytes and lowers it when they stop being accepted, and it settles
where those two meet. That is the same answer a sweep would give, arrived at by
the code that will use it -- so this reads the controller's own decisions rather
than driving the rate from outside.

Three earlier attempts at this measured nothing, and each failed for a different
reason worth keeping, because every one of them looked like a dead source:

  * Restarting the server per rate. The restart drops the device's connection,
    and the device then spends the measurement reconnecting.
  * Changing the rate with SIGUSR1 in place. That killed the server outright.
  * Opening and closing the serial port per channel. **Opening the port resets
    the device.** The ESP32 board's auto-reset circuit is wired to the DTR and
    RTS lines, so a fresh open toggles them and reboots the chip; the boot
    banner in the capture is the evidence, and every "no data" run has one in
    it. The port is therefore opened once, before anything else, and held open
    for the whole run -- through every server restart and every channel.

The server IS restarted between channels, and that is deliberate: a channel file
naming one channel means whatever the device asks for, the answer is the channel
being measured. The device reconnects on its own within a couple of seconds.

    python3 tools/channel_capacity.py --channels ch000,ch013 --seconds 90
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

RATE = re.compile(r"RATE (\d+) fps: fps=(\d+) video=(\d+)kB worst_write=(\d+)ms \((.*)\)")
DEVICE_FIELDS = ("frames", "interval_ms", "dropped", "decode_max_ms", "in_bps",
                 "rx_pkts", "rx_audio", "nobuf", "stripes", "starts", "late",
                 "hdrgap", "rssi")
DEVICE = re.compile(r"CLOCK_ESTIMATED " + " ".join(
    rf"{n}=(-?\d+)" for n in
    ("interval_frames", "interval_ms", "dropped", "decode_max_ms", "in_bps",
     "rx_pkts", "rx_audio", "nobuf", "maxstripes", "starts", "late",
     "hdrgap_max", "rssi")))


def open_device(port: str) -> serial.Serial:
    """Open the serial port without resetting the chip, and keep it open.

    The board resets when DTR and RTS are toggled, and pyserial toggles them on
    open. Holding the port open for the whole run means the reset happens once,
    before any measurement, instead of once per channel in the middle of one.
    """
    device = serial.Serial()
    device.port = port
    device.baudrate = 115200
    device.timeout = 0.3
    # Set before opening: on macOS the state is applied as the port is opened,
    # so a value written afterwards has already been preceded by the toggle that
    # does the damage.
    device.dtr = False
    device.rts = False
    device.open()
    device.reset_input_buffer()
    # Opening it resets the chip -- see the module note -- so the first thing to
    # do is wait for it to come back. Twenty seconds is generous next to the
    # ~18 s measured from reset to the first interval log, and it is spent once
    # rather than once per channel.
    print("opened the serial port; the device resets on open, waiting for it",
          flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if "RAW LAN prototype" in device.readline().decode("utf-8", "replace"):
            print("  device is up", flush=True)
            break
    else:
        print("  no boot banner in 30 s; continuing anyway", flush=True)
    return device


def read_device(device: serial.Serial, seconds: float) -> list[dict]:
    rows: list[dict] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = DEVICE.search(device.readline().decode("utf-8", "replace"))
        if found:
            rows.append(dict(zip(DEVICE_FIELDS, (int(v) for v in found.groups()))))
    return rows


def serve_one(channel: str, url: str, port: int, seconds: float,
              startup: float, device: serial.Serial) -> dict:
    """Run one channel to a finish and report what the rate settled at."""
    channels_file = Path("/tmp/capacity-channel.txt")
    channels_file.write_text(f"{channel} | capacity | {url}\n")
    log = Path(f"/tmp/capacity-{channel}.log")
    environment = dict(os.environ, TV_ADAPTIVE="1",
                       TV_CHANNELS_FILE=str(channels_file))
    handle = log.open("wb")
    server = subprocess.Popen(
        [sys.executable, "-u", "-m", "server.tv_server", "live",
         "--bind", "192.168.0.125", "--port", str(port), "--channel", channel],
        cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT)
    try:
        time.sleep(startup)
        rows = read_device(device, seconds)
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        time.sleep(2)
    changes = [m for m in (RATE.search(line)
                           for line in log.read_text(errors="replace").splitlines())
               if m]
    return {"channel": channel, "url": url, "device": rows, "log": str(log),
            "settled": int(changes[-1].group(1)) if changes else None,
            "steps": [(int(m.group(1)), m.group(5)) for m in changes]}


def channel_urls(path: Path) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            table[parts[0]] = parts[2]
    return table


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels", default="ch000,ch013")
    parser.add_argument("--channels-file", default="channels.txt",
                        help="where the channel URLs are read from")
    parser.add_argument("--seconds", type=float, default=90,
                        help="measurement time per channel, after startup")
    parser.add_argument("--startup", type=float, default=28,
                        help="seconds for the reserve to fill before measuring")
    parser.add_argument("--serial", default="/dev/cu.usbmodem101")
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args()

    table = channel_urls(ROOT / args.channels_file)
    wanted = [c.strip() for c in args.channels.split(",")]
    missing = [c for c in wanted if c not in table]
    if missing:
        print(f"not in {args.channels_file}: {', '.join(missing)}")
        return 1

    # Opened first and never closed. See the module note: opening this port is
    # what was resetting the device, and a measurement cannot survive that.
    device = open_device(args.serial)
    try:
        results = []
        for channel in wanted:
            print(f"--- {channel}: {args.seconds:.0f}s, adaptive", flush=True)
            results.append(serve_one(channel, table[channel], args.port,
                                     args.seconds, args.startup, device))
    finally:
        device.close()

    print(f"\n{'channel':<9}{'settled':>8}{'video kB/s':>12}{'frames/10s':>11}"
          f"{'audio/10s':>10}{'nobuf':>7}{'late':>6}{'rssi':>6}  rate steps")
    for got in results:
        rows = got["device"][1:]
        if not rows:
            print(f"{got['channel']:<9}{'--':>8}   no device data"
                  f" (see {got['log']})")
            continue
        n = len(rows)
        mean = lambda key: sum(r[key] for r in rows) / n
        steps = " ".join(f"{fps}:{why}" for fps, why in got["steps"][:5])
        print(f"{got['channel']:<9}{got['settled'] or '--':>8}"
              f"{mean('in_bps') / 1000:>12.0f}{mean('frames'):>11.0f}"
              f"{mean('rx_audio'):>10.0f}{mean('nobuf'):>7.1f}"
              f"{mean('late'):>6.1f}{rows[-1]['rssi']:>6}  {steps}")
    print("\nThe settled rate is the ceiling the controller found: it raised the"
          " rate while bytes\nwere accepted and lowered it when they were not."
          " The kB/s column is what that\nchannel's picture costs on this link"
          " at that rate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
