"""Short file-import checks, not a device performance benchmark."""
import sys
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.media import FPS, import_video, Media, schedule

@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
class VideoImportTests(unittest.TestCase):
    def test_local_video_with_audio_and_silent_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for audio in (False, True):
                source = root / ('sound.mkv' if audio else 'silent.mkv')
                command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                           'testsrc2=s=640x360:r=12:d=2']
                if audio:
                    command += ['-f','lavfi','-i','sine=frequency=440:duration=2']
                command += ['-c:v','ffv1']
                if audio:
                    command += ['-c:a','pcm_s16le','-shortest']
                subprocess.run(command + [str(source)], check=True, timeout=20)
                media = import_video(source, root / ('out-sound' if audio else 'out-silent'), seconds=2)
                loaded = Media.load(media.directory)
                self.assertEqual(loaded.duration_ms, 2000)
                self.assertEqual(len(loaded.audio_at(99)), 640)
                # Last frame of the two-second import: the index depends on the
                # shared frame rate, so derive it rather than writing 23.
                self.assertTrue(loaded.frame_at(2 * FPS - 1).startswith(b'\xff\xd8'))
                manifest = json.loads((media.directory / 'manifest.json').read_text())
                self.assertEqual((manifest['width'], manifest['height'], manifest['frame_count']),
                                 (160, 120, 2 * FPS))
                self.assertLessEqual(max(p.stat().st_size for p in media.directory.glob('frame-*.jpg')), 24576)
                if not audio:
                    self.assertEqual(loaded.audio_at(0), bytes(640))
                events = list(schedule(2100, loaded.duration_ms))
                self.assertEqual(next(e[3] for e in events if e[1]==3 and e[2]==2000), 0)

    def test_centered_letterbox_portrait_and_non_square_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # White source makes untouched black borders independently measurable.
            for index, (size, sar, expected_box) in enumerate((
                    ('640x360', '1', (0, 14, 160, 104)),
                    ('360x640', '1', (46, 0, 114, 120)),
                    ('320x240', '2', (0, 30, 160, 90)),
                    ('640x480', '1', (0, 0, 160, 120)))):
                source = root / f'source-{index}.mkv'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                f'color=c=white:s={size}:r=12:d=1', '-vf', f'setsar={sar}',
                                '-c:v', 'ffv1', str(source)], check=True, timeout=20)
                media = import_video(source, root / f'out-{index}', seconds=1)
                raw = subprocess.run(['ffmpeg', '-v', 'error', '-i',
                                      str(media.directory / 'frame-000000.jpg'),
                                      '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                     capture_output=True, check=True, timeout=20).stdout
                self.assertEqual(len(raw), 160 * 120 * 3)
                lit = [(x, y) for y in range(120) for x in range(160)
                       if min(raw[(y * 160 + x)*3:(y * 160 + x)*3+3]) > 200]
                box = (min(x for x, _ in lit), min(y for _, y in lit),
                       max(x for x, _ in lit)+1, max(y for _, y in lit)+1)
                self.assertEqual(box, expected_box)
                manifest_path = media.directory / 'manifest.json'
                manifest = json.loads(manifest_path.read_text())
                manifest.update(width=320, height=240)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    Media.load(media.directory)

if __name__ == '__main__':
    unittest.main()
