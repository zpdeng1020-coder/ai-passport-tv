"""Live transcoding checks. Network tests are opt-in; framing tests are offline."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.live import LiveChannel, LiveError, frame_jpeg, chunk_pcm, CHANNELS
from server.protocol import AUDIO_BYTES, VIDEO_MAX


def _feed(stream):
    """Minimal readable stand-in for a pipe."""
    class Feed:
        def __init__(self, data):
            self.data = data

        def read(self, _size=None):
            # Real pipes signal end of data with an empty read, not an exception.
            if not self.data:
                return b""
            chunk, self.data = self.data, b""
            return chunk
        def close(self):
            pass
    return Feed(stream)


class FramingTests(unittest.TestCase):
    def test_jpeg_split_handles_partial_and_trailing_bytes(self):
        frames, stop = [], threading.Event()
        payload = b"\xff\xd8AAA\xff\xd9" + b"\xff\xd8BBBB\xff\xd9"
        # EOF is a genuine transcode failure in production; here only the two
        # complete frames that arrived before it matter.
        with self.assertRaises(LiveError):
            frame_jpeg(_feed(payload + b"\xff\xd8UNTERMINATED"), frames.append, stop)
        self.assertEqual(frames, [b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBBB\xff\xd9"])
        self.assertTrue(all(len(f) <= VIDEO_MAX for f in frames))

    def test_oversized_frame_is_rejected_not_truncated(self):
        frames, stop = [], threading.Event()
        with self.assertRaises(LiveError):
            frame_jpeg(_feed(b"\xff\xd8" + b"A" * (VIDEO_MAX + 1)), frames.append, stop)
        self.assertEqual(frames, [])

    def test_pcm_reblocking_is_exact(self):
        blocks, stop = [], threading.Event()
        with self.assertRaises(LiveError):
            chunk_pcm(_feed(bytes(AUDIO_BYTES * 3 + 5)), blocks.append, stop)
        self.assertEqual(len(blocks), 3)  # the 5-byte tail is never emitted
        self.assertTrue(all(len(b) == AUDIO_BYTES for b in blocks))


@unittest.skipUnless(os.environ.get("TV_LIVE_TEST") == "1",
                     "set TV_LIVE_TEST=1 to fetch a real channel")
class LiveNetworkTests(unittest.TestCase):
    def test_real_channel_yields_frames_and_audio(self):
        channel = LiveChannel(CHANNELS["cgtn"])
        channel.start()
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if channel.failure():
                    self.fail(f"transcode failed: {type(channel.failure()).__name__}")
                if channel.audio and channel.video:
                    break
                time.sleep(0.05)
            self.assertTrue(channel.audio, "no PCM produced")
            self.assertTrue(channel.video, "no JPEG produced")
            frame = channel.pop_video()
            self.assertTrue(frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"))
            self.assertLessEqual(len(frame), VIDEO_MAX)
            self.assertEqual(len(channel.pop_audio()), AUDIO_BYTES)
        finally:
            channel.close()


class StubChannel:
    """Duck-typed stand-in so the live sender is tested without ffmpeg or network."""

    def __init__(self, audio_chunks=200, video_frames=60):
        self.audio = [bytes(AUDIO_BYTES)] * audio_chunks
        self.video = [b"\xff\xd8S" + b"x" * 60 + b"\xff\xd9"] * video_frames
        self.failed = False
        self.dropped_video = 0
        self.skipped_audio = 0

    def start(self):
        pass

    def has_data(self):
        return bool(self.audio or self.video)

    def prebuffered(self):
        return True

    def audio_pending(self):
        return bool(self.audio)

    def video_pending(self):
        return bool(self.video)

    def pop_audio(self):
        return self.audio.pop(0) if self.audio else None

    def pop_video(self):
        return self.video.pop(0) if self.video else None

    def failure(self):
        return RuntimeError("stub") if self.failed else None

    def diagnostics(self):
        return "stub"

    def close(self):
        pass


class StubFactory:
    """Stands in for LiveChannel so no ffmpeg process is started."""

    def __init__(self):
        self.created = []

    def __call__(self, url, ffmpeg="ffmpeg", user_agent=""):
        # The real factory takes a per-channel User-Agent; the stub records the
        # url so a test can assert which channel was selected.
        channel = StubChannel()
        self.created.append(url)
        return channel


class ConfigContractTests(unittest.TestCase):
    """Guards the device/server CONFIG agreement.

    The device validates CONFIG field by field and drops the connection on any
    mismatch, so a server-side key collision is invisible until hardware runs.
    """

    def test_channel_list_does_not_collide_with_audio_channel_count(self):
        """Parse the CONFIG the live path actually sends, not a rebuilt copy.

        Asserting against a hand-built dict cannot catch the real failure: if
        the sender started writing the list into "channels" again, that dict
        would still look correct while every device session died.
        """
        import json
        runner = LiveSenderTests("test_live_sender_streams_audio_and_video")
        seen = {}
        runner._run(factory=StubFactory(), seconds=0.3, capture=seen)
        self.assertTrue(seen.get("config"), "no CONFIG packet was captured")
        payload = json.loads(seen["config"])
        # "channels" is the audio channel count, which the device requires to be 1.
        self.assertEqual(payload["channels"], 1)
        self.assertIsInstance(payload["channels"], int)
        self.assertIsInstance(payload["channel_list"], list)
        self.assertTrue(payload["channel_list"])
        for entry in payload["channel_list"]:
            self.assertIsInstance(entry.get("id"), str)

    def test_config_matches_the_device_contract(self):
        from server.tv_server import CONFIG
        from server.media import AUDIO_CHUNK_MS, FPS, HEIGHT, WIDTH
        expected = {"width": WIDTH, "height": HEIGHT, "fps": FPS,
                    "sample_rate": 16000, "channels": 1, "sample_bits": 16,
                    "audio_chunk_ms": AUDIO_CHUNK_MS, "video_max_bytes": 24576}
        for key, value in expected.items():
            self.assertEqual(CONFIG[key], value, key)
            self.assertNotIsInstance(CONFIG[key], (list, dict), key)
        self.assertEqual(CONFIG.get("start_delay_ms"), 200)

    def test_channel_ids_fit_the_device_buffer(self):
        """Ids must be short, printable and unique; the device skips anything else."""
        from server.live import channel_list
        entries = channel_list()
        from server.live import TV_CHANNEL_MAX
        self.assertLessEqual(len(entries), TV_CHANNEL_MAX)
        seen = set()
        for entry in entries:
            cid = entry["id"]
            self.assertTrue(cid and len(cid) < 16, cid)  # TV_CHANNEL_ID_MAX
            self.assertTrue(cid.isascii() and cid.isprintable(), cid)
            self.assertNotIn(cid, seen)
            seen.add(cid)

    def test_every_line_of_the_channel_file_reaches_the_table(self):
        """The file must be read whole, not up to some packet-sized limit.

        The read used to be capped at TV_CONTROL_MAX, which is a wire limit for
        one control packet. Once the table outgrew that number the file was cut
        mid-line, the half line parsed as a single field and parse_channels
        raised: the server then failed at import while channels.txt was fine, so
        nothing was served at all. Counting the rows in the file is what makes
        that visible, because a truncated read still returns a table.
        """
        import server.live as live
        source = Path(live.__file__).resolve().parent.parent / live.CHANNELS_FILE
        self.assertTrue(source.is_file(), f"no channel file at {source}")
        text = source.read_text(encoding="utf-8")
        expected = [line for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        self.assertGreater(len(expected), 0)
        self.assertEqual(len(live.CHANNELS), len(expected),
                         "the channel file has more rows than reached the table")

    def test_oversized_channel_file_is_rejected_not_truncated(self):
        """A file too large for the device is an error, never a partial table."""
        import server.live as live
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            # One byte past the ceiling, built from legal lines so only the
            # total size can be what fails.
            line = "ch%03d | n | http://x/" % 0
            path.write_text(line * (live.TV_CHANNEL_MAX *
                                    live.MAX_CHANNEL_LINE_BYTES // len(line) + 2),
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                live.load_channels(path)

    def test_read_limit_leaves_room_for_a_real_table(self):
        """The read ceiling must exceed any table the device would accept."""
        import server.live as live
        source = Path(live.__file__).resolve().parent.parent / live.CHANNELS_FILE
        size = source.stat().st_size
        ceiling = live.TV_CHANNEL_MAX * live.MAX_CHANNEL_LINE_BYTES
        self.assertGreater(ceiling, size,
                           f"read ceiling {ceiling} is below the {size}-byte table")


class CommandTests(unittest.TestCase):
    def test_transcode_is_paced_with_re(self):
        """Real-time pacing keeps the host queues from draining mid-session."""
        from server.live import ffmpeg_command
        command = ffmpeg_command("http://example.invalid/x.m3u8", 1, 2, "ffmpeg")
        self.assertIn("-re", command)
        self.assertLess(command.index("-re"), command.index("-i"))

    def test_nobuffer_input_flag_is_not_reintroduced(self):
        """+nobuffer reads as a latency win and costs 9 s of startup on HLS.

        Measured against the live allowlist: with it the first video frame
        arrived at 11.2 s, without it at 2.5 s. The device abandons a session
        that sees no media for 12 s, so the flag put every connection at the
        edge of its deadline.
        """
        from server.live import ffmpeg_command
        command = ffmpeg_command("http://example.invalid/x.m3u8", 1, 2, "ffmpeg")
        joined = " ".join(command)
        self.assertNotIn("+nobuffer", joined)
        self.assertNotIn("nobuffer", joined)


class LiveSenderTests(unittest.TestCase):
    """Exercises the full pacing path, which unit-testing only framing would miss."""

    def _run(self, seconds=1.5, factory=None, channel=None, capture=None,
             server_channel=""):
        """Run the real accept loop so listener-dependent pacing is covered."""
        import socket
        from server.tv_server import AVServer
        from server.protocol import Kind, Packet, json_bytes, send_packet, receive_packet
        token = b"t" * 32
        failures = []
        factory = factory or StubFactory()
        server = AVServer(None, token, "127.0.0.1", 0, 60000,
                          logger=failures.append)
        server.live_enabled = True
        server.channel_factory = factory
        # Mirrors what main() does with --channel; the fallback path reads it.
        server.channel_name = server_channel
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5), "server did not start listening")
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        hello = {"version": 1, "token": token.decode()}
        if channel is not None:
            hello["channel"] = channel
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello)))
        kinds, last_pts, session = [], {}, None
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds:
                packet = receive_packet(client, timeout=0.5, expected_session=session)
                kinds.append(packet.kind.name)
                session = packet.session
                if packet.kind == Kind.CONFIG and capture is not None:
                    capture["config"] = packet.payload.decode("utf-8")
                if packet.kind == Kind.PCM:
                    self.assertEqual(packet.pts_ms, last_pts.get("PCM", 0))
                    last_pts["PCM"] = packet.pts_ms + 20
                    self.assertEqual(len(packet.payload), AUDIO_BYTES)
                elif packet.kind == Kind.JPEG:
                    # Video is paced against audio, so its PTS may lag or lead
                    # but must never repeat.
                    self.assertGreater(packet.pts_ms, last_pts.get("JPEG", -1))
                    last_pts["JPEG"] = packet.pts_ms
        except TimeoutError:
            pass
        except (OSError, EOFError) as error:
            self.fail(f"connection ended early: {type(error).__name__} "
                      f"(server phase={server.phase}, log={failures})")
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return kinds

    def test_live_sender_streams_audio_and_video(self):
        kinds = self._run()
        self.assertEqual(kinds[0], "CONFIG")
        self.assertIn("PCM", kinds)
        self.assertIn("JPEG", kinds)

    def test_requested_channel_is_selected_and_unknown_falls_back(self):
        """Switching is a new connection naming a channel; bad names must not fail."""
        from server.live import CHANNELS, DEFAULT_CHANNEL
        # Named from the live table rather than written out, so changing the
        # channel list does not silently turn this into a test of the fallback.
        second = next(key for key in CHANNELS if key != DEFAULT_CHANNEL)
        for requested, expected in ((second, second),
                                    ("not-a-channel", DEFAULT_CHANNEL),
                                    (None, DEFAULT_CHANNEL)):
            factory = StubFactory()
            kinds = self._run(factory=factory, channel=requested, seconds=0.4)
            self.assertEqual(kinds[0], "CONFIG")
            self.assertEqual(factory.created, [CHANNELS[expected]])

    def test_server_default_channel_names_the_configured_one(self):
        """A device that asks for nothing gets the operator's default.

        The fallback must read the server's configured channel, not the module
        constant: --channel only reached the startup message before, so an
        operator running --channel cctv9 still served cctv1 to any device whose
        handshake carried no channel, while the log claimed otherwise.
        """
        from server.live import CHANNELS, DEFAULT_CHANNEL
        other = next(key for key in CHANNELS if key != DEFAULT_CHANNEL)
        for configured, requested in ((other, None), (other, "not-a-channel"),
                                      (DEFAULT_CHANNEL, None)):
            factory = StubFactory()
            kinds = self._run(factory=factory, channel=requested, seconds=0.4,
                              server_channel=configured)
            self.assertEqual(kinds[0], "CONFIG")
            self.assertEqual(factory.created, [CHANNELS[configured]],
                             f"configured={configured} requested={requested}")

    def test_live_mode_signal_handler_stops_the_accept_loop(self):
        """SIGINT must reach the flag serve() actually polls.

        The live branch used to install handlers on a local Event that serve()
        never read, so Ctrl-C left the listener running until SIGKILL.
        """
        import signal
        from server import tv_server
        seen = {}
        original = tv_server.AVServer.serve

        def fake_serve(self):
            seen["server"] = self
            # Deliver the signal the way the operator would, then confirm the
            # loop would exit on its own condition.
            os.kill(os.getpid(), signal.SIGINT)
            seen["stopped"] = self.stop.wait(2)

        from server.live import CHANNELS
        chosen = next(iter(CHANNELS))
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_bytes(b"t" * 32)
            os.chmod(token_file, 0o600)
            tv_server.AVServer.serve = fake_serve
            try:
                with mock.patch.object(sys, "argv",
                                       ["tv_server", "live", "--channel", chosen,
                                        "--bind", "127.0.0.1",
                                        "--token-file", str(token_file)]):
                    code = tv_server.main()
            finally:
                tv_server.AVServer.serve = original
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, signal.SIG_DFL)
        self.assertEqual(code, 0)
        self.assertTrue(seen.get("stopped"), "SIGINT did not set the loop's stop flag")
        self.assertEqual(seen["server"].channel_name, chosen)

    def test_audio_is_paced_to_real_time_not_flushed(self):
        """The stub offers 4 s of audio; a 1.5 s run must not send all of it.

        An unpaced sender drains the queue instantly and then goes silent,
        which underruns the device even though every byte was valid.
        """
        seconds = 1.5
        kinds = self._run(seconds=seconds)
        audio = kinds.count("PCM")
        self.assertLess(audio, seconds * 50 * 1.6,
                        f"sent {audio} audio packets in {seconds}s; sender is not paced")
        self.assertGreater(audio, seconds * 50 * 0.4,
                           f"only {audio} audio packets in {seconds}s; audio starved")


if __name__ == "__main__":
    unittest.main()
