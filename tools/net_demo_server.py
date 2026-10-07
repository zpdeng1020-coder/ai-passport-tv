#!/usr/bin/env python3
"""Server for the minimal network benchmark (main/net_demo.c).

Deliberately does not use server/ code: no protocol.py, no pacing, no rate
control. Two modes, chosen per run:

    python tools/net_demo_server.py bulk              # step 2: link ceiling
    python tools/net_demo_server.py frames            # step 3: full path ceiling

bulk    sends zeros as fast as TCP accepts them. The device only counts bytes,
        so its KB/s readout is what the link can carry into the chip.
frames  sends the frames of main/demo_clip.bin back to back with no pacing.
        TCP back-pressure makes the server exactly as fast as the device, so
        the FPS on the device's screen is the ceiling of receive + inflate +
        expand + draw. Frames over 22528 bytes are skipped (reported).

The device dials in; this listens. Serves one connection at a time and, once
the device drops it, waits for the next.
"""
import argparse
import socket
import struct
import sys
import time
from pathlib import Path

CLIP = Path(__file__).resolve().parents[1] / "main" / "demo_clip.bin"   # --clip overrides
FRAME_MAX = 22528


def load_clip() -> list[bytes]:
    data = CLIP.read_bytes()
    assert data[:4] == b"DCL1", "not a DCL1 clip"
    count = struct.unpack_from("<H", data, 4)[0]
    lengths = struct.unpack_from(f"<{count}I", data, 8)
    at = 8 + 4 * count
    frames = []
    for n in lengths:
        frames.append(data[at:at + n])
        at += n
    kept = [f for f in frames if len(f) <= FRAME_MAX]
    print(f"clip: {count} frames, kept {len(kept)}, skipped {count - len(kept)} "
          f"over {FRAME_MAX} B; avg {sum(map(len, kept)) // len(kept)} B", flush=True)
    return kept


def serve(conn: socket.socket, mode: str, clip: list[bytes]) -> None:
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.sendall(b"NDM1" + bytes([1 if mode == "bulk" else 2]))
    start = time.monotonic()
    sent = frames = 0
    last = start
    if mode == "bulk":
        chunk = bytes(16384)
        while True:
            conn.sendall(chunk)
            sent += len(chunk)
            if time.monotonic() - last >= 2:
                print(f"  bulk: {sent / (time.monotonic() - start) / 1024:.0f} KB/s "
                      f"avg", flush=True)
                last = time.monotonic()
    else:
        i = 0
        while True:
            f = clip[i % len(clip)]
            conn.sendall(struct.pack("<I", len(f)) + f)
            sent += len(f) + 4
            frames += 1
            i += 1
            if time.monotonic() - last >= 2:
                span = time.monotonic() - start
                print(f"  frames: {frames / span:.1f} fps  {sent / span / 1024:.0f} KB/s "
                      f"(avg since connect)", flush=True)
                last = time.monotonic()


LEVELS_KBPS = [100, 200, 300, 400, 600, 800, 1200]
LEVEL_SECONDS = 8
DGRAM = 1400


def serve_udp(udp: socket.socket, peer: tuple) -> None:
    """Wait for the device's hello, then send datagrams at stepped rates."""
    time.sleep(1.0)   # let the device close TCP and bind its UDP port
    print(f"  udp sending to {peer[0]}:{peer[1]}", flush=True)
    seq = 0
    for level in LEVELS_KBPS:
        start = time.perf_counter()
        sent = 0
        head = struct.pack("<II", 0, level)
        while (t := time.perf_counter() - start) < LEVEL_SECONDS:
            if sent < level * 1024 * t:
                udp.sendto(struct.pack("<II", seq, level) + bytes(DGRAM - 8), peer)
                seq += 1
                sent += DGRAM
            else:
                time.sleep(0.0005)
        print(f"  udp offered {level} KB/s: sent {sent / LEVEL_SECONDS / 1024:.0f} KB/s "
              f"({seq} datagrams so far)", flush=True)


FPS_LEVELS = [8, 12, 16, 20, 24, 30, 40]
AUDIO_MARK = 0xFFFFFFF0
AUDIO_CHUNK = 1280          # 40 ms of 16 kHz s16 mono, the product's chunk


class AudioPump:
    """Sends one 1280-byte chunk every 40 ms on the same socket as the picture.

    poll() is called from inside the picture sender's busy-wait loops, so audio
    keeps its own 25/s cadence while a frame's datagrams are being paced out.
    """

    def __init__(self, udp, peer, pcm: bytes):
        self.udp, self.peer, self.pcm = udp, peer, pcm
        self.chunks = len(pcm) // AUDIO_CHUNK
        self.seq = 0
        self.next = time.perf_counter()
        self.max_late = 0.0

    def poll(self) -> None:
        now = time.perf_counter()
        while now >= self.next:
            self.max_late = max(self.max_late, now - self.next)
            at = (self.seq % self.chunks) * AUDIO_CHUNK
            hdr = struct.pack("<IHHII", AUDIO_MARK, 0, 1, self.seq, 25)
            self.udp.sendto(hdr + self.pcm[at:at + AUDIO_CHUNK], self.peer)
            self.seq += 1
            self.next += 0.040
            now = time.perf_counter()
SPREAD = 0.4   # fraction of a frame period over which one frame's datagrams are spread


def split_stripes(f: bytes) -> list[tuple[int, bytes]]:
    """(stripe index, compressed stripe) for every stripe the frame actually carries."""
    count = f[1]
    lens = struct.unpack_from(f">{count}H", f, 2)
    at = 2 + 2 * count
    out = []
    for s, n in enumerate(lens):
        if n:
            out.append((s, f[at:at + n]))
        at += n
    return out


def serve_udp_frames(udp: socket.socket, clip: list[bytes], peer: tuple, pcm: bytes = b"", slots: bool = False) -> None:
    time.sleep(1.0)   # let the device close TCP and bind its UDP port
    # Created after the sleep: its clock starts at construction, and one made before
    # it wakes a second behind and sends that second of audio in a single burst.
    audio = AudioPump(udp, peer, pcm) if pcm else None
    print(f"  udp sending to {peer[0]}:{peer[1]}", flush=True)
    chunk = DGRAM - 16
    n = 0
    seq = 0
    for fps in FPS_LEVELS:
        start = time.perf_counter()
        nxt = start
        frames = sent = 0
        max_late = max_send = 0.0
        late20 = 0
        while nxt - start < LEVEL_SECONDS:
            while time.perf_counter() < nxt:
                if audio: audio.poll()
            late = time.perf_counter() - nxt
            max_late = max(max_late, late)
            late20 += late > 0.02
            f = clip[n % len(clip)]
            if slots:
                parts = split_stripes(f)
                spread = SPREAD / fps / max(len(parts), 1)
                for s, z in parts:
                    frags = (len(z) + chunk - 1) // chunk
                    for fi in range(frags):
                        hdr = struct.pack("<IHHII", n, s, (fi << 8) | frags, len(z), seq)
                        seq += 1
                        if audio: audio.poll()
                        udp.sendto(hdr + z[fi * chunk:(fi + 1) * chunk], peer)
                    t = time.perf_counter() + spread
                    while time.perf_counter() < t:
                        if audio: audio.poll()
                sent += len(f)
                frames += 1
                n += 1
                nxt += 1.0 / fps
                continue
            count = (len(f) + chunk - 1) // chunk
            spread = SPREAD / fps / count
            for i in range(count):
                hdr = struct.pack("<IHHII", n, i, count, len(f), fps)
                if audio: audio.poll()
                t0 = time.perf_counter()
                udp.sendto(hdr + f[i * chunk:(i + 1) * chunk], peer)
                max_send = max(max_send, time.perf_counter() - t0)
                t = time.perf_counter() + spread
                while time.perf_counter() < t:
                    if audio: audio.poll()
            sent += len(f)
            frames += 1
            n += 1
            nxt += 1.0 / fps
        print(f"  udp offered {fps} fps: sent {frames / LEVEL_SECONDS:.1f} fps "
              f"{sent / LEVEL_SECONDS / 1024:.0f} KB/s  max_late={max_late*1000:.0f}ms "
              f"late>20ms={late20} max_sendto={max_send*1000:.1f}ms"
              + (f"  audio: {audio.seq} chunks sent, max_late={audio.max_late*1000:.0f}ms" if audio else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["bulk", "frames", "udp", "udpframes", "udpslots"])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--audio", default="", help="udpframes: raw s16le 16 kHz mono PCM sent alongside the picture, 1280 B per 40 ms")
    ap.add_argument("--ifindex", type=int, default=0, help="udpframes: force UDP out of this Windows interface index (IP_UNICAST_IF), to choose wired vs Wi-Fi sender")
    ap.add_argument("--clip", default="", help="frames to send instead of main/demo_clip.bin")
    ap.add_argument("--fps", type=int, default=0, help="udpframes: hold this rate instead of ramping")
    ap.add_argument("--spread", type=float, default=0.4, help="udpframes: fraction of the frame period one frame's datagrams are spread over")
    args = ap.parse_args()
    global SPREAD, CLIP
    SPREAD = args.spread
    if args.clip:
        CLIP = Path(args.clip)
    if args.fps:
        global FPS_LEVELS
        FPS_LEVELS = [args.fps] * 1000   # ~2 h without the device having to reconnect
    clip = load_clip() if args.mode in ("frames", "udpframes", "udpslots") else []
    pcm = Path(args.audio).read_bytes() if args.audio else b""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    udp = None
    if args.mode in ("udp", "udpframes", "udpslots"):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind((args.bind, args.port))
        if args.ifindex:
            udp.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(args.ifindex))   # IP_UNICAST_IF
    print(f"{args.mode}: listening on {args.bind}:{args.port}", flush=True)
    while True:
        conn, peer = srv.accept()
        print(f"device connected from {peer[0]}", flush=True)
        try:
            if args.mode in ("udp", "udpframes", "udpslots"):
                conn.sendall(b"NDM1" + bytes([{"udp": 3, "udpframes": 4, "udpslots": 5}[args.mode]]))
                conn.close()
                if args.mode == "udp":
                    serve_udp(udp, (peer[0], 8096))
                else:
                    serve_udp_frames(udp, clip, (peer[0], 8096), pcm, args.mode == "udpslots")
            else:
                serve(conn, args.mode, clip)
        except (OSError, socket.timeout) as e:
            print(f"  device dropped: {type(e).__name__}", flush=True)
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
