"""Relay the container's port 8096 to a development machine, byte for byte.

The device is configured with one server address and changing it means writing
to the device's own storage, which is the one operation on this project that has
already gone wrong once. Relaying instead leaves the device untouched while the
server runs somewhere more convenient: the device connects here, and everything
it sends is forwarded to the real server.

Back-pressure is preserved in both directions by connecting the two sockets with
select and a bounded buffer rather than by reading one whole message and writing
it -- the device's flow control depends on the server being unable to write when
the device's window is full, and a relay that buffered freely would remove
exactly the signal the pacing is built on.

    python3 tools/relay_to_mac.py --listen 192.168.0.114:8096 --target 192.168.0.125:8096
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import time


def pump(source: socket.socket, sink: socket.socket, buffer_size: int) -> bool:
    """Move whatever is available from source to sink. False when the source ended."""
    data = source.recv(buffer_size)
    if not data:
        return False
    sink.sendall(data)
    return True


def relay_once(listener: socket.socket, target: tuple[str, int], buffer_size: int) -> None:
    device, peer = listener.accept()
    print(f"device {peer[0]} connected", flush=True)
    server = socket.socket()
    server.connect(target)
    device.setblocking(False)
    server.setblocking(False)
    began = time.monotonic()
    moved = [0, 0]
    try:
        while True:
            readable, _, _ = select.select([device, server], [], [], 30)
            if not readable:
                print("  idle for 30 s; closing", flush=True)
                break
            for source in readable:
                sink = server if source is device else device
                try:
                    if not pump(source, sink, buffer_size):
                        return
                except (BlockingIOError, InterruptedError):
                    continue
                moved[0 if source is device else 1] += 1
    finally:
        device.close()
        server.close()
        print(f"  closed after {time.monotonic()-began:.1f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="192.168.0.114:8096")
    parser.add_argument("--target", default="192.168.0.125:8096")
    parser.add_argument("--buffer", type=int, default=4096)
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    target_host, target_port = args.target.rsplit(":", 1)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, int(port)))
    listener.listen(4)
    print(f"relaying {args.listen} -> {args.target}", flush=True)
    while True:
        try:
            relay_once(listener, (target_host, int(target_port)), args.buffer)
        except Exception as error:
            print(f"  relay error: {type(error).__name__}: {error}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
