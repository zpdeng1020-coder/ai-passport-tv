"""A fixed frame rate per channel, and what happens to a frame on its way out.

The rate a channel is produced at is the source's own, found once when the channel
starts and held for the session. These check that the answer is read correctly
(the frame rate ffprobe reports is not always the one that is meant), that
everything downstream of it agrees, and that a frame is compressed once, at the
moment it is sent, against what the device has and what the byte budget has left.
"""
import collections
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server.live as L
from server import frames
from server.media import AUDIO_CHUNK_MS, FPS
from server.protocol import HEADER
from server.timeline import ContentTimeline


def probe_says(avg, real, returncode=0):
    out = json.dumps({"streams": [{"avg_frame_rate": avg, "r_frame_rate": real}]})
    return SimpleNamespace(returncode=returncode, stdout=out, stderr="")


class ReadingFrameRates(unittest.TestCase):
    def test_a_rate_as_ffprobe_writes_it(self):
        self.assertEqual(L.parse_rate("25/1"), 25.0)
        self.assertAlmostEqual(L.parse_rate("30000/1001"), 29.97, places=2)
        self.assertEqual(L.parse_rate("25"), 25.0)

    def test_unknown_is_none_not_an_error(self):
        # "0/0" is how ffprobe says it does not know, and it is common on live HLS.
        for text in ("0/0", "", "abc", "25/0", "-5/1", None):
            self.assertIsNone(L.parse_rate(text), text)

    def test_the_average_beats_the_container_rate(self):
        # 25 fps carried as 50 fields: r_frame_rate 50, avg_frame_rate 25.
        self.assertEqual(L.choose_fps(25.0, 50.0), 25)

    def test_the_container_rate_is_used_when_the_average_is_unknown(self):
        self.assertEqual(L.choose_fps(None, 25.0), 25)
        self.assertEqual(L.choose_fps(0, 24.0), 24)

    def test_a_rate_over_the_devices_bound_is_halved_not_clipped(self):
        # Clipping 50 to 30 would resample 50 -> 30 unevenly; halving keeps every
        # other frame, which is what a 25 fps programme carried at 50 is.
        self.assertEqual(L.choose_fps(None, 50.0), 25)
        self.assertEqual(L.choose_fps(None, 60.0), 30)
        self.assertEqual(L.choose_fps(None, 120.0), 30)

    def test_the_rate_is_rounded_and_never_below_one(self):
        self.assertEqual(L.choose_fps(29.97, None), 30)
        self.assertEqual(L.choose_fps(23.976, None), 24)
        self.assertEqual(L.choose_fps(0.4, None), 1)

    def test_nothing_known_is_none(self):
        self.assertIsNone(L.choose_fps(None, None))
        self.assertIsNone(L.choose_fps(0, 0))

    def test_every_answer_is_a_rate_the_device_accepts(self):
        for rate in (1, 5, 12, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 240):
            fps = L.choose_fps(rate, None)
            self.assertTrue(1 <= fps <= 30, (rate, fps))


class ProbingASource(unittest.TestCase):
    def setUp(self):
        L._FPS_CACHE.clear()
        self.addCleanup(L._FPS_CACHE.clear)

    def probe(self, url="http://example.invalid/a.m3u8", **kw):
        return L.probe_source_fps(url, "ffmpeg", "")

    def test_it_reads_the_average_rate(self):
        with patch.object(L.subprocess, "run", return_value=probe_says("25/1", "50/1")):
            self.assertEqual(self.probe(), 25)

    def test_a_second_ask_for_the_same_channel_does_not_probe_again(self):
        with patch.object(L.subprocess, "run", return_value=probe_says("25/1", "25/1")) as run:
            self.assertEqual(self.probe(), 25)
            self.assertEqual(self.probe(), 25)
        self.assertEqual(run.call_count, 1)

    def test_a_failed_probe_is_none_and_is_not_remembered(self):
        with patch.object(L.subprocess, "run", return_value=probe_says("25/1", "25/1", 1)):
            self.assertIsNone(self.probe())
        with patch.object(L.subprocess, "run", return_value=probe_says("30/1", "30/1")):
            self.assertEqual(self.probe(), 30, "the earlier failure must not stick")

    def test_a_timeout_a_missing_binary_and_junk_are_all_just_none(self):
        for effect in (subprocess.TimeoutExpired("ffprobe", 5), FileNotFoundError(),
                       OSError("boom")):
            with patch.object(L.subprocess, "run", side_effect=effect):
                self.assertIsNone(self.probe(), effect)
        junk = SimpleNamespace(returncode=0, stdout="not json", stderr="")
        with patch.object(L.subprocess, "run", return_value=junk):
            self.assertIsNone(self.probe())
        none = SimpleNamespace(returncode=0, stdout=json.dumps({"streams": []}), stderr="")
        with patch.object(L.subprocess, "run", return_value=none):
            self.assertIsNone(self.probe())

    def test_a_network_source_is_asked_with_the_user_agent_the_decoder_uses(self):
        with patch.object(L.subprocess, "run", return_value=probe_says("25/1", "25/1")) as run:
            self.probe("http://example.invalid/b.m3u8")
        command = run.call_args[0][0]
        self.assertIn("-user_agent", command)
        self.assertEqual(command[command.index("-user_agent") + 1], frames.DEFAULT_USER_AGENT)

    def test_the_probe_is_bounded_in_time(self):
        with patch.object(L.subprocess, "run", return_value=probe_says("25/1", "25/1")) as run:
            self.probe("http://example.invalid/c.m3u8")
        self.assertEqual(run.call_args[1]["timeout"], L.FPS_PROBE_TIMEOUT_S)


class TheRateReachesEverythingThatUsesIt(unittest.TestCase):
    def test_the_graph_and_the_command_carry_the_rate(self):
        self.assertIn("fps=12,", L.source_graph(12))
        self.assertIn("fps=30,", L.source_graph(30))
        command = L.source_command("x.ts", 1, 2, "ffmpeg", "", 30)
        self.assertIn("fps=30,", command[command.index("-filter_complex") + 1])

    def test_without_a_rate_the_nominal_one_is_used(self):
        command = L.source_command("x.ts", 1, 2, "ffmpeg", "")
        self.assertIn(f"fps={FPS},", command[command.index("-filter_complex") + 1])

    def channel(self):
        channel = L.LiveChannel.__new__(L.LiveChannel)
        channel.url, channel.ffmpeg, channel.user_agent = "http://example.invalid/d", "ffmpeg", ""
        channel.lock = threading.Lock()
        channel._budget = frames.ByteBudget(250_000, FPS)
        return channel

    def test_the_probed_rate_sets_the_timeline_and_the_budget_and_is_kept(self):
        channel = self.channel()
        with patch.dict(os.environ), patch.object(L, "probe_source_fps", return_value=30):
            os.environ.pop("TV_FPS", None)
            self.assertEqual(channel.resolve_fps(), 30)
        self.assertEqual(channel.fps, 30)
        self.assertEqual(channel.fps_note, "probed")
        self.assertAlmostEqual(channel.timeline.video.interval_ms, 1000 / 30)
        self.assertEqual(channel._budget.fps, 30)

    def test_a_source_that_does_not_say_is_played_at_the_nominal_rate(self):
        channel = self.channel()
        with patch.dict(os.environ), patch.object(L, "probe_source_fps", return_value=None):
            os.environ.pop("TV_FPS", None)
            self.assertEqual(channel.resolve_fps(), FPS)
        self.assertIn("did not say", channel.fps_note)

    def test_an_explicit_TV_FPS_wins_and_nothing_is_probed(self):
        channel = self.channel()
        with patch.dict(os.environ, {"TV_FPS": "20"}), \
                patch.object(L, "probe_source_fps") as probe:
            self.assertEqual(channel.resolve_fps(), FPS)
        probe.assert_not_called()
        self.assertEqual(channel.fps_note, "TV_FPS")


def noise_frame(seed):
    n = frames.FRAME_PIXELS
    import random
    return random.Random(seed).getrandbits(8 * n).to_bytes(n, "little")


class AFrameOnItsWayOut(unittest.TestCase):
    def channel(self, rate=250_000):
        channel = L.LiveChannel.__new__(L.LiveChannel)
        channel.lock = threading.RLock()
        channel.video = collections.deque(maxlen=100)
        channel.video_at = collections.deque(maxlen=100)
        channel.video_content = collections.deque(maxlen=100)
        channel.video_epoch = 0.0
        channel.dropped_video = 0
        channel._video_advanced = False
        channel._shown, channel._delta_tick = None, 0
        self.now = 1000.0
        channel._budget = frames.ByteBudget(rate, FPS)
        channel.encoded_video_bytes, channel.encode_seconds, channel.coarse_frames = 0, 0.0, 0
        return channel

    def test_a_queued_frame_is_not_compressed_until_it_is_sent(self):
        channel = self.channel()
        raw = noise_frame(1)
        with patch.object(frames, "DELTA", True), \
                patch.object(frames, "frame_packets", side_effect=AssertionError("compressed at push")):
            channel._push_video(raw, 0.0)
        self.assertEqual(len(channel.video), 1)
        self.assertEqual(channel.video[0].raw, raw)
        self.assertEqual(len(channel.video[0]), 0)

    def test_with_delta_off_the_packets_are_made_at_push_as_before(self):
        channel = self.channel()
        with patch.object(frames, "DELTA", False):
            channel._push_video(noise_frame(2), 0.0)
        self.assertGreater(len(channel.video[0]), 0)
        self.assertFalse(hasattr(channel.video[0], "raw"))

    def queued(self, raw):
        item = L._QueuedFrame()
        item.raw = raw
        return item

    def test_the_first_frame_is_whole(self):
        channel = self.channel()
        raw = noise_frame(3)
        out = channel._delta_encode(self.queued(raw))
        self.assertEqual(sum(1 for s in _unpack(out) if s is not None), frames.STRIPES)
        wire = sum(len(p) + HEADER.size for p in out)
        self.assertEqual(channel.encoded_video_bytes, wire)

    def test_a_repeat_costs_only_the_refresh_stripe(self):
        channel = self.channel()
        raw = noise_frame(4)
        channel._delta_encode(self.queued(raw))
        out = channel._delta_encode(self.queued(raw))
        self.assertEqual(sum(1 for s in _unpack(out) if s is not None), 1)

    def test_a_frame_too_big_for_the_target_is_coarsened_and_counted(self):
        channel = self.channel(rate=100_000)
        out = channel._delta_encode(self.queued(picture_frame()))
        self.assertGreater(channel.coarse_frames, 0)
        self.assertEqual(sum(1 for s in _unpack(out) if s is not None), frames.STRIPES,
                         "every stripe is from this frame, none left out")
        self.assertNotEqual(channel._shown, picture_frame(), "shown is what was drawn")

    def test_a_frame_without_a_raw_copy_passes_through_untouched(self):
        channel = self.channel()
        packets = ["a", "b"]
        self.assertIs(channel._delta_encode(packets), packets)

    def test_the_sender_can_change_the_rate(self):
        channel = self.channel(rate=250_000)
        channel.set_video_rate(120_000)
        self.assertEqual(channel._budget.rate, 120_000)


def picture_frame():
    return bytes((x * 3 + y * 5) % 256 for y in range(frames.HEIGHT) for x in range(frames.WIDTH))


def _unpack(payloads):
    out, expected = [], 0
    for payload in payloads:
        first, count = payload[0], payload[1]
        while len(out) < first:
            out.append(None)
        lengths = [int.from_bytes(payload[2 + 2 * i:4 + 2 * i], "big") for i in range(count)]
        at = 2 + 2 * count
        for n in lengths:
            out.append(payload[at:at + n] or None)
            at += n
        expected += count
    while len(out) < frames.STRIPES:
        out.append(None)
    return out


if __name__ == "__main__":
    unittest.main()
