#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIND="${TV_BIND:-192.168.0.125}"
PORT="${TV_PORT:-8096}"
CHANNEL="${1:-ch000}"

echo "=== 启动 B 档服务 (240x180 4/3放大平铺 / 12~14 fps 满速 / 160 kB/s 预算) ==="
exec python3 "$ROOT/tools/run_live_v2.py" \
    --repo "$ROOT" \
    --channels "$ROOT/channels.txt" \
    --channel "$CHANNEL" \
    --bind "$BIND" \
    --port "$PORT" \
    --geometry 240x180 \
    --fps 14 \
    --start-fps 10 \
    --min-fps 6 \
    --video-budget 160000 \
    --adaptive yes
