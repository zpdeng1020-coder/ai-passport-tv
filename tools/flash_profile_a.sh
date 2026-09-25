#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-/dev/cu.usbmodem101}"
BIN="$ROOT/build/FoloToy-AI-Passport-profile-a-320x240.bin"

if [ ! -f "$BIN" ]; then
    echo "Error: $BIN not found!"
    exit 1
fi

echo "=== 一键刷回 A 档固件 (320x240 原生固件，保留 NVS 与 Wi-Fi 配置) ==="
python3 -m esptool --chip esp32c3 -p "$PORT" -b 460800 --before default_reset --after hard_reset write_flash 0x10000 "$BIN"
echo "=== A 档固件刷入成功！==="
