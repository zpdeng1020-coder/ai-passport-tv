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
from server.media import AUDIO_CHUNK_MS, FPS

FRAME_MS = 1000.0 / FPS
FPS_FRAME_MS = FRAME_MS
import server.live as L
from server.live import (LiveChannel, LiveError, read_frames, chunk_pcm,
                         CHANNELS, VIDEO_SYNC_TOLERANCE_S)
from server.timeline import (BASIS_COMMON_DECODE, BASIS_LAUNCH,
                             BASIS_MEDIA_START, ContentTimeline, SessionClock)
from server.protocol import AUDIO_BYTES, VIDEO_CONTINUES, VIDEO_MAX
from server.media import AUDIO_CHUNK_MS
from server import frames as format


def _feed(stream, chunk=0):
    """Minimal readable stand-in for a pipe.

    `chunk` caps how much comes back from one read. A real pipe does that
    whether or not it is asked to -- it hands over what has arrived -- and the
    frame reader exists precisely because of it, so a stand-in that always
    returned everything would test a reader that did not need to loop.
    """
    class Feed:
        def __init__(self, data):
            self.data = data

        def read(self, size=None):
            # Real pipes signal end of data with an empty read, not an exception.
            if not self.data:
                return b""
            take = len(self.data)
            for limit in (size, chunk):
                if limit:
                    take = min(take, limit)
            result, self.data = self.data[:take], self.data[take:]
            return result

        def close(self):
            pass
    return Feed(stream)


def indexed(value):
    """One frame's worth of index bytes, all the same so equality is easy.

    The 1024-byte palette block the rawvideo muxer appends to every pal8 frame
    is part of the frame on the wire, so it is part of what the reader is asked
    to consume -- see TRAILER_BYTES in frames.py.
    """
    return bytes((value,)) * format.FRAME_PIXELS


def trailer(value=0):
    return bytes((value,)) * format.TRAILER_BYTES


class FramingTests(unittest.TestCase):
    def test_frames_are_read_by_length_and_a_short_tail_is_dropped(self):
        """A raw pipe carries no markers, so a short read ends the stream.

        There is nothing to resynchronise to and half a frame cannot be drawn,
        which is why the tail is dropped rather than padded -- and why the
        reader raises, since in production the pipe closing *is* a failure.
        """
        produced, stop = [], threading.Event()
        with self.assertRaises(LiveError):
            read_frames(_feed(indexed(1) + trailer() + indexed(2) + trailer()
                              + bytes(1000)), produced.append, stop)
        self.assertEqual(produced, [indexed(1), indexed(2)])

    def test_the_muxer_palette_block_is_consumed_not_read_as_pixels(self):
        """Every pal8 frame carries a palette the device is sent separately.

        If the reader did not step over it the next frame would start 1024
        bytes early and the whole stream would shear -- a picture that looks
        like slow tearing rather than an error, which is why this is pinned.
        """
        produced, stop = [], threading.Event()
        # Delivered in short pieces, the way a pipe delivers it, so the reader
        # has to reassemble both the frame and the block after it. The pipe
        # closing at the end is itself a failure in production, which is why
        # this expects the raise and then checks what arrived before it.
        with self.assertRaises(LiveError):
            read_frames(_feed(indexed(7) + trailer(0xAB) + indexed(9) + trailer(0xAB),
                              chunk=7000), produced.append, stop)
        self.assertEqual(produced, [indexed(7), indexed(9)])

    def test_packets_of_a_frame_stay_inside_the_device_limit(self):
        """The worst a packet can be is every stripe stored rather than compressed.

        Random bytes are the adversarial case: deflate cannot shrink them, so
        this is the packet size the device would have to accept if a channel
        ever carried noise. The device ends the session on anything larger, so
        it is checked here at the one place the packets are built rather than
        trusted.
        """
        import os
        noise = os.urandom(format.FRAME_PIXELS)
        packets = format.frame_packets(noise)
        # Bounded between one packet a stripe and one packet the frame, and the
        # two ends are the two things that can go wrong: fewer packets than
        # stripes means some packet carries more than the budget, and more than
        # a stripe's worth each means the split is pointless.
        self.assertGreaterEqual(len(packets), 1)
        self.assertLessEqual(len(packets), format.STRIPES)
        # Incompressible noise is the case the budget cannot honour -- every
        # stripe is at its stored size -- so its packets should be the smallest
        # the format can make them, and each must still be within the limit the
        # device enforces.
        for payload in packets:
            self.assertLessEqual(len(payload), VIDEO_MAX)
            self.assertEqual([len(s) for s in format.unpack(payload)],
                             [format.STRIPE_PIXELS]
                             * len(format.unpack(payload)))

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
        # The palette comes first, as it does in a session: it is chosen from
        # the source and the picture process is given the same file, so start()
        # refuses to run without one.
        channel.build_palette()
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
            self.assertTrue(channel.video, "no picture produced")
            # The palette has to be there before the session can draw anything:
            # the device looks every index up in it, so a channel with no
            # palette is a black screen rather than an error.
            self.assertEqual(len(channel.palette), format.PALETTE_BYTES)
            # The frame and its session timestamp come out together: the
            # packets of one frame all share this one value.
            taken = channel.pop_video()
            self.assertIsNotNone(taken, "no frame was selected")
            packets, stamp = taken
            self.assertTrue(packets, "a frame with no packets")
            self.assertIsInstance(stamp, int)
            # Every packet of the frame unpacked, in order, is exactly one
            # frame's worth of indices: this is the end-to-end check that the
            # cut, the compression and the length table agree.
            stripes = [s for payload in packets for s in format.unpack(payload)]
            self.assertEqual(len(stripes), format.STRIPES)
            self.assertTrue(all(len(s) == format.STRIPE_PIXELS for s in stripes))
            block, audio_stamp = channel.pop_audio()
            self.assertEqual(len(block), AUDIO_BYTES)
            self.assertIsInstance(audio_stamp, int)
        finally:
            channel.close()


class PacketAtomicityTests(unittest.TestCase):
    """The device reads a packet's payload as `length` bytes and looks at none of them.

    That makes one property load-bearing above all others: the bytes between a
    header and whatever follows the payload must belong to that packet and
    nothing else. Nothing in the protocol lets the device notice a violation --
    it will take an audio packet as picture data, fail to inflate the stripe, and
    read the next header from the wrong offset.

    A fault of exactly this shape was shipped: the picture was written in slices
    with the sound served between the slices, so a PCM packet landed inside a
    picture packet on every frame. It survived the whole test suite, because
    every other check here looks at framing -- sequence numbers, timestamps,
    continuation flags -- and a spliced packet leaves all of those plausible.
    """

    def _frame_like_the_device(self, stream: bytes) -> tuple[int, int]:
        """Return （packets read, picture payloads containing another header).

        Written to mirror main/av_player.c: read 24 bytes, take `length` from
        them, and step over exactly that many -- never searching, because the
        device has nothing to search with.
        """
        from server.protocol import HEADER, MAGIC, Kind
        buf = bytearray(stream)
        seen = embedded = 0
        while len(buf) >= HEADER.size:
            magic, _, kind, _, _, _, _, length = HEADER.unpack_from(buf)
            self.assertEqual(magic, MAGIC,
                             "framing was lost: the byte at a packet boundary is "
                             "not a header, which is the signature of one packet "
                             "written inside another")
            total = HEADER.size + length
            if len(buf) < total:
                break
            payload = bytes(buf[HEADER.size:total])
            seen += 1
            if kind == Kind.JPEG and MAGIC in payload:
                embedded += 1
            del buf[:total]
        return seen, embedded

    def test_the_check_detects_a_packet_written_inside_another(self):
        """The check has to fail on a stream that is genuinely broken.

        A test that cannot fail proves nothing, and this one is guarding the
        fault that took longest to find. So it is shown a stream that is broken
        in exactly that way, and has to report it.
        """
        from server.protocol import Kind, Packet
        picture = Packet(Kind.JPEG, 1, 2, 0, b"\x00\x01" + b"x" * 500).encode()
        sound = Packet(Kind.PCM, 1, 3, 0, b"a" * AUDIO_BYTES).encode()
        spliced = picture[:100] + sound + picture[100:]
        with self.assertRaises(AssertionError):
            self._frame_like_the_device(spliced)

    def test_a_clean_pair_of_packets_is_accepted(self):
        """And it has to pass on a stream that is fine, or it is just noise."""
        from server.protocol import Kind, Packet
        stream = (Packet(Kind.PCM, 1, 2, 0, b"a" * AUDIO_BYTES).encode()
                  + Packet(Kind.JPEG, 1, 3, 0, b"\x00\x01" + b"x" * 500).encode()
                  + Packet(Kind.PCM, 1, 4, 0, b"a" * AUDIO_BYTES).encode())
        seen, embedded = self._frame_like_the_device(stream)
        self.assertEqual((seen, embedded), (3, 0))


class StubChannel:
    """Duck-typed stand-in so the live sender is tested without ffmpeg or network."""

    def __init__(self, audio_chunks=200, video_frames=60):
        self.audio = [bytes(AUDIO_BYTES)] * audio_chunks
        # A queue entry is one frame *as its packets*, which is what the live
        # channel queues and what the sender iterates -- two packets here, so
        # the stub exercises the multi-packet path the device relies on.
        frame = [format.packet(0, [b"s" * 64]), format.packet(1, [b"s" * 64])]
        self.video = [list(frame) for _ in range(video_frames)]
        # Arrival times, in step with the two queues. They are diagnostics now
        # -- alignment runs on the content times below -- but they are kept
        # because the real channel keeps them and a stub that had dropped them
        # would not exercise the same pops.
        self.audio_at = [i * AUDIO_CHUNK_MS / 1000 for i in range(audio_chunks)]
        self.video_at = [i * AUDIO_CHUNK_MS / 1000 for i in range(video_frames)]
        # Content times, which is what the pops are now decided on. Each stream
        # advances at ITS OWN production rate from a shared origin -- the sound
        # by one chunk, the picture by one frame interval -- because that is what
        # the real counters measure and it is what makes the two comparable. An
        # earlier version of this stub gave both the same step, which is not a
        # simplification: it made the picture's content run at the sound's rate,
        # so the picture fell behind by a fixed amount per frame and the drift
        # counter reported 9 frames behind on a stream that had none.
        self.audio_content = [i * AUDIO_CHUNK_MS for i in range(audio_chunks)]
        self.video_content = [i * FRAME_MS for i in range(video_frames)]
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        # A calibrated timeline, because the sender now refuses to pair without
        # one and reports the refusal. A stub that lacks it exercises the
        # refusal path rather than the streaming path every test here is about.
        self.timeline = ContentTimeline(FRAME_MS, AUDIO_CHUNK_MS)
        self.timeline.calibrate(0.0, 0.0)
        self.failed = False
        self.dropped_video = 0
        self.skipped_audio = 0
        self.palette = format.palette_bytes(bytes(3 * format.PALETTE_ENTRIES))

    def build_palette(self):
        return self.palette

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
        if not self.audio or not self.audio_content:
            return None
        if self.audio_at:
            self.audio_at.pop(0)
        return self.audio.pop(0), self.session_clock.audio(self.audio_content.pop(0))

    def audio_depth_ms(self):
        # Mirrors LiveChannel.audio_depth_ms: the picture queue is held to the
        # sound's own depth so the two describe the same moment.
        return len(self.audio) * AUDIO_CHUNK_MS

    def pop_video(self, keep=0):
        # Mirrors LiveChannel.pop_video: alignment is by CONTENT TIME, so this
        # returns the frame whose content matches the sound at the head of its
        # queue, gives up any frame older than that, and returns nothing while
        # the matching frame has not arrived.
        #
        # The pairing is expressed on the sound's clock by the offset, which is
        # zero here because both streams are generated from the same origin.
        if not self.video or not self.video_content or not self.audio_content:
            return None
        sound_at = self.audio_content[0]
        while (self.video_content
               and self.video_content[0] < sound_at - VIDEO_SYNC_TOLERANCE_S * 1000):
            self.video.pop(0)
            self.video_content.pop(0)
            if self.video_at:
                self.video_at.pop(0)
        if not self.video or not self.video_content:
            return None
        if self.video_content[0] > sound_at + VIDEO_SYNC_TOLERANCE_S * 1000:
            return None
        if self.video_at:
            self.video_at.pop(0)
        content = self.video_content.pop(0)
        # The frame's OWN content, exactly as `LiveChannel.pop_video` does it.
        # Stamping from `audio_items` here would put this stub back on the
        # scheme two reviews disproved, and every test driving it would then be
        # testing the wrong rule -- which is how that scheme survived so long.
        stamp = self.session_clock.video(content)
        if stamp is None:
            self.dropped_video += 1
            return None
        self.session_clock.note_pair(content, sound_at)
        return self.video.pop(0), stamp

    def picture_lag_s(self):
        if not self.video_at or not self.audio_at:
            return 0.0
        return self.video_at[0] - self.audio_at[0]

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
                    "audio_chunk_ms": AUDIO_CHUNK_MS,
                    "video_max_bytes": format.VIDEO_MAX,
                    # The device checks the stripe height against its own
                    # AV_STRIPE_ROWS and refuses CONFIG on a mismatch: it is the
                    # one number that says both ends are cutting the frame the
                    # same way, so it has to be in the packet.
                    "stripe_rows": format.STRIPE_ROWS}
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
        # Parsed from THAT file, by that path, rather than read out of the table
        # the import happened to build.
        #
        # `load_channels()` resolves `channels.txt` against the process's
        # working directory, so the table it builds at import depends on where
        # the tests were started: from the repository it holds the real 127
        # rows, from `tests/` it falls back to the four built-in channels. The
        # assertion used to compare the repository's row count against whichever
        # of those two had happened, so it passed or failed on the caller's
        # directory -- a test that reports the harness rather than the code.
        # Passing the path in makes it measure the same thing every time.
        saved = dict(live.CHANNELS), dict(live.CHANNEL_LABELS), live.DEFAULT_CHANNEL
        try:
            live.load_channels(source)
            self.assertEqual(len(live.CHANNELS), len(expected),
                             "the channel file has more rows than reached the table")
        finally:
            # Every module-level name `load_channels` rebinds is restored, not
            # just CHANNELS. Restoring the dicts by update left DEFAULT_CHANNEL
            # pointing at a key that the next test's table did not have, which
            # made an unrelated test fail with KeyError -- a test leaking state
            # into another test, which is worse than the flakiness this replaced.
            live.CHANNELS, live.CHANNEL_LABELS, live.DEFAULT_CHANNEL = saved

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
        from server.live import source_command
        command = source_command("http://example.invalid/x.m3u8", 1, 2, "ffmpeg")
        self.assertIn("-re", command)
        self.assertLess(command.index("-re"), command.index("-i"))

    def test_one_process_produces_both_streams_and_their_timestamps(self):
        """The whole of the common-input change, as assertions on the command.

        Both payloads map from one ffmpeg invocation, and both timestamps are
        logged by filters in that same invocation's graph. Two processes each
        counting their own frames is what this replaced: counting is a rate and
        not a clock, and it cannot see a gap.
        """
        from server.live import source_command
        command = source_command("http://example.invalid/x.m3u8", 1, 2, "ffmpeg")
        # One input.
        self.assertEqual(command.count("-i"), 1)
        # Two outputs, each mapped from a named graph output.
        self.assertEqual(command.count("-map"), 2)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("showinfo", graph)
        self.assertIn("ashowinfo", graph)
        # The log level is what makes those filters produce anything at all.
        self.assertEqual(command[command.index("-loglevel") + 1], "info")
        # One audio frame is one device block, which is what makes one
        # `ashowinfo` line describe one block. A stereo frame would carry two.
        self.assertIn("channel_layouts=mono", graph)
        self.assertIn("asetnsamples=n=640", graph)

    def test_nobuffer_input_flag_is_not_reintroduced(self):
        """+nobuffer reads as a latency win and costs 9 s of startup on HLS.

        Measured against the live allowlist: with it the first video frame
        arrived at 11.2 s, without it at 2.5 s. The device abandons a session
        that sees no media for 12 s, so the flag put every connection at the
        edge of its deadline.
        """
        from server.live import source_command
        joined = " ".join(source_command("http://example.invalid/x.m3u8", 1, 2,
                                         "ffmpeg"))
        self.assertNotIn("+nobuffer", joined)
        self.assertNotIn("nobuffer", joined)

    def test_picture_is_index_bytes_not_an_encoded_image(self):
        """The device has no image decoder in this path, so the pipe must be raw.

        An accidental -f image2 or a return of -pix_fmt yuv420p would still
        produce a plausible command line and still start ffmpeg; what it would
        not do is produce anything the device could draw, so the two settings
        that make the output indices are pinned here.

        Dithering is checked for the same reason one step further out: it is
        noise by construction, it is on by default, and it is applied by the
        same scaler that produces the indices -- so leaving it default costs
        roughly half the compression, silently.
        """
        from server.live import source_command
        command = source_command("http://example.invalid/x.m3u8", 1, 2, "ffmpeg")
        # rgb8, and this assertion is the inverse of the one that stood here
        # before. rgb8 is ffmpeg's fixed 3-3-2 grid rather than a colour depth,
        # which made it wrong while the palette was adaptive -- the indices
        # would be looked up in a table they were not chosen from. The palette
        # is now that same grid, so quantising onto it is the point, and pal8
        # would mean a palette PNG that has to be sampled from the opening
        # seconds of the source and then frozen: the exact fault being fixed.
        self.assertIn("rgb8", command)
        self.assertNotIn("pal8", command)
        self.assertIn("rawvideo", command)
        self.assertEqual(command[command.index("-sws_dither") + 1], "none")
        # No second input, no paletteuse: the colours come from a rule both
        # ends implement, never from a file the server sampled.
        self.assertNotIn("paletteuse", " ".join(command))
        self.assertEqual(command.count("-i"), 1)


class PictureSoundPlacementTests(unittest.TestCase):
    """What reaches the device, read off the wire rather than from the loop's own bookkeeping."""

    def test_the_sender_cannot_substitute_its_own_timestamp(self):
        """The information must survive frame selection -- that is the whole ask.

        `pop_video()` decides which frame matches which sound, and it is the
        only place that knows. If the sender computes the timestamp afterwards
        from its own progress, the decision's result is discarded: the stamp
        then describes where the sender had got to, which equals the sound's
        position on a healthy stream and stops doing so on the stream that
        matters.

        So this gives the channel a stamp no counter can produce and checks that
        it is the one on the wire. The old sender -- `video_pts = max(audio_pts,
        last_video_pts + 1)` -- ignores what `pop_video` returns, and therefore
        fails this while passing every test that only checks timestamps rise.
        """
        import socket
        from server.tv_server import AVServer
        from server.protocol import Kind, Packet, json_bytes, send_packet, receive_packet

        class StampedChannel(StubChannel):
            """A channel that names its frames in a way no counter could."""

            def pop_video(self, keep=0):
                taken = super().pop_video(keep)
                if taken is None:
                    return None
                packets, _content_ms = taken
                # Deliberately far from anything a count of sent items gives,
                # and on the sound's grid so the grid check below still holds.
                StampedChannel.issued += 1
                return packets, 4_000_000 + StampedChannel.issued * AUDIO_CHUNK_MS

        StampedChannel.issued = 0

        class StampedFactory(StubFactory):
            def __call__(self, url, ffmpeg="ffmpeg", user_agent=""):
                self.created.append(url)
                return StampedChannel()

        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StampedFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0,
                                   json_bytes({"version": 1, "token": token.decode()})))
        picture, session = [], None
        start = time.monotonic()
        try:
            while time.monotonic() - start < 1.5:
                packet = receive_packet(client, timeout=0.5, expected_session=session)
                session = packet.session
                if packet.kind == Kind.JPEG and not (packet.flags & VIDEO_CONTINUES):
                    picture.append(packet.pts_ms)
        except (TimeoutError, EOFError):
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertTrue(picture, "no picture was sent")
        for pts in picture:
            self.assertGreaterEqual(
                pts, 4_000_000,
                f"frame stamped {pts} ms: the sender's own value reached the "
                f"wire, so the frame's timestamp did not come from the pairing")

    def test_the_picture_carries_a_position_the_sound_occupies(self):
        """Read off the wire, because that is where the value is lost.

        The frame is chosen on content time, but what reaches the device is a
        session timestamp, and the two are equal only while nothing has been
        dropped. This drives a real session and checks the property the device
        depends on: each frame's timestamp is a position some sound block
        occupies, on the same clock and in the same units.

        **This test does not tell the two schemes apart, and it is recorded here
        rather than quietly left to look as though it does.** The old sender
        computed `max(audio_pts, last_video_pts + 1)`, and on a stream where the
        sound keeps flowing that expression equals the sound block's index --
        the same number the new mapping produces. It was run against the old
        rule and passed. A healthy stream cannot distinguish them, which is why
        the defect survived every earlier test.

        What separates them is that the sender must not be *able* to compute its
        own value, which is what
        `test_the_sender_cannot_substitute_its_own_timestamp` checks. This one
        is kept as the device's contract: whatever rule is in place, what
        reaches the wire has to sit on the sound's clock. A change that broke
        that would fail here.
        """
        import socket
        from server.tv_server import AVServer
        from server.protocol import Kind, Packet, json_bytes, send_packet, receive_packet
        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0,
                                   json_bytes({"version": 1, "token": token.decode()})))
        sound, picture, session = [], [], None
        # The channel object has to be caught while the session is live: the
        # server drops its reference when the session ends, and the counters
        # below only exist on it.
        clock = None
        start = time.monotonic()
        try:
            while time.monotonic() - start < 2.0:
                packet = receive_packet(client, timeout=0.5, expected_session=session)
                session = packet.session
                if packet.kind == Kind.PCM:
                    sound.append(packet.pts_ms)
                elif packet.kind == Kind.JPEG and not (packet.flags & VIDEO_CONTINUES):
                    picture.append(packet.pts_ms)
                if clock is None and getattr(server, "live_channel", None) is not None:
                    clock = server.live_channel.session_clock
        except (TimeoutError, EOFError):
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertTrue(sound, "no sound was sent")
        self.assertTrue(picture, "no picture was sent")
        # The sound defines the clock: zero first, then one chunk at a time.
        self.assertEqual(sound[0], 0)
        self.assertEqual(sound[1] - sound[0], AUDIO_CHUNK_MS)
        # **Frames are NOT on the chunk grid, and asserting that they were was
        # wrong.** This test used to require each picture timestamp to be a
        # multiple of the audio chunk, which is true only for the scheme an
        # external review disproved: stamping a frame with the position of the
        # sound it was paired with. A frame's own content falls between chunks --
        # that is what the stamp is for -- so the values are milliseconds, not
        # chunk multiples.
        #
        # What must hold is the device's contract: the picture's timestamp is on
        # the sound's clock, in the sound's units, and advances.
        for pts in picture:
            self.assertIsInstance(pts, int)
            self.assertGreaterEqual(pts, 0)
        self.assertEqual(picture, sorted(set(picture)),
                         "video timestamps must strictly increase")
        # And it names a position the sound actually reaches, not one far ahead
        # of everything sent.
        self.assertLessEqual(max(picture), max(sound) + 2 * FPS_FRAME_MS)
        # And the drift the wire cannot show is measured rather than inferred.
        # What is checked here is that the number is produced at all and is
        # bounded by the rule that produced it: `pop_video` pairs only within
        # the tolerance, so no residual can exceed it.
        #
        # The stub's own residual sits near the negative edge of that bound and
        # stays there, which is the stub and not the code: it queues a fixed
        # number of frames and a fixed number of chunks from one origin at two
        # different rates, so the picture's head falls steadily further behind
        # the sound's as the sound is consumed the faster of the two. A bound is
        # all this harness can honestly support; the drift arithmetic itself is
        # covered by `test_timeline`.
        self.assertIsNotNone(clock, "the session's clock was never reachable")
        self.assertGreater(clock.pairs, 0, "no pairing was recorded")
        self.assertLessEqual(abs(clock.residual_worst_ms),
                             VIDEO_SYNC_TOLERANCE_S * 1000 + 1000.0 / FPS,
                             f"a pair exceeded the tolerance: "
                             f"worst {clock.residual_worst_ms:+.0f} ms")
        self.assertEqual(clock.audio_gaps, 0)

class LiveSenderTests(unittest.TestCase):
    """Exercises the full pacing path, which unit-testing only framing would miss."""

    def _run(self, seconds=1.5, factory=None, channel=None, capture=None,
             server_channel="", return_server=False):
        """Run the real accept loop so listener-dependent pacing is covered.

        A session does not start until the pre-buffer target is met, and the
        stub queue is 4 s of sound against a 3 s target, so it is met on the
        first pass. Tests that need a longer or shorter window therefore change
        `seconds` rather than the reserve.
        """
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
                    last_pts["PCM"] = packet.pts_ms + AUDIO_CHUNK_MS
                    self.assertEqual(len(packet.payload), AUDIO_BYTES)
                elif packet.kind == Kind.JPEG:
                    # A frame is several packets under one timestamp. The first
                    # of them must advance the clock -- the device rejects a
                    # video packet whose timestamp repeats, which is what stops
                    # a stream being reordered -- and the rest must declare
                    # themselves continuations of it, sharing its timestamp.
                    # Asserting only that timestamps rise would pass a stream
                    # that never marked its continuations, which the device
                    # would then throw away as duplicates.
                    if packet.flags & VIDEO_CONTINUES:
                        self.assertEqual(packet.pts_ms, last_pts.get("JPEG"),
                                         "continuation does not share its frame's clock")
                    else:
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
        return (kinds, server) if return_server else kinds

    def test_the_sender_uses_the_controller_and_not_a_fixed_rate(self):
        """The rate the sender paces by has to be the one the controller chose.

        The controller has its own tests, and passing those while the sender
        still paces by media.FPS would be the whole feature doing nothing --
        the shape of bug where each half works and the join does not. This
        drives a real session and reads the rate back out of it.

        Measured, not asserted from a constant: CONFIG announces media.FPS and
        the sender deliberately does not pace by it, so the two disagreeing is
        the expected state and a test that compared them would pass while
        proving nothing.
        """
        from server.rate import RateController
        kinds, server = self._run(seconds=1.5, capture={}, return_server=True)
        self.assertIn("JPEG", kinds)
        # The controller is per session and starts where rate.py says, so a
        # session that has just begun is at that rate whatever the file's
        # default is.
        self.assertIsInstance(getattr(server, "_last_controller", None),
                              RateController)

    def test_live_sender_streams_audio_and_video(self):
        kinds = self._run()
        self.assertEqual(kinds[0], "CONFIG")
        # The palette comes before any picture and is not optional: the device
        # cannot turn an index into a colour without it, so a session that
        # skipped it would draw a black screen rather than fail loudly.
        self.assertEqual(kinds[1], "PALETTE")
        self.assertIn("PCM", kinds)
        self.assertIn("JPEG", kinds)

    def _run_reading_payloads(self, seconds=1.2):
        """Run a session and hand back （kind, payload) for every packet read.

        The existing sender test checks the framing -- sequence numbers,
        timestamps, continuation flags -- and a fault that leaves all of those
        intact while corrupting the bytes between the header and the next header
        passes it. That fault has happened: the sound was written into the middle
        of a picture packet, and because the device reads `length` bytes from the
        header as one unbroken run, every session ended after a handful of
        packets. Nothing in the test suite saw it.
        """
        import socket
        from server.tv_server import AVServer
        from server.protocol import Kind, Packet, json_bytes, send_packet, receive_packet
        token = b"t" * 32
        failures = []
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=failures.append)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5), "server did not start listening")
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0,
                                   json_bytes({"version": 1, "token": token.decode()})))
        packets, session = [], None
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds:
                packet = receive_packet(client, timeout=0.5, expected_session=session)
                session = packet.session
                packets.append(packet)
        except (TimeoutError, OSError, EOFError):
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return packets

    def test_a_second_session_can_run(self):
        """Two sessions in a row, because state that outlives one broke the next.

        The one-second diagnostic kept its timestamp on the server object, so it
        was skipped on a first session and taken on every later one -- and it
        read two names that were assigned further down the function. The second
        connection therefore raised on its first pass, and the handler that
        caught it printed "the program could not start" and ended the process.
        A single-session test cannot see this, which is why the suite did not.
        """
        first = self._run(seconds=0.4)
        self.assertIn("JPEG", first, "first session produced no picture")
        second = self._run(seconds=0.4)
        self.assertIn("JPEG", second,
                      "the second session produced no picture; state kept from "
                      "the first one is interfering with it")

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

class PictureReachesTheDeviceTests(unittest.TestCase):
    """A session that sends sound and no picture at all.

    This is the failure the live path actually had, and no test in this file
    covered it: every other test drives `StubChannel`, which answers
    `pop_video()` from its own queues and never consults a calibration, so the
    server could refuse every frame for ever while the whole suite stayed green.

    Measured on the real device before it was fixed: `audio_sent` reached 2634
    with `video_sent` at 0 across two minutes, sound playing perfectly and the
    screen black. The cause was `stream_start_times()` returning None on a live
    URL -- which it does for every channel in the table -- leaving the timeline
    uncalibrated so that `pop_video()` refused to pair, for ever, by design.
    """

    class OfflineChannel(LiveChannel):
        """`LiveChannel` with the two ffmpeg processes left out.

        Everything the session actually exercises -- `pop_video`, the queues,
        the content clocks, `session_clock` -- is the real implementation. Only
        `start()` is replaced, because it would otherwise launch decoders
        against a URL this environment cannot reach, and the failure would then
        be a 404 rather than the thing under test.
        """

        def __init__(self, url):
            super().__init__(url)
            self.palette = bytes(format.PALETTE_BYTES)
            self.error = None

        def build_palette(self):
            return self.palette

        def start(self):
            # The two decoders are missing, so the queues are filled by hand
            # below; what `start()` would have contributed is its epochs, which
            # exist already and are what a live source calibrates from.
            #
            # The probe is made to answer the way a live playlist does -- it
            # responds, and the streams carry no start_time. That distinction
            # now decides whether the fallback is allowed at all, so a mock that
            # merely returned None would be refused, correctly.
            self.calibrate_from_source()

        def feed(self):
            frame = bytes(range(256)) * (format.WIDTH * format.HEIGHT // 256)
            # The timestamps the decoder would have supplied, given by hand
            # because it is the decoder that is missing here. One frame per
            # 1/FPS and one block per chunk, both counting from the same origin
            # -- which is what coming out of a single decode means, and is the
            # property these tests are about.
            for n in range(L.PREBUFFER_FRAMES + 10):
                self._push_video(frame, n * 1000.0 / L.FPS)
            for n in range(L.PREBUFFER_CHUNKS + 10):
                self._push_audio(bytes(AUDIO_BYTES), n * AUDIO_CHUNK_MS)

    LIVE_PROBE_NOTE = ("the source reports no start_time for its video stream")

    def _session(self, probe=None):
        """Run one session, with the media probe stubbed.

        `stream_start_times` is patched rather than left to run, and that is not
        tidiness. It shells out to ffprobe against a live URL with a 20-second
        timeout, so leaving it in makes these tests depend on the network: they
        passed or failed according to whether that host answered, which is a
        test reporting the weather. `probe` says what ffprobe would have said --
        None for a live source that reports no start times, a tuple for a file.
        """
        import socket
        from server.tv_server import AVServer
        from server.protocol import Kind, Packet, json_bytes, send_packet, receive_packet

        channel = self.OfflineChannel(next(iter(L.CHANNELS.values())))
        channel.feed()

        class Factory:
            def __call__(self, url, ffmpeg="ffmpeg", user_agent=""):
                return channel

        token = b"t" * 32
        log = []
        def answering(url, ffmpeg, user_agent="", note=None):
            if probe is not None:
                return probe
            # A live playlist: it answers, and reports no start times. That is
            # the only failure that licenses the launch-time fallback.
            if note is not None:
                note.append(self.LIVE_PROBE_NOTE)
            return None

        probe_patch = mock.patch.object(L, "stream_start_times",
                                        side_effect=answering)
        probe_patch.start()
        self.addCleanup(probe_patch.stop)
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=log.append)
        server.live_enabled = True
        server.channel_factory = Factory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        send_packet(client, Packet(Kind.HELLO, 0, 0, 0,
                                   json_bytes({"version": 1, "token": token.decode()})))
        kinds, session = [], None
        start = time.monotonic()
        try:
            while time.monotonic() - start < 4.0:
                try:
                    packet = receive_packet(client, timeout=0.5,
                                            expected_session=session)
                except TimeoutError:
                    # A quiet half-second is not the end of the session. A real
                    # session's start includes an ffprobe call against the live
                    # source, which is exactly the step under test, and treating
                    # that gap as a dead connection is how this test first
                    # reported "no sound was sent" about a session that was
                    # simply still starting.
                    continue
                session = packet.session
                kinds.append(packet.kind.name)
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return kinds, server, channel, log

    def test_a_session_sends_pictures_and_not_only_sound(self):
        """Both streams must reach the device, and the picture is the one that
        can silently go missing.

        Sound is sent from a counter that cannot stop; the picture needs a
        calibration and a matching frame, so it can be withheld indefinitely
        while every other part of the session looks healthy -- which is exactly
        what happened, for two minutes, on the real device, with `audio_sent`
        past 2600 and `video_sent` at 0.

        `probe=None` is the case that matters: it is what ffprobe returns for
        every channel in the table.
        """
        for probe in (None, (0.0, 0.0)):
            with self.subTest(probe=probe):
                kinds, _server, channel, log = self._session(probe=probe)
                self.assertIn("PCM", kinds,
                              f"no sound was sent at all (log={log[-2:]})")
                self.assertIn("JPEG", kinds,
                              "sound was sent and no picture at all: the frame "
                              "selection refused every frame (log=%r)"
                              % (log[-2:],))
                self.assertTrue(channel.timeline.calibrated)

    def test_a_live_source_is_calibrated_and_not_left_black(self):
        """The black-screen regression, still guarded.

        A live playlist is the case where the probe never used to answer, so
        the timeline stayed uncalibrated and `pop_video()` refused for ever --
        sound playing perfectly against a black screen for two minutes. The
        basis is now the decode itself, so this cannot recur; the assertion is
        kept because the consequence of it recurring is not subtle.
        """
        channel = self.OfflineChannel(next(iter(L.CHANNELS.values())))
        channel.start()
        self.assertTrue(channel.timeline.calibrated,
                        "a live source left the timeline uncalibrated, so no "
                        "frame can ever be paired and the screen stays black")
        self.assertEqual(channel.timeline.basis, BASIS_COMMON_DECODE)

    def test_a_file_whose_streams_start_apart_is_related_by_the_decode(self):
        """The review's 600 ms counterexample, answered rather than inferred.

        A file whose audio starts 600 ms in is a real difference, and the two
        designs before this one both lost it: the arrival-time version folded
        delivery into it, and the launch-time version measured the scheduler
        instead. Now the decoder reports each stream's own `pts_time`, so the
        600 ms is present in the numbers rather than being something an offset
        has to reconstruct. The offset is zero because there is only one clock.
        """
        channel = self.OfflineChannel(next(iter(L.CHANNELS.values())))
        with mock.patch.object(L, "stream_start_times", return_value=(0.0, 0.6)):
            channel.calibrate_from_source()
        self.assertEqual(channel.timeline.basis, BASIS_COMMON_DECODE)
        self.assertEqual(channel.timeline.offset_ms, 0.0)
        self.assertNotEqual(channel.timeline.basis, BASIS_MEDIA_START,
                            "an inferred offset came back")


class TheSessionDoesNotAskTheSourceTests(unittest.TestCase):
    """Where the relationship between the two clocks now comes from.

    Two earlier designs chose an offset by inference and an external review
    disproved both. Counting each stream's items measured a rate and called it
    a clock. Failing that, the code asked `ffprobe` for the streams' start
    times, and when the probe did not answer it fell back to the two processes'
    launch times -- so deciding the basis on *whether the probe answered* made
    every way the probe could fail a way to choose the wrong calibration. The
    first correction classified by the URL's extension, and the review defeated
    it with the same file served from `/watch?id=1` instead of `/source.mkv`.

    Both are gone. One decoder produces the picture, the sound and every
    timestamp, so the two clocks are one clock and the offset is zero by
    construction. There is nothing left to infer and nothing that can fail into
    a wrong answer. The last three methods here still exercise the retired
    probe, deliberately: it remains in the module as the record of what was
    tried, and code kept as a record should still be covered.
    """

    def _build(self, url, video_epoch=0.0, audio_epoch=0.6):
        channel = L.LiveChannel.__new__(L.LiveChannel)
        channel.url = url
        channel.ffmpeg = "ffmpeg"
        channel.user_agent = ""
        channel.video_epoch = video_epoch
        channel.audio_epoch = audio_epoch
        channel.timeline = ContentTimeline(1000.0 / FPS, AUDIO_CHUNK_MS)
        return channel

    @staticmethod
    def _probe(reason):
        def probe(url, ffmpeg, user_agent="", note=None):
            if note is not None and reason is not None:
                note.append(reason)
            return None
        return probe

    NO_START = "the source reports no start_time for its video stream"
    MISSING = "ffprobe is not installed"

    def test_every_url_shape_takes_the_same_path(self):
        """The four URLs that defeated both earlier designs, and a fifth.

        A stored file, the same file behind a query string, a download
        endpoint, a live playlist and a `.mp4` all now reach the same answer,
        because the answer no longer depends on anything about the URL.
        """
        for url in ("http://host/source.mkv", "http://host/watch?id=1",
                    "http://host/download/9", "http://host/stream",
                    "http://host/movie.mp4"):
            with self.subTest(url=url):
                channel = self._build(url)
                self.assertTrue(channel.calibrate_from_source(), url)
                self.assertEqual(channel.timeline.offset_ms, 0.0)
                self.assertEqual(channel.timeline.basis, BASIS_COMMON_DECODE)
                self.assertTrue(channel.timeline.calibrated)

    def test_the_probe_is_never_consulted(self):
        """A probe that cannot run is now harmless, which is the point.

        While the probe decided the basis, every way it could fail was a way to
        choose a wrong calibration. It is off the session path now; this pins
        that so a later change cannot quietly put it back.
        """
        channel = self._build("http://host/live.m3u8")
        with mock.patch.object(L, "stream_start_times",
                               side_effect=self._probe(self.MISSING)) as probe:
            self.assertTrue(channel.calibrate_from_source())
        self.assertEqual(probe.call_count, 0,
                         "the session asked the source about its own timing")

    def test_a_session_never_produces_an_approximate_offset(self):
        """`approximate` stays a state the clock can be in, not a path a
        session takes. If a session could reach it, `calibrated` would stop
        meaning what the acceptance criteria need it to mean.
        """
        channel = self._build("http://host/live.m3u8")
        channel.calibrate_from_source()
        self.assertNotEqual(channel.timeline.basis, BASIS_LAUNCH)
        self.assertNotEqual(channel.timeline.state, "approximate")
        self.assertNotEqual(channel.timeline.state, "unknown")

    def test_the_probe_carries_the_channels_user_agent(self):
        """A source that 403s the probe but serves the decoders.

        Measured by the review: the two ffmpeg processes send the channel's
        User-Agent and read the source; ffprobe sent none, was refused, and the
        code read that refusal as the media having no start times.
        """
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            raise FileNotFoundError("no ffprobe here")

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            L.stream_start_times("http://host/live.m3u8", "ffmpeg", "Agent/1.0")
        self.assertIn("-user_agent", seen["command"])
        self.assertIn("Agent/1.0", seen["command"])

    def test_a_probe_failure_reports_why(self):
        """The reason a probe failed is not the same as the media's answer."""
        noted = []
        with mock.patch.object(L.subprocess, "run",
                               side_effect=FileNotFoundError("nope")):
            self.assertIsNone(
                L.stream_start_times("http://host/x.m3u8", "ffmpeg",
                                     "Agent/1.0", noted))
        self.assertTrue(noted, "a failed probe must say why it failed")
        self.assertIn("not installed", noted[0])

    def test_the_two_failure_kinds_are_distinguished(self):
        """The single predicate the whole decision rests on."""
        self.assertTrue(L.LiveChannel.has_media_start_times([self.NO_START]))
        self.assertFalse(L.LiveChannel.has_media_start_times([self.MISSING]))
        self.assertFalse(L.LiveChannel.has_media_start_times([]))

