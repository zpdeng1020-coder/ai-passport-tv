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

Two mechanisms, because the two platforms answer different questions and only
one of them is a matter of taste.

On POSIX, the launcher holds a pipe open to each child and never writes to it.
A pipe is not a value that can go stale; it is a handle. When the launcher ends
for any reason at all -- including SIGKILL, which no handler can catch -- the
kernel closes it and the child's read returns end-of-file. Nothing needs to
know anything: the child is simply told.

Windows does not arrive at the same answer by the same route. The bundle is
started through a bootloader that runs the real program as its own child, and
that bootloader does not pass stdin through, so the pipe a child would be
reading is not the one the launcher holds. It was measured -- the build passed
on macOS and Linux and failed on Windows -- rather than reasoned about, and the
first version of this file made exactly that mistake.

Windows has a mechanism for this, and it is the right one to use: a job object.
Assign a process to a job and set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and the
operating system terminates every process in it when the last handle to the job
closes -- which happens when the process holding it dies, however it dies. This
is what the flag exists for. It is also not a polling loop and not a heuristic:
the kernel does it.

So `attach()` is what the launcher calls for each child, and `start()` is what a
child calls for itself. On POSIX the first creates a pipe and the second reads
it; on Windows the first creates a job and assigns the child to it, and the
second does nothing at all, because there is nothing for the child to do -- its
exit has already been arranged by someone else.
"""

from __future__ import annotations

import os
import subprocess
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

# How often the POSIX launcher asks whether it has been reparented, and how to
# change it. Not a setting anyone should need: it is a net under a case that
# should not happen, and the interval only decides how long an already-dead
# program takes to notice.
DEFAULT_POLL_SECONDS = 1.0
POLL_ENV = "TV_PARENT_POLL_SECONDS"

_started = False

# The job object, kept alive for as long as this process runs. Letting it be
# collected would close the handle, and closing the handle is what kills the
# processes in it -- including this one. Held in a module global for that
# reason and not for convenience.
_job = None


def _windows_job() -> int | None:
    """A job object that kills its members when this process dies.

    Returns its handle, or None if the platform would not provide one. The
    structures are declared here rather than pulled from a library because
    ctypes is the standard library's way to reach this API, and the three calls
    involved are stable Win32 that has not changed since Windows 7.
    """
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
            handle, job_object_extended_limit_information,
            ctypes.byref(information), ctypes.sizeof(information)):
        kernel32.CloseHandle(handle)
        return None
    return handle


def attach(process: subprocess.Popen) -> None:
    """Arrange for `process` to end when this process does, on any platform.

    POSIX: hand it a pipe this process holds open and never writes to. The
    child reads it in `start()`; the kernel does the rest.

    Windows: assign it to a job object that kills its members when the last
    handle closes. Nothing is asked of the child, and `start()` does nothing
    there -- which is why this half exists at all, since the launcher cannot
    reach into the child to arrange it from that side.

    Failure is silent in both cases, and deliberately: this is a backstop under
    a case that should not happen. A machine where the platform refuses is no
    worse off than before, and turning that into a start-up failure would trade
    a rare problem for a certain one.
    """
    if os.name == "posix":
        # The pipe is what the child watches. Giving it here rather than where
        # the process is started keeps both halves of the arrangement in one
        # file -- the process must be started with stdin=PIPE for this to have
        # anything to hand over.
        process.stdin = process.stdin or subprocess.PIPE
        return

    global _job
    if _job is None:
        _job = _windows_job()
    if _job is None:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).AssignProcessToJobObject(
            _job, int(process._handle))     # type: ignore[attr-defined]
    except Exception:
        # A process already in a job that forbids nesting, or an unexpected
        # handle. Not fatal, and not worth reporting: see above.
        return


def watch_parent() -> bool:
    """End this process when the one that started it does.

    For the launcher, not for its children -- they have `start()` and `attach()`
    below, which are exact in a way this cannot always be. This is the outer
    layer, and it exists because a bundled program is not one process.

    PyInstaller puts a bootloader in front: the executable the user runs unpacks
    the bundle and then runs the real program as its own child. So "the program"
    is two processes, and the one a user or a script kills -- the bootloader --
    is not the one holding the children's job object or watching their pipes.
    Killing the bootloader therefore leaves the launcher orphaned, still running,
    still holding both ports; the smoke test found exactly that on Windows, where
    `terminate()` is `TerminateProcess` and reaches nothing but its target. On
    POSIX the bootloader forwards SIGTERM to its child, which is why the same
    check passed there and hid this.

    Two mechanisms, because the platforms genuinely differ:

    * Windows: open a handle to the parent and wait on it. The handle becomes
      signalled when the process ends, whatever ended it, so this is exact --
      no polling, no interval to choose, and nothing the other process has to
      cooperate with.
    * POSIX: ask whether the parent has changed. There is a better-looking
      answer and it is not one: `waitpid` only works on one's own children, and
      the pipe this module uses elsewhere belongs to the children rather than
      here. `getppid` is documented to return 1 once the parent is gone, and a
      second is far more often than this needs to be right -- it is a case that
      should not happen.

    Returns whether a watch was started, which is only useful to a test.
    """
    if os.name == "nt":
        return _watch_parent_on_windows()
    return _watch_parent_by_repolling()


def _watch_parent_on_windows() -> bool:
    """Wait on a handle to the parent process. Exact and event-driven."""
    import ctypes
    import threading

    synchronize = 0x00100000
    infinite = 0xFFFFFFFF
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(synchronize, False, os.getppid())
    except Exception:
        return False
    if not handle:
        # No rights to the parent, or it is already gone. Nothing to watch;
        # the pipe and job arrangements still cover the children.
        return False

    def watch() -> None:
        kernel32.WaitForSingleObject(handle, infinite)
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    return True


def _watch_parent_by_repolling() -> bool:
    """Notice that this process has been reparented, and leave."""
    import threading
    import time

    original = os.getppid()
    if original <= 1:
        # Already an orphan, so there is nothing to wait for and nothing that
        # would change. Reported rather than treated as an error.
        return False
    interval = os.environ.get(POLL_ENV)
    seconds = float(interval) if interval else DEFAULT_POLL_SECONDS

    def watch() -> None:
        while True:
            time.sleep(seconds)
            if os.getppid() != original:
                os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    return True


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
    if os.name != "posix":
        # On Windows the arrangement was made from the other side, when the
        # launcher put this process in a job object -- there is nothing to
        # watch and nothing to wait for. Doing it here would not work anyway:
        # the bundle is started through a bootloader that does not pass stdin
        # through, so the stream this would read is not the launcher's.
        return False
    if sys.stdin is None:
        # A windowed build, or a process started with the streams closed. The
        # launcher sets both the variable and the pipe, so this is a case worth
        # surviving rather than an impossible one.
        return False
    try:
        descriptor = sys.stdin.fileno()
    except (OSError, ValueError):
        # No file descriptor behind it -- a replaced stream, or a closed one.
        # There is nothing to watch, and nothing to report: this is a backstop.
        return False
    _started = True

    def watch() -> None:
        # The file descriptor, not `sys.stdin.buffer`. Reading through the
        # buffered object takes a lock that Python's shutdown also wants, and a
        # daemon thread holding it when the interpreter finalises is a fatal
        # error -- "_enter_buffered_busy: could not acquire lock ... at
        # interpreter shutdown, possibly due to daemon threads", which turns a
        # clean exit into a crash. Found by running it, not by reading about
        # it. `os.read` is the raw call and takes no such lock; the buffer is
        # never touched, so there is nothing to contend for.
        try:
            while os.read(descriptor, _BUFFER):
                # Anything at all on this pipe would mean someone other than
                # the launcher is using it, which is not a case that exists.
                # Looping rather than treating a byte as a signal keeps the
                # thread's only two outcomes: the parent is gone, or it is not.
                pass
        except OSError:
            # The descriptor was closed under us, which is the parent having
            # gone as much as end-of-file is.
            pass
        # Deliberately abrupt. Everything this program holds is released by
        # exiting -- sockets, ffmpeg children, file handles -- and the ordinary
        # shutdown path is not available here: it is written for a process that
        # still has its parent, and in a bundled build the code it would reach
        # for may be exactly what has been deleted.
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    return True
