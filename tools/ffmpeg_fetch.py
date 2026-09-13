"""Getting ffmpeg onto a machine that does not have it.

ffmpeg is the one thing the server cannot do without and cannot ship. It is a
separate program, not a Python library: the channels arrive in formats the device
cannot read, and ffmpeg is what turns them into the pictures and sound it can.
Everything else the server needs is either the standard library or the channel
table, so this is the only remaining step between downloading the program and
watching television -- and until now it was the step that asked the most of
someone: install Homebrew, then install ffmpeg through it, or work out which
package manager their system uses.

It is not bundled with this project, for a licensing reason rather than a
technical one. The ffmpeg builds published for Python use are GPL builds, and
redistributing one inside a downloaded program brings the obligation to supply
its source. Fetching it on the user's own machine at their own request is not
redistribution. So the download happens at first run, once, and is kept for next
time.

Where it comes from: the `imageio-ffmpeg` project on PyPI publishes a wheel per
platform with an ffmpeg binary inside, which is the only source found that covers
macOS, Linux and Windows in one predictable shape. A wheel is a zip file, so the
standard library can unpack it and this stays a standard-library program.

Two things are pinned rather than looked up. The version, so that a new release
upstream cannot change what this downloads without a change here -- and the
SHA-256 of each wheel, because the result is a program this one then runs. An
index that has been tampered with would otherwise be enough to execute anything.
If a pinned hash stops matching, the download is refused and nothing is executed.

A wheel is 20-31 MB, mostly ffmpeg itself, so the caller announces it before it
starts rather than letting a long silence happen unexplained.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# The release this file knows about. Bumping it means pasting in the new
# hashes below, which is the point: the update cannot happen by itself.
VERSION = "0.6.0"

# Where the wheels live. The index is queried only for the download URL, never
# for the hashes -- those are the constants below, so a wrong index cannot
# substitute a different file.
INDEX = "https://pypi.org/pypi/imageio-ffmpeg/json"

# filename: (sha256, size in bytes). Taken from the PyPI release metadata.
WHEELS = {
    "imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl": (
        "b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742", 21113891),
    "imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl": (
        "9d2baaf867088508d4a3458e61eeb30e945c4ad8016025545f66c4b5aaef0a61", 24932969),
    "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_aarch64.whl": (
        "1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc", 25632706),
    "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl": (
        "c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282", 29498237),
    "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl": (
        "02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a", 31246824),
    "imageio_ffmpeg-0.6.0-py3-none-win32.whl": (
        "196faa79366b4a82f95c0f4053191d2013f4714a715780f0ad2a68ff37483cc2", 19652251),
}

# Which wheel suits which machine. Keyed by what `platform` reports, which
# spells the same architecture differently on each system: an ARM Mac is
# "arm64", an ARM Linux box is "aarch64", and Windows reports "AMD64".
#
# Windows on ARM is absent deliberately and falls back to the 64-bit Intel
# build, which Windows runs under emulation. A 32-bit build would also run but
# would be the slower choice on a machine that can do better.
#
# The builds are not all the same ffmpeg version, which the wheel filenames do
# not show and which was found by looking inside them: macOS and 64-bit Windows
# carry 7.1, Linux 7.0.2, and the 32-bit Windows build only 4.2.2. Everything the
# server asks ffmpeg to do -- HLS input, MJPEG output, raw PCM, -re pacing -- is
# older than 4.2, so the gap does not matter today. It is written down because
# the difference is invisible from the outside, and someone debugging a
# Windows-specific behaviour would otherwise have no reason to suspect it.
PLATFORM_WHEELS: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): "imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl",
    ("Darwin", "x86_64"): (
        "imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl"),
    ("Linux", "aarch64"): "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_aarch64.whl",
    ("Linux", "x86_64"): "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl",
    ("Windows", "AMD64"): "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
    ("Windows", "ARM64"): "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
    ("Windows", "x86"): "imageio_ffmpeg-0.6.0-py3-none-win32.whl",
}

# Where the binary sits inside the wheel, and which names in that directory are
# not it.
_BINARIES = "imageio_ffmpeg/binaries/"
_NOT_THE_BINARY = ("README.md", "__init__.py")

# Read size while downloading. Large enough that the per-chunk overhead is
# nothing, small enough that the progress line moves several times a second.
_CHUNK = 256 * 1024

DOWNLOAD_TIMEOUT_S = 60
_OPEN_TIMEOUT_S = 30


class FetchError(Exception):
    """Something went wrong that the reader can act on.

    The message is written to be shown: it says what failed and what to do
    instead, because the people who reach it are being asked to fetch a program
    they did not know they needed.
    """


def wheel_for(system: str | None = None, machine: str | None = None) -> str | None:
    """The wheel filename for this machine, or None when there is not one.

    None means the platform is not covered, which is reported as "install ffmpeg
    yourself" rather than as a download failure -- the two need different advice
    and only one of them is worth retrying.
    """
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    return PLATFORM_WHEELS.get((system, machine))


def cached_path(data_dir: Path) -> Path:
    """Where the fetched ffmpeg is kept.

    The version is part of the name so that raising VERSION replaces the file
    instead of finding the old one still sitting there under the same name and
    reporting success.
    """
    name = f"ffmpeg-{VERSION}"
    if platform.system() == "Windows":
        name += ".exe"
    return data_dir / name


def is_usable(path: Path) -> bool:
    """Whether a file is there and looks runnable.

    A plain existence check plus a non-zero size. The contents need no checking:
    they were verified against a pinned hash before being written, and the write
    is atomic, so a file that exists is a file that was verified.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _download_url(wheel: str) -> str:
    """Ask the index where this wheel can be had.

    The index is trusted for the address only. What arrives is hashed and
    compared against the constant, so an index that answered with somewhere
    else to download from gains nothing by it.
    """
    import json

    try:
        with urllib.request.urlopen(INDEX, timeout=_OPEN_TIMEOUT_S) as response:
            metadata = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FetchError(
            f"无法查询下载地址：{error}\n"
            "  请检查网络连接。若本机需要代理，请设置 https_proxy 后重试。") from error

    for entry in metadata.get("urls", []):
        if entry.get("filename") == wheel:
            url = entry.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
    raise FetchError(
        f"下载源里没有 {wheel}。\n"
        "  这通常意味着本项目固定了一个已被撤下的版本，请报告这个问题。\n"
        "  也可以先自行安装 ffmpeg 应急。")


def _download(url: str, on_progress=None) -> bytes:
    """Fetch the whole file, reporting progress as it goes.

    Held in memory rather than streamed to a file: the wheel is at most about
    31 MB, the hash has to be computed over all of it anyway, and keeping it in
    one piece means a failed download leaves nothing behind to clean up.
    """
    request = urllib.request.Request(url, headers={"User-Agent": f"ai-passport-tv/{VERSION}"})
    chunks: list[bytes] = []
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            # Content-Length is used only for the progress figure. A server that
            # omits it costs a percentage display, not the download.
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if on_progress:
                    on_progress(received, total)
    except (urllib.error.URLError, OSError) as error:
        raise FetchError(
            f"下载失败：{error}\n"
            "  请检查网络连接后重试。若本机需要代理，请设置 https_proxy。") from error
    return b"".join(chunks)


def _extract(blob: bytes, destination: Path) -> None:
    """Take the ffmpeg binary out of the wheel and put it at `destination`.

    The binary's name inside the wheel is discovered rather than written down
    here. It carries the ffmpeg version as well as the platform
    (`ffmpeg-macos-aarch64-v7.1`), so a fixed name would have to be kept in step
    with the pinned release by hand -- the sort of thing that is correct until
    somebody bumps one and forgets the other. There is exactly one binary in that
    directory, which is what makes looking for it reliable.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as error:
        raise FetchError("下载的文件不是有效的压缩包，可能不完整。请重试。") from error

    candidates = [
        name for name in archive.namelist()
        if name.startswith(_BINARIES)
        and not name.endswith("/")
        and not name.endswith(_NOT_THE_BINARY)
    ]
    if len(candidates) != 1:
        raise FetchError(
            f"压缩包里的可执行文件数量异常（{len(candidates)} 个），已停止。\n"
            "  这通常意味着固定的版本内容变了。")

    # Written beside the destination and moved into place, so an interrupted
    # extraction cannot leave a half-written file that the next run treats as
    # the real thing. os.replace overwrites atomically on every platform this
    # supports.
    temporary = destination.with_name(destination.name + ".part")
    try:
        with archive.open(candidates[0]) as source, open(temporary, "wb") as sink:
            shutil.copyfileobj(source, sink, _CHUNK)
        # The executable bit is what makes the next run able to start it. Set on
        # the temporary file so that it is never briefly present without it.
        os.chmod(temporary, os.stat(temporary).st_mode | stat.S_IXUSR | stat.S_IXGRP
                 | stat.S_IXOTH)
        os.replace(temporary, destination)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise FetchError(f"写入 ffmpeg 失败：{error}") from error


def fetch(data_dir: Path, on_announce=None, on_progress=None) -> Path:
    """Download, verify and unpack ffmpeg. Returns where it was put.

    Raises FetchError with a message meant for the reader. Nothing is executed
    from the download and nothing is left in place unless the hash matched.
    """
    wheel = wheel_for()
    if wheel is None:
        raise FetchError(
            f"没有适配本机（{platform.system()} / {platform.machine()}）的下载包。")

    expected_hash, expected_size = WHEELS[wheel]
    destination = cached_path(data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise FetchError(
            f"无法写入数据目录 {data_dir}：{error}\n"
            "  可以设置环境变量 TV_DATA_DIR 指向一个可写目录后重试。") from error

    if on_announce:
        on_announce(expected_size)

    url = _download_url(wheel)
    blob = _download(url, on_progress)

    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected_hash:
        # Refused rather than warned about: this file is about to be run, and a
        # mismatch means it is not the file that was checked when this version
        # was written.
        raise FetchError(
            "下载的文件校验值不符，已停止，没有运行任何东西。\n"
            f"  期望 {expected_hash}\n"
            f"  实际 {digest}\n"
            "  可能是下载不完整或文件被替换。请重试；若反复出现请报告。")

    _extract(blob, destination)
    return destination


def ensure(data_dir: Path, on_announce=None, on_progress=None) -> Path:
    """The fetched ffmpeg, downloading it the first time only.

    The cache is checked first, so every run after the first is silent and
    offline. That matters more than it sounds: this program is started by
    double-clicking, often on a laptop that is not always online.
    """
    cached = cached_path(data_dir)
    if is_usable(cached):
        return cached
    return fetch(data_dir, on_announce=on_announce, on_progress=on_progress)


if __name__ == "__main__":  # pragma: no cover - a convenience, not a feature
    # Running this file directly fetches ffmpeg and says where it went, for
    # anyone who would rather do this step on its own than start the server to
    # trigger it.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from tools.console import use_utf8 as _use_utf8

    # This block prints Chinese, and on Windows the console's default encoding
    # cannot represent it. The library above does not need this -- its callers
    # set it -- but running this file directly makes it the entry point.
    _use_utf8()

    from tools import datadir as _datadir

    def announce(size: int) -> None:
        print(f"正在获取视频转换组件 ffmpeg（约 {size / 1e6:.0f} MB，仅此一次）…")

    def progress(done: int, total: int) -> None:
        if total:
            sys.stdout.write(f"\r  {done / total * 100:5.1f}%  "
                             f"{done / 1e6:5.1f}/{total / 1e6:.1f} MB")
            sys.stdout.flush()

    try:
        print(f"已就绪：{ensure(_datadir.data_dir(), announce, progress)}")
    except FetchError as error:
        print(f"\n{error}", file=sys.stderr)
        raise SystemExit(1)
