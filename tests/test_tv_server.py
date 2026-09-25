#!/usr/bin/env python3
"""Host framing, authentication, scheduling and optional ffmpeg integration tests."""

from __future__ import annotations

import contextlib
import json
import subprocess
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.tv_server import (AVServer, TOKEN_ENV, authenticate, load_token,
                            local_ipv4)
from server import frames
from server.media import (AUDIO_BYTES, AUDIO_CHUNK_MS, AUDIO_LEAD_MS, DURATION_MS, FPS, FRAME_COUNT, HEIGHT, Media, START_DELAY_MS, VIDEO_LEAD_MS, WIDTH, prepare, schedule, synthetic_frame, validate_frame)
from server.protocol import (HEADER, Kind, Packet, ProtocolError, VIDEO_MAX,
                             json_bytes, json_object, receive_packet, send_packet)
from server.rate import RateController
from server.timeline import SessionClock

# Synthetic test-only credential, never a deployment credential.
TEST_TOKEN = b"host-test-only-not-a-real-secret"


def hello(token=TEST_TOKEN.decode()):
    return Packet(Kind.HELLO, 0, 0, 0, json_bytes({"version": 1, "token": token}))


@contextlib.contextmanager
def socket_pair():
    left, right = socket.socketpair()
    with left, right:
        left.setblocking(False)
        right.setblocking(False)
        yield left, right


def indexed_frame(fill=0):
    """A frame of one index value: the smallest thing that is still a frame.

    There is no header to forge and no marker to get wrong -- an indexed frame
    is exactly one byte a pixel -- so a fixture is just the right number of
    bytes, and a test that wants a different picture changes the byte.
    """
    return bytes((fill,)) * frames.FRAME_PIXELS


class ProtocolTests(unittest.TestCase):
    def test_header_is_exact_network_order_24_bytes(self):
        packet = Packet(Kind.PCM, 0x10203040, 2, 20, bytes(AUDIO_BYTES))
        raw = packet.encode()
        self.assertEqual(HEADER.size, 24)
        # The magic, the version, the kind, the reserved byte and the flags are
        # literals because they are the wire format and must not drift. The
        # length is built from the definition instead: writing it out as a
        # constant makes this test fail whenever a payload size changes, which
        # says nothing about whether the header is still in network order --
        # the property it exists to check.
        self.assertEqual(raw[:20], bytes.fromhex("4641563101030000102030400000000200000014"))
        self.assertEqual(int.from_bytes(raw[20:24], "big"), AUDIO_BYTES)

    def test_fragmented_and_coalesced_stream(self):
        packets = [hello(), Packet(Kind.PCM, 3, 1, 0, bytes(AUDIO_BYTES)), Packet(Kind.END, 3, 2, 20)]
        raw = b"".join(packet.encode() for packet in packets)
        with socket_pair() as (sender, receiver):
            def fragment():
                for offset in range(0, len(raw), 7):
                    sender.sendall(raw[offset:offset + 7])
                    time.sleep(0.0001)
            thread = threading.Thread(target=fragment)
            thread.start()
            try:
                self.assertEqual([receive_packet(receiver, 1) for _ in packets], packets)
            finally:
                thread.join()

    def test_rejects_invalid_headers_before_body(self):
        # position 3 is the flags byte. The baseline kind is 4, which is the
        # only kind allowed to carry a flag -- and only the value 1 -- so a
        # flag on any other kind, or any other value, is refused before the
        # body is read. A zero flag is the ordinary case and is not an error.
        baseline = [b"FAV1", 1, 4, 0, 1, 0, 0, 12]
        for position, value in ((0, b"NOPE"), (1, 2), (2, 99),
                                (7, 24577), (7, 0xFFFFFFFF)):
            with self.subTest(position=position, value=value), socket_pair() as (sender, receiver):
                fields = baseline.copy()
                fields[position] = value
                sender.sendall(HEADER.pack(*fields))
                with self.assertRaises(ProtocolError):
                    receive_packet(receiver)
        # A flag on a kind that has no flags is refused.
        for kind in (2, 3, 5, 6):
            with self.subTest(kind=kind), socket_pair() as (sender, receiver):
                sender.sendall(HEADER.pack(b"FAV1", 1, kind, 1, 1, 0, 0, 0))
                with self.assertRaises(ProtocolError):
                    receive_packet(receiver)
        # An undefined flag bit on video is refused too.
        with socket_pair() as (sender, receiver):
            sender.sendall(HEADER.pack(b"FAV1", 1, 4, 0x80, 1, 0, 0, 12))
            with self.assertRaises(ProtocolError):
                receive_packet(receiver)
        with socket_pair() as (sender, receiver):
            sender.sendall(HEADER.pack(b"FAV1", 1, 4, 0, 2, 0, 0, 10))
            with self.assertRaisesRegex(ProtocolError, "session"):
                receive_packet(receiver, expected_session=1)

    def test_payload_boundaries(self):
        # Derived from the limits, so raising the control ceiling to carry a long
        # channel list does not leave this test asserting the old boundary.
        from server.protocol import CONTROL_MAX, VIDEO_MAX
        for kind, length in ((Kind.HELLO, CONTROL_MAX), (Kind.CONFIG, CONTROL_MAX),
                             (Kind.ERROR, CONTROL_MAX), (Kind.PCM, AUDIO_BYTES),
                             (Kind.JPEG, VIDEO_MAX), (Kind.END, 0)):
            Packet(kind, 1, 1, 1, bytes(length)).encode()
        for kind, length in ((Kind.HELLO, CONTROL_MAX + 1), (Kind.ERROR, 0),
                             (Kind.PCM, 639), (Kind.PCM, 641),
                             (Kind.JPEG, VIDEO_MAX + 1), (Kind.END, 1)):
            with self.subTest(kind=kind, length=length), self.assertRaises(ProtocolError):
                Packet(kind, 1, 1, 1, bytes(length)).encode()
        for value in (-1, 2**32):
            with self.assertRaises(ProtocolError):
                Packet(Kind.END, 1, 1, value).encode()

    def test_eof_timeout_and_no_magic_resync(self):
        with socket_pair() as (sender, receiver):
            sender.sendall(b"FA")
            sender.close()
            with self.assertRaises(EOFError):
                receive_packet(receiver)
        with socket_pair() as (sender, receiver):
            sender.sendall(b"F")
            before = time.monotonic()
            with self.assertRaises(TimeoutError):
                receive_packet(receiver, 0.04)
            self.assertLess(time.monotonic() - before, 0.2)
        with socket_pair() as (sender, receiver):
            sender.sendall(b"garbage!" + hello().encode())
            with self.assertRaises(ProtocolError):
                receive_packet(receiver)

    def test_total_receive_deadline_not_reset_by_fragments(self):
        with socket_pair() as (sender, receiver):
            def trickle():
                for byte in hello().encode()[:12]:
                    sender.send(bytes([byte]))
                    time.sleep(0.015)
            thread = threading.Thread(target=trickle)
            thread.start()
            try:
                before = time.monotonic()
                with self.assertRaises(TimeoutError):
                    receive_packet(receiver, 0.05)
                self.assertLess(time.monotonic() - before, 0.15)
            finally:
                thread.join()

    def test_nonreading_peer_has_bounded_send(self):
        with socket_pair() as (sender, receiver):
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            try:
                while True:
                    sender.send(bytes(4096))
            except BlockingIOError:
                pass
            before = time.monotonic()
            with self.assertRaises(TimeoutError):
                send_packet(sender, Packet(Kind.JPEG, 1, 1, 0, bytes(VIDEO_MAX)), 0.04)
            self.assertLess(time.monotonic() - before, 0.2)


class AuthenticationTests(unittest.TestCase):
    def test_valid_hello_and_rejections(self):
        authenticate(hello(), TEST_TOKEN)
        for packet in (hello("wrong-but-long-enough"), hello("x"),
                       Packet(Kind.HELLO, 9, 0, 0, hello().payload),
                       Packet(Kind.HELLO, 0, 1, 0, hello().payload),
                       Packet(Kind.HELLO, 0, 0, 0, b'{"version":true,"token":"x"}'),
                       Packet(Kind.HELLO, 0, 0, 0, b'{"version":2,"token":"x"}'),
                       Packet(Kind.HELLO, 0, 0, 0, b'[]'),
                       Packet(Kind.HELLO, 0, 0, 0, b'\xff')):
            with self.assertRaises(ProtocolError):
                authenticate(packet, TEST_TOKEN)
        with self.assertRaises(ProtocolError):
            json_object(b'{"version":1,"version":1}')

    def test_environment_and_restricted_token_file(self):
        # Taken from the module rather than written out, so that renaming the
        # variable cannot leave this test quietly checking a name the server
        # no longer reads -- which is what happened when the server side was
        # renamed and this line kept the old spelling.
        with patch.dict(os.environ, {TOKEN_ENV: TEST_TOKEN.decode()}, clear=True):
            self.assertEqual(load_token(), TEST_TOKEN)
            with self.assertRaises(ValueError):
                load_token(Path("unused"))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / "pairing-token"
            path.write_bytes(TEST_TOKEN + b"\n")
            path.chmod(0o600)
            self.assertEqual(load_token(path), TEST_TOKEN)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_token(path)
            path.chmod(0o600)
            symlink = path.parent / "link"
            symlink.symlink_to(path)
            with self.assertRaises(OSError):
                load_token(symlink)
            path.write_bytes(b"x" * 200)
            with self.assertRaises(ValueError):
                load_token(path)

    def test_no_configured_token_means_no_token_required(self):
        """A device set up without a token must still be served.

        The device sends whatever it was given, which is nothing when the setup
        page was not used to give it one. Refusing that would make the server
        unusable for exactly the devices it is meant to serve, so an unset token
        has to mean "do not check" rather than "reject everything".
        """
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_token())
        # The opening HELLO is still inspected: no token required is not the
        # same as no protocol.
        authenticate(hello(), None)
        for packet in (Packet(Kind.HELLO, 9, 0, 0, hello().payload),
                       Packet(Kind.HELLO, 0, 1, 0, hello().payload),
                       Packet(Kind.HELLO, 0, 0, 0, b'{"version":2,"token":"x"}'),
                       Packet(Kind.HELLO, 0, 0, 0, b'[]')):
            with self.assertRaises(ProtocolError):
                authenticate(packet, None)

    def test_a_configured_token_is_still_enforced(self):
        """Setting a token must keep working exactly as it did."""
        authenticate(hello(), TEST_TOKEN)
        with self.assertRaises(ProtocolError):
            authenticate(hello("wrong-but-long-enough"), TEST_TOKEN)
        # A device that was given no token cannot get in once one is required.
        with self.assertRaises(ProtocolError):
            authenticate(hello(""), TEST_TOKEN)

    def test_lan_only_addresses(self):
        for address in ("127.0.0.1", "192.168.1.10", "10.0.0.2", "172.16.0.2"):
            self.assertTrue(local_ipv4(address))
        for address in ("0.0.0.0", "8.8.8.8", "224.0.0.1", "::1", "example.org"):
            self.assertFalse(local_ipv4(address))


class ScheduleTests(unittest.TestCase):
    def test_thirty_minutes_monotonic_and_exact_counts(self):
        # Derived from the media constants so a frame-rate change does not need
        # this arithmetic rewritten by hand.
        duration_ms = 1800000
        audio_count = duration_ms // AUDIO_CHUNK_MS
        video_count = duration_ms * FPS // 1000
        # The last slot of each stream falls one step short of the duration.
        last_audio_pts = (audio_count - 1) * AUDIO_CHUNK_MS
        last_video_pts = (video_count - 1) * 1000 // FPS
        counts, previous, last_due = {3: 0, 4: 0}, {3: -1, 4: -1}, -AUDIO_LEAD_MS - 1
        for due, kind, pts, index in schedule(duration_ms):
            self.assertGreaterEqual(due, last_due)
            self.assertGreater(pts, previous[kind])
            self.assertEqual(pts - due, AUDIO_LEAD_MS if kind == 3 else VIDEO_LEAD_MS)
            # The index is the slot's position within one loop of the media, so
            # its period is the media's own length in chunks -- not a second's
            # worth. Deriving it from the same constants the scheduler uses is
            # what keeps this test about the schedule rather than about 20 ms.
            audio_cycle = DURATION_MS // AUDIO_CHUNK_MS
            self.assertEqual(
                index, counts[kind] % (FRAME_COUNT if kind == 4 else audio_cycle))
            counts[kind] += 1
            previous[kind], last_due = pts, due
        self.assertEqual(counts, {3: audio_count, 4: video_count})
        self.assertEqual(previous, {3: last_audio_pts, 4: last_video_pts})

    def test_shared_loop_origin_and_interleaved_streams(self):
        """Both streams share one origin and both must reach the end of the run.

        Equal leads mean the merge alternates rather than letting one type run
        to completion first, so a video slot is never permanently behind an
        audio slot. The earlier assertion here demanded the opposite (that wire
        PTS went backwards across a type change), which only happened while the
        leads differed and video was being starved.
        """
        events = list(schedule(10100))
        for kind in (3, 4):
            self.assertTrue(any(k == kind and pts == 10000 and index == 0
                                for _, k, pts, index in events))
        audio = sum(1 for _, k, _, _ in events if k == 3)
        video = sum(1 for _, k, _, _ in events if k == 4)
        # Both streams run to the end of the 10100 ms window: every chunk whose
        # slot starts inside it, and the frame whose slot starts at 10000 ms.
        # Rounded up rather than down, because the window is 10100 ms and the
        # chunk is not a divisor of it -- a chunk starting at 10080 is inside.
        self.assertEqual(audio, -(-10100 // AUDIO_CHUNK_MS))
        # A frame slot exists whenever its timestamp is under the window, so
        # count slots rather than scaling the window by the frame rate.
        self.assertEqual(video, sum(1 for i in range(10000)
                                    if i * 1000 // FPS < 10100))

    def test_frame_length_is_the_whole_validation(self):
        """A frame is one byte a pixel; anything else is refused.

        There is no structure to walk, so the length is the entire check -- and
        it has to be exact, because a short frame would be drawn as a torn
        picture rather than rejected somewhere downstream.
        """
        raw = indexed_frame(7)
        validate_frame(raw)
        for invalid in (raw[:-1], raw + bytes(1), bytes(frames.FRAME_PIXELS - 1),
                        bytes(frames.FRAME_PIXELS + 1), b""):
            with self.assertRaises(ValueError):
                validate_frame(invalid)


class LiveServerTests(unittest.TestCase):
    def setUp(self):
        self.server = AVServer(Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT),
                               TEST_TOKEN, port=0, duration_ms=250)
        self.errors = []
        def serve():
            try:
                self.server.serve()
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.assertTrue(self.server.ready.wait(2))

    def tearDown(self):
        self.server.stop.set()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def connect(self):
        connection = socket.create_connection(("127.0.0.1", self.server.port), timeout=1)
        connection.setblocking(False)
        self.addCleanup(connection.close)
        return connection

    def test_handshake_stream_and_end(self):
        connection = self.connect()
        send_packet(connection, hello())
        config = receive_packet(connection, 1)
        self.assertEqual(config.kind, Kind.CONFIG)
        config_json = json_object(config.payload)
        self.assertEqual(config_json["session"], config.session)
        for key, value in {"width": WIDTH, "height": HEIGHT, "fps": FPS,
                           "video_max_bytes": frames.VIDEO_MAX,
                           "stripe_rows": frames.STRIPE_ROWS,
                           "sample_rate": 16000,
                           "channels": 1, "sample_bits": 16,
                           "audio_chunk_ms": AUDIO_CHUNK_MS,
                           "start_delay_ms": START_DELAY_MS,
                           "audio_lead_ms": AUDIO_LEAD_MS,
                           "video_lead_ms": VIDEO_LEAD_MS}.items():
            self.assertEqual(config_json[key], value)
        self.assertNotEqual(config.session, 0)
        packets = []
        while True:
            packet = receive_packet(connection, 1, config.session)
            packets.append(packet)
            if packet.kind == Kind.END:
                break
        self.assertEqual([p.seq for p in packets], list(range(1, len(packets) + 1)))
        # The server serves a 250 ms window here. Count slots the way the
        # scheduler defines them -- a slot exists while its timestamp is under
        # the window -- rather than scaling the window, which rounds differently
        # because the first chunk starts at 0.
        self.assertEqual(sum(p.kind == Kind.PCM for p in packets),
                         sum(1 for i in range(1000) if i * AUDIO_CHUNK_MS < 250))
        # One frame is several packets, so the count is the frame slots times
        # the packets a frame takes. Derived from the frame that is actually
        # streamed rather than from a constant: how many packets a frame needs
        # depends on how well it compressed, which is a property of the picture
        # and not of any number set in this repository.
        frame_slots = sum(1 for i in range(1000) if i * 1000 // FPS < 250)
        packets_per_frame = len(frames.frame_packets(indexed_frame(9)))
        self.assertEqual(sum(p.kind == Kind.JPEG for p in packets),
                         frame_slots * packets_per_frame)
        # Every packet of a frame but the first is marked as continuing it, and
        # each frame starts a new one.
        video = [p for p in packets if p.kind == Kind.JPEG]
        self.assertEqual(sum(p.flags == 0 for p in video), frame_slots)

    def test_bad_authentication_never_receives_config(self):
        connection = self.connect()
        send_packet(connection, hello("invalid-credential-value"))
        with self.assertRaises((EOFError, ConnectionResetError)):
            receive_packet(connection, 1)

    def test_busy_client_rejected_and_stop_session(self):
        connection = self.connect()
        send_packet(connection, hello())
        config = receive_packet(connection, 1)
        second = self.connect()
        with self.assertRaises((EOFError, ConnectionResetError)):
            receive_packet(second, 1)
        send_packet(connection, Packet(Kind.END, config.session, 1, 0))
        # Already buffered media may precede EOF.
        with self.assertRaises((EOFError, ConnectionResetError)):
            while True:
                receive_packet(connection, 1)
        self.assertGreaterEqual(self.server.rejected, 1)

    def test_session_mismatch_closes_connection(self):
        connection = self.connect()
        send_packet(connection, hello())
        config = receive_packet(connection, 1)
        send_packet(connection, Packet(Kind.END, config.session ^ 1, 1, 0))
        with self.assertRaises((EOFError, ConnectionResetError)):
            while True:
                receive_packet(connection, 1)

    def test_a_connection_dropped_at_the_handshake_is_not_a_failure(self):
        """The ordinary outcome of changing channel, counted honestly.

        Three things produce this and none of them is a fault: the device
        switching channel, the device going to sleep, and anything on the
        network checking whether the port is open. Each opens a connection and
        closes it without a word. Counted as failures, they made a healthy run
        end with "失败 3 次" -- a number the reader has no way to place, printed
        as the last thing the program says.

        Distinguished by how far the session got rather than by which exception
        was raised: a peer that leaves during the handshake never authenticated,
        so nothing had happened yet that could fail.
        """
        connection = self.connect()
        connection.close()
        # The server notices on its next read. Nothing is asserted about when,
        # only about the account it ends up keeping.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.abandoned == 0:
            time.sleep(0.05)
        self.assertEqual(self.server.abandoned, 1)
        self.assertEqual(self.server.failed, 0,
                         "a dropped handshake was counted as a server fault")

    def test_changing_channel_repeatedly_leaves_only_clean_ends(self):
        """What the device actually does, and what it should cost.

        A channel change is an END on the live session followed by a new
        connection -- the protocol has no other way to switch, and the server
        accepts nothing else once a session is running. So this happens every
        time someone presses UP, and none of it is a fault.

        It is checked because a plausible-looking tidiness in the server works
        against it: any connection left waiting in the backlog when a session
        ends is closed and counted, which is right for a straggler from the old
        session and wrong for a device that has already reconnected. Which of
        the two it is cannot be told from the socket, so the honest account is
        the one this asserts -- a completed session with nothing against it.
        """
        for _ in range(5):
            connection = self.connect()
            send_packet(connection, hello())
            config = receive_packet(connection, 1)
            send_packet(connection, Packet(Kind.END, config.session, 1, 0))
            # Let the END be read before dropping the connection, which is what
            # the device does: it sends END and waits to be let go. Closing the
            # instant after sending puts the END and the FIN in the same
            # instant, and the server may then be part way through a frame --
            # several writes now, not one -- and see the departure as a broken
            # pipe rather than as the request it was. That race exists in the
            # server too and is handled there; the test should not manufacture
            # it five times over and then complain that the count is wrong.
            time.sleep(0.05)
            connection.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.completed < 5:
            time.sleep(0.05)
        self.assertEqual(self.server.completed, 5)
        self.assertEqual(self.server.failed, 0)
        self.assertEqual(
            self.server.rejected, 0,
            "换台被记成了「拒绝」；这个数字会出现在程序退出时的那行提示里，"
            "用户看到的是一次正常的换台变成了一次故障")

    def test_a_session_that_breaks_after_authentication_is_a_failure(self):
        """The other side of the same line, so the change cannot be a pretext
        for counting nothing at all."""
        connection = self.connect()
        send_packet(connection, hello())
        config = receive_packet(connection, 1)
        self.assertNotEqual(config.session, 0)
        # Authenticated and streaming; now vanish mid-session.
        connection.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.failed == 0:
            time.sleep(0.05)
        self.assertEqual(self.server.failed, 1)
        self.assertEqual(self.server.abandoned, 0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg optional for offline preparation only")
class PreparationTests(unittest.TestCase):
    def test_generate_ten_second_shared_timeline_and_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "media"
            media = prepare(destination)
            self.assertEqual(len(media.frames), FRAME_COUNT)
            self.assertEqual(len(media.pcm), 320000)
            # Every frame is exactly one byte a pixel; there is no quality to
            # trade and no ceiling to stay under.
            self.assertTrue(all(len(f) == frames.FRAME_PIXELS for f in media.frames))
            for second in range(10):
                samples = struct.unpack("<16000h", media.pcm[second * 32000:(second + 1) * 32000])
                self.assertGreater(max(abs(value) for value in samples[:800]), 2000)
                self.assertEqual(max(abs(value) for value in samples[810:]), 0)
            self.assertEqual(media, Media.load(destination))
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual((manifest["width"], manifest["height"]),
                             (frames.WIDTH, frames.HEIGHT))
            for index in range(FRAME_COUNT):
                self.assertEqual(len(synthetic_frame(index)), frames.FRAME_PIXELS)
            # The stored frame is what the device draws: indices, one a pixel.
            stored = (destination / "frame-000.idx").read_bytes()
            self.assertEqual(len(stored), frames.FRAME_PIXELS)
            # Two different counts must differ on screen, or the generator is
            # drawing the same picture every frame and the test is looking at
            # something that would never move.
            self.assertNotEqual(synthetic_frame(0), synthetic_frame(1))
            manifest.update(width=16, height=16)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                Media.load(destination)
            manifest.update(width=frames.WIDTH, height=frames.HEIGHT)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(FileExistsError):
                prepare(destination)
            # A frame of the wrong length is refused at load, so a truncated
            # file is an error rather than a torn picture on the panel.
            (destination / "frame-000.idx").write_bytes(bytes(frames.FRAME_PIXELS + 1))
            with self.assertRaises(ValueError):
                Media.load(destination)


class WaitingForTheSoundTests(unittest.TestCase):
    """What the send loop does while there is a picture queue but no frame due.

    `pop_video()` answers "the frame whose content matches the sound at the head
    of the queue", so on a channel whose decoder is behind the audio there are
    passes with a non-empty video queue and nothing to send. That is a normal
    state, and the loop has to keep doing everything else while it lasts: wait,
    update the controller, and print its periodic report. The three are exactly
    what notices a stream going wrong, and they were the three being skipped.

    The defect this covers was found from outside with a controlled clock: 500
    attempts to take a frame, and **zero** waits, zero controller updates and
    zero reports over more than five seconds.
    """

    def test_no_frame_due_still_updates_the_controller_and_waits(self):
        server = AVServer(Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT),
                          TEST_TOKEN, port=0, duration_ms=250)
        sent = []

        class WaitingChannel:
            """A picture queue that is never ready, and a sound that always is.

            Frames are `pending` so the loop takes the `send_video` branch and
            reaches `pop_video`; `pop_video` then answers None, which is the
            state under test. Audio is held back so the branch that sends it is
            not what keeps the loop busy.
            """
            video = [1, 2, 3]
            audio = []
            # The pop interface changed shape: a pop now answers with the
            # payload AND the session timestamp it goes on the wire with, since
            # the timestamp is decided where the item is chosen. A stub that
            # still returned a bare payload would be exercising last round's
            # sender.
            session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)

            def pop_video(self, keep=0):
                return None

            def video_pending(self):
                return True

            def audio_pending(self):
                return False

            def pop_audio(self):
                return b"\x00" * AUDIO_BYTES, self.session_clock.audio(0.0)

            def has_data(self):
                return True

            def failure(self):
                return None

            def picture_lag_s(self):
                return 0.0

            dropped_video = 0

        server.stop = threading.Event()
        observes = []
        real_observe = RateController.observe

        def counting_observe(controller, *args, **kwargs):
            observes.append(1)
            return real_observe(controller, *args, **kwargs)

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.setblocking(False)

        def stop_after_a_while():
            time.sleep(1.2)
            server.stop.set()

        threading.Thread(target=stop_after_a_while, daemon=True).start()
        with patch.object(RateController, "observe", counting_observe):
            started = time.monotonic()
            server._pace_live(left, WaitingChannel(), 1)
            elapsed = time.monotonic() - started
        # The loop ran for the whole second and a bit rather than returning at
        # once, so the wait is real and not a spin that falls out on the first
        # pass.
        self.assertGreater(elapsed, 1.0)
        # And the controller was consulted, which is what was not happening.
        self.assertGreaterEqual(len(observes), 1)


if __name__ == "__main__":
    unittest.main()
