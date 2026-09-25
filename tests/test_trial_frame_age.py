"""Do not send an old picture merely because a permissive default allows it."""
import unittest
from unittest.mock import patch

from server import live


class FrameAgeRegression(unittest.TestCase):
    def test_stale_picture_is_dropped_without_restamping_its_content(self):
        url = "http://127.0.0.1/reviewer-no-network-fixture"
        with patch.dict(live.CHANNELS, {"reviewer": url}):
            channel = live.LiveChannel(url, "ffmpeg")
        channel.build_palette()
        channel.calibrate_from_source()
        for index in range(26):
            channel._push_audio(bytes(live.AUDIO_BYTES), index * 40)
        for _ in range(25):
            channel.pop_audio()
        # Pending audio is at 1000 ms. The old default (1 s) returned the
        # 400 ms picture; the trial default must drop it and keep the real
        # 900 ms timestamp of the next picture, not relabel the old one.
        for stamp in (400, 900):
            channel._push_video(bytes(live.frames.FRAME_PIXELS), stamp)
        result = channel.pop_video()
        self.assertIsNotNone(result)
        self.assertEqual(result[1], 900)
        self.assertEqual(channel.dropped_video, 1)


if __name__ == "__main__":
    unittest.main()
