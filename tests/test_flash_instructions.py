"""The flashing instructions, in the three places they are written down.

The same four steps are stated three times: on the flashing page a user opens
in a browser, in the English README, and in the Chinese one. Nothing connected
them, and they drifted -- all three said the device restarts on its own once
flashing finishes, and it does not.

Two facts were wrong underneath that sentence, and both were checked against
the hardware rather than assumed:

* The device talks over the ESP32-C3's built-in USB peripheral. `loader.after()`
  resets by pulling the RTS line, which is how an external USB-to-serial chip
  is wired; a line belonging to a USB peripheral has no reset pin to pull.
* The device has a 520 mAh battery. Unplugging the cable stops the charging and
  nothing else, so it does not restart the chip either.

The consequence is not cosmetic. After flashing, the chip sits in download mode
with a dark screen, which to anyone who does not know what download mode is
looks exactly like a failed write. The step that fixes it -- hold the power
button to switch the device off and on -- was missing from all three.

These checks are about that step being present, not about the wording. A test
that pinned the sentences would fail on every improvement to them, and the
useful property is narrower: whoever edits one of these places has to keep
telling the reader how to get the device running.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The page and the two READMEs, each with the phrase that tells the reader to
# power-cycle the device. Different words, because they are written for
# different places -- a page read mid-task and a README read beforehand.
PLACES = {
    "docs/flash/index.html": ("按住电源键", "关机"),
    "README.zh_CN.md": ("按住电源键", "关机"),
    "README.md": ("power button", "switch off"),
}


class PowerCycleStepTests(unittest.TestCase):
    def _text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_every_place_tells_the_reader_to_power_cycle(self):
        for relative, phrases in PLACES.items():
            with self.subTest(place=relative):
                text = self._text(relative)
                for phrase in phrases:
                    self.assertIn(
                        phrase, text,
                        f"{relative} no longer tells the reader to switch the device "
                        f"off and on after flashing; without that step the screen "
                        f"stays dark and the flash looks failed")

    # The sections that describe flashing, and only those. Scoping matters here
    # and the first version of this test did not do it: the README elsewhere
    # says the device restarts itself after the setup page is submitted, and
    # that sentence is true -- the firmware calls esp_restart() once the
    # credentials are saved. A blanket ban on the phrase failed on it.
    #
    # The two cases differ in mechanism, which is the whole point: one is the
    # firmware restarting on purpose, the other is a reset signal that has no
    # pin to travel along. Telling them apart requires looking at where the
    # sentence is, not only at what it says.
    SECTIONS = {
        "docs/flash/index.html": ("<h2>刷完之后</h2>", '<div class="warnbox">'),
        "README.zh_CN.md": ("## 第一步：刷固件", "## 第二步"),
        "README.md": ("## Step 1", "## Step 2"),
    }

    def _flashing_section(self, relative: str) -> str:
        text = self._text(relative)
        start, end = self.SECTIONS[relative]
        beginning = text.index(start)
        return text[beginning:text.index(end, beginning)]

    def test_the_flashing_section_does_not_claim_a_restart_by_itself(self):
        """The sentence that was wrong, in the place it was wrong.

        A regression here would be silent: the instruction reads perfectly well
        and is simply untrue of this hardware, and the reader has no way to tell
        a screen dark in download mode from a screen dark after a bad write.
        """
        forbidden = (
            "设备已重启", "会自己重启", "会自动重启", "拔掉数据线，设备会",
            "restarts on its own", "will restart itself", "restarts automatically",
        )
        for relative in self.SECTIONS:
            with self.subTest(place=relative):
                section = self._flashing_section(relative)
                for phrase in forbidden:
                    self.assertNotIn(
                        phrase, section,
                        f"{relative} says, while describing flashing, that the device "
                        f"restarts by itself; it has a battery and built-in USB, so "
                        f"nothing it does on its own takes it out of download mode")

    def test_the_provisioning_section_may_still_say_it(self):
        """The other side of the same line, so the scoping cannot be a pretext
        for banning the words everywhere -- in the setup flow it is accurate."""
        section = self._text("README.zh_CN.md")
        self.assertIn("设备会自己重启", section,
                      "the setup flow no longer says the device restarts after the "
                      "credentials are saved, which it does")

    def test_the_page_does_not_run_a_reset_that_cannot_work(self):
        """`loader.after()` defaults to a reset this hardware ignores.

        Asserted at the source level because the failure is invisible from the
        outside: the call succeeds, esptool logs "Hard resetting via RTS pin...",
        and nothing happens. Anyone re-adding it would see a green log line and
        no reason to doubt it.
        """
        page = self._text("docs/flash/index.html")
        body = re.sub(r"(?m)^\s*//.*$", "", page)     # ignore the explanation
        self.assertNotIn(
            "loader.after(", body,
            "the page calls loader.after(), whose default reset pulls a line this "
            "device's USB peripheral does not have; the call reports success and "
            "resets nothing")

    def test_the_forbidden_phrases_are_ones_that_would_actually_appear(self):
        """The scan has to be able to fail, or it proves nothing."""
        text = "1. 拔掉数据线，设备会自己重启。"
        self.assertIn("会自己重启", text)


if __name__ == "__main__":
    unittest.main()
