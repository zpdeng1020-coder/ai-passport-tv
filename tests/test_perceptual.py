"""Giving detail up perceptually when a frame does not fit its byte target.

    PYTHONPATH=. python tests/test_perceptual.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import frames, perceptual

if not perceptual.AVAILABLE:
    raise unittest.SkipTest("numpy is needed for these tests")

import numpy as np

W, H = frames.WIDTH, frames.HEIGHT


def gradient(seed=0, noise=6):
    """A smooth colour picture with a little noise, as ffmpeg's 3-3-2 indices."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:H, 0:W]
    base = np.stack([x * 255.0 / W, y * 255.0 / H, (x + y) * 255.0 / (W + H)], -1)
    rgb = np.clip(base + rng.normal(0, noise, base.shape), 0, 255)
    return ((rgb[..., 0] // 32).astype(np.uint8) << 5 | (rgb[..., 1] // 32).astype(np.uint8) << 2
            | (rgb[..., 2] // 64).astype(np.uint8))


def distance(a, b):
    gap = perceptual._PALETTE_W[a] - perceptual._PALETTE_W[b]
    return (gap * gap).sum(-1)


class UnderATarget(unittest.TestCase):
    def frame(self, seed=0):
        return gradient(seed).tobytes()

    def whole(self, raw, shown=None, tick=0):
        return frames._wire_size(frames.choose_stripes(raw, shown, tick, 1 << 30))

    def test_a_frame_that_fits_is_sent_exactly_as_it_came(self):
        raw = self.frame()
        chosen, drawn, level = perceptual.encode_within(raw, None, 0, 1 << 30)
        self.assertEqual(level, 0)
        self.assertEqual(drawn, raw)
        self.assertEqual(chosen, frames.choose_stripes(raw, None, 0, 1 << 30))

    def test_a_frame_over_the_target_is_brought_under_it(self):
        raw = self.frame()
        target = self.whole(raw) * 6 // 10
        chosen, drawn, level = perceptual.encode_within(raw, None, 0, target)
        self.assertGreater(level, 0)
        self.assertLessEqual(frames._wire_size(chosen), target)

    def test_giving_up_detail_costs_less_error_than_the_colour_ladder(self):
        raw = self.frame(2)
        target = self.whole(raw) * 7 // 10
        _, snapped, level = perceptual.encode_within(raw, None, 0, target)
        _, laddered, _ = frames.encode_within(raw, None, 0, target)
        reference = np.frombuffer(raw, np.uint8).reshape(H, W)
        self.assertGreater(level, 0)
        self.assertLess(distance(np.frombuffer(snapped, np.uint8).reshape(H, W), reference).mean(),
                        distance(np.frombuffer(laddered, np.uint8).reshape(H, W), reference).mean())

    def test_no_pixel_moves_farther_than_the_threshold_of_its_level(self):
        raw = self.frame(5)
        first = perceptual.encode_within(raw, None, 0, 1 << 30)
        shown = frames.apply_stripes(first[1], None, first[0])
        moved = gradient(6, noise=25).tobytes()
        target = self.whole(moved, shown, 1) // 3
        _, drawn, level = perceptual.encode_within(moved, shown, 1, target)
        self.assertGreater(level, 0)
        far = distance(np.frombuffer(drawn, np.uint8).reshape(H, W),
                       np.frombuffer(moved, np.uint8).reshape(H, W))
        self.assertLessEqual(float(far.max()), perceptual.LEVELS[level] + 1e-2)
        self.assertGreater(float(far.max()), 0.0, "something was actually given up")

    def test_any_hint_fits_the_target_and_is_never_finer_than_the_cheapest_level(self):
        raw = self.frame(3)
        target = self.whole(raw) // 2
        cheapest = perceptual.encode_within(raw, None, 0, target, hint=0)[2]
        for hint in (0, 1, 5, 9):
            chosen, _, level = perceptual.encode_within(raw, None, 0, target, hint=hint)
            self.assertLessEqual(frames._wire_size(chosen), target, hint)
            self.assertGreaterEqual(level, cheapest, hint)
            self.assertLessEqual(level, max(cheapest, hint), hint)

    def test_easy_content_returns_to_full_detail_whatever_the_hint(self):
        self.assertEqual(perceptual.encode_within(self.frame(3), None, 0, 1 << 30, hint=9)[2], 0)

    def test_a_target_nothing_can_meet_still_returns_a_frame(self):
        chosen, drawn, level = perceptual.encode_within(self.frame(4), None, 0, 200)
        self.assertEqual(level, len(perceptual.LEVELS) - 1)
        self.assertEqual(len(drawn), frames.FRAME_PIXELS)

    def test_a_wrong_sized_frame_is_refused(self):
        with self.assertRaises(ValueError):
            perceptual.encode_within(bytes(10), None, 0, 1000)


if __name__ == "__main__":
    unittest.main()
