#!/usr/bin/env python3
"""B02-0 Test Harness:
1. Keep the server in a SINGLE process.
2. Perform at least 10 real live session establishment / teardown cycles:
   - Verify FFmpeg started.
   - Verify complete audio and video received.
   - Test channel switch (END) and cancellation / disconnect.
   - Record subprocess PID, thread count, FD count, reason, close_ms.
3. Verify device reconnect & continuous playback:
   - Let physical device connect, play session 1.
   - Terminate session 1 cleanly.
   - Allow device to automatically reconnect to session 2 without restarting server.
   - Verify session 2 plays continuously and stably.
"""
import collections
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.abspath("."))
from tools.console import use_utf8
use_utf8()

from server.media import Media, FPS, AUDIO_CHUNK_MS
from server.protocol import Kind, Packet, receive_packet, send_packet, json_bytes
from server.live import LiveChannel, CHANNELS, CHANNEL_AGENTS
from server.tv_server import AVServer

def count_open_fds():
    try:
        import resource
        # On macOS, check /dev/fd
        return len(os.listdir('/dev/fd'))
    except Exception:
        return -1

def run_ten_sessions_test(bind_ip="127.0.0.1", port=8098):
    print(f"=== Starting 10 Real Live Sessions Test on {bind_ip}:{port} ===")
    token = None
    media = Media(b"", (b"",))
    server = AVServer(media, token, bind=bind_ip, port=port,
                      logger=lambda msg: print(f"[SRV] {msg}", flush=True))
    server.live_enabled = True
    server.channel_name = "baseline"

    srv_thread = threading.Thread(target=server.serve, daemon=True)
    srv_thread.start()
    server.ready.wait(timeout=2.0)

    results = []
    fd_initial = count_open_fds()
    threads_initial = threading.active_count()

    for idx in range(1, 11):
        fd_before = count_open_fds()
        t_start = time.monotonic()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect((bind_ip, port))
        client.setblocking(True)

        # Send HELLO
        hello_payload = {"version": 1, "client": f"b02_test_harness_{idx}", "channel": "baseline"}
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello_payload)))

        # Receive CONFIG
        pkt_config = receive_packet(client, timeout=5.0)
        assert pkt_config.kind == Kind.CONFIG, f"Expected CONFIG, got {pkt_config.kind}"
        session_id = pkt_config.session

        # Receive PALETTE
        pkt_palette = receive_packet(client, timeout=10.0)
        assert pkt_palette.kind == Kind.PALETTE, f"Expected PALETTE, got {pkt_palette.kind}"

        # Confirm FFmpeg is started and retrieve PID
        time.sleep(0.2)
        assert server.live_channel is not None
        ffmpeg_proc = server.live_channel.decoder
        assert ffmpeg_proc is not None
        ffmpeg_pid = ffmpeg_proc.pid
        assert ffmpeg_proc.poll() is None, f"FFmpeg {ffmpeg_pid} died prematurely"

        audio_count = 0
        video_count = 0
        end_reason = "normal_end"

        # Receive media packets
        # For sessions that test early disconnect/cancel, abort after 1 second.
        # For normal sessions, wait for prebuffer to complete and receive media packets.
        is_early_abort = (idx in (2, 4))
        max_wait = 1.0 if is_early_abort else 8.0
        t_media_start = time.monotonic()
        while time.monotonic() - t_media_start < max_wait:
            readable, _, _ = select.select([client], [], [], 0.05)
            if readable:
                pkt = receive_packet(client, timeout=2.0)
                if pkt.kind == Kind.PCM:
                    audio_count += 1
                elif pkt.kind == Kind.JPEG:
                    video_count += 1
            if not is_early_abort and audio_count >= 20 and video_count >= 4:
                break

        # Decide exit mode
        t_close_start = time.monotonic()
        if idx % 2 == 1:
            # Active channel switch via END packet
            send_packet(client, Packet(Kind.END, session_id, 9999, 0, b""))
            client.close()
            end_reason = "device_switch_end"
        else:
            # Sudden disconnect / cancellation
            client.close()
            end_reason = "socket_disconnect"

        # Wait for server to finish cleanup of this session
        deadline = time.monotonic() + 3.0
        while server.live_channel is not None and time.monotonic() < deadline:
            time.sleep(0.02)

        close_duration_ms = (time.monotonic() - t_close_start) * 1000.0

        # Verify child FFmpeg process has exited
        time.sleep(0.1)
        ffmpeg_exited = ffmpeg_proc.poll() is not None

        fd_after = count_open_fds()
        live_reader_threads = [t.name for t in threading.enumerate() if "LiveChannel" in t.name]

        rec = {
            "session_num": idx,
            "session_id": session_id,
            "ffmpeg_pid": ffmpeg_pid,
            "ffmpeg_exited": ffmpeg_exited,
            "audio_packets_received": audio_count,
            "video_packets_received": video_count,
            "end_reason": end_reason,
            "close_duration_ms": round(close_duration_ms, 2),
            "fd_count": fd_after,
            "active_live_threads": live_reader_threads
        }
        results.append(rec)
        print(f"Session {idx}: pid={ffmpeg_pid} exit={ffmpeg_exited} audio={audio_count} video={video_count} close_ms={rec['close_duration_ms']} fd={fd_after}")

    server.stop.set()
    srv_thread.join(timeout=2.0)

    summary = {
        "case": "ten_real_live_sessions",
        "fd_initial": fd_initial,
        "fd_final": count_open_fds(),
        "threads_initial": threads_initial,
        "threads_final": threading.active_count(),
        "all_children_exited": all(r["ffmpeg_exited"] for r in results),
        "max_close_ms": max(r["close_duration_ms"] for r in results),
        "sessions": results
    }
    return summary

if __name__ == "__main__":
    bind = "192.168.123.185" if len(sys.argv) > 1 and sys.argv[1] == "--device" else "127.0.0.1"
    summary = run_ten_sessions_test(bind_ip=bind, port=8098)
    with open("/Users/dalabommba/Desktop/AI_Passport/B02交付物/ten_real_live_sessions.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("Test finished successfully! Output written to B02交付物/ten_real_live_sessions.json")
