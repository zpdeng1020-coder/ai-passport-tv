"""Change the server address stored in the device's NVS, over the serial port.

The device reads where to connect from NVS ("wifi"/"ota_url") and that stored
value takes precedence over anything compiled in, so pointing it at a different
machine otherwise means putting it into setup mode and typing the address into
its web page. For comparing one server against another on the same device, that
is too much ceremony for a string.

The value sits in the NVS data partition as an ordinary string, so this reads
the region, replaces the bytes of the old address, repairs the two CRCs NVS
checks on read, and writes the region back. It refuses anything it does not
recognise rather than guessing.

    python3 tools/set_server_addr.py --port /dev/cu.usbmodem1101 --list
    python3 tools/set_server_addr.py --port /dev/cu.usbmodem1101 --address 192.168.0.125:8096

Offsets come from the device's own partition table, read out of the image rather
than assumed:

    nvs  data nvs  0x9000  0x6000

Nothing outside the NVS partition is touched.

A device can hold more than one ota_url entry: NVS appends a new copy when the
value changes and leaves the old one in place, so which copy is live depends on
the order it resolves them. This rewrites every live copy, which is what you
want when moving a device to a different server.

How NVS lays a string entry out, as of ESP-IDF v5.5.3:

    page       4096 bytes: 32-byte header, 32-byte entry-state bitmap, then
               126 entries of 32 bytes
    entry      nsIndex 0, datatype 1, span 2, chunkIndex 3, crc32 4-7,
               key[16] 8-23, union 24-31
    SZ union   dataSize u16 at 24, reserved u16 at 26, dataCrc32 u32 at 28
    the bytes  start at the *next* entry, run for dataSize bytes, and are
               padded with 0xff to the end of the span
    entry CRC  esp_rom_crc32_le over bytes[0:4], then bytes[8:24], then
               bytes[24:32], starting from 0xffffffff
    data CRC   esp_rom_crc32_le over the string bytes, starting from
               0xffffffff

Both CRC positions and the length field are easy to get wrong in ways that
leave an entry the device silently ignores, so this recomputes them and counts
only entries that pass their own CRCs as live.

An address is replaced in place, so it must fit the entry's span. Every IPv4
address and port does; a long hostname might not.
"""

from __future__ import annotations

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
KEY = b"ota_url"
TYPE_SZ = 0x21


def entry_crc(entry: bytes) -> int:
    """Item::calculateCrc32() from nvs_types.cpp, in Python's crc32 terms.

    esp_rom_crc32_le(crc, buf, n) is zlib.crc32(buf, crc): both invert on the
    way in and on the way out. The crc32 field itself is skipped.
    """
    running = zlib.crc32(entry[0:4], 0xFFFFFFFF)
    running = zlib.crc32(entry[8:24], running)
    return zlib.crc32(entry[24:32], running) & 0xFFFFFFFF


def data_crc(payload: bytes) -> int:
    """Item::calculateCrc32(data, size) -- the string's own checksum."""
    return zlib.crc32(payload, 0xFFFFFFFF) & 0xFFFFFFFF


def live_strings(region: bytes):
    """Yield (offset, span, payload, room) for every intact ota_url entry.

    Walking the page grid rather than scanning for the key matters: a
    continuation block holds raw string bytes and could contain the key's
    letters, and a hit there would have no header to read.
    """
    for page in range(NVS_SIZE // PAGE_SIZE):
        base = page * PAGE_SIZE + PAGE_HEADER + PAGE_BITMAP
        for slot in range(ENTRIES_PER_PAGE):
            at = base + slot * ENTRY_SIZE
            entry = bytes(region[at:at + ENTRY_SIZE])
            # 0xff is erased flash; 0x00 is an untyped slot.
            if entry[1] in (0xFF, 0x00):
                continue
            if entry[8:24].rstrip(b"\x00") != KEY:
                continue
            span, datatype = entry[2], entry[1]
            if datatype != TYPE_SZ or span < 2:
                print(f"key {KEY.decode()} at {at:#06x} is type {datatype:#04x} "
                      f"span {span}, not a string; refusing to guess",
                      file=sys.stderr)
                raise SystemExit(1)
            if struct.unpack_from("<I", entry, 4)[0] != entry_crc(entry):
                # NVS leaves superseded entries behind; one that fails its own
                # checksum is not a copy the device would read.
                print(f"key {KEY.decode()} at {at:#06x} fails its entry CRC; "
                      f"skipping the dead copy", file=sys.stderr)
                continue
            length = struct.unpack_from("<H", entry, 24)[0]
            room = ENTRY_SIZE * (span - 1)
            if length > room:
                print(f"key {KEY.decode()} at {at:#06x} claims {length} bytes "
                      f"in a {room}-byte span; refusing to guess", file=sys.stderr)
                raise SystemExit(1)
            payload = bytes(region[at + ENTRY_SIZE:at + ENTRY_SIZE + length])
            if struct.unpack_from("<I", entry, 28)[0] != data_crc(payload):
                print(f"key {KEY.decode()} at {at:#06x} fails its data CRC; "
                      f"skipping the dead copy", file=sys.stderr)
                continue
            yield at, span, payload, room


def rewrite(region: bytearray, new: bytes) -> None:
    """Put `new` into every live ota_url entry, repairing both CRCs."""
    for at, span, _, room in live_strings(region):
        # The payload area is the span minus its own entry, padded with the
        # same 0xff an erase leaves behind.
        start = at + ENTRY_SIZE
        region[start:start + room] = new.ljust(room, b"\xff")
        struct.pack_into("<H", region, at + 24, len(new))
        struct.pack_into("<I", region, at + 28, data_crc(new))
        struct.pack_into("<I", region, at + 4,
                         entry_crc(bytes(region[at:at + ENTRY_SIZE])))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True,
                        help="serial port, e.g. /dev/cu.usbmodem1101")
    parser.add_argument("--address", help="host:port, e.g. 192.168.0.125:8096")
    parser.add_argument("--esptool", default="python -m esptool")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="print the stored address and stop")
    args = parser.parse_args()
    if not args.list and not args.address:
        parser.error("--address is required unless --list is given")

    reader = args.esptool.split()
    # esptool writes a progress log to stdout, so the image goes through a
    # temporary file rather than a pipe; asking for "-" yields the log instead.
    with tempfile.TemporaryDirectory() as work:
        image = pathlib.Path(work) / "nvs.bin"
        subprocess.run(reader + ["--chip", "esp32c3", "-p", args.port,
                                 "read_flash", hex(NVS_OFFSET), hex(NVS_SIZE),
                                 str(image)], check=True)
        blob = image.read_bytes()
        if len(blob) != NVS_SIZE:
            print(f"read {len(blob)} bytes, expected {NVS_SIZE}", file=sys.stderr)
            return 1
        region = bytearray(blob)

        found = [(at, payload, room) for at, _, payload, room in live_strings(region)]
        if not found:
            print("no live ota_url entry in NVS; set the address on the device's "
                  "setup page first, then point it elsewhere with this tool",
                  file=sys.stderr)
            return 1
        for at, payload, room in found:
            print(f"{at:#06x}: {payload.decode('ascii', 'replace')} "
                  f"({len(payload)} bytes, {room} of room)")

        if args.list:
            return 0

        new = args.address.encode("ascii")
        if all(new == payload for _, payload, _ in found):
            print("already set to that address; nothing to do")
            return 0
        for at, _, room in found:
            if len(new) > room:
                print(f"new address is {len(new)} bytes and entry {at:#06x} "
                      f"holds {room}; a shorter or equal address is required",
                      file=sys.stderr)
                return 1

        rewrite(region, new)
        print(f"new address    : {new.decode('ascii')}")

        if args.dry_run:
            print("dry run; nothing written")
            return 0

        image.write_bytes(bytes(region))
        subprocess.run(reader + ["--chip", "esp32c3", "-p", args.port,
                                 "write_flash", hex(NVS_OFFSET), str(image)],
                       check=True)
    print("written; the device picks it up on its next connection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
