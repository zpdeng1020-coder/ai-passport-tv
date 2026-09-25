#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIND="${TV_BIND:-192.168.0.125}"
PORT="${TV_PORT:-8096}"
CHANNEL="${1:-ch000}"

echo "=== 启动 A 档服务 (320x240 原生点对点 / 5~7 fps 自适应 / 130 kB/s 预算) ==="
exec python3 "$ROOT/tools/run_live_v2.py" \
    --repo "$ROOT" \
    --channels "$ROOT/channels.txt" \
    --channel "$CHANNEL" \
    --bind "$BIND" \
    --port "$PORT" \
    --fps 7 \
    --start-fps 5 \
    --min-fps 4 \
    --video-budget 130000 \
    --adaptive yes
