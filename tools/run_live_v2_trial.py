#!/usr/bin/env python3
"""Run a LiveV2 trial with concurrent ESP32-C3 serial telemetry capture.

Executes tools/run_live_v2.py while recording device serial output, parsing
both server-side and client-side metrics into a unified summary.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from summarize_live_trial import summarize

try:
    import serial
except ImportError:
    serial = None

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BAUD = 115200


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--channels", type=Path, default=REPO_ROOT / "channels.txt")
    parser.add_argument("--channel", default="ch000")
    parser.add_argument("--seconds", type=int, default=180, help="Observation seconds after media start and warm-up")
    parser.add_argument("--warmup-seconds", type=int, default=0)
    parser.add_argument("--startup-timeout", type=int, default=60)
    parser.add_argument("--loop", action="store_true", help="Finite baseline only; omit for IPTV")
    parser.add_argument("--video-budget", type=int, default=120000)
    parser.add_argument("--engine", choices=("v2", "legacy"), default="v2")
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--adaptive", choices=("yes", "no"), default="no")
    parser.add_argument("--low-res", action="store_true", default=False)
    parser.add_argument("--smooth", action="store_true", default=False)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--serial-port", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if serial is None:
        parser.error("pyserial is required; no device capture started")
    if args.seconds <= 0 or args.warmup_seconds < 0 or args.startup_timeout <= 0:
        parser.error("Invalid observation, warm-up or startup duration")
    repo = args.repo.resolve()
    channels = args.channels.resolve()
    if not channels.is_file() or not (repo / "tools/run_live_v2.py").is_file():
        parser.error("Existing candidate repository and channel file required")
    try:
        opened_serial = serial.Serial(args.serial_port, DEFAULT_BAUD, timeout=0.1)
    except Exception as error:
        parser.error("Serial port unavailable: " + type(error).__name__)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="live-capture-", dir=args.output_dir.resolve()))
    started = time.monotonic()
    event_records = []
    event_lock = threading.Lock()
    event_file = (run_dir / "capture_events.jsonl").open("x", encoding="utf-8", buffering=1)
    reader_failed = threading.Event()
    media_ready = threading.Event()

    def event(kind, **fields):
        with event_lock:
            entry = dict(t=round(time.monotonic()-started, 6), event=kind, **fields)
            event_records.append(entry)
            event_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    event("capture_started", utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
          channel=args.channel, engine=args.engine, video_budget=args.video_budget,
          observation_seconds=args.seconds, warmup_seconds=args.warmup_seconds,
          loop=args.loop, firmware_identity="UNVERIFIED")

    server_log_file = (run_dir / "server.log").open("w", encoding="utf-8", buffering=1)
    serial_log_file = (run_dir / "serial.log").open("w", encoding="utf-8", buffering=1)

    print(f"=== LiveV2 Hardware Trial ===")
    print(f"Channel: {args.channel} | Engine: {args.engine} | Budget: {args.video_budget} B/s")
    print(f"Bind: {args.bind}:{args.port} | Serial: {args.serial_port}")
    print(f"Output: {run_dir}")

    stop_event = threading.Event()
    device_windows = []
    audio_empty_events = []

    def read_serial():
        ser = opened_serial
        try:
            while not stop_event.is_set():
                line = ser.readline()
                if line:
                    s = line.decode("utf-8", errors="replace").strip()
                    if s:
                        now_str = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        serial_log_file.write(f"[t={time.monotonic()-started:.6f} DEV] {s}\n")
                        serial_log_file.flush()

                        # Parse CLOCK_ESTIMATED
                        m = re.search(
                            r"interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+).*?heap=(\d+).*?decode_max_ms=(\d+).*?late=(\d+).*?stray=(\d+).*?nobuf=(\d+)",
                            s,
                        )
                        if m:
                            frames = int(m.group(1))
                            ms = int(m.group(2))
                            heap = int(m.group(4))
                            rec = {
                                "frames": frames,
                                "interval_ms": ms,
                                "fps": round(frames * 1000.0 / ms, 2) if ms > 0 else 0.0,
                                "dropped": int(m.group(3)),
                                "heap": heap,
                                "decode_max_ms": int(m.group(5)),
                                "late": int(m.group(6)),
                                "stray": int(m.group(7)),
                                "nobuf": int(m.group(8)),
                            }
                            device_windows.append(rec)
                            print(f"  [DEVICE WINDOW {len(device_windows)}] frames={rec['frames']} ms={rec['interval_ms']} "
                                  f"fps={rec['fps']} dropped={rec['dropped']} late={rec['late']} nobuf={rec['nobuf']} heap={rec['heap']}")

                        if "AUDIO_EMPTY" in s:
                            audio_empty_events.append(s)
                            print(f"  [DEVICE EVENT] AUDIO_EMPTY: {s}")

                        if "Session reset" in s:
                            print(f"  [DEVICE EVENT] Session reset: {s}")
        except Exception as e:
            event("serial_error", error_type=type(e).__name__)
            reader_failed.set()
            print("Serial reader failed; capture cannot pass")
        finally:
            if ser:
                try: ser.close()
                except Exception: pass

    ser_thread = threading.Thread(target=read_serial, daemon=True)
    ser_thread.start()

    # Launch run_live_v2.py
    cmd = [
        sys.executable,
        "-u",
        str(repo / "tools/run_live_v2.py"),
        "--repo", str(repo),
        "--channels", str(channels),
        "--channel", args.channel,
        "--bind", args.bind,
        "--port", str(args.port),
        "--video-budget", str(args.video_budget),
        "--engine", args.engine,
        "--fps", str(args.fps),
        "--adaptive", args.adaptive,
        "--output-dir", str(run_dir),
    ]

    if args.low_res:
        cmd.append("--low-res")

    if args.smooth:
        cmd.append("--smooth")

    if args.loop:
        cmd.append("--loop")

    print(f"Starting server: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True,
        )
    except Exception as error:
        event("capture_error", error_type=type(error).__name__)
        stop_event.set()
        ser_thread.join(timeout=2)
        opened_serial.close()
        for handle in (server_log_file, serial_log_file, event_file): handle.close()
        return 1
    event("server_started", pid=proc.pid)

    server_reports = []
    backlog_trims = 0

    def read_server():
        nonlocal backlog_trims
        try:
            for line in proc.stdout:
                server_log_file.write(f"[t={time.monotonic()-started:.6f} SRV] {line}")
                server_log_file.flush()
                if not media_ready.is_set() and ("LIVE2_START " in line or
                        (args.engine == "legacy" and "live t=" in line)):
                    event("media_started")
                    media_ready.set()
                if "LIVE2 session=" in line:
                    server_reports.append(line.strip())
                    print(f"  [SERVER] {line.strip()}")
                elif "LIVE2_BACKLOG_TRIM" in line:
                    backlog_trims += 1
                    print(f"  [SERVER EVENT] {line.strip()}")
                elif "LIVE2_START" in line or "LIVE2_SCHEDULE_REBUFFER" in line:
                    print(f"  [SERVER EVENT] {line.strip()}")
        except Exception as error:
            event("server_reader_error", error_type=type(error).__name__)
            reader_failed.set()

    srv_thread = threading.Thread(target=read_server, daemon=True)
    srv_thread.start()

    capture_status = "CAPTURE_INCOMPLETE"
    exit_code = 1

    def wait_to(deadline):
        while time.monotonic() < deadline:
            if reader_failed.is_set(): raise RuntimeError("serial capture failed")
            if proc.poll() is not None: raise RuntimeError("server exited early")
            time.sleep(0.1)

    try:
        deadline = time.monotonic() + args.startup_timeout
        while not media_ready.wait(0.1):
            if reader_failed.is_set() or proc.poll() is not None:
                raise RuntimeError("media startup failed")
            if time.monotonic() >= deadline: raise TimeoutError("media startup timed out")
        wait_to(time.monotonic() + args.warmup_seconds)
        event("observation_started")
        wait_to(time.monotonic() + args.seconds)
        event("observation_completed")
        capture_status, exit_code = "CAPTURE_COMPLETED", 0
    except KeyboardInterrupt:
        capture_status, exit_code = "CAPTURE_INTERRUPTED", 130
        event("capture_interrupted")
    except Exception as error:
        event("capture_error", error_type=type(error).__name__)
    finally:
        event("stop_requested", cause=capture_status)
        if proc.poll() is None: proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            event("shutdown_error", reason="forced_termination")
            capture_status, exit_code = "CAPTURE_INCOMPLETE", 1
            if os.name == "posix": os.killpg(proc.pid, signal.SIGKILL)
            else: proc.kill()
            proc.wait(timeout=3)
        event("server_exited", returncode=proc.returncode)
        srv_thread.join(timeout=3)
        # Preserve terminal playback counters, then classify reconnect attempts.
        time.sleep(3)
        stop_event.set()
        ser_thread.join(timeout=2)
        opened_serial.close()
        ser_thread.join(timeout=1)
        if reader_failed.is_set() or srv_thread.is_alive() or ser_thread.is_alive():
            event("capture_error", reason="reader_failure_or_join_timeout")
            capture_status, exit_code = "CAPTURE_INCOMPLETE", 1
        if not srv_thread.is_alive(): server_log_file.close()
        else: server_log_file.flush()
        if not ser_thread.is_alive(): serial_log_file.close()
        else: serial_log_file.flush()
        result = summarize((run_dir / "serial.log").read_text(),
                           (run_dir / "server.log").read_text(), event_records)
        if not result["all_windows"]["count"] or not result["terminal_counters_complete"]:
            capture_status, exit_code = "CAPTURE_INCOMPLETE", 1
        result.update(capture_status=capture_status,
                      requested_observation_seconds=args.seconds,
                      warmup_seconds=args.warmup_seconds, run_dir=str(run_dir))
        event("capture_finished", capture_status=capture_status)
        if not srv_thread.is_alive() and not ser_thread.is_alive(): event_file.close()
        else: event_file.flush()
        with (run_dir / "trial_summary.json").open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
        print(json.dumps({key: result[key] for key in
              ("capture_status", "acceptance", "all_windows", "observation_windows",
               "silence_seconds", "server", "warnings")}, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
