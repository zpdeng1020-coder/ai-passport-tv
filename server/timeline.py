"""When a piece of content happened, as opposed to when it turned up here.

The distinction is the whole of this module, and the project has been wrong
about it for its entire life. Alignment used to compare **arrival** times: an
item's position in the session was `time.monotonic() - that_decoder's_start`.
That number changes when the network stalls, when the reader is slow, when a
process is descheduled -- none of which are properties of the content. An
external review built the counterexample: the same picture, delivered 600 ms
late, stopped matching the sound it belonged to.

Two things are wrong with arrival-as-content and they need different fixes.

**Within one stream**, arrival says nothing about content order or spacing. A
decoder that is paced at real time emits frame N at N/FPS seconds of content,
whether that frame arrived on time or three seconds late. So a stream's content
time is counted, not measured:

    content_ms = items_produced * interval_ms

**Between the two streams**, the counters share no origin: there are two ffmpeg
processes, each starting its own clock at its own moment. That relationship can
only be observed once, at the start, and it is a constant thereafter -- it is
each process's startup latency, and a process that took 300 ms to produce its
first frame has a 300 ms offset for the rest of the session, not a 300 ms error
on every frame.

So this module does two separate things and keeps them separate:

- `advance()` counts content forward, from the production rate alone.
- `calibrate()` observes the origin gap **once**, from the first item of each
  stream, and refuses to do it again.

What is deliberately NOT here: any use of a per-item arrival time. Once a stream
is calibrated, an item's content time is fixed at the moment it is pushed and
never recomputed. That is what makes the review's 600 ms counterexample pass.

The module has no ESP-IDF, no sockets and no ffmpeg. It is arithmetic over
counters, so the counterexamples can be run in milliseconds on the host rather
than argued about against a live stream.
"""
from __future__ import annotations

import collections


# How far apart two streams' content times may be and still be called the same
# moment. 40 ms is one audio chunk, the granularity the sound is cut into, so
# anything under it is not an offset a viewer could see.
DEFAULT_TOLERANCE_MS = 40

# The bases the origin relationship may be measured from, named so that a reader
# never has to infer which one produced the offset. See `calibrate`.
#
# `BASIS_COMMON_DECODE` is the one the server now uses: both streams come out of
# a single ffmpeg process and carry that process's own timestamps, so their
# relationship is not measured at all -- it is the same clock by construction.
BASIS_COMMON_DECODE = "one decode, the decoder's own timestamps"
BASIS_MEDIA_START = "media stream start times"
BASIS_LAUNCH = "process launch times (live source; ffprobe reports no start times)"

# How well the two clocks are known to be related. Three states, because an
# external review was right that two are not enough: "we have not measured it"
# and "we measured it from something that is not the media" call for different
# answers, and a single boolean reported the second as the first's opposite.
#
# `APPROXIMATE` is deliberately still *usable* -- refusing to pair on it left
# the screen black, measured, with the sound climbing to 2634 packets while the
# picture sent none. What it may not do is pass as synchronised.
STATE_UNKNOWN = "unknown"
STATE_APPROXIMATE = "approximate"
STATE_FROM_MEDIA = "from media timestamps"


def state_of_basis(basis: str) -> str:
    """Which calibration state a basis establishes.

    Only the two bases that come from the media put a session in the state a
    synchronisation claim may be made from. A launch-time offset is a real
    number and a usable one; it is not evidence about the programme.
    """
    if basis in (BASIS_COMMON_DECODE, BASIS_MEDIA_START):
        return STATE_FROM_MEDIA
    return STATE_APPROXIMATE


class Stream:
    """One stream's content clock: a counter and a fixed origin."""

    __slots__ = ("name", "interval_ms", "count", "origin_ms", "origin_set")

    def __init__(self, name: str, interval_ms: float):
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self.name = name
        self.interval_ms = float(interval_ms)
        self.count = 0
        self.origin_ms = 0.0
        self.origin_set = False

    def take(self) -> float:
        """Content time of the next item, and count it as produced.

        Called once per item, in production order, by the thread that reads the
        stream. The value returned is this item's content time for the rest of
        its life: it is stamped on the item now and never derived again.
        """
        stamp = self.origin_ms + self.count * self.interval_ms
        self.count += 1
        return stamp

    def reset(self) -> None:
        self.count = 0
        self.origin_ms = 0.0
        self.origin_set = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Stream({self.name!r}, interval={self.interval_ms:.3f}ms, "
                f"count={self.count}, origin={self.origin_ms:.1f}ms, "
                f"set={self.origin_set})")


class ContentTimeline:
    """Two streams' content clocks, and the one observation that relates them.

    The relationship is calibrated from the first item of each stream and then
    frozen. Before calibration, `offset_ms` is None and any pairing that needs
    it must refuse rather than guess -- a session that starts before both
    streams have produced anything has no basis for saying which content is
    simultaneous, and pretending otherwise is how a startup gap became a
    lip-sync error.
    """

    def __init__(self, video_interval_ms: float, audio_interval_ms: float,
                 tolerance_ms: float = DEFAULT_TOLERANCE_MS):
        self.video = Stream("video", video_interval_ms)
        self.audio = Stream("audio", audio_interval_ms)
        self.tolerance_ms = float(tolerance_ms)
        # Audio content time minus video content time at the observed origin.
        self.offset_ms: float | None = None
        # Why calibration did not happen, for the log. Named rather than a bare
        # boolean because "not calibrated yet" and "refused to recalibrate" are
        # different states and only one of them is a fault.
        self.calibration_note = "not attempted"
        # What the offset was measured FROM. Never a bare number: the same
        # 600 ms means "the media says so" or "the scheduler did this" depending
        # entirely on which basis produced it, and those have opposite meanings.
        self.basis = ""
        self.recalibrations_refused = 0
        self.state = STATE_UNKNOWN

    # -- calibration ------------------------------------------------------

    def calibrate(self, video_start_s: float, audio_start_s: float,
                  basis: str = BASIS_MEDIA_START) -> bool:
        """Relate the two streams from **their own media start times**.

        Both ffmpeg processes decode the same source from its beginning, so what
        a stream's content clock measures is where that stream begins *in the
        media* -- which is what `ffprobe -show_entries stream=start_time`
        reports. Two streams whose media starts coincide are simultaneous; a
        stream that starts 600 ms into the media has 600 ms less content, and
        that difference belongs to the programme, not to the reader.

        **Two earlier versions of this were wrong, and reviewing why is worth
        more than the fix.**

        The first used each stream's *first arrival*. That folded delivery
        latency into the offset: a picture delivered 600 ms late calibrated to
        -600 ms.

        The second used the two `Popen` timestamps, on the argument that each
        decoder is paced at real time from its own launch. That is true and it
        is irrelevant: when a process starts says **when it begins producing**,
        not **which part of the media it produces**. An external review proved
        it with real ffmpeg and this project's own `LiveChannel`: the same file,
        with only the audio process launched 600 ms later, calibrated to +600 ms
        and discarded the picture's first four frames -- the picture had not
        changed at all. And a file whose audio genuinely starts at 600 ms
        calibrated to +0.2 ms, so the real 600 ms offset was erased.

        Both mistakes have one shape: **taking something about the reader as
        something about the programme.** The arrival is about the network; the
        launch time is about the scheduler. Neither is in the media. The stream
        start time is, and it is the only one of the three that ffprobe can
        answer for both streams at once.

        Returns True if this call set the offset, False if it was already set.
        """
        if self.offset_ms is not None:
            self.recalibrations_refused += 1
            return False
        self.offset_ms = (float(audio_start_s) - float(video_start_s)) * 1000.0
        self.basis = basis
        self.state = state_of_basis(basis)
        self.calibration_note = f"calibrated from {basis}"
        return True

    @property
    def calibrated(self) -> bool:
        """Whether the relationship is known to come from the media itself.

        This is the state a synchronisation claim may be made from, and it is
        **not** the same question as whether the mapping may be used. Asking
        this one to gate pairing is what left the screen black: on a live source
        with no start times it is never true, so `pop_video()` refused for ever.
        """
        return self.state == STATE_FROM_MEDIA

    @property
    def usable(self) -> bool:
        """Whether the mapping may be used to place items at all.

        True for an approximation as well, because a labelled approximation is
        strictly better than a black screen -- and the label is what keeps it
        from being mistaken for the other thing.
        """
        return self.state != STATE_UNKNOWN

    # -- content time -----------------------------------------------------

    def video_now(self) -> float:
        """Content time of the next video item, without consuming it."""
        return self.video.origin_ms + self.video.count * self.video.interval_ms

    def audio_now(self) -> float:
        """Content time of the next audio item, without consuming it."""
        return self.audio.origin_ms + self.audio.count * self.audio.interval_ms

    def audio_in_video_units(self, audio_content_ms: float) -> float:
        """An audio content time expressed on the video stream's clock.

        The derivation, because the sign is the whole of it and getting it
        backwards produces a plausible-looking offset in the wrong direction.
        A decoder started at wall time `T` emits content `c` at wall time
        `T + c`. So a thing that happened at wall time `t` has content `t - T`
        in either stream, and:

            video_content = t - T_v
            audio_content = t - T_a
            => audio_content = video_content + (T_a - T_v)
            => video_content = audio_content - (T_a - T_v)

        `offset_ms` is `T_a - T_v`, measured once from the two arrivals. So an
        audio time maps to a video time by **adding** it, and the inverse
        subtracts.
        """
        if self.offset_ms is None:
            raise ValueError("the two streams have not been related yet")
        return audio_content_ms + self.offset_ms

    def video_in_audio_units(self, video_content_ms: float) -> float:
        if self.offset_ms is None:
            raise ValueError("the two streams have not been related yet")
        return video_content_ms - self.offset_ms

    # -- the decision the queues need -------------------------------------

    def relates(self, video_content_ms: float, audio_content_ms: float) -> bool:
        """Whether these two items describe the same moment, within tolerance."""
        if self.offset_ms is None:
            return False
        return abs(video_content_ms - self.audio_in_video_units(audio_content_ms)) \
            <= self.tolerance_ms

    def verdict(self, video_content_ms: float | None,
                audio_content_ms: float | None) -> str:
        """Which side is ahead, in words a log can carry.

        Four answers rather than two, because "the picture is not due yet" and
        "there is no picture at all" have opposite remedies and looked identical
        while the decision was a single `None`.

        - `"no-video"` / `"no-audio"`: that queue is empty. Nothing to pair.
        - `"unrelated"`: both streams have content but their origins have not
          been related yet. Refuse rather than guess.
        - `"video-early"`: the picture is ahead of the sound; wait for it.
        - `"video-late"`: the picture is behind; it has missed its moment.
        - `"together"`: within tolerance.
        """
        if video_content_ms is None:
            return "no-video"
        if audio_content_ms is None:
            return "no-audio"
        if self.offset_ms is None:
            return "unrelated"
        # Positive means the front of the picture queue is content that is
        # *newer* than the sound being played -- it has not reached its moment
        # yet, so the answer is to wait. Negative means it is older, which is
        # the case with no remedy: that moment has passed.
        delta = video_content_ms - self.audio_in_video_units(audio_content_ms)
        if delta > self.tolerance_ms:
            return "video-early"
        if delta < -self.tolerance_ms:
            return "video-late"
        return "together"


class SourceState:
    """Whether a window with no picture means the source or the link.

    The controller was once able to read an empty window as spare capacity and
    raise the rate, and the repair for that was a guard. A guard is not a
    diagnosis: "the source has stopped producing" and "we are sending faster
    than the link takes" both show as an empty window from the sender's seat,
    and they call for opposite responses.

    What separates them is whether content is still arriving. A source that has
    stopped advances neither counter; a congested link keeps both advancing
    while the send queue grows and nothing leaves.
    """

    #: Content is flowing and the queues are within bounds.
    FLOWING = "flowing"
    #: Content has stopped arriving. Do not touch the rate; there is no
    #: measurement in this window.
    STARVED = "starved"
    #: Content is arriving and the queue is growing past its bound. This is the
    #: case that wants the rate to come down.
    CONGESTED = "congested"

    def __init__(self, stalled_windows_before_starved: int = 2):
        self.stalled_windows_before_starved = stalled_windows_before_starved
        self._stalled = 0
        self.state = self.FLOWING
        self.note = "start"

    def observe(self, video_advanced: bool, audio_advanced: bool,
                queue_over_bound: bool) -> str:
        """Classify the window that just ended.

        `video_advanced` and `audio_advanced` say whether either stream produced
        anything at all. `queue_over_bound` says whether the outgoing queue is
        holding more content than its bound allows.

        A single quiet window is not starvation: an HLS origin delivers in
        bursts, and a segment's worth of audio is followed by a wait for the
        next segment. Only a run of them is a stopped source, which is why this
        takes more than one window to say so.
        """
        if video_advanced or audio_advanced:
            self._stalled = 0
            if queue_over_bound:
                self.state = self.CONGESTED
                self.note = "content arriving and the queue is over its bound"
            else:
                self.state = self.FLOWING
                self.note = "content arriving"
            return self.state
        self._stalled += 1
        if self._stalled >= self.stalled_windows_before_starved:
            self.state = self.STARVED
            self.note = f"no content for {self._stalled} windows"
        else:
            # Not yet enough evidence. Neither starved nor flowing: a window
            # with no measurement in it, which is what the controller must be
            # told rather than being told "flowing".
            self.state = self.FLOWING if not queue_over_bound else self.CONGESTED
            self.note = f"quiet window {self._stalled}/{self.stalled_windows_before_starved}"
        return self.state

    @property
    def measured(self) -> bool:
        """Whether the last window carried anything a rate decision may use."""
        return self.state != self.STARVED


# What the mapping can say about a picture. Four answers rather than a stamp or
# a bare `None`, because the two ways of having no stamp need opposite handling
# and looked identical while there was only one of them:
#
#   * content inside sound the sender dropped has been lost to the viewer, and
#     the frame is discarded -- keeping it would push the recovery frame's
#     timestamp up by the size of the hole;
#   * content whose sound has not been taken yet has not reached its moment,
#     and the frame is held -- discarding it loses a picture that was going to
#     be perfectly placeable a moment later.
#
# Collapsing them was measured: a frame that should have been dropped was
# stamped, and the recovery frame that should have got 137 ms was then clamped
# to 334 ms by the strictly-increasing rule.
VERDICT_PLACED = "placed"
VERDICT_IN_HOLE = "in-dropped-sound"
VERDICT_NOT_YET = "sound-not-taken-yet"
VERDICT_NO_ANCHOR = "no-sound-taken"


class SessionClock:
    """Content time turned into the time the wire carries, gap by gap.

    Content time and session time are different quantities and neither may be
    used as the other.

    **Content time** is where a thing sits in the programme. It is not zero at
    the start of a session and it moves by whatever the source does.

    **Session time** is where a thing sits in the listening. It starts at zero
    and advances by exactly one chunk per chunk received, because that is what
    the device's playback clock counts: `estimated_pts()` in
    `main/av_player.c` is `submitted_samples / 16000`, and `av_stream_accept()`
    refuses a sound packet whose timestamp is not exactly the previous one plus
    one chunk.

    Sound the sender dropped was never heard, so it occupies no session time
    even though it occupies content time. That is the whole relationship, and
    it is piecewise rather than a single subtraction: every discontinuity starts
    a new segment in which session time runs parallel to content time at
    distance one chunk per chunk.

    **The picture is stamped with its own content position, carried onto this
    clock.** This is the correction of a defect that an external review found by
    construction and then measured: the stamp used to be the session position of
    *the sound the frame was paired with*, so a frame whose content was 333 ms
    older than that sound was drawn when the sound reached the sound's moment --
    making the picture 333 ms late, and hiding the fact that it was.

        stamp = session_time_of(the frame's OWN content)

    is what the device needs in order to draw the frame at the right moment,
    and the frame's content is the only thing that knows what that is. **A
    correct timestamp is necessary and not sufficient**: when the frame is
    actually drawn also depends on when it is sent, on buffering and frame
    drops, and on the device's own playback. Nothing in this module measures
    what a viewer hears against what they see, and `diagnostics()` below must
    not be read as though it did.

    `audio_items` remains, and is not the picture's timestamp any more: it is
    the count of sound blocks taken, which the sender uses to pace itself.
    """

    __slots__ = ("chunk_ms", "tolerance_ms", "audio_items", "video_items", "last_video_ms",
                 "pairs", "residual_sum_ms", "residual_worst_ms",
                 "content_gap_ms", "audio_gaps", "frames_unplaceable",
                 "frames_in_hole", "last_verdict",
                 "_segments", "_prev_audio_content")

    def __init__(self, chunk_ms: float, tolerance_ms: float = 350.0):
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        self.chunk_ms = float(chunk_ms)
        # How far the picture's session position may sit from the sound's at the
        # same instant. It is the same quantity as the sender's pairing
        # tolerance and is used for a different purpose: see `video`.
        self.tolerance_ms = float(tolerance_ms)
        self.audio_items = 0
        self.video_items = 0
        self.last_video_ms = -1
        # How far the picture's content sat from the sound it was paired with.
        # Positive means the picture is ahead.
        self.pairs = 0
        self.residual_sum_ms = 0.0
        self.residual_worst_ms = 0.0
        # Content the source skipped over, in milliseconds, and how often.
        self.content_gap_ms = 0
        self.audio_gaps = 0
        # Frames the mapping could not place, because their content falls inside
        # sound the sender dropped. Counted rather than silently extrapolated:
        # see `session_of`.
        self.frames_unplaceable = 0
        # Of those, the ones refused because their content sits in sound that
        # was dropped, as against the ones merely not due yet. Only this count
        # is a loss; the other is ordinary waiting.
        self.frames_in_hole = 0
        self.last_verdict = VERDICT_NO_ANCHOR
        # (content_start, session_start, content_end) per segment. `content_end`
        # is exclusive and is None while the segment is the newest one, because
        # the audio it describes is still arriving.
        #
        # A segment END is as necessary as its start, and its absence was a
        # defect an external review measured: with only starts recorded, content
        # inside a dropped interval was extrapolated along the segment before it
        # and handed a session time no sound occupies.
        self._segments: list[tuple[float, float, float | None]] = []
        self._prev_audio_content: float | None = None

    def reset(self) -> None:
        """A new session starts at zero. Nothing carries over."""
        self.audio_items = 0
        self.video_items = 0
        self.last_video_ms = -1
        self.pairs = 0
        self.residual_sum_ms = 0.0
        self.residual_worst_ms = 0.0
        self.content_gap_ms = 0
        self.audio_gaps = 0
        self.frames_unplaceable = 0
        self.frames_in_hole = 0
        self.last_verdict = VERDICT_NO_ANCHOR
        self._segments = []
        self._prev_audio_content = None

    @property
    def anchored(self) -> bool:
        """Whether any sound has been taken, so session time means anything."""
        return bool(self._segments)

    def session_of(self, content_ms: float) -> float | None:
        """Session time of a content moment, or None if there is not one.

        Three cases, and the middle one is why this returns an optional.

        **Inside a segment**: session time runs parallel to content time from
        that segment's start, so the answer is the distance from it.

        **Inside a gap**: the sound there was produced by the source and dropped
        by the sender, so the viewer never heard it and the session has no time
        for it. **There is no answer, and `None` is the answer.** Returning a
        value by extrapolating the segment before the gap is what an external
        review caught: a picture at content 4750 ms was given session 4750 ms in
        a session whose sound had only ever reached 120 ms, and the frames around
        it pushed the recovery frame's timestamp up by 4.8 seconds.

        **Before the first segment**: the session has not begun, but a picture
        that arrives before the first sound is the ordinary startup case rather
        than an error, so the first segment is extended backwards and the result
        is allowed to be negative -- `video()` clamps it, because a wire
        timestamp has no way to say "before the start".
        """
        if not self._segments:
            return None
        first = self._segments[0]
        if content_ms < first[0]:
            # Before the session's first sound. The first segment is extended
            # backwards so a picture that arrives during startup still has a
            # place; `video()` clamps the negative result.
            return float(content_ms) - first[0] + first[1]
        for start_content, start_session, end_content in self._segments:
            if start_content <= content_ms and (end_content is None
                                                or content_ms < end_content):
                # Inside this segment. The newest one has no end yet and so
                # extends forward -- but only FORWARD: reaching it does not mean
                # everything before it belongs to it, which is the case the
                # check below covers.
                return float(content_ms) - start_content + start_session
        # Not inside any segment: the content sits between a closed segment's
        # end and the next one's start, which is exactly the sound the sender
        # dropped. There is no session moment here to return.
        return None

    def audio(self, content_ms: float) -> int:
        """Session timestamp for a sound block, and count it as taken.

        Contiguous by construction, which the device requires: each block
        advances the device's clock by exactly one chunk and the timestamp has
        to say so. A source discontinuity moves the content map without moving
        this, and is counted in `content_gap_ms` where an operator can see it.
        """
        content_ms = float(content_ms)
        # This block's session time, and the number the device will be sent.
        # Computed before the map is touched, because a segment begins at THIS
        # block's session time and not one chunk past the previous segment.
        value = int(round(self.audio_items * self.chunk_ms))
        if self._prev_audio_content is None:
            # The anchor: the first sound of the session, whose session time is
            # zero by definition.
            self._segments = [(content_ms, float(value), None)]
        else:
            step = content_ms - self._prev_audio_content
            if step > self.chunk_ms * 1.5:
                missed = int(round(step - self.chunk_ms))
                self.content_gap_ms += missed
                self.audio_gaps += 1
                # Close the segment being left behind, at the end of the sound
                # that was actually received. Without this the segment has no
                # far edge and content inside the gap is extrapolated along it.
                previous = self._segments[-1]
                self._segments[-1] = (previous[0], previous[1],
                                      self._prev_audio_content + self.chunk_ms)
                # And start a new one, running parallel to content from this
                # block's own session time.
                self._segments.append((content_ms, float(value), None))
        self._prev_audio_content = content_ms
        self.audio_items += 1
        return value

    def placement(self, content_ms: float,
                  pending_audio_ms: float | None = None) -> tuple[str, int | None]:
        """Where a picture belongs, and what to do when it does not belong anywhere.

        `pending_audio_ms` is the content position of the next sound block the
        sender intends to take -- the head of its queue. It is what closes the
        current segment in time. Without it a gap can only be seen once the
        sound *after* the gap has been taken, and until then the open segment
        extends forward across the hole; an external review measured the result:
        a 280 ms gap whose recovery frame should have been stamped 137 ms was
        clamped to 334 ms, a 197 ms error, with the refusal counter still at 0.

        The verdict separates two states that a bare `None` used to merge.
        `IN_HOLE` is a loss and the frame goes; `NOT_YET` is ordinary waiting
        and the frame stays in the queue. Treating the second as the first
        discards a picture that would have been placeable one block later.

        Returns (verdict, session timestamp or None). See `video()` for the
        timestamp alone.

        `content_ms` is the picture's content moment expressed on the sound's
        clock -- see `ContentTimeline.video_in_audio_units`.

        `None` before any sound has been taken, and that is not a placeholder:
        without an anchor there is no session time, and inventing one would place
        the picture against a moment the sound never described.

        Strictly increasing, because the device refuses a video timestamp that
        does not advance. Two pictures whose content maps to the same
        millisecond are two pictures the device cannot tell apart by time at
        all, so the tie is broken by one millisecond; this is the only counter
        left in the mapping and it is bounded by the frame interval.

        Clamped at zero rather than refused: a picture whose content precedes the
        session's first sound may be drawn at once, and a wire timestamp has no
        way to express "before the start".
        """
        self.last_verdict = VERDICT_NO_ANCHOR
        if not self.anchored:
            return VERDICT_NO_ANCHOR, None
        # The open segment's forward edge, and whether the next sound continues
        # it. Closing the segment here -- before the sound after the gap has
        # been taken -- is the whole of the short-gap fix: the head of the
        # audio queue is already visible while the segment is still open, so
        # the hole is known in time rather than one block too late.
        edge = None
        if self._prev_audio_content is not None and pending_audio_ms is not None:
            edge = self._prev_audio_content + self.chunk_ms
            if pending_audio_ms <= edge:
                # The next sound continues this segment; it simply extends.
                edge = None
        if edge is not None and content_ms >= edge:
            if content_ms < pending_audio_ms:
                # Inside sound the sender dropped. The viewer never heard this
                # moment, so there is no session time to draw it at.
                self.frames_unplaceable += 1
                self.frames_in_hole += 1
                self.last_verdict = VERDICT_IN_HOLE
                return VERDICT_IN_HOLE, None
            # Past the next sound as well, so its moment has not arrived. The
            # frame is held, not discarded.
            self.last_verdict = VERDICT_NOT_YET
            return VERDICT_NOT_YET, None
        place = self.session_of(content_ms)
        if place is not None and place > (self.audio_items * self.chunk_ms
                                          + self.tolerance_ms):
            # A second guard, for the case `session_of` cannot see in time.
            #
            # A gap is only *known* once the sound after it arrives, and that
            # sound arrives at the head of the queue -- so a frame paired just
            # before that moment is stamped while the segment is still open and
            # still extending forward across the hole. An external review
            # measured the result: three frames whose content sat inside a
            # 4.88 s hole were stamped 4750, 4833 and 4917, and the recovery
            # frame, mapping correctly to 120 ms, was then clamped to 4918 by
            # the strictly-increasing rule. One refused frame would have
            # prevented all 4798 ms of it.
            #
            # **The test is one-sided on purpose.** A frame may be *behind* the
            # sound the session has taken -- that is the normal state, because
            # the sound runs ahead of the picture by the queue depth -- but it
            # may never be *ahead* of it. Nothing has been heard that has not
            # been taken, so a picture claiming to sit later than the newest
            # sound is claiming a moment that does not exist yet, and the only
            # way the map produces that is by extrapolating across a hole.
            #
            # The allowance above the head is the pairing tolerance, because a
            # picture is paired *within* tolerance of the sound at the head and
            # the head has not been taken yet. An earlier version compared the
            # absolute distance instead and refused healthy frames 400 ms behind
            # the sound, which is ordinary queue depth rather than a fault.
            self.frames_unplaceable += 1
            self.last_verdict = VERDICT_NOT_YET
            return VERDICT_NOT_YET, None
        if place is None:
            # The content sits inside sound the sender dropped, so there is no
            # session moment for it. Refusing is the answer and it is load
            # bearing: the alternative -- letting a large backward step through
            # -- is what produced a 4798 ms error on the recovery frame, because
            # the monotonic clamp below then pushed 120 ms up to follow a
            # picture that should never have been given a timestamp at all.
            self.frames_unplaceable += 1
            self.frames_in_hole += 1
            self.last_verdict = VERDICT_IN_HOLE
            return VERDICT_IN_HOLE, None
        value = int(round(place))
        if value < 0:
            value = 0
        if value <= self.last_video_ms:
            value = self.last_video_ms + 1
        self.last_video_ms = value
        self.video_items += 1
        self.last_verdict = VERDICT_PLACED
        return VERDICT_PLACED, value

    def video(self, content_ms: float,
              pending_audio_ms: float | None = None) -> int | None:
        """The session timestamp for a picture, or None.

        The timestamp alone, for callers that only send placeable frames. The
        sender uses `placement()` instead, because it also has to tell a frame
        whose sound was dropped from one whose sound is merely not due yet.
        """
        return self.placement(content_ms, pending_audio_ms)[1]

    def note_pair(self, video_content_ms: float, audio_content_ms: float) -> None:
        """Record how far the picture's content sat from the sound's, in ms.

        Positive means the picture is ahead. Both arguments are on the sound's
        clock. Called where the pairing decision is made, so the two numbers
        describe one instant.

        This is a residual and not a count, and the difference matters. A count
        of pairs that were not exactly equal fires on ordinary jitter, because a
        picture is *placed within a tolerance* of its sound and never exactly on
        it -- a stream in perfect health produced nine such "behind" pairs on the
        first run of this counter, which is a number that cannot be acted on.

        It measures **queue depth difference**, not lip-sync error: the two are
        placed on one clock by `audio()` and `video()` above, so a constant
        residual means the picture queue is that much deeper than the sound's,
        not that the viewer sees a mismatch. A residual that *walks* is the
        thing to watch.
        """
        residual = float(video_content_ms) - float(audio_content_ms)
        self.pairs += 1
        self.residual_sum_ms += residual
        if abs(residual) > abs(self.residual_worst_ms):
            self.residual_worst_ms = residual

    def diagnostics(self) -> str:
        """One line for the periodic report."""
        mean = self.residual_sum_ms / self.pairs if self.pairs else 0.0
        return (f"audio={self.audio_items} video={self.video_items}"
                f" pair={self.pairs}"
                f" drift={mean:+.0f}ms worst={self.residual_worst_ms:+.0f}ms"
                f" gaps={self.audio_gaps}/{self.content_gap_ms}ms"
                f" unplaceable={self.frames_unplaceable}"
                f" hole={self.frames_in_hole}")
