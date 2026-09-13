#!/usr/bin/env python3
"""Build the server into a single executable.

Run from anywhere: `python3 tools/build_server.py`. The result is written to
`build-server/`, which is not tracked -- the executable is distributed through
the releases page, like the firmware, so that the repository does not carry a
7 MB binary per version and per platform.

PyInstaller is not a dependency of the program, only of building it, and it is
not installed by default. The message below says how to get it rather than
failing with an ImportError.

Building on the machine you intend to run on is the supported case. PyInstaller
does cross-compile between some platforms and not others, and rather than try to
describe which combinations work, this refuses to guess: the CI builds each
platform on its own runner, which is simpler and always correct.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "tv-server.spec"
OUTPUT = ROOT / "build-server"
WORK = ROOT / "build-server-work"

# This script prints progress in Chinese, and on a Windows runner the console's
# default code page cannot represent it: the build died on its first line with a
# UnicodeEncodeError, before compiling anything. Imported by path rather than
# through the package, because this file is run as a script and the repository
# root is not on the search path yet.
sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

use_utf8()


def pyinstaller_available() -> bool:
    """Whether PyInstaller can be run.

    Checked by importing rather than by looking for a command: it is run as a
    module below, so an import is what actually has to succeed.
    """
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the server executable.")
    parser.add_argument("--clean", action="store_true",
                        help="remove previous build output first")
    args = parser.parse_args(argv)

    if not pyinstaller_available():
        print("需要先安装 PyInstaller（只是构建需要，运行不需要）：", file=sys.stderr)
        print("    python3 -m pip install pyinstaller", file=sys.stderr)
        return 1

    if not SPEC.is_file():
        print(f"找不到构建配置：{SPEC}", file=sys.stderr)
        return 1

    # An executable from a different platform cannot be run, so the artifact
    # records where it was built. The name is what the release page shows.
    system = platform.system().lower()
    machine = platform.machine().lower()

    if args.clean:
        for path in (OUTPUT, WORK):
            if path.is_dir():
                shutil.rmtree(path)
                print(f"已删除 {path.relative_to(ROOT)}")

    OUTPUT.mkdir(exist_ok=True)

    # --specpath is not among the options: PyInstaller refuses it when a .spec
    # file is given, because the spec already decides where things go. Only the
    # output and scratch directories are overridden, and both are kept out of the
    # repository by .gitignore.
    print(f"正在为 {system}/{machine} 构建…")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--noconfirm",
         "--distpath", str(OUTPUT),
         "--workpath", str(WORK),
         str(SPEC)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("构建失败。", file=sys.stderr)
        return result.returncode

    produced = OUTPUT / ("tv-server.exe" if platform.system() == "Windows" else "tv-server")
    if not produced.is_file():
        print(f"构建报告成功，但没有找到 {produced}", file=sys.stderr)
        return 1

    # Renamed to carry the platform, because the release page holds all of them
    # side by side and a file called `tv-server` three times over is not
    # something a person can choose between.
    is_windows = platform.system() == "Windows"
    stem = f"tv-server-{system}-{machine}"
    named = produced.with_name(stem + (".exe" if is_windows else ""))
    if named.exists():
        named.unlink()
    produced.rename(named)

    # On macOS and Linux the executable is shipped inside a zip, and on Windows
    # it is shipped as itself. The difference is not cosmetic.
    #
    # A release asset is served as a plain file download, and the POSIX
    # executable bit is not part of the file -- it is metadata the filesystem
    # keeps. What arrives on the user's machine is therefore mode 0644, and
    # whatever they double-click cannot be run: macOS reports "permission
    # denied" or opens it in a text editor. A zip stores that bit and restores
    # it on extraction, which is the only way to deliver a runnable program as
    # a download. Measured, not assumed: the released binary downloads as
    # -rw-r--r-- and the same file inside a zip extracts as -rwxr-xr-x.
    #
    # Windows needs none of this: it has no executable bit, and an .exe runs
    # because of its extension.
    if not is_windows:
        archive = named.with_name(named.name + ".zip")
        if archive.exists():
            archive.unlink()
        _zip_with_mode(named, archive)
        named.unlink()
        published = archive
    else:
        published = named

    size = published.stat().st_size
    print()
    print(f"完成：{published.relative_to(ROOT)}  ({size / 1e6:.1f} MB)")
    if not is_windows:
        print(f"  可执行文件在压缩包里：{named.name}（解压后即有执行权限）")
    return 0


def _zip_with_mode(source: Path, archive: Path) -> None:
    """Put one file into a zip, preserving its Unix permission bits.

    `ZipFile.write` does not do this on its own: it records what the filesystem
    reports, and on a machine where the bit was never set the archive would be
    extracted without it just the same. The mode is set on the entry explicitly
    so that the result does not depend on the building machine's umask or on
    the file's permissions at the moment this runs.
    """
    import zipfile

    mode = 0o755
    info = zipfile.ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
    # The high 16 bits are the Unix mode; the low bits are the MS-DOS
    # attributes, which is what tools on Windows read.
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(info, source.read_bytes())


if __name__ == "__main__":
    raise SystemExit(main())
