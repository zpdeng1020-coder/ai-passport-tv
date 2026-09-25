"""Choosing the picture rate while a session runs.

The link between the computer and the device carries a fixed number of bytes a
second, and the sound's share of it is fixed by the protocol. The rest belongs
to the picture, and how far it stretches depends on what the channel is showing:
a still studio shot compresses to a fraction of a fast-moving one. A frame rate
chosen once cannot be right for both. Set it for the busy channel and a quiet
one plays at half the frames it could; set it for the quiet one and the busy
channel asks for more than the link has, the device falls behind, and the
session ends in the middle of a picture.

What that looks like from the sofa is the picture stopping for several seconds
and the device's waiting screen appearing, over and over.

So the rate is a decision taken continuously rather than a constant. This module
is that decision and nothing else: it is given what the last window of sending
actually cost and it answers with the rate to use next. It holds no socket, no
clock and no state that outlives a session, so it can be driven through every
case it will meet far faster than hardware can be made to meet them.

Why the target is not simply measured bytes:

    The device could be told to send exactly what the link carries, and on a
    perfect link that would be right. This link is not perfect -- it is Wi-Fi
    in a house, shared with everything else on it -- so a rate that sits at the
    ceiling is a rate that is over it several times a minute. The controller
    therefore aims below the ceiling and treats the measured cost of a write as
    the signal, because that is what says the ceiling is near: an unsaturated
    link accepts four kilobytes in about fifty milliseconds, and a saturated
    one takes four times that and is still not done.
"""

from __future__ import annotations

import os

# The picture's share of the link, in bytes a second -- and it is no longer a
# backstop. It is the number the rate ceiling is derived from.
#
# The derivation that produced this number is wrong and the number is kept only
# because it is the one that has been measured with, so that a change to it is
# a change of one thing.
#
# It was derived as "the link carries about 160 kB/s (device-reported `in_bps`),
# the sound's share is fixed at 32 kB/s and is counted nowhere here, so 160 less
# 32 leaves about 128 for the picture". **Both halves of that are now known to
# be false.** `in_bps` is not link throughput -- it is the compressed video
# bytes that successfully inflated, so it counts neither the sound nor anything
# the device discarded, and subtracting the sound from it double-counts the
# omission. And 160 is not the link's ceiling: measured on the device, the same
# channel receiving 91-101 kB/s under this budget reached 184-195 kB/s when the
# budget was doubled, so 160 is closer to where this controller converges than
# to a property of the link.
#
# What the number remains is the picture's share as a working point, and the
# ceiling is derived from it as `budget / frame_bytes`. Replacing it needs a
# measured replacement, not a better-sounding derivation -- the last three
# attempts to raise it by reasoning ended the same way: three channels dropped
# 47 to 174 frames and a fourth collapsed, which is why this one is unchanged
# and the reason is recorded here rather than left to be rediscovered.
#
# It used to be 210000, described as "deliberately generous" and "a backstop,
# not a measurement", on the reasoning that packets a second were the scarcer
# resource and the byte ceiling was never reached. Two things were wrong with
# that. A ceiling nothing reaches cannot stop a climb, and the climb is what put
# the device's queue full and the audio lead at -1200 ms before a write finally
# went slow. And with the ceiling now derived per channel, this figure is not a
# limit imposed on the picture -- it is the measurement that sets each channel's
# rate.
VIDEO_BUDGET_BYTES = int(os.environ.get("TV_VIDEO_BUDGET", "185000"))

# The rates the controller may choose between.
#
# The floor is what still reads as movement rather than as a slideshow, and six
# is the lowest measured to do that: four was the figure the viewer described as
# changing slides.
#
# The ceiling is above what the current geometry is expected to reach on a quiet
# channel, because reaching it is what tells the controller the link has room; a
# limit that is never approached cannot be distinguished from one that is being
# hit.
#
# It was twelve, then fifteen, and both were set from fixed-rate probes that had
# a contradiction in them: asking for eighteen frames a second failed to
# establish a session while asking for twenty-one established one and delivered
# 13.9. A limit that refuses a smaller request and grants a larger one is not a
# limit, it is a measurement artefact -- and a ceiling set from it stops the
# controller short of wherever the real one is.
#
# So the ceiling is now above anything this link has shown, and the controller
# is left to find the working point by its own signals: it drops a frame at a
# time when a write is slow or the window is heavy, and climbs back when four
# windows in a row are comfortable. That is the mechanism the numbers here were
# standing in for.
MIN_FPS = int(os.environ.get("TV_MIN_FPS", "3"))
# The outermost bound on the picture rate. What the controller aims at is NOT
# this: it is derived per channel in `ceiling()`, from the budget divided by what
# a frame of that channel actually costs. This only bounds the derivation.
#
# That distinction is the whole history of this number, so it is worth stating
# rather than accumulating. The ceiling has been 11, 12, 15 and 5, each time set
# from a fixed-rate probe on ONE channel, and each time the figure was a stand-in
# for a measurement nobody had taken: what a frame of the channel in front of you
# costs. Measured across 103 channels, that runs from 2266 bytes to 31271 -- a
# factor of 13.8 -- so no single rate can serve the list. Set for a busy channel
# a quiet one plays at a third of what it could; set for a quiet one the busy
# channel asks for more than the link has.
#
# Five is what stands today, and it is what has been shown to hold a session for
# five hundred and sixty seconds. It is a floor on ambition rather than a
# finding: with it at twelve the derivation was measured working exactly as the
# arithmetic says -- a median channel measured 19036 bytes a frame, computed a
# ceiling of six and settled at 5 to 7; the cheapest measured 2900 bytes,
# computed twelve, and climbed 5, 6, 7, 8, 9, 10, 11, 12 -- but a session at
# twelve overshot on the way up (5, 6, 7, 8, 9 before a write went slow, with the
# device's queue full and the audio lead at -1200 ms) and fell to three without
# recovering.
#
# So the derivation is in place and this bounds it until two things are true: the
# byte budget stops the climb before the link does, and a throttle keeps the
# producer from filling the queue while the sender runs slower. Neither has a
# session's worth of measurement behind it yet. See `ceiling()`.
MAX_FPS = int(os.environ.get("TV_MAX_FPS", "12"))

# Where a session begins: at the ceiling, which is now itself a measurement
# rather than a guess.
#
# It used to be ten, "just below where sessions land", written when the
# controller settled at 10 to 11 and the ceiling was twelve. The ceiling has
# since come down to the five the link actually carries, and a start rate above
# the ceiling is not a starting point -- it is a range the controller refuses to
# construct, which is how this was noticed: every live-sender test failed at
# construction with "start rate is outside the range".
#
# It is NOT the ceiling, and that changed when the ceiling did. The ceiling is
# now above anything a channel reaches, so starting there would open every
# session over the link and cost several seconds of dropping frames before the
# controller found its level -- on a heavy channel, from 12 down to 3.
#
# Five is where the median channel was measured landing (17632 bytes a frame
# against the same fixed budget the ceiling is derived from), so a session
# starts close to right for a
# typical channel in either direction: a cheap one climbs, a heavy one steps
# down, and neither spends the opening seconds walking several steps.
# Same figure, different reason -- it was the ceiling, and it is now a starting
# guess.
START_FPS = int(os.environ.get("TV_START_FPS", "5"))

# How long a 4 KiB write may take before the link is judged to be filling up,
# and the figure below which it is judged to have room.
#
# Both are measured against the same thing: an unsaturated link moves four
# kilobytes in about fifty to seventy milliseconds. A quarter of a second is
# several times that and means the socket is refusing bytes faster than it
# takes them, which is the state that precedes a session ending -- the device
# stops reading, the sender's write never finishes, and the frame is abandoned.
#
# The gap between the two is deliberate and is the controller's hysteresis. A
# single threshold would put the rate into a loop: one slow write drops it, the
# link clears, the next window raises it, and it spends the session oscillating
# instead of choosing.
SLOW_WRITE_MS = 250.0
FAST_WRITE_MS = 120.0

# Windows of comfort needed before the rate goes up.
#
# Down immediately, up slowly, because the two are not symmetric mistakes: too
# high and the viewer sees the picture stop, while too low is a slightly less
# smooth picture on a channel that could have afforded more.
#
# Two rather than four, and the reason is the shape of the losses rather than
# their number. At fourteen frames a second the controller sat between 10.4 and
# 13.6 with a median of 11.9 while a fixed fourteen held 14.4 -- it was climbing
# four seconds at a time and being knocked back by a single dropped frame, so it
# spent the session on the way up rather than at the top. Measured over the same
# 300 seconds: four windows gave a median of 11.9 and two give 13.4.
WINDOWS_BEFORE_UP = 2


# Whether the rate is chosen as the session runs, or held at media.FPS.
#
# Held is the right setting for a measurement and for nothing else: a sweep that
# wanted to know what the device does at ten frames a second would learn instead
# what the controller does about ten frames a second. It is off by default,
# because adapting is the point.
ADAPTIVE = os.environ.get("TV_ADAPTIVE", "1") != "0"


class FixedRate:
    """A stand-in that answers with the rate it was given, forever.

    Used when TV_ADAPTIVE=0. It answers the same three questions the real
    controller does -- what rate, what happened, why -- so the sender needs no
    branch of its own for the fixed case, and a measurement cannot accidentally
    be run against a sender that behaves differently from the one shipping.
    """

    def __init__(self, fps: int, budget: int = 300000):
        self.fps = fps
        self.budget = budget
        self.maximum = fps
        self.minimum = fps
        self.frame_bytes = None
        self.frame_bytes_now = None
        self.bytes = 0
        self.worst_write_ms = 0.0
        self.dropped = 0
        self.reason = "fixed"

    def observe(self, video_bytes: int, worst_write_ms: float,
                dropped: int = 0, frames: int = 0,
                window_s: float = 1.0) -> int:
        self.bytes, self.worst_write_ms = video_bytes, worst_write_ms
        self.dropped = dropped
        return self.fps

    def cost_now(self) -> float | None:
        """What a frame costs, erring towards the larger of the two estimates.

        The smoothed figure is stable and the instantaneous one is quick, and
        taking the larger means a source whose cost has just gone up is acted on
        at once while one that has just gone down is given time to prove it.
        Nothing here needs the second case to be quick: a rate that is briefly
        too low costs a few frames, and the climb is what corrects it.
        """
        if self.frame_bytes is None:
            return self.frame_bytes_now
        if self.frame_bytes_now is None:
            return self.frame_bytes
        return max(self.frame_bytes, self.frame_bytes_now)

    def ceiling(self) -> int:
        """The highest rate this channel can be carried at, from its own frames.

        The budget is the picture's share of the link in bytes a second; a frame
        of this channel costs `frame_bytes`; the rate that exactly spends the
        budget is the quotient. Below the measured cost of a frame it is a
        calculation, not a guess, and it is why one ceiling could not serve the
        list: measured across 103 channels, a frame runs from 2266 bytes to
        31271, so the same budget allows ten frames on one channel and three on
        another.

        Clamped to the configured range, and to `maximum` when nothing has been
        measured yet -- a ceiling derived from no measurement would be a number
        invented rather than taken.
        """
        if self.frame_bytes is None or self.frame_bytes <= 0:
            return self.maximum
        derived = int(self.budget / self.frame_bytes)
        return max(self.minimum, min(self.maximum, derived))

    def describe(self) -> str:
        return f"fps={self.fps} video={self.bytes // 1000}kB (fixed)"


class RateController:
    """Decides the next picture rate from what the last window cost.

    One window is one call to `observe`, and the caller decides how long a
    window is -- a second of wall clock in the sender. The controller keeps
    only the counts it needs to judge that window, so a session can start with
    a fresh one and nothing carries over from the channel before.
    """

    def __init__(self, start: int = START_FPS, minimum: int = MIN_FPS,
                 maximum: int = MAX_FPS, budget: int = VIDEO_BUDGET_BYTES):
        if not minimum <= start <= maximum:
            raise ValueError("start rate is outside the range")
        # What one frame of THIS channel costs, in bytes, as a moving average.
        # None until the first window that carried a frame, because it cannot be
        # known before then and a guess would be acted on.
        self.frame_bytes: float | None = None
        # The most recent window's own cost, used where speed matters. See
        # cost_now and observe.
        self.frame_bytes_now: float | None = None
        # How long the window being judged actually lasted. See observe.
        self.window_s = 1.0
        if minimum < 1:
            raise ValueError("a rate below one is not a rate")
        self.minimum, self.maximum, self.budget = minimum, maximum, budget
        self.fps = start
        self._comfortable = 0
        # True for the window that just ended when it carried no picture and no
        # measurable write, so capacity must not be inferred from it. See the
        # empty-window case in observe().
        self.insufficient = False # Everything the log and the tests want to see about the last decision.
        self.bytes = 0
        self.worst_write_ms = 0.0
        self.dropped = 0
        # Whether the window before this one also dropped frames. A ceiling is
        # only lowerd for drops that keep coming; see observe().
        self._dropped_last_window = False
        self.reason = "starting"

    def observe(self, video_bytes: int, worst_write_ms: float,
                dropped: int = 0, frames: int = 0,
                window_s: float = 1.0) -> int:
        """Take one window's measurements and return the rate for the next one.

        `video_bytes` is the picture's own share of what went out -- the sound
        is excluded because it is fixed and would otherwise make every channel
        look equally heavy. `worst_write_ms` is the longest a single write took,
        and it is the worst rather than the average on purpose: the average of a
        window that is mostly comfortable and briefly stuck is comfortable, and
        it is the stuck moment that ends a session.

        `dropped` is how many frames **this sender** gave up in the window
        because they could not be placed in time.

        **It is not device feedback, and this docstring used to claim it was.**
        An external review caught the contradiction: the value comes from
        `window_dropped` in `tv_server.py`, which is incremented on the branch
        where the sender abandons a frame, and the device reports nothing --
        there is no reverse message carrying its `late`, `nobuf` or dropped
        counts, and nothing reads them if there were. What the sender cannot see
        from its own socket is exactly the case described below, and it is still
        invisible: a fast link and a device that cannot draw look identical from
        here. Adding that path means a new protocol message and a receive loop
        that currently accepts only END. Measured: with the ceiling
        raised above the device's own, every session climbed to 14.9 frames a
        second, the receiver ran out of free buffers (its `nobuf` count went 0,
        12, 45 in three windows), frames were abandoned from then on, and the
        count never recovered -- while every window looked comfortable to the
        two signals below, because a link that is fast and a device that cannot
        draw are indistinguishable from the sending side. The writes were
        prompt -- the worst measured 43 ms against a 250 ms line -- and the
        bytes were under budget. Nothing was wrong except that pictures were
        being thrown away.
        """
        self.bytes, self.worst_write_ms = video_bytes, worst_write_ms
        self.dropped = dropped
        # What this channel's frames actually cost. This is the measurement the
        # ceiling was always guessing at, and it is free: the sender already
        # knows the bytes it put out and the frames it sent. A moving average
        # rather than the last window alone, because one window can be a still
        # shot or a cut.
        #
        # It is a measurement of the SOURCE, not of the rate: the same channel
        # produces the same bytes a frame at any frame rate, so this converges
        # whatever the controller is doing while it converges.
        if frames > 0:
            sample = video_bytes / frames
            # Kept separate from the smoothed figure, and this is what makes a
            # source that changes under a session safe.
            #
            # `sample` is the CONTENT's cost: bytes sent divided by frames sent.
            # The frame rate cancels out of it, so it is the same number whether
            # the controller was running at three frames or twelve -- which means
            # it can be read the instant it arrives, without waiting for the
            # average to catch up.
            #
            # The smoothed figure is what the ceiling is derived from, where a
            # stable number is wanted; this one is what the budget is tested
            # against, where a fast one is. A channel that cuts from a studio
            # shot to a chase can multiply its frame cost in a single second,
            # and an average with a four-second time constant would hold the old
            # rate for most of that.
            self.frame_bytes_now = sample
            if self.frame_bytes is None:
                self.frame_bytes = sample
            else:
                self.frame_bytes += (sample - self.frame_bytes) / 4.0
        # Whether this window is one of a run of lossy windows, decided before
        # anything acts on it.
        #
        # `_dropped_last_window` is the previous window's answer, so it has to be
        # read before it is overwritten. An isolated loss and a run of losses
        # mean different things: a run is a real ceiling and the rate comes
        # down, while a single burst that stops was measured at a fixed fifteen
        # frames a second as the pipeline emptying itself once -- the dropped
        # count climbed to 53 over the first seconds and then stayed there for
        # the remaining two minutes with the rate steady at 14.4. Treating that
        # burst as a ceiling put the controller at 10.5 where the device was
        # running 14.4.
        drop_running = bool(dropped) and self._dropped_last_window
        self._dropped_last_window = bool(dropped)
        # The window is normalised by how long it actually lasted, and that is
        # a correction rather than a nicety.
        #
        # "One decision a second" is what the sender intends, but the check is
        # `now - window_started >= 1.0` evaluated once per pass of a loop that
        # blocks inside a socket write -- so a window in which the loop was held
        # for 800 ms runs for 1.8 seconds and carries 1.8 seconds of frames.
        # Compared against a per-second budget it reads as over every time, and
        # the rate steps down on a link with nothing wrong with it.
        #
        # Measured with the ceiling at twelve: `fps=4 video=123kB frame=16885B`,
        # and 123000 / 16885 is 7.3 frames where the rate says four. Over a 1.8
        # second window it is 68 kB/s, which is 4 frames x 17 kB and exactly the
        # frame cost beside it. Window after window read 121, 127, 122, 126, 123
        # kB against a budget of 120 and stepped down each time, 5, 4, 3, while
        # `worst_write` stayed at 25 to 50 ms. That walk to the floor is what
        # this line removes.
        self.window_s = max(0.05, window_s)
        achieved = video_bytes / self.window_s
        # Two questions, two estimates, and the asymmetry is deliberate.
        #
        # Going DOWN is the conservative move, so it uses whichever estimate is
        # larger: losing a few frames to an unnecessary step down costs the
        # viewer far less than sending into a link that is already full. The
        # instantaneous cost is what catches a source that has just got heavier,
        # within the window it happened in.
        #
        # Going UP needs the opposite: evidence that the link has room, which
        # the achieved rate actually provides and a projection cannot.
        #
        # **A window that measured nothing does not get to use the projection.**
        # This is where the empty-window guard belongs, and putting it further
        # down was wrong: it came after the branch that acts on `over`, so an
        # established frame cost went on being multiplied by the current rate
        # and read as "still over budget" no matter how many empty windows
        # passed. Reproduced from an external review -- start at 8, feed one
        # 30000-byte frame, then three empty windows:
        #
        #     7, then 6, 5, 4, each reason "sent 0 kB in the window"
        #
        # The rate fell three times on three windows that carried nothing, which
        # is exactly what the guard was added to prevent. What it protects is the
        # *projection*: a stale cost says what the last real window cost, not
        # what this one is costing. The measurement that is still valid in an
        # empty window -- a slow write, or a drop -- is not protected, and is
        # judged below as it always was.
        # A window with nothing in it carries no measurement, and the rate must
        # not move on one. **There was a `measured_by_caller` argument here and
        # it is deliberately gone**: the sender was going to pass
        # `False` when it knew the source had stopped, and working out what that
        # should do showed it changed nothing. A window where the source is gone
        # but queued content is still going out has real bytes and a real write
        # time, and those are congestion evidence whatever the source is doing
        # -- slowing down is right there. A window where the queues are empty
        # already reads as unmeasured from the counters alone. So the argument
        # could not affect any input, and an argument that cannot affect any
        # input is a claim that it does.
        measured = bool(frames or video_bytes or worst_write_ms > 0.0 or dropped)
        # Set here, with the measurement it describes, and not at the end of
        # the function.
        #
        # It used to be cleared after the over-budget branch and after the drop
        # branch, both of which return early -- so a window that carried a real
        # slow write, a real overload or a real drop left the flag holding the
        # *previous* window's answer. Reproduced from an external review:
        #
        #     empty window  -> insufficient True
        #     200000 B, 500 ms write, 5 frames -> still True, reason "write was 500 ms"
        #
        # The rate came down correctly; only the label was stale. Nothing reads
        # this flag in the playback path, so it is a reporting fault and not a
        # second playback fault -- but a status that describes the wrong window
        # is exactly the kind of thing this project keeps being misled by, and
        # the three early-return paths are the ones worth naming.
        self.insufficient = not measured
        projected = (self.cost_now() or 0.0) * self.fps if measured else 0.0
        over = (worst_write_ms > SLOW_WRITE_MS
                or achieved > self.budget
                or projected > self.budget)
        # A dropped frame is the strongest form of "over" -- the device could not
        # take what it was sent -- but it is not a substitute for the other two.
        #
        # This used to return from inside the drop branch, before `over` was
        # even computed, so a window that both lost a frame AND recorded a write
        # stall or an over-budget total was judged on the drop alone. An isolated
        # drop is tolerated by design, so in that window every other piece of
        # evidence was discarded too. Measured with the controller's own inputs:
        # 960000 bytes in one second and a 5000 ms worst write, and an isolated
        # drop reported -- the controller held its rate where the same window
        # without the drop stepped down.
        #
        # So the drop test now decides only whether the *isolated* case is
        # tolerated. Everything else is decided by `over`, as it is in any other
        # window, and the two reasons are both named when both apply because they
        # call for different fixes and look identical from outside.
        if over:
            self.reason = ("write was %.0f ms" % worst_write_ms
                           if worst_write_ms > SLOW_WRITE_MS
                           else "sent %d kB in the window" % (video_bytes // 1000))
            if dropped:
                self.reason += " and dropped %d frame%s" % (dropped, "" if dropped == 1 else "s")
            self._comfortable = 0
            if self.fps > self.minimum:
                self.fps -= 1
            else:
                self.reason += "; already at the floor"
            return self.fps
        if dropped:
            self.reason = "dropped %d frame%s" % (dropped, "" if dropped == 1 else "s")
            if drop_running:
                self.reason += "; second window running"
                if self.fps > self.minimum:
                    self.fps -= 1
                # A run of losses is a real ceiling, so the run of comfortable
                # windows starts again from nothing.
                self._comfortable = 0
            return self.fps
        # A clean window after a lossy one does not clear the run of comfortable
        # windows, and that is the point of keeping the two apart. An isolated
        # dropped frame at the working point costs the picture one frame; making
        # it reset the climb cost the session its rate -- measured, four windows
        # of comfort needed and one loss in each cycle to clear it, so the
        # controller spent 300 seconds travelling and never arrived: median 11.9
        # against a fixed fourteen's 14.4.
        # Not over. Raising is a separate question and needs more evidence, so
        # the window has to look comfortable on both counts before it counts.
        # Judged the same way and for the same reason, in reverse: a window
        # sent at a rate that has just come down is small whatever the link is
        # like, so reading it as "room to spare" is how the controller climbs
        # back into a link that is already full.
        #
        # And a window with nothing in it is not evidence of anything. This is
        # the third case, added after an external review reproduced it: six
        # consecutive windows of 0 bytes, 0 frames and a 0 ms write climbed the
        # rate 5, 6, 6, 7, 7, 8. Every one of them looked comfortable on both
        # counts -- the writes were instant because nothing was written, and the
        # bytes were under budget because there were none. That is precisely the
        # situation these windows exist to be suspicious of: a source that has
        # stopped producing and a link with room to spare are the same reading
        # from here, and only one of them means "send faster".
        #
        # So a window that carried no picture and no measurement is marked
        # insufficient, the run of comfortable windows is broken by it, and the
        # capacity inference is frozen for that window. It does not lower the
        # rate: an empty window is not evidence of congestion either.
        if not measured:
            self._comfortable = 0
            self.reason = "no measurement in the window; holding"
            return self.fps
        if worst_write_ms < FAST_WRITE_MS and achieved < self.budget * 0.8:
            self._comfortable += 1
            if self._comfortable >= WINDOWS_BEFORE_UP and self.fps < self.ceiling():
                self.fps += 1
                self._comfortable = 0
                self.reason = "comfortable for %d windows" % WINDOWS_BEFORE_UP
                return self.fps
            self.reason = "comfortable (%d/%d)" % (self._comfortable, WINDOWS_BEFORE_UP)
        else:
            # Neither over the line nor comfortable: the window was ordinary.
            # The run of comfortable windows is broken, because what is wanted
            # before raising is a link with room to spare, not one that is
            # merely not yet failing.
            self._comfortable = 0
            self.reason = "holding"
        return self.fps

    def cost_now(self) -> float | None:
        """What a frame costs, erring towards the larger of the two estimates.

        The smoothed figure is stable and the instantaneous one is quick, and
        taking the larger means a source whose cost has just gone up is acted on
        at once while one that has just gone down is given time to prove it.
        Nothing here needs the second case to be quick: a rate that is briefly
        too low costs a few frames, and the climb is what corrects it.
        """
        if self.frame_bytes is None:
            return self.frame_bytes_now
        if self.frame_bytes_now is None:
            return self.frame_bytes
        return max(self.frame_bytes, self.frame_bytes_now)

    def ceiling(self) -> int:
        """The highest rate this channel can be carried at, from its own frames.

        The budget is the picture's share of the link in bytes a second; a frame
        of this channel costs `frame_bytes`; the rate that exactly spends the
        budget is the quotient. Below the measured cost of a frame it is a
        calculation, not a guess, and it is why one ceiling could not serve the
        list: measured across 103 channels, a frame runs from 2266 bytes to
        31271, so the same budget allows ten frames on one channel and three on
        another.

        Clamped to the configured range, and to `maximum` when nothing has been
        measured yet -- a ceiling derived from no measurement would be a number
        invented rather than taken.
        """
        if self.frame_bytes is None or self.frame_bytes <= 0:
            return self.maximum
        derived = int(self.budget / self.frame_bytes)
        return max(self.minimum, min(self.maximum, derived))

    def describe(self) -> str:
        """One line for the rate report, from the last window's own numbers."""
        frame = (f" frame={self.frame_bytes:.0f}B/{self.frame_bytes_now or 0:.0f}B"
                 if self.frame_bytes is not None else "")
        return (f"fps={self.fps} video={self.bytes // 1000}kB"
                f"/{self.window_s:.2f}s{frame} "
                f"ceiling={self.ceiling()} "
                f"worst_write={self.worst_write_ms:.0f}ms "
                f"dropped={self.dropped} ({self.reason})")
