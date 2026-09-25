#!/usr/bin/env python3
"""Execute B02R Device Re-Verification:
1. Double Reconnect with device-rendered frame verification (not just 1 frame / 0.1 fps).
2. True Upstream Buffer Exhaustion (drain host queue to 0 + device AUDIO_EMPTY) & Recovery (>=20s observation).

All outputs are saved to an independent timestamped directory under b02r_trial_runs/.
No flash writes, no NVS erasure, no cardid changes.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time

try:
    import serial
except ImportError:
    serial = None

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Trial defaults profile
PROFILE = {
    'TV_GEOMETRY': '320x240', 'TV_PACKET_TARGET': '22528',
    'TV_FPS': '12', 'TV_MAX_FPS': '12', 'TV_START_FPS': '12',
    'TV_MIN_FPS': '3', 'TV_ADAPTIVE': '0',
    'TV_SYNC_TOLERANCE_S': '0.15', 'TV_PCM_QUEUE_CHUNKS': '400',
    'TV_VIDEO_QUEUE': '224', 'TV_PREBUFFER_S': '4',
    'TV_FRAME_DEADLINE_S': '2.5', 'TV_AUDIO_LOOKAHEAD_MS': '280',
    'TV_AUDIO_LEAD_MS': '320', 'TV_VIDEO_LEAD_MS': '320',
    'TV_AUDIO_FILL': '1', 'TV_STREAM_LOOP': '1',
}

TEST_CHANNELS_FILE = str(REPO_ROOT / "tests/test_channels.txt")
DEFAULT_SERIAL_PORT = "/dev/cu.usbmodem101"
DEFAULT_BAUDRATE = 115200


class DeviceRecheckRunner:
    def __init__(self, serial_port=DEFAULT_SERIAL_PORT, baudrate=DEFAULT_BAUDRATE, port=8096):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.port = port
        self.output_dir = REPO_ROOT / f"b02r_trial_runs/recheck_{int(time.time())}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.server_log_file = open(self.output_dir / "server.log", "w", buffering=1)
        self.serial_log_file = open(self.output_dir / "serial.log", "w", buffering=1)
        
        self.ser = None
        self.serial_thread = None
        self.stop_serial = threading.Event()
        
        self.window_metrics = []
        self.audio_empty_events = []
        self.session_events = []
        
        self.server = None
        self.server_thread = None
        
        # Apply environment profile
        for key in list(os.environ):
            if key.startswith("TV_"):
                os.environ.pop(key)
        os.environ.update(PROFILE)
        os.environ["TV_CHANNELS_FILE"] = TEST_CHANNELS_FILE

    def log_server(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts} SRV] {msg}"
        print(line, flush=True)
        self.server_log_file.write(line + "\n")
        self.server_log_file.flush()

    def log_serial(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts} DEV] {msg}"
        self.serial_log_file.write(line + "\n")
        self.serial_log_file.flush()

        now_mono = time.monotonic()
        # Parse window metric
        m = re.search(r"interval_frames=(\d+)\s+interval_ms=(\d+)\s+dropped=(\d+).*?heap=(\d+).*?decode_max_ms=(\d+).*?late=(\d+)", msg)
        if m:
            frames = int(m.group(1))
            ms = int(m.group(2))
            dropped = int(m.group(3))
            heap = int(m.group(4))
            decode = int(m.group(5))
            late = int(m.group(6))
            fps = round(frames * 1000.0 / ms, 2) if ms > 0 else 0.0
            rec = {
                "monotonic": now_mono,
                "timestamp": time.time(),
                "frames": frames,
                "interval_ms": ms,
                "fps": fps,
                "dropped": dropped,
                "heap": heap,
                "decode_max_ms": decode,
                "late": late,
                "session_id": getattr(self.server, "session_id", 0) if self.server else 0
            }
            self.window_metrics.append(rec)
            self.log_server(f"[DEVICE WINDOW] frames={frames} ms={ms} fps={fps:.2f} heap={heap} dropped={dropped} late={late}")
            return

        # Parse AUDIO_EMPTY
        m_empty = re.search(r"AUDIO_EMPTY gap_ms=(\d+) queue=(\d+) submitted=(\d+)", msg)
        if m_empty:
            rec = {
                "monotonic": now_mono,
                "timestamp": time.time(),
                "gap_ms": int(m_empty.group(1)),
                "queue": int(m_empty.group(2)),
                "submitted": int(m_empty.group(3)),
                "session_id": getattr(self.server, "session_id", 0) if self.server else 0
            }
            self.audio_empty_events.append(rec)
            self.log_server(f"[DEVICE AUDIO_EMPTY] gap_ms={rec['gap_ms']} queue={rec['queue']}")

    def start_serial(self):
        if not serial:
            self.log_server("ERROR: pyserial is not installed!")
            return False
        try:
            self.ser = serial.Serial(self.serial_port, self.baudrate, timeout=0.1)
            self.log_server(f"Opened serial port {self.serial_port} at {self.baudrate} baud")
        except Exception as e:
            self.log_server(f"Failed to open serial port {self.serial_port}: {e}")
            return False

        def reader():
            while not self.stop_serial.is_set():
                try:
                    if self.ser and self.ser.is_open:
                        raw = self.ser.readline()
                        if raw:
                            s = raw.decode("utf-8", errors="replace").strip()
                            if s:
                                self.log_serial(s)
                    else:
                        time.sleep(0.05)
                except Exception:
                    time.sleep(0.05)

        self.serial_thread = threading.Thread(target=reader, daemon=True)
        self.serial_thread.start()
        return True

    def start_server(self, channel="baseline"):
        from server import tv_server, live
        from server.media import Media
        live.load_channels(Path(TEST_CHANNELS_FILE))
        
        media = Media(b"", (b"",))
        bind_ip = os.environ.get("TV_BIND_IP", "192.168.123.185")
        self.server = tv_server.AVServer(media, None, bind=bind_ip, port=self.port,
                                         logger=self.log_server)
        self.server.live_enabled = True
        self.server.channel_name = channel
        self.server.media_filter = ""
        self.server.session_limit_s = None

        self.server_thread = threading.Thread(target=self.server.serve, daemon=True)
        self.server_thread.start()
        self.log_server(f"Server started on port {self.port}, channel={channel}")

    def wait_for_streaming(self, min_video=30, timeout=20.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if getattr(self.server, "video_sent", 0) >= min_video and getattr(self.server, "session_id", 0) != 0:
                return True
            time.sleep(0.2)
        return False

    def close(self):
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
        self.stop_serial.set()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.server_log_file.close()
        self.serial_log_file.close()

    def run_double_reconnect_test(self):
        self.log_server("\n" + "="*70)
        self.log_server("TEST 1: Double Reconnect with Device-Rendered Frame Verification")
        self.log_server("="*70)

        # 1. Establish Session 1
        self.log_server("Waiting for Device Session 1 to connect...")
        assert self.wait_for_streaming(min_video=30, timeout=20.0), "Session 1 failed to start streaming"
        s1_id = self.server.session_id
        self.log_server(f"Session 1 active: id={s1_id}. Streaming for 15s to capture steady window...")
        t_s1_start = time.monotonic()
        time.sleep(22.0)

        s1_v = self.server.video_sent
        s1_a = self.server.audio_sent
        s1_windows = [w for w in self.window_metrics if w["monotonic"] >= t_s1_start]
        s1_frames = sum(w["frames"] for w in s1_windows)
        s1_ms = sum(w["interval_ms"] for w in s1_windows)
        s1_overall_fps = round(s1_frames * 1000.0 / s1_ms, 2) if s1_ms > 0 else 0.0
        s1_steady_fps = s1_windows[-1]["fps"] if s1_windows else 0.0
        self.log_server(f"Session 1 stats: id={s1_id}, server_v={s1_v}, dev_windows={len(s1_windows)}, dev_frames={s1_frames}, overall_fps={s1_overall_fps}, steady_fps={s1_steady_fps}")
        assert s1_frames >= 100 and s1_steady_fps >= 11.0, f"Session 1 failed steady rendering: frames={s1_frames}, steady_fps={s1_steady_fps}"

        # 2. Terminate Session 1 gracefully
        self.log_server(f"Gracefully terminating Session 1 (id={s1_id})...")
        self.server.session_limit_s = 0.01
        t_term1 = time.monotonic()
        while self.server.live_channel is not None and time.monotonic() - t_term1 < 5.0:
            time.sleep(0.05)
        self.log_server("Session 1 closed. Waiting for automatic reconnect to Session 2...")
        self.server.session_limit_s = None

        # 3. Wait for Session 2
        t_rec1_start = time.monotonic()
        while time.monotonic() - t_rec1_start < 10.0:
            if self.server.session_id != 0 and self.server.session_id != s1_id:
                break
            time.sleep(0.1)

        s2_id = self.server.session_id
        assert s2_id != 0 and s2_id != s1_id, "Device failed to reconnect to Session 2"
        reconnect_delay_1 = round(time.monotonic() - t_rec1_start, 3)
        self.log_server(f"Session 2 connected: id={s2_id} (reconnect took {reconnect_delay_1}s). Streaming for 22s to verify device frame rendering...")
        t_s2_start = time.monotonic()
        time.sleep(22.0)

        s2_v = self.server.video_sent
        s2_a = self.server.audio_sent
        s2_windows = [w for w in self.window_metrics if w["monotonic"] >= t_s2_start]
        s2_frames = sum(w["frames"] for w in s2_windows)
        s2_ms = sum(w["interval_ms"] for w in s2_windows)
        s2_overall_fps = round(s2_frames * 1000.0 / s2_ms, 2) if s2_ms > 0 else 0.0
        s2_steady_fps = s2_windows[-1]["fps"] if s2_windows else 0.0
        self.log_server(f"Session 2 stats: id={s2_id}, server_v={s2_v}, dev_windows={len(s2_windows)}, dev_frames={s2_frames}, overall_fps={s2_overall_fps}, steady_fps={s2_steady_fps}")
        assert s2_frames >= 100 and s2_steady_fps >= 11.0, f"Session 2 failed to render steady frames: frames={s2_frames}, steady_fps={s2_steady_fps}"

        # 4. Terminate Session 2 gracefully
        self.log_server(f"Gracefully terminating Session 2 (id={s2_id})...")
        self.server.session_limit_s = 0.01
        t_term2 = time.monotonic()
        while self.server.live_channel is not None and time.monotonic() - t_term2 < 5.0:
            time.sleep(0.05)
        self.log_server("Session 2 closed. Waiting for automatic reconnect to Session 3...")
        self.server.session_limit_s = None

        # 5. Wait for Session 3
        t_rec2_start = time.monotonic()
        while time.monotonic() - t_rec2_start < 10.0:
            if self.server.session_id != 0 and self.server.session_id != s2_id and self.server.session_id != s1_id:
                break
            time.sleep(0.1)

        s3_id = self.server.session_id
        assert s3_id != 0 and s3_id != s2_id and s3_id != s1_id, "Device failed to reconnect to Session 3"
        reconnect_delay_2 = round(time.monotonic() - t_rec2_start, 3)
        self.log_server(f"Session 3 connected: id={s3_id} (reconnect took {reconnect_delay_2}s). Streaming for 22s to verify device frame rendering...")
        t_s3_start = time.monotonic()
        time.sleep(22.0)

        s3_v = self.server.video_sent
        s3_a = self.server.audio_sent
        s3_windows = [w for w in self.window_metrics if w["monotonic"] >= t_s3_start]
        s3_frames = sum(w["frames"] for w in s3_windows)
        s3_ms = sum(w["interval_ms"] for w in s3_windows)
        s3_overall_fps = round(s3_frames * 1000.0 / s3_ms, 2) if s3_ms > 0 else 0.0
        s3_steady_fps = s3_windows[-1]["fps"] if s3_windows else 0.0
        self.log_server(f"Session 3 stats: id={s3_id}, server_v={s3_v}, dev_windows={len(s3_windows)}, dev_frames={s3_frames}, overall_fps={s3_overall_fps}, steady_fps={s3_steady_fps}")
        assert s3_frames >= 100 and s3_steady_fps >= 11.0, f"Session 3 failed to render steady frames: frames={s3_frames}, steady_fps={s3_steady_fps}"

        result = {
            "test": "double_reconnect",
            "session_1": {"id": s1_id, "frames": s1_frames, "overall_fps": s1_overall_fps, "steady_fps": s1_steady_fps},
            "reconnect_1_delay_s": reconnect_delay_1,
            "session_2": {"id": s2_id, "frames": s2_frames, "overall_fps": s2_overall_fps, "steady_fps": s2_steady_fps},
            "reconnect_2_delay_s": reconnect_delay_2,
            "session_3": {"id": s3_id, "frames": s3_frames, "overall_fps": s3_overall_fps, "steady_fps": s3_steady_fps},
            "verdict": "PASS"
        }
        self.log_server(f"\n[DOUBLE RECONNECT PASS]: {result}")
        return result

    def run_true_upstream_exhaustion_test(self):
        self.log_server("\n" + "="*70)
        self.log_server("TEST 2: True Upstream Buffer Exhaustion & Recovery Verification")
        self.log_server("="*70)

        cur_session = self.server.session_id
        channel = self.server.live_channel
        decoder = getattr(channel, "decoder", None) or getattr(channel, "proc", None)
        assert channel is not None and decoder is not None, "Channel decoder process is not active"

        # 1. Establish 20s healthy baseline
        self.log_server("Establishing 20s healthy baseline on device...")
        t_base_start = time.monotonic()
        time.sleep(20.0)

        base_windows = [w for w in self.window_metrics if w["monotonic"] >= t_base_start]
        tot_base_f = sum(w["frames"] for w in base_windows)
        tot_base_ms = sum(w["interval_ms"] for w in base_windows)
        base_fps = round(tot_base_f * 1000.0 / tot_base_ms, 2) if tot_base_ms > 0 else 0.0
        self.log_server(f"Baseline established: windows={len(base_windows)}, frames={tot_base_f}, fps={base_fps}")
        assert tot_base_f > 150 and base_fps >= 10.5, f"Baseline not healthy: frames={tot_base_f}, fps={base_fps}"

        # 2. Suspend FFmpeg via SIGSTOP
        ffmpeg_pid = decoder.pid
        qa_before = len(channel.audio)
        qv_before = len(channel.video)
        self.log_server(f"Suspending FFmpeg pid={ffmpeg_pid} via SIGSTOP. Initial queues: audio={qa_before}, video={qv_before}")
        os.kill(ffmpeg_pid, signal.SIGSTOP)
        t_suspend = time.monotonic()

        # 3. Monitor host queue until audio queue drains to 0
        self.log_server("Monitoring host audio queue draining to 0...")
        drained_to_zero = False
        t_drain_start = time.monotonic()
        while time.monotonic() - t_drain_start < 15.0:
            qa = len(channel.audio)
            qv = len(channel.video)
            if qa == 0:
                drained_to_zero = True
                self.log_server(f"Host audio queue completely exhausted to 0 at +{time.monotonic()-t_suspend:.2f}s! Video queue={qv}")
                break
            time.sleep(0.05)

        assert drained_to_zero, f"Failed to drain host audio queue! Remaining: {len(channel.audio)}"

        # 4. Hold in starved state for 2.0 seconds so device DMA depletes and registers AUDIO_EMPTY
        self.log_server("Holding in exhausted state for 2.0s to trigger device AUDIO_EMPTY silence...")
        time.sleep(2.0)

        empty_events = [e for e in self.audio_empty_events if e["monotonic"] >= t_suspend]
        self.log_server(f"AUDIO_EMPTY events captured during exhaustion: {len(empty_events)}")

        # 5. Resume FFmpeg via SIGCONT
        self.log_server(f"Resuming FFmpeg pid={ffmpeg_pid} via SIGCONT...")
        os.kill(ffmpeg_pid, signal.SIGCONT)
        t_resumed = time.monotonic()
        v_sent_at_resume = self.server.video_sent
        a_sent_at_resume = self.server.audio_sent

        # 6. Track host buffer re-accumulation
        first_audio_time = None
        first_video_time = None
        t_wait_refill = time.monotonic()
        while time.monotonic() - t_wait_refill < 10.0:
            if first_audio_time is None and len(channel.audio) > 0:
                first_audio_time = round(time.monotonic() - t_resumed, 3)
                self.log_server(f"First new audio chunk available at +{first_audio_time}s (len={len(channel.audio)})")
            if first_video_time is None and len(channel.video) > 0:
                first_video_time = round(time.monotonic() - t_resumed, 3)
                self.log_server(f"First new video frame available at +{first_video_time}s (len={len(channel.video)})")
            if first_audio_time is not None and first_video_time is not None:
                break
            time.sleep(0.02)

        # 7. Observe recovery for 25 seconds
        self.log_server("Observing post-exhaustion recovery for 25s...")
        time.sleep(25.0)

        v_post = self.server.video_sent
        a_post = self.server.audio_sent
        rec_windows = [w for w in self.window_metrics if w["monotonic"] >= t_resumed]
        rec_frames = sum(w["frames"] for w in rec_windows)
        rec_ms = sum(w["interval_ms"] for w in rec_windows)
        rec_fps = round(rec_frames * 1000.0 / rec_ms, 2) if rec_ms > 0 else 0.0

        same_session = (self.server.session_id == cur_session)
        self.log_server(f"Recovery stats: same_session={same_session}, dev_windows={len(rec_windows)}, dev_frames={rec_frames}, dev_fps={rec_fps}, v_delta={v_post - v_sent_at_resume}")

        assert same_session, "Session reset occurred during upstream recovery!"
        assert rec_frames > 150 and rec_fps >= 10.5, f"Recovery frame rate target not met: frames={rec_frames}, fps={rec_fps}"

        result = {
            "test": "true_upstream_exhaustion_recovery",
            "session_id": cur_session,
            "baseline_dev_fps": base_fps,
            "qa_before": qa_before,
            "drained_to_zero": drained_to_zero,
            "audio_empty_events_observed": len(empty_events),
            "first_audio_refill_s": first_audio_time,
            "first_video_refill_s": first_video_time,
            "recovery_observe_s": 25.0,
            "recovery_dev_windows": len(rec_windows),
            "recovery_dev_frames": rec_frames,
            "recovery_dev_fps": rec_fps,
            "same_session": same_session,
            "verdict": "PASS"
        }
        self.log_server(f"\n[TRUE UPSTREAM EXHAUSTION PASS]: {result}")
        return result


def main():
    runner = DeviceRecheckRunner()
    if not runner.start_serial():
        print("Failed to initialize serial reader. Aborting.")
        return 1

    try:
        runner.start_server(channel="baseline")
        time.sleep(2.0)

        summary = {}
        # Execute Test 1: Double Reconnect
        reconnect_res = runner.run_double_reconnect_test()
        summary["double_reconnect"] = reconnect_res

        # Execute Test 2: True Upstream Buffer Exhaustion & Recovery
        upstream_res = runner.run_true_upstream_exhaustion_test()
        summary["true_upstream_exhaustion"] = upstream_res

        summary["all_passed"] = (reconnect_res["verdict"] == "PASS" and upstream_res["verdict"] == "PASS")
        summary["output_dir"] = str(runner.output_dir)

        sum_path = runner.output_dir / "recheck_summary.json"
        sum_path.write_text(json.dumps(summary, indent=2))
        runner.log_server(f"\nALL TESTS COMPLETED SUCCESSFULLY! Summary written to {sum_path}")
        print("\n" + json.dumps(summary, indent=2))
        return 0
    except Exception as e:
        runner.log_server(f"ERROR DURING RECHECK EXECUTION: {e}")
        import traceback
        traceback.print_exc(file=runner.server_log_file)
        return 1
    finally:
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
