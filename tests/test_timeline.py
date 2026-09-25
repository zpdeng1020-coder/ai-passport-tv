#!/usr/bin/env python3
"""The content timeline, driven through the counterexamples that broke arrival time.

Every case here is one an external review asked for, or one that has already
failed against the old alignment at least once. They run in milliseconds on the
host, which is the point of keeping the arithmetic in a module: the same
scenarios against a live stream need a channel that misbehaves on cue, and three
rounds of this project were spent arguing about readings that a controlled input
settles immediately.

The headline case is the first one: the same content, delivered 600 ms late,
must keep the same content time.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.timeline import (BASIS_COMMON_DECODE, BASIS_LAUNCH, BASIS_MEDIA_START,
                             ContentTimeline, SessionClock, SourceState, Stream)


VIDEO_MS = 1000 / 12      # twelve frames a second
AUDIO_MS = 40             # one chunk


def timeline(video_interval=VIDEO_MS, audio_interval=AUDIO_MS) -> ContentTimeline:
    """A calibrated pair whose two streams begin together in the media."""
    line = ContentTimeline(video_interval, audio_interval)
    line.calibrate(video_start_s=0.0, audio_start_s=0.0)
    return line


class StreamTests(unittest.TestCase):
    def test_content_time_counts_rather_than_measures(self):
        """A stream's content time comes from its rate, not from a clock.

        Twelve frames a second means the tenth frame is 750 ms of content,
        whenever it turned up here.
        """
        stream = Stream("video", VIDEO_MS)
        stamps = [stream.take() for _ in range(13)]
        self.assertEqual(stamps[0], 0.0)
        self.assertAlmostEqual(stamps[12], 1000.0, places=6)

    def test_a_rejected_rate_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            Stream("video", 0)
        with self.assertRaises(ValueError):
            Stream("video", -1)


class ArrivalDoesNotMoveContentTests(unittest.TestCase):
    """The defect this module exists to remove, in its own test class."""

    def test_content_delivered_late_keeps_its_content_time(self):
        """The review's counterexample, exactly.

        The two decoders started together; only the picture's *delivery* was
        disturbed, by 600 ms. The old rule compared arrivals, so the picture
        stopped matching the sound it belonged to. A counted content clock does
        not see the delay at all -- and neither does the offset, because the
        offset comes from the process starts and not from the first arrival.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(video_start_s=0.0, audio_start_s=0.0)
        frames = [line.video.take() for _ in range(25)]
        chunks = [line.audio.take() for _ in range(25)]
        # Everything below is the same content as the run above; the picture
        # simply spent 600 ms longer getting here. Nothing in the module is
        # told about the delay, because nothing needs to be.
        self.assertAlmostEqual(line.offset_ms, 0.0, places=6)
        # Twelve frames a second against twenty-five chunks a second, so the
        # indices do not run together: the tenth frame is 833 ms of content and
        # the twenty-first chunk is 840 ms. The module pairs by content time,
        # which is why the indices differ and the moments do not.
        self.assertTrue(line.relates(frames[10], chunks[20]))
        self.assertTrue(line.relates(frames[0], chunks[0]))
        # The frame is 10 x the interval of content behind the stream's start,
        # in this run as in any other.
        self.assertAlmostEqual(frames[10] - frames[0], 10 * VIDEO_MS, places=6)

    def test_a_late_first_arrival_does_not_enter_the_offset(self):
        """The regression that the counterexample exposed in the calibration.

        Calibrating from the first arrival put 600 ms of transit into the
        offset, and the streams never paired again. The offset comes from the
        process starts, so a first arrival 600 ms late changes nothing.
        """
        prompt = ContentTimeline(VIDEO_MS, AUDIO_MS)
        prompt.calibrate(video_start_s=0.0, audio_start_s=0.0)
        stalled = ContentTimeline(VIDEO_MS, AUDIO_MS)
        stalled.calibrate(video_start_s=0.0, audio_start_s=0.0)
        self.assertEqual(prompt.offset_ms, stalled.offset_ms)
        for line in (prompt, stalled):
            self.assertTrue(line.relates(line.video.take(), line.audio.take()))

    def test_a_stall_after_calibration_changes_nothing(self):
        """A mid-session delivery stall is not a content offset.

        The reader is blocked for a while and then everything arrives at once.
        Content order and spacing are untouched; only the arrivals moved.
        """
        line = timeline()
        first = [line.video.take() for _ in range(5)]
        # ... the reader stalls here, and nothing about content changed ...
        rest = [line.video.take() for _ in range(5)]
        self.assertAlmostEqual(rest[0] - first[-1], VIDEO_MS, places=6)
        self.assertAlmostEqual(rest[-1] - first[0], 9 * VIDEO_MS, places=6)

    def test_the_offset_is_measured_once_and_then_refused(self):
        """Recalibrating from later arrivals is how a stall becomes an offset.

        Two independent decoders have one relationship, fixed at startup. A
        second observation is not a correction, it is the defect.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        self.assertTrue(line.calibrate(0.0, 0.3))
        self.assertFalse(line.calibrate(0.0, 0.9))
        self.assertFalse(line.calibrate(0.0, -1.2))
        self.assertEqual(line.recalibrations_refused, 2)
        # The first observation is the one in force.
        self.assertAlmostEqual(line.offset_ms, 300.0, places=6)

    def test_pairing_is_refused_before_the_origins_are_related(self):
        """A session that has not seen both streams cannot say what matches.

        Refusing is the answer; guessing is what turned a startup gap into a
        lip-sync error.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        self.assertFalse(line.calibrated)
        self.assertEqual(line.verdict(0.0, 0.0), "unrelated")
        self.assertFalse(line.relates(0.0, 0.0))
        with self.assertRaises(ValueError):
            line.audio_in_video_units(0.0)


class AgainstTheOldMethodTests(unittest.TestCase):
    """The same input through both alignments, so the difference is a fact.

    The old rule is reproduced here as arithmetic rather than described. It is
    `arrival - own process start`, and the point of putting it beside the new
    one is that a reader can check the claim instead of taking it.
    """

    TOLERANCE_S = 0.35   # VIDEO_SYNC_TOLERANCE_S, the old constant

    @staticmethod
    def old_method_matches(video_arrival_s, audio_arrival_s,
                           video_launch_s, audio_launch_s):
        video_at = video_arrival_s - video_launch_s
        audio_at = audio_arrival_s - audio_launch_s
        return abs(video_at - audio_at) <= AgainstTheOldMethodTests.TOLERANCE_S

    def test_the_old_method_fails_the_review_counterexample(self):
        """Same content, the picture delivered 600 ms late, one decoder each.

        Both decoders started at the same instant, so the old rule compares the
        arrivals directly -- and 600 ms is past its 350 ms tolerance, so the
        picture stops pairing with the sound it belongs to.
        """
        self.assertFalse(self.old_method_matches(
            video_arrival_s=3.6, audio_arrival_s=3.0,
            video_launch_s=0.0, audio_launch_s=0.0))

    def test_the_new_method_passes_it(self):
        """The same late delivery, through a counted content clock.

        Both items are the same moment of content, so their stamps are equal
        however late one of them arrived.
        """
        line = timeline()
        video_stamp = line.video.take()      # arrives 600 ms late; stamp unmoved
        audio_stamp = line.audio.take()
        self.assertTrue(line.relates(video_stamp, audio_stamp))

    def test_both_methods_agree_when_delivery_is_prompt(self):
        """The new rule is not merely more permissive.

        On a quiet link the two agree, which is why the defect took so long to
        show: it only appears when delivery is disturbed.
        """
        self.assertTrue(self.old_method_matches(3.0, 3.0, 0.0, 0.0))
        line = timeline()
        self.assertTrue(line.relates(line.video.take(), line.audio.take()))


class LaunchTimeIsNotContentTests(unittest.TestCase):
    """The fifth review's two counterexamples, from real ffmpeg.

    Both were produced by launching the project's own `LiveChannel` against
    locally generated files, and both come from the same mistake in different
    directions: **taking something about the reader for something about the
    programme.** The launch time of a process says when it starts producing, not
    which part of the media it produces.
    """

    def test_delaying_a_process_does_not_move_the_content(self):
        """Counterexample one: same file, audio process launched 600 ms later.

        The media did not change, so the offset must not either. Calibrating
        from the launches made it +600 ms and the picture's first four frames
        were discarded as "late" -- content thrown away because a process was
        scheduled later.
        """
        media_relation = (0.0, 0.0)          # both streams begin together
        prompt = ContentTimeline(VIDEO_MS, AUDIO_MS)
        prompt.calibrate(*media_relation)
        delayed = ContentTimeline(VIDEO_MS, AUDIO_MS)
        delayed.calibrate(*media_relation)   # launches are not consulted
        self.assertEqual(prompt.offset_ms, delayed.offset_ms)
        self.assertEqual(delayed.offset_ms, 0.0)

        # And content still pairs by moment, so nothing is discarded for being
        # "late". The indices differ because the rates do: a frame every 83.3 ms
        # against a chunk every 40 ms, so the fifth frame (333 ms) belongs with
        # the ninth chunk (320 ms).
        frames = [delayed.video.take() for _ in range(5)]
        chunks = [delayed.audio.take() for _ in range(9)]
        self.assertTrue(delayed.relates(frames[4], chunks[8]))
        self.assertEqual(delayed.verdict(frames[4], chunks[8]), "together")

    def test_a_real_offset_in_the_media_is_kept(self):
        """Counterexample two: the audio genuinely starts 600 ms into the file.

        This difference belongs to the programme and must survive. Calibrating
        from the launches erased it -- the offset came out near zero because
        both processes had been started at the same moment.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(video_start_s=0.0, audio_start_s=0.6)
        self.assertAlmostEqual(line.offset_ms, 600.0, places=6)
        # The audio's first chunk is 600 ms of content, not 0: the media really
        # does begin there, and a stream that begins late has less content.
        self.assertAlmostEqual(line.audio.take(), 0.0, places=6)
        self.assertEqual(line.audio_in_video_units(0.0), 600.0)

    def test_the_offset_comes_from_the_media_and_nowhere_else(self):
        """The three candidates, side by side, on one input.

        Arrival and launch are both reader-side. Only the media start time
        answers the question the offset is asked, and the two failures above are
        what happens when the others are used instead.
        """
        media = (0.0, 0.6)
        arrivals = (2.4, 1.9)        # the picture turned up later than the sound
        launches = (0.0, 0.0)        # both processes started together

        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(*media)
        self.assertEqual(line.offset_ms, 600.0)
        # Neither of the other two appears in it.
        self.assertNotAlmostEqual(line.offset_ms,
                                  (arrivals[1] - arrivals[0]) * 1000, places=3)
        self.assertNotAlmostEqual(line.offset_ms,
                                  (launches[1] - launches[0]) * 1000, places=3)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.line = timeline()

    def test_the_verdict_names_all_five_cases(self):
        """"No picture yet" and "no picture at all" have opposite remedies."""
        self.assertEqual(self.line.verdict(None, 0.0), "no-video")
        self.assertEqual(self.line.verdict(0.0, None), "no-audio")
        self.assertEqual(self.line.verdict(0.0, 0.0), "together")
        # The picture queue's front is 400 ms NEWER than the sound being played:
        # that frame has not reached its moment, so it is early.
        self.assertEqual(self.line.verdict(400.0, 0.0), "video-early")
        # Older than the sound: that moment has passed.
        self.assertEqual(self.line.verdict(-400.0, 0.0), "video-late")

    def test_the_tolerance_is_the_audio_chunk(self):
        """Within one chunk is not an offset a viewer could see."""
        self.line.tolerance_ms = 40
        self.assertEqual(self.line.verdict(40.0, 0.0), "together")
        self.assertEqual(self.line.verdict(41.0, 0.0), "video-early")

    def test_the_slower_decoder_is_the_one_whose_content_lags(self):
        """The sign of the offset, checked against a worked example.

        Audio started 600 ms later, so at any wall-clock instant the audio
        stream has 600 ms LESS content than the video stream. Audio content
        `a` therefore belongs with video content `a + 600`, and comparing them
        directly is 600 ms wrong -- which is the error the offset removes.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(video_start_s=0.0, audio_start_s=0.6)
        self.assertEqual(line.offset_ms, 600.0)
        self.assertEqual(line.audio_in_video_units(600.0), 1200.0)
        self.assertEqual(line.verdict(1200.0, 600.0), "together")
        self.assertEqual(line.verdict(600.0, 600.0), "video-late")

    def test_a_nonzero_startup_gap_does_not_show_as_an_offset(self):
        """The reason the offset exists: two decoders, started apart.

        With the offset calibrated, content that IS simultaneous pairs, even
        though it arrived a third of a second apart.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(video_start_s=0.0, audio_start_s=0.35)
        for _ in range(20):
            video_stamp = line.video.take()
        for _ in range(6):
            audio_stamp = line.audio.take()
        # The video's 19th frame is 1583 ms of content; the audio's 5th chunk is
        # 200 ms. They are not the same moment, and the verdict says which way.
        self.assertEqual(line.verdict(video_stamp, audio_stamp), "video-early")
        # Expressed on the sound's clock instead, they pair.
        simultaneous_audio = line.video_in_audio_units(video_stamp)
        self.assertEqual(line.verdict(video_stamp, simultaneous_audio), "together")


class MultiPacketFrameTests(unittest.TestCase):
    """A frame that crosses as several packets carries ONE content time.

    The acceptance script for this was checked with a black frame, which encodes
    to a single packet -- so it demonstrated nothing about the case it named. An
    external review produced the counterexample with a fixed-seed noise frame at
    the same geometry: **four packets, 77003 bytes**. Multi-packet frames are not
    a corner case here; they are what ordinary content does.
    """

    def test_a_loud_frame_really_does_split(self):
        """The premise, measured rather than assumed."""
        import random
        from server import frames
        random.seed(1234)
        noise = bytes(random.randrange(256) for _ in range(frames.WIDTH * frames.HEIGHT))
        packets = frames.frame_packets(noise)
        self.assertGreater(len(packets), 1,
                           "the fixture no longer splits; pick another seed")

    def test_the_packets_of_one_frame_share_one_content_time(self):
        """Alignment stamps the frame, not each packet.

        `pop_video()` returns the list of packets for one frame and the content
        time belongs to the frame; the packets carry no stamp of their own, so
        there is nothing for them to disagree about. This checks the shape the
        sender relies on: one entry in `video_content` per frame pushed.
        """
        import collections, threading
        import server.live as L
        from server.timeline import ContentTimeline, SourceState
        channel = L.LiveChannel.__new__(L.LiveChannel)
        channel.lock = threading.RLock()
        channel.video = collections.deque(maxlen=100)
        channel.video_at = collections.deque(maxlen=100)
        channel.video_epoch = 0.0
        channel.audio_epoch = 0.0
        channel.video_content = collections.deque(maxlen=100)
        channel.audio = collections.deque(maxlen=100)
        channel.audio_at = collections.deque(maxlen=100)
        channel.audio_content = collections.deque(maxlen=100)
        channel.dropped_video = 0
        channel.skipped_audio = 0
        channel.source = SourceState()
        channel.session_clock = SessionClock(chunk_ms=AUDIO_MS)
        channel.timeline = ContentTimeline(VIDEO_MS, AUDIO_MS)
        channel.timeline.calibrate(video_start_s=0.0, audio_start_s=0.0)
        channel._video_advanced = channel._audio_advanced = False

        import random
        from server import frames
        random.seed(1234)
        noise = bytes(random.randrange(256) for _ in range(frames.WIDTH * frames.HEIGHT))
        packets = frames.frame_packets(noise)
        self.assertGreater(len(packets), 1)

        # One frame pushed: one content stamp, and the packet list stays whole.
        channel._push_video(noise, 0.0)
        self.assertEqual(len(channel.video), 1, "one frame is one queue entry")
        self.assertEqual(len(channel.video_content), 1,
                         "one frame is one content stamp, however many packets")
        self.assertEqual(len(channel.video[0]), len(packets))

        # And it is returned as one unit. Several blocks of sound are queued,
        # because the sender takes the sound first in every pass and `pop_video`
        # then pairs the picture against the sound still waiting -- with one
        # block queued there would be nothing left to pair against, which is a
        # fixture artefact and not the condition under test. A real session
        # holds seconds of sound.
        for _ in range(10):
            channel.audio.append(b"a")
            channel.audio_at.append(0.0)
            channel.audio_content.append(channel.timeline.audio.take())
        channel.pop_audio()
        got = channel.pop_video()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], packets, "all packets of the frame leave together")
        # One session timestamp for the whole frame, and it is an integer
        # because the wire carries milliseconds.
        self.assertIsInstance(got[1], int)


class TheFrameIsStampedWithItsOwnContentTests(unittest.TestCase):
    """The mapping defect an external review found by construction.

    The stamp used to be the session position of *the sound the frame was paired
    with*, so a frame whose content was older than that sound was drawn when the
    sound reached the sound's moment -- the picture was late by exactly the
    difference the pairing had tolerated, and the code could not see it because
    it had thrown the difference away.
    """

    CHUNK = 40.0

    def test_a_frame_older_than_its_sound_keeps_the_difference(self):
        """The reviewer's measurement, as an assertion.

        `666.667 ms` of picture paired with `1000 ms` of sound was stamped
        `1000` -- 333 ms of error added by the stamping, in a stream with no
        audio gap at all.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(25):
            clock.audio(1000.0 + i * self.CHUNK)
        # The frame's own content, on the sound's clock: three chunks behind the
        # sound at the head of the queue.
        head = 1000.0 + 25 * self.CHUNK
        stamp = clock.video(head - 3 * self.CHUNK)
        self.assertEqual(stamp, int(round(25 * self.CHUNK - 3 * self.CHUNK)))
        self.assertNotEqual(stamp, int(round(25 * self.CHUNK)),
                            "the frame was stamped with the sound's position, "
                            "not its own")

    def test_a_frame_ahead_of_its_sound_also_keeps_the_difference(self):
        """Both signs, because a fix that only works one way is a sign error."""
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(25):
            clock.audio(1000.0 + i * self.CHUNK)
        head = 1000.0 + 25 * self.CHUNK
        # Taken in content order, because the wire timestamp is held strictly
        # increasing: asking for a later frame first would make the earlier one
        # a tie to break, which is a different test.
        behind = clock.video(head - 2 * self.CHUNK)
        ahead = clock.video(head + 2 * self.CHUNK)
        self.assertEqual(behind, int(round(23 * self.CHUNK)))
        self.assertEqual(ahead, int(round(27 * self.CHUNK)))

    def test_the_mapping_is_still_strictly_increasing(self):
        """The device refuses a video timestamp that does not advance.

        Frames whose content is out of order, or two frames inside one chunk,
        must still produce rising timestamps -- the device has no other way to
        tell a reordered stream from a corrupt one.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(10):
            clock.audio(0.0)
        stamps = [clock.video(v) for v in (500.0, 100.0, 300.0, 300.0, 300.0)]
        for earlier, later in zip(stamps, stamps[1:]):
            self.assertLess(earlier, later, f"{stamps}")
        self.assertEqual(len(set(stamps)), len(stamps))

    def test_the_first_sound_anchors_zero(self):
        """Content time is not session time, and this is where they are related.

        A source joined at minute nine has content time 540000; the device
        requires the session to start at zero.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        self.assertIsNone(clock.video(540000.0),
                          "no session time exists before any sound")
        self.assertFalse(clock.anchored)
        self.assertEqual(clock.audio(540000.0), 0)
        self.assertTrue(clock.anchored)
        self.assertEqual(clock.video(540000.0), 0)
        self.assertEqual(clock.video(540000.0 + self.CHUNK), int(self.CHUNK))


class AContentGapStartsANewSegmentTests(unittest.TestCase):
    """A dropped block removes content time without removing session time.

    The map is therefore piecewise. Fitting one line through both sides of a
    discontinuity would place every later picture at the wrong moment by the
    size of the gap.
    """

    CHUNK = 40.0

    def test_the_session_clock_stays_contiguous_across_a_skip(self):
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(0.0)
        clock.audio(self.CHUNK)
        after = clock.audio(10 * self.CHUNK)      # the source jumped
        self.assertEqual(after, int(2 * self.CHUNK),
                         "the device requires one chunk per block received")

    def test_content_after_the_skip_maps_relative_to_the_new_segment(self):
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(0.0)
        clock.audio(self.CHUNK)
        skipped_at = 10 * self.CHUNK
        clock.audio(skipped_at)
        # The block received at `skipped_at` is session 2*chunk, so content at
        # `skipped_at` maps there and not to a value 8 chunks earlier.
        self.assertEqual(clock.video(skipped_at), int(2 * self.CHUNK))
        self.assertEqual(clock.video(skipped_at + self.CHUNK),
                         int(3 * self.CHUNK))

    def test_content_before_the_skip_is_unaffected_by_it(self):
        """The segment boundary may not move what came before it."""
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(0.0)
        clock.audio(self.CHUNK)
        before = clock.video(self.CHUNK)
        clock.audio(50 * self.CHUNK)
        # A fresh clock that never saw the skip, for comparison.
        other = SessionClock(chunk_ms=self.CHUNK)
        other.audio(0.0)
        other.audio(self.CHUNK)
        self.assertEqual(before, other.video(self.CHUNK))

    def test_the_gap_is_measured_and_counted(self):
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(1000.0)
        clock.audio(1040.0)
        clock.audio(3040.0)
        self.assertEqual(clock.audio_gaps, 1)
        self.assertEqual(clock.content_gap_ms, 1960)


class AFrameInsideADroppedIntervalTests(unittest.TestCase):
    """The mapping defect an external review found after the previous fix.

    A gap is only *known* once the sound after it arrives. Frames paired just
    before that moment are stamped while the segment is still open, so the map
    extrapolates the segment before the gap forward across it -- giving a picture
    a session time the session's sound never reached. The review measured the
    consequence: the recovery frame, which maps correctly, was then pushed up by
    the strictly-increasing rule to follow those frames.
    """

    CHUNK = 40.0

    def _clock_in_a_gap(self):
        """A clock whose newest segment starts after a 4.88 s hole."""
        clock = SessionClock(chunk_ms=self.CHUNK, tolerance_ms=350.0)
        for i in range(3):
            clock.audio(1000.0 + i * self.CHUNK)
        # 122 blocks were never produced; the next one is at content 5000 ms.
        clock.audio(5000.0)
        return clock

    def test_content_inside_the_hole_has_no_session_time(self):
        """Not "a wrong time" -- no time at all. The sound was never heard."""
        clock = self._clock_in_a_gap()
        for content in (4750.0, 4833.333, 4916.667):
            with self.subTest(content=content):
                self.assertIsNone(clock.session_of(content))
                self.assertIsNone(clock.video(content))

    def test_a_refused_frame_does_not_advance_the_picture_clock(self):
        """The clamp is what turned a bad frame into a 4.8 s error.

        With no frame inside the gap given a value, the recovery frame's own
        correct session time survives.
        """
        clock = self._clock_in_a_gap()
        for content in (4750.0, 4833.333, 4916.667):
            self.assertIsNone(clock.video(content))
        self.assertEqual(clock.last_video_ms, -1)
        recovered = clock.video(5000.0)
        self.assertEqual(recovered, int(3 * self.CHUNK),
                         "the recovery frame must land on the sound's position")

    def test_the_refusals_are_counted(self):
        """A silent refusal is indistinguishable from a frame never offered."""
        clock = self._clock_in_a_gap()
        for content in (4750.0, 4833.333, 4916.667):
            clock.video(content)
        self.assertEqual(clock.frames_unplaceable, 3)
        self.assertIn("unplaceable=3", clock.diagnostics())

    def test_content_after_the_hole_maps_normally(self):
        """The new segment runs parallel to content from its own start."""
        clock = self._clock_in_a_gap()
        self.assertEqual(clock.session_of(5000.0), 3 * self.CHUNK)
        self.assertEqual(clock.session_of(5040.0), 4 * self.CHUNK)
        # 1000 ms further into the programme is 1000 ms further into the
        # session: the segment advances at one millisecond per millisecond.
        self.assertEqual(clock.session_of(6000.0), 3 * self.CHUNK + 1000.0)

    def test_content_before_the_hole_still_maps(self):
        """The guard may not disable the ordinary case it replaced."""
        clock = self._clock_in_a_gap()
        self.assertEqual(clock.session_of(1000.0), 0.0)
        self.assertEqual(clock.session_of(1040.0), self.CHUNK)

    def test_a_consistent_stream_is_not_refused(self):
        """The agreement guard must not fire on a healthy stream.

        This is the test that keeps the fix from becoming a blanket refusal: a
        picture paired within tolerance of the sound at the head of the queue
        has session positions that differ by no more than the tolerance.
        """
        clock = SessionClock(chunk_ms=self.CHUNK, tolerance_ms=350.0)
        for i in range(50):
            clock.audio(0.0 + i * self.CHUNK)
        # Take frames at their own content times, always near the head.
        for i in range(10, 40):
            content = i * self.CHUNK
            stamp = clock.video(content)
            self.assertIsNotNone(stamp, f"frame at {content} was refused")
        self.assertEqual(clock.frames_unplaceable, 0)

    def test_a_frame_far_from_the_sound_at_the_head_is_refused(self):
        """The guard itself, isolated from the map.

        A picture whose session position is further from the sound's than the
        pairing tolerance cannot have come from a pairing, so something is
        wrong with the map and the frame gets no timestamp.
        """
        clock = SessionClock(chunk_ms=self.CHUNK, tolerance_ms=350.0)
        for i in range(50):
            clock.audio(0.0 + i * self.CHUNK)
        # 50 blocks taken, so the newest sound is at 2000 ms and the head -- not
        # yet taken -- is one chunk beyond. A picture claiming 5000 ms is
        # claiming a moment four seconds of sound away from anything heard.
        self.assertIsNone(clock.video(5000.0),
                          "a frame seconds ahead of the newest sound must be "
                          "refused rather than clamped into the next frame")
        # And the boundary is the head plus one pairing tolerance, so a frame
        # just inside it is still placed.
        self.assertIsNotNone(clock.video(50 * self.CHUNK + 350.0))


class CalibrationBasisTests(unittest.TestCase):
    """Which basis related the two clocks, and why it matters that it is named.

    A settlement on the number alone is not enough. The same 600 ms offset means
    "the media starts 600 ms apart" or "the second process started 600 ms later"
    depending entirely on which basis produced it, and those have opposite
    meanings -- one is a fact about the programme and the other is a fact about
    the scheduler. The project has shipped each of them, wrongly, in turn.
    """

    def test_a_media_start_calibration_records_its_basis(self):
        timeline = ContentTimeline(100.0, 40.0)
        self.assertTrue(timeline.calibrate(0.0, 0.6))
        self.assertEqual(timeline.offset_ms, 600.0)
        self.assertEqual(timeline.basis, BASIS_MEDIA_START)

    def test_a_launch_calibration_is_not_mistaken_for_a_media_one(self):
        """The live-source case, which the file counterexample does not cover.

        These are physically different situations and the review's argument
        applies to only one of them. A file has content whether or not anyone
        decodes it, so launch order says nothing about which part is produced. A
        live source has no content before a decoder attaches, so both processes
        attach to the same live edge and the launch difference IS the content
        difference. Storing the basis is what keeps the distinction visible
        instead of leaving one number to be read as the other.
        """
        timeline = ContentTimeline(100.0, 40.0)
        timeline.calibrate(0.0, 0.6, basis=BASIS_LAUNCH)
        self.assertEqual(timeline.offset_ms, 600.0)   # same number ...
        self.assertNotEqual(timeline.basis, BASIS_MEDIA_START)  # ... other meaning
        self.assertIn("launch", timeline.basis)

    def test_the_basis_is_empty_until_something_calibrates(self):
        """An offset of None and an offset of 0 are different states."""
        timeline = ContentTimeline(100.0, 40.0)
        self.assertFalse(timeline.calibrated)
        self.assertEqual(timeline.basis, "")
        self.assertIsNone(timeline.offset_ms)

    def test_the_basis_survives_a_refused_recalibration(self):
        """A second call must not silently relabel the first one's basis."""
        timeline = ContentTimeline(100.0, 40.0)
        timeline.calibrate(0.0, 0.6)
        self.assertFalse(timeline.calibrate(0.0, 9.0, basis=BASIS_LAUNCH))
        self.assertEqual(timeline.offset_ms, 600.0)
        self.assertEqual(timeline.basis, BASIS_MEDIA_START)
        self.assertEqual(timeline.recalibrations_refused, 1)


class SessionClockTests(unittest.TestCase):
    """Content time turned into wire time, which is where it was being lost.

    The device's playback clock is `submitted_samples / 16000`: it counts sound
    it has actually received. So a wire timestamp is a property of the session,
    and content time is a property of the programme, and the two are the same
    number only while nothing has been dropped.
    """

    CHUNK = 40.0

    def test_the_sound_starts_at_zero_and_advances_by_one_chunk(self):
        """The device refuses anything else, so this is a contract, not a style.

        `av_stream_accept()` requires each audio packet's timestamp to be
        exactly the previous one plus `AV_AUDIO_MS`, and the first to be zero.
        Content time does not have those properties -- a stream joined at
        minute nine has content time 540000 -- so it cannot be sent as it is.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        stamps = [clock.audio(540000 + i * self.CHUNK) for i in range(5)]
        self.assertEqual(stamps, [0, 40, 80, 120, 160])

    def test_a_content_skip_does_not_break_the_wire_sequence(self):
        """The sound the sender dropped was never heard and occupies no time.

        This is the case the old sender got wrong in the other direction: it
        counted what it sent, so it could not tell a skip from a steady stream
        and reported nothing. Here the wire stays contiguous, because the
        device requires it, and the skip is counted separately where an
        operator can see it.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(1000.0)
        clock.audio(1040.0)
        after = clock.audio(3040.0)
        self.assertEqual(after, 80, "the wire must stay contiguous")
        self.assertEqual(clock.audio_gaps, 1)
        # 1960 and not 2000: the blocks that came in cover [1000,1080) and
        # [3040,3080), and what is missing is the 1960 ms between them. The
        # distance between two stamps includes the second one's own length,
        # which was received and therefore was heard.
        self.assertEqual(clock.content_gap_ms, 1960)

    def test_an_ordinary_chunk_is_not_counted_as_a_gap(self):
        """A gap counter that fires on every chunk is not a gap counter."""
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(20):
            clock.audio(7.0 + i * self.CHUNK)
        self.assertEqual((clock.audio_gaps, clock.content_gap_ms), (0, 0))

    def test_the_picture_is_placed_on_the_sound_clock(self):
        """The property that survived the correction, stated the new way.

        A picture's timestamp is still a position on the sound's clock and in
        the sound's units -- that is what the device compares it against. What
        changed is *which* position: the picture's own content, not the position
        of the sound it was paired with. Taking several frames at their own
        content times must therefore give those content times, mapped.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(10):
            clock.audio(0.0 + i * self.CHUNK)
        # Frames near the sound at the head of the queue, which is the only
        # kind `pop_video` ever selects: the guard in `video` refuses a frame
        # whose session position is further from the head's than the pairing
        # tolerance, so a frame ten chunks adrift is not a case to assert about.
        for i in (8, 9, 10):
            content = i * self.CHUNK
            self.assertEqual(clock.video(content), int(round(content)))

    def test_a_repeated_chunk_breaks_the_tie_by_a_millisecond(self):
        """The one case where the picture's stamp is not exactly the sound's.

        Two pictures inside one chunk of sound have no different moments to be
        given, and the device refuses a video timestamp that does not advance.
        So the second one is put a millisecond later. Stated as a bound rather
        than as equality, because the value is not a sound position and
        pretending otherwise is how a test starts lying: what matters is that
        the error is bounded by the number of pictures crammed into one chunk,
        and that the next chunk recovers exactly.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(0.0)
        at = 3 * self.CHUNK
        first, second, third = (clock.video(at) for _ in range(3))
        self.assertEqual(first, 120)
        self.assertEqual(second, 121)
        self.assertEqual(third, 122)
        self.assertLess(third - first, self.CHUNK)
        self.assertEqual(clock.video(4 * self.CHUNK), 160,
                         "the next chunk is exact again")

    def test_two_pictures_in_one_chunk_still_advance(self):
        """The device rejects a video timestamp that does not advance.

        Two pictures inside one chunk of sound cannot be told apart by time at
        all, so the tie is broken by a millisecond. This is the one place a
        counter survives, and it is bounded to stay inside the chunk it breaks
        the tie in.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        clock.audio(0.0)
        stamps = [clock.video(0.0) for _ in range(4)]
        self.assertEqual(stamps, [0, 1, 2, 3])
        self.assertEqual(stamps, sorted(set(stamps)))
        # And the next chunk still lands where it should.
        self.assertEqual(clock.video(self.CHUNK), 40)

    def test_the_drift_the_wire_cannot_show_is_measured(self):
        """Contiguous timestamps are what the device needs and what hides drift.

        Because the picture is stamped with the sound's position, the two
        advance together by construction and their difference says nothing. The
        drift has to be measured at the pairing, on content time, which is the
        only place both clocks are visible.

        Stated as a mean rather than as a count of unequal pairs, because a
        count of unequal pairs is not a measurement of anything: a picture is
        placed *within a tolerance* of its sound and never exactly on it, so
        every healthy stream produces such pairs and the count says only how
        many frames were sent.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        for _ in range(10):
            clock.audio(0.0)
        # Steady: the picture sits 20 ms behind its sound every time.
        for i in range(20):
            clock.note_pair(video_content_ms=1000.0 + i * 100 - 20,
                            audio_content_ms=1000.0 + i * 100)
        self.assertEqual(clock.pairs, 20)
        self.assertEqual(round(clock.residual_sum_ms / clock.pairs), -20)
        self.assertEqual(clock.residual_worst_ms, -20)

    def test_a_walking_residual_is_visible_where_the_mean_would_hide_it(self):
        """A mean alone can sit at zero while the error grows without bound.

        The first half of a session with the picture 40 ms early and the second
        half 60 ms late averages to -10 ms, which reads as almost aligned. The
        worst residual is what refuses that reading, and it is why the report
        carries both. The worst is kept by magnitude and ties go to the first
        one seen, which is why the two halves are not symmetric here.
        """
        clock = SessionClock(chunk_ms=self.CHUNK)
        for i in range(10):
            clock.note_pair(video_content_ms=1000.0 + i * 100 + 40,
                            audio_content_ms=1000.0 + i * 100)
        for i in range(10):
            clock.note_pair(video_content_ms=3000.0 + i * 100 - 60,
                            audio_content_ms=3000.0 + i * 100)
        self.assertEqual(round(clock.residual_sum_ms / clock.pairs), -10)
        self.assertEqual(clock.residual_worst_ms, -60)

    def test_a_new_session_starts_at_zero(self):
        """A channel change is a new session and the device re-anchors."""
        clock = SessionClock(chunk_ms=self.CHUNK)
        for _ in range(5):
            clock.audio(5000.0)
        clock.reset()
        self.assertEqual(clock.audio(5000.0), 0)
        self.assertEqual(clock.audio_items, 1)
        self.assertEqual(clock.diagnostics().count("gaps=0/0ms"), 1)


class SourceStateTests(unittest.TestCase):
    """Telling a stopped source from a full link, which look identical."""

    def test_a_quiet_window_is_not_yet_starvation(self):
        """An HLS origin delivers in bursts; one quiet window means nothing."""
        state = SourceState(stalled_windows_before_starved=2)
        state.observe(video_advanced=False, audio_advanced=False,
                      queue_over_bound=False)
        self.assertEqual(state.state, SourceState.FLOWING)
        self.assertIn("1/2", state.note)

    def test_a_run_of_quiet_windows_is_starvation(self):
        state = SourceState(stalled_windows_before_starved=2)
        state.observe(False, False, False)
        state.observe(False, False, False)
        self.assertEqual(state.state, SourceState.STARVED)
        self.assertFalse(state.measured)

    def test_content_arriving_with_a_full_queue_is_congestion(self):
        """The other case, and the one the rate should answer."""
        state = SourceState()
        state.observe(video_advanced=True, audio_advanced=True,
                      queue_over_bound=True)
        self.assertEqual(state.state, SourceState.CONGESTED)
        self.assertTrue(state.measured)

    def test_content_arriving_with_room_is_flowing(self):
        state = SourceState()
        state.observe(True, True, False)
        self.assertEqual(state.state, SourceState.FLOWING)
        self.assertTrue(state.measured)

    def test_the_two_states_are_distinguishable(self):
        """The whole point: an empty window and a full one are not one reading.

        This is what the controller could not tell apart, and why it once read
        an empty window as spare capacity.
        """
        starved = SourceState(stalled_windows_before_starved=1)
        starved.observe(False, False, False)
        congested = SourceState(stalled_windows_before_starved=1)
        congested.observe(True, True, True)
        self.assertNotEqual(starved.state, congested.state)
        self.assertFalse(starved.measured)
        self.assertTrue(congested.measured)

    def test_content_returning_clears_starvation(self):
        """Recovery is a state, not a latch."""
        state = SourceState(stalled_windows_before_starved=1)
        state.observe(False, False, False)
        self.assertEqual(state.state, SourceState.STARVED)
        state.observe(True, True, False)
        self.assertEqual(state.state, SourceState.FLOWING)
        self.assertTrue(state.measured)

    def test_only_one_of_the_two_streams_needs_to_be_advancing(self):
        """Audio alone keeps a session worth measuring."""
        state = SourceState(stalled_windows_before_starved=1)
        state.observe(False, True, False)
        self.assertEqual(state.state, SourceState.FLOWING)


class RecoveryTests(unittest.TestCase):
    """What happens across a starvation, which is where an offset would stick."""

    def test_content_time_does_not_drift_across_an_outage(self):
        """A gap in delivery must not become a gap in content.

        The clock is counted, so a stream that produced 100 items is at
        100 x interval regardless of how the arrivals were spaced. That is what
        stops a starvation from accumulating an offset afterwards.
        """
        line = timeline()
        stamps = []
        for _ in range(10):
            stamps.append(line.video.take())
        # The reader is blocked for two seconds; no items are produced.
        for _ in range(10):
            stamps.append(line.video.take())
        self.assertAlmostEqual(stamps[10] - stamps[9], VIDEO_MS, places=6)
        self.assertAlmostEqual(stamps[19] - stamps[0], 19 * VIDEO_MS, places=6)

    def test_the_origin_survives_a_reset_of_the_count(self):
        """A new session re-counts; it must also re-calibrate.

        Keeping the old offset across a restart would apply the previous
        session's startup gap to a new pair of processes, which is exactly the
        class of error this module removes.
        """
        line = timeline()
        self.assertTrue(line.calibrated)
        line.video.reset()
        line.audio.reset()
        self.assertEqual(line.video.count, 0)
        # The offset belongs to the pair of processes, so a reset clears it too.
        self.assertTrue(line.calibrated)   # caller decides; reset does not clear it


class TheSoundOneBlockAheadIsVisibleTests(unittest.TestCase):
    """A gap is known before the sound after it is taken, if the queue is asked.

    The previous fix closed a segment only when the sound *following* the gap
    was taken. Until then the open segment extended forward across the hole, so
    a picture whose content sat inside the hole was stamped with a session time
    no sound occupies. An external review measured it on the shortest useful
    case and gave the numbers this class asserts:

        sound taken at content 0, 40, 80 -> session 0, 40, 80
        the queue's head is content 400, not yet taken, so the hole is [120, 400)
        a picture at content 333.333 is inside that hole
        the recovery picture at content 416.667 belongs at session 137

    Without the queue's head the same sequence stamps the 333.333 picture at
    333, and the strictly-increasing rule then clamps the recovery picture from
    137 up to 334 -- a 197 ms error.
    """

    CHUNK = 40.0

    def taken_and_one_pending(self):
        """Three blocks taken, the fourth visible in the queue but not taken."""
        clock = SessionClock(chunk_ms=self.CHUNK)
        self.assertEqual([clock.audio(0.0), clock.audio(40.0), clock.audio(80.0)],
                         [0, 40, 80])
        return clock

    def test_a_picture_inside_the_hole_is_refused_not_extrapolated(self):
        """Asked for the picture before the recovery sound: the first order."""
        clock = self.taken_and_one_pending()
        verdict, stamp = clock.placement(333.333, pending_audio_ms=400.0)
        self.assertEqual(verdict, "in-dropped-sound")
        self.assertIsNone(stamp)
        self.assertEqual(clock.frames_in_hole, 1)

    def test_the_recovery_picture_gets_its_own_moment(self):
        """Asked for the picture after the recovery sound: the second order."""
        clock = self.taken_and_one_pending()
        # The recovery sound arrives on the wire as session 120, not 400.
        self.assertEqual(clock.audio(400.0), 120)
        # Nothing was stamped in between, so the clamp has nothing to clamp to.
        self.assertEqual(clock.video(416.667, pending_audio_ms=440.0), 137)

    def test_both_orders_agree(self):
        """The two call orders are the same session, so they must not differ."""
        before = self.taken_and_one_pending()
        self.assertEqual(before.placement(333.333, pending_audio_ms=400.0)[0],
                         "in-dropped-sound")
        before.audio(400.0)
        after = self.taken_and_one_pending()
        after.audio(400.0)
        self.assertEqual(before.video(416.667, pending_audio_ms=440.0),
                         after.video(416.667, pending_audio_ms=440.0))

    def test_the_recovery_picture_is_not_clamped_when_nothing_was_stamped(self):
        """The measured 197 ms error, as an assertion.

        Stamping the in-hole picture at 333 is what pushed the recovery frame
        to 334. With the in-hole picture refused, last_video_ms is untouched.
        """
        clock = self.taken_and_one_pending()
        clock.placement(333.333, pending_audio_ms=400.0)
        clock.audio(400.0)
        self.assertEqual(clock.last_video_ms, -1)
        self.assertEqual(clock.video(416.667, pending_audio_ms=440.0), 137)

    def test_a_picture_beyond_the_next_sound_waits_rather_than_being_dropped(self):
        """The distinction the review asked for, on the far side of the hole.

        A picture whose sound has not been taken yet is held. Discarding it
        would lose a picture that becomes placeable one block later, and the
        refusal counter would not show it as waiting either.
        """
        clock = self.taken_and_one_pending()
        verdict, stamp = clock.placement(900.0, pending_audio_ms=400.0)
        self.assertEqual(verdict, "sound-not-taken-yet")
        self.assertIsNone(stamp)
        self.assertEqual(clock.frames_in_hole, 0,
                         "waiting is not a loss and must not be counted as one")

    def test_a_contiguous_sound_never_closes_the_segment(self):
        """The queue head continues the segment, so nothing is a hole."""
        clock = self.taken_and_one_pending()
        # Head at 120 is the next block, so content up to it is still inside.
        self.assertEqual(clock.video(100.0, pending_audio_ms=120.0), 100)
        self.assertEqual(clock.frames_in_hole, 0)

    def test_without_the_queue_head_the_old_behaviour_is_unchanged(self):
        """No information about what comes next, so no horizon is invented."""
        clock = self.taken_and_one_pending()
        self.assertEqual(clock.video(333.333), 333)

    def test_without_the_queue_head_the_measured_error_reappears(self):
        """The defect itself, pinned so the fix cannot be credited to luck.

        Given no queue head, the 197 ms error the review measured is still
        produced: the in-hole picture is stamped 333, and the strictly
        increasing rule then clamps the recovery picture from 137 to 334. That
        this is reachable here is the point -- it is the queue head, and only
        the queue head, that fixes it.
        """
        clock = self.taken_and_one_pending()
        self.assertEqual(clock.video(333.333), 333)
        clock.audio(400.0)
        self.assertEqual(clock.video(416.667), 334)


class TheCalibrationStateIsNotABooleanTests(unittest.TestCase):
    """Unknown, approximate and media-derived are three states, not two.

    An external review found the acceptance script printing `calibrated=True`
    for the launch-time fallback while the same round's notes said that
    approximation is not treated as calibrated. Both cannot be true.
    """

    def test_a_common_decode_is_calibrated(self):
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)
        self.assertTrue(line.calibrated)
        self.assertEqual(line.state, "from media timestamps")

    def test_launch_times_are_usable_but_not_calibrated(self):
        """The distinction that keeps the screen from going black.

        An approximation may be used to pair -- refusing to left the picture at
        zero frames while the sound climbed -- but it is not a synchronisation
        result and must not report as one.
        """
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        line.calibrate(0.0, 0.6, basis=BASIS_LAUNCH)
        self.assertFalse(line.calibrated)
        self.assertTrue(line.usable)
        self.assertEqual(line.state, "approximate")

    def test_nothing_measured_is_neither(self):
        line = ContentTimeline(VIDEO_MS, AUDIO_MS)
        self.assertFalse(line.calibrated)
        self.assertFalse(line.usable)
        self.assertEqual(line.state, "unknown")


if __name__ == "__main__":
    unittest.main()
