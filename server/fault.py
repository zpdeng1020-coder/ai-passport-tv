"""Fault injection harness for TV server controlled experiments (B02-R).

Enables packet-boundary downstream pauses and upstream transcode pauses
without corrupting TCP streams or violating protocol contracts.
Binds every injection request to experiment_id and session_id with
explicit lifecycle tracking (REQUESTED, STARTED, COMPLETED, CANCELLED).
"""
import dataclasses
import enum
import os
import select
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class InjectionStatus(str, enum.Enum):
    REQUESTED = "REQUESTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclasses.dataclass
class PauseRequest:
    experiment_id: str
    target_session_id: int
    duration_s: float
    label: str
    status: InjectionStatus = InjectionStatus.REQUESTED
    created_at: float = dataclasses.field(default_factory=time.monotonic)
    wall_start: float = 0.0
    wall_end: float = 0.0
    actual_s: float = 0.0
    started_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    completed_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    error: Optional[str] = None
    qa_before: int = 0
    qa_after: int = 0
    qv_before: int = 0
    qv_after: int = 0
    audio_sent_before: int = 0
    video_sent_before: int = 0


class FaultInjector:
    """Manages controlled pause injection on valid FAV1 packet boundaries."""

    def __init__(self, logger: Optional[Callable[[str], None]] = None):
        self.logger = logger or (lambda msg: None)
        self.lock = threading.Lock()
        self._pending_downstream_pause: Optional[PauseRequest] = None
        self.events: List[Dict[str, Any]] = []

    def request_downstream_pause(
        self,
        duration_s: float,
        target_session_id: int,
        experiment_id: str = "",
        label: str = ""
    ) -> PauseRequest:
        """Schedule a pause before the next complete packet is started."""
        with self.lock:
            # If an unstarted request exists, cancel it first
            if self._pending_downstream_pause and self._pending_downstream_pause.status == InjectionStatus.REQUESTED:
                old = self._pending_downstream_pause
                old.status = InjectionStatus.CANCELLED
                old.error = "Superseded by new injection request"
                old.completed_event.set()
                self.logger(f"[INJECTOR] Superseded pending pause {old.label} (exp={old.experiment_id})")

            exp_id = experiment_id or f"exp_{int(time.time() * 1000)}"
            lbl = label or f"downstream_pause_{duration_s}s"
            req = PauseRequest(
                experiment_id=exp_id,
                target_session_id=target_session_id,
                duration_s=duration_s,
                label=lbl
            )
            self._pending_downstream_pause = req
            self.logger(
                f"[INJECTOR] Scheduled downstream pause: {duration_s}s ({lbl}) "
                f"exp={exp_id} target_session={target_session_id}"
            )
            return req

    def cancel_pending(self, current_session: Optional[int] = None, reason: str = "") -> None:
        """Cancel any pending unstarted request whose session doesn't match or on session teardown."""
        with self.lock:
            if self._pending_downstream_pause and self._pending_downstream_pause.status == InjectionStatus.REQUESTED:
                req = self._pending_downstream_pause
                if current_session is None or req.target_session_id != current_session:
                    req.status = InjectionStatus.CANCELLED
                    req.error = reason or f"Cancelled due to session change (target={req.target_session_id}, current={current_session})"
                    req.completed_event.set()
                    self.logger(f"[INJECTOR] Cancelled pause {req.label} (exp={req.experiment_id}): {req.error}")
                    self._pending_downstream_pause = None

    def check_pause(self, connection: Any, session: int, server: Any) -> Tuple[bool, float]:
        """Called at packet boundary in _pace_live.
        
        Returns:
            (continue_session: bool, actual_pause_s: float)
            If continue_session is False, device left or server stopped; caller must terminate session.
        """
        pause_req: Optional[PauseRequest] = None
        with self.lock:
            if self._pending_downstream_pause is not None:
                if self._pending_downstream_pause.target_session_id != session:
                    # Target session does not match current session! Cancel immediately.
                    mismatch_req = self._pending_downstream_pause
                    mismatch_req.status = InjectionStatus.CANCELLED
                    mismatch_req.error = f"Session mismatch: requested for session {mismatch_req.target_session_id}, but current session is {session}"
                    mismatch_req.completed_event.set()
                    self.logger(f"[INJECTOR] Rejecting pause {mismatch_req.label}: {mismatch_req.error}")
                    self._pending_downstream_pause = None
                    return True, 0.0
                pause_req = self._pending_downstream_pause
                self._pending_downstream_pause = None

        if pause_req is None:
            return True, 0.0

        t_start = time.monotonic()
        pause_req.status = InjectionStatus.STARTED
        pause_req.wall_start = t_start
        pause_req.started_event.set()

        channel = getattr(server, "live_channel", None)
        qa_before = len(channel.audio) if channel else 0
        qv_before = len(channel.video) if channel else 0
        audio_sent_before = getattr(server, "audio_sent", 0)
        video_sent_before = getattr(server, "video_sent", 0)
        pause_req.qa_before = qa_before
        pause_req.qv_before = qv_before
        pause_req.audio_sent_before = audio_sent_before
        pause_req.video_sent_before = video_sent_before

        duration_s = pause_req.duration_s
        label = pause_req.label
        exp_id = pause_req.experiment_id

        server.logger(
            f"session={session} INJECT_PAUSE_START exp={exp_id} label={label} target={duration_s:.3f}s "
            f"wall={t_start:.3f} qa={qa_before} qv={qv_before} "
            f"a_sent={audio_sent_before} v_sent={video_sent_before}"
        )

        device_left = False
        while not server.stop.is_set():
            now = time.monotonic()
            elapsed = now - t_start
            if elapsed >= duration_s:
                break
            remaining = duration_s - elapsed
            readable = select.select([connection], [], [], min(0.02, remaining))[0]
            if connection in readable:
                server.phase = "control_receive"
                if server._device_left(connection, session):
                    server.logger(f"session={session} device departed during injected pause {label}")
                    device_left = True
                    break

        t_end = time.monotonic()
        actual_duration = t_end - t_start
        pause_req.wall_end = t_end
        pause_req.actual_s = round(actual_duration, 4)
        qa_after = len(channel.audio) if channel else 0
        qv_after = len(channel.video) if channel else 0
        pause_req.qa_after = qa_after
        pause_req.qv_after = qv_after

        if device_left or server.stop.is_set():
            pause_req.status = InjectionStatus.CANCELLED
            pause_req.error = "Device departed or server stopped during pause"
            pause_req.completed_event.set()
            event_record = {
                "type": "downstream_pause",
                "experiment_id": exp_id,
                "label": label,
                "status": "CANCELLED",
                "error": pause_req.error,
                "target_s": duration_s,
                "actual_s": pause_req.actual_s,
                "wall_start": round(t_start, 4),
                "wall_end": round(t_end, 4),
                "session": session,
                "qa_before": qa_before,
                "qa_after": qa_after,
                "qv_before": qv_before,
                "qv_after": qv_after,
                "audio_sent_before": audio_sent_before,
                "video_sent_before": video_sent_before,
            }
            with self.lock:
                self.events.append(event_record)
            return False, 0.0

        pause_req.status = InjectionStatus.COMPLETED
        pause_req.completed_event.set()

        event_record = {
            "type": "downstream_pause",
            "experiment_id": exp_id,
            "label": label,
            "status": "COMPLETED",
            "target_s": duration_s,
            "actual_s": pause_req.actual_s,
            "wall_start": round(t_start, 4),
            "wall_end": round(t_end, 4),
            "session": session,
            "qa_before": qa_before,
            "qa_after": qa_after,
            "qv_before": qv_before,
            "qv_after": qv_after,
            "audio_sent_before": audio_sent_before,
            "video_sent_before": video_sent_before,
        }
        with self.lock:
            self.events.append(event_record)

        server.logger(
            f"session={session} INJECT_PAUSE_END exp={exp_id} label={label} actual={actual_duration:.3f}s "
            f"wall={t_end:.3f} qa={qa_after} qv={qv_after}"
        )
        return True, actual_duration

    def inject_upstream_pause(
        self,
        channel: Any,
        duration_s: float,
        target_session_id: int = 0,
        experiment_id: str = "",
        label: str = ""
    ) -> Dict[str, Any]:
        """Suspend upstream FFmpeg process using SIGSTOP/SIGCONT and measure host buffer absorption."""
        if not channel or not channel.decoder:
            raise RuntimeError("No active decoder process to pause")

        pid = channel.decoder.pid
        exp_id = experiment_id or f"exp_up_{int(time.time() * 1000)}"
        lbl = label or f"upstream_pause_{duration_s}s"
        t_start = time.monotonic()
        qa_before = len(channel.audio)
        qv_before = len(channel.video)

        self.logger(
            f"[INJECTOR] Upstream pause start: pid={pid} exp={exp_id} target={duration_s}s "
            f"session={target_session_id} qa={qa_before} qv={qv_before}"
        )
        os.kill(pid, signal.SIGSTOP)
        try:
            time.sleep(duration_s)
        finally:
            os.kill(pid, signal.SIGCONT)

        t_end = time.monotonic()
        actual_duration = t_end - t_start
        qa_after = len(channel.audio)
        qv_after = len(channel.video)

        rec = {
            "type": "upstream_pause",
            "experiment_id": exp_id,
            "label": lbl,
            "status": "COMPLETED",
            "session": target_session_id,
            "pid": pid,
            "target_s": duration_s,
            "actual_s": round(actual_duration, 4),
            "wall_start": round(t_start, 4),
            "wall_end": round(t_end, 4),
            "qa_before": qa_before,
            "qa_after": qa_after,
            "qv_before": qv_before,
            "qv_after": qv_after,
            "qa_delta": qa_after - qa_before,
            "qv_delta": qv_after - qv_before,
            "absorbed_by_host": (qa_after > 0)
        }
        with self.lock:
            self.events.append(rec)

        self.logger(
            f"[INJECTOR] Upstream pause end: pid={pid} exp={exp_id} actual={actual_duration:.3f}s "
            f"qa={qa_before}->{qa_after} qv={qv_before}->{qv_after} absorbed={rec['absorbed_by_host']}"
        )
        return rec
