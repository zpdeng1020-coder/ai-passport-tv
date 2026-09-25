"""The adaptive picture rate, driven through every case it will meet.

These run in milliseconds what would take a session per case on hardware, which
is the reason the decision is a module of its own rather than inline in the
sender. The failures it exists to prevent -- a session ending mid-picture on a
channel too heavy for the link, and a quiet channel playing at half the rate it
could -- are both slow to reproduce against a real device and hard to attribute
once they happen.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.rate import (FAST_WRITE_MS, MAX_FPS, MIN_FPS, SLOW_WRITE_MS,
                         START_FPS, WINDOWS_BEFORE_UP, RateController)


# The rate the behaviour is observed from: the top of the range, so every case
# below is a step DOWN from it and the arithmetic does not depend on what the
# range happens to be. It was written as 8 and 10 while the range was [6, 12];
# when the ceiling came down to the measured five, those literals were outside
# the range and every case failed at construction -- a test that pins a constant
# it does not test.
# One below the ceiling, so there is room to climb as well as to fall. Starting
# AT the ceiling made the climbing cases untestable: the controller was already
# at the top and the assertion that it had risen was asserting it had broken its
# own limit.
TOP = MAX_FPS - 1

class ControllerTests(unittest.TestCase):
    def test_a_stalled_write_lowers_the_rate_immediately(self):
        """One write over the line is enough, and it cannot be averaged away.

        This is the case the controller exists for. A window that is mostly
        comfortable and briefly stuck has a comfortable average, and it is the
        stuck moment that ends the session -- so the worst write is what is
        judged, and one of them is enough to step down.
        """
        control = RateController(start=TOP)
        self.assertEqual(control.observe(120_000, SLOW_WRITE_MS + 50), TOP - 1)
        # And again, on the next window, rather than recovering first: the link
        # said nothing about having cleared. Clamped at the floor, because the
        # point being tested is that it falls twice without recovering, not that
        # the range has a particular size.
        self.assertEqual(control.observe(120_000, SLOW_WRITE_MS + 50),
                         max(TOP - 2, MIN_FPS))

    def test_going_over_budget_lowers_the_rate_even_with_fast_writes(self):
        """Bytes and write time are separate limits, and either one is enough.

        A channel can be light enough to write quickly and still too heavy to
        keep up with, if it is producing large frames faster than the link can
        carry them. Judging only the write time would miss that entirely.
        """
        control = RateController(start=TOP, budget=170_000)
        self.assertEqual(control.observe(200_000, 60.0), TOP - 1)
        self.assertIn("kB", control.reason)

    def test_the_rate_bottoms_out_and_says_so(self):
        """A link that stays bad must not walk the rate below the range."""
        control = RateController(start=MIN_FPS)
        self.assertEqual(control.observe(999_999, 999.0), MIN_FPS)
        self.assertIn("floor", control.reason)

    def test_the_rate_tops_out_and_stays_there(self):
        control = RateController(start=MAX_FPS)
        for _ in range(WINDOWS_BEFORE_UP * 3):
            rate = control.observe(10_000, 40.0)
        self.assertEqual(rate, MAX_FPS)

    def test_raising_needs_a_run_of_comfortable_windows(self):
        """Not one good window: a run of them, or the rate oscillates.

        Raising on any single comfortable window is the mistake that turns the
        controller into an oscillator -- the rate goes up, the link fills, a
        write is slow, the rate comes down, the link clears. The viewer sees the
        picture stutter in time with the judgement rather than settle.
        """
        control = RateController(start=TOP)
        for _ in range(WINDOWS_BEFORE_UP - 1):
            self.assertEqual(control.observe(50_000, 60.0), TOP)
        self.assertEqual(control.observe(50_000, 60.0), TOP + 1)

    def test_an_ordinary_window_does_not_count_towards_raising(self):
        """Sitting just under the limit is not room, and must not raise the rate.

        A window at 90% of the budget is not over it and is not comfortable
        either. Counting it would raise the rate into a link that has just
        demonstrated it is nearly full.
        """
        control = RateController(start=TOP, budget=170_000)
        for _ in range(WINDOWS_BEFORE_UP * 2):
            self.assertEqual(control.observe(160_000, 60.0), TOP)

    def test_a_comfortable_window_after_a_bad_one_starts_the_count_again(self):
        """The run has to be unbroken; one bad window resets it.

        The bad window also lowers the rate, so the run that follows climbs back
        to where it was rather than past it. Both halves matter: the climb takes
        the full run again -- the three comfortable windows before the fault do
        not count towards it -- and it stops at the rate the fault cost, which
        is the check that the fault is not immediately undone.
        """
        control = RateController(start=TOP)
        for _ in range(WINDOWS_BEFORE_UP - 1):
            control.observe(50_000, 60.0)
        lowered = control.observe(50_000, SLOW_WRITE_MS + 1)
        for _ in range(WINDOWS_BEFORE_UP - 1):
            self.assertEqual(control.observe(50_000, 60.0), lowered)
        self.assertEqual(control.observe(50_000, 60.0), TOP)

    def test_a_window_between_the_two_thresholds_holds(self):
        """Between comfortable and over: neither raised nor lowered."""
        control = RateController(start=TOP)
        self.assertEqual(control.observe(50_000, (FAST_WRITE_MS + SLOW_WRITE_MS) / 2), TOP)
        self.assertEqual(control.reason, "holding")

    def test_the_range_is_enforced_at_construction(self):
        with self.assertRaises(ValueError):
            RateController(start=MIN_FPS - 1)
        with self.assertRaises(ValueError):
            RateController(start=MAX_FPS + 1)
        with self.assertRaises(ValueError):
            RateController(minimum=0)

    def test_a_session_starts_from_the_declared_rate(self):
        """Nothing carries over from the channel before.

        A viewer changing from a heavy channel to a light one should not spend
        the first seconds climbing back from a floor the previous channel
        earned. Each session gets a new controller, and this is what says so.
        """
        heavy = RateController()
        for _ in range(10):
            heavy.observe(999_999, 999.0)
        self.assertEqual(heavy.fps, MIN_FPS)
        self.assertEqual(RateController().fps, START_FPS)

    def test_the_report_names_which_limit_was_met(self):
        """The two limits look identical from outside and call for different fixes."""
        by_write = RateController(start=TOP)
        by_write.observe(10_000, SLOW_WRITE_MS + 10)
        self.assertIn("ms", by_write.describe())
        by_bytes = RateController(start=TOP, budget=100_000)
        by_bytes.observe(500_000, 30.0)
        self.assertIn("kB", by_bytes.describe())

    def test_an_isolated_drop_in_an_otherwise_quiet_window_is_tolerated(self):
        """The design: one loss at the working point costs a frame, not a rate.

        A burst that stops was measured at a fixed fifteen frames a second as
        the pipeline emptying itself once -- the dropped count climbed to 53
        over the first seconds and then stayed there for two minutes with the
        rate steady. Stepping down for that first burst put the controller at
        10.5 where the device was running 14.4.
        """
        control = RateController(start=TOP)
        self.assertEqual(control.observe(50_000, 60.0, dropped=1), TOP)

    def test_a_drop_in_an_overloaded_window_does_not_hide_the_overload(self):
        """An isolated drop must not become a reason to ignore everything else.

        This is the case that was wrong. The drop branch returned before the
        over-budget and slow-write tests were even computed, so a window that
        lost one frame AND recorded a five-second write was judged on the drop
        alone -- and an isolated drop is tolerated by design, so the rate was
        held where the same window without the drop stepped down. Tolerating one
        loss is not a reason to discard the evidence beside it.
        """
        quiet = RateController(start=TOP)
        self.assertEqual(quiet.observe(50_000, 60.0, dropped=1), TOP)

        overloaded = RateController(start=TOP)
        held = overloaded.observe(960_000, 5000.0, dropped=1)
        self.assertEqual(held, TOP - 1)

    def test_a_drop_in_a_slow_write_window_still_lowers_the_rate(self):
        """Same defect, reached by the other of the two limits."""
        control = RateController(start=TOP)
        self.assertEqual(control.observe(10_000, SLOW_WRITE_MS + 1, dropped=1), TOP - 1)

    def test_a_run_of_drops_lowers_the_rate_once_per_window(self):
        """A run is a ceiling, and the run is what distinguishes it from a burst."""
        control = RateController(start=TOP)
        # The first loss is tolerated; the second consecutive one is not.
        self.assertEqual(control.observe(50_000, 60.0, dropped=1), TOP)
        self.assertEqual(control.observe(50_000, 60.0, dropped=1), TOP - 1)
        self.assertEqual(control.observe(50_000, 60.0, dropped=1), TOP - 2)

    def test_a_clean_window_ends_the_run_of_drops(self):
        """A burst that stops must stop costing the rate, or the climb never lands."""
        control = RateController(start=TOP)
        control.observe(50_000, 60.0, dropped=1)
        control.observe(50_000, 60.0, dropped=1)
        lowered = TOP - 1
        self.assertEqual(control.observe(50_000, 60.0), lowered)
        # And the next isolated loss is treated as isolated again.
        self.assertEqual(control.observe(50_000, 60.0, dropped=1), lowered)

    def test_an_empty_window_never_raises_the_rate(self):
        """A window with no picture in it is not evidence of anything.

        Found by an external review and reproduced exactly: six consecutive
        windows of 0 bytes, 0 frames and a 0 ms write climbed the rate
        5, 6, 6, 7, 7, 8. Both comfort tests passed every time -- the writes
        were instant because nothing was written and the bytes were under
        budget because there were none. A source that stopped producing and a
        link with room to spare read identically from the sending side, and
        only one of them means "send faster".
        """
        control = RateController()
        climbed = [control.observe(0, 0.0, dropped=0, frames=0, window_s=1.0)
                   for _ in range(6)]
        self.assertEqual(climbed, [START_FPS] * 6)
        self.assertIn("no measurement", control.reason)

    def test_an_empty_window_does_not_lower_the_rate_either(self):
        """Empty is not congestion. It freezes the inference; it does not act."""
        control = RateController(start=TOP)
        self.assertEqual(control.observe(0, 0.0, dropped=0, frames=0), TOP)

    def test_an_empty_window_breaks_the_run_of_comfortable_windows(self):
        """The run must start again after it, or the climb resumes as if it counted.

        A window that carried nothing cannot be part of the evidence that the
        link has room, so it has to cost the run rather than be skipped over.
        """
        control = RateController()
        self.assertEqual(control.observe(50_000, 60.0), START_FPS)   # 1 comfortable
        control.observe(0, 0.0, dropped=0, frames=0)                  # breaks it
        self.assertEqual(control.observe(50_000, 60.0), START_FPS)   # 1 again, not 2
        self.assertEqual(control.observe(50_000, 60.0), START_FPS + 1)

    def test_a_window_with_picture_in_it_still_climbs_normally(self):
        """The empty-window guard must not stop the ordinary climb."""
        control = RateController()
        for _ in range(WINDOWS_BEFORE_UP - 1):
            self.assertEqual(control.observe(50_000, 60.0), START_FPS)
        self.assertEqual(control.observe(50_000, 60.0), START_FPS + 1)
        self.assertFalse(control.insufficient)

    def test_a_window_with_bytes_but_no_frames_is_not_treated_as_empty(self):
        """Bytes moved, so the link answered a question; that is a measurement."""
        control = RateController(start=TOP)
        control.observe(50_000, 60.0, dropped=0, frames=0)
        self.assertFalse(control.insufficient)

    def test_a_stale_cost_does_not_keep_lowering_the_rate_when_the_source_stops(self):
        """The guard makes no difference if it runs after the branch it guards.

        Found by an external review and reproduced exactly. The empty-window
        guard was placed after the over-budget test, so an established frame
        cost went on being multiplied by the current rate and read as "still
        over budget" however many empty windows passed:

            7, then 6, 5, 4, each reason "sent 0 kB in the window"

        Three windows carrying nothing, three steps down. The original case --
        a fresh controller fed six empty windows -- passed the whole time,
        because a controller with no cost estimate has nothing to project.
        """
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        self.assertEqual(control.observe(30_000, 20, frames=1), 7)
        for _ in range(3):
            self.assertEqual(control.observe(0, 0, frames=0), 7)
            self.assertTrue(control.insufficient)

    def test_a_stale_cost_still_bounds_the_rate_when_the_source_returns(self):
        """Freezing the projection must not discard the cost itself.

        The cost is a property of the content and stays valid across an outage;
        what an empty window cannot say is that the current rate is over it.
        So the rate holds while the source is gone and falls again once it
        comes back heavy.
        """
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(30_000, 20, frames=1)
        for _ in range(3):
            control.observe(0, 0, frames=0)
        # Source returns, same heavy frames: now the projection is measured and
        # the rate responds again.
        self.assertEqual(control.observe(60_000, 20, frames=2), 6)

    def test_an_empty_window_still_reports_a_real_slow_write(self):
        """No picture is not the same as no evidence.

        An empty window can still carry a write that took too long, and the
        guard must not swallow it -- that is congestion measured directly,
        rather than inferred from a cost.
        """
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        self.assertEqual(control.observe(0, SLOW_WRITE_MS + 1, frames=0), 7)
        self.assertIn("ms", control.reason)

    def test_an_empty_window_still_reports_a_drop(self):
        """Same, for the other direct signal from the device."""
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(30_000, 20, frames=1, dropped=1)
        self.assertNotIn("no measurement", control.reason)

    def test_the_insufficient_flag_clears_on_the_next_real_window(self):
        """It describes the window that just ended, not a state that sticks.

        **This test on its own proved nothing**, and an external review said so:
        a light, ordinary window reaches the end of `observe()` and is cleared
        there. The clearing used to sit *after* the over-budget branch and after
        the drop branch, both of which return early, so the three paths below
        left the flag holding the previous window's answer. The four cases
        together are what covers it.
        """
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(0, 0, frames=0)
        self.assertTrue(control.insufficient)
        control.observe(10_000, 20, frames=1)
        self.assertFalse(control.insufficient)

    def test_the_flag_clears_when_the_next_window_is_an_overload(self):
        """The early-return path that the old test could not reach."""
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(0, 0, frames=0)
        control.observe(200_000, 500, frames=5)
        self.assertFalse(control.insufficient)
        self.assertIn("ms", control.reason)

    def test_the_flag_clears_when_the_next_window_is_only_a_slow_write(self):
        """No picture, but the socket answered: that is a measurement."""
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(0, 0, frames=0)
        control.observe(0, SLOW_WRITE_MS + 1, frames=0)
        self.assertFalse(control.insufficient)

    def test_the_flag_clears_when_the_next_window_reports_a_drop(self):
        """The other early return."""
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        control.observe(0, 0, frames=0)
        control.observe(0, 0, frames=0, dropped=1)
        self.assertFalse(control.insufficient)

    def test_the_flag_is_true_only_while_the_window_is_empty(self):
        """It describes one window. Two empties then a real one ends it."""
        control = RateController(start=8, minimum=3, maximum=12, budget=120_000)
        for _ in range(2):
            control.observe(0, 0, frames=0)
            self.assertTrue(control.insufficient)
        control.observe(10_000, 20, frames=1)
        self.assertFalse(control.insufficient)

    def test_the_dropped_count_is_still_reported(self):
        """The reason survives the reordering: an overloaded window names both."""
        control = RateController(start=TOP)
        control.observe(960_000, 5000.0, dropped=3)
        self.assertIn("dropped", control.describe())
        self.assertIn("ms", control.describe())


if __name__ == "__main__":
    unittest.main()
