#!/usr/bin/env python3
"""Start everything this computer has to run, and say what to type on the device.

Two programs have to be running for the device to play anything:

* the media server, which pulls a channel and transcodes it (port 8096)
* the channel page, where the list of channels is chosen (port 8097)

Starting them separately works and is documented in server/README.md, but it
means knowing two commands, two ports and which one to open in a browser. This
starts both, prints the one address the device needs, and restarts the media
server if the channel list is changed while it runs -- otherwise saving on the
page has no effect until the next restart, which is the sort of thing that gets
reported as "the page does not work".

The checks come first and are the point of the whole script. Every one of them
reports what is missing, why it is needed, and what to do about it, because the
alternative is a stack trace at the moment the user least wants to read one.

Two locations matter here and they are not the same place. `CODE_ROOT` is where
the code is, which settles `import server` and the path to the channel page;
`datadir.data_dir()` is where the channel list is written and where the children
run. From a checkout both are the repository, and it would be tempting to keep
one variable for them. A bundled build is the case that separates them: the code
unpacks into a temporary directory that is deleted on exit, so anything written
there is lost. `tools/datadir.py` argues the point at length.

Standard library only, like the rest of the server.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# datadir answers "where does the data go", and every path below depends on that
# answer, so it has to be importable before the rest of this module runs. Running
# this file as a script puts its own directory on the search path rather than the
# repository root, so the package form `tools.datadir` resolves only once the
# root has been added by hand. That is the same bootstrapping the module needed
# for `server`, and it is why the import sits below rather than at the top.
_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from tools import datadir, ffmpeg_fetch  # noqa: E402  (after the path above)

# Where the code lives. Importing `server` is the only thing this is for, and it
# is not where anything is written -- see `data_dir` below for that. The two used
# to be one variable, which is correct from a checkout and wrong from a bundled
# executable, where this path names a temporary directory.
CODE_ROOT = datadir.code_root()

# Run as a file, Python puts this file's own directory on the search path, not
# the repository root -- so `import server` fails from here even though the
# package is right there. Adding the root explicitly is what makes the script
# runnable both as `python3 tools/launch.py` and by double-clicking it, which is
# the whole point of having it.
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

# From reading the code rather than copying the README, which says 3.11:
# every module has `from __future__ import annotations`, so the subscripted
# generics in signatures are strings at run time, and the newest standard-library
# call used anywhere is bytes.removesuffix, which arrived in 3.9.
MIN_PYTHON = (3, 9)

MEDIA_PORT = 8096
CONFIG_PORT = 8097

# How often the channel file is checked for changes. Fast enough that saving on
# the page feels immediate, slow enough to be free: one stat() per second.
CHANNELS_POLL_S = 1.0

# Where ffmpeg was found or fetched, once that has been decided. A module-level
# setting rather than a parameter threaded through every call: both places that
# start the media server need it, and neither has anything else to say about it.
FFMPEG: str | None = None


def python_is_new_enough() -> bool:
    return sys.version_info >= MIN_PYTHON


def find_ffmpeg() -> str | None:
    """The ffmpeg to use, or None.

    AV_FFMPEG first, then whatever is on PATH. One setting for people who have
    it installed somewhere unusual, and no configuration at all for everyone
    else -- which is the common case, since every platform's package manager
    puts it on PATH. Both are checked before anything is fetched, so a machine
    that already has ffmpeg never downloads one.
    """
    override = os.environ.get("AV_FFMPEG")
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("ffmpeg")


def resolve_ffmpeg() -> str | None:
    """The ffmpeg to use, fetching one if this machine has none.

    Three questions in the order that costs the least to answer: did someone name
    one, is one already installed, has one been fetched before. Only when all
    three come back empty does anything go over the network -- and only once,
    because the answer is kept in the data directory.

    Returns None when the download failed, having already explained why. The
    caller prints the same guidance it always did, so a machine that cannot reach
    the index is no worse off than before this existed.
    """
    found = find_ffmpeg()
    if found:
        return found

    data = datadir.data_dir()
    cached = ffmpeg_fetch.cached_path(data)
    if ffmpeg_fetch.is_usable(cached):
        return str(cached)

    def announce(size: int) -> None:
        print(f"本机没有 ffmpeg，正在获取（约 {size / 1e6:.0f} MB，仅此一次）…", flush=True)

    def progress(done: int, total: int) -> None:
        # Overwritten in place rather than printed line by line: this runs while
        # the reader is waiting, and a wall of percentages is not progress.
        if total:
            sys.stdout.write(f"\r  {done / total * 100:5.1f}%  "
                             f"{done / 1e6:.1f}/{total / 1e6:.1f} MB")
            sys.stdout.flush()

    try:
        path = ffmpeg_fetch.ensure(data, on_announce=announce, on_progress=progress)
    except ffmpeg_fetch.FetchError as error:
        # The output above ended mid-line with a percentage, so start a fresh one
        # before the explanation.
        print()
        print(str(error), file=sys.stderr)
        return None
    print(f"\r  已就绪：{path}", flush=True)
    return str(path)


def ffmpeg_advice() -> list[str]:
    """How to install ffmpeg on this system, in the words of that system.

    A per-platform command rather than a link to a download page: the reader
    already has a terminal open, and the package manager they already use
    handles the download, the signature and the updates. Handing them a tarball
    instead would mean asking them to trust a URL and put a binary on their PATH
    by hand, which is worse in every way.
    """
    system = platform.system()
    if system == "Darwin":
        if shutil.which("brew"):
            return ["    brew install ffmpeg"]
        return [
            "    这台电脑上没有 Homebrew，先装它（一条命令，见 https://brew.sh），",
            "    再执行：brew install ffmpeg",
        ]
    if system == "Windows":
        if shutil.which("winget"):
            return ["    winget install ffmpeg"]
        if shutil.which("scoop"):
            return ["    scoop install ffmpeg",
                    "    （或者装 winget 后执行：winget install ffmpeg）"]
        return [
            "    用 winget 安装（Windows 10 及以后自带）：",
            "        winget install ffmpeg",
        ]
    # Linux and the BSDs: name the manager that is actually present.
    for manager, command in (
        ("apt", "sudo apt install ffmpeg"),
        ("dnf", "sudo dnf install ffmpeg"),
        ("pacman", "sudo pacman -S ffmpeg"),
        ("apk", "sudo apk add ffmpeg"),
        ("zypper", "sudo zypper install ffmpeg"),
        ("pkg", "sudo pkg install ffmpeg"),
    ):
        if shutil.which(manager):
            return [f"    {command}"]
    return ["    用本系统的软件包管理器安装 ffmpeg。"]


def report_missing_ffmpeg() -> None:
    print()
    print("找不到 ffmpeg。", flush=True)
    print("设备要收到的是一帧帧图片和一小段一小段的音频，而网络上的频道是另一种格式，", flush=True)
    print("中间必须由 ffmpeg 转换。它不随 Python 一起安装，所以缺了它服务器无法工作。", flush=True)
    print()
    print("安装方法：", flush=True)
    for line in ffmpeg_advice():
        print(line, flush=True)
    print()
    print("装好后重新运行本程序。如果 ffmpeg 装在非标准位置，", flush=True)
    print("设置环境变量 AV_FFMPEG 指向它的完整路径即可。", flush=True)
    print()


def spawn(command: list[str]) -> subprocess.Popen:
    """Start a child in its own process group, with its output left alone.

    Output is inherited rather than captured. Capturing it into a pipe that
    nobody reads is a hang waiting to happen: the pipe holds about 64 KB, and a
    child that fills it blocks for ever on its next write. Here that would be
    the media server -- which prints a line every few seconds while playing --
    freezing partway through a session with no error and no clue. Letting both
    children write to the terminal is also the honest choice: there is nothing
    in their output worth hiding, and when something goes wrong it is the first
    thing worth reading.

    The separate process group is about what happens on the way out. ffmpeg is a
    grandchild, started by the media server to do the transcoding, and killing
    only the server can leave a transcode running with nothing reading from it.
    Signalling the group reaches the whole tree. It also keeps Ctrl-C away from
    the children, so exactly one process decides the order things stop in -- the
    server gets to close its socket and end its own ffmpeg rather than being
    interrupted mid-write.
    """
    # The working directory is the data directory, because the channel list is
    # found relative to it (server/live.py looks for a plain `channels.txt`).
    # Setting it here is what makes the bundled build read the user's list rather
    # than looking for one beside the executable. The path is absolute and
    # already created by data_dir(), so a child that starts before anything is
    # written still has a valid working directory.
    #
    # That move costs the children their other use of the working directory:
    # `python -m server.av_server` finds the package through the working
    # directory, and running from the data directory instead made the media
    # server exit with "No module named 'server'". PYTHONPATH puts the code back
    # on the search path without moving the working directory back, which is what
    # keeps the two jobs of that one setting separate. Prepended rather than
    # appended so the checkout's own modules win over anything installed
    # system-wide with the same name.
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(CODE_ROOT) if not existing else os.pathsep.join([str(CODE_ROOT), existing])
    )

    kwargs: dict = {"cwd": datadir.data_dir(), "env": environment}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, **kwargs)


def stop(process: subprocess.Popen | None) -> None:
    """Ask a child to stop, then insist. Safe to call on one already gone."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or not ours to signal. Waiting below still applies.
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # Ten seconds is generous for a process that closes a socket. One that
        # has not stopped by now is stuck, and leaving it would keep the port
        # occupied for the next run.
        process.kill()
        process.wait()


def start_media_server(channel: str) -> subprocess.Popen:
    """Launch the media server.

    A process rather than a thread: the server installs its own SIGINT handler
    and reads it as "stop listening and shut down cleanly". In a thread it would
    never receive that signal, and Ctrl-C would leave it unable to close its
    socket or end an in-flight transcode.

    The ffmpeg path is passed on the command line rather than through the
    environment, because the server reads it from `--ffmpeg` and nowhere else --
    it defaults to the bare name "ffmpeg" and looks it up on PATH. Setting
    AV_FFMPEG alone left the child searching for a program that is not there: the
    media server started, accepted a connection, and then failed to begin
    transcoding. Caught by running the whole thing with ffmpeg hidden, which is
    the case this feature exists for and the only one that shows it.
    """
    command = [sys.executable, "-u", "-m", "server.av_server", "live",
               "--channel", channel, "--port", str(MEDIA_PORT)]
    if FFMPEG:
        command += ["--ffmpeg", FFMPEG]
    return spawn(command)


def start_config_page() -> subprocess.Popen:
    """Launch the channel page. Bound to loopback, which is where it is opened."""
    return spawn([sys.executable, "-u",
                  str(CODE_ROOT / "tools" / "channel_config.py"),
                  "--port", str(CONFIG_PORT)])


def channels_mtime() -> float | None:
    """Modification time of the channel file, or None if there is not one.

    The media server reads the table once, at start-up. That is the right design
    -- re-reading it mid-session would change the list under a running device --
    but it means a saved change does nothing until a restart, and from the
    page's side that is indistinguishable from the save having failed.
    """
    path = datadir.channels_file()
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def channels_now() -> dict:
    """The channel table as it is on disk right now.

    `server.live.CHANNELS` is read once, when that module is first imported, and
    never re-read -- deliberately, so that a running session does not have its
    channel list changed underneath it. That is the wrong answer for a restart,
    which is starting a new session and needs the table the user has just saved.

    Re-imported rather than cached: the point is to see the current file, and a
    cached copy is exactly what would still be showing the old table.
    """
    import importlib

    from server import live

    importlib.reload(live)
    return live.CHANNELS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start the media server and the channel page.")
    parser.add_argument("--channel", default=None,
                        help="channel to open first (default: the server's own default)")
    parser.add_argument("--no-config-page", action="store_true",
                        help="start only the media server")
    args = parser.parse_args(argv)

    if not python_is_new_enough():
        running = ".".join(str(part) for part in sys.version_info[:3])
        needed = ".".join(str(part) for part in MIN_PYTHON)
        print(f"Python {running} 太旧，需要 {needed} 或更新的版本。", file=sys.stderr)
        print(f"当前用的是：{sys.executable}", file=sys.stderr)
        return 1

    # Resolved once, at the top, and recorded where the media server can be told
    # about it. A fetched copy is not on PATH, so the child has to be given the
    # path explicitly -- see start_media_server.
    global FFMPEG
    FFMPEG = resolve_ffmpeg()
    if FFMPEG is None:
        report_missing_ffmpeg()
        return 1

    # What the reader needs is the address to type on the device. The media
    # server already prints it, from the address it actually bound -- which is
    # the truth, and better than anything this file could work out separately.
    # Printing it again here would say the same thing twice, and if the two ever
    # disagreed the reader would have no way to tell which to believe.
    #
    # Imported here rather than at the top because it reads the channel file as a
    # side effect of being imported, and the working directory has to be the data
    # directory by then. The packages under `server/` are reached through
    # CODE_ROOT on the search path.
    # The parent moves to the data directory too, not just the children it
    # starts. It reads the channel table itself -- to pick a channel now, and to
    # pick one again after a save -- and `server.live` finds that table relative
    # to the working directory. Left where it was started, it read a different
    # (usually absent) table and quietly fell back to the built-in channels, so
    # the restart after a save passed a channel name that the server, reading the
    # real table, rejected. Changed before the import below, because the import
    # is what reads the file.
    try:
        os.chdir(datadir.data_dir())
    except OSError as error:
        print(f"无法切换到数据目录：{error}", file=sys.stderr)
        return 1

    from server.live import CHANNELS

    # The media server takes --channel as a required choice, so "whichever it
    # would have picked" is not something it can be asked for. It is read here
    # instead: the first entry of the table, which is exactly what the server
    # falls back to when a device asks for a channel it does not know. Asking
    # for a specific one is still possible with --channel.
    channel = args.channel or next(iter(CHANNELS))
    if channel not in CHANNELS:
        print(f"没有这个频道：{channel}", file=sys.stderr)
        print(f"可用的前几个：{', '.join(list(CHANNELS)[:5])}", file=sys.stderr)
        return 1

    # Printed before the children start, so it is not buried by their output.
    # The location is not predictable from the outside -- it is beside the
    # program when that directory can be written to and in the user's own
    # application directory when it cannot -- so a channel list that went
    # somewhere unexpected is otherwise indistinguishable from one that was never
    # saved.
    print(datadir.describe(datadir.data_dir()), flush=True)

    if not datadir.channels_file().is_file():
        print("还没有 channels.txt，将使用内置的默认频道。", flush=True)
        print("在下面的频道配置页里挑选并保存，就会生成它。", flush=True)
        print()

    if not args.no_config_page:
        # Prints before the children start, so it is not buried by their logs --
        # which is the one thing this script adds that the children do not do
        # for themselves.
        print(f"频道配置页：http://127.0.0.1:{CONFIG_PORT}", flush=True)
        print("  在这台电脑的浏览器里打开它，挑选频道、调整顺序。", flush=True)
        print()

    print("按 Ctrl-C 结束。下面媒体服务器会打印设备上要填的地址。", flush=True)
    print()

    server = start_media_server(channel)
    page = None if args.no_config_page else start_config_page()
    seen_mtime = channels_mtime()

    try:
        while True:
            time.sleep(CHANNELS_POLL_S)

            if server.poll() is not None:
                # The server exited on its own. Most often that is a bind failure
                # -- something else on 8096 -- which it has already explained.
                print(f"\n媒体服务器已退出（返回码 {server.returncode}）。", flush=True)
                return server.returncode or 1

            if page is not None and page.poll() is not None:
                # The page dying does not stop playback, so it is reported and
                # the server is left running rather than taking the device down
                # with it.
                print(f"\n频道配置页已退出（返回码 {page.returncode}）；媒体服务器继续运行。",
                      flush=True)
                page = None

            # A change is any difference from what was seen last time, including
            # the first appearance of the file. The file starts absent when the
            # data directory is new -- which is every first run of a bundled
            # build -- and the earlier version of this only restarted on a change
            # between two times, so saving the first channel list took effect
            # only after the next restart. Measured: the page reported a
            # successful save and the server went on offering the built-in
            # channels.
            current = channels_mtime()
            if current != seen_mtime:
                seen_mtime = current
                # Only worth restarting when there is now a file to read. Going
                # the other way -- a file removed while running -- leaves the
                # server on its current channel list, which is the same thing
                # that happens when it is edited by hand and it does not warrant
                # tearing down a working stream.
                if current is not None:
                    print("\n频道表已更新，正在重启媒体服务器…", flush=True)
                    stop(server)
                    # The channel being played may not be in the table that was
                    # just saved -- the first save replaces the built-in list
                    # entirely, and none of those names appear in it. The media
                    # server takes --channel as a required choice from the table
                    # it reads at start-up, so passing a name that is no longer
                    # there is a hard error and the server exits, leaving nothing
                    # listening. Measured: saving a first channel list printed
                    # "invalid choice: 'cgtn'" and stopped on port 8096, which
                    # the user sees as the picture going away when they save.
                    #
                    # Falling back to the first entry of the new table is what
                    # the server itself does when a device asks for a channel it
                    # does not know, so the behaviour stays consistent.
                    available = channels_now()
                    if channel not in available and available:
                        replacement = next(iter(available))
                        print(f"频道 {channel} 已不在新表里，改为 {replacement}。",
                              flush=True)
                        channel = replacement
                    server = start_media_server(channel)
                    print("已重启，设备会自动重新连接。", flush=True)
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
        return 0
    finally:
        # In a finally block, so the children are cleaned up however this
        # function is left -- including by an exception nobody expected. A
        # leaked media server keeps port 8096 open, and the next run then fails
        # to bind with no sign that the previous one is still there.
        stop(page)
        stop(server)


if __name__ == "__main__":
    raise SystemExit(main())
