# PyInstaller configuration for the server, built into one executable.
#
# One file rather than a directory: the point of this build is that a person
# downloads one thing and double-clicks it. A directory bundle would unpack
# instantly but would have to stay together, so the download becomes an archive
# and the instruction becomes "extract it first" -- most of the way back to the
# thing this replaces. The cost is a second or so of unpacking at each start.
#
# What goes in: the three parts of the program (tools/, server/), and the channel
# table as data. What stays out: the firmware sources, the documentation, the
# tests and the design assets, none of which the server reads -- they are most of
# the repository and none of the 6 MB that results.

import os
from pathlib import Path

# PyInstaller runs this file with its own working directory, so the repository
# root is found from this file's location rather than assumed.
ROOT = Path(SPECPATH).resolve().parent

# The channel table the program falls back to. It is read from beside the
# executable only on a first run, to give a new user a populated list instead of
# the four built-in channels -- see tools/packaged_entry.py and prepare_data_dir.
datas = [
    (str(ROOT / "channels.txt"), "."),
]

# Imported somewhere PyInstaller's static analysis cannot follow, or through a
# name it cannot resolve. `server` is reached from inside a function in
# tools/launch.py, and the sub-commands are dispatched by a string at run time.
# Listing them here is what keeps them in the bundle; without it the failure is a
# "No module named 'server'" in the user's hands, not at build time.
hiddenimports = [
    "server",
    "server.av_server",
    "server.live",
    "server.media",
    "server.netident",
    "server.protocol",
    "tools",
    "tools.datadir",
    "tools.ffmpeg_fetch",
    "tools.launch",
    "tools.channel_config",
    "tools.subcommands",
]

# Modules that are imported but never needed at run time. Excluding them keeps
# the executable smaller; the list is deliberately short, because an exclusion
# that turns out to be wrong only shows up as an error in front of a user.
excludes = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "test",
]

a = Analysis(
    [str(ROOT / "tools" / "packaged_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="av-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Compression on. The executable is started once per session and unpacked to
    # a temporary directory, so the cost is a moment at start-up against several
    # megabytes of download.
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # A console program: its entire output is what the reader has to act on --
    # the address to type on the device, and any explanation of what is missing.
    # A windowed build would show nothing at all on Windows.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
