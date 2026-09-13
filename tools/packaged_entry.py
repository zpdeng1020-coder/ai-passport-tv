"""The entry point of the bundled executable.

Bundling turns three programs into one file, and something has to decide which
of them to be when it starts. That is all this module does.

The reason it is needed at all: the launcher starts the media server and the
channel page as separate processes, both by asking Python to run something else.
`sys.executable -m server.tv_server` needs a Python installation and a package on
the search path; a script path needs that script to exist on disk. Inside a
bundled executable neither is true -- `sys.executable` is the executable itself,
and the source it would point at is unpacked into a temporary directory that no
longer exists by the time anyone looks. The sub-programs have to be reachable
through the executable itself, which means it has to know what it was asked to
be.

The dispatch is by the first argument, and the names are deliberately not things
a person would type: they are an internal protocol between two processes, not a
command-line interface, and a user who typed one by accident should get the
normal behaviour rather than a server with no terminal attached. Nothing here is
documented for users and nothing should depend on it staying the same.

Everything is imported at the top rather than inside the branches. PyInstaller
decides what to include by walking imports it can see, and an import inside a
function that it cannot resolve statically is left out of the bundle -- the
failure then appears at run time, in the user's hands, as "No module named
'server'". Importing first is what makes the analysis find them.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# The frozen executable has no useful sys.path: the modules it needs are inside
# it. Adding the code root only matters when this file is run from a checkout,
# where it makes `from tools import ...` and `from server import ...` resolve the
# same way they do for tools/launch.py.
_CODE_ROOT = Path(__file__).resolve().parents[1]
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))

# Before anything is printed. On Windows the console's default encoding cannot
# represent Chinese, so the first message this program writes would end it with
# a UnicodeEncodeError -- every message it writes is in Chinese. See
# tools/console.py.
from tools.console import use_utf8  # noqa: E402  (needs the path above)

use_utf8()

# Imported for PyInstaller's analysis as much as for use: it walks imports it can
# see, and one it cannot resolve is left out of the bundle.
#
# But not all of them can be imported at this point, and that is not a detail.
# `server.live` reads the channel table *when it is imported* and keeps what it
# found. Importing it here would read the table before this program has decided
# where its data directory is -- with the working directory still wherever the
# user started it, usually with no channel file at all -- and the built-in four
# channels would be what everything downstream saw. That is not a theory: it
# produced a start-up that picked "cgtn" and then handed it to a media server
# that had read the real table, which answered "invalid choice: 'cgtn'" and
# exited, leaving nothing listening on the port.
#
# So the order is: chdir first, then import. The spec lists everything, which is
# what keeps the lazily imported modules in the bundle.
from tools import channel_config, datadir, ffmpeg_fetch, launch  # noqa: E402,F401
from tools.subcommands import CONFIG_COMMAND, MEDIA_COMMAND  # noqa: E402

# Named in packaging/tv-server.spec's hiddenimports, and imported here only as
# the names are actually needed -- see _load_server_modules below.
SERVER_MODULES = ("tv_server", "live", "media", "netident", "protocol")


def _load_server_modules():
    """Import the server package, now that the working directory is settled.

    Returns the `server` package so callers can reach into it the way an ordinary
    import would.
    """
    import server

    for name in SERVER_MODULES:
        __import__(f"server.{name}")
    return server


def prepare_data_dir() -> None:
    """Give a new data directory a channel list to start from.

    Without this a bundled build starts with the four channels that are built
    into the server, and the user has to open the page, wait for the playlist to
    load and choose -- which works, but is a worse first run than the one a
    checkout gives, where the repository's own `channels.txt` is right there.

    Copied once, and never again: an existing file is the user's, however it got
    there. Overwriting it would delete a channel list they had arranged, and the
    only moment this runs is start-up, when losing that would be least expected.
    """
    if not datadir.is_frozen():
        # A checkout already has the table beside the code, and there the data
        # directory is that same directory. Copying would be a no-op at best and
        # a surprise at worst.
        return

    target = datadir.channels_file()
    if target.exists():
        return

    bundled = datadir.code_root() / "channels.txt"
    if not bundled.is_file():
        # Built without the data file. Not fatal -- the server has channels of
        # its own -- so this is not reported as an error.
        return

    try:
        shutil.copyfile(bundled, target)
    except OSError:
        # Read-only data directory, or a full disk. The program still runs on the
        # built-in channels, so failing here would turn a lesser experience into
        # no experience.
        return


def _run_media(argv: list[str]) -> int:
    """Become the media server.

    Takes the remaining arguments, because tv_server.main does. The two entry
    points are not written alike -- see _run_config.

    The server is loaded here rather than at the top of the file, which is why
    _load_server_modules exists: by the time anything calls this, the working
    directory has been set and the channel table that `server.live` reads at
    import time is the right one.
    """
    server = _load_server_modules()
    return server.tv_server.main(argv)


def _run_config(argv: list[str]) -> int:
    """Become the channel page.

    The remaining arguments are passed explicitly. Letting it read `sys.argv`
    would hand it the executable's own sub-command as well -- by the time this
    runs, sys.argv is `[tv-server, __config, --port, 8097]` and argparse rejects
    `__config` as an unknown argument. Taking the slice keeps the internal
    protocol out of the page's parser.
    """
    return channel_config.main(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # The working directory is set before anything reads a file, because the
    # channel table is found relative to it and the data directory is not where
    # the executable happens to be.
    try:
        os.chdir(datadir.data_dir())
    except OSError as error:
        print(f"无法切换到数据目录：{error}", file=sys.stderr)
        return 1

    # Only the launcher prepares the data directory. The sub-commands are started
    # by it and by then the work is done -- and doing it twice would race two
    # processes copying the same file.
    if not arguments or arguments[0] not in (MEDIA_COMMAND, CONFIG_COMMAND):
        prepare_data_dir()

    if arguments and arguments[0] == MEDIA_COMMAND:
        return _run_media(arguments[1:])
    if arguments and arguments[0] == CONFIG_COMMAND:
        return _run_config(arguments[1:])

    # No sub-command: this is someone starting the program, which is the whole
    # point of it existing. The launcher takes over from here.
    return launch.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
