"""Serve a media file over HTTP at its own real-time bitrate, as a live source.

The media server only takes http(s) channel URLs and pulls them like a live
stream, so a recorded clip has to be presented that way. The file is sent from
the start on every request at about the rate it plays at, so ffmpeg on the other
end sees footage arriving in real time rather than a file it can read in a
second. Range requests are ignored: a live stream has no seeking.

    python tools/paced_source.py clip.ts --port 9100
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def duration_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("clip", type=Path)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--speed", type=float, default=1.02,
                        help="send this much faster than real time, so the "
                             "consumer's queue is never starved")
    args = parser.parse_args()

    size = args.clip.stat().st_size
    rate = size / duration_s(args.clip) * args.speed

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            start = time.monotonic()
            sent = 0
            with open(args.clip, "rb") as handle:
                while True:
                    block = handle.read(16384)
                    if not block:
                        return
                    try:
                        self.wfile.write(block)
                    except OSError:
                        return
                    sent += len(block)
                    ahead = sent / rate - (time.monotonic() - start)
                    if ahead > 0.01:
                        time.sleep(ahead)

    print(f"serving {args.clip.name}: {size} bytes at {rate / 1024:.0f} kB/s "
          f"on 127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
