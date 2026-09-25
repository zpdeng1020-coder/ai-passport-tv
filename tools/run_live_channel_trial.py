#!/usr/bin/env python3
"""Run a live TV channel trial (e.g. ch000 CCTV1) using the B02R candidate profile.
Captures both server.log and serial.log into an independent directory under b02r_trial_runs/.
"""
import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

try:
    import serial
except ImportError:
    serial = None

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.run_b02r_device_recheck import PROFILE, DEFAULT_SERIAL_PORT, DEFAULT_BAUDRATE

def main():
    channel_key = sys.argv[1] if len(sys.argv) > 1 else "ch000"
    duration_s = int(sys.argv[2]) if len(sys.argv) > 2 else 35
    bind_ip = os.environ.get("TV_BIND_IP", "192.168.123.185")
    port = 8096

    output_dir = REPO_ROOT / f"b02r_trial_runs/trial_{channel_key}_{int(time.time())}"
    output_dir.mkdir(parents=True, exist_ok=True)

    server_log = open(output_dir / "server.log", "w", buffering=1)
    serial_log = open(output_dir / "serial.log", "w", buffering=1)

    print(f"Starting Live Trial on {channel_key} for {duration_s}s. Output dir: {output_dir}")

    # Start serial reader
    stop_serial = threading.Event()
    window_metrics = []

    def serial_loop():
        ser = None
        try:
            ser = serial.Serial(DEFAULT_SERIAL_PORT, DEFAULT_BAUDRATE, timeout=0.1)
            while not stop_serial.is_set():
                line = ser.readline()
                if line:
                    s = line.decode("utf-8", errors="replace").strip()
                    if s:
                        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        serial_log.write(f"[{ts} DEV] {s}\n")
                        serial_log.flush()
                        m = re.search(r"interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+).*?heap=(\d+).*?decode_max_ms=(\d+).*?late=(\d+)", s)
                        if m:
                            rec = {
                                "frames": int(m.group(1)),
                                "interval_ms": int(m.group(2)),
                                "fps": round(int(m.group(1)) * 1000.0 / int(m.group(2)), 2),
                                "dropped": int(m.group(3)),
                                "heap": int(m.group(4)),
                                "late": int(m.group(6)),
                            }
                            window_metrics.append(rec)
                            print(f"[DEVICE WINDOW] frames={rec['frames']} ms={rec['interval_ms']} fps={rec['fps']:.2f} heap={rec['heap']} late={rec['late']}")
        except Exception as e:
            print(f"Serial reader exception: {e}")
        finally:
            if ser:
                try: ser.close()
                except Exception: pass

    ser_thread = threading.Thread(target=serial_loop, daemon=True)
    ser_thread.start()

    # Launch run_b02r_trial.py
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools/run_b02r_trial.py"),
        "--repo", str(REPO_ROOT),
        "--channels", str(REPO_ROOT / "channels.txt"),
        "--channel", channel_key,
        "--bind", bind_ip,
        "--port", str(port),
        "--output-dir", str(output_dir)
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)

    def srv_pipe():
        for line in proc.stdout:
            server_log.write(line)
            server_log.flush()
            if "live t=" in line or "STATE_CHANGE" in line:
                print(line.strip())

    srv_thread = threading.Thread(target=srv_pipe, daemon=True)
    srv_thread.start()

    # Wait for duration
    time.sleep(duration_s)

    # Stop server gracefully
    print(f"Stopping trial after {duration_s}s...")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    time.sleep(2)
    stop_serial.set()
    ser_thread.join(timeout=2)
    server_log.close()
    serial_log.close()

    summary = {
        "channel": channel_key,
        "duration_s": duration_s,
        "windows_count": len(window_metrics),
        "total_frames": sum(w["frames"] for w in window_metrics),
        "windows": window_metrics,
        "output_dir": str(output_dir)
    }
    (output_dir / "trial_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Trial complete. Summary written to {output_dir / 'trial_summary.json'}")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
