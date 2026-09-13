#!/usr/bin/env python3
"""Start a built executable and check that it really runs.

Building successfully is not the same as working, and the gap between them is
exactly where this build's problems have been. PyInstaller decides what to ship
by following imports it can resolve, so a module reached only through a string at
run time is left out and the bundle builds without complaint -- then dies in
front of whoever downloaded it. Several failures of that kind were found by hand
while writing this; a build that only reported "succeeded" would have shipped
every one of them.

Five things are checked, in order:

1. The executable answers `--help` with a successful exit code, in one language.
   This loads the launcher and everything it imports, which is where a missing
   module shows up first.
2. It can verify a TLS certificate, with the machine's own bundle taken away.
   Everything this program does on the network is HTTPS, so a build that cannot
   do this can do nothing at all -- and that is what shipped once, because the
   failure is invisible until the executable runs on a computer other than the
   one that built it. Tested here, before the port checks, because it needs no
   port and a busy runner must not hide it.
3. Started with no arguments in an empty directory, it stays up. That exercises
   the whole start-up path -- the data directory is resolved and created, the
   channel table is prepared, and both sub-processes are started by re-invoking
   the executable, which is the dispatch that exists only in a bundled build.
4. The media server's port accepts a connection. A live process proves the
   dispatch worked; a listening socket proves the server inside it came up.
5. A first run leaves a channel table beside the executable, where the user can
   find it.
6. Ending the program -- by SIGTERM, which is what closing a terminal window
   sends -- ends the two processes it started, so the ports are free for the
   next run. Without this the children survive their parent and the next run
   reports "端口已被占用" with no way to tell why.

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
import shutil
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
from tools.subcommands import CERTS_COMMAND  # noqa: E402

use_utf8()

# How long to wait for the program to reach the point of listening. It unpacks
# itself, then starts two children, one of which may be fetching ffmpeg, so this
# is generous on purpose: a slow machine must not look like a broken build. A
# missing module fails immediately and no amount of waiting helps it.
START_TIMEOUT_S = 90
HELP_TIMEOUT_S = 60
CONNECT_TIMEOUT_S = 20
# How long the children are given to leave after the launcher is signalled.
# They close a socket and end their own ffmpeg, so this is generous; a child
# that has not gone by now was never asked.
ORPHAN_TIMEOUT_S = 30

# The media server's default port. Used to recognise its socket, so that an
# unrelated listener on the runner is not mistaken for this program's.
MEDIA_PORT = 8096

# How to read what a child printed. The encoding is stated rather than left to
# the platform, and this is not a detail: the default for `text=True` is the
# system code page, so on a Windows runner the program's Chinese output cannot
# be decoded at all and `subprocess.run` raises UnicodeDecodeError in its own
# reader thread -- after which stdout is None and the comparison below fails
# with a TypeError about NoneType. That is exactly how this build broke: three
# thousand lines of correct implementation, and the check that reads its output
# could not read Chinese. Errors are replaced for the same reason the program's
# own console wrapper replaces them: a mis-decoded character in a message is
# readable, an exception is a build that stops.
_CAPTURE_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def check_help(executable: Path) -> None:
    """Check that the executable loads and reports itself."""
    result = subprocess.run([str(executable), "--help"],
                            capture_output=True, timeout=HELP_TIMEOUT_S, **_CAPTURE_TEXT)
    if result.returncode != 0:
        raise SystemExit(
            f"--help 返回 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    # The launcher's own description, so this cannot pass on the help output of
    # some other program that happened to be invoked.
    if "启动媒体服务器和频道配置页" not in (result.stdout + result.stderr):
        raise SystemExit(f"--help 的输出不像本程序：\n{result.stdout}")
    # And in one language throughout. argparse writes the `usage` and `options`
    # headings itself, in English; they are rewritten by the launcher, and this
    # is where a regression would show up -- as a downloaded program explaining
    # itself in two languages in four lines.
    for english in ("usage:", "options:", "optional arguments:"):
        if english in result.stdout:
            raise SystemExit(
                f"--help 里还有 argparse 自带的英文标题 {english!r}：\n{result.stdout}")


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
    # The mode is copied, not invented. Setting 0o755 here was the second half
    # of the same mistake the caller made: it gave the test a runnable file no
    # matter what the artifact's permissions were, so an artifact that users
    # could not run passed anyway.
    if os.name == "posix":
        shutil.copymode(executable, staged)
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
    """Whether something accepts a connection at this address.

    Connecting and closing without a word, which is what this does, is
    indistinguishable at the other end from a device that changed its mind --
    the server counts it as a rejected connection, and says so in the line it
    prints when it stops. That is this check's own fingerprint, not a fault it
    found: a run of this script leaves one behind, which is worth knowing
    before reading that line as evidence of a problem.
    """
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _as_released(target: Path) -> Path:
    """The executable as a user receives it, unpacked if it ships in an archive.

    macOS and Linux builds are published inside a zip, because a release asset
    download does not carry the POSIX executable bit and the program inside
    would arrive unrunnable. Testing the build output directly would test a file
    nobody downloads: it still has the bit the build gave it.

    Unpacked here, in a temporary directory, so that the permissions examined
    afterwards are the ones the archive restores -- which is the same thing a
    user gets.

    Unpacked by hand rather than with `ZipFile.extractall`, which does not
    restore permission bits: extracting this archive with it yields mode 0644
    while the same archive unpacked by `unzip`, or by double-clicking it in
    Finder, yields 0755. Testing through extractall therefore reported a broken
    artifact that works -- the second time in this file that a tool's convenience
    diverged from what the user experiences. The mode is read from the entry and
    applied explicitly.
    """
    import stat
    import tempfile
    import zipfile

    if target.is_dir():
        archives = [p for p in sorted(target.iterdir())
                    if p.is_file() and p.suffix == ".zip"]
        if len(archives) == 1:
            target = archives[0]
    if target.suffix != ".zip":
        return target

    destination = Path(tempfile.mkdtemp(prefix="ai-passport-release-"))
    with zipfile.ZipFile(target) as archive:
        entries = [i for i in archive.infolist() if not i.is_dir()]
        if len(entries) != 1:
            raise SystemExit(
                f"{target.name} 里应当恰好有一个文件，实际有 {len(entries)} 个")
        entry = entries[0]
        unpacked = destination / entry.filename
        unpacked.write_bytes(archive.read(entry))
        # What the archive says the mode is. Falls back to 0644 for an entry
        # written by a tool that recorded no Unix mode at all, which is the
        # honest reading rather than inventing an executable bit.
        mode = (entry.external_attr >> 16) & 0o7777
        unpacked.chmod(mode if mode else stat.S_IRUSR | stat.S_IWUSR)
    return unpacked


def check_certificates(executable: Path) -> None:
    """Check that this build can verify a TLS certificate at all.

    Everything this program does on the network is HTTPS, and a build without
    certificate authorities can do none of it: the playlist does not load and
    ffmpeg is never fetched. That is not hypothetical -- the macOS build from CI
    did exactly this, and the first person to meet it was a user, in the first
    thing they tried. The cause was the certificate path recorded by the build
    machine's Python, which is not on the reader's computer.

    The failure cannot be seen in the source and needs no network to detect. It
    is a question about whether a default TLS context has any authorities in it,
    and it has to be put to the built executable: `python3` on the build machine
    finds a working bundle and answers as though all were well.

    Which raises the question this check would otherwise get wrong. Run on the
    machine that built it, the executable may well succeed -- if the path baked
    into it happens to exist there. It is the reader's computer that does not
    have that path, and that is the machine the check has to imitate. Pointing
    the certificate variables at a file that is not there does exactly that: it
    removes the bundle the build machine had, leaving only what this build can
    find on its own, which is what a user gets.

    Nothing is fetched. Contacting a real host would make this check depend on
    someone else's uptime, and would answer a different question -- whether that
    host is reachable -- when the one that matters is whether verification is
    possible here at all.
    """
    absent = str(Path(tempfile.gettempdir()) / "not-a-certificate-bundle.pem")
    environment = dict(os.environ)
    environment["SSL_CERT_FILE"] = absent
    environment["SSL_CERT_DIR"] = absent + ".d"
    result = subprocess.run([str(executable), CERTS_COMMAND], env=environment,
                            capture_output=True, timeout=HELP_TIMEOUT_S, **_CAPTURE_TEXT)
    answer = (result.stdout + result.stderr).strip()
    print(f"  2. HTTPS 证书（模拟用户机器，也就是已发布产物在别处的表现）\n"
          f"     {answer}", flush=True)
    if result.returncode != 0 or "authorities=0" in answer:
        raise SystemExit(
            "这个产物在别的机器上无法校验 HTTPS 证书，凡是联网的功能都会失败：\n"
            f"  {answer}\n"
            "检查 tools/certs.py 有没有被打进产物，以及它找到的是不是本机原有的证书文件。")


def check_children_leave(process: subprocess.Popen, workdir: Path) -> None:
    """Check that ending the program also ends the two it started.

    The launcher starts the media server and the channel page as separate
    processes, in their own session so that one process can decide the order
    they stop in. The cost of that arrangement is that nothing else tells them
    to stop: if the launcher dies without asking, they outlive it and go on
    holding ports 8096 and 8097, and the next run reports "端口已被占用" with
    nothing on screen to connect it to a program the user thinks they closed.

    That is not hypothetical either. SIGTERM -- what a closed terminal window
    and a plain `kill` both send -- took Python's default path and skipped the
    cleanup entirely. The check below sends exactly that signal, because
    `terminate()` is what a user's action amounts to and what the previous
    version of this file used in its own cleanup, testing the one signal that
    worked.

    Checked by looking for the ports rather than for processes: a process list
    has to be filtered by name and by user, while a port that is still open is
    the thing that actually breaks the next run.
    """
    print("  6. 收到终止信号后子进程一并退出 …", flush=True)
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    # The children exit on their own once the launcher asks them. A moment is
    # allowed for that: they close sockets and end their own ffmpeg.
    deadline = time.monotonic() + ORPHAN_TIMEOUT_S
    while time.monotonic() < deadline:
        if not any(_port_is_open(host, port)
                   for host, port in ((h, MEDIA_PORT) for h in _candidate_hosts())):
            print("     通过", flush=True)
            return
        time.sleep(0.5)

    raise SystemExit(
        f"程序已退出，但 {MEDIA_PORT} 端口仍被占用：媒体服务器被留成了孤儿进程。\n"
        "下次启动会报「端口已被占用」，用户只能重启电脑。\n"
        "检查 tools/launch.py 的 _install_termination_handlers 是否覆盖了 SIGTERM，"
        "以及 tools/packaged_entry.py 的 _exit_when_orphaned 是否生效。")


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
        if not candidates:
            raise SystemExit(f"{target} 里没有 tv-server* 产物")
        # A zip is preferred when both forms are present, because the zip is
        # what gets published: macOS and Linux builds are shipped inside one so
        # that the executable bit survives the download. Given the choice, the
        # one to test is the one users receive.
        archives = [p for p in candidates if p.suffix == ".zip"]
        if len(archives) > 1 or (not archives and len(candidates) > 1):
            raise SystemExit(
                f"{target} 里有 {len(candidates)} 个 tv-server*，无法确定用哪个")
        target = archives[0] if archives else candidates[0]

    # Unpacked here if it arrived as one, so that what gets tested is what a
    # user ends up with rather than what happened to be on the build machine.
    target = _as_released(target)

    if not target.is_file():
        raise SystemExit(f"找不到 {target}")

    # The executable bit is checked, not granted. This line used to chmod the
    # file before testing it, which is how the smoke test passed on artifacts
    # that could not be run by anyone who downloaded them: a release asset
    # arrives as mode 0644, because the executable bit is filesystem metadata
    # rather than part of the file. Granting it here tested a file that users
    # would never have. Now the packaged form is unpacked exactly as a download
    # would be and the resulting permissions are what gets tested.
    if os.name == "posix" and not target.stat().st_mode & 0o111:
        raise SystemExit(
            f"{target.name} 没有执行权限（mode {oct(target.stat().st_mode & 0o777)}）。"
            "用户下载后双击会得到 permission denied。"
            "macOS/Linux 的产物应当打成 zip 发布，zip 会保留执行权限位。")

    print(f"检查 {target.name}（{target.stat().st_size / 1e6:.1f} MB）", flush=True)

    print("  1. --help …", flush=True)
    check_help(target)
    print("     通过", flush=True)

    # Before anything that needs a port. The question is about this build alone
    # -- whether it carries the means to verify a certificate -- and a build
    # that cannot do that is broken whatever the ports are doing. Asked here so
    # that a busy port on the runner does not hide it.
    check_certificates(target)

    if listening_address(timeout=1):
        raise SystemExit(f"启动前端口 {MEDIA_PORT} 已被占用，无法判断是不是本程序在监听。")

    print("  3. 空目录启动（下方为其输出）…", flush=True)
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
        print(f"  4. {host}:{port} 接受连接 …", flush=True)
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
        print(f"  5. 数据目录内容：{produced}", flush=True)
        if not any(name.startswith("channels") for name in produced):
            raise SystemExit(
                f"没有在程序旁边生成频道表。目录内容：{produced}")
        print("     通过", flush=True)

        check_children_leave(process, workdir)
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
