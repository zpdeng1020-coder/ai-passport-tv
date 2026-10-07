"""Choosing which stripes of a frame to send, within a byte budget.

What this is for: the frame rate is fixed for a session, so when the link is short
the only thing left to give is bytes per frame. A frame therefore carries only the
stripes that changed, and of those only as many as the budget pays for, biggest
change first. Measured on recorded channels, 15-34% of stripes are byte-identical
to the previous frame's and 50-88% differ in 10% of pixels or fewer, so a full
frame spends most of its bytes on a picture the panel is already showing.

It is host logic only; no device was involved. The properties worth more than the
rest are here: a frame that is never sent must not change what the device is
assumed to be showing, the picture the device ends up with after a run of frames
must be the last frame's own picture when the budget is not binding, and a stripe
that misses out on the budget must still be the first in line the next frame.
"""
import random
import unittest
import zlib

from server import frames

BIG = 10 ** 9


def make_frame(seed: int) -> bytes:
    n = frames.FRAME_PIXELS
    return random.Random(seed).getrandbits(8 * n).to_bytes(n, "little")


def stripe(frame: bytes, at: int) -> bytes:
    return frame[at * frames.STRIPE_PIXELS:(at + 1) * frames.STRIPE_PIXELS]


def altered(frame: bytes, at: int, share: float, seed: int = 0) -> bytes:
    """`frame` with `share` of stripe `at`'s pixels changed to a different value."""
    buf = bytearray(frame)
    lo = at * frames.STRIPE_PIXELS
    count = int(frames.STRIPE_PIXELS * share)
    for i in random.Random(seed).sample(range(frames.STRIPE_PIXELS), count):
        buf[lo + i] = (buf[lo + i] + 1) % 256
    return bytes(buf)


def sent_at(chosen: list[bytes]) -> list[int]:
    return [at for at, s in enumerate(chosen) if s]


def base_cost(refresh_stripe: bytes) -> int:
    """Header, length table and the refresh stripe: what a frame costs before
    any stripe competes for the budget."""
    return 2 + 2 * frames.STRIPES + len(refresh_stripe)


def unpack_to_stripe(payloads: list[bytes]) -> list[bytes | None]:
    """Stripe by stripe, None where the frame carried no stripe."""
    out: list[bytes | None] = []
    expected = 0
    for payload in payloads:
        first, count = payload[0], payload[1]
        assert first == expected, f"stripe run starts at {first}, expected {expected}"
        while len(out) < first:
            out.append(None)
        table = 2
        lengths = [int.from_bytes(payload[table + 2 * i:table + 2 * i + 2], "big")
                   for i in range(count)]
        assert sum(lengths) == len(payload) - table - 2 * count
        data = table + 2 * count
        for length in lengths:
            out.append(payload[data:data + length] or None)
            data += length
        expected += count
    while len(out) < frames.STRIPES:
        out.append(None)
    assert len(out) == frames.STRIPES
    return out


def panel_after(sent: list[list[bytes | None]]) -> bytes:
    """What the panel shows after those frames: the last stripe sent for each."""
    shown = bytearray(frames.FRAME_PIXELS)
    for stripes in sent:
        for at, s in enumerate(stripes):
            if s is not None:
                shown[at * frames.STRIPE_PIXELS:(at + 1) * frames.STRIPE_PIXELS] = zlib.decompress(s)
    return bytes(shown)


class ChoosingStripes(unittest.TestCase):
    def test_first_frame_sends_every_stripe(self):
        raw = make_frame(1)
        self.assertEqual(frames.choose_stripes(raw, None, 0, 0), frames.compress_stripes(raw))

    def test_identical_frame_sends_only_the_refresh(self):
        raw = make_frame(2)
        self.assertEqual(sent_at(frames.choose_stripes(raw, raw, 4, BIG)), [4])

    def test_the_refresh_rotates_so_nothing_stays_stale(self):
        raw = make_frame(3)
        self.assertEqual([sent_at(frames.choose_stripes(raw, raw, t, BIG))[0]
                          for t in range(frames.STRIPES)], list(range(frames.STRIPES)))

    def test_only_the_changed_stripe_goes_besides_the_refresh(self):
        raw = make_frame(4)
        shown = altered(raw, 3, 0.5)
        self.assertEqual(sent_at(frames.choose_stripes(raw, shown, 9, BIG)), [3, 9])

    def test_a_stripe_below_the_tolerance_waits(self):
        raw = make_frame(5)
        shown = altered(raw, 3, frames.DELTA_MAX_DIFF / 2)
        self.assertEqual(sent_at(frames.choose_stripes(raw, shown, 9, BIG)), [9])

    def test_no_allowance_still_sends_the_refresh(self):
        # A frame with no payload is not a thing this protocol can send, and the
        # refresh is what prevents it however tight the budget is.
        raw = make_frame(6)
        shown = altered(altered(raw, 2, 0.5, 1), 5, 0.5, 2)
        self.assertEqual(sent_at(frames.choose_stripes(raw, shown, 0, 0)), [0])

    def test_the_biggest_change_wins_a_budget_that_fits_one(self):
        raw = make_frame(7)
        shown = altered(altered(raw, 3, 0.5, 1), 5, 0.1, 2)
        z3, z5 = frames._deflate(raw, 3), frames._deflate(raw, 5)
        allowed = base_cost(frames._deflate(raw, 9)) + max(len(z3), len(z5))
        self.assertEqual(sent_at(frames.choose_stripes(raw, shown, 9, allowed)), [3, 9])

    def test_a_stripe_that_does_not_fit_does_not_block_a_smaller_one(self):
        # Stripe 3 changes a lot and is noise, so it is expensive and ranks first;
        # stripe 5 changes a little and is nearly flat, so it is cheap. With room
        # for only the cheap one, the cheap one must still go.
        flat = bytes(frames.FRAME_PIXELS)
        raw = bytearray(flat)
        lo3, lo5 = 3 * frames.STRIPE_PIXELS, 5 * frames.STRIPE_PIXELS
        raw[lo3:lo3 + frames.STRIPE_PIXELS] = make_frame(8)[:frames.STRIPE_PIXELS]
        for i in range(0, frames.STRIPE_PIXELS, 25):
            raw[lo5 + i] = 200
        raw = bytes(raw)
        cheap, dear = frames._deflate(raw, 5), frames._deflate(raw, 3)
        self.assertLess(len(cheap), len(dear))
        allowed = base_cost(frames._deflate(raw, 9)) + len(cheap) + 1
        self.assertEqual(sent_at(frames.choose_stripes(raw, flat, 9, allowed)), [5, 9])

    def test_a_stripe_that_missed_the_budget_ranks_first_next_frame(self):
        # Nothing is remembered about who missed out. The device still holds the
        # old stripe, so the next frame it differs by at least as much and is
        # ranked at least as high.
        raw = make_frame(9)
        shown = altered(altered(raw, 3, 0.5, 1), 5, 0.1, 2)
        first = frames.choose_stripes(raw, shown, 9, base_cost(frames._deflate(raw, 9)))
        self.assertEqual(sent_at(first), [9])
        shown = frames.apply_stripes(raw, shown, first)
        second = frames.choose_stripes(raw, shown, 10, base_cost(frames._deflate(raw, 10))
                                       + len(frames._deflate(raw, 3)))
        self.assertEqual(sent_at(second), [3, 10])

    def test_a_wrong_sized_frame_is_refused(self):
        with self.assertRaises(ValueError):
            frames.choose_stripes(b"x", None, 0, 0)


class WhatTheDeviceIsShown(unittest.TestCase):
    def test_a_stripe_not_sent_is_left_as_it_was(self):
        raw, shown = make_frame(10), make_frame(11)
        chosen = [b""] * frames.STRIPES
        chosen[2] = b"x"
        merged = frames.apply_stripes(raw, shown, chosen)
        self.assertEqual(stripe(merged, 2), stripe(raw, 2))
        self.assertEqual(stripe(merged, 3), stripe(shown, 3))

    def test_before_the_first_frame_the_base_is_the_frame(self):
        raw = make_frame(12)
        self.assertEqual(frames.apply_stripes(raw, None, frames.compress_stripes(raw)), raw)

    def test_the_picture_after_a_run_of_frames_is_the_last_frame(self):
        # Noise changes nearly every pixel of every stripe, so with an unlimited
        # budget and no tolerance this sends everything; what it checks is that
        # the assembly of stripes across frames is exact.
        video = [make_frame(s) for s in (20, 21, 22)]
        shown, sent = None, []
        for tick, raw in enumerate(video):
            chosen = frames.choose_stripes(raw, shown, tick, BIG, min_diff=0.0)
            shown = frames.apply_stripes(raw, shown, chosen)
            sent.append(unpack_to_stripe(frames.pack_stripes(chosen)))
        self.assertEqual(panel_after(sent), video[-1])

    def test_a_frame_that_is_never_sent_does_not_change_what_is_shown(self):
        # Frames a full queue discards are dropped before any of this runs, so the
        # comparison is against the last frame SENT: here the middle frame is
        # decoded and never sent, and the third's choice is against the first.
        first, third = make_frame(30), make_frame(32)
        chosen = frames.choose_stripes(third, first, 7, BIG)
        for at in range(frames.STRIPES):
            if at == 7 or chosen[at]:
                continue
            self.assertLessEqual(frames.differing_pixels(stripe(first, at), stripe(third, at)),
                                 int(frames.STRIPE_PIXELS * frames.DELTA_MAX_DIFF))

    def test_skipped_stripes_cost_two_bytes_each(self):
        chunks = [b""] * (frames.STRIPES - 1) + [b"z" * 40]
        payloads = frames.pack_stripes(chunks)
        self.assertEqual(len(payloads), 1)
        self.assertEqual((payloads[0][0], payloads[0][1]), (0, frames.STRIPES))
        self.assertEqual(len(payloads[0]), 2 + 2 * frames.STRIPES + 40)
        self.assertEqual(unpack_to_stripe(payloads)[frames.STRIPES - 1], b"z" * 40)

    def test_pack_stripes_matches_a_full_frame_with_no_gaps(self):
        raw = make_frame(13)
        self.assertEqual(frames.pack_stripes(frames.compress_stripes(raw)),
                         frames.frame_packets(raw))


class TheByteBudget(unittest.TestCase):
    def test_a_frame_is_the_rate_over_the_frame_rate(self):
        self.assertEqual(frames.ByteBudget(500_000, 25).target(), 20_000)

    def test_it_follows_the_rate_and_the_frame_rate(self):
        budget = frames.ByteBudget(500_000, 25)
        budget.set_rate(250_000)
        self.assertEqual(budget.target(), 10_000)
        budget.set_fps(50)
        self.assertEqual(budget.target(), 5_000)


def picture(seed: int, colours: int = 256) -> bytes:
    """A frame with real structure: smooth gradients plus a little noise."""
    r = random.Random(seed)
    out = bytearray()
    for y in range(frames.HEIGHT):
        for x in range(frames.WIDTH):
            v = (x * 3 + y * 5 + r.randrange(6)) % colours
            out.append(v)
    return bytes(out)


class FittingATarget(unittest.TestCase):
    def test_a_frame_that_fits_is_sent_exactly_as_it_is(self):
        raw = bytes(frames.FRAME_PIXELS)
        chosen, drawn, rung = frames.encode_within(raw, None, 0, 20_000)
        self.assertEqual((rung, drawn), (0, raw))

    def test_a_frame_that_does_not_fit_is_made_coarser_until_it_does(self):
        raw = picture(1)
        lossless = frames._wire_size(frames.encode_within(raw, None, 0, 1 << 30)[0])
        target = int(lossless * 0.7)
        chosen, drawn, rung = frames.encode_within(raw, None, 0, target)
        self.assertGreater(rung, 0)
        self.assertNotEqual(drawn, raw)
        self.assertLessEqual(frames._wire_size(chosen), target)

    def test_a_tighter_target_never_gives_a_finer_picture(self):
        raw = picture(2)
        rungs = [frames.encode_within(raw, None, 0, t)[2] for t in (40_000, 20_000, 10_000, 5_000)]
        self.assertEqual(rungs, sorted(rungs))

    def test_every_stripe_sent_is_from_this_frame(self):
        # The point of coarsening instead of dropping: nothing old is mixed in.
        raw, shown = picture(3), picture(4)
        chosen, drawn, _ = frames.encode_within(raw, shown, 5, 12_000)
        for at, z in enumerate(chosen):
            if z:
                self.assertEqual(zlib.decompress(z), stripe(drawn, at))

    def test_the_coarsest_rung_falls_back_to_leaving_stripes_out(self):
        raw, shown = picture(5), picture(6)
        chosen, _, rung = frames.encode_within(raw, shown, 0, 3_000)
        self.assertEqual(rung, len(frames.LADDER) - 1)
        self.assertLessEqual(frames._wire_size(chosen), 3_000 + len(frames._deflate(raw, 0)))

    def test_nothing_is_remembered_between_frames(self):
        a, b = picture(7), picture(8)
        first = frames.encode_within(a, None, 0, 15_000)
        frames.encode_within(b, None, 0, 5_000)
        self.assertEqual(frames.encode_within(a, None, 0, 15_000), first)

    def test_the_ladder_starts_lossless_and_only_coarsens(self):
        self.assertEqual(frames.LADDER[0], bytes(range(256)))
        for table in frames.LADDER:
            self.assertEqual(len(table), 256)
            self.assertEqual(table[0], 0)
        distinct = [len(set(t)) for t in frames.LADDER]
        self.assertEqual(distinct, sorted(distinct, reverse=True))


if __name__ == "__main__":
    unittest.main()
