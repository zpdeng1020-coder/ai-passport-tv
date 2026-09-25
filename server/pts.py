"""Both streams' own timestamps, out of the one decode that produced them.

The server used to run two ffmpeg processes and count frames on each side: a
picture's content time was `frames_produced / fps` and a sound block's was
`blocks_produced * 40 ms`, with the relationship between the two measured once
from `ffprobe`. Counting is a rate, not a clock. It is right on average and
wrong on every individual item, and it cannot see a gap at all: a decoder that
skips 300 ms of audio still emits one block per 40 ms, so the count closes the
hole and shifts everything after it.

What replaces it is the decoder's own answer. One ffmpeg process reads the
source once and produces both payloads; `showinfo` and `ashowinfo` sit in that
process's filter graph and log each item's `pts_time` as it passes. The
timestamps therefore describe the same decode as the bytes, and they are already
on one timeline, so no offset has to be inferred from start times or launch
times.

**One log line per item, checked by index.** `showinfo` logs a frame before the
muxer writes it, so the timestamp is always available by the time its bytes have
been read. Each line carries its own `n`, and the reader compares that against
the count of items it has read. A mismatch means a line was lost, and a lost
line would silently shift every later item by one; refusing is the only answer
that does not produce a plausible-looking stream with the wrong times in it.

Measured on a live channel (12 s, mono 16 kHz, 12 fps): 144 `showinfo` lines,
`n` contiguous from 0, one per frame written; 300 `ashowinfo` lines for 300
device blocks of 1280 bytes, `n` contiguous from 0. `asetnsamples=n=640` and a
forced mono layout are what make one line equal one device block -- without the
mono conversion a stereo frame carries two blocks and the correspondence is
halved, which is exactly how the first version of this was wrong.
"""

from __future__ import annotations

import collections
import re
import threading

# ffmpeg writes, for each item:
#   [Parsed_showinfo_3 @ 0x...] n:   0 pts:      0 pts_time:0       duration: ...
#   [Parsed_ashowinfo_6 @ 0x...] n:0 pts:0 pts_time:0 fmt:s16 channels:1 ...
# `Parsed_ashowinfo` does not contain `Parsed_showinfo`, so the two are told
# apart by the filter name and not by the fields, which are the same shape.
VIDEO_FILTER = b"Parsed_showinfo"
AUDIO_FILTER = b"Parsed_ashowinfo"
_FIELDS = re.compile(rb"\] n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([-\d.eE+]+)")


def parse(line: bytes) -> tuple[str, int, float] | None:
    """Which stream, which index, what time. None if the line is not one of ours.

    `showinfo` also emits side-data and colour-range lines with the same prefix
    and no `n:`; requiring the full field sequence is what keeps those out.
    """
    if AUDIO_FILTER in line:
        kind = "audio"
    elif VIDEO_FILTER in line:
        kind = "video"
    else:
        return None
    found = _FIELDS.search(line)
    if found is None:
        return None
    return kind, int(found.group(1)), float(found.group(3))


class Misaligned(RuntimeError):
    """The decoder's own index did not match the item that was read.

    Raised rather than repaired: the timestamps after this point are all one
    item out, and a stream stamped with the wrong times still looks like a
    stream, which is the failure mode this whole module exists to avoid.
    """


class Timestamps:
    """The decoder's timestamps, in the order it produced them.

    Indexed, and the index is load-bearing: a reader that has just taken the
    Nth item asks for the Nth timestamp and is told if it does not get it.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._video: collections.deque[tuple[int, float]] = collections.deque()
        self._audio: collections.deque[tuple[int, float]] = collections.deque()
        self._expected = {"video": 0, "audio": 0}
        self.received = {"video": 0, "audio": 0}
        self._closed = False
        # What the decoder said, kept whole: when a session ends badly the only
        # useful artefact is the last thing the instrument reported.
        self.tail: collections.deque[bytes] = collections.deque(maxlen=64)

    def note(self, line: bytes) -> None:
        """Record one logged item. Called from the stderr reader thread."""
        self.tail.append(line)
        found = parse(line)
        if found is None:
            return
        kind, index, seconds = found
        with self._condition:
            queue = self._video if kind == "video" else self._audio
            queue.append((index, seconds))
            self.received[kind] += 1
            self._condition.notify_all()

    def take(self, kind: str, timeout: float = 5.0) -> float | None:
        """The next timestamp for `kind`, in seconds, or None if none came.

        Blocks until the decoder has logged the item the caller has just read.
        It should never actually wait: the filter logs an item before the bytes
        reach the pipe, so by the time the payload has been read the line is
        already here. The timeout is for the case that is not true -- a
        decoder that has stopped -- where waiting for ever would turn a stalled
        source into a stalled server.
        """
        with self._condition:
            queue = self._video if kind == "video" else self._audio
            deadline = _now() + timeout
            while not queue and not self._closed:
                remaining = deadline - _now()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if not queue:
                return None
            index, seconds = queue.popleft()
            expected = self._expected[kind]
            if index != expected:
                raise Misaligned(
                    f"{kind} timestamp index {index} arrived where {expected} "
                    f"was due; a decoder log line was lost and every later "
                    f"timestamp would be one item out")
            self._expected[kind] += 1
            return seconds

    def close(self) -> None:
        """Wake up any threads waiting in take() and mark closed."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def reset(self) -> None:
        """A new decoder starts its own indexing. Nothing carries over."""
        with self._condition:
            self._video.clear()
            self._audio.clear()
            self._expected = {"video": 0, "audio": 0}
            self.received = {"video": 0, "audio": 0}
            self._closed = False


def _now() -> float:
    # time.monotonic, named once so the import list above stays honest about
    # what this module needs: a clock, a condition and a regex.
    import time
    return time.monotonic()


class Reader(threading.Thread):
    """Drain the decoder's stderr into a `Timestamps`.

    A thread of its own and not a later read: ffmpeg blocks when nothing is
    consuming its log, so an undrained stderr is a stalled stream that looks
    exactly like a stalled source.
    """

    def __init__(self, stream, timestamps: Timestamps):
        super().__init__(daemon=True, name="decoder-log")
        self._stream = stream
        self._timestamps = timestamps
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            for line in iter(self._stream.readline, b""):
                self._timestamps.note(line)
        except (OSError, ValueError) as error:
            self.error = error
