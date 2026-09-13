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
from server.media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, FPS, FRAME_COUNT, HEIGHT,
                          Media, START_DELAY_MS, VIDEO_LEAD_MS, WIDTH, prepare,
                          schedule, synthetic_frame, validate_jpeg)
from server.protocol import (HEADER, Kind, Packet, ProtocolError, json_bytes,
                             json_object, receive_packet, send_packet)

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


def jpeg_metadata():
    # Deliberately metadata-only fixture, not claimed as an entropy-decodable JPEG.
    sof = b"\x08\x00\x78\x00\xa0\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8\xff\xc0" + struct.pack("!H", len(sof) + 2) + sof + b"\xff\xda\x00\x02\xff\xd9"


class ProtocolTests(unittest.TestCase):
    def test_header_is_exact_network_order_24_bytes(self):
        packet = Packet(Kind.PCM, 0x10203040, 2, 20, bytes(640))
        raw = packet.encode()
        self.assertEqual(HEADER.size, 24)
        self.assertEqual(raw[:24], bytes.fromhex("464156310103000010203040000000020000001400000280"))

    def test_fragmented_and_coalesced_stream(self):
        packets = [hello(), Packet(Kind.PCM, 3, 1, 0, bytes(640)), Packet(Kind.END, 3, 2, 20)]
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
        baseline = [b"FAV1", 1, 4, 0, 1, 0, 0, 12]
        for position, value in ((0, b"NOPE"), (1, 2), (2, 99), (3, 1),
                                (7, 24577), (7, 0), (7, 0xFFFFFFFF)):
            with self.subTest(position=position, value=value), socket_pair() as (sender, receiver):
                fields = baseline.copy()
                fields[position] = value
                sender.sendall(HEADER.pack(*fields))
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
                             (Kind.ERROR, CONTROL_MAX), (Kind.PCM, 640),
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
                send_packet(sender, Packet(Kind.JPEG, 1, 1, 0, bytes(24576)), 0.04)
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
            self.assertEqual(index, counts[kind] % (FRAME_COUNT if kind == 4 else 500))
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
        # Both streams run to the end of the 10100 ms window: one 20 ms chunk
        # more than 10 s of audio, and the frame whose slot starts at 10000 ms.
        self.assertEqual(audio, 10100 // AUDIO_CHUNK_MS)
        # A frame slot exists whenever its timestamp is under the window, so
        # count slots rather than scaling the window by the frame rate.
        self.assertEqual(video, sum(1 for i in range(10000)
                                    if i * 1000 // FPS < 10100))

    def test_jpeg_metadata_boundary_validation(self):
        raw = jpeg_metadata()
        validate_jpeg(raw)
        for invalid in (raw.replace(b"\xff\xc0", b"\xff\xc2"), raw[:-1],
                        raw.replace(b"\x01\x22", b"\x01\x21"),
                        raw.replace(b"\x00\x78\x00\xa0", b"\x00\xf0\x01\x40"),
                        raw.replace(b"\x00\x78\x00\xa0", b"\x00\x79\x00\xa0"),
                        raw.replace(b"\x00\x78\x00\xa0", b"\x00\x78\x00\xa1"),
                        raw + bytes(24576)):
            with self.assertRaises(ValueError):
                validate_jpeg(invalid)


class LiveServerTests(unittest.TestCase):
    def setUp(self):
        self.server = AVServer(Media(bytes(320000), (jpeg_metadata(),) * FRAME_COUNT),
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
                           "video_max_bytes": 24576, "sample_rate": 16000,
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
        self.assertEqual(sum(p.kind == Kind.JPEG for p in packets),
                         sum(1 for i in range(1000) if i * 1000 // FPS < 250))

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
            connection.close()
            time.sleep(0.05)

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
            self.assertLessEqual(max(map(len, media.frames)), 24576)
            for second in range(10):
                samples = struct.unpack("<16000h", media.pcm[second * 32000:(second + 1) * 32000])
                self.assertGreater(max(abs(value) for value in samples[:800]), 2000)
                self.assertEqual(max(abs(value) for value in samples[810:]), 0)
            self.assertEqual(media, Media.load(destination))
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual((manifest["width"], manifest["height"]), (160, 120))
            for index in range(FRAME_COUNT):
                self.assertEqual(len(synthetic_frame(index)), 160 * 120 * 3)
            decoded = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(destination / "frame-000.jpg"),
                 "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                capture_output=True, check=True, timeout=20).stdout
            self.assertEqual(len(decoded), 160 * 120 * 3)
            manifest.update(width=320, height=240)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                Media.load(destination)
            manifest.update(width=160, height=120)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(FileExistsError):
                prepare(destination)
            (destination / "frame-000.jpg").write_bytes(bytes(24577))
            with self.assertRaises(ValueError):
                Media.load(destination)


if __name__ == "__main__":
    unittest.main()
