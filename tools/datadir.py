"""Where the running program keeps the things it writes.

Two different questions used to have one answer, and that is what broke the
packaged build. *Where is the code* is settled when the program starts: from a
checkout it is the repository, from a bundled executable it is a temporary
directory that the runtime creates and then deletes. *Where is the data* has to
outlive the process, because `channels.txt` is the user's channel list and is
edited from a web page between runs.

The old code could use one path for both because a checkout is both. Bundling
splits them, and a single `__file__`-derived path then points into the temporary
directory -- so saving on the channel page would either fail or appear to
succeed and lose the edit when the program exited.

So this module answers only the second question, and answers it the same way
whether or not the program is bundled.

The order below is a preference, not a guess:

1. `AV_DATA_DIR`, when set. Tests and anyone who wants to keep the data
   elsewhere need a way to say so without a command-line flag on a program that
   is meant to be double-clicked.

2. Beside the running program, when that directory can be written to. A bundled
   executable is normally downloaded somewhere the user chose, so the data lands
   somewhere they can see -- which makes it easy to back up, and makes deleting
   the program delete its data. The write test is the point: the same directory
   is read-only often enough to matter. A macOS disk image is read-only by
   design and programs are routinely run straight from one, and on Windows the
   program may sit in a directory under `Program Files`.

3. The per-user application directory. Reached only when the program's own
   directory cannot be written to, which for a bundled build means the data goes
   somewhere the user will not stumble across. That is worse for discovering it,
   and it is why the caller prints the path it settled on: a file the user
   cannot find is the same as no file.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

ENV_DATA_DIR = "AV_DATA_DIR"

# Where the data goes when the program's own directory is not writable. One name
# across platforms, spelled the way each platform expects to see it.
APP_DIR_NAME = "ai-passport-tv"

# The file the write test creates. Named after the program so that a probe left
# behind by a crash is recognisable, rather than an unexplained empty file.
_PROBE_PREFIX = ".ai-passport-tv-write-test-"


def is_frozen() -> bool:
    """True when running from a bundled executable rather than a checkout.

    PyInstaller sets `sys.frozen` in the executable it builds. It is read rather
    than `getattr(sys, "_MEIPASS", None)`, because a one-file bundle has both and
    the first is the one that stays true for a directory bundle as well.
    """
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """The directory holding code that is running.

    Not the same as the data directory, and the two must not be confused -- see
    the module docstring. From a checkout this is the repository root, so the
    code and the package `server` next to it stay importable. From a bundled
    executable it is the directory the user put the executable in, which is what
    rule 2 of the data directory is about.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def code_root() -> Path:
    """Where the code and its bundled read-only files are.

    The counterpart to `data_dir`, and deliberately a separate function: these
    are the two answers that used to be one, and keeping them apart is the point
    of this module. Files found here may be read and must never be written.

    A checkout holds them in the repository. A one-file bundle unpacks them into
    a temporary directory that the runtime deletes on exit -- which is why
    anything written here is lost, and why `data_dir` answers separately.
    """
    if is_frozen():
        # Set by PyInstaller for the lifetime of the process; the name is part
        # of its API, not an internal detail we happen to know.
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return Path(bundled)
        # A directory bundle has no extraction step and keeps its files beside
        # the executable instead.
        return program_dir()
    return Path(__file__).resolve().parents[1]


def _is_writable(directory: Path) -> bool:
    """Whether a file can actually be created in `directory`.

    Creating and removing a file rather than asking `os.access`: the permission
    bits can say yes while the filesystem says no, which is exactly the case this
    has to catch. A macOS disk image reports the directory as writable in some
    configurations and then refuses the write; a read-only mount does the same.
    The only reliable question is whether a write succeeds, so that is the
    question asked.

    Errors are swallowed into `False` because every caller treats a failure the
    same way -- by trying the next location -- and the exception itself carries
    nothing a user could act on.
    """
    try:
        with tempfile.NamedTemporaryFile(prefix=_PROBE_PREFIX, dir=directory):
            pass
    except OSError:
        return False
    return True


def user_data_dir() -> Path:
    """The per-user application directory for this platform.

    Not created here. It is a candidate until something is written into it, and
    creating a directory on every run -- including runs that end up using a
    different location -- would leave empty directories behind on machines that
    never needed it.
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        # APPDATA is set in every normal session. Falling back to the home
        # directory keeps this from raising on a stripped-down environment,
        # where an exception here would be a crash before anything is even
        # running.
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        # The XDG variable when the user has set one, the conventional path
        # otherwise. Both are followed rather than only the variable: a session
        # that does not export it is still a normal Linux desktop.
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def data_dir() -> Path:
    """The directory to write to, creating it only if it is the one chosen.

    Returned as a `Path` whether or not it exists yet: the caller may only be
    reading, and an empty directory created for a read that then finds nothing
    is a side effect nobody asked for.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        path = Path(override).expanduser()
        # Created even though the caller may only read, because this one was
        # asked for by name: an override that silently does nothing is worse
        # than a directory appearing.
        path.mkdir(parents=True, exist_ok=True)
        return path

    beside = program_dir()
    if _is_writable(beside):
        return beside

    path = user_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def channels_file() -> Path:
    """The channel table the user edits, wherever the data directory landed."""
    return data_dir() / "channels.txt"


def describe(path: Path) -> str:
    """One line naming where the data lives, for the start-up output.

    Printed because the location varies and the user cannot otherwise tell which
    of several possible places to look in. Where the file is, is not obvious from
    the outside, and a channel list nobody can find is the same as one that was
    never saved.
    """
    return f"数据目录：{path}"
