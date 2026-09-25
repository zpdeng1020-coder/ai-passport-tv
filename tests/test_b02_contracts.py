"""Regression contracts for Batch B02: fast close, exception isolation, and redaction."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server.live as live
import server.tv_server as tv
from server.pts import Timestamps
from server.media import Media, FRAME_COUNT


class TestB02Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("ffmpeg not available")

    def test_real_decoder_fast_close(self):
        """Close of a real FFmpeg decoder must take < 500 ms and reap child completely."""
        with tempfile.TemporaryDirectory(prefix="b02-test-") as tmp:
            src = Path(tmp) / "source.mkv"
            subprocess.run(
                [
                    self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000:duration=2",
                    "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", str(src)
                ],
                check=True, timeout=15
            )
            with patch.dict(live.CHANNELS, {"review": str(src)}):
                ch = live.LiveChannel(str(src), self.ffmpeg)
            ch.build_palette()
            ch.start()
            proc = ch.decoder
            deadline = time.monotonic() + 5
            ready = False
            while time.monotonic() < deadline:
                with ch.lock:
                    ready = bool(ch.video) and bool(ch.audio)
                if ready:
                    break
                if ch.failure():
                    break
                time.sleep(0.01)
            self.assertTrue(ready, "Fixture failed to produce video and audio")

            t0 = time.monotonic()
            ch.close()
            elapsed_ms = (time.monotonic() - t0) * 1000

            self.assertLess(elapsed_ms, 500.0, f"close took {elapsed_ms:.1f} ms, expected < 500 ms")
            self.assertIsNotNone(proc.poll(), "Subprocess must be reaped")
            alive_threads = [t.name for t in [*ch.threads, ch.pts_reader] if t and t.is_alive()]
            self.assertEqual(alive_threads, [], "No reader threads should be alive")

    def test_injected_diagnostics_failure_preserves_error_and_closes(self):
        """Diagnostics failure in finally must not skip close or overwrite original error."""
        class BrokenChannel:
            def __init__(self):
                self.closed = False
            def build_palette(self):
                raise live.LiveError("original_source_failure")
            def failure(self):
                return live.LiveError("original_source_failure")
            def diagnostics(self):
                raise RuntimeError("diagnostic_failure")
            def close(self):
                self.closed = True

        channel = BrokenChannel()
        media = Media(bytes(320000), (bytes(320 * 240),) * FRAME_COUNT)
        server = tv.AVServer(media, None, port=0)
        server.channel_factory = lambda *a: channel

        reported_error = None
        with patch.dict(tv.CHANNELS, {"review": "http://127.0.0.1/fixture"}), \
             patch.object(tv, "channel_list", return_value=[]), \
             patch.object(tv, "send_packet"):
            try:
                server._live_session(None, "review")
            except Exception as exc:
                reported_error = exc

        self.assertTrue(channel.closed, "channel.close() must be called even when diagnostics fails")
        self.assertIsInstance(reported_error, live.LiveError)
        self.assertIn("original_source_failure", str(reported_error))

    def test_injected_close_failure_preserves_error(self):
        """Close failure in finally must not overwrite the original business error."""
        class BrokenChannel:
            def __init__(self):
                self.closed = False
            def build_palette(self):
                raise live.LiveError("original_source_failure")
            def failure(self):
                return live.LiveError("original_source_failure")
            def diagnostics(self):
                return "fixture diagnostic"
            def close(self):
                self.closed = True
                raise RuntimeError("cleanup_failure")

        channel = BrokenChannel()
        media = Media(bytes(320000), (bytes(320 * 240),) * FRAME_COUNT)
        server = tv.AVServer(media, None, port=0)
        server.channel_factory = lambda *a: channel

        reported_error = None
        with patch.dict(tv.CHANNELS, {"review": "http://127.0.0.1/fixture"}), \
             patch.object(tv, "channel_list", return_value=[]), \
             patch.object(tv, "send_packet"):
            try:
                server._live_session(None, "review")
            except Exception as exc:
                reported_error = exc

        self.assertTrue(channel.closed, "channel.close() must be called")
        self.assertIsInstance(reported_error, live.LiveError)
        self.assertIn("original_source_failure", str(reported_error))

    def test_diagnostic_tail_redaction(self):
        """Secrets, tokens, and query params in decoder tail must be redacted."""
        with patch.dict(live.CHANNELS, {"review": "http://127.0.0.1/fixture"}):
            ch = live.LiveChannel("http://127.0.0.1/fixture", self.ffmpeg)
        ch.timestamps = Timestamps()
        ch.timestamps.note(b"decoder error at https://example.invalid/live.m3u8?token=SECRET_TOKEN_12345&auth=ABC\n")
        diag = ch.diagnostics()
        self.assertNotIn("SECRET_TOKEN_12345", diag)
        self.assertNotIn("ABC", diag)
        self.assertIn("<redacted>", diag)
        ch.close()


if __name__ == "__main__":
    unittest.main()
