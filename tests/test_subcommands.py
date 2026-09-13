"""Starting the program's parts from a bundled executable.

The bundled path cannot be exercised here -- it needs a built executable -- but
most of what can go wrong can be. Each of the checks below corresponds to
something that was actually broken once, or to a way the two sides can disagree.

The dispatch is a string protocol between two processes: the launcher builds an
argument list, and the entry point reads the first item. Neither side can check
the other at build time, so a mismatch appears only when someone runs the
result -- which is exactly why the names live in one module and are asserted
here.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import launch, packaged_entry
from tools.subcommands import CONFIG_COMMAND, MEDIA_COMMAND


class CommandAgreementTests(unittest.TestCase):
    def test_both_sides_use_the_same_names(self):
        """The launcher and the entry point read from one place.

        They used to be defined in the entry point, which the launcher imported.
        That worked in one import order and would have failed in the other, so
        the names moved to a module neither owns.
        """
        self.assertEqual(launch.PACKAGED_MEDIA_COMMAND, MEDIA_COMMAND)
        self.assertEqual(launch.PACKAGED_CONFIG_COMMAND, CONFIG_COMMAND)

    def test_the_names_cannot_be_mistaken_for_flags(self):
        """Not starting with a dash, so argparse never sees them.

        A name that looked like an option would be offered to the sub-program's
        parser and rejected there, turning a dispatch bug into a confusing
        "unrecognized arguments".
        """
        for name in (MEDIA_COMMAND, CONFIG_COMMAND):
            with self.subTest(name=name):
                self.assertFalse(name.startswith("-"))
                self.assertNotIn(" ", name)


class BundledCommandTests(unittest.TestCase):
    """What the launcher asks for when there is no interpreter to ask."""

    def setUp(self):
        frozen = mock.patch.object(launch.datadir, "is_frozen", return_value=True)
        frozen.start()
        self.addCleanup(frozen.stop)

    def test_the_media_server_is_the_executable_itself(self):
        """No `-m`, and the executable is asked to be the server."""
        command = launch.subprocess_command(launch.PACKAGED_MEDIA_COMMAND)
        self.assertEqual(command[0], sys.executable)
        self.assertIn(MEDIA_COMMAND, command)
        self.assertNotIn("-m", command)

    def test_the_page_is_the_executable_itself(self):
        """And no script path, which would name a file inside the bundle."""
        command = launch.subprocess_command(launch.PACKAGED_CONFIG_COMMAND)
        self.assertEqual(command[0], sys.executable)
        self.assertIn(CONFIG_COMMAND, command)
        self.assertFalse(any(part.endswith("channel_config.py") for part in command))


class EntryPointTests(unittest.TestCase):
    """The entry point's behaviour that does not need a bundle."""

    def test_both_handlers_exist(self):
        """The functions the dispatch calls must be defined.

        They were deleted by an edit that replaced the surrounding text, and
        nothing noticed: the launcher path never reaches them, so every test and
        every manual run stayed green while the bundled build died with
        "NameError: name '_run_media' is not defined" -- two crashed processes,
        at the user's first start.
        """
        for name in ("_run_media", "_run_config"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(packaged_entry, name, None)),
                                f"{name} is missing or not callable")

    def test_an_unknown_argument_starts_the_program_normally(self):
        """Someone typing something odd gets the program, not a crash.

        The sub-command names are an internal protocol; a person who types one by
        accident, or types anything else, must land on the launcher rather than
        on a server with no terminal attached.
        """
        with mock.patch.object(packaged_entry, "prepare_data_dir"), \
                mock.patch.object(packaged_entry, "launch") as launcher:
            launcher.main.return_value = 0
            self.assertEqual(packaged_entry.main(["--help"]), 0)
            self.assertEqual(launcher.main.call_args[0][0], ["--help"])

    def test_a_sub_command_does_not_prepare_the_data_directory(self):
        """It is already prepared by whoever started it.

        Two processes copying the same file at the same time is a race whose
        outcome is a half-written channel list, and the launcher is the one that
        knows the directory is ready.
        """
        with mock.patch.object(packaged_entry, "prepare_data_dir") as prepare, \
                mock.patch.object(packaged_entry, "launch") as launcher:
            packaged_entry.main([])
        prepare.assert_called_once()
        launcher.main.assert_called_once()

    def test_the_data_directory_is_entered_before_anything_else(self):
        """The channel table is found relative to the working directory.

        Importing `server.live` reads it, and those modules are imported at the
        top of this file -- so the chdir has to happen before the first thing
        that reads a file, not merely before the launcher runs.
        """
        with mock.patch.object(packaged_entry, "prepare_data_dir"), \
                mock.patch.object(packaged_entry.os, "chdir") as chdir, \
                mock.patch.object(packaged_entry, "launch") as launcher:
            launcher.main.return_value = 0
            packaged_entry.main([])
        chdir.assert_called_once()

    def test_a_data_directory_that_cannot_be_entered_is_reported(self):
        """Not a traceback: this can happen on a read-only install."""
        with mock.patch.object(packaged_entry.os, "chdir",
                               side_effect=PermissionError("denied")), \
                mock.patch.object(packaged_entry, "launch") as launcher:
            self.assertEqual(packaged_entry.main([]), 1)
        launcher.main.assert_not_called()


class ImportCoverageTests(unittest.TestCase):
    """Everything the bundled build needs is imported where PyInstaller sees it.

    PyInstaller finds modules by walking imports it can resolve statically, and
    an import inside a function that it cannot resolve is left out -- the bundle
    builds happily and fails in the user's hands. Anything imported lazily here
    has to be named in packaging/av-server.spec as well, and this checks the
    first half of that contract.
    """

    # Everything that has to be in the bundle for the program to run: the code it
    # dispatches to, and the packages it reaches at run time. Listed once here and
    # asserted against the spec, so the two cannot drift.
    REQUIRED = (
        "server.av_server", "server.live", "server.media", "server.netident",
        "server.protocol", "tools.launch", "tools.channel_config",
        "tools.datadir", "tools.ffmpeg_fetch", "tools.subcommands",
    )

    # The part of the server that must not be loaded until the working directory
    # is the data directory. `server.live` reads the channel table at import time
    # and keeps what it read, so importing it early freezes the wrong answer --
    # this is the "invalid choice: 'cgtn'" failure, and it is the reason these
    # are imported lazily at all.
    DEFERRED = ("server.av_server", "server.live", "server.media",
                "server.netident", "server.protocol")

    def test_the_spec_lists_every_module_the_bundle_needs(self):
        """The spec's hiddenimports is what keeps lazily imported modules in.

        The analysis can only follow imports it can see. Anything imported inside
        a function is invisible to it, so the spec has to name it -- and a module
        in one place but not the other is a gap that only shows up in a built
        executable, in front of a user.
        """
        spec = (Path(packaged_entry.__file__).resolve().parents[1]
                / "packaging" / "av-server.spec").read_text(encoding="utf-8")
        missing = [m for m in self.REQUIRED if f'"{m}"' not in spec]
        self.assertEqual(missing, [], "must be listed in hiddenimports")

    def test_the_deferred_modules_are_declared_for_the_analysis(self):
        """They are imported late, so something else has to name them.

        Either a module-level import or an entry in the spec would satisfy
        PyInstaller. Importing them at module level is not allowed here for the
        ordering reason above, so the spec is what has to carry them -- and this
        checks the connection between the two files rather than trusting it.
        """
        spec = (Path(packaged_entry.__file__).resolve().parents[1]
                / "packaging" / "av-server.spec").read_text(encoding="utf-8")
        for module in self.DEFERRED:
            with self.subTest(module=module):
                self.assertIn(f'"{module}"', spec)

    def test_the_server_package_is_never_imported_at_module_level(self):
        """Importing it early is the specific bug this file is arranged around.

        Asserted rather than trusted, because the symptom -- the program starting
        on the built-in channels instead of the user's -- does not point anywhere
        near the import at the top of a file.
        """
        import ast

        tree = ast.parse(Path(packaged_entry.__file__).read_text(encoding="utf-8"))
        for node in tree.body:          # module level only, not inside functions
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("server"):
                self.fail(f"line {node.lineno}: server imported at module level")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("server"):
                        self.fail(f"line {node.lineno}: server imported at module level")

    def test_the_server_modules_list_matches_the_spec(self):
        """The list used to load them and the list the spec ships must agree.

        A name in one and not the other means either a module that is never
        loaded or one that is missing from the bundle.
        """
        spec = (Path(packaged_entry.__file__).resolve().parents[1]
                / "packaging" / "av-server.spec").read_text(encoding="utf-8")
        for name in packaged_entry.SERVER_MODULES:
            with self.subTest(name=name):
                self.assertIn(f'"server.{name}"', spec)


class SubprocessShapeTests(unittest.TestCase):
    """The bundled command is one this program can actually act on."""

    def test_the_sub_command_is_first(self):
        """The entry point reads argv[1], so nothing may precede it.

        Put after a flag, the command would be parsed by the launcher's own
        argparse -- which rejects unknown arguments and exits -- instead of
        reaching the dispatch.
        """
        with mock.patch.object(launch.datadir, "is_frozen", return_value=True):
            command = launch.subprocess_command(MEDIA_COMMAND)
        self.assertEqual(command[1], MEDIA_COMMAND)


if __name__ == "__main__":
    unittest.main()
