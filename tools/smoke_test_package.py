#!/usr/bin/env python3
"""Start a built executable and check that it really runs.

Building successfully is not the same as working, and the gap between them is
exactly where this build's problems have been. PyInstaller decides what to ship
by following imports it can resolve, so a module reached only through a string at
run time is left out and the bundle builds without complaint -- then dies in
front of whoever downloaded it. Several failures of that kind were found by hand
while writing this; a build that only reported "succeeded" would have shipped
every one of them.

Three things are checked, in order:

1. The executable answers `--help` with a successful exit code. This loads the
   launcher and everything it imports, which is where a missing module shows up
   first.
2. Started with no arguments in an empty directory, it stays up. That exercises
   the whole start-up path -- the data directory is resolved and created, the
   channel table is prepared, and both sub-processes are started by re-invoking
   the executable, which is the dispatch that exists only in a bundled build.
3. The media server's port accepts a connection. A live process proves the
   dispatch worked; a listening socket proves the server inside it came up.

ffmpeg is not required and no channel is played. The question is whether the
program starts and serves, not whether a stream works -- that would need the
network and a live public channel, which is a different question and a flaky one
to ask in CI.

The program's own output is left alone: it is inherited, not captured, so it
appears in the build log in the order it happened. Reading a stream that mixes
newlines with the carriage-return rewrites of a download progress bar, and
reassembling it for display, is more code than it is worth and was got wrong
twice before being removed.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Before anything is printed. This script's messages are Chinese, and a Windows
# console's default encoding cannot represent them -- the first print ended the
# process with a UnicodeEncodeError, in CI, after the program it had just built
# was working fine.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.console import use_utf8  # noqa: E402

use_utf8()

# How long to wait for the program to reach the point of listening. It unpacks
# itself, then starts two children, one of which may be fetching ffmpeg, so this
# is generous on purpose: a slow machine must not look like a broken build. A
# missing module fails immediately and no amount of waiting helps it.
START_TIMEOUT_S = 90
HELP_TIMEOUT_S = 60
CONNECT_TIMEOUT_S = 20

# The media server's default port. Used to recognise its socket, so that an
# unrelated listener on the runner is not mistaken for this program's.
MEDIA_PORT = 8096


def check_help(executable: Path) -> None:
    """Check that the executable loads and reports itself."""
    result = subprocess.run([str(executable), "--help"],
                            capture_output=True, text=True, timeout=HELP_TIMEOUT_S)
    if result.returncode != 0:
        raise SystemExit(
            f"--help 返回 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    # The launcher's own description, so this cannot pass on the help output of
    # some other program that happened to be invoked.
    if "Start the media server" not in (result.stdout + result.stderr):
        raise SystemExit(f"--help 的输出不像本程序：\n{result.stdout}")


def start_in_empty_directory(executable: Path) -> tuple[subprocess.Popen, Path]:
    """Start it the way a new user would: an empty directory, no arguments.

    The executable is copied there rather than run from where it was built,
    because it decides where to keep its data by testing whether the directory it
    lives in can be written to. Running it from a shared location would test that
    location's permissions instead of the ones a user will have.
    """
    workdir = Path(tempfile.mkdtemp(prefix="ai-passport-smoke-"))
    staged = workdir / executable.name
    staged.write_bytes(executable.read_bytes())
    if os.name == "posix":
        staged.chmod(0o755)
    process = subprocess.Popen([str(staged)], cwd=workdir)
    return process, workdir


def listening_address(timeout: float = START_TIMEOUT_S) -> tuple[str, int] | None:
    """Wait for the media server to listen, and return where.

    The address matters, not just the port. This server binds the machine's LAN
    address, because a device elsewhere on the network has to reach it -- so
    connecting to 127.0.0.1 finds nothing even while it is working exactly as
    intended. Asking "is something listening on this port" is not the same
    question, and answering that one instead made a working build look broken.

    Found by asking the operating system, one address at a time, rather than by
    reading the program's output. Parsing the output was tried first and
    abandoned: the download progress bar rewrites itself with carriage returns,
    so "one line" is not a notion that output has.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for host in _candidate_hosts():
            if _port_is_open(host, MEDIA_PORT):
                return host, MEDIA_PORT
        time.sleep(0.5)
    return None


def _candidate_hosts() -> list[str]:
    """Addresses this program might be listening on, loopback last.

    Its own LAN address first, because that is what it binds when it can. Then
    loopback, which covers a machine with no LAN address at all -- enough for a
    CI runner, where a bound socket on any interface proves the server started.
    """
    hosts: list[str] = []
    try:
        # No packet is sent; the kernel just picks the interface a route would
        # use, and the local end of that socket is this machine's own address.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.5)
            probe.connect(("192.0.2.1", 9))     # RFC 5737, never routed
            hosts.append(probe.getsockname()[0])
    except OSError:
        pass
    hosts += ["127.0.0.1", "::1"]
    return hosts


def _port_is_open(host: str, port: int) -> bool:
    """Whether something accepts a connection at this address."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start a built server executable and check that it works.")
    parser.add_argument("path", type=Path,
                        help="the executable, or the directory holding it")
    args = parser.parse_args(argv)

    target = args.path
    if target.is_dir():
        candidates = [p for p in sorted(target.iterdir())
                      if p.is_file() and p.name.startswith("tv-server")]
        if len(candidates) != 1:
            raise SystemExit(
                f"{target} 里有 {len(candidates)} 个 tv-server*，无法确定用哪个")
        target = candidates[0]
    if not target.is_file():
        raise SystemExit(f"找不到 {target}")
    if os.name == "posix":
        target.chmod(target.stat().st_mode | 0o111)

    print(f"检查 {target.name}（{target.stat().st_size / 1e6:.1f} MB）", flush=True)

    print("  1. --help …", flush=True)
    check_help(target)
    print("     通过", flush=True)

    if listening_address(timeout=1):
        raise SystemExit(f"启动前端口 {MEDIA_PORT} 已被占用，无法判断是不是本程序在监听。")

    print("  2. 空目录启动（下方为其输出）…", flush=True)
    process, workdir = start_in_empty_directory(target)
    try:
        found: tuple[str, int] | None = None
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SystemExit(
                    f"程序在进入监听状态之前退出（返回码 {process.returncode}）。")
            for host in _candidate_hosts():
                if _port_is_open(host, MEDIA_PORT):
                    found = (host, MEDIA_PORT)
                    break
            if found:
                break
            time.sleep(0.5)
        if found is None:
            raise SystemExit(f"启动后 {START_TIMEOUT_S} 秒内没有进入监听状态。")
        print("     通过", flush=True)

        host, port = found
        print(f"  3. {host}:{port} 接受连接 …", flush=True)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while time.monotonic() < deadline and not _port_is_open(host, port):
            time.sleep(0.5)
        if not _port_is_open(host, port):
            raise SystemExit(f"{host}:{port} 没有接受连接。")
        print("     通过", flush=True)

        # A first run leaves the data directory and the channel table beside the
        # executable. Checked because that is the promise the packaged build
        # makes, and a start-up that works while writing somewhere unexpected is
        # a failure the user meets later, when they cannot find their channels.
        produced = sorted(p.name for p in workdir.iterdir())
        print(f"  4. 数据目录内容：{produced}", flush=True)
        if not any(name.startswith("channels") for name in produced):
            raise SystemExit(
                f"没有在程序旁边生成频道表。目录内容：{produced}")
        print("     通过", flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    print()
    print("冒烟测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
