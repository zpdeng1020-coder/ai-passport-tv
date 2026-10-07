#!/usr/bin/env python3
"""Log the board's console without a time limit: one file per day, local time on
every line, and the port is reopened if it disappears (a reset can re-enumerate
USB serial). Opens with DTR/RTS low so attaching does not reset the board.

    python tools/serial_log.py COM3 [--dir LOGDIR]

Files are LOGDIR/device-YYYYMMDD.log. LOGDIR defaults to $AV_LOG_DIR, then to
../ai-passport-logs beside the repository, so logs stay out of the working tree.
Pair it with tools/log_stamp.py for the server's output. Search the result for:
RX_EXIT, AUDIO_EMPTY, AUDIO_STARVED, "Session reset", "bounded PCM queue full",
"nobuf=[1-9]", "### port error".
"""
import datetime as dt
import os
import sys
import time

import serial


def log_dir(argv: list[str]) -> str:
    if "--dir" in argv:
        path = argv[argv.index("--dir") + 1]
    else:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.environ.get("AV_LOG_DIR") or os.path.join(os.path.dirname(repo), "ai-passport-logs")
    os.makedirs(path, exist_ok=True)
    return path


def main() -> int:
    port = sys.argv[1]
    directory = log_dir(sys.argv)
    f, day = None, None
    buf = b""
    ser = None

    def write(text: str) -> None:
        nonlocal f, day
        now = dt.datetime.now()
        if day != now.date():
            if f:
                f.close()
            day = now.date()
            f = open(os.path.join(directory, f"device-{day:%Y%m%d}.log"), "a", encoding="utf-8", errors="replace")
        f.write(f"{now:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" {text}\n")
        f.flush()

    write("### serial_log started port=" + port)
    while True:
        try:
            if ser is None:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, 115200, 0.5
                ser.dtr = False
                ser.rts = False
                ser.open()
                write("### port opened")
            buf += ser.read(4096)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                write(line.decode("utf-8", "replace").rstrip())
        except (serial.SerialException, OSError) as e:
            write(f"### port error: {type(e).__name__}: {e}")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
