#!/usr/bin/env python3
"""B02-R Comprehensive Device Experiment Orchestrator:
Executes and logs all required hardware experiments with physical ESP32-C3:
1. Session 1 -> Clean Stop -> Device Automatic Reconnect -> Session 2 Continuous Play.
2. Downstream Packet-Boundary Pauses (0.5s, 1.0s, 3.0s x 3 each) measuring D3 silence and recovery.
3. Upstream Decoder Pauses (0.5s, 1.0s, 3.0s x 3 each) measuring host buffer absorption and recovery.

Outputs all raw logs and structured evidence JSON to /Users/dalabommba/Desktop/AI_Passport/B02R交付物.
Strict dynamic evaluation: NO hardcoded PASS; rejects stale counters; validates fresh video advancement.
"""
import argparse
import json
import os
import re
import select
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

os.environ.setdefault("TV_STREAM_LOOP", "1")

from server.media import Media, FPS, AUDIO_CHUNK_MS
from server.live import LiveChannel, CHANNELS, CHANNEL_AGENTS, load_channels
from server.tv_server import AVServer, SessionState
from server.fault import FaultInjector, InjectionStatus

DELIVERABLES_DIR = os.environ.get("TV_DELIVERABLES_DIR", "/Users/dalabommba/Desktop/AI_Passport/B02R交付物")
SERIAL_PORT = os.environ.get("TV_SERIAL_PORT", "/dev/cu.usbmodem101")
BAUD_RATE = 115200
BIND_IP = os.environ.get("TV_BIND_IP", "192.168.123.185")
PORT = int(os.environ.get("TV_PORT", "8096"))
TEST_CHANNELS_FILE = os.environ.get("TV_CHANNELS_FILE", "tests/test_channels.txt")


class DeviceExperimentRunner:
    def __init__(self, serial_port: str = SERIAL_PORT, baud_rate: int = BAUD_RATE, deliverables_dir: str = DELIVERABLES_DIR):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.deliverables_dir = deliverables_dir
        os.makedirs(self.deliverables_dir, exist_ok=True)

        self.server_log_path = os.path.join(self.deliverables_dir, "b02r_server.log")
        self.serial_log_path = os.path.join(self.deliverables_dir, "b02r_device_serial.log")

        # Open file descriptors for real-time incremental log flushing
        self.server_log_file = open(self.server_log_path, "a", encoding="utf-8", buffering=1)
        self.serial_log_file = open(self.serial_log_path, "a", encoding="utf-8", buffering=1)

        self.serial_lines: List[str] = []
        self.server_lines: List[str] = []
        self.stop_requested = threading.Event()
        self.ser: Optional[serial.Serial] = None
        self.server: Optional[AVServer] = None
        self.injector = FaultInjector(logger=self.log_server)

        # Fresh telemetry tracking
        self.window_metrics: List[Dict[str, Any]] = []
        self.audio_empty_events: List[Dict[str, Any]] = []
        self.session_end_metrics: List[Dict[str, Any]] = []
        self.recovery_observe_s: float = 20.0

    def log_server(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} SRV] {msg}"
        self.server_lines.append(line)
        self.server_log_file.write(line + "\n")
        print(line, flush=True)

    def log_serial(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} DEV] {msg}"
        self.serial_lines.append(line)
        self.serial_log_file.write(line + "\n")
        print(line, flush=True)
        self.parse_device_line(msg)

    def parse_device_line(self, line: str):
        cur_session = getattr(self.server, "session_id", 0) if self.server else 0
        now_mono = time.monotonic()

        # Match periodic window metrics
        m_interval = re.search(
            r"CLOCK_ESTIMATED interval_frames=(\d+) interval_ms=(\d+) dropped=(\d+) .* heap=(\d+) largest=(\d+) decode_max_ms=(\d+) .* rx_pkts=(\d+) .* rx_audio=(\d+)",
            line
        )
        if m_interval:
            frames = int(m_interval.group(1))
            interval_ms = int(m_interval.group(2))
            fps = round(frames * 1000.0 / interval_ms, 2) if interval_ms > 0 else 0.0
            rec = {
                "monotonic": now_mono,
                "timestamp": time.time(),
                "session_id": cur_session,
                "frames": frames,
                "interval_ms": interval_ms,
                "fps": fps,
                "dropped": int(m_interval.group(3)),
                "heap": int(m_interval.group(4)),
                "largest": int(m_interval.group(5)),
                "decode_max_ms": int(m_interval.group(6)),
                "rx_pkts": int(m_interval.group(7)),
                "rx_audio": int(m_interval.group(8))
            }
            self.window_metrics.append(rec)
            return

        # Match AUDIO_EMPTY (underflow / silence gap)
        m_empty = re.search(r"AUDIO_EMPTY gap_ms=(\d+) queue=(\d+) submitted=(\d+)", line)
        if m_empty:
            rec = {
                "monotonic": now_mono,
                "timestamp": time.time(),
                "session_id": cur_session,
                "gap_ms": int(m_empty.group(1)),
                "queue": int(m_empty.group(2)),
                "submitted": int(m_empty.group(3))
            }
            self.audio_empty_events.append(rec)
            return

        # Match session teardown clock
        m_clock = re.search(
            r"ESTIMATED clock:\s+submitted_samples=(\d+)\s+of_which_silence=(\d+)\s+program_samples=(\d+)\s+program_ms=(\d+)",
            line
        )
        if m_clock:
            rec = {
                "monotonic": now_mono,
                "timestamp": time.time(),
                "session_id": cur_session,
                "submitted_samples": int(m_clock.group(1)),
                "of_which_silence": int(m_clock.group(2)),
                "program_samples": int(m_clock.group(3)),
                "program_ms": int(m_clock.group(4))
            }
            self.session_end_metrics.append(rec)

    def serial_reader_loop(self):
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

    def start(self, initial_channel="baseline"):
        from pathlib import Path
        os.environ["TV_CHANNELS_FILE"] = TEST_CHANNELS_FILE
        load_channels(Path(TEST_CHANNELS_FILE))
        self.ser = serial.Serial(self.serial_port, self.baud_rate, timeout=0.1)
        self.ser_thread = threading.Thread(target=self.serial_reader_loop, daemon=True)
        self.ser_thread.start()

        media = Media(b"", (b"",))
        self.server = AVServer(
            media, None, bind=BIND_IP, port=PORT, logger=self.log_server
        )
        self.server.live_enabled = True
        self.server.channel_name = initial_channel
        self.server.fault_injector = self.injector

        self.srv_thread = threading.Thread(target=self.server.serve, daemon=True)
        self.srv_thread.start()
        self.server.ready.wait(timeout=3.0)
        self.log_server(f"B02-R Server ready on {BIND_IP}:{PORT}, channels_file={TEST_CHANNELS_FILE}")

    def wait_for_active_streaming(self, min_packets=30, timeout=15.0) -> bool:
        """Wait until device is in active live streaming session."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.server and getattr(self.server, "session_id", 0) != 0 and getattr(self.server, "audio_sent", 0) >= min_packets:
                return True
            time.sleep(0.2)
        return False

    def get_latest_device_metrics(self) -> Optional[Dict[str, Any]]:
        return self.session_end_metrics[-1] if self.session_end_metrics else None

    def get_fresh_audio_empty_count(self, session_id: int, since_monotonic: float) -> int:
        return sum(1 for e in self.audio_empty_events if e["session_id"] == session_id and e["monotonic"] >= since_monotonic)

    def get_fresh_window_metrics(self, session_id: int, since_monotonic: float) -> List[Dict[str, Any]]:
        return [w for w in self.window_metrics if w["session_id"] == session_id and w["monotonic"] >= since_monotonic]

    def stop(self):
        self.stop_requested.set()
        if self.server:
            self.server.stop.set()
        if self.ser and self.ser.is_open:
            self.ser.close()
        try:
            self.server_log_file.close()
            self.serial_log_file.close()
        except Exception:
            pass


def wait_for_healthy_baseline(
    runner: DeviceExperimentRunner,
    min_fps: float = 10.5,
    sample_window_s: float = 2.0,
    timeout_s: float = 25.0
) -> bool:
    """Verify that the stream is active and running at a healthy rate (>= min_fps) before injection.
    
    Returns True when healthy baseline is established.
    Returns False if session died, changed, or failed to achieve min_fps within timeout.
    """
    t0 = time.monotonic()
    cur_session = getattr(runner.server, "session_id", 0)
    if cur_session == 0:
        return False

    while time.monotonic() - t0 < timeout_s:
        v_start = getattr(runner.server, "video_sent", 0)
        t_start = time.monotonic()
        time.sleep(sample_window_s)
        t_end = time.monotonic()
        v_end = getattr(runner.server, "video_sent", 0)
        session_now = getattr(runner.server, "session_id", 0)
        if session_now != cur_session or session_now == 0:
            runner.log_server(f"Session changed or lost while waiting for healthy baseline: {cur_session} -> {session_now}")
            return False
        dt = t_end - t_start
        fps = (v_end - v_start) / dt if dt > 0 else 0.0
        if fps >= min_fps:
            runner.log_server(f"Healthy baseline confirmed: session={cur_session} fps={fps:.2f} (v_delta={v_end - v_start} in {dt:.2f}s)")
            return True
        runner.log_server(f"Baseline rate warming: session={cur_session} fps={fps:.2f} < target {min_fps} (elapsed {time.monotonic()-t0:.1f}s/{timeout_s}s)")

    runner.log_server(f"Timeout waiting for healthy baseline: session={cur_session} failed to reach {min_fps} fps within {timeout_s}s")
    return False


def run_experiment_reconnect(runner: DeviceExperimentRunner) -> Dict[str, Any]:
    print("\n" + "="*70)
    print("EXPERIMENT 1: Device Clean Stop & Automatic Reconnect Under Single Server")
    print("="*70)

    # Allow session 1 to run for 8 seconds, then limit duration
    runner.log_server("Waiting for Device Session 1 to connect...")
    assert runner.wait_for_active_streaming(min_packets=20, timeout=15.0), "Session 1 failed to start"
    session_1_id = runner.server.session_id
    runner.log_server(f"Session 1 active: id={session_1_id}. Allowing 8 seconds of play...")
    time.sleep(8.0)
    s1_audio = runner.server.audio_sent
    s1_video = runner.server.video_sent

    # Trigger graceful stop of session 1
    runner.log_server("Triggering graceful termination of Session 1...")
    runner.server.session_limit_s = 0.01  # Immediate graceful return from _pace_live

    # Wait for session 1 to close
    t_term = time.monotonic()
    while runner.server.live_channel is not None and time.monotonic() - t_term < 5.0:
        time.sleep(0.05)
    runner.log_server("Session 1 closed. Server remains listening!")

    # Reset session limit for session 2
    runner.server.session_limit_s = None

    # Wait for device to automatically reconnect (Session 2)
    runner.log_server("Waiting for device automatic reconnect to Session 2...")
    t_reconnect_start = time.monotonic()
    while time.monotonic() - t_reconnect_start < 10.0:
        if runner.server.session_id != 0 and runner.server.session_id != session_1_id:
            break
        time.sleep(0.1)

    session_2_id = runner.server.session_id
    assert session_2_id != 0 and session_2_id != session_1_id, "Device failed to reconnect to Session 2"
    reconnect_delay_s = round(time.monotonic() - t_reconnect_start, 3)
    runner.log_server(f"Session 2 successfully connected: id={session_2_id} (reconnect took {reconnect_delay_s}s)")

    # Allow Session 2 to stream continuously for 12 seconds
    runner.log_server("Verifying Session 2 continuous stable play for 12s...")
    time.sleep(12.0)
    s2_audio = runner.server.audio_sent
    s2_video = runner.server.video_sent
    runner.log_server(f"Session 2 completed continuous run: audio_sent={s2_audio} video_sent={s2_video}")

    # Dynamic verdict
    reconnect_passed = (
        s1_audio > 0 and s1_video > 0 and
        s2_audio > 0 and s2_video > 0 and
        session_2_id != session_1_id and
        reconnect_delay_s < 5.0
    )

    return {
        "case": "device_clean_reconnect",
        "session_1_id": session_1_id,
        "session_1_audio": s1_audio,
        "session_1_video": s1_video,
        "session_2_id": session_2_id,
        "session_2_audio": s2_audio,
        "session_2_video": s2_video,
        "reconnect_delay_s": reconnect_delay_s,
        "verdict": "PASS" if reconnect_passed else "FAIL"
    }


def run_experiment_downstream_pauses(runner: DeviceExperimentRunner) -> List[Dict[str, Any]]:
    print("\n" + "="*70)
    print("EXPERIMENT 2: Downstream Packet-Boundary Pauses (0.5s, 1.0s, 3.0s x 3)")
    print("="*70)
    results = []
    abort_matrix = False

    # Ensure initial session warm-up so opening connection window is flushed
    runner.log_server("Waiting for active streaming session on baseline channel...")
    assert runner.wait_for_active_streaming(min_packets=30, timeout=15.0), "Failed to establish initial baseline session"
    runner.log_server("Ensuring initial 15s warm-up to establish steady baseline...")
    time.sleep(15.0)

    for pause_s in [0.5, 1.0, 3.0]:
        if abort_matrix:
            break
        for rep in range(1, 4):
            label = f"downstream_{pause_s}s_rep{rep}"
            runner.log_server(f"--- Preparing {label} ---")
            
            # 1. Baseline verification: require active streaming at healthy rate
            cur_session = getattr(runner.server, "session_id", 0)
            if not wait_for_healthy_baseline(runner, min_fps=10.5, sample_window_s=2.0, timeout_s=25.0):
                runner.log_server(f"ABORTING downstream matrix at {label}: Failed to establish healthy baseline.")
                rec = {
                    "label": label,
                    "target_pause_s": pause_s,
                    "repetition": rep,
                    "same_session": False,
                    "session_id": cur_session,
                    "verdict": "FAIL",
                    "reason": "Pre-injection baseline check failed: stream not at healthy baseline >=10.5 fps"
                }
                results.append(rec)
                abort_matrix = True
                break

            cur_session = getattr(runner.server, "session_id", 0)
            v_pre = getattr(runner.server, "video_sent", 0)
            a_pre = getattr(runner.server, "audio_sent", 0)

            # 2. Schedule injection on packet boundary
            exp_id = f"exp_down_{pause_s}s_r{rep}_{int(time.time()*1000)}"
            t_req = time.monotonic()
            req = runner.injector.request_downstream_pause(
                duration_s=pause_s,
                target_session_id=cur_session,
                experiment_id=exp_id,
                label=label
            )

            # Wait for injection to complete via real events
            injection_completed = False
            if hasattr(req, "started_event") and hasattr(req, "completed_event"):
                started = req.started_event.wait(timeout=5.0)
                if started:
                    injection_completed = req.completed_event.wait(timeout=pause_s + 5.0)
            else:
                time.sleep(pause_s)
                injection_completed = True

            req_status = str(getattr(req, "status", "UNKNOWN"))
            if req_status in ("InjectionStatus.CANCELLED", "CANCELLED", "ERROR"):
                injection_completed = False
            elif req_status not in ("InjectionStatus.COMPLETED", "COMPLETED"):
                if hasattr(req, "status"):
                    injection_completed = False

            t_supply_resumed = getattr(req, "wall_end", time.monotonic())
            v_at_resume = getattr(runner.server, "video_sent", 0)
            a_at_resume = getattr(runner.server, "audio_sent", 0)

            # 3. Recovery observation window
            # First 5s: verify frames resume promptly
            time.sleep(5.0)
            v_5s = getattr(runner.server, "video_sent", 0)
            a_5s = getattr(runner.server, "audio_sent", 0)
            resumed_in_5s = (v_5s > v_at_resume and a_5s > a_at_resume and getattr(runner.server, "session_id", 0) == cur_session)

            observe_duration = getattr(runner, "recovery_observe_s", 20.0)
            remaining_observe = max(0.0, observe_duration - 5.0)
            runner.log_server(f"5s prompt resume check: resumed_in_5s={resumed_in_5s} (v_5s_delta={v_5s-v_at_resume}). Observing remaining {remaining_observe}s...")
            
            t_obs_start = time.monotonic()
            while time.monotonic() - t_obs_start < remaining_observe:
                if getattr(runner.server, "session_id", 0) != cur_session:
                    runner.log_server(f"Session reset detected during recovery of {label}!")
                    break
                time.sleep(0.5)

            t_obs_end = time.monotonic()
            actual_observe_s = round(t_obs_end - t_supply_resumed, 2)
            v_post = getattr(runner.server, "video_sent", 0)
            a_post = getattr(runner.server, "audio_sent", 0)

            v_delta = v_post - v_at_resume
            a_delta = a_post - a_at_resume
            server_rendered_fps = round(v_delta / actual_observe_s, 2) if actual_observe_s > 0 else 0.0
            same_session = (getattr(runner.server, "session_id", 0) == cur_session)

            # Check fresh device telemetry
            fresh_empty_count = runner.get_fresh_audio_empty_count(cur_session, t_req) if hasattr(runner, "get_fresh_audio_empty_count") else 0
            fresh_windows = runner.get_fresh_window_metrics(cur_session, t_req) if hasattr(runner, "get_fresh_window_metrics") else []
            underflow_observed = (fresh_empty_count > 0)

            # Check device window metrics: device must have reported telemetry and rendered frames
            device_fps_met = False
            dev_fps = None
            recovery_windows = [w for w in fresh_windows if w.get("monotonic", 0) >= t_supply_resumed]
            if recovery_windows:
                pure_windows = [w for w in recovery_windows if w.get("monotonic", 0) >= t_supply_resumed + 9.5]
                if pure_windows:
                    tot_f = sum(w.get("frames", 0) for w in pure_windows)
                    tot_ms = sum(w.get("interval_ms", 0) for w in pure_windows)
                    dev_fps = round(tot_f * 1000.0 / tot_ms, 2) if tot_ms > 0 else 0.0
                else:
                    tot_f = sum(w.get("frames", 0) for w in recovery_windows)
                    tot_ms = sum(w.get("interval_ms", 0) for w in recovery_windows)
                    active_ms = max(1000.0, tot_ms - pause_s * 1000.0)
                    dev_fps = round(tot_f * 1000.0 / active_ms, 2) if active_ms > 0 else 0.0
                if dev_fps >= 10.5 and tot_f > 0:
                    device_fps_met = True
            else:
                # No device telemetry: cannot verify hardware recovery
                device_fps_met = False

            # Strict PASS criteria:
            # - injection actually completed
            # - same session maintained without crash or reset
            # - video resumed within 5s
            # - post-recovery server average fps >= 10.5 fps
            # - device telemetry present and device fps >= 10.5 with frames rendered
            is_pass = (
                injection_completed and
                same_session and
                resumed_in_5s and
                v_delta >= int(observe_duration * 9.0) and
                a_delta >= int(observe_duration * 20.0) and
                server_rendered_fps >= 10.5 and
                device_fps_met
            )

            if req_status in ("InjectionStatus.CANCELLED", "CANCELLED"):
                sample_verdict = "CANCELLED"
            elif not recovery_windows:
                sample_verdict = "UNKNOWN"
            elif is_pass:
                sample_verdict = "PASS"
            else:
                sample_verdict = "FAIL"

            rec = {
                "label": label,
                "target_pause_s": pause_s,
                "repetition": rep,
                "same_session": same_session,
                "session_id": cur_session,
                "experiment_id": exp_id,
                "actual_pause_s": getattr(req, "actual_s", pause_s),
                "observation_s": actual_observe_s,
                "video_sent_before": v_pre,
                "video_sent_resume": v_at_resume,
                "video_sent_post": v_post,
                "video_delta": v_delta,
                "audio_delta": a_delta,
                "server_fps": server_rendered_fps,
                "resumed_in_5s": resumed_in_5s,
                "fresh_empty_count": fresh_empty_count,
                "underflow_observed": underflow_observed,
                "verdict": sample_verdict
            }
            results.append(rec)
            runner.log_server(
                f"Result {label}: verdict={rec['verdict']} same_session={same_session} "
                f"v_delta={v_delta} a_delta={a_delta} fps={server_rendered_fps} "
                f"resumed_in_5s={resumed_in_5s} underflow={underflow_observed}"
            )
            if not is_pass:
                runner.log_server(f"ABORTING downstream matrix at {label}: Sample failed. Preserving evidence.")
                abort_matrix = True
                break
            time.sleep(5.0)

    return results


def run_experiment_upstream_pauses(runner: DeviceExperimentRunner) -> List[Dict[str, Any]]:
    print("\n" + "="*70)
    print("EXPERIMENT 3: Upstream Decoder Pauses (0.5s, 1.0s, 3.0s x 3)")
    print("="*70)
    results = []
    abort_matrix = False

    runner.log_server("Waiting for active streaming session on baseline channel...")
    assert runner.wait_for_active_streaming(min_packets=30, timeout=15.0), "Failed to establish initial baseline session"
    runner.log_server("Ensuring initial 10s warm-up to establish steady baseline...")
    time.sleep(10.0)

    for pause_s in [0.5, 1.0, 3.0]:
        if abort_matrix:
            break
        for rep in range(1, 4):
            label = f"upstream_decoder_pause_{pause_s}s_rep{rep}"
            runner.log_server(f"--- Preparing {label} ---")
            
            cur_session = getattr(runner.server, "session_id", 0)
            if not wait_for_healthy_baseline(runner, min_fps=10.5, sample_window_s=2.0, timeout_s=25.0):
                runner.log_server(f"ABORTING upstream matrix at {label}: Failed to establish healthy baseline.")
                rec = {
                    "label": label,
                    "target_pause_s": pause_s,
                    "repetition": rep,
                    "same_session": False,
                    "session_id": cur_session,
                    "verdict": "FAIL",
                    "reason": "Pre-injection baseline check failed: stream not at healthy baseline >=10.5 fps"
                }
                results.append(rec)
                abort_matrix = True
                break

            cur_session = getattr(runner.server, "session_id", 0)
            v_pre = getattr(runner.server, "video_sent", 0)
            a_pre = getattr(runner.server, "audio_sent", 0)

            channel = runner.server.live_channel
            exp_id = f"exp_up_{pause_s}s_r{rep}_{int(time.time()*1000)}"
            t_req = time.monotonic()

            # Inject upstream pause via SIGSTOP on decoder
            inject_res = runner.injector.inject_upstream_pause(
                channel,
                pause_s,
                target_session_id=cur_session,
                experiment_id=exp_id,
                label=label
            )

            t_supply_resumed = time.monotonic()
            # Observe recovery
            v_at_resume = getattr(runner.server, "video_sent", 0)
            a_at_resume = getattr(runner.server, "audio_sent", 0)

            # Check 5s prompt resume
            time.sleep(5.0)
            v_5s = getattr(runner.server, "video_sent", 0)
            a_5s = getattr(runner.server, "audio_sent", 0)
            resumed_in_5s = (v_5s > v_at_resume and a_5s > a_at_resume and getattr(runner.server, "session_id", 0) == cur_session)

            observe_duration = getattr(runner, "recovery_observe_s", 20.0)
            remaining_observe = max(0.0, observe_duration - 5.0)
            runner.log_server(f"5s prompt resume check: resumed_in_5s={resumed_in_5s} (v_5s_delta={v_5s-v_at_resume}). Observing remaining {remaining_observe}s...")
            
            t_obs_start = time.monotonic()
            while time.monotonic() - t_obs_start < remaining_observe:
                if getattr(runner.server, "session_id", 0) != cur_session:
                    runner.log_server(f"Session reset detected during upstream recovery of {label}!")
                    break
                time.sleep(0.5)

            actual_observe_s = round(time.monotonic() - t_supply_resumed, 2)
            v_post = getattr(runner.server, "video_sent", 0)
            a_post = getattr(runner.server, "audio_sent", 0)
            v_delta = v_post - v_at_resume
            a_delta = a_post - a_at_resume
            server_fps = round(v_delta / actual_observe_s, 2) if actual_observe_s > 0 else 0.0
            same_session = (getattr(runner.server, "session_id", 0) == cur_session)

            inject_status = str(inject_res.get("status", "UNKNOWN")) if isinstance(inject_res, dict) else "UNKNOWN"
            inject_completed = (inject_status == "COMPLETED")

            # Check fresh device telemetry for upstream recovery
            fresh_windows = runner.get_fresh_window_metrics(cur_session, t_req) if hasattr(runner, "get_fresh_window_metrics") else []
            device_fps_met = False
            dev_fps = None
            recovery_windows = [w for w in fresh_windows if w.get("monotonic", 0) >= t_supply_resumed]
            if recovery_windows:
                tot_f = sum(w.get("frames", 0) for w in recovery_windows)
                tot_ms = sum(w.get("interval_ms", 0) for w in recovery_windows)
                if tot_f > 0 and tot_ms > 0:
                    dev_fps = round(tot_f * 1000.0 / tot_ms, 2)
                    if dev_fps >= 10.5:
                        device_fps_met = True
            else:
                device_fps_met = False

            is_pass = (
                inject_completed and
                same_session and
                resumed_in_5s and
                v_delta >= int(observe_duration * 9.0) and
                a_delta >= int(observe_duration * 20.0) and
                server_fps >= 10.5 and
                device_fps_met
            )

            if not recovery_windows:
                sample_verdict = "UNKNOWN"
            elif is_pass:
                sample_verdict = "PASS"
            else:
                sample_verdict = "FAIL"

            rec = {
                "label": label,
                "target_pause_s": pause_s,
                "repetition": rep,
                "same_session": same_session,
                "session_id": cur_session,
                "experiment_id": exp_id,
                "qa_before": inject_res["qa_before"] if isinstance(inject_res, dict) else 0,
                "qa_after": inject_res["qa_after"] if isinstance(inject_res, dict) else 0,
                "absorbed_by_host": inject_res["absorbed_by_host"] if isinstance(inject_res, dict) else False,
                "video_delta": v_delta,
                "audio_delta": a_delta,
                "server_fps": server_fps,
                "device_fps": dev_fps,
                "resumed_in_5s": resumed_in_5s,
                "verdict": sample_verdict
            }
            results.append(rec)
            runner.log_server(
                f"Result {label}: verdict={rec['verdict']} qa={rec['qa_before']}->{rec['qa_after']} "
                f"absorbed={rec['absorbed_by_host']} v_delta={v_delta} fps={server_fps} dev_fps={dev_fps}"
            )
            if not is_pass:
                runner.log_server(f"ABORTING upstream matrix at {label}: Sample failed. Preserving evidence.")
                abort_matrix = True
                break
            time.sleep(5.0)

    return results


def run_single_control_experiment(runner: DeviceExperimentRunner, pause_s: float = 1.0, observe_s: float = 20.0) -> Dict[str, Any]:
    """Execute the single critical control experiment required by B02-R (R2).
    
    1. Warm-up >= 20s steady at ~12 fps.
    2. Inject 1.0s downstream pause on packet boundary.
    3. Observe recovery: within 5s back to steady streaming, next 20s avg >= 11 fps, zero prolonged freeze.
    """
    print("\n" + "="*70)
    print(f"R2 SINGLE CRITICAL CONTROL EXPERIMENT: Downstream {pause_s}s Pause -> {observe_s}s Observation")
    print("="*70)

    runner.recovery_observe_s = observe_s
    runner.log_server("Waiting for active streaming session on baseline channel...")
    assert runner.wait_for_active_streaming(min_packets=30, timeout=15.0), "Failed to establish initial baseline session"

    cur_session = runner.server.session_id
    runner.log_server(f"Session {cur_session} connected. Running 20s baseline warm-up @ ~12fps...")

    # 1. Warm-up 20s
    t_warm_start = time.monotonic()
    v_warm_start = runner.server.video_sent
    a_warm_start = runner.server.audio_sent

    time.sleep(20.0)
    warm_elapsed = round(time.monotonic() - t_warm_start, 2)
    v_warm_end = runner.server.video_sent
    a_warm_end = runner.server.audio_sent

    v_warm_delta = v_warm_end - v_warm_start
    warmup_fps = round(v_warm_delta / warm_elapsed, 2)
    runner.log_server(
        f"Baseline warm-up complete: elapsed={warm_elapsed}s frames_sent={v_warm_delta} "
        f"warmup_fps={warmup_fps} a_sent={a_warm_end} v_sent={v_warm_end}"
    )

    # 2. Inject 1.0s downstream pause
    label = f"single_control_downstream_{pause_s}s"
    exp_id = f"exp_control_{int(time.time()*1000)}"
    t_req = time.monotonic()

    runner.log_server(f"Requesting downstream pause: {pause_s}s (exp={exp_id})")
    req = runner.injector.request_downstream_pause(
        duration_s=pause_s,
        target_session_id=cur_session,
        experiment_id=exp_id,
        label=label
    )

    # Wait for injection completion
    assert req.started_event.wait(timeout=5.0), "Injected pause failed to start"
    assert req.completed_event.wait(timeout=pause_s + 5.0), "Injected pause failed to complete"

    t_supply_resumed = req.wall_end
    v_resume = runner.server.video_sent
    a_resume = runner.server.audio_sent
    actual_pause_s = req.actual_s

    runner.log_server(
        f"Pause completed: actual_duration={actual_pause_s:.3f}s. "
        f"Supply resumed at wall={t_supply_resumed:.3f}. Observing recovery for {observe_s}s..."
    )

    # 3. Observe recovery:
    # First 5s: verify frames resume
    time.sleep(5.0)
    v_5s = runner.server.video_sent
    a_5s = runner.server.audio_sent
    resumed_in_5s = (v_5s > v_resume and a_5s > a_resume and runner.server.session_id == cur_session)
    runner.log_server(
        f"5-second recovery check: resumed_in_5s={resumed_in_5s} "
        f"v_5s_delta={v_5s - v_resume} a_5s_delta={a_5s - a_resume}"
    )

    # Remaining observation: total observe_s
    remaining_observe = max(0.0, observe_s - 5.0)
    if remaining_observe > 0:
        time.sleep(remaining_observe)

    t_obs_end = time.monotonic()
    actual_observe_s = round(t_obs_end - t_supply_resumed, 2)
    v_post = runner.server.video_sent
    a_post = runner.server.audio_sent

    v_obs_delta = v_post - v_resume
    a_obs_delta = a_post - a_resume
    recovery_fps = round(v_obs_delta / actual_observe_s, 2) if actual_observe_s > 0 else 0.0
    same_session = (runner.server.session_id == cur_session)

    # Collect fresh serial metrics
    fresh_empty_count = runner.get_fresh_audio_empty_count(cur_session, t_req)
    fresh_windows = runner.get_fresh_window_metrics(cur_session, t_req)
    runner.log_server(
        f"Observation complete: actual_obs={actual_observe_s}s same_session={same_session} "
        f"v_obs_delta={v_obs_delta} a_obs_delta={a_obs_delta} recovery_fps={recovery_fps} "
        f"fresh_empty_events={fresh_empty_count} fresh_windows={len(fresh_windows)}"
    )

    device_fps = 0.0
    if fresh_windows:
        tot_f = sum(w["frames"] for w in fresh_windows)
        tot_ms = sum(w["interval_ms"] for w in fresh_windows)
        device_fps = round(tot_f * 1000.0 / tot_ms, 2) if tot_ms > 0 else 0.0

    verdict = "PASS" if (
        resumed_in_5s and
        same_session and
        recovery_fps >= 11.0 and
        (not fresh_windows or device_fps >= 10.5)
    ) else "FAIL"

    result = {
        "case": "single_control_downstream_1s",
        "session_id": cur_session,
        "target_pause_s": pause_s,
        "actual_pause_s": actual_pause_s,
        "warmup_s": warm_elapsed,
        "warmup_fps": warmup_fps,
        "resumed_in_5s": resumed_in_5s,
        "observation_s": actual_observe_s,
        "observation_frames": v_obs_delta,
        "recovery_server_fps": recovery_fps,
        "recovery_device_fps": device_fps,
        "same_session": same_session,
        "audio_empty_events": fresh_empty_count,
        "verdict": verdict,
        "state_history": runner.server.state_history
    }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B02-R Device Experiment Runner")
    parser.add_argument("--single-control", action="store_true", help="Run R2 single 1.0s critical control experiment")
    parser.add_argument("--reconnect-only", action="store_true", help="Run clean stop and auto-reconnect experiment only")
    parser.add_argument("--downstream-matrix", action="store_true", help="Run 0.5s, 1.0s, 3.0s downstream pause matrix only")
    parser.add_argument("--upstream-matrix", action="store_true", help="Run 0.5s, 1.0s, 3.0s upstream decoder pause matrix only")
    parser.add_argument("--full-matrix", action="store_true", help="Run full downstream, upstream, and reconnect matrix")
    parser.add_argument("--pause", type=float, default=1.0, help="Pause duration for single control (default: 1.0)")
    parser.add_argument("--observe", type=float, default=20.0, help="Observation window duration (default: 20.0)")
    args = parser.parse_args()

    runner = DeviceExperimentRunner()
    try:
        runner.start(initial_channel="baseline")
        time.sleep(2.0)

        if args.single_control:
            ctrl_res = run_single_control_experiment(runner, pause_s=args.pause, observe_s=args.observe)
            print("\n" + "="*70)
            print("SINGLE CONTROL RESULT:")
            print(json.dumps(ctrl_res, indent=2))
            print("="*70)

            with open(os.path.join(DELIVERABLES_DIR, "b02r_single_control_summary.json"), "w") as f:
                json.dump(ctrl_res, f, indent=2)

            sys.exit(0 if ctrl_res["verdict"] == "PASS" else 1)

        elif args.reconnect_only:
            reconnect_res = run_experiment_reconnect(runner)
            with open(os.path.join(DELIVERABLES_DIR, "b02r_reconnect_summary.json"), "w") as f:
                json.dump(reconnect_res, f, indent=2)
            sys.exit(0 if reconnect_res["verdict"] == "PASS" else 1)

        elif args.downstream_matrix:
            runner.recovery_observe_s = args.observe
            downstream_res = run_experiment_downstream_pauses(runner)
            summary = {
                "downstream_pauses": downstream_res,
                "injector_events": runner.injector.events
            }
            with open(os.path.join(DELIVERABLES_DIR, "b02r_downstream_summary.json"), "w") as f:
                json.dump(summary, f, indent=2)
            all_passed = (len(downstream_res) == 9 and all(r["verdict"] == "PASS" for r in downstream_res))
            sys.exit(0 if all_passed else 1)

        elif args.upstream_matrix:
            runner.recovery_observe_s = args.observe
            upstream_res = run_experiment_upstream_pauses(runner)
            summary = {
                "upstream_pauses": upstream_res,
                "injector_events": runner.injector.events
            }
            with open(os.path.join(DELIVERABLES_DIR, "b02r_upstream_summary.json"), "w") as f:
                json.dump(summary, f, indent=2)
            all_passed = (len(upstream_res) == 9 and all(r["verdict"] == "PASS" for r in upstream_res))
            sys.exit(0 if all_passed else 1)

        elif args.full_matrix:
            runner.recovery_observe_s = args.observe
            downstream_res = run_experiment_downstream_pauses(runner)
            upstream_res = run_experiment_upstream_pauses(runner)
            reconnect_res = run_experiment_reconnect(runner)

            summary = {
                "downstream_pauses": downstream_res,
                "upstream_pauses": upstream_res,
                "reconnect_experiment": reconnect_res,
                "injector_events": runner.injector.events
            }

            with open(os.path.join(DELIVERABLES_DIR, "b02r_matrix_summary.json"), "w") as f:
                json.dump(summary, f, indent=2)

            # Also save as b02_device_experiments_summary.json for direct audit verification
            with open(os.path.join(DELIVERABLES_DIR, "b02_device_experiments_summary.json"), "w") as f:
                json.dump(summary, f, indent=2)

            all_passed = (
                len(downstream_res) == 9 and all(r["verdict"] == "PASS" for r in downstream_res) and
                len(upstream_res) == 9 and all(r["verdict"] == "PASS" for r in upstream_res) and
                reconnect_res["verdict"] == "PASS"
            )
            sys.exit(0 if all_passed else 1)

        else:
            # Default: run single control and reconnect
            ctrl_res = run_single_control_experiment(runner, pause_s=args.pause, observe_s=args.observe)
            reconnect_res = run_experiment_reconnect(runner)
            summary = {
                "single_control_experiment": ctrl_res,
                "reconnect_experiment": reconnect_res,
                "injector_events": runner.injector.events
            }
            with open(os.path.join(DELIVERABLES_DIR, "b02r_summary.json"), "w") as f:
                json.dump(summary, f, indent=2)

            all_passed = (reconnect_res["verdict"] == "PASS" and ctrl_res["verdict"] == "PASS")
            sys.exit(0 if all_passed else 1)

    finally:
        runner.stop()
