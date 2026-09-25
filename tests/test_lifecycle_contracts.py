#!/usr/bin/env python3
"""Lifecycle and resource leak regression tests for LiveChannel and AVServer."""

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server.live as live
from server import frames
from server.media import FRAME_COUNT, Media
from server.protocol import (
    Kind,
    Packet,
    json_bytes,
    receive_packet,
    send_packet,
)
from server.tv_server import AVServer

TEST_TOKEN = b"test-token-lifecycle-contracts-1234"


def hello(token=TEST_TOKEN.decode()):
    return Packet(Kind.HELLO, 0, 0, 0, json_bytes({"version": 1, "token": token}))


def indexed_frame(fill=0):
    return bytes([fill]) * (320 * 240)


def get_open_fd_count() -> int:
    """Return number of open file descriptors for the current process on macOS/Linux."""
    dev_fd = Path("/dev/fd")
    if dev_fd.exists():
        try:
            return len(list(dev_fd.iterdir()))
        except OSError:
            pass
    return 0


class LifecycleContractsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        assert cls.ffmpeg, "ffmpeg must be available in PATH"

    def test_real_ffmpeg_start_then_close(self):
        """Real FFmpeg starts, feeds, and close() cleanly reaps the child."""
        with tempfile.TemporaryDirectory(prefix="tv-lifecycle-") as folder:
            source = Path(folder) / "test_src.mkv"
            subprocess.run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x240:rate=12:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=16000:duration=2",
                    "-c:v",
                    "ffv1",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=True,
                timeout=15,
            )

            url = str(source)
            with patch.dict(live.CHANNELS, {"lifecycle-fixture": url}):
                ch = live.LiveChannel(url, self.ffmpeg)
                ch.build_palette()
                ch.start()
                proc = ch.decoder
                self.assertIsNotNone(proc)
                self.assertIsNone(proc.poll())

                # Wait for at least one item
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with ch.lock:
                        if len(ch.video) > 0 and len(ch.audio) > 0:
                            break
                    time.sleep(0.02)

                # Close channel
                ch.close()

                # Process must be terminated and waited (no zombie)
                self.assertIsNotNone(proc.poll())
                for t in ch.threads:
                    self.assertFalse(t.is_alive())
                if ch.pts_reader:
                    self.assertFalse(ch.pts_reader.is_alive())

    def test_repeated_close_is_idempotent(self):
        """Calling close() multiple times does not raise an error."""
        url = "http://127.0.0.1/fake"
        with patch.dict(live.CHANNELS, {"fake": url}):
            ch = live.LiveChannel(url)
            ch.close()
            ch.close()
            ch.close()

    def test_diagnostics_formatting_without_attribute_error(self):
        """diagnostics() works properly when decoder is present, stopped, or absent."""
        url = "http://127.0.0.1/fake"
        with patch.dict(live.CHANNELS, {"fake": url}):
            ch = live.LiveChannel(url)
            diag = ch.diagnostics()
            self.assertIsInstance(diag, str)

            # Simulated process
            proc = subprocess.Popen(
                ["true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait()
            ch.decoder = proc
            diag = ch.diagnostics()
            self.assertIn("decoder_exit=0", diag)

    def test_start_failure_cleanup_no_leaks(self):
        """If start() fails midway, all opened fds and subprocesses are cleaned up."""
        fd_before = get_open_fd_count()
        url = "http://127.0.0.1/nonexistent_file_or_invalid_port"
        with patch.dict(live.CHANNELS, {"fake": url}):
            ch = live.LiveChannel(url, self.ffmpeg)
            ch.build_palette()
            # Cause failure in calibrate_from_source
            with patch.object(ch, "calibrate_from_source", side_effect=RuntimeError("simulated start failure")):
                with self.assertRaises(RuntimeError):
                    ch.start()

            # Decoder must have been closed
            if ch.decoder is not None:
                self.assertIsNotNone(ch.decoder.poll())

        fd_after = get_open_fd_count()
        if fd_before > 0:
            self.assertLessEqual(fd_after, fd_before + 1)

    def test_consecutive_sessions_no_resource_leak(self):
        """10 consecutive client connections establish and end without leaking threads or FDs."""
        media = Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT)
        server = AVServer(media, TEST_TOKEN, port=0, duration_ms=100)
        errors = []

        def serve():
            try:
                server.serve()
            except Exception as error:
                errors.append(error)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        self.assertTrue(server.ready.wait(2))

        thread_count_before = threading.active_count()
        fd_count_before = get_open_fd_count()

        # Connect 10 times consecutively
        for i in range(10):
            conn = socket.create_connection(("127.0.0.1", server.port), timeout=1)
            send_packet(conn, hello())
            config = receive_packet(conn, 1)
            self.assertEqual(config.kind, Kind.CONFIG)
            conn.close()
            time.sleep(0.02)

        server.stop.set()
        # Wake up accept loop
        try:
            waker = socket.create_connection(("127.0.0.1", server.port), timeout=1)
            waker.close()
        except OSError:
            pass
        server_thread.join(timeout=2)

        thread_count_after = threading.active_count()
        fd_count_after = get_open_fd_count()

        self.assertEqual(errors, [])
        self.assertLessEqual(thread_count_after, thread_count_before)
        if fd_count_before > 0:
            self.assertLessEqual(fd_count_after, fd_count_before + 2)


if __name__ == "__main__":
    unittest.main()
