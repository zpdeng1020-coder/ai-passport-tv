#!/usr/bin/env python3
"""Read lines on stdin and write them with local time to LOGDIR/<name>-YYYYMMDD.log,
one file per day. LOGDIR is chosen as in tools/serial_log.py.

    python -u -m server.tv_server live --channel <id> --bind <addr> 2>&1 | python tools/log_stamp.py server [--dir LOGDIR]

Search the server log for: STATE_CHANGE, Traceback, ProtocolError, LiveError,
channel_audio_empty, "source=starved".
"""
import datetime as dt
import os
import sys


def log_dir(argv: list[str]) -> str:
    if "--dir" in argv:
        path = argv[argv.index("--dir") + 1]
    else:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.environ.get("AV_LOG_DIR") or os.path.join(os.path.dirname(repo), "ai-passport-logs")
    os.makedirs(path, exist_ok=True)
    return path


def main() -> int:
    name = sys.argv[1]
    directory = log_dir(sys.argv)
    f, day = None, None
    for raw in sys.stdin.buffer:
        now = dt.datetime.now()
        if day != now.date():
            if f:
                f.close()
            day = now.date()
            f = open(os.path.join(directory, f"{name}-{day:%Y%m%d}.log"), "a", encoding="utf-8", errors="replace")
        f.write(f"{now:%Y-%m-%d %H:%M:%S.%f}"[:-3] + " " + raw.decode("utf-8", "replace").rstrip() + "\n")
        f.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
