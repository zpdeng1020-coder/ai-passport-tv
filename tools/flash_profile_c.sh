#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-/dev/cu.usbmodem101}"
BIN="$ROOT/build/FoloToy-AI-Passport-profile-c-280x210.bin"

if [ ! -f "$BIN" ]; then
    echo "Error: $BIN not found!"
    exit 1
fi

echo "=== 一键刷入 C 档固件 (280x210 8/7放大高画质固件，保留 NVS 与 Wi-Fi 配置) ==="
python3 -m esptool --chip esp32c3 -p "$PORT" -b 460800 --before default-reset --after hard-reset write-flash 0x10000 "$BIN"
echo "=== C 档固件刷入成功！==="
