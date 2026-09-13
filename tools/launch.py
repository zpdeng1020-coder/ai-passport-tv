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

ROOT = Path(__file__).resolve().parents[1]

# Run as a file, Python puts this file's own directory on the search path, not
# the repository root -- so `import server` fails from here even though the
# package is right there. Adding the root explicitly is what makes the script
# runnable both as `python3 tools/launch.py` and by double-clicking it, which is
# the whole point of having it.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def python_is_new_enough() -> bool:
    return sys.version_info >= MIN_PYTHON


def find_ffmpeg() -> str | None:
    """The ffmpeg to use, or None.

    AV_FFMPEG first, then whatever is on PATH. One setting for people who have
    it installed somewhere unusual, and no configuration at all for everyone
    else -- which is the common case, since every platform's package manager
    puts it on PATH.
    """
    override = os.environ.get("AV_FFMPEG")
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("ffmpeg")


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
            "    This computer has no Homebrew, so install it first (one command,",
            "    from https://brew.sh), then: brew install ffmpeg",
        ]
    if system == "Windows":
        if shutil.which("winget"):
            return ["    winget install ffmpeg"]
        if shutil.which("scoop"):
            return ["    scoop install ffmpeg", "    (or install winget and use: winget install ffmpeg)"]
        return [
            "    Install ffmpeg with winget (built into Windows 10 and later):",
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
    return ["    Install ffmpeg with this system's package manager."]


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
    kwargs: dict = {"cwd": ROOT}
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
    """
    return spawn([sys.executable, "-u", "-m", "server.av_server", "live",
                  "--channel", channel, "--port", str(MEDIA_PORT)])


def start_config_page() -> subprocess.Popen:
    """Launch the channel page. Bound to loopback, which is where it is opened."""
    return spawn([sys.executable, "-u", str(ROOT / "tools" / "channel_config.py"),
                  "--port", str(CONFIG_PORT)])


def channels_mtime() -> float | None:
    """Modification time of the channel file, or None if there is not one.

    The media server reads the table once, at start-up. That is the right design
    -- re-reading it mid-session would change the list under a running device --
    but it means a saved change does nothing until a restart, and from the
    page's side that is indistinguishable from the save having failed.
    """
    path = ROOT / "channels.txt"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


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

    if find_ffmpeg() is None:
        report_missing_ffmpeg()
        return 1

    # What the reader needs is the address to type on the device. The media
    # server already prints it, from the address it actually bound -- which is
    # the truth, and better than anything this file could work out separately.
    # Printing it again here would say the same thing twice, and if the two ever
    # disagreed the reader would have no way to tell which to believe.
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

    if not (ROOT / "channels.txt").is_file():
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

            current = channels_mtime()
            if current is not None and seen_mtime is not None and current != seen_mtime:
                seen_mtime = current
                print("\n频道表已更新，正在重启媒体服务器…", flush=True)
                stop(server)
                server = start_media_server(channel)
                print("已重启，设备会自动重新连接。", flush=True)
            elif current is not None:
                seen_mtime = current
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
