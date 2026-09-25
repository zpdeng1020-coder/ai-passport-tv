#!/usr/bin/env python3
"""The two-ends agreement check, driven with configurations the device refuses.

This script had no tests, and it showed: three fields it printed were never
compared, so it answered "Both ends agree; CONFIG will be accepted" for a server
sending fps=31, sample_rate=8000 or audio_chunk_ms=20 -- every one of which
`config_valid()` rejects. An external review found it by setting those three and
reading the exit code, which was 0 each time.

**A check whose pass message is broader than what it checks is worse than no
check**, because it gets quoted as evidence. So the cases below are the ones
that were wrong, and each asserts the exit code rather than the wording: the
code is what a build script acts on.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import frames
from tools import check_config_agreement as agreement


class AgreementTests(unittest.TestCase):
    def run_check(self, **overrides) -> tuple[int, str]:
        """Run the real main() with a modified CONFIG, capturing its output."""
        saved = dict(agreement.CONFIG)
        agreement.CONFIG.update(overrides)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = agreement.main()
        finally:
            agreement.CONFIG.clear()
            agreement.CONFIG.update(saved)
        return code, out.getvalue()

    def test_the_shipped_configuration_is_accepted(self):
        """The default has to pass, or the check is useless in the other direction."""
        code, text = self.run_check()
        self.assertEqual(code, 0, text)
        self.assertIn("will be accepted", text)

    def test_the_geometry_the_device_compiles_in_is_agreed(self):
        """The check that already worked: one end's constants against the other's."""
        code, text = self.run_check(width=frames.WIDTH + 8)
        self.assertEqual(code, 1)
        self.assertIn("width", text)

    def test_an_out_of_range_frame_rate_fails(self):
        """The device accepts 1..30 (`json_between`), and 31 is outside it."""
        code, text = self.run_check(fps=31)
        self.assertEqual(code, 1, text)
        self.assertIn("1..30", text)

    def test_an_out_of_range_frame_rate_inside_the_range_still_passes(self):
        """The bounds themselves, so the range is not accidentally exclusive."""
        for value in (1, 30):
            code, text = self.run_check(fps=value)
            self.assertEqual(code, 0, f"fps={value}: {text}")

    def test_a_sample_rate_the_device_does_not_accept_fails(self):
        code, text = self.run_check(sample_rate=8000)
        self.assertEqual(code, 1, text)
        self.assertIn("sample_rate", text)

    def test_an_audio_chunk_length_the_device_does_not_accept_fails(self):
        """Checked against AV_AUDIO_MS in the firmware and media.py on the host.

        Three files have to hold the same number, which is one relation more
        than comparing against a literal.
        """
        code, text = self.run_check(audio_chunk_ms=20)
        self.assertEqual(code, 1, text)
        self.assertIn("audio_chunk_ms", text)

    def test_the_channels_key_is_the_audio_channel_count(self):
        """It has collided with `channel_list` twice; this is what catches it."""
        code, text = self.run_check(channels=2)
        self.assertEqual(code, 1, text)

    def test_every_checked_field_reports_its_own_mismatch(self):
        """One at a time, so a failure names which field and not just "bad config"."""
        for field, value in (("fps", 31), ("sample_rate", 8000),
                             ("channels", 2), ("sample_bits", 8),
                             ("audio_chunk_ms", 20), ("start_delay_ms", 500),
                             ("video_max_bytes", 1)):
            code, text = self.run_check(**{field: value})
            self.assertEqual(code, 1, f"{field}={value} was accepted: {text}")
            self.assertIn(field, text, f"{field} failed without naming itself")


if __name__ == "__main__":
    unittest.main()
