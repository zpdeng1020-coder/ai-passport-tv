"""Where the data directory is chosen, and what happens when it cannot be.

The choice is the whole content of tools/datadir.py, and getting it wrong is
quiet: the program still runs, and the user's channel list goes somewhere they
will not find it. So each rule is checked here, including the fallback that only
happens on a read-only directory -- which is not something a developer machine
produces by accident, and would therefore never be exercised by hand.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import datadir


class DataDirTests(unittest.TestCase):
    def test_env_override_wins_and_is_created(self):
        """The override is used even when the program directory is writable.

        It exists to let someone say where the data goes, so anything that could
        outrank it would make it unreliable exactly when it is needed.
        """
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "data"
            with mock.patch.dict(os.environ, {datadir.ENV_DATA_DIR: str(target)}):
                self.assertEqual(datadir.data_dir(), target)
            self.assertTrue(target.is_dir(), "the override is created, not just returned")

    def test_env_override_accepts_a_tilde(self):
        """`~/...` is expanded, because a person writing it means their home."""
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ,
                                 {datadir.ENV_DATA_DIR: "~/sub",
                                  "HOME": home}):
                chosen = datadir.data_dir()
            self.assertEqual(chosen, Path(home) / "sub")

    def test_program_directory_is_used_when_writable(self):
        """Rule 2. The data ends up beside the program, where the user can see it."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(datadir, "program_dir", return_value=Path(directory)):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(datadir.ENV_DATA_DIR, None)
                    self.assertEqual(datadir.data_dir(), Path(directory))

    def test_read_only_program_directory_falls_back_to_the_user_directory(self):
        """Rule 3, and the reason the write test is a real write.

        A read-only directory is what a bundled program sees when it is run from
        a macOS disk image or from somewhere under Program Files. Permission bits
        are not consulted, so this asserts the behaviour that matters: the
        fallback is reached, and the data still lands somewhere.
        """
        with tempfile.TemporaryDirectory() as read_only, \
                tempfile.TemporaryDirectory() as elsewhere:
            os.chmod(read_only, 0o500)
            try:
                with mock.patch.object(datadir, "program_dir",
                                       return_value=Path(read_only)):
                    with mock.patch.object(datadir, "user_data_dir",
                                           return_value=Path(elsewhere) / "fallback"):
                        os.environ.pop(datadir.ENV_DATA_DIR, None)
                        chosen = datadir.data_dir()
                self.assertEqual(chosen, Path(elsewhere) / "fallback")
                self.assertTrue(chosen.is_dir())
            finally:
                os.chmod(read_only, 0o700)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unwritable_directory_is_reported_unwritable(self):
        """`_is_writable` asks by writing, so it must say no when writing fails."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            os.chmod(target, 0o500)
            try:
                self.assertFalse(datadir._is_writable(target))
            finally:
                os.chmod(target, 0o700)

    def test_the_write_test_leaves_nothing_behind(self):
        """A probe that stayed would accumulate a file per run."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertTrue(datadir._is_writable(target))
            self.assertEqual(list(target.iterdir()), [])

    def test_channels_file_follows_the_data_directory(self):
        """The channel list is looked up in one place, not two.

        Splitting these is what would let the page write one file while the
        server reads another, which presents as "saving does nothing".
        """
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {datadir.ENV_DATA_DIR: directory}):
                self.assertEqual(datadir.channels_file(),
                                 Path(directory) / "channels.txt")

    def test_frozen_and_checkout_are_distinguished(self):
        """The bundled case is detected from `sys.frozen`, and only from it.

        `_MEIPASS` is also present in a one-file bundle, but it names the
        temporary extraction directory rather than the program, and a directory
        bundle does not have it at all.
        """
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(datadir.is_frozen())
            self.assertEqual(datadir.program_dir(),
                             Path(sys.executable).resolve().parent)
        if hasattr(sys, "frozen"):
            delattr(sys, "frozen")
        self.assertFalse(datadir.is_frozen())

    def test_code_root_is_not_the_data_dir_when_bundled(self):
        """The separation this module exists for, asserted directly.

        A one-file bundle unpacks into a temporary directory and deletes it on
        exit. Writing the channel list there would lose the edit, so the two
        answers must differ -- if they are ever the same value under `frozen`,
        the packaged build is broken in the way this module was written to
        prevent.
        """
        with tempfile.TemporaryDirectory() as extracted, \
                tempfile.TemporaryDirectory() as beside:
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", extracted, create=True), \
                    mock.patch.object(sys, "executable",
                                      str(Path(beside) / "av-server")), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(datadir.ENV_DATA_DIR, None)
                self.assertEqual(datadir.code_root(), Path(extracted))
                # Compared resolved: program_dir() resolves the executable's
                # directory, and on macOS the temporary directory is reached
                # through a symlink (/var against /private/var), so the
                # unresolved spelling of the same path would compare unequal.
                self.assertEqual(datadir.data_dir(), Path(beside).resolve())
                self.assertNotEqual(datadir.code_root(), datadir.data_dir())
        if hasattr(sys, "frozen"):
            delattr(sys, "frozen")

    def test_code_root_from_a_checkout_is_the_repository(self):
        """Unbundled, the code root is the repository, as it always was."""
        self.assertEqual(datadir.code_root(),
                         Path(__file__).resolve().parents[1])

    def test_program_dir_from_a_checkout_is_the_repository(self):
        """Unbundled, the program directory is the checkout.

        This is what keeps `run.sh` working unchanged: the code directory and the
        data directory are the same path there, exactly as they were before this
        module existed.
        """
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(datadir.program_dir(), expected)


if __name__ == "__main__":
    unittest.main()
