"""Short file-import checks, not a device performance benchmark."""
import sys
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.media import (AUDIO_BYTES, AUDIO_CHUNK_MS, FPS, HEIGHT, Media,
                          WIDTH, import_video, schedule)
from server import frames as format


def frame_rgb(media, index):
    """One stored frame as RGB triples, through the clip's own palette.

    The frames on disk are indices, so the only way to ask a question about the
    picture -- is this pixel white, where does the letterbox end -- is to look
    each index up in the palette that was sent with it. Reading them any other
    way would test a re-derivation of the colours rather than the colours the
    device will actually show.
    """
    palette = media.palette
    raw = (media.directory / f'frame-{index:06d}.idx').read_bytes()
    colours = []
    for value in raw:
        pair = palette[2 * value:2 * value + 2]
        rgb565 = int.from_bytes(pair, 'big')
        r5, g6, b5 = (rgb565 >> 11) & 0x1F, (rgb565 >> 5) & 0x3F, rgb565 & 0x1F
        colours.append(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))
    return colours


def lit_box(colours):
    """The tightest box containing every near-white pixel, as (x0, y0, x1, y1)."""
    lit = [(i % WIDTH, i // WIDTH) for i, c in enumerate(colours) if min(c) > 200]
    return (min(x for x, _ in lit), min(y for _, y in lit),
            max(x for x, _ in lit) + 1, max(y for _, y in lit) + 1)


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
                # The last chunk of the clip, named rather than written out:
                # two seconds holds fifty chunks at forty milliseconds and a
                # hundred at twenty, so a hard-coded index is a test of the
                # chunk size wearing a test of the audio's disguise.
                last_audio = loaded.duration_ms // AUDIO_CHUNK_MS - 1
                self.assertEqual(len(loaded.audio_at(last_audio)), AUDIO_BYTES)
                # A frame is exactly one byte a pixel here: there is no marker
                # to look for and no encoder in between, which is the whole
                # reason this format was chosen. The last frame of the two
                # seconds is named from the shared frame rate rather than
                # written out, so changing FPS does not silently test nothing.
                packets = loaded.frame_at(2 * FPS - 1)
                stripes = [s for payload in packets for s in format.unpack(payload)]
                self.assertEqual(len(stripes), format.STRIPES)
                self.assertTrue(all(len(s) == format.STRIPE_PIXELS for s in stripes))
                manifest = json.loads((media.directory / 'manifest.json').read_text())
                self.assertEqual((manifest['width'], manifest['height'], manifest['frame_count']),
                                 (WIDTH, HEIGHT, 2 * FPS))
                self.assertEqual(
                    max(p.stat().st_size for p in media.directory.glob('frame-*.idx')),
                    format.FRAME_PIXELS)
                # The clip carries its own palette, and the device is sent these
                # exact bytes: a clip indexed with colours the device was never
                # told about renders as noise, not as a slightly wrong picture.
                self.assertEqual(len(loaded.palette), format.PALETTE_BYTES)
                if not audio:
                    self.assertEqual(loaded.audio_at(0), bytes(AUDIO_BYTES))
                events = list(schedule(2100, loaded.duration_ms))
                self.assertEqual(next(e[3] for e in events if e[1]==3 and e[2]==2000), 0)

    def test_centered_letterbox_portrait_and_non_square_pixels(self):
        """The stored frame must be the picture ffmpeg was asked for.

        The expected box is measured, not written down: ffmpeg is run over the
        same source with the same geometry and no palette in the path, and the
        frame that comes out of the import is compared against it. A written
        number would pin the geometry to whatever it happened to be when the
        test was first run, and would keep passing while both the filter chain
        and the expectation drifted together.

        The anamorphic case is the one that earns its keep: it is where the
        palette sampler and the picture reader disagreed, and where the bars
        above and below a letterboxed picture were mapped through a palette
        that had been sampled from a different geometry and therefore had no
        black in it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (size, sar) in enumerate((('640x360', '1'), ('360x640', '1'),
                                                 ('320x240', '2'), ('640x480', '1'))):
                source = root / f'source-{index}.mkv'
                # White source makes untouched black borders independently measurable.
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                f'color=c=white:s={size}:r=12:d=1', '-vf', f'setsar={sar}',
                                '-c:v', 'ffv1', str(source)], check=True, timeout=20)
                reference = subprocess.run(
                    ['ffmpeg', '-v', 'error', '-i', str(source), '-vf', f'fps={FPS},{format.FIT}',
                     '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    capture_output=True, check=True, timeout=20).stdout
                expected = lit_box(
                    [tuple(reference[3 * i:3 * i + 3]) for i in range(WIDTH * HEIGHT)])
                media = import_video(source, root / f'out-{index}', seconds=1)
                self.assertEqual(lit_box(frame_rgb(media, 0)), expected,
                                 f"{size} sar={sar}")
                manifest_path = media.directory / 'manifest.json'
                manifest = json.loads(manifest_path.read_text())
                manifest.update(width=160, height=120)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    Media.load(media.directory)


if __name__ == '__main__':
    unittest.main()
