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

from . import netident
from .live import (CHANNELS, CHANNEL_AGENTS, DEFAULT_CHANNEL, LiveChannel, LiveError,
                   AUDIO_LATE_RESET_MS, AUDIO_MAX_LOOKAHEAD_MS,
                   PREBUFFER_TIMEOUT_S, VIDEO_LATE_DROP_MS, channel_list)
# Timing constants come from media.py, which both senders share: live.py imports
# them from there too, so re-exporting them from live would invite the same
# copy-that-drifts problem the leads already suffered from.
from .media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, DURATION_MS, FPS, HEIGHT,
                    START_DELAY_MS, VIDEO_LEAD_MS, WIDTH, Media, import_video,
                    prepare, schedule)
from .protocol import (AUDIO_BYTES, IO_TIMEOUT, Kind, Packet, ProtocolError,
                       json_bytes, json_object, receive_packet, send_packet)

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
CONFIG = {"width": WIDTH, "height": HEIGHT, "fps": FPS, "sample_rate": 16000,
          "channels": 1, "sample_bits": 16, "audio_chunk_ms": AUDIO_CHUNK_MS,
          "video_max_bytes": 24576, "duration_ms": DURATION_MS,
          "start_delay_ms": START_DELAY_MS, "audio_lead_ms": AUDIO_LEAD_MS,
          "video_lead_ms": VIDEO_LEAD_MS}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))


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
        self.channel_name = ""
        self.live_note = ""
        self.ffmpeg = "ffmpeg"
        # Injectable so the pacing loop can be tested without ffmpeg or network.
        self.channel_factory = LiveChannel
        self.logger = logger or (lambda message: None)

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.port))
            self.port = listener.getsockname()[1]
            listener.listen(1)
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
                    # Large enough that a whole JPEG (up to 24 KiB) fits in the
                    # free space, so the write completes instead of blocking
                    # partway and timing out mid-packet, which would corrupt the
                    # stream. The kernel doubles this value.
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
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

    def _wait_until(self, connection: socket.socket, target: float, session: int) -> bool:
        assert self.listener is not None
        self.phase = "schedule_wait"
        while not self.stop.is_set():
            delay = max(0.0, min(0.05, target - time.monotonic()))
            readable = select.select([connection, self.listener], [], [], delay)[0]
            if self.listener in readable:
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
            # start() is inside the try: a failing Popen would otherwise leak its
            # pipes and stderr file on every reconnect attempt.
            channel.start()
            self.live_channel = channel
            session = secrets.randbelow(0xFFFFFFFF) + 1
            self.session_id = session
            # Answer the handshake before waiting for the prebuffer. Each
            # connection starts a fresh ffmpeg, and its first media can take
            # seconds; the device abandons the handshake after 2 s, so waiting
            # first meant it never saw CONFIG and dropped every session.
            send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                           json_bytes(dict(CONFIG, session=session,
                                                           channel=channel_id,
                                                           channel_list=channel_list()))))
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
            self._pace_live(connection, channel, session)
        finally:
            # Capture ffmpeg's diagnostics before dropping the reference: the
            # caller reports them after this returns, and clearing first made
            # every live failure log an empty note. Only a real failure is worth
            # reporting; a normal channel switch would otherwise log warnings.
            if channel.failure() is not None:
                self.live_note = channel.diagnostics()
            channel.close()
            self.live_channel = None

    def _pace_live(self, connection: socket.socket, channel: LiveChannel,
                   session: int) -> None:
        # The origin must not precede "now" by more than the audio lead: ffmpeg
        # fills its queues far faster than real time, so a past-dated origin
        # would make the first slots due immediately and flush a burst of audio
        # the device cannot buffer, followed by silence long enough to underrun.
        origin = time.monotonic() + max(START_DELAY_MS, AUDIO_LEAD_MS) / 1000
        audio_pts = 0
        video_pts = 0
        # Count frames and derive the timestamp instead of adding 1000 // FPS
        # each time: at 6 fps that step is 166 ms and loses 0.67 ms per frame,
        # about 2.4 s per hour, which the device would see as picture drifting
        # away from the audio clock it aligns to.
        video_index = 0
        seq = 1
        last_report = 0.0
        while not self.stop.is_set():
            if channel.failure():
                raise LiveError("transcode stopped")
            now = time.monotonic()
            video_pts = video_index * 1000 // FPS
            audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            # The frame's due time hangs off the audio position, not off the
            # origin. Both media already carry timestamps on one shared origin,
            # and the device aligns pictures to its audio clock; deriving the due
            # time from the origin instead let video keep its own schedule while
            # back-pressure moved audio to a slower one, so pictures ran ahead of
            # sound. Anchored here, a frame is due exactly when the audio it
            # accompanies is, and lagging audio delays the picture with it.
            video_at = audio_at + (video_pts - audio_pts) / 1000
            # Audio owns the timeline and is never dropped. Video is matched to
            # it, but must be sent when its own slot comes due: waiting for the
            # audio queue to drain first would starve video permanently.
            # Cap how far audio may run ahead of the wall clock. Without this a
            # single slow loop iteration lets the sender flush a burst the
            # device's 400 ms PCM queue cannot hold, then fall silent.
            audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000
            # The socket must accept the write now. A blocked socket means the
            # device has filled its queue and stopped reading, which is exactly
            # the back-pressure that keeps this sender on the device's clock.
            # A due chunk that cannot be written is simply left for the next
            # iteration -- audio is never dropped, so neither audio_pts nor seq
            # advances -- and the loop retries a few milliseconds later. Waiting
            # here instead, or failing on a timeout, turned ordinary back-pressure
            # into an ended session.
            writable = bool(select.select([], [connection], [], 0)[1])
            send_audio = (now >= audio_at and channel.audio_pending()
                          and audio_lookahead_ms <= AUDIO_MAX_LOOKAHEAD_MS
                          and writable)
            send_video = now >= video_at and channel.video_pending()
            if send_audio and (not send_video or audio_at <= video_at):
                block = channel.pop_audio()
                self.phase = "live_pcm_send"
                send_packet(connection, Packet(Kind.PCM, session, seq, audio_pts, block))
                audio_pts += AUDIO_CHUNK_MS
                seq += 1
                self.audio_sent += 1
            elif send_video:
                frame = channel.pop_video()
                if now - video_at > VIDEO_LATE_DROP_MS / 1000:
                    # Too late to be useful, so discard it. The index must then
                    # jump to the slot belonging to the current moment: letting
                    # it advance one frame at a time left it permanently behind
                    # the clock, and every later frame was therefore also late.
                    # The device rendered nothing at all in that state.
                    self.dropped_video += 1
                    behind_ms = (now - video_at) * 1000
                    skipped = int(behind_ms * FPS // 1000) + 1
                    video_index += skipped
                    # Those skipped slots already have frames sitting in the
                    # queue. Leaving them there would send stale pictures under
                    # fresh timestamps, so drop them with the slots.
                    for _ in range(skipped - 1):
                        if channel.pop_video() is None:
                            break
                        self.dropped_video += 1
                elif writable:
                    # The same probe the audio branch uses. A frame that cannot
                    # start now is dropped rather than attempted: a send that
                    # cannot finish inside the protocol timeout would block this
                    # single-threaded loop and stop audio with it, and a partial
                    # packet must never be left on the wire.
                    self.phase = "live_jpeg_send"
                    send_packet(connection, Packet(Kind.JPEG, session, seq, video_pts, frame))
                    seq += 1
                    self.video_sent += 1
                    video_index += 1
                else:
                    # Congested: give the slot up so audio keeps the timeline.
                    self.dropped_video += 1
                    video_index += 1
            else:
                # Nothing is due yet. Sleep briefly instead of blocking until the
                # next deadline: a long wait here would not notice inbound END or
                # disconnect, and a past deadline would spin.
                if not channel.has_data():
                    # An empty queue on a single pass is the normal shape of an
                    # HLS origin: segments arrive in bursts, so ffmpeg emits a
                    # segment's worth of audio and then waits for the next one.
                    # Only a sustained gap is a real fault, and lateness has to be
                    # measured from here rather than from the timestamp taken at
                    # the top of the loop, which is stale by the time we look.
                    empty_since = time.monotonic()
                    while not channel.has_data():
                        if channel.failure():
                            raise LiveError("transcode stopped")
                        if time.monotonic() - empty_since > AUDIO_LATE_RESET_MS / 1000:
                            raise TimeoutError("live audio starved; reconnect for a new origin")
                        if not self._wait_until(connection, time.monotonic() + 0.02, session):
                            return
                else:
                    time.sleep(0.005)
            if now - last_report >= 5:
                self.logger(f"DEPTH t={now-origin:6.1f}s audio={len(channel.audio):5d} "
                            f"video={len(channel.video):5d} la={audio_lookahead_ms:7.0f} "
                            f"sent_a={self.audio_sent} prod_drop={channel.skipped_audio}")
                last_report = now
            if now - last_report >= 10:
                last_report = now
                self.logger(f"session={session} state=live pts_ms={audio_pts} "
                            f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                            f"dropped_video_total={self.dropped_video} "
                            f"transcode_dropped={channel.dropped_video}")
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
        origin = time.monotonic() + 0.2
        seq = 1
        for due_ms, kind, pts, index in schedule(self.duration_ms, self.media.duration_ms):
            if not self._wait_until(connection, origin + due_ms / 1000, session):
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
                payload = self.media.audio_at(index)
            else:
                payload = self.media.frame_at(index)
            self.phase = "pcm_send" if kind == Kind.PCM else "jpeg_send"
            send_packet(connection, Packet(Kind(kind), session, seq, pts, payload))
            if kind == Kind.PCM:
                self.audio_sent += 1
            else:
                self.video_sent += 1
            if self.audio_sent and self.audio_sent % 500 == 0 and kind == Kind.PCM:
                self.logger(f"session={session} state=streaming pts_ms={pts} "
                            f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                            f"dropped_video_total={self.dropped_video}")
            seq += 1
        if self._wait_until(connection, origin + self.duration_ms / 1000, session):
            self.phase = "end_send"
            send_packet(connection, Packet(Kind.END, session, seq, self.duration_ms))


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
            # What to type on the device, printed rather than left to be looked
            # up. This is the one value the reader has to carry across by hand,
            # and it is the step people get wrong -- an address typed with a
            # mistake looks identical to a server that is not running.
            for line in netident.describe(args.bind, args.port):
                print(line, flush=True)
            # After the address, not before it. This is a caveat, and a caveat
            # printed ahead of the instruction delays the one thing the reader
            # opened the window to find. One line, and about what it means
            # rather than the name of a setting: whoever wants a token will look
            # for how, and everyone else has just been told something true about
            # their own network.
            if token is None:
                print("提示：本网络上的其他设备也能收看这台电脑转发的频道。", flush=True)
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
        for line in netident.describe(args.bind, args.port):
            print(line, flush=True)
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
