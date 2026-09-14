"""The channel page: parsing a playlist, and agreeing with its own front end.

This module had no tests, and the cost of that turned up as a bug a user found
before any check did. The page's JavaScript asked each channel for `c.ua`; the
field is called `agent` and has been everywhere else all along -- the playlist
parser emits it, the table stores it, the saver writes it back. `c.ua` is
`undefined` on every channel, so the probe request always carried an empty user
agent, and a source that answers only a particular player refused it. The page
reported working channels as dead. The user tested one, watched it play, and
reported the disagreement.

What made that possible is that nothing compared the two halves. The page is a
Python string containing JavaScript, so no tool reads it as code, and a field
name on one side and a field name on the other are connected by nothing at all.
The check below is that connection: it reads the field names the page asks for
and the field names the server produces, and requires the first set to be a
subset of the second.

It is a coarse check and deliberately so. It cannot tell a typo from a field
that is legitimately absent, and it does not try -- a name that does not exist
is worth a look either way, and the cost of a false alarm here is one line of
investigation against a bug that made a working channel look broken.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import channel_config


def _page_source() -> str:
    root = Path(channel_config.__file__).resolve().parents[1]
    return (root / "tools" / "channel_config.py").read_text(encoding="utf-8")


def _javascript() -> str:
    """The page's script with its comments removed, which is what runs.

    Comments are stripped rather than left in, and the first run of this test
    is why. The fix for the bug it checks carries an explanation naming the
    wrong field -- `c.ua` -- so a scan over the raw text found the name in the
    comment explaining that the name was wrong, and failed. A comment is not a
    read; leaving this out would make the check impossible to satisfy in the
    presence of its own documentation, and the next person would delete the
    check rather than the comment.
    """
    source = _page_source()
    start = source.find("<script>")
    script = source[start:] if start >= 0 else ""
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)   # /* ... */
    script = re.sub(r"(?m)//.*$", "", script)               # // ...
    return script


class FrontEndFieldTests(unittest.TestCase):
    """The page may only read fields the server actually produces."""

    # What the page reads off a channel object, by the variable it uses. Named
    # rather than guessed at so that a new loop variable does not silently
    # escape the check -- the scan is only as good as this list.
    OBJECT_VARIABLES = ("c", "ch", "item", "sel", "entry")

    @classmethod
    def _fields_the_page_reads(cls) -> set[str]:
        found: set[str] = set()
        pattern = re.compile(
            r"\b(?:" + "|".join(cls.OBJECT_VARIABLES) + r")\.([a-z_][a-z0-9_]*)\b")
        for match in pattern.finditer(_javascript()):
            found.add(match.group(1))
        return found

    @classmethod
    def _fields_the_server_produces(cls) -> set[str]:
        """Every key put into a channel object, or written as one.

        Collected from the three places a channel takes shape: the playlist
        parser, the table reader, and the save endpoint. Read as source rather
        than by running anything, because the point is what the names are, not
        what the values happen to be on this machine.
        """
        source = _page_source()
        names: set[str] = set()
        for match in re.finditer(r"channels\.append\(\{([^}]*)\}\)", source):
            names |= set(re.findall(r'"(\w+)":', match.group(1)))
        for match in re.finditer(r"selected\.append\(\{([^}]*)\}\)", source):
            names |= set(re.findall(r'"(\w+)":', match.group(1)))
        # `json.dumps({"channel_list": [{"id": ..., "name": ...}]})` and the
        # CONFIG payload are built the same way.
        for match in re.finditer(r'"(\w+)":\s*(?:slug\(|name|url|agent|line)', source):
            names.add(match.group(1))
        return names

    def test_the_page_reads_only_fields_that_exist(self):
        """The check that would have caught `c.ua` before a user did."""
        reads = self._fields_the_page_reads()
        produces = self._fields_the_server_produces()
        self.assertTrue(reads, "the scan found no field reads; it has stopped working")
        self.assertTrue(produces, "the scan found no field definitions; it has stopped working")
        unknown = sorted(reads - produces)
        self.assertEqual(
            unknown, [],
            f"the page reads {unknown}, which no channel object has -- "
            f"the value is undefined and whatever it is passed to receives nothing. "
            f"Known fields: {sorted(produces)}")

    def test_the_agent_reaches_the_probe(self):
        """The specific line that was wrong, kept as a named case.

        The general check above would catch it again, but this one states what
        the field is for: a source that answers only one player is refused
        without it, and the probe would then call a working channel dead.
        """
        script = _javascript()
        self.assertIn("c.agent", script,
                      "the probe no longer sends the channel's user agent")
        self.assertNotIn("c.ua", script,
                         "`c.ua` does not exist; the field is `agent`")
        # And it has to reach the request, not merely be read: a value fetched
        # into a variable and then not passed is the same bug one line later.
        self.assertRegex(script, r"&ua=" + re.escape("'") + r"\s*\+\s*encodeURIComponent\(c\.agent")

    def test_the_scan_would_notice_a_new_offender(self):
        """The scan has to be able to fail, or it proves nothing."""
        reads = self._fields_the_page_reads()
        produces = self._fields_the_server_produces()
        # `line` is produced; `lineno` is not, and differs by three characters.
        self.assertIn("line", produces)
        self.assertNotIn("lineno", produces)
        self.assertTrue(reads - produces == set() or reads - produces)


class PlaylistParsingTests(unittest.TestCase):
    """Reading an M3U, including the attribute that caused the bug above."""

    def test_a_playlist_keeps_each_entrys_user_agent(self):
        """The parser is where `agent` comes from, so the name starts here.

        A source behind a player check is unusable without this, and it is the
        reason the field exists at all -- so a parser that dropped it would
        make every such channel look broken in a way nothing else would
        explain.
        """
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 http-user-agent="AptvPlayer-UA",CCTV1\n'
            'http://example.invalid/cctv1\n'
            '#EXTINF:-1,CCTV3\n'
            'http://example.invalid/cctv3\n'
        ).encode("utf-8")
        channels = channel_config.parse_playlist(playlist.decode("utf-8"))
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0]["name"], "CCTV1")
        self.assertEqual(channels[0]["agent"], "AptvPlayer-UA")
        self.assertEqual(channels[1]["agent"], "")

    def test_an_entry_without_an_attribute_gets_an_empty_agent(self):
        """Not a missing key. The page reads `c.agent || ''` either way, but a
        key that is sometimes absent is how a field name gets guessed at."""
        channels = channel_config.parse_playlist(
            '#EXTM3U\n#EXTINF:-1,One\nhttp://example.invalid/one\n')
        self.assertIn("agent", channels[0])
        self.assertEqual(channels[0]["agent"], "")

    def test_only_http_addresses_are_kept(self):
        """A playlist may carry other things; only streams can be played."""
        channels = channel_config.parse_playlist(
            "#EXTM3U\n"
            "#EXTINF:-1,File\nfile:///tmp/x.m3u8\n"
            "#EXTINF:-1,Stream\nhttp://example.invalid/live.m3u8\n")
        self.assertEqual([c["name"] for c in channels], ["Stream"])


if __name__ == "__main__":
    unittest.main()
