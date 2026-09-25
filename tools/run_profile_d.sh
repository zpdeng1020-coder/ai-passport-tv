#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIND="${TV_BIND:-192.168.0.125}"
PORT="${TV_PORT:-8096}"
CHANNEL="${1:-ch022}"

echo "=== 启动 D 档优化版服务 (320x180 宽屏原生点对点 / 12kB黄金双包 / 185 kB/s 预算 / 14ms微秒级帧内平滑) ==="
exec python3 "$ROOT/tools/run_live_v2.py" \
    --repo "$ROOT" \
    --channels "$ROOT/channels.txt" \
    --channel "$CHANNEL" \
    --bind "$BIND" \
    --port "$PORT" \
    --geometry 320x180 \
    --packet-target 16384 \
    --pre-filter "smartblur=lr=1.0:ls=0.6:lt=8" \
    --fps 12 \
    --start-fps 8 \
    --min-fps 6 \
    --video-budget 185000 \
    --adaptive yes
