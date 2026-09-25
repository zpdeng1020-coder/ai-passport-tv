#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIND="${TV_BIND:-192.168.0.125}"
PORT="${TV_PORT:-8096}"
CHANNEL="${1:-ch000}"

echo "=== 启动 C 档服务 (280x210 8/7放大高画质 / 10~11 fps / 192 kB/s 预算) ==="
exec python3 "$ROOT/tools/run_live_v2.py" \
    --repo "$ROOT" \
    --channels "$ROOT/channels.txt" \
    --channel "$CHANNEL" \
    --bind "$BIND" \
    --port "$PORT" \
    --geometry 280x210 \
    --fps 11 \
    --start-fps 8 \
    --min-fps 5 \
    --video-budget 192000 \
    --adaptive yes
