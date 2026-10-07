#!/usr/bin/env python3
"""Capture the board's console for N seconds into a file (and echo the lines that
carry measurements). Opens the port with DTR/RTS held low so it does not reset a
USB-Serial/JTAG device that is mid-run.

    python tools/serial_capture.py COM4 60 out.log [--grep FRAMES,JITTER]
"""
import sys
import time

import serial


def main() -> int:
    port, seconds, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    keep = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[4] == "--grep" else []
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.5
    s.dtr = False
    s.rts = False
    s.open()
    end = time.time() + seconds
    buf = b""
    with open(out, "w", encoding="utf-8", errors="replace") as f:
        while time.time() < end:
            buf += s.read(4096)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip()
                f.write(f"{time.time():.1f} {text}\n")
                f.flush()
                if keep and any(k in text for k in keep):
                    print(text, flush=True)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
