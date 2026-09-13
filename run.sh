#!/usr/bin/env bash
# Start the media server and the channel page.
#
# Thin on purpose. Every check worth making -- Python version, ffmpeg, the
# address to type on the device -- is in tools/launch.py, which is one program
# that runs everywhere. Writing them again here in shell would mean a second
# copy to keep in step, and a third in run.bat, and the three would drift.
#
# Double-clicking this file works on macOS when it is marked executable. From a
# terminal: ./run.sh
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

# python3 first, then python: on some systems only one of the two exists, and on
# Windows-with-Git-Bash `python` is the one that is present.
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" tools/launch.py "$@"
    fi
done

echo "找不到 Python。" >&2
echo "本项目需要 Python 3.9 或更新版本，从 https://www.python.org/downloads/ 下载安装。" >&2
exit 1
