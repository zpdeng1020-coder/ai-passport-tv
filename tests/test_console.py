"""Making Chinese messages printable on a console that cannot encode them.

Every message this program prints for a person is Chinese, and on Windows the
console's default encoding is the system code page, which cannot represent it.
Writing the first message raised UnicodeEncodeError and ended the process -- so
the program would die before telling the user anything, including why.

Found on a Windows CI runner. It is tested here because the failure cannot be
reproduced on the machines this is developed on: macOS and Linux consoles are
UTF-8, so the same code works and looks correct there.
"""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import console


class StreamTests(unittest.TestCase):
    def test_a_cp1252_stream_is_switched_to_utf8(self):
        """The exact configuration Windows starts with.

        cp1252 is what an English Windows console reports, and it is the
        encoding that failed. Constructing one here reproduces the failure
        without needing Windows: writing Chinese to it raises.
        """
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        # The failure being fixed, asserted so this test cannot pass vacuously
        # if the codec is ever made lenient.
        with self.assertRaises(UnicodeEncodeError):
            stream.write("数据目录")
            stream.flush()

        with mock.patch.object(sys, "stdout", stream), \
                mock.patch.object(sys, "stderr", io.TextIOWrapper(io.BytesIO(),
                                                                  encoding="cp1252")):
            console.use_utf8()

        stream.write("数据目录")
        stream.flush()
        self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")

    def test_unencodable_characters_replace_rather_than_raise(self):
        """A question mark in a line beats an exception that ends the program.

        This is not hypothetical for this program: it prints channel names, and
        those come from a playlist maintained by other people.
        """
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(sys, "stdout", stream), \
                mock.patch.object(sys, "stderr", io.TextIOWrapper(io.BytesIO(),
                                                                  encoding="cp1252")):
            console.use_utf8()
        self.assertEqual(stream.errors, "replace")

    def test_a_stream_without_reconfigure_is_left_alone(self):
        """Under pythonw.exe there is no console, and there may be no stream.

        Also covers a test harness replacing stdout with something of its own:
        an object with no `reconfigure` is not a wrapper around a console, so it
        is not what would fail, and touching it would be the bug.
        """
        class Bare:
            def write(self, text):
                return len(text)

        bare = Bare()
        with mock.patch.object(sys, "stdout", bare), \
                mock.patch.object(sys, "stderr", bare):
            console.use_utf8()      # must not raise
        self.assertFalse(hasattr(bare, "encoding"))

    def test_no_streams_at_all_is_not_an_error(self):
        """A windowed build has None for both, and still has to start."""
        with mock.patch.object(sys, "stdout", None), \
                mock.patch.object(sys, "stderr", None):
            console.use_utf8()

    def test_it_can_be_called_twice(self):
        """Each entry point calls it without knowing whether another has.

        The bundled build's entry point calls it, and then the launcher it
        imports calls it again. Making the second call fail would mean the two
        had to agree about which one runs first.
        """
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(sys, "stdout", stream), \
                mock.patch.object(sys, "stderr", io.TextIOWrapper(io.BytesIO(),
                                                                  encoding="cp1252")):
            console.use_utf8()
            console.use_utf8()
        stream.write("仍然可以写中文")
        stream.flush()

    def test_a_closed_stream_is_not_fatal(self):
        """Reconfiguring a closed stream raises; that must not end the program.

        The message this protects is cosmetic. Failing to set the encoding on a
        stream nobody can write to anyway is not worth stopping for.
        """
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        stream.close()
        with mock.patch.object(sys, "stdout", stream), \
                mock.patch.object(sys, "stderr", None):
            console.use_utf8()      # must not raise


class EntryPointTests(unittest.TestCase):
    """Every program that prints Chinese has to call this first.

    Checked by reading the source, because the failure only appears on Windows:
    a missing call is invisible on the machines this is developed on. All three
    entry points print Chinese before doing anything else.
    """

    ENTRIES = ("tools/packaged_entry.py", "tools/launch.py",
               "tools/channel_config.py", "tools/build_server.py")

    def test_every_entry_point_switches_to_utf8(self):
        root = Path(__file__).resolve().parents[1]
        for relative in self.ENTRIES:
            with self.subTest(entry=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertIn("use_utf8()", source,
                              f"{relative} prints Chinese without setting the encoding")

    def test_the_call_comes_before_anything_is_printed(self):
        """After the imports, but ahead of the first message.

        Position matters and is easy to lose in a refactor: a call placed after
        the first print does not protect that print.
        """
        root = Path(__file__).resolve().parents[1]
        for relative in self.ENTRIES:
            with self.subTest(entry=relative):
                source = (root / relative).read_text(encoding="utf-8")
                call = source.index("use_utf8()")
                # The first string literal that looks like a message to a person,
                # rather than the module docstring or a help= argument.
                for marker in ("print(", "sys.stdout.write("):
                    at = source.find(marker)
                    if at != -1:
                        self.assertLess(call, at,
                                        f"{relative}: use_utf8() comes after the first {marker}")


if __name__ == "__main__":
    unittest.main()
