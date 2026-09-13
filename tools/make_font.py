#!/usr/bin/env python3
"""Generate the bitmap font the on-screen overlay draws with.

The player owns the panel through the raw path, and the BSP refuses to
initialise LVGL once raw has claimed it, so the overlay cannot use an LVGL
label. It draws glyphs itself, and this script produces the table it reads.

Each glyph is a fixed 16x16 cell so drawing is plain arithmetic. ASCII goes into
a C header; non-ASCII goes into a binary blob the build embeds, because the full
set is tens of thousands of bytes and as C source it would be a megabyte of hex
that slows every rebuild. See ui_text.c for the blob layout.

Coverage is the whole of GB2312 plus every non-ASCII character in the channel
file. Generating only the characters in channels.txt is what produced the tofu:
the table was made from an older file, so every channel added afterwards drew
its new characters as gaps.

Usage:
    python3 tools/make_font.py
    python3 tools/make_font.py --text-file channels.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is required: python3 -m pip install Pillow")

CELL = 16
LATIN_FONT = "/System/Library/Fonts/Menlo.ttc"
CJK_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
# A code point in the private use area: no font maps it, so it renders the
# font's "missing glyph" box. Comparing against it is how a character the face
# cannot draw is told apart from one it can.
NOTDEF_PROBE = ""


def _rasterise(font: ImageFont.FreeTypeFont, char: str) -> list[int] | None:
    """Rasterise one character centred in a cell, or None if it will not fit."""
    left, top, right, bottom = font.getbbox(char)
    width, height = right - left, bottom - top
    if width > CELL or height > CELL:
        return None
    image = Image.new("L", (CELL, CELL), 0)
    ImageDraw.Draw(image).text(((CELL - width) // 2 - left, (CELL - height) // 2 - top),
                               char, font=font, fill=255)
    pixels = image.load()
    rows = []
    for y in range(CELL):
        row = 0
        for x in range(CELL):
            if pixels[x, y] > 128:
                row |= 1 << (CELL - 1 - x)
        rows.append(row)
    return rows


class Face:
    """One font file, with the missing-glyph bitmap already measured."""

    def __init__(self, path: str, size: int):
        self.font = ImageFont.truetype(path, size)
        self.notdef = _rasterise(self.font, NOTDEF_PROBE)

    def rows(self, char: str) -> list[int] | None:
        rows = _rasterise(self.font, char)
        if rows is None:
            return None
        # A glyph identical to the missing-glyph box means this face has no such
        # character. Keeping it would draw a box on screen, which is the tofu
        # this script exists to avoid; dropping it leaves a blank the size of the
        # character instead, and lets a later face provide the real glyph.
        if rows == self.notdef and not char.isspace():
            return None
        return rows


def advance_of(rows: list[int]) -> int:
    """How far the pen moves after this glyph.

    A 16-pixel advance for every ASCII character would put only 20 of them
    across a 320-pixel screen and look scattered, so narrow characters advance
    by their own ink; wide characters keep the full cell, which is the only
    sensible spacing for CJK.
    """
    columns = [x for x in range(CELL)
               if any(rows[y] & (1 << (CELL - 1 - x)) for y in range(CELL))]
    if not columns:
        return 6  # space and any other blank glyph
    width = columns[-1] - columns[0] + 1
    return CELL if width >= CELL - 2 else width + 2


def rows_to_bytes(rows: list[int]) -> list[int]:
    """Big-endian 16-bit rows, so the C table reads left to right."""
    out = []
    for row in rows:
        out.extend(((row >> 8) & 0xFF, row & 0xFF))
    return out


def c_bytes(values: list[int], indent: str = "    ") -> str:
    lines = []
    for start in range(0, len(values), 12):
        chunk = ", ".join(f"0x{value:02X}" for value in values[start:start + 12])
        lines.append(f"{indent}{chunk},")
    return "\n".join(lines)


def gb2312_characters() -> list[str]:
    """Every character GB2312 encodes: 6763 hanzi plus punctuation and kana.

    This is the set a Chinese TV channel name is written in, so covering it means
    a new channel needs font regeneration only if it uses a character from
    outside it -- a rare hanzi, or another script.
    """
    characters = []
    for high in range(0xA1, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                characters.append(bytes([high, low]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return characters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "main" / "ui_font_data.h")
    parser.add_argument("--blob", type=Path,
                        default=Path(__file__).resolve().parents[1] / "main" / "ui_font_cjk.bin")
    parser.add_argument("--text", default="", help="extra characters to include")
    parser.add_argument("--text-file", type=Path,
                        help="file whose non-ASCII characters should be included")
    parser.add_argument("--size", type=int, default=14, help="latin point size")
    parser.add_argument("--cjk-size", type=int, default=15, help="CJK point size")
    parser.add_argument("--no-gb2312", action="store_true",
                        help="include only the characters actually requested")
    args = parser.parse_args()

    wanted = set(args.text)
    if args.text_file:
        wanted |= {char for char in args.text_file.read_text(encoding="utf-8")
                   if ord(char) > 0x7F}
    if not args.no_gb2312:
        wanted |= set(gb2312_characters())
    # ASCII has its own table below.
    non_ascii = sorted({char for char in wanted if ord(char) > 0x7F}, key=ord)

    latin = Face(LATIN_FONT, args.size)
    cjk = Face(CJK_FONT, args.cjk_size)

    ascii_glyphs, ascii_advances = [], []
    for code in range(32, 127):
        rows = latin.rows(chr(code))
        if rows is None:
            # A blank cell rather than a shift of every later index.
            ascii_glyphs.append([0] * (CELL * 2))
            ascii_advances.append(6)
        else:
            ascii_glyphs.append(rows_to_bytes(rows))
            ascii_advances.append(advance_of(rows))

    entries, undrawable, from_latin = [], [], []
    for char in non_ascii:
        rows = cjk.rows(char)
        if rows is None:
            # Not in the CJK face: other scripts (Cyrillic, accented Latin) live
            # in the Latin face instead.
            rows = latin.rows(char)
            if rows is not None:
                from_latin.append(char)
        if rows is None:
            undrawable.append(char)
            continue
        entries.append((char, rows_to_bytes(rows), advance_of(rows)))

    # Sorted by code point so the device can binary-search instead of scanning:
    # a linear scan over thousands of glyphs on a single-core 160 MHz part would
    # be felt in the frame path.
    entries.sort(key=lambda entry: ord(entry[0]))

    blob = bytearray()
    blob += len(entries).to_bytes(4, "little")
    for char, _, _ in entries:
        blob += ord(char).to_bytes(4, "little")
    for _, _, advance in entries:
        blob.append(advance)
    for _, glyph, _ in entries:
        blob += bytes(glyph)
    args.blob.write_bytes(blob)

    parts = [
        "// Generated by tools/make_font.py - do not edit by hand.",
        f"// 1-bit glyphs, {CELL}x{CELL}, most significant bit leftmost, two bytes per row.",
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"#define UI_GLYPH_W {CELL}u",
        f"#define UI_GLYPH_H {CELL}u",
        f"#define UI_GLYPH_BYTES {CELL * 2}u",
        "",
        "// ASCII 32..126, indexed by (character - 32).",
        "#define UI_FONT_ASCII_FIRST 32",
        "#define UI_FONT_ASCII_COUNT 95",
        "static const uint8_t ui_font_ascii[UI_FONT_ASCII_COUNT][UI_GLYPH_BYTES] = {",
    ]
    for code, glyph in zip(range(32, 127), ascii_glyphs):
        # Never print the character itself: a backslash in a // comment would
        # splice the following line into it, and other characters can confuse
        # tooling. The code point identifies the glyph unambiguously.
        parts.append(f"    {{ // code {code}")
        parts.append(c_bytes(glyph, "      "))
        parts.append("    },")
    parts.append("};")
    parts.append("")
    parts.append("static const uint8_t ui_font_ascii_adv[UI_FONT_ASCII_COUNT] = {")
    parts.append(c_bytes(ascii_advances))
    parts.append("};")
    parts.append("")
    parts.append(f"// Every other glyph, {len(entries)} of them, is in {args.blob.name},")
    parts.append("// embedded in the image by the component's EMBED_FILES and searched by")
    parts.append("// code point at run time. Kept out of this header because the same data as")
    parts.append("// C source is about a megabyte of hex that every rebuild would reparse.")
    parts.append(f"#define UI_FONT_CJK_COUNT {len(entries)}")
    args.output.write_text("\n".join(parts) + "\n", encoding="utf-8")

    print(f"wrote {args.output}: {len(ascii_glyphs)} ASCII glyphs")
    print(f"wrote {args.blob}: {len(entries)} glyphs, {len(blob)} bytes "
          f"({len(blob) / 1024:.0f} KB)")
    if from_latin:
        print(f"  {len(from_latin)} glyphs came from the Latin face: "
              f"{''.join(from_latin[:40])}")
    if undrawable:
        print(f"  {len(undrawable)} characters no face could draw, left blank: "
              f"{''.join(undrawable[:40])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
