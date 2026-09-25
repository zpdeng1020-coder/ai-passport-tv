#!/usr/bin/env python3
"""30-Minute Continuous Stability Acceptance Test (B02-4):
- Paces baseline material in an infinite stream loop (TV_STREAM_LOOP=1).
- Runs for 30 minutes (1800s) against physical ESP32-C3 hardware.
- Tracks warm-up period, rolling 10s rendered FPS, heap memory, packet delivery, and resets.
- Asserts warm-up completed and post-warmup average rendered FPS >= 11 fps.
- Saves all raw logs and structured metrics to /Users/dalabommba/Desktop/AI_Passport/B02交付物.
"""
import argparse
import json
import os
import re
import serial
import signal
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath("."))
from tools.console import use_utf8
use_utf8()

os.environ["TV_STREAM_LOOP"] = "1"
os.environ.setdefault("TV_START_FPS", "12")
os.environ.setdefault("TV_SYNC_TOLERANCE_S", "0.15")
os.environ.setdefault("TV_ADAPTIVE", "0")
os.environ.setdefault("TV_PCM_QUEUE_CHUNKS", "400")
os.environ.setdefault("TV_FRAME_DEADLINE_S", "2.5")

from pathlib import Path
from server.media import Media, FPS, AUDIO_CHUNK_MS
from server.live import LiveChannel, CHANNELS, CHANNEL_AGENTS, load_channels
from server.tv_server import AVServer

DELIVERABLES_DIR = os.environ.get("TV_DELIVERABLES_DIR", "/Users/dalabommba/Desktop/AI_Passport/B02R交付物")
SERIAL_PORT = os.environ.get("TV_SERIAL_PORT", "/dev/cu.usbmodem101")
BAUD_RATE = 115200
BIND_IP = os.environ.get("TV_BIND_IP", "192.168.123.185")
PORT = int(os.environ.get("TV_PORT", "8096"))
TEST_CHANNELS_FILE = os.environ.get("TV_CHANNELS_FILE", "tests/test_channels.txt")

class StabilityTestRunner:
    def __init__(self, target_duration_s: int = 1800, channel: str = "baseline"):
        self.target_duration_s = target_duration_s
        self.channel_name = channel
        os.makedirs(DELIVERABLES_DIR, exist_ok=True)
        self.server_log_path = os.path.join(DELIVERABLES_DIR, "stability_30min_server.log")
        self.serial_log_path = os.path.join(DELIVERABLES_DIR, "stability_30min_serial.log")
        self.server_log_file = open(self.server_log_path, "w", encoding="utf-8", buffering=1)
        self.serial_log_file = open(self.serial_log_path, "w", encoding="utf-8", buffering=1)

        self.serial_lines: List[str] = []
        self.server_lines: List[str] = []
        self.stop_requested = threading.Event()
        self.ser: Optional[serial.Serial] = None
        self.server: Optional[AVServer] = None
        self.periodic_metrics: List[Dict[str, Any]] = []
        self.session_resets = 0

    def log_server(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} SRV] {msg}"
        self.server_lines.append(line)
        if self.server_log_file and not self.server_log_file.closed:
            self.server_log_file.write(line + "\n")
        print(line, flush=True)

    def log_serial(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} DEV] {msg}"
        self.serial_lines.append(line)
        if self.serial_log_file and not self.serial_log_file.closed:
            self.serial_log_file.write(line + "\n")
        print(line, flush=True)
        self.parse_device_line(msg)

    def parse_device_line(self, line: str):
        if "Session reset" in line or "RX connect failed" in line:
            self.session_resets += 1

        # CLOCK_ESTIMATED interval_frames=... interval_ms=... dropped=... queue_high=... heap=...
        m_interval = re.search(
            r"CLOCK_ESTIMATED interval_frames=(\d+) interval_ms=(\d+) dropped=(\d+) .* heap=(\d+) largest=(\d+) decode_max_ms=(\d+) .* rx_pkts=(\d+) .* rx_audio=(\d+)",
            line
        )
        if m_interval:
            frames = int(m_interval.group(1))
            interval_ms = int(m_interval.group(2))
            dropped = int(m_interval.group(3))
            heap = int(m_interval.group(4))
            largest = int(m_interval.group(5))
            decode_max_ms = int(m_interval.group(6))
            rx_pkts = int(m_interval.group(7))
            rx_audio = int(m_interval.group(8))

            fps = round(frames * 1000.0 / interval_ms, 2) if interval_ms > 0 else 0.0
            rec = {
                "timestamp": round(time.time(), 3),
                "frames": frames,
                "interval_ms": interval_ms,
                "fps": fps,
                "dropped": dropped,
                "heap": heap,
                "largest_block": largest,
                "decode_max_ms": decode_max_ms,
                "rx_pkts": rx_pkts,
                "rx_audio": rx_audio
            }
            self.periodic_metrics.append(rec)
            self.log_server(f"[METRIC] 10s Window: rendered_fps={fps} dropped={dropped} heap={heap} largest={largest}")

    def serial_loop(self):
        while not self.stop_requested.is_set():
            try:
                if self.ser and self.ser.is_open:
                    line = self.ser.readline()
                    if line:
                        s = line.decode("utf-8", errors="replace").strip()
                        if s:
                            self.log_serial(s)
                else:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.05)

    def run(self) -> Dict[str, Any]:
        os.environ["TV_CHANNELS_FILE"] = TEST_CHANNELS_FILE
        load_channels(Path(TEST_CHANNELS_FILE))
        self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        ser_thread = threading.Thread(target=self.serial_loop, daemon=True)
        ser_thread.start()

        media = Media(b"", (b"",))
        self.server = AVServer(
            media, None, bind=BIND_IP, port=PORT, logger=self.log_server
        )
        self.server.live_enabled = True
        self.server.channel_name = self.channel_name

        srv_thread = threading.Thread(target=self.server.serve, daemon=True)
        srv_thread.start()
        self.server.ready.wait(timeout=3.0)
        self.log_server(f"Stability server started on {BIND_IP}:{PORT}, channel={self.channel_name}, target_duration={self.target_duration_s}s")

        # Wait for device session to connect before starting the endurance clock
        self.log_server("Waiting for device connection...")
        t_wait = time.monotonic()
        while time.monotonic() - t_wait < 30.0:
            if getattr(self.server, "session_id", 0) != 0 and getattr(self.server, "audio_sent", 0) >= 10:
                self.log_server(f"Device connected to session {self.server.session_id}. Starting endurance timer.")
                break
            time.sleep(0.2)

        t_start = time.monotonic()
        warmup_duration_s = 30.0
        total_target_run_s = self.target_duration_s + (warmup_duration_s if self.target_duration_s >= 1800 else 0.0)

        try:
            last_progress = 0
            while time.monotonic() - t_start < total_target_run_s:
                elapsed = time.monotonic() - t_start
                remaining = total_target_run_s - elapsed
                if int(elapsed) - last_progress >= 30:
                    last_progress = int(elapsed)
                    self.log_server(f"Progress: {elapsed:.0f}s / {total_target_run_s:.0f}s ({remaining:.0f}s remaining), metrics_count={len(self.periodic_metrics)}")
                time.sleep(0.5)
        finally:
            self.stop_requested.set()
            if self.server:
                self.server.stop.set()
            # Wait for server cleanup and final device teardown metrics before closing serial
            time.sleep(2.0)
            if self.ser and self.ser.is_open:
                self.ser.close()

        total_elapsed = round(time.monotonic() - t_start, 2)
        
        # Analyze metrics
        # Exclude initial warm-up (first 3 windows = 30 seconds)
        post_warmup = self.periodic_metrics[3:] if len(self.periodic_metrics) > 3 else []
        if any("interval_ms" in m for m in post_warmup):
            total_f = sum(m.get("frames", 0) for m in post_warmup)
            total_ms = sum(m.get("interval_ms", 0) for m in post_warmup)
            avg_fps = round(total_f * 1000.0 / total_ms, 2) if total_ms > 0 else 0.0
        else:
            fps_list_raw = [m["fps"] for m in post_warmup] if post_warmup else [0.0]
            avg_fps = round(sum(fps_list_raw) / len(fps_list_raw), 2) if fps_list_raw else 0.0

        fps_list = [m["fps"] for m in post_warmup] if post_warmup else [0.0]
        min_heap = min((m["heap"] for m in self.periodic_metrics), default=0)
        max_decode_ms = max((m["decode_max_ms"] for m in self.periodic_metrics), default=0)

        # Gate criteria:
        # 1. 30min endurance must have target_duration_s >= 1800 and actual >= 1800
        # 2. post_warmup must contain >= 170 windows of steady state (no fallback to warmup)
        # 3. average rendered fps >= 11.0
        # 4. zero unexpected session resets
        is_30min_target = (self.target_duration_s >= 1800)
        has_full_duration = (total_elapsed >= 1800.0)
        has_adequate_windows = (len(post_warmup) >= 180 and sum(m.get("interval_ms", 10000) for m in post_warmup) >= 1800000)
        fps_target_met = (avg_fps >= 11.0)
        no_resets = (self.session_resets == 0)

        if is_30min_target:
            if has_full_duration and has_adequate_windows and fps_target_met and no_resets:
                verdict = "PASS"
            else:
                verdict = "FAIL"
        else:
            # Smoke run or short debug test: never emit PASS for 30min gate
            verdict = "SMOKE_COMPLETED" if (fps_target_met and no_resets) else "SMOKE_FAILED"

        summary = {
            "test_case": "30min_continuous_stability" if is_30min_target else "smoke_stability",
            "channel": self.channel_name,
            "target_duration_s": self.target_duration_s,
            "actual_duration_s": total_elapsed,
            "windows_count": len(self.periodic_metrics),
            "post_warmup_windows": len(post_warmup),
            "warmup_s": warmup_duration_s,
            "average_rendered_fps": avg_fps,
            "min_fps_window": min(fps_list) if fps_list else 0.0,
            "max_fps_window": max(fps_list) if fps_list else 0.0,
            "min_free_heap_bytes": min_heap,
            "max_decode_ms": max_decode_ms,
            "session_resets_detected": self.session_resets,
            "fps_target_met": fps_target_met,
            "verdict": verdict
        }

        # Save summary
        with open(os.path.join(DELIVERABLES_DIR, "stability_30min_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        try:
            self.server_log_file.flush()
            self.server_log_file.close()
            self.serial_log_file.flush()
            self.serial_log_file.close()
        except Exception:
            pass

        self.log_server(f"Stability test complete. Summary: {summary}")
        return summary

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=1800, help="Test duration in seconds (default: 1800)")
    parser.add_argument("--channel", default="baseline", help="Channel name (default: baseline)")
    args = parser.parse_args()

    runner = StabilityTestRunner(target_duration_s=args.duration, channel=args.channel)
    summary = runner.run()
    print("\n" + json.dumps(summary, indent=2))
