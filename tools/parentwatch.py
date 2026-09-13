"""Leaving when the program that started this one is gone.

The launcher runs the media server and the channel page as separate processes,
in a session of their own so that exactly one process decides the order things
stop in. It asks them to stop before it exits, and it does that for every signal
that means "stop" as well as for Ctrl-C. What no signal handler can cover is
being killed outright: SIGKILL, a crash, or on Windows a `TerminateProcess`,
which cannot be intercepted at all. Then the two children outlive their parent
and go on holding ports 8096 and 8097, and the next run reports a busy port with
nothing on screen to connect it to a program the user believes they closed.

Asking the operating system who the parent is does not answer this. It does on
POSIX, where an orphan is reparented and the number changes, and that was the
first attempt. On Windows the parent's id stays what it was when the process
started, for ever, so the check never fires -- and that is not a subtle
difference: the build passed on macOS and Linux and failed on Windows.

So the question is asked of something that cannot lie about it. The launcher
gives each child a stdin pipe and never writes to it. A pipe is not a value
that can be stale; it is a handle, and when the launching process ends for any
reason whatsoever the operating system closes its end, and the child's read
returns end-of-file. That is the whole mechanism, and it is the same on every
platform -- there is nothing here that knows which system it is running on.

It is also why stdin and not something else. It is the one stream a child here
has no use for: both programs write to the terminal and read nothing, and the
ffmpeg they may start is given `DEVNULL` and `-nostdin`, so nothing downstream
consumes what this holds open.

Opt-in through the environment rather than assumed, because the same programs
run without a launcher: someone following the server's README starts the media
server by hand, with a terminal on stdin, and a watchdog there would sit on the
keyboard. Only a child the launcher started reads the variable, because only
the launcher sets it.

Nothing here is written to. The read is the point, and it blocks until the
parent is gone, which is the same as saying it never returns while the program
is being used normally.
"""

from __future__ import annotations

import os
import sys
import threading

# Set by the launcher on the children it starts. Named after this module's job
# rather than after the mechanism, so that changing how the parent's absence is
# detected would not make the name a lie.
WATCH_ENV = "TV_WATCH_PARENT"

# What the watch reads. One byte, because reading more would mean waiting for
# more: the launcher writes nothing, so the only outcomes are "still there" and
# end-of-file, and a read that returned as soon as it had a byte would be a
# byte that never comes.
_BUFFER = 1

_started = False


def start() -> bool:
    """Watch for the launcher's exit on a thread, if this process has one.

    Returns whether a watch was started, which is only useful to a test. Safe
    to call from every entry point: a process that has already started one does
    not start a second, and one the launcher did not start does nothing.

    A daemon thread, so it can never hold up an exit -- this exists to make the
    program stop, and a program that could not exit because of it would be a
    worse bug than the one it fixes.
    """
    global _started
    if _started or not os.environ.get(WATCH_ENV):
        return False
    if sys.stdin is None:
        # A windowed build, or a process started with the streams closed. The
        # launcher sets both the variable and the pipe, so this is a case worth
        # surviving rather than an impossible one.
        return False
    _started = True

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(_BUFFER):
                # Anything at all on this pipe would mean someone other than
                # the launcher is using it, which is not a case that exists.
                # Looping rather than treating a byte as a signal keeps the
                # thread's only two outcomes: the parent is gone, or it is not.
                pass
        except (OSError, ValueError):
            # The stream was closed under us. That is the parent having gone as
            # much as end-of-file is.
            pass
        # Deliberately abrupt. Everything this program holds is released by
        # exiting -- sockets, ffmpeg children, file handles -- and the ordinary
        # shutdown path is not available here: it is written for a process that
        # still has its parent, and in a bundled build the code it would reach
        # for may be exactly what has been deleted.
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    return True
