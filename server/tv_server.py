"""Single-client LAN server; standard library only, no runtime transcoding."""

from __future__ import annotations

import argparse
import errno
import hmac
import ipaddress
import os
import secrets
import select
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import enum
from typing import Any, Dict, List, Optional, Tuple

class SessionState(str, enum.Enum):
    PLAYING = "PLAYING"
    STARVED_REBUFFERING = "STARVED_REBUFFERING"
    RECOVERING = "RECOVERING"
    CLOSED = "CLOSED"

from . import frames, netident
from .timeline import SourceState
from .rate import ADAPTIVE, FixedRate, RateController
from .live import (CHANNELS, CHANNEL_AGENTS, DEFAULT_CHANNEL, LiveChannel, LiveError,
                   AUDIO_MAX_LOOKAHEAD_MS,
                   AUDIO_LATE_RESET_MS, PREBUFFER_SECONDS, PREBUFFER_TIMEOUT_S,
                   VIDEO_QUEUE_SECONDS,
                   VIDEO_LATE_DROP_MS, channel_list)
# Timing constants come from media.py, which both senders share: live.py imports
# them from there too, so re-exporting them from live would invite the same
# copy-that-drifts problem the leads already suffered from.
from .media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, DURATION_MS, FPS, HEIGHT,
                    START_DELAY_MS, VIDEO_LEAD_MS, WIDTH, Media, import_video,
                    prepare, schedule)
from .protocol import (AUDIO_BYTES, IO_TIMEOUT, VIDEO_CONTINUES, Kind, Packet,
                       ProtocolError, json_bytes, json_object, receive_packet,
                       send_packet, _write_slice)

# The name of the environment variable an operator can set instead of
# --token-file. Renamed with everything else on this side: it is the server's
# own setting, not a name shared with the device. The firmware has a
# compile-time constant of the same spelling in main/av_config.h, but only the
# token's VALUE has to agree between them; the two names are free to differ, and
# having them differ is less misleading than suggesting one is the other.
TOKEN_ENV = "TV_PAIRING_TOKEN"
# Every value here is the constant the sender actually paces by, never a copy of
# it. A hand-copied "video_lead_ms": 50 stayed behind after the leads were
# unified, so the device was told a timing that no longer matched the sender.
CONFIG = {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": FPS,
          "sample_rate": 16000,
          "channels": 1, "sample_bits": 16, "audio_chunk_ms": AUDIO_CHUNK_MS,
          "video_max_bytes": frames.VIDEO_MAX,
          # The two values the device checks its own geometry against. Sending
          # them means a server and a firmware that disagree refuse to talk,
          # rather than drawing stripes at the wrong rows.
          "stripe_rows": frames.STRIPE_ROWS,
          "duration_ms": DURATION_MS,
          "start_delay_ms": START_DELAY_MS, "audio_lead_ms": AUDIO_LEAD_MS,
          "video_lead_ms": VIDEO_LEAD_MS}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))

# The slowest this is allowed to assume the link can be, used only to work out
# how long a frame may take before the sender gives up on it. It is a bound on
# failing, not a schedule: pacing is the scheduler's job, and it already drops a
# frame whose slot has passed.
#
# A third of what the device was measured taking, which is about 98 kB/s with
# the picture and the sound both running. The earlier figure of 16 kB/s was an
# order of magnitude below an older reading and left the allowance so large that
# the ceiling below always applied instead, making the whole calculation a
# constant with extra steps. At a third, a frame of live television is allowed
# about a second: comfortably more than the 170 ms it needs, and still short
# enough that a genuinely stuck socket is noticed inside a couple of frames.
MIN_LINK_BYTES_PER_SEC = 32 * 1024

# How long a frame has to reach the device before the sender abandons the session.
#
# Just under the 1500 ms the device allows for reading one picture packet
# (main/av_player.c), so that whatever happens the sender is the one that decides
# when to give up rather than being told by a closed socket. It was 630 ms -- the
# frame's bytes over the pessimistic 32 kB/s above -- which put the sender's
# patience at less than half the receiver's and made an ordinary stall look like
# a dead link.
FRAME_WRITE_DEADLINE_S = float(os.environ.get("TV_FRAME_DEADLINE_S", "2.5"))

# The socket's send buffer, which is not a pacing control at all.
#
# It sat at 16384 (32768 effective, since the kernel doubles it) because that
# matched the device's advertised receive window, and the reasoning was that
# holding more only pushes data onto the wire sooner than the device wants it.
# That is true of a sender that runs ahead, and this one does not: it is paced
# off the device's own audio clock, so the buffer only ever holds what the link
# has not yet accepted.
#
# Measured, and this is what says the old figure was binding: `netstat` on the
# sending side repeatedly showed the send queue pinned at exactly 16384 bytes --
# the whole buffer, full, with the sender waiting on it -- while the session ran.
# A buffer that is always full is a buffer that is too small, not a sender that
# is too eager. What it costs is throughput: the link measured 151 to 162 kB/s
# across every frame rate tried, which is a figure that does not move when the
# offered load does, and that is the signature of a window rather than a rate.
#
# It was raised to 65536 on 2026-09-16 to see whether the window was the limit,
# and it is not. The signature looked right -- 151 to 162 kB/s whatever the
# offered load -- but with the buffer at 65536 the send queue backed up to 64 kB
# and the picture rate FELL, from 5.0 to 2.2 frames a second, with no failed
# session. A sender holding 64 kB it cannot deliver is not short of room; it is
# short of link. The figure is back to 16384, where the queue sitting full means
# the buffer is the right size for a link that is already the constraint.
SEND_BUFFER_BYTES = int(os.environ.get("TV_SEND_BUFFER", "16384"))

# How much of a picture packet is written before the sound gets a turn.
#
# Measured both ways, and the two failure modes pull in opposite directions. At
# 512 bytes the slicing itself was the cost: every slice ends in a select, and
# the device reported a 23232-byte packet arriving only 3790 bytes deep after
# 563 ms -- a sixth of it -- against the same link carrying the whole packet in
# about 300 ms when written in one go. At the other end, one unsliced write
# blocks the loop for that same 300 ms and the sound goes with it.
#
# Four kilobytes is the compromise: a slice occupies the loop for roughly 70 ms
# at the measured rate, well inside the 300 ms the device tolerates, and a
# packet of 20 kB becomes five or six slices rather than forty.
VIDEO_SLICE_BYTES = 4096

# A single write slower than this is reported with its size and its position in
# the frame, which is what tells a socket that is full apart from a loop that is
# simply slow.
#
# The two media share one loop and one socket, so a write that blocks is the
# sound's problem as much as the picture's, and the report says which packet it
# was and how far into it. Measured with this at 50 ms during the 2026-09-15
# investigation: nothing in a healthy run reaches it, while a run that is losing
# packets shows 60 to 430 ms writes. Set it to 0 to report every write, or to a
# large number to turn the line off.
PROBE_SLOW_S = float(os.environ.get("TV_PROBE_SLOW_S", "0.05"))


def local_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return any(ip in network for network in PRIVATE_NETWORKS)


def validate_token(token: str) -> bytes:
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    if any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    return token.encode("ascii")


def load_token(path: Path | None = None) -> bytes | None:
    """The pairing token, or None when none is configured.

    None is not an error. A device that was set up without being given a token
    sends an empty one, and a server that insists on a token would then refuse
    every connection from a perfectly ordinary device. Running without one is
    allowed and is reported loudly by the caller: it means anything that can
    reach this port can watch, which is fine on a home network and wrong the
    moment the port is exposed.

    When a token IS supplied it is enforced exactly as before.
    """
    environment = os.environ.get(TOKEN_ENV)
    if path is not None and environment is not None:
        raise ValueError("choose either token environment or token file")
    if path is None:
        if environment is None:
            return None
        return validate_token(environment)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ValueError("token file must be owner-only, regular, mode 0400 or 0600")
        raw = stream.read(130)
    # One optional trailing LF, not arbitrary whitespace, and bounded file size.
    try:
        return validate_token(raw.removesuffix(b"\n").decode("ascii"))
    except UnicodeError:
        raise ValueError("token file must contain printable ASCII") from None


def authenticate(packet: Packet, token: bytes | None) -> None:
    """Check the opening HELLO, and the token in it when one is required."""
    if (packet.kind != Kind.HELLO or packet.session != 0 or
            packet.seq != 0 or packet.pts_ms != 0):
        raise ProtocolError("expected initial HELLO")
    hello = json_object(packet.payload)
    if type(hello.get("version")) is not int or hello["version"] != 1:
        raise ProtocolError("unsupported HELLO version")
    if token is None:
        # No token required, so whatever the device offered is not inspected.
        # The HELLO shape has still been checked above: a client that cannot
        # speak the protocol is refused even when it does not have to identify
        # itself.
        return
    supplied = hello.get("token")
    if not isinstance(supplied, str):
        raise ProtocolError("authentication failed")
    try:
        supplied_bytes = validate_token(supplied)
    except ValueError:
        raise ProtocolError("authentication failed") from None
    if not hmac.compare_digest(supplied_bytes, token):
        raise ProtocolError("authentication failed")


class AVServer:
    """One thread owns accept/read/write. Extra connections are closed, not queued.

    Socket buffers are bounded (the OS may round/double SO_SNDBUF); user space
    holds one <=24 KiB outgoing packet and a fixed <=3.3 MiB ten-second media set.
    All blocking operations have a deadline and shutdown is polled every 50 ms.
    """

    def __init__(self, media: Media, token: bytes | None, bind: str = "127.0.0.1",
                 port: int = 8096, duration_ms: int = 1800000, logger=None):
        if not local_ipv4(bind):
            raise ValueError("bind must be an explicit loopback or RFC1918 IPv4 address")
        if not 1 <= duration_ms <= 86400000:
            raise ValueError("duration must be between 1 ms and 24 hours")
        if token is not None:
            # Validated here so a malformed token is an error at construction
            # rather than a refused connection later. None means no token is
            # required, which authenticate() treats as "do not check".
            validate_token(token.decode("ascii"))
        self.media, self.token = media, token
        self.bind, self.port, self.duration_ms = bind, port, duration_ms
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.listener: socket.socket | None = None
        self.completed = self.rejected = self.failed = self.dropped_video = 0
        # A connection that closed before it became a session. Kept apart from
        # `failed` because it is the ordinary outcome of three things that are
        # not failures: the device changing channel, the device going to sleep,
        # and anything at all on the network checking whether the port is open.
        # Each opens a connection and closes it again without saying anything,
        # which is an error at the socket layer and nothing of the sort in the
        # world it is being counted in. Counted rather than discarded so the
        # diagnostic log still shows every connection that arrived.
        self.abandoned = 0
        self.audio_sent = self.video_sent = 0
        self.session_id = 0
        self.phase = "idle"
        self.live_channel: LiveChannel | None = None
        self.live_enabled = False
        # "" sends both; "audio" or "video" is a diagnostic aid, never a
        # viewer-facing setting. See _pace_live.
        self.media_filter = ""
        self.channel_name = ""
        self.live_note = ""
        self.ffmpeg = "ffmpeg"
        # Injectable so the pacing loop can be tested without ffmpeg or network.
        self.channel_factory = LiveChannel
        self.logger = logger or (lambda message: None)
        self.session_limit_s: float | None = None
        self.fault_injector: Any | None = None
        self.session_state: SessionState = SessionState.CLOSED
        self.state_history: List[Dict[str, Any]] = []

    def set_session_state(self, state: SessionState, session: int, reason: str = "", **kwargs: Any) -> None:
        """Record explicit session state transitions with wall clock and telemetry metrics."""
        old_state = getattr(self, "session_state", SessionState.CLOSED)
        self.session_state = state
        now_m = time.monotonic()
        rec = {
            "timestamp": round(now_m, 4),
            "session": session,
            "from_state": old_state.value if isinstance(old_state, SessionState) else str(old_state),
            "to_state": state.value if isinstance(state, SessionState) else str(state),
            "reason": reason,
            "audio_sent": getattr(self, "audio_sent", 0),
            "video_sent": getattr(self, "video_sent", 0),
            "dropped_video": getattr(self, "dropped_video", 0),
            **kwargs
        }
        if not hasattr(self, "state_history") or self.state_history is None:
            self.state_history = []
        self.state_history.append(rec)
        detail_str = " ".join(f"{k}={v}" for k, v in kwargs.items())
        self.logger(
            f"session={session} STATE_CHANGE {rec['from_state']} -> {rec['to_state']} "
            f"reason='{reason}' {detail_str}".strip()
        )

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.port))
            self.port = listener.getsockname()[1]
            # Room for a device that is retrying.
            #
            # One was enough while a session either worked or ended promptly,
            # but this loop serves one connection at a time and a session that
            # is struggling holds it for as long as its own timeouts allow. The
            # device retries once a second, and with a backlog of one every
            # attempt after the first was refused before it was ever accepted:
            # measured on the hardware as "RX connect failed" with a session
            # that lasted exactly 3049 ms, which is the device's own three-second
            # connect timeout and not a fault in anything it did.
            #
            # Eight is more retries than a device can produce while one is being
            # served, and it costs nothing: the entries are descriptors the
            # kernel has not accepted yet, not threads.
            listener.listen(8)
            listener.setblocking(False)
            self.ready.set()
            while not self.stop.is_set():
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                connection, peer = listener.accept()
                with connection:
                    if not local_ipv4(peer[0]):
                        self.rejected += 1
                        continue
                    connection.setblocking(False)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    # Big enough for one picture packet to be written without
                    # blocking partway, and no bigger.
                    #
                    # This sat at 131072 and the receiver was measured spending
                    # 2799 ms of a ten-second window parked in its audio
                    # flow-control wait -- the sender was far enough ahead that
                    # the device kept filling its queue and stopping. The kernel
                    # doubles whatever is set here, so 16384 gives an effective
                    # window of 32768, which matches the device's own advertised
                    # receive window: past that the device stops reading anyway,
                    # and holding more here only pushes data onto the wire
                    # sooner than it wants it.
                    #
                    # Larger values were measured and are worse: at 65536 a run
                    # ended with ten failed sessions and the server exiting
                    # altogether.
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                          SEND_BUFFER_BYTES)

                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    self.session_id = self.audio_sent = self.video_sent = 0
                    outcome, reason = "closed", "none"
                    self.phase = "hello_receive"
                    try:
                        self._session(connection)
                        self.completed += 1
                    except (OSError, EOFError, ProtocolError, LiveError) as error:
                        # A write might be partial: close, never send another frame.
                        # Deliberately omit payloads, token, peer IP, exception repr.
                        #
                        # Whether this is a fault depends on how far the session
                        # got. A peer that vanished during the handshake never
                        # became a session -- nothing was authenticated, nothing
                        # was sent, and at the other end somebody changed channel
                        # or closed a window. Counting that as a failure made a
                        # healthy run end with "失败 3 次" and no way to find out
                        # what the three were. Past authentication it is a real
                        # fault, and stays one.
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        # No fallback capture here: _live_session already records
                        # ffmpeg's diagnostics in its own finally, before clearing
                        # self.live_channel, so anything read at this point would
                        # be stale. Only the error class name is logged, never
                        # media, peers or exception text.
                    except Exception as error:
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        import traceback
                        tb = traceback.extract_tb(error.__traceback__)
                        sanitized_tb = " -> ".join([f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in tb[-3:]])
                        self.live_note = f"unexpected={type(error).__name__}[{sanitized_tb}]"
                    finally:
                        # Only code-defined phases and exception class names; never str/repr(error).
                        note = f" note={self.live_note}" if self.live_note else ""
                        self.logger(f"session={self.session_id} state={outcome} "
                                    f"reason={reason} phase={self.phase} "
                                    f"authenticated={int(self.session_id != 0)} "
                                    f"audio_sent={self.audio_sent} video_sent={self.video_sent}{note}")
                        self.live_note = ""
                # Reject backlog arrivals from the old session, not future sessions.
                self._reject_waiting()
        self.listener = None

    def _reject_waiting(self) -> None:
        assert self.listener is not None
        # Bounded work even under connection flood.
        for _ in range(8):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            connection.close()
            self.rejected += 1

    def _device_left(self, connection: socket.socket, session: int) -> bool:
        """True when the device has finished with this session.

        Either it sent END -- the ordinary way to change channel -- or it closed
        the connection outright. Both are the same thing from here, and neither
        is a fault. Called before a write, and between the writes of one frame,
        because a closed socket still accepts a write until one is attempted:
        without this the departure surfaces as a broken pipe part way through a
        frame, and an ordinary channel change is counted as an error.

        Raises ProtocolError only for something that really is one: a peer that
        sends anything other than END while the picture is running.

        A connection that ends without END is *not* caught here. It used to be,
        and that made the operator's count depend on which of two code paths
        noticed the departure first: a device that had authenticated and then
        vanished was counted as a completed session if this function saw the
        FIN, and as a failure if the waiting loop saw it instead. Measured, that
        was about one run in ten ending with the wrong number -- and the number
        is the one thing the operator is shown at exit, so being right nine
        times out of ten is being wrong. The design line is the handshake, not
        the FIN: nothing authenticated is a device that changed its mind, and
        everything after it that breaks is a fault worth reporting. Letting the
        error through is what puts it on the correct side of that line.
        """
        if not select.select([connection], [], [], 0)[0]:
            return False
        try:
            packet = receive_packet(connection, IO_TIMEOUT, expected_session=session)
        except TimeoutError:
            # select() can report a connection readable whose data is not yet a
            # whole packet. The device is still there and this is not a
            # departure, so the session carries on -- the same judgement
            # _wait_until makes for the same reason.
            #
            # Caught separately, and this matters: TimeoutError is a subclass of
            # OSError, so the old code's broad "OSError means the device went
            # away" also swallowed this one. Removing that clause wholesale took
            # the real departure away with the false one and turned every
            # half-arrived packet into a failed session -- measured, a session
            # every six seconds, which is what made the first hardware
            # measurements of this change unreadable.
            return False
        except EOFError:
            # A peer that closed without sending END. Let it through: whether
            # that counts as a fault is a question about the handshake, and
            # serve() is where that is decided.
            raise
        if packet.kind != Kind.END:
            raise ProtocolError("only END allowed while playing")
        return True

    def _wait_until(self, connection: socket.socket, target: float, session: int) -> bool:
        self.phase = "schedule_wait"
        while not self.stop.is_set():
            delay = max(0.0, min(0.05, target - time.monotonic()))
            watch = [connection]
            if self.listener is not None:
                watch.append(self.listener)
            readable = select.select(watch, [], [], delay)[0]
            if self.listener is not None and self.listener in readable:
                self._reject_waiting()
            if connection in readable:
                self.phase = "control_receive"
                try:
                    packet = receive_packet(connection, expected_session=session)
                except TimeoutError:
                    # select() can report readable for a connection whose data is
                    # not yet consumable. That is not a peer error and must not
                    # end a live session; resume waiting for the next deadline.
                    self.phase = "schedule_wait"
                    continue
                if packet.kind != Kind.END:
                    raise ProtocolError("only END allowed after HELLO")
                return False
            if time.monotonic() >= target:
                return True
        return False

    def _live_session(self, connection: socket.socket, channel_id: str) -> None:
        """Pace one live channel against a single monotonic origin.

        A channel switch is a new session: the device reconnects asking for the
        next channel, so every session owns one transcode process and one time
        origin. Audio PTS advances by exactly 20 ms per 640-byte block and is
        never revoked; video frames are matched to that position and a frame
        whose slot has passed is skipped rather than sent late.
        """
        self.phase = "live_start"
        # The agent travels with the channel: many mirrors answer only the
        # player they were captured for, and 403 anything else.
        channel = self.channel_factory(CHANNELS[channel_id], self.ffmpeg,
                                       CHANNEL_AGENTS.get(channel_id, ""))
        self.channel_name = channel_id
        try:
            session = secrets.randbelow(0xFFFFFFFF) + 1
            self.session_id = session
            # CONFIG goes out before anything is asked of the source, and that
            # ordering is the whole reason this works.
            #
            # The device allows two seconds for the handshake and gives up
            # silently: it closes, logs bytes=0/24 and reconnects, which from
            # this side is an ordinary EOF a second or so after HELLO. Choosing
            # a palette means sampling a second and a half of a live source, so
            # doing it first put two seconds of ffmpeg in front of the handshake
            # and every session died before its first byte -- measured on the
            # real device, six seconds apart, for as long as it was left running.
            # A comment further down used to warn about exactly this hazard
            # while the palette call sat above it doing it anyway.
            #
            # Nothing here depends on the palette: CONFIG describes the format,
            # not the colours in it.
            send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                           json_bytes(dict(CONFIG, session=session,
                                                           channel=channel_id,
                                                           channel_list=channel_list()))))
            self.phase = "live_palette"
            # Now the palette, and only now. It has to be in hand before the
            # first frame, so it is built here rather than lazily, and the device
            # waits for it inside the session it has already accepted.
            # Required, not optional: a session that skipped it would send
            # indices the device has no colours for and draw a black screen
            # while everything else looked healthy.
            try:
                channel.build_palette()
            except LiveError as error:
                # Said plainly, because it is the one failure the device cannot
                # work out for itself: it would otherwise wait for a palette
                # that is never coming.
                try:
                    send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                   json_bytes({"reason": "source unavailable"})))
                except OSError:
                    pass
                raise LiveError(str(error)) from None
            if not channel.palette:
                raise LiveError("channel has no palette")
            # start() is inside the try: a failing Popen would otherwise leak its
            # pipes and stderr file on every reconnect attempt.
            channel.start()
            self.live_channel = channel
            # A calibration that did not happen is reported where a person will
            # see it, not merely stored on an object.
            #
            # An external review made the distinction: `timeline.calibration_note`
            # was being set and never surfaced, `start()` discarded the return
            # value, `failure()` stayed None and `prebuffered()` only counts
            # queue length -- so "the server knows ffprobe is missing" and "the
            # operator can find out" were two different facts.
            #
            # It is a WARNING and not a failure: sound and picture still flow,
            # the picture simply has no verified session position. Refusing the
            # session outright would turn a degraded mode into no service.
            if not channel.timeline.calibrated:
                self.logger(
                    f"session={session} WARNING no clock calibration: "
                    f"{channel.timeline.calibration_note}; the picture will not "
                    f"be paired and no frames will be sent")
            # The palette follows CONFIG and precedes every frame. It belongs to
            # this channel: a viewer changing channel gets a new one, which is
            # why it is sent per session rather than once at connection.
            send_packet(connection, Packet(Kind.PALETTE, session, 1, 0,
                                           channel.palette))
            self.logger(f"session={session} state=live channel={channel_id}")
            self.phase = "live_prebuffer"
            deadline = time.monotonic() + PREBUFFER_TIMEOUT_S
            while not channel.prebuffered() and time.monotonic() < deadline:
                if channel.failure():
                    # Same courtesy as below: say why, so the device can move on
                    # rather than sit through its own timeout.
                    try:
                        send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                       json_bytes({"reason": "source unavailable"})))
                    except OSError:
                        pass
                    raise LiveError("transcode failed before prebuffer completed")
                # Poll the connection and the stop flag, not just the buffer.
                # Sleeping the whole timeout here ignored an operator's stop for
                # up to ten seconds and, worse, could not see the device give up
                # and reconnect: the accept loop is single-threaded, so the new
                # connection waited behind this one and every attempt inherited
                # the same delay.
                if self.stop.is_set():
                    return
                # _wait_until owns the phase while it polls, so restore ours
                # before acting on its answer; otherwise a session that ended
                # here would be logged as a scheduling wait.
                waiting = self._wait_until(connection, min(deadline, time.monotonic() + 0.02), session)
                self.phase = "live_prebuffer"
                if not waiting:
                    return
            if not channel.prebuffered():
                # Tell the device why before dropping the connection. It hears the
                # reason immediately instead of waiting out its own first-media
                # allowance, which is how a dead source used to hold the screen on
                # the test pattern for half a minute.
                try:
                    send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                   json_bytes({"reason": "no media from source"})))
                except OSError:
                    pass
                raise LiveError(f"no media within {PREBUFFER_TIMEOUT_S} s of starting the channel")
            self.set_session_state(SessionState.PLAYING, session, reason="session_started")
            self._pace_live(connection, channel, session)
        finally:
            self.set_session_state(SessionState.CLOSED, session, reason="session_closed",
                                   audio_sent=self.audio_sent, video_sent=self.video_sent)
            if self.fault_injector is not None:
                self.fault_injector.cancel_pending(current_session=None, reason=f"Session {session} ended")
            try:
                if channel.failure() is not None:
                    try:
                        self.live_note = channel.diagnostics()
                    except Exception as diag_err:
                        self.logger(f"session={session} WARNING: channel diagnostics failed: {diag_err}")
            finally:
                try:
                    channel.close()
                except Exception as close_err:
                    self.logger(f"session={session} WARNING: channel close failed: {close_err}")
                finally:
                    self.live_channel = None

    def _pace_live(self, connection: socket.socket, channel: LiveChannel,
                   session: int) -> None:
        engine = os.environ.get("TV_LIVE_ENGINE", "legacy")
        if engine == "v2":
            from .live_sender import LiveSender
            LiveSender(self, connection, channel, session).run()
            return
        if engine != "legacy":
            raise ValueError("TV_LIVE_ENGINE must be legacy or v2")
        # The origin must not precede "now" by more than the audio lead: ffmpeg
        # fills its queues far faster than real time, so a past-dated origin
        # would make the first slots due immediately and flush a burst of audio
        # the device cannot buffer, followed by silence long enough to underrun.
        #
        # It does not have to be pushed back by the pre-buffer depth, and briefly
        # was. The reserve in the queue is not dated into the origin; it is the
        # cushion that lets the queue stay deep while the sender runs at real
        # time. What stops the opening from being a burst is the pair of caps
        # below -- AUDIO_MAX_LOOKAHEAD_MS on the sound and a slot per frame
        # interval on the picture -- not the origin. Dating the origin a reserve
        # ahead therefore bought nothing and cost that much extra delay on top of
        # the reserve's own wait.
        # The origin is set by whichever of the two is larger, and the apparent
        # redundancy is not one.
        #
        # It reads as though AUDIO_LEAD_MS cancels out -- the release time below
        # is `origin + (audio_pts - AUDIO_LEAD_MS)`, so raising the origin by the
        # lead looks like it must lower the release instant by the same amount.
        # It was changed to `now + START_DELAY_MS` on that reasoning and put
        # back, because the reasoning was wrong: what the expression decides is
        # not when the first chunk goes but how far `audio_pts` may run ahead of
        # the clock, and solving it gives 320 ms either way when the lead is 320.
        # The change instead cut the cushion to 120 ms, since `origin` sets the
        # wall-clock instant the timestamps are measured against and the lead is
        # measured from it.
        #
        # The cushion is what stands between a picture packet and a dead session,
        # and it is too small -- see the changelog for the measurements -- but it
        # is sized by AUDIO_LEAD_MS and by what the device tolerates, not by this
        # line.
        origin = time.monotonic() + max(START_DELAY_MS, AUDIO_LEAD_MS) / 1000
        audio_pts = 0
        # There is no `video_pts` here and there is deliberately no
        # `last_video_pts` either. Both existed so that the picture's timestamp
        # could be derived in this loop, and neither can be: the timestamp now
        # comes out of `pop_video()` with the frame, decided from the sound the
        # frame was paired with. An initialiser left here would be dead code
        # that reads like the place the value still comes from.
        # The frame slot the last picture was sent in, or -1 for none yet.
        #
        # The slot a pass is in is worked out from the clock, and a frame may go
        # out only when that number has moved past this one. Keeping the last
        # slot rather than a "next due time" is what makes the pace impossible
        # to escape: there is no variable that a slow pass, a pause or a burst
        # can leave disagreeing with the present. See the comment in the loop
        # for the two ways this went wrong when it was a due-time instead.
        last_slot = -1
        # The picture rate, chosen as the session runs rather than fixed for it.
        #
        # A live channel's frames do not all cost the same: a studio shot
        # compresses to a fraction of one with movement in it, and the same
        # frames a second is therefore twice the bytes on one channel and half
        # on another. A rate chosen for the worst case is a rate a quiet channel
        # is stuck at. See server/rate.py for what decides it and why the
        # decision is separate from this loop.
        #
        # `fps` here is the rate in force; the controller may change it once a
        # second, and everything below that used media.FPS now reads this
        # instead. media.FPS remains the announced rate in CONFIG -- it is not
        # that the device paces by, since every frame carries its own timestamp
        # and is scheduled against the audio clock, so the picture can move
        # without telling it.
        # Adaptive by default, and each channel's ceiling is derived from that
        # channel's own frame size rather than fixed for the list.
        #
        # This comment used to say the adaptive version "is written and tested
        # but is not in use" and to recommend `TV_ADAPTIVE=1` -- while
        # `ADAPTIVE` in rate.py defaults to true and the running program has
        # always taken that branch. A comment that contradicts the code is worse
        # than no comment: the next reader trusts it, and after a context
        # compaction that reader is whoever picks this up next.
        #
        # What is in force, measured on the device: the rate moves between the
        # floor of 3 and a ceiling derived as `budget / this channel's bytes a
        # frame`, so a cheap channel settles near 10 and a heavy one near 3. It
        # steps down the moment a write is slow or a window is over budget and
        # climbs after two comfortable windows.
        #
        # `TV_ADAPTIVE=0` still exists and pins the rate at media.FPS, which is
        # what a measurement wants: a sweep that asked what the device does at
        # ten frames a second would otherwise be told what the controller does
        # about ten frames a second.
        controller = RateController() if ADAPTIVE else FixedRate(FPS)
        fps = controller.fps
        # Kept on the server as well as in the loop so a test can read back the
        # rate a real session settled on, and so the log after a session says
        # where it ended rather than where it started.
        self._last_controller = controller
        # What the controller is given when its second is up: the picture's own
        # bytes, and the worst single write inside the window. Both are cleared
        # here and accumulated in the loop.
        window_video_bytes = 0
        # Frames that actually reached the wire in this window. The controller
        # needs it to work out what a frame of this channel costs, which is what
        # its rate ceiling is derived from -- see RateController.ceiling.
        window_frames = 0
        window_worst_write = 0.0
        # Frames given up in this window, reported to the controller. It is the
        # only signal there that comes from the device rather than from this
        # socket: prompt writes and modest byte totals say the link had room,
        # and say nothing about whether the thing on the far end could draw what
        # it was sent.
        window_dropped = 0
        window_started = time.monotonic()
        self._recovery_start = 0.0
        # CONFIG took sequence 0 and the palette sequence 1, so the first
        # media packet is 2. The device refuses any packet whose sequence is
        # not exactly the next one.
        seq = 2
        last_report = 0.0
        last_audio = last_video = 0
        # The longest this loop went without putting anything on the wire, and
        # how much of the interval it spent writing.
        #
        # The device reports its own longest wait between packet headers, and
        # the two numbers together say which side stalled. The device's figure
        # alone cannot: a server that is sending steadily into a link that is
        # dropping looks exactly like a server that has stopped. This is the
        # same measurement taken at the other end, so the pair is decisive.
        gap_start = time.monotonic()
        max_send_gap = 0.0
        busy_us = 0.0

        def note_send(took: float) -> None:
            nonlocal gap_start, max_send_gap, busy_us
            at = time.monotonic()
            idle = at - gap_start
            if idle > max_send_gap:
                max_send_gap = idle
            busy_us += took * 1e6
            gap_start = at
        self.wire_bytes = 0
        while not self.stop.is_set():
            if channel.failure():
                raise LiveError("transcode stopped")
            now = time.monotonic()
            if self.session_limit_s is not None and (now - origin) >= self.session_limit_s:
                self.logger(f"session={session} TEST_LIMIT_REACHED {self.session_limit_s}s: ending session gracefully")
                return
            if self.fault_injector is not None:
                ok, pause_duration = self.fault_injector.check_pause(connection, session, self)
                if not ok:
                    return
                if pause_duration > 0:
                    # Injected downstream pause completed.
                    now = time.monotonic()
                    # Re-anchor origin to prevent negative lookahead backlog burst
                    origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
                    slot_now = int((now - origin) * fps)
                    # Pre-roll audio: hold video for 2 slots (~160ms) to allow audio to pre-fill device buffer
                    last_slot = slot_now + max(2, int(0.20 * fps))
                    self._recovery_start = now
                    self.set_session_state(
                        SessionState.RECOVERING,
                        session,
                        reason="pause_completed",
                        pause_duration_s=round(pause_duration, 4),
                        realigned_origin=round(origin, 4),
                        audio_pts=audio_pts,
                        slot_now=slot_now
                    )
                    # Reset rate controller window so paused interval does not corrupt rate controller
                    window_started = now
                    window_worst_write = 0.0
                    window_video_bytes = 0
                    window_frames = 0
                    window_dropped = 0
            self._loops = getattr(self, "_loops", 0) + 1
            # The picture's timestamp is a position on the shared timeline, taken
            # from the clock -- the same origin the sound is measured against --
            # and NOT a count of slots consumed.
            #
            # It used to be `video_index * 1000 // FPS`, and `video_index`
            # advanced by one whenever a frame was dropped as well as when one
            # was sent. Dropping is exactly what happens when the link is under
            # strain, so the count ran ahead of real time for as long as the
            # congestion lasted, and every frame went out stamped in the future:
            # measured with the transport probe, the picture's clock had reached
            # 5.0 s while the sound was still at 0.12 s.
            #
            # The device waits for its audio clock to reach a frame's timestamp
            # before drawing it, so a future-stamped frame is not late, it is
            # early -- and it sat there holding a receive buffer for as long as
            # the gap, reading nothing, until the sound underran and the session
            # died. That is the mechanism behind every failure in this project's
            # hardware testing; it is one bug, not a shortage of bandwidth.
            #
            # A frame may not share a timestamp with the one before it, because
            # the device rejects a video packet whose timestamp does not advance,
            # so the value is held strictly increasing.
            #
            # And it follows the sound, which is the master clock. The two were
            # derived from different things -- pictures from the wall clock,
            # sound by adding 20 ms a packet -- so they drifted apart, and the
            # device, which expects both to describe one timeline, ended up with
            # the sound ahead of the picture and refused the stream. Measured:
            # audio_next_pts=220 while video_pts=200, and with the picture
            # following the sound instead, the two read 6100 and 6100.
            # Computed here, COMMITTED only when a frame actually goes out --
            # and the difference between those two is a bug this loop had.
            #
            # `last_video_pts` used to be advanced on every pass, including
            # passes that sent nothing. The timestamp then measured how many
            # times the loop had gone round rather than where the picture had
            # reached, and because this loop spins whenever it has nothing to
            # do, the gap opened fast: a controlled test held the queue full but
            # returned no matching frame for five thousand passes, and the first
            # frame to go out afterwards carried pts=6318 ms while the sound was
            # still at 1280 ms.
            #
            # The device schedules a frame against its audio clock, so a frame
            # stamped five seconds into the future is not late -- it is early,
            # and it sits holding a receive buffer until the sound catches up.
            # That mechanism is written up at length above; this is one of the
            # ways to trigger it.
            # NOT computed here. The picture's timestamp is decided where the
            # picture is chosen, from the sound it is paired with, and travels
            # out of `pop_video()` with the frame. See SessionClock for why a
            # value computed at the top of this loop cannot be that timestamp:
            # it would be a statement about how far this loop has got, and it
            # would be committed only on the passes that happen to send, so the
            # picture would additionally be stamped with the moment a frame was
            # found rather than the moment its content belongs to.
            audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            # When the picture may go is a separate question from what its
            # timestamp says, and the two were briefly the same expression --
            # which stops the picture entirely.
            #
            # The sound's clock advances as a consequence of sending it: the
            # instant a chunk goes out, audio_pts has moved 20 ms on and the next
            # audio slot is due 20 ms later. A frame made due at that same
            # instant is therefore due exactly when the sound is, every time, and
            # the sound is served first by design -- so the picture never gets a
            # turn. Measured with the two locked together: five minutes of
            # audio_pps=50.0 with video_pps=0.0, the picture queue pinned full at
            # 180 frames while the producer discarded nine frames a second.
            #
            # So the picture keeps its own slot, one frame interval apart on the
            # wall clock, and is paced by it. That is the same timebase the
            # sound is ultimately held to (AUDIO_MAX_LOOKAHEAD_MS keeps it from
            # running ahead), so the two stay in step without the picture being
            # scheduled on the sound's own bookkeeping.
            #
            # A frame may go out when the slot the clock is now in is later than
            # the one the last picture went out in. Both numbers are read from
            # the clock, so the pace holds however slow or irregular the passes
            # are, and there is no variable left for a slow pass to corrupt.
            #
            # Two earlier attempts kept a due *time* and both failed on hardware
            # in ways worth remembering, because both looked correct:
            #
            #   * "video_at = max(video_at, now - 1/FPS)" -- pulling the due time
            #     towards the present so a late schedule could not release a
            #     backlog. It runs on every pass, including passes that send
            #     nothing, so after any pause the time sat one interval in the
            #     past; the send then advanced it to exactly now; and the next
            #     pass pulled it back again. The two cancelled, "is it due" was
            #     true every pass, and the picture left at whatever rate the
            #     queue could supply: measured video_pps=25.8 against 4 fps.
            #
            #   * "frames_due = max(frames_due + 1, int((now-origin)*FPS) + 1)"
            #     -- advancing a count to the slot the clock is on, for the same
            #     anti-backlog reason. Same failure by a different route: the
            #     clock keeps moving during the send, so the expression always
            #     landed on the present, the next pass found the frame due again,
            #     and the rate climbed 30, 36, 41, 44 while the wanted figure
            #     was 32 packets a second.
            #
            # Both were a due time compared against a moving present by a
            # predicate with no memory of what had already been sent. The slot
            # number below has that memory.
            slot_now = int((now - origin) * fps)
            # Audio owns the timeline and is never dropped. Video is matched to
            # it, but must be sent when its own slot comes due: waiting for the
            # audio queue to drain first would starve video permanently.
            # Cap how far audio may run ahead of the wall clock. Without this a
            # single slow loop iteration lets the sender flush a burst the
            audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000
            if audio_lookahead_ms < -400.0:
                # Network backpressure or slow socket writes caused sender to fall behind real time.
                # Re-anchor origin to current audio transmission point to eliminate negative lookahead deficit,
                # ensuring video pacing and interleaving continue without starvation.
                origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
                slot_now = int((now - origin) * fps)
                last_slot = min(last_slot, slot_now - 1)
                audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000.0
            # The one-second trace, and it lives here rather than at the top of
            # the loop because everything it prints is computed between the two.
            #
            # It was at the top, reading names that were assigned further down.
            # That is not a style question: `_last_trace` is kept on the server
            # object and survives the session, so the guard below it is false on
            # a first session and true on every later one -- meaning the second
            # connection raised UnboundLocalError on its first pass, which is
            # not caught by serve() and fell through to the handler that prints
            # "the program could not start", ending the process. A diagnostic
            # that stops the program is worse than no diagnostic.
            if now - getattr(self, "_last_trace", now) >= 1.0:
                self._last_trace = now
                self.logger(
                    f"SEND t={now-origin:6.1f}s loops={self._loops} "
                    f"a={self.audio_sent} v={self.video_sent} "
                    f"qa={len(channel.audio)} qv={len(channel.video)} "
                    f"look={audio_lookahead_ms:7.0f} "
                    f"a_due={int(audio_at<=now)} "
                    f"v_due={int(slot_now > last_slot)}")
            # The socket must accept the write now. A blocked socket means the
            # device has filled its queue and stopped reading, which is exactly
            # the back-pressure that keeps this sender on the device's clock.
            # A due chunk that cannot be written is simply left for the next
            # iteration -- audio is never dropped, so neither audio_pts nor seq
            # advances -- and the loop retries a few milliseconds later. Waiting
            # here instead, or failing on a timeout, turned ordinary back-pressure
            # into an ended session.
            # Read before writing. The device sends END when the viewer changes
            # channel and closes at once, and a closed socket still accepts a
            # write until one is attempted -- so sending first meant the loop
            # discovered the departure as a broken pipe part way through a frame
            # and counted an ordinary channel change as a fault. Checking for
            # something to read first lets the END be seen for what it is.
            readable, writable_now, _ = select.select([connection], [connection], [], 0)
            if connection in readable:
                self.phase = "control_receive"
                self._device_left(connection, session)
                return
            writable = bool(writable_now)
            # Audio is due, and audio outranks the picture. Strictly: while a
            # chunk is due the picture waits, however ready it is.
            #
            # The rule used to be softer -- audio yielded only to a frame that
            # could actually be written -- and that reads as the more careful
            # arrangement while being the broken one. A frame becomes "ready"
            # within a millisecond or two of its slot, so on any channel keeping
            # up, audio was refused nearly every pass. Measured on the device:
            # one audio packet in the first eight seconds of a session, the PCM
            # queue empty, and a teardown for underrun. The comment here used to
            # describe that exact deadlock while the condition below caused it.
            #
            # The other soft rule failed the same way for the same reason: audio
            # yielded whenever video was merely *due*, and a frame whose
            # timestamp lags its audio is due for as long as it lags. Either way
            # the picture ended up holding the socket against the sound.
            #
            # Strict priority cannot starve the picture, because audio is due
            # for 20 ms in every 1000 and the loop runs many times faster than
            # that. It costs at most a millisecond of picture delay per chunk,
            # against a device that cannot play the sound at all without it.
            self.wire_bytes = getattr(self, "wire_bytes", 0)
            # Whether the sound may be sent EARLIER than the moment it is for.
            #
            # `now >= audio_at` says no, and that term is the reason the device's
            # PCM queue has never held more than its opening transient. The
            # release instant works out as `origin + (audio_pts - AUDIO_LEAD_MS)`
            # with `origin = t0 + AUDIO_LEAD_MS`, so it is `t0 + audio_pts`: with
            # the timestamps advancing by one chunk per chunk sent, the sound is
            # released at exactly real time and cannot get ahead of it. Neither
            # knob above can change that, and both were measured trying to:
            # the device's queue high-water was 280 ms with the lead at 320, at
            # 700, and with the lookahead cap at 280 and at 700.
            #
            # What that costs is everything. The device plays at real time too,
            # so its queue neither fills nor empties by itself, and a picture
            # packet on the wire -- `elapsed_ms=612` in the device's own log --
            # is 612 ms in which nothing feeds it, against a 300 ms tolerance.
            # Measured with the cap raised to 700 ms and the gate left in place:
            # five underruns, eleven resets and four failed sessions in three
            # hundred seconds, queue high-water 280 ms.
            #
            # With the gate off the sender fills the queue until the sound is
            # AUDIO_MAX_LOOKAHEAD_MS ahead of the wall clock and holds it there,
            # which is what that cap was always written to mean -- its own
            # comment calls it the protection against a burst the device cannot
            # hold. Off by default so both behaviours can be measured.
            audio_fill = os.environ.get("TV_AUDIO_FILL", "1") != "0"
            audio_due = (channel.audio_pending()
                         and audio_lookahead_ms <= AUDIO_MAX_LOOKAHEAD_MS
                         and (audio_fill or now >= audio_at))
            # Audio is due, and the picture is not blocked by it.
            #
            # "not audio_due" was the rule here and it starved the picture
            # completely. Audio is due for 20 ms out of every 20, so with the
            # two in an if/elif the picture only ever went out in the sliver
            # between one chunk being sent and the next falling due -- and
            # because the chunk that was sent makes the next one due
            # immediately, that sliver does not exist. Measured: the server
            # itself sent 3.8 picture packets a second where 6 were wanted,
            # with its audio queue sitting at 892 chunks, and the device
            # reported 1 completed frame in 10 seconds.
            #
            # Audio still goes first, every pass, and the picture is served in
            # the same pass once the sound has been brought up to its lead.
            send_audio = audio_due and writable
            send_video = slot_now > last_slot and writable and channel.video_pending()
            # Diagnosis only: `--media audio` or `--media video` runs a session
            # with one half switched off, so a fault can be attributed to the
            # picture or the sound instead of being argued about. Both default
            # to on, which is the only setting a viewer ever uses.
            if self.media_filter == "audio":
                send_video = False
            elif self.media_filter == "video":
                # The picture is paced from the audio position, so the audio
                # timeline is advanced by hand rather than switched off: with it
                # frozen, every frame is due at once and the run says nothing.
                # audio_at is recomputed from audio_pts each pass, so this is
                # the whole of it.
                send_audio = False
                audio_pts += AUDIO_CHUNK_MS
            self._trace = getattr(self, "_trace", 0) + 1
            if os.environ.get("TV_TRACE") == "1":
                self.logger(
                    f"TRACE n={self._trace} t={(now-origin):6.2f}s "
                    f"due={int(audio_due)} wr={int(writable)} qa={len(channel.audio)} "
                    f"qv={len(channel.video)} look={audio_lookahead_ms:7.0f} "
                    f"sa={self.audio_sent} sv={self.video_sent} "
                    f"late={self.dropped_video} "
                    f"frameK={sum(self._frame_sizes[-3:]) // 1024 if getattr(self,'_frame_sizes',None) else 0} "
                    f"wire_kbps={(self.wire_bytes*8/1000)/max(0.001, now-origin):7.1f}")
            if send_audio:
                # As many chunks as have come due, not one per pass.
                #
                # A pass sends one chunk and one chunk only, which is right
                # while nothing else occupies the loop -- and wrong the moment
                # something does. Writing a picture packet takes about 300 ms,
                # which is fifteen audio slots, and the fifteen passes needed to
                # make them up each have their own chance of meeting another
                # picture packet. Measured, the lead drifted steadily negative
                # instead of recovering.
                #
                # The catch-up is bounded by what has actually come due, so it
                # cannot run ahead of the clock, and by the same lookahead the
                # single-chunk path used, so it cannot overrun the device.
                # Bounded in count as well as by the clock: the lookahead test
                # is taken against the time this pass began, so a long catch-up
                # would still be measured against a stale `now` and could run
                # past the intended lead. Thirty-two chunks is 640 ms, more than
                # the device can hold, so the bound is never the binding one in
                # normal running -- it is here so that a pathological queue
                # cannot turn this loop into the only thing the process does.
                for _ in range(32):
                    if not ((audio_fill or audio_at <= now)
                            and channel.audio_pending()
                            and audio_pts - AUDIO_LEAD_MS - (now - origin) * 1000
                                <= AUDIO_MAX_LOOKAHEAD_MS
                            and select.select([], [connection], [], 0)[1]):
                        break
                    taken = channel.pop_audio()
                    if taken is None:
                        break
                    block, stamp = taken
                    self.phase = "live_pcm_send"
                    send_packet(connection, Packet(Kind.PCM, session, seq, stamp, block))
                    self.wire_bytes += len(block) + 24
                    # `stamp` and `audio_pts` are two different quantities and
                    # both are needed. `stamp` is what goes on the wire: the
                    # session position of this block, counted from the clock the
                    # device keeps. `audio_pts` is the sender's own pacing
                    # reckoning -- when the next block comes due -- and it
                    # advances by one chunk per block SENT, which is what makes
                    # it the right variable for a wait. They are equal on a
                    # healthy stream and are deliberately not merged, because
                    # the whole point of the stamp is that it comes from the
                    # sound rather than from this loop's progress.
                    audio_pts += AUDIO_CHUNK_MS
                    seq += 1
                    self.audio_sent += 1
                    audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            sent_something = send_audio
            if send_video:
                sent_something = True
                # Held to the sound's own depth, so the two stay describing the
                # same moment. See VIDEO_QUEUE_SECONDS in live.py: capping the
                # picture below the sound does not shorten the delay, it only
                # pulls the picture out of step.
                chosen = channel.pop_video()
                frame, video_pts = chosen if chosen is not None else (None, None)
                # A frame that is not due yet is not a frame that is late, and
                # the two must not be confused. pop_video now answers "the frame
                # whose content matches the sound at the head of its queue", and
                # while the decoder has not produced that frame yet there is
                # nothing to send -- so the slot is NOT spent and the next pass
                # tries again.
                #
                # Spending the slot here would be the old mistake wearing new
                # clothes: the picture would be paced by the clock rather than by
                # the sound, which is what put it out of step in the first place.
                if frame is None:
                    # Nothing to send this pass, and the pass still has to do
                    # everything below.
                    #
                    # This used to `continue`, which skipped the wait, the
                    # controller update and the periodic report -- so the three
                    # things that exist to notice a stream going wrong were
                    # starved by the case that most needs them. Measured on the
                    # original loop with a controlled clock: the video queue
                    # holding frames that did not yet match the sound, 500
                    # attempts to take one, and **zero** waits, zero controller
                    # updates and zero reports over more than five seconds.
                    #
                    # Sleeping here alone would not have fixed it either: it is
                    # the controller going unupdated that leaves the rate
                    # wherever it was when the trouble started.
                    sent_something = False
                else:
                    # The slot is spent whether or not the frame reaches the wire:
                    # a frame dropped for congestion must not hand its slot to the
                    # next one, or a congested link would deliver the whole backlog
                    # at whatever rate the link allowed.
                    #
                    # Marked before the send and from the value the decision was
                    # made on, not from the clock afterwards. Reading the clock again
                    # here would let a send that took most of an interval skip the
                    # slot it had just used, which is the accumulation this whole
                    # arrangement exists to avoid.
                    last_slot = slot_now
                    self._frame_sizes = getattr(self, "_frame_sizes", [])
                    self._frame_sizes.append(sum(len(p) for p in frame))
                    # No lateness veto. The device draws a frame as it arrives, so a
                    # frame that is a little behind is a frame the viewer sees a
                    # little late, not one worth throwing away -- and throwing it
                    # away is not free: the index then jumps forward to catch up, and
                    # measured on the hardware that turned into a self-sustaining
                    # race, seven frames discarded every second against twelve
                    # arriving, with the queue stuck near full and the picture never
                    # settling.
                    #
                    # The queue is what bounds staleness now. It holds a fixed number
                    # of frames and discards the oldest when full, so the server can
                    # never send anything older than the picture the device has
                    # already fallen behind on.
                    if writable:
                        # The same probe the audio branch uses. A frame that cannot
                        # start now is dropped rather than attempted: a send that
                        # cannot finish inside the protocol timeout would block this
                        # single-threaded loop and stop audio with it, and a partial
                        # packet must never be left on the wire.
                        #
                        # All of a frame's packets go out together or none do. The
                        # device draws a stripe as it arrives, so a frame whose
                        # second packet was dropped would leave the bottom of the
                        # picture showing the frame before it -- better to keep the
                        # whole old frame on screen and drop the new one, which is
                        # what the index advancing by one without sending does.
                        self.phase = "live_video_send"
                        # One deadline for the whole frame, sized to the frame.
                        #
                        # It used to be a flat IO_TIMEOUT of a quarter second, which
                        # was right while a frame was five packets of a few kilobytes
                        # and is wrong now that one is fifteen packets and tens of
                        # kilobytes: measured on hardware, a frame needed about 290 ms
                        # to cross the link and the deadline fired first, so the
                        # session was torn down mid-frame and the device reconnected.
                        # From outside that looked exactly like the picture being too
                        # heavy for the link -- sessions lasted one to four seconds
                        # and delivered two to three frames a second -- and the fault
                        # was in fact the sender giving up 40 ms too early.
                        #
                        # The deadline is a bound on failing, not a schedule. Pacing is
                        # the scheduler's job and it already drops a frame whose slot
                        # has passed, so a slow frame should cost that frame, not the
                        # session. The allowance is the frame's own size at a
                        # deliberately pessimistic rate, so it grows with the picture
                        # rather than being guessed once.
                        # Bounded by what the frame is worth, not by how long it
                        # might take.
                        #
                        # A frame occupies one slot of the timeline -- at 4 fps that
                        # is 250 ms -- and the sound has to be fed throughout. An
                        # allowance of IO_TIMEOUT plus the frame's size at a
                        # pessimistic rate came to about 1.5 s for a 20 kB frame, so
                        # a frame that could not be placed blocked the single loop
                        # for fifteen times its own slot, and audio went out with it.
                        # Measured: sessions delivered 12 packets in 12 seconds, one
                        # a second, which is the loop turning over once per frame
                        # instead of once per 20 ms of sound.
                        #
                        # Sized to the frame, with a floor and a ceiling.
                        #
                        # A fixed allowance cannot work here, and the measurements
                        # say why from both sides. Too small and an ordinary frame
                        # fails: a frame is now two packets of about 12 to 17 kB
                        # each, the device takes them at roughly 98 kB/s, so one
                        # packet occupies 122 to 173 ms against the flat 250 ms that
                        # used to be allowed -- no margin for a retransmission, and
                        # every expiry ended the session rather than the frame.
                        # Measured: sessions ending after 6 to 16 video packets,
                        # with the device reporting the reset at header-read.
                        #
                        # Too large and a frame that cannot be placed blocks the one
                        # loop that also feeds the sound; the history in this comment
                        # records that as 1.5 s of silence and a torn-down session.
                        #
                        # So: the frame's own bytes at a deliberately pessimistic
                        # rate, never below one slot and never above four. A frame
                        # that cannot be placed in that is dropped, which costs a
                        # picture and keeps the timeline.
                        frame_bytes = sum(len(part) for part in frame)
                        # The ceiling does not scale with the frame rate, and it did.
                        #
                        # It was `min(allowance, 4.0 / fps)`, which reads as "four
                        # frames' worth" and is not: a frame's bytes do not shrink
                        # when fewer of them are sent a second, they are the same
                        # bytes on the same link. So the same 30 kB frame was
                        # allowed 1.0 s at 4 fps and 0.33 s at 12, and the comment
                        # two paragraphs up is explicit that a quarter of a second
                        # leaves no room for a retransmission and that every expiry
                        # ends the session. The ceiling was a frame rate changing
                        # how long a frame may take, which is not a thing that rate
                        # controls.
                        #
                        # Fixed at the headroom a retransmission needs over the
                        # measured 170 ms crossing, and no more: this is a bound on
                        # failing, not a schedule.
                        # How long a frame has to reach the device, and it is the
                        # device's own patience that sets it.
                        #
                        # The device allows 1500 ms to read one picture packet
                        # (`io_all(...,1500)` in main/av_player.c) and, since the
                        # audio change, 3000 ms of silence before it gives up on the
                        # session. This deadline used to work out at about 630 ms for
                        # a native frame -- the frame's bytes over a worst-case 32
                        # kB/s -- so the server gave up less than halfway through the
                        # device's own patience, and one 378 ms slice, measured on a
                        # device that had stopped reading in order to draw, was
                        # enough to exhaust it partway through a frame. The result
                        # was the failure this investigation kept arriving at:
                        # `TimeoutError` in live_video_send, the session torn down by
                        # the end that could still have waited.
                        #
                        # So it sits just under the device's 1500 ms rather than
                        # under a pessimistic estimate of the link. The packet is in
                        # the socket's buffer by the time this matters, so a frame
                        # that arrives late arrives whole and is drawn late -- a
                        # picture a fraction of a second behind, against a channel
                        # lost altogether. The bytes-based term is kept as a floor
                        # for the frame interval itself, so a very slow declared rate
                        # does not stretch the deadline past what one frame is for.
                        frame_deadline = time.monotonic() + max(1.0 / fps, FRAME_WRITE_DEADLINE_S)
                        for n, part in enumerate(frame):
                            # Between packets, give the device the chance to say it
                            # has gone: a frame is several writes and the device
                            # closes as soon as it has sent END, so without this the
                            # departure surfaces as a broken pipe part way through
                            # and an ordinary channel change is counted as a fault.
                            self.phase = "control_receive"
                            if self._device_left(connection, session):
                                return
                            # Let the sound through between the picture's packets.
                            #
                            # A frame used to be five packets and this loop could send
                            # all of them before returning to the scheduler, because
                            # five small writes fit in the socket's send buffer. At
                            # fifteen packets a frame is around thirty-seven
                            # kilobytes, and once the device's window is full the
                            # write blocks here for up to the frame's whole deadline
                            # -- during which no audio is sent at all. Measured on
                            # hardware, sessions died of audio underrun every six
                            # seconds with the server's own audio queue sitting full.
                            #
                            # Nothing about the format requires the packets of a
                            # frame to be adjacent on the wire: each carries its own
                            # length and the frame's timestamp, and the device draws
                            # stripes as they come -- as long as each packet's own
                            # bytes stay together. So audio goes out whenever it is
                            # due, and the picture resumes afterwards.
                            #
                            # Everything that has come due, not one chunk. A packet
                            # takes about 200 ms to cross, which is ten audio slots,
                            # and making those up one per packet leaves the deficit
                            # growing for as long as the picture keeps moving.
                            #
                            # The size of this catch-up was reduced to six on the
                            # theory that the burst was overflowing the device's
                            # Wi-Fi pool and losing packets, and that made things
                            # distinctly worse: eighteen failed sessions in two and a
                            # half minutes, against a handful at thirty-two. A
                            # shallow catch-up cannot repay the audio the picture
                            # withheld, the deficit grows, and the sound underruns
                            # for want of sending rather than for want of link.
                            #
                            # So the burst is not what breaks it, and this is back to
                            # what it was. What is measured, at 8 frames a second on a
                            # live channel: the server sends 50 audio packets a second
                            # and never blocks for more than 388 ms, while the device
                            # counts only 317 to 361 of them arriving over the same
                            # ten seconds. Packets are being lost between the two, and
                            # the loss is not caused by this loop's burst size.
                            for _ in range(32):
                                now_in_frame = time.monotonic()
                                audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
                                lookahead = ((audio_pts - AUDIO_LEAD_MS)
                                             - (now_in_frame - origin) * 1000)
                                if not ((audio_fill or now_in_frame >= audio_at)
                                        and channel.audio_pending()
                                        and lookahead <= AUDIO_MAX_LOOKAHEAD_MS
                                        and select.select([], [connection], [], 0)[1]):
                                    break
                                taken = channel.pop_audio()
                                if taken is None:
                                    break
                                block, stamp = taken
                                self.phase = "live_pcm_send"
                                _audio_t0 = time.monotonic()
                                send_packet(connection, Packet(Kind.PCM, session, seq,
                                                               stamp, block),
                                            deadline=frame_deadline)
                                note_send(time.monotonic() - _audio_t0)
                                audio_pts += AUDIO_CHUNK_MS
                                seq += 1
                                self.audio_sent += 1
                            self.phase = "live_video_send"
                            # Every packet of one frame carries the same timestamp,
                            # and all but the first say so with a flag: the device
                            # rejects a video packet whose timestamp does not
                            # advance, which is what stops a stream from being
                            # reordered, so a continuation has to announce itself
                            # rather than look like a repeat.
                            #
                            # One picture packet goes out as one uninterrupted run
                            # of bytes. Nothing else may be written into the middle
                            # of it, and that is a property of the device rather
                            # than a preference of this code:
                            #
                            #   bool video_ok=io_all(fd,v.jpeg,v.length,false,1500);
                            #   -- main/av_player.c:869
                            #
                            # The device reads the header, takes `length` from it,
                            # and then reads exactly that many bytes as the payload.
                            # It has no way to tell that some of them were meant as
                            # an audio packet; it would take those 664 bytes as
                            # picture data, fail to inflate the stripe, and -- worse
                            # -- read the next header from a byte that is not one.
                            # One such packet ends the session. The comment at
                            # main/av_player.c:842 goes further and records this as
                            # the reason a receiver-side audio-draining loop was
                            # removed: "The very next bytes on the socket are
                            # therefore this packet's payload -- never an audio
                            # packet."
                            #
                            # An earlier version of this loop sliced the packet and
                            # served the sound between the slices. Measured, every
                            # session died within eight to twelve picture packets.
                            #
                            # So the sound is served between packets, which is where
                            # the device is between reads, and the packet itself is
                            # written in slices -- `_write_slice` rather than
                            # `send_packet` -- so that the loop is not held for the
                            # whole packet and can notice a stop request. The slices
                            # are a scheduling courtesy; the atomicity is the
                            # contract.
                            wire = Packet(Kind.JPEG, session, seq, video_pts, part,
                                          VIDEO_CONTINUES if n else 0).encode()
                            sent = 0
                            while sent < len(wire):
                                self.phase = "live_video_send"
                                data = memoryview(wire)[sent:sent + VIDEO_SLICE_BYTES]
                                _probe_t0 = time.monotonic()
                                if not _write_slice(connection, data, frame_deadline):
                                    raise TimeoutError("video packet deadline expired")
                                _probe_dt = time.monotonic() - _probe_t0
                                note_send(_probe_dt)
                                # What the rate controller judges the window by. The
                                # slice, not the packet or the frame: this is the
                                # unit the socket is asked to accept at once, and
                                # the delay it answers with is the link's own
                                # account of how full it is.
                                if getattr(self, "session_state", None) != SessionState.RECOVERING and _probe_dt > window_worst_write:
                                    window_worst_write = _probe_dt
                                if _probe_dt > PROBE_SLOW_S:
                                    self.logger(
                                        f"PROBE slice n={n} bytes={len(data)} "
                                        f"took_ms={_probe_dt * 1000:.0f} phase={self.phase}")
                                sent += len(data)
                            self.wire_bytes += len(part) + 24
                            # The picture's share only. The sound is fixed by the
                            # protocol at 32 kB/s on every channel, so counting it
                            # would make every channel look equally heavy and hide
                            # the difference the controller is here to find.
                            window_video_bytes += len(part) + 24
                            seq += 1
                            self.video_sent += 1
                        # One frame, counted once -- and the placement is the whole
                        # of the fix.
                        #
                        # This increment used to be inside the packet loop above, so
                        # a frame split into two packets counted as two frames. The
                        # controller divides the window's bytes by this number to
                        # work out what a frame of this channel costs, and that
                        # quotient is what its rate ceiling is derived from: a
                        # controlled test sent five complete frames as ten packets
                        # and the controller was told `frames=10`, giving 12540
                        # bytes a frame where the truth was 25081 -- a ceiling
                        # derived from half the real cost.
                        #
                        # There is a bitter symmetry here: an external reviewer had
                        # just pointed out that the firmware's `panel_frames` counts
                        # packets rather than frames, and while checking that, this
                        # one -- mine, added with the very change that reads it --
                        # had the same shape.
                        window_frames += 1
                        if getattr(self, "session_state", None) == SessionState.RECOVERING:
                            rec_start = getattr(self, "_recovery_start", window_started)
                            # Keep RECOVERING until link transient settles (at least 1.5s and 10 frames)
                            if (now - rec_start >= 1.5) and window_frames >= 10:
                                self.set_session_state(
                                    SessionState.PLAYING,
                                    session,
                                    reason="recovered_to_steady_state",
                                    video_pts=video_pts,
                                    realigned_origin=round(origin, 4),
                                    current_fps=fps
                                )
                                window_started = time.monotonic()
                                window_worst_write = 0.0
                                window_video_bytes = 0
                                window_frames = 0
                                window_dropped = 0
                    else:
                        # Congested: give the frame up so audio keeps the timeline.
                        # Nothing advances here any more: the timestamp comes from
                        # the clock, so a dropped frame costs a picture and no more.
                        self.dropped_video += 1
                        window_dropped += 1

            if not sent_something:
                # Nothing is due yet. Sleep briefly instead of blocking until the
                # next deadline: a long wait here would not notice inbound END or
                # disconnect, and a past deadline would spin.
                if not channel.has_data():
                    empty_since = time.monotonic()
                    empty_threshold = 0.5 if getattr(self, "session_state", None) == SessionState.RECOVERING else 0.3
                    # Wait briefly for ordinary inter-chunk delivery before declaring starvation
                    while not channel.audio_pending() and self.media_filter != "video" and (time.monotonic() - empty_since < empty_threshold):
                        time.sleep(0.005)

                    if not channel.audio_pending() and self.media_filter != "video":
                        # Sustained starvation gap. Declare STARVED_REBUFFERING.
                        self.set_session_state(
                            SessionState.STARVED_REBUFFERING,
                            session,
                            reason="channel_audio_empty",
                            empty_since=round(empty_since, 4)
                        )
                        # Device tolerance: AUDIO_SILENCE_MAX_MS is 3000ms.
                        # Wait up to TV_STARVE_TIMEOUT_S (default 5.0s) before giving up and requesting reconnect.
                        starve_timeout_s = float(os.environ.get("TV_STARVE_TIMEOUT_S", "5.0"))
                        while True:
                            if channel.failure():
                                raise LiveError("transcode stopped")
                            if time.monotonic() - empty_since > starve_timeout_s:
                                raise TimeoutError(f"live audio starved beyond device budget ({starve_timeout_s}s); reconnect for a new origin")
                            
                            has_enough = False
                            if self.media_filter == "video":
                                has_enough = channel.video_pending()
                            elif self.media_filter == "audio":
                                has_enough = len(channel.audio) >= 4
                            else:
                                has_enough = channel.video_pending() and len(channel.audio) >= 4

                            if has_enough:
                                break
                            if not self._wait_until(connection, time.monotonic() + 0.02, session):
                                return

                        starve_duration = time.monotonic() - empty_since
                        now = time.monotonic()
                        origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
                        slot_now = int((now - origin) * fps)
                        # Allow video to be sent immediately upon resume
                        last_slot = slot_now - 1
                        self._recovery_start = now
                        self.set_session_state(
                            SessionState.RECOVERING,
                            session,
                            reason="channel_data_resumed",
                            starve_duration_s=round(starve_duration, 4),
                            realigned_origin=round(origin, 4),
                            audio_pts=audio_pts,
                            slot_now=slot_now
                        )
                        window_started = now
                        window_worst_write = 0.0
                        window_video_bytes = 0
                        window_frames = 0
                        window_dropped = 0
                    else:
                        time.sleep(0.005)
                    window_frames = 0
                    window_dropped = 0
                else:
                    time.sleep(0.005)
            # One report, every five seconds, with the send rates in it.
            #
            # There used to be two, and the second could never run: the first
            # raised the same timestamp past the second's threshold before it was
            # tested, so the session summary it was meant to print never appeared
            # in any log. What it would have said is the diagnosis -- packets out
            # per second against packets produced -- so it is said here instead,
            # as rates rather than running totals, because those are what can be
            # compared against a frame rate.
            # One decision a second, made from the second that just ended.
            #
            # A second is the window because it is long enough to hold several
            # frames on any channel and short enough that a link which starts
            # refusing bytes is noticed before the session ends -- the whole
            # failure takes about ten seconds from the first slow write to the
            # device giving up, so the decision has to be made several times
            # inside it.
            if now - window_started >= 1.0:
                # Whether the source is still producing, asked BEFORE the
                # controller is told anything. The distinction it makes is the
                # one the controller has been missing since it was written:
                # "nothing arrived because the source stopped" and "nothing
                # arrived because we are sending faster than the link takes it"
                # are the same reading from this seat, and they want opposite
                # responses. An empty window is not evidence of spare capacity.
                #
                # Asked of the channel rather than required of it: a substitute
                # channel -- the test doubles, and anything else that means to
                # stand in for a live one -- may not have a source to observe,
                # and the controller's own counters remain a correct answer for
                # it. Requiring this method made every such stand-in fail at
                # runtime, which is a worse outcome than losing the distinction.
                if hasattr(channel, "source_state"):
                    source = channel.source_state(
                        queue_over_bound=len(channel.video) >= channel.video.maxlen)
                else:
                    source = SourceState.FLOWING
                # Kept for the periodic report below. **It was computed and then
                # dropped into a local variable**, which an external review
                # pointed out: a classification that reaches nothing is the same
                # as no classification, and it was being cited as done.
                #
                # It is reported rather than fed to the controller, and that is
                # deliberate. The review also warned against the obvious next
                # step -- wiring `measured` in as a master switch -- because a
                # stopped source can still have queued content whose real writes
                # are valid evidence about the downstream. Source progress and
                # downstream capacity are two measurements and must not be
                # collapsed into one boolean.
                self._last_source_state = source
                if getattr(self, "session_state", None) != SessionState.RECOVERING:
                    changed = controller.observe(window_video_bytes,
                                                 window_worst_write * 1000,
                                                 window_dropped,
                                                 frames=window_frames,
                                                 window_s=now - window_started)
                    if changed != fps:
                        fps = changed
                        # Re-derive the slot from the new rate against the same
                        # origin, rather than carrying the old numbering over. The
                        # slot is a count of intervals since the origin, so it means
                        # a different thing at a different rate -- carrying it would
                        # make a slow-down look like a very long time with no frame
                        # sent, and the next pass would send every frame it owed at
                        # once.
                        slot_now = int((now - origin) * fps)
                        last_slot = slot_now
                        self.logger(f"RATE {fps} fps: {controller.describe()}")
                window_video_bytes, window_worst_write = 0, 0.0
                window_dropped = window_frames = 0
                window_started = now
            if now - last_report >= 5:
                elapsed = now - last_report if last_report else 5.0
                self.logger(
                    f"live t={now-origin:6.1f}s fps={fps} "
                    f"rate=({controller.reason}) source={getattr(self, '_last_source_state', '?')} "
                    # Where the picture's CONTENT sat against the sound's, as
                    # counted at the pairing. This is the one sync measurement
                    # the wire timestamps cannot carry, because they are
                    # contiguous by design: `a_lag` and `v_lag` below are queue
                    # depths -- what is waiting to be sent -- and neither says
                    # whether what was sent described the right moment.
                    f"clock=({channel.session_clock.diagnostics()}) "
                    # Which basis related the two content clocks. Printed per
                    # report and not merely stored, because the same offset
                    # means opposite things depending on where it came from --
                    # and because a field that is set but never shown is a
                    # field no operator can act on. An external review counted
                    # zero occurrences of this in the whole previous package.
                    # `getattr` because the report must not be the thing that
                    # ends a session, and a channel stand-in without a timeline
                    # is a test shape rather than a fault. The real channel
                    # always has one.
                    f"basis=({getattr(getattr(channel, 'timeline', None), 'basis', None) or 'none'}) "
                    f"audio_q={len(channel.audio):4d} "
                    f"video_q={len(channel.video):4d} "
                    # How far behind live each stream's NEXT packet is, in
                    # seconds of content. The frame sent is the OLDEST in the
                    # queue, so the depth is the lag -- and the two numbers have
                    # to agree or the picture and the sound describe different
                    # moments. This is the measurement the sync question needs
                    # and it was being inferred from queue counts instead.
                    f"a_lag={len(channel.audio) * AUDIO_CHUNK_MS / 1000:5.1f}s "
                    f"v_lag={channel.picture_lag_s():5.1f}s "
                    f"lookahead_ms={audio_lookahead_ms:6.0f} "
                    f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                    f"audio_pps={(self.audio_sent - last_audio) / elapsed:5.1f} "
                    f"video_pps={(self.video_sent - last_video) / elapsed:5.1f} "
                    f"late_video={self.dropped_video} prod_drop={channel.dropped_video} "
                    f"send_gap_max_ms={max_send_gap * 1000:5.0f} "
                    f"busy={busy_us / 1e6:4.1f}s")
                last_report = now
                last_audio, last_video = self.audio_sent, self.video_sent
                max_send_gap = 0.0
                busy_us = 0.0
        # A server-side stop is a clean end of session, not a stream fault: the
        # loop condition is the only other way out, so an operator's Ctrl-C was
        # logged as a failure and reported ffmpeg diagnostics that did not exist.
        if self.stop.is_set():
            return
        raise TimeoutError("live session cancelled")

    def _session(self, connection: socket.socket) -> None:
        self.phase = "hello_receive"
        hello = receive_packet(connection, expected_session=0)
        self.phase = "authenticate"
        authenticate(hello, self.token)
        if self.live_enabled:
            # The device names the channel it wants, so switching is just a new
            # connection. An unknown or absent name falls back to the default
            # rather than failing: the device may predate the channel list.
            # The --channel argument is the server's default, not a constant:
            # falling back to DEFAULT_CHANNEL here silently overrode the operator's
            # choice whenever the device sent no channel or an unlisted one, while
            # the startup line still announced the argument.
            requested = json_object(hello.payload).get("channel")
            if isinstance(requested, str) and requested in CHANNELS:
                channel_id = requested
            else:
                channel_id = self.channel_name or DEFAULT_CHANNEL
            self._live_session(connection, channel_id)
            return
        session = secrets.randbelow(0xFFFFFFFF) + 1
        self.session_id = session
        self.logger(f"session={session} state=authenticated")
        self.phase = "config_send"
        send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                       json_bytes(dict(CONFIG, session=session, duration_ms=self.media.duration_ms))))
        # The palette before any picture, exactly as the live path does. The
        # device cannot turn an index into a colour without it, and a session
        # that starts with frames would draw black until one arrived.
        send_packet(connection, Packet(Kind.PALETTE, session, 1, 0, self.media.palette))
        origin = time.monotonic() + 0.2
        # CONFIG took sequence 0 and the palette sequence 1.
        seq = 2
        for due_ms, kind, pts, index in schedule(self.duration_ms, self.media.duration_ms):
            if not self._wait_until(connection, origin + due_ms / 1000, session):
                return
            # Read before writing, for the same reason the live loop does: a
            # device that has changed channel sends END and closes at once, and
            # a closed socket still accepts a write until one is attempted. Send
            # first and the departure is discovered as a broken pipe part way
            # through a frame, which counts an ordinary channel change as a
            # fault -- and it is that count the operator sees on exit.
            self.phase = "control_receive"
            if self._device_left(connection, session):
                return
            now = time.monotonic()
            # Do not replay a large stale backlog after host/network suspension.
            if kind == Kind.PCM and now > origin + pts / 1000 + 0.1:
                self.phase = "audio_schedule_late"
                raise TimeoutError("audio schedule stalled; reconnect for a new origin")
            if kind == Kind.JPEG and now > origin + (pts + 83) / 1000:
                self.dropped_video += 1
                continue
            if kind == Kind.PCM:
                self.phase = "pcm_send"
                send_packet(connection, Packet(Kind.PCM, session, seq, pts,
                                               self.media.audio_at(index)))
                seq += 1
                self.audio_sent += 1
            else:
                # A frame is several packets, all under one timestamp, and all
                # but the first marked as continuing it. Same shape as the live
                # path, so the device needs no idea which one it is watching.
                self.phase = "video_send"
                # One deadline for the whole frame; see send_packet.
                frame_deadline = time.monotonic() + IO_TIMEOUT
                for n, part in enumerate(self.media.frame_at(index)):
                    # Between packets, give the device the chance to say it has
                    # gone. Checking only once per frame leaves the channel
                    # change unanswered for as long as the frame takes; a frame
                    # is several packets and the device closes as soon as END is
                    # sent, so a write would then fail part way and be recorded
                    # as a fault instead of as the ordinary thing it is.
                    if n:
                        self.phase = "control_receive"
                        if self._device_left(connection, session):
                            return
                    send_packet(connection, Packet(
                        Kind.JPEG, session, seq, pts, part,
                        VIDEO_CONTINUES if n else 0),
                        deadline=frame_deadline)
                    seq += 1
                    self.video_sent += 1
            if self.audio_sent and self.audio_sent % 500 == 0 and kind == Kind.PCM:
                self.logger(f"session={session} state=streaming pts_ms={pts} "
                            f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                            f"dropped_video_total={self.dropped_video}")
        if self._wait_until(connection, origin + self.duration_ms / 1000, session):
            self.phase = "end_send"
            send_packet(connection, Packet(Kind.END, session, seq, self.duration_ms))


def print_where_to_connect(bind: str, port: int, token: bytes | None) -> None:
    """The last thing printed before serving: the address, then the caveats.

    Together because they are one block and their order is the whole point of
    it. This lived inline in two branches, which is why the third line below
    was easy to leave out of both.

    The first line is the address to type on the device, and it is the one
    value the reader has to carry across by hand -- an address typed with a
    mistake looks exactly like a server that is not running.

    Then the two things worth knowing that the address itself does not say.
    Both come after it, because both are caveats and a caveat printed ahead of
    the instruction delays the thing the reader opened the window to find.
    """
    for line in netident.describe(bind, port):
        print(line, flush=True)

    # Whether anything on this network can watch, which is about their network
    # rather than about a setting. Whoever wants a token will look for how, and
    # everyone else has just been told something true.
    if token is None:
        print("提示：本网络上的其他设备也能收看这台电脑转发的频道。", flush=True)

    # And that closing this window ends the stream. It is not obvious, it costs
    # the reader a television picture to get wrong, and there was nothing
    # anywhere that said it -- the window looks like a log, and a log is
    # something you close when you have finished reading it.
    #
    # A black screen on the device and a window that was tidied away are two
    # events a person has no reason to connect. Saying it here costs one line
    # and is the only place it can be said while the reader is still looking.
    print("提示：关掉这个窗口，服务就停止了，电视会中断。", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="generate a new local ten-second media set")
    prepare_parser.add_argument("--media-dir", required=True, type=Path)
    prepare_parser.add_argument("--ffmpeg", default="ffmpeg")
    import_parser = commands.add_parser("import-video", help="convert a local video clip for the device")
    import_parser.add_argument("--input", required=True, type=Path)
    import_parser.add_argument("--media-dir", required=True, type=Path)
    import_parser.add_argument("--seconds", type=int, default=60)
    import_parser.add_argument("--start", type=float, default=0)
    import_parser.add_argument("--ffmpeg", default="ffmpeg")
    live_parser = commands.add_parser("live", help="transcode one allowlisted public channel in real time")
    live_parser.add_argument("--channel", required=True, choices=sorted(CHANNELS))
    live_parser.add_argument("--ffmpeg", default="ffmpeg")
    # A diagnostic, not a feature: it exists so that "the picture starves the
    # sound" can be tested by removing one of them, rather than by reasoning
    # about which is to blame.
    live_parser.add_argument("--media", choices=("both", "audio", "video"),
                             default="both")
    # No default address. A device on the network cannot reach a server bound to
    # loopback, so "127.0.0.1" as a default meant every user had to supply this
    # -- and supplying it requires knowing the machine's own address, which is
    # the thing they came here not to look up. Omitted means "work it out".
    live_parser.add_argument("--bind", default=None,
                             help="LAN address to serve on (default: detected automatically)")
    live_parser.add_argument("--port", type=int, default=8096)
    live_parser.add_argument("--token-file", type=Path)
    live_parser.add_argument("--duration-seconds", type=int, default=1800)
    run_parser = commands.add_parser("run", help="serve prepared local video or synthetic media")
    run_parser.add_argument("--media-dir", required=True, type=Path)
    run_parser.add_argument("--bind", default=None,
                            help="LAN address to serve on (default: detected automatically)")
    run_parser.add_argument("--port", type=int, default=8096)
    run_parser.add_argument("--token-file", type=Path)
    run_parser.add_argument("--duration-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    # Both serving commands need a LAN address, and neither can be served on a
    # name. Worked out here so the two paths cannot disagree about it, and so the
    # value that gets printed is the one that was actually bound.
    if args.command in ("live", "run") and args.bind is None:
        args.bind = netident.lan_address()
        if args.bind is None:
            print("这台电脑好像没连上网络。接上网络再运行本程序。", file=sys.stderr)
            return 1
    try:
        if args.command == "prepare":
            media = prepare(args.media_dir, args.ffmpeg)
            print(f"已生成 10 秒素材，{len(media.frames)} 张画面，单张最大 {max(map(len, media.frames))} 字节。")
            return 0
        if args.command == "import-video":
            media = import_video(args.input, args.media_dir, args.seconds, args.start, args.ffmpeg)
            print(f"已导入 {media.duration_ms // 1000} 秒；{WIDTH}x{HEIGHT}，{FPS} 帧每秒，16 kHz 单声道。")
            return 0
        token = load_token(args.token_file)
        if args.command == "live":
            server = AVServer(None, token, args.bind, args.port, args.duration_seconds * 1000,
                              logger=lambda message: print(message, flush=True))
            server.live_enabled = True
            server.ffmpeg = args.ffmpeg
            server.media_filter = "" if args.media == "both" else args.media
            server.channel_name = args.channel
            # The handlers must set the flag the accept loop actually reads. A
            # separate local Event was never consulted by serve(), so Ctrl-C did
            # nothing and the only way out was a signal the default handler
            # turned into a traceback.
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: server.stop.set())
            # Each device connection starts its own transcode process, so a
            # channel switch is a new session and a dead ffmpeg only ends that
            # session. The accept loop itself never needs restarting.
            #
            # No channel count here. It said "Default channel ch000; 127
            # channels available", which is a line about the program's internal
            # state: the reader never chose ch000 and cannot act on the number.
            # What they need is the address below, and it was competing with
            # this for attention.
            # What to type on the device, then the two things the address does
            # not say. See print_where_to_connect.
            print_where_to_connect(args.bind, args.port, token)
            server.serve()
            # Was "Stopped: completed=0, failed=0, rejected=0, dropped_video=0."
            # -- four counters that mean nothing to whoever just pressed Ctrl-C,
            # and it is the last thing the program says. The counts are still
            # worth keeping for diagnosis, so they are printed only when
            # something actually went wrong; a clean exit says so in plain
            # words.
            #
            # Connections abandoned during the handshake are not part of that
            # condition. A device changing channel produces one every time, and
            # counting them as trouble made an ordinary end of run look as
            # though something had broken three times over -- the reader is
            # left holding a number with nothing to attach it to.
            if server.failed or server.rejected or server.dropped_video:
                print(f"已停止。出错 {server.failed} 次，拒绝 {server.rejected} 次，"
                      f"丢帧 {server.dropped_video} 次。", flush=True)
            else:
                print("已停止。", flush=True)
            return 0
        server = AVServer(Media.load(args.media_dir), token, args.bind, args.port,
                          args.duration_seconds * 1000, logger=lambda message: print(message, flush=True))
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: server.stop.set())
        print_where_to_connect(args.bind, args.port, token)
        server.serve()
        if server.failed or server.rejected or server.dropped_video:
            print(f"已停止：正常结束 {server.completed} 次，出错 {server.failed} 次，"
                  f"被拒绝 {server.rejected} 次，丢弃画面 {server.dropped_video} 帧。")
        else:
            print("已停止。", flush=True)
        return 0
    except OSError as error:
        # Bind failures are otherwise indistinguishable from a token or media
        # problem, and a stale listener on the same port looks like a silent
        # no-op. errno carries no paths or credentials.
        # errno.EADDRINUSE rather than the numbers: it is 48 on macOS and 98 on
        # Linux, and writing those out is how a check ends up covering one
        # platform and silently not the other.
        if error.errno == errno.EADDRINUSE:
            print("端口 8096 已被占用——多半是上一次的程序还没关干净。", file=sys.stderr)
            print("把之前的窗口关掉，或重启电脑后再试。", file=sys.stderr)
        else:
            print(f"网络端口打不开（errno={error.errno}）。", file=sys.stderr)
        return 1
    except Exception:
        # Subprocess errors/file paths/environment may carry sensitive values.
        print("程序没能启动。常见原因是 ffmpeg 缺失或频道表有问题；", file=sys.stderr)
        print("把上面最后几行输出发给项目的维护者可以定位。", file=sys.stderr)
        if os.environ.get("TV_TRACEBACK") == "1":
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
