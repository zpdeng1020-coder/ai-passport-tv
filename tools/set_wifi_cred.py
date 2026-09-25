#!/usr/bin/env python3
"""Update Wi-Fi credentials and server address stored in the device's NVS partition.

Reads NVS at 0x9000..0xf000, updates ssid, password, and ota_url in place (with null terminator),
recomputes data and entry CRCs, and writes back via esptool.
"""
import argparse
import pathlib
import struct
import subprocess
import sys
import tempfile
import zlib

NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
PAGE_SIZE = 4096
PAGE_HEADER = 32
PAGE_BITMAP = 32
ENTRY_SIZE = 32
ENTRIES_PER_PAGE = 126
TYPE_SZ = 0x21
TYPE_U8 = 0x01


def entry_crc(entry: bytes) -> int:
    running = zlib.crc32(entry[0:4], 0xFFFFFFFF)
    running = zlib.crc32(entry[8:24], running)
    return zlib.crc32(entry[24:32], running) & 0xFFFFFFFF


def data_crc(payload: bytes) -> int:
    return zlib.crc32(payload, 0xFFFFFFFF) & 0xFFFFFFFF


def find_live_string_slots(region: bytes, target_key: bytes):
    for page in range(NVS_SIZE // PAGE_SIZE):
        base = page * PAGE_SIZE + PAGE_HEADER + PAGE_BITMAP
        for slot in range(ENTRIES_PER_PAGE):
            at = base + slot * ENTRY_SIZE
            entry = bytes(region[at:at + ENTRY_SIZE])
            if entry[1] in (0xFF, 0x00):
                continue
            if entry[8:24].rstrip(b"\x00") != target_key:
                continue
            span, datatype = entry[2], entry[1]
            if datatype != TYPE_SZ or span < 2:
                continue
            if struct.unpack_from("<I", entry, 4)[0] != entry_crc(entry):
                continue
            length = struct.unpack_from("<H", entry, 24)[0]
            room = ENTRY_SIZE * (span - 1)
            payload = bytes(region[at + ENTRY_SIZE:at + ENTRY_SIZE + length])
            if struct.unpack_from("<I", entry, 28)[0] != data_crc(payload):
                continue
            yield at, span, payload, room


def rewrite_key(region: bytearray, key: bytes, new_val_str: str):
    new_val = new_val_str.encode("utf-8") + b"\x00"
    slots = list(find_live_string_slots(region, key))
    if not slots:
        print(f"Warning: key '{key.decode()}' not found in NVS", file=sys.stderr)
        return False
    for at, span, old_val, room in slots:
        old_display = old_val.rstrip(b"\x00").decode(errors='replace')
        new_display = new_val_str
        print(f"Updating '{key.decode()}' at {at:#06x} (old: '{old_display}') -> '{new_display}' (len={len(new_val)})")
        if len(new_val) > room:
            raise ValueError(f"Value '{new_display}' ({len(new_val)} bytes) exceeds room ({room} bytes)")
        start = at + ENTRY_SIZE
        region[start:start + room] = new_val.ljust(room, b"\xff")
        struct.pack_into("<H", region, at + 24, len(new_val))
        struct.pack_into("<I", region, at + 28, data_crc(new_val))
        struct.pack_into("<I", region, at + 4, entry_crc(bytes(region[at:at + ENTRY_SIZE])))
    return True


def rewrite_channel(region: bytearray, channel: int):
    # Search for u8 'channel' keys
    for page in range(NVS_SIZE // PAGE_SIZE):
        base = page * PAGE_SIZE + PAGE_HEADER + PAGE_BITMAP
        for slot in range(ENTRIES_PER_PAGE):
            at = base + slot * ENTRY_SIZE
            entry = bytearray(region[at:at + ENTRY_SIZE])
            if entry[1] == TYPE_U8 and entry[8:24].rstrip(b"\x00") == b"channel":
                entry[24] = channel
                struct.pack_into("<I", entry, 4, entry_crc(bytes(entry)))
                region[at:at + ENTRY_SIZE] = entry
                print(f"Set channel at {at:#06x} to {channel}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--ssid")
    parser.add_argument("--password")
    parser.add_argument("--server", help="Server address host:port, e.g. 192.168.0.125:8096")
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--esptool", default="python3 -m esptool")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    cmd = args.esptool.split()
    with tempfile.TemporaryDirectory() as work:
        work_dir = pathlib.Path(work)
        bin_path = work_dir / "nvs.bin"

        read_cmd = cmd + ["--port", args.port, "read-flash", f"{NVS_OFFSET:#x}", f"{NVS_SIZE:#x}", str(bin_path)]
        subprocess.run(read_cmd, check=True)

        data = bytearray(bin_path.read_bytes())

        if args.list:
            for k in (b"ssid", b"password", b"ota_url"):
                for at, span, val, room in find_live_string_slots(data, k):
                    print(f"  {k.decode()}: '{val.rstrip(bchr(0)).decode(errors='replace')}' (at {at:#06x}, room={room})")
            return 0

        modified = False
        if args.ssid:
            if rewrite_key(data, b"ssid", args.ssid):
                modified = True
        if args.password:
            if rewrite_key(data, b"password", args.password):
                modified = True
        if args.server:
            if rewrite_key(data, b"ota_url", args.server):
                modified = True
        if args.channel is not None:
            rewrite_channel(data, args.channel)
            modified = True

        if modified:
            bin_path.write_bytes(data)
            write_cmd = cmd + ["--port", args.port, "write-flash", f"{NVS_OFFSET:#x}", str(bin_path)]
            subprocess.run(write_cmd, check=True)
            print("Successfully updated NVS on device!")
        else:
            print("No changes made.")


if __name__ == "__main__":
    sys.exit(main())
