"""Making the program's own output printable on every platform it runs on.

Every message this program writes for a person is in Chinese. On Windows, the
console's default encoding is the system code page -- cp1252 on an English
installation, cp936 on a Chinese one -- and writing a Chinese character to it
raises UnicodeEncodeError and ends the process. Not one message: the first one.

Found by running the build on a Windows runner, where it died before printing
"正在为 windows/amd64 构建…". The same failure would have met every user of the
Windows download at the first line the program printed, which is the point at
which they have nothing else to go on.

The fix is to set UTF-8 on the streams this program writes to, and to do it
before anything is written. It has to happen at the few places the program
starts rather than at each print, because the first message comes from whichever
of them is running and there is no earlier hook.

Two details that are easy to get wrong:

* `reconfigure` exists on TextIOWrapper, which is what `sys.stdout` is when it
  is a terminal or a pipe. Under pythonw.exe, or when a stream has been replaced
  by something else, it may be absent -- hence the attribute check rather than
  an unguarded call.

* Errors are replaced rather than raised. If a character still cannot be
  encoded, a question mark in one line is a message that can be read; an
  exception is a program that stops. This is text for a person, and there is no
  such thing as a message so important that crashing on it is better.
"""

from __future__ import annotations

import sys

# UTF-8 with a byte-order mark? No: the BOM is a Windows convention for files
# read by other Windows programs, and these are streams read by a terminal. It
# would appear as stray characters ahead of the first line.
ENCODING = "utf-8"


def use_utf8() -> None:
    """Make this program's output printable whatever the console expects.

    Safe to call more than once and safe to call when there is no console at
    all, so each entry point can call it without checking whether another
    already has.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            # A windowed build, or a process started with the streams closed.
            # Nothing to configure, and nothing is written to them either.
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Not a TextIOWrapper -- a test harness may have replaced it with
            # something that has no encoding of its own. Leaving it alone is
            # right: it is not the console that would fail.
            continue
        try:
            reconfigure(encoding=ENCODING, errors="replace")
        except (ValueError, OSError):
            # Already closed, or the encoding cannot be changed on this stream.
            # Failing here would turn a cosmetic problem into a fatal one.
            continue
