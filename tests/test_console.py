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
    """Every script that prints Chinese has to set the encoding first.

    Found by scanning rather than by keeping a list. A list was tried first and
    was wrong within the hour: it named four files, and the fifth -- the smoke
    test runner -- failed on Windows with exactly the error this module exists to
    prevent, because nobody had remembered to add it. A check that has to be
    updated whenever a file is added protects only the files someone thought of.
    """

    # Where runnable scripts live. `server/` has none: it is a package whose
    # modules are imported, and the bundled entry point sets the encoding before
    # any of them is loaded.
    SCRIPT_DIRS = ("tools",)

    @classmethod
    def _scripts_with_chinese_output(cls) -> list[Path]:
        """Scripts that can print Chinese and do not set the encoding.

        A script is included when it contains a Chinese string inside something
        that writes to a stream, and does not import the helper. Both halves are
        needed: plenty of files hold Chinese in comments and docstrings, which
        never reach a console and would be a false alarm.
        """
        import ast

        root = Path(__file__).resolve().parents[1]
        offenders: list[Path] = []
        for directory in cls.SCRIPT_DIRS:
            for path in sorted((root / directory).glob("*.py")):
                source = path.read_text(encoding="utf-8")
                if "use_utf8" in source:
                    continue
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name not in ("print", "write", "writeLine"):
                        continue
                    for argument in ast.walk(node):
                        if (isinstance(argument, ast.Constant)
                                and isinstance(argument.value, str)
                                and any("一" <= ch <= "鿿" for ch in argument.value)):
                            offenders.append(path)
                            break
                    else:
                        continue
                    break
                else:
                    continue
                break
        return offenders

    def test_no_script_prints_chinese_without_setting_the_encoding(self):
        offenders = self._scripts_with_chinese_output()
        self.assertEqual(
            [str(p.name) for p in offenders], [],
            "these print Chinese but never call use_utf8(); on Windows the first "
            "such message raises UnicodeEncodeError and ends the process")

    def test_the_scan_would_notice_a_new_offender(self):
        """The scan has to be able to fail, or it proves nothing.

        A file is written into the scanned directory that prints Chinese and does
        not import the helper, and the scan must report it. Without this, a
        mistake in the scan -- a wrong glob, a condition that never matches --
        would read as "everything is fine".
        """
        root = Path(__file__).resolve().parents[1]
        probe = root / "tools" / "_probe_offender.py"
        probe.write_text('print("中文消息")\n', encoding="utf-8")
        try:
            offenders = self._scripts_with_chinese_output()
        finally:
            probe.unlink()
        self.assertIn("_probe_offender.py", [p.name for p in offenders])

    def test_a_script_that_only_mentions_chinese_in_comments_is_not_flagged(self):
        """Comments do not reach a console, so they are not this check's business.

        Without this the scan would flag most of the repository and be turned
        off, which is worse than not having it.
        """
        root = Path(__file__).resolve().parents[1]
        probe = root / "tools" / "_probe_comments.py"
        probe.write_text('# 这里全是中文注释\n"""还有中文文档。"""\n'
                         'print("ascii only")\n', encoding="utf-8")
        try:
            offenders = self._scripts_with_chinese_output()
        finally:
            probe.unlink()
        self.assertNotIn("_probe_comments.py", [p.name for p in offenders])


class CaptureEncodingTests(unittest.TestCase):
    """Reading a subprocess's output has to state the encoding as well.

    The mirror image of the problem above, and the one that was left. Writing
    Chinese to a cp1252 console fails; so does *reading* it, because
    `subprocess.run(text=True)` decodes with the same system code page. On the
    Windows runner the smoke test raised UnicodeDecodeError inside its own
    reader thread, which left stdout as None and turned the next line into
    "TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'" -- a
    message about the check, saying nothing about the program, which was
    correct.

    Only `text=True` without an encoding is flagged. Stating one, or reading
    bytes and decoding deliberately, are both answers to the problem; the
    second is what the certificate check does, since it reads a line of machine
    output rather than prose.
    """

    # Everything that runs and reads back another process. Both the tools and
    # the smoke test matter: the smoke test is where it actually broke.
    SCANNED_DIRS = ("tools", "tests")

    def _captures_without_an_encoding(self) -> list[str]:
        import ast

        root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for directory in self.SCANNED_DIRS:
            for path in sorted((root / directory).glob("*.py")):
                if path.name == Path(__file__).name:
                    # This file discusses text=True in its own docstrings.
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name not in ("run", "check_output", "Popen", "call"):
                        continue
                    keywords = {k.arg: k.value for k in node.keywords if k.arg}
                    text = keywords.get("text") or keywords.get("universal_newlines")
                    if not (isinstance(text, ast.Constant) and text.value is True):
                        continue
                    if "encoding" not in keywords:
                        offenders.append(f"{path.name}:{node.lineno}")
        return offenders

    def test_no_capture_decodes_with_the_platform_default(self):
        offenders = self._captures_without_an_encoding()
        self.assertEqual(
            offenders, [],
            "these read a child process's output without saying what encoding "
            "it is in; on Windows the platform default cannot represent the "
            "Chinese this program prints, and the check itself dies instead")

    def test_the_scan_would_notice_a_new_offender(self):
        """The scan has to be able to fail, or it proves nothing."""
        root = Path(__file__).resolve().parents[1]
        probe = root / "tools" / "_probe_capture.py"
        probe.write_text(
            "import subprocess\n"
            "subprocess.run(['x'], text=True, capture_output=True)\n",
            encoding="utf-8")
        try:
            offenders = self._captures_without_an_encoding()
        finally:
            probe.unlink()
        self.assertIn("_probe_capture.py:2", offenders)

    def test_stating_the_encoding_is_accepted(self):
        """The fix, and not another way of writing the bug."""
        root = Path(__file__).resolve().parents[1]
        probe = root / "tools" / "_probe_capture_ok.py"
        probe.write_text(
            "import subprocess\n"
            "subprocess.run(['x'], text=True, encoding='utf-8', errors='replace')\n"
            "subprocess.run(['x'], capture_output=True)   # bytes, decoded later\n",
            encoding="utf-8")
        try:
            offenders = self._captures_without_an_encoding()
        finally:
            probe.unlink()
        self.assertNotIn("_probe_capture_ok.py:2", offenders)
        self.assertNotIn("_probe_capture_ok.py:3", offenders)


if __name__ == "__main__":
    unittest.main()
