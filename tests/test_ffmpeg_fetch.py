"""Choosing, verifying and unpacking the fetched ffmpeg.

No network here. What is tested is everything around the download -- which wheel
a machine gets, when the cache is used instead, what happens when a hash does not
match, and what comes out of the archive. The download itself is one call to
urllib and is exercised for real by the manual run recorded in the changelog.

The hash check matters most. This module downloads a program and the server then
runs it, so a mismatch has to stop everything rather than warn -- and that is
only true if it is tested where the mismatch can be produced on purpose.
"""

import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import ffmpeg_fetch


def make_wheel(binary_name: str = "ffmpeg-macos-aarch64-v7.1",
               contents: bytes = b"\x7fELF fake") -> bytes:
    """A wheel shaped like the real ones, small enough to build in a test.

    Same layout the index publishes: the binary under `imageio_ffmpeg/binaries/`
    beside a README and an `__init__.py` that must not be mistaken for it.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("imageio_ffmpeg/binaries/README.md", "Exes are dropped here.")
        archive.writestr("imageio_ffmpeg/binaries/__init__.py", "")
        archive.writestr(f"imageio_ffmpeg/binaries/{binary_name}", contents)
        archive.writestr("imageio_ffmpeg/__init__.py", "")
    return buffer.getvalue()


class PlatformChoiceTests(unittest.TestCase):
    def test_each_supported_platform_gets_its_own_wheel(self):
        """The platforms this project claims to support all resolve."""
        for system, machine in [("Darwin", "arm64"), ("Darwin", "x86_64"),
                                ("Linux", "aarch64"), ("Linux", "x86_64"),
                                ("Windows", "AMD64"), ("Windows", "x86"),
                                ("Windows", "ARM64")]:
            with self.subTest(system=system, machine=machine):
                self.assertIsNotNone(ffmpeg_fetch.wheel_for(system, machine))

    def test_an_unknown_platform_resolves_to_nothing(self):
        """Not covered is reported as such, not as a failed download.

        The two need different advice: one is worth retrying, the other never
        will be.
        """
        self.assertIsNone(ffmpeg_fetch.wheel_for("Plan9", "mips"))
        self.assertIsNone(ffmpeg_fetch.wheel_for("Darwin", "ppc"))

    def test_every_platform_wheel_is_pinned(self):
        """A platform that resolves but has no hash would download unverified.

        The lookup and the pinned hashes are separate tables, so this checks they
        have not drifted apart -- an omission there silently disables the check
        that makes the download safe.
        """
        for wheel in ffmpeg_fetch.PLATFORM_WHEELS.values():
            with self.subTest(wheel=wheel):
                self.assertIn(wheel, ffmpeg_fetch.WHEELS)
                digest, size = ffmpeg_fetch.WHEELS[wheel]
                self.assertEqual(len(digest), 64)
                self.assertGreater(size, 0)

    def test_windows_on_arm_borrows_the_64_bit_build(self):
        """ARM Windows is served the Intel build, which it runs emulated.

        Pinned as a test because the reason is in a comment and comments drift:
        the alternative is a 32-bit build that also runs but does worse.
        """
        arm = ffmpeg_fetch.wheel_for("Windows", "ARM64")
        self.assertIn("win_amd64", arm)


class CacheTests(unittest.TestCase):
    def test_the_cached_copy_is_used_without_downloading(self):
        """The second run must not touch the network.

        Started by double-clicking, often on a laptop that is not online -- a
        cache that is only consulted after a failed download would make the
        second run fail on a train.
        """
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            cached = ffmpeg_fetch.cached_path(data)
            cached.write_bytes(b"\x7fELF")
            with mock.patch.object(ffmpeg_fetch, "fetch") as download:
                self.assertEqual(ffmpeg_fetch.ensure(data), cached)
            download.assert_not_called()

    def test_a_missing_cache_downloads(self):
        """The first run does download, so the test above is not vacuous."""
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with mock.patch.object(ffmpeg_fetch, "fetch",
                                   return_value=Path("/fetched")) as download:
                self.assertEqual(ffmpeg_fetch.ensure(data), Path("/fetched"))
            download.assert_called_once()

    def test_an_empty_file_is_not_a_cache_hit(self):
        """A zero-byte leftover is not ffmpeg, whatever its name says.

        A download killed mid-write once left a file here; treating it as present
        would turn a one-off interruption into a permanent failure that no amount
        of retrying repairs.
        """
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            ffmpeg_fetch.cached_path(data).write_bytes(b"")
            with mock.patch.object(ffmpeg_fetch, "fetch",
                                   return_value=Path("/fetched")) as download:
                ffmpeg_fetch.ensure(data)
            download.assert_called_once()

    def test_the_cache_name_carries_the_version(self):
        """Bumping VERSION must not find the old file still sitting there."""
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with mock.patch.object(ffmpeg_fetch, "VERSION", "9.9.9"):
                self.assertIn("9.9.9", ffmpeg_fetch.cached_path(data).name)


class VerificationTests(unittest.TestCase):
    def _fetch_with(self, blob: bytes, wheel: str = "imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl"):
        """Run fetch() against a stubbed network, returning the data directory."""
        directory = tempfile.mkdtemp()
        with mock.patch.object(ffmpeg_fetch, "wheel_for", return_value=wheel), \
                mock.patch.object(ffmpeg_fetch, "_download_url",
                                  return_value="https://example.invalid/x.whl"), \
                mock.patch.object(ffmpeg_fetch, "_download", return_value=blob):
            return directory, ffmpeg_fetch.fetch(Path(directory))

    def test_a_matching_hash_is_unpacked_and_made_executable(self):
        """The happy path, end to end through the real extractor."""
        blob = make_wheel(contents=b"\x7fELF real")
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              (digest, len(blob))}):
            directory, path = self._fetch_with(blob)
        self.assertEqual(path.read_bytes(), b"\x7fELF real")
        if os.name == "posix":
            self.assertTrue(path.stat().st_mode & 0o111,
                            "the file has to be executable or it cannot be started")

    def test_a_wrong_hash_stops_everything(self):
        """And writes nothing: a file that is about to be run is not a warning.

        This is the one check standing between a tampered index and arbitrary
        code running on the user's machine.
        """
        blob = make_wheel()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              ("0" * 64, len(blob))}):
            directory, _ = ("", None)
            with tempfile.TemporaryDirectory() as data:
                with mock.patch.object(ffmpeg_fetch, "wheel_for",
                                       return_value="imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl"), \
                        mock.patch.object(ffmpeg_fetch, "_download_url",
                                          return_value="https://example.invalid/x.whl"), \
                        mock.patch.object(ffmpeg_fetch, "_download",
                                          return_value=blob):
                    with self.assertRaises(ffmpeg_fetch.FetchError) as caught:
                        ffmpeg_fetch.fetch(Path(data))
                self.assertIn("校验值不符", str(caught.exception))
                self.assertEqual(list(Path(data).iterdir()), [],
                                 "nothing may be left behind when the hash is wrong")

    def test_an_archive_with_no_binary_is_refused(self):
        """A well-formed wheel of the wrong shape is caught, not unpacked blindly."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("imageio_ffmpeg/binaries/README.md", "nothing here")
        blob = buffer.getvalue()
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              (digest, len(blob))}):
            with self.assertRaises(ffmpeg_fetch.FetchError):
                self._fetch_with(blob)

    def test_an_archive_with_several_binaries_is_refused(self):
        """More than one is refused too, not resolved by picking the first.

        The extractor finds the binary by looking rather than by name, which
        works only while there is exactly one. A release that shipped a second
        file -- a 32-bit build alongside the 64-bit one, say -- would otherwise
        be satisfied by whichever name sorted first, and which one that is comes
        from the archive rather than from anything checked here. Both directions
        are asserted: refusing only the empty case would leave the check passing
        for the wrong reason.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("imageio_ffmpeg/binaries/README.md", "two of them")
            archive.writestr("imageio_ffmpeg/binaries/ffmpeg-aarch64-v7.1", b"one")
            archive.writestr("imageio_ffmpeg/binaries/ffmpeg-x86_64-v7.1", b"two")
        blob = buffer.getvalue()
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              (digest, len(blob))}):
            with self.assertRaises(ffmpeg_fetch.FetchError) as caught:
                self._fetch_with(blob)
            self.assertIn("数量异常", str(caught.exception))

    def test_a_download_that_is_not_a_zip_is_refused(self):
        """Truncated or substituted content fails with a message, not a traceback."""
        blob = b"this is not a zip file"
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              (digest, len(blob))}):
            with self.assertRaises(ffmpeg_fetch.FetchError) as caught:
                self._fetch_with(blob)
            self.assertIn("压缩包", str(caught.exception))

    def test_an_interrupted_extraction_leaves_no_partial_file(self):
        """The `.part` file is a name the next run will never mistake for ffmpeg.

        Checked by looking at what a failed write leaves behind, since the
        alternative -- a half-written file under the real name -- would be
        accepted as a cache hit on the next run and never repaired.
        """
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            destination = ffmpeg_fetch.cached_path(data)
            blob = make_wheel()
            with mock.patch("shutil.copyfileobj", side_effect=OSError("disk full")):
                with self.assertRaises(ffmpeg_fetch.FetchError):
                    ffmpeg_fetch._extract(blob, destination)
            self.assertFalse(destination.exists())
            self.assertEqual([p.name for p in data.iterdir()], [],
                             "the partial file is cleaned up as well")

    def test_the_reader_is_told_the_size_before_a_long_download(self):
        """20-31 MB with no warning reads as a hang."""
        seen = []
        blob = make_wheel()
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch.dict(ffmpeg_fetch.WHEELS,
                             {"imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl":
                              (digest, 12345678)}):
            with tempfile.TemporaryDirectory() as data:
                with mock.patch.object(ffmpeg_fetch, "wheel_for",
                                       return_value="imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl"), \
                        mock.patch.object(ffmpeg_fetch, "_download_url",
                                          return_value="https://example.invalid/x.whl"), \
                        mock.patch.object(ffmpeg_fetch, "_download", return_value=blob):
                    ffmpeg_fetch.fetch(Path(data), on_announce=seen.append)
        self.assertEqual(seen, [12345678])

    def test_an_unwritable_data_directory_is_reported(self):
        """mkdir fails on a read-only location; that has to reach the reader."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested"
            with mock.patch.object(ffmpeg_fetch, "wheel_for",
                                   return_value="imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl"), \
                    mock.patch.object(Path, "mkdir",
                                      side_effect=PermissionError("read-only")):
                with self.assertRaises(ffmpeg_fetch.FetchError) as caught:
                    ffmpeg_fetch.fetch(target)
            self.assertIn("AV_DATA_DIR", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
