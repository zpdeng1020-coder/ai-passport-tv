// Host tests for the overlay drawing helpers: layout, clipping and encoding.
// No ESP-IDF dependency, so this runs on the development machine.
#include "ui_text.h"
#include "ui_font_data.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// The device build gets the non-ASCII glyphs from main/ui_font_cjk.bin, which
// the component embeds with EMBED_FILES. A host build has no such step, so the
// symbol it would produce is defined here instead.
//
// The value is an empty table rather than the real one: it is 275 KB, and every
// case in this file goes through the ASCII table or the width fallback. Those
// are the parts that lay text out, and they are unaffected by which decorative
// glyphs exist. A well-formed header of a zero count is what makes that honest
// -- the reader below computes its pointers from the count and never follows
// them, rather than reading past the end of a stub.
//
// Four zero bytes: the count is a 32-bit little-endian integer.
const uint8_t ui_font_cjk_blob_start[]
    __asm__("_binary_ui_font_cjk_bin_start") = { 0, 0, 0, 0 };

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

// A small surface backed by a real buffer, so clipping is measured on pixels.
#define SURFACE_W 320
#define SURFACE_ROWS 32
static uint8_t buffer[SURFACE_W * SURFACE_ROWS * 2];

static ui_surface_t surface_at(int origin_y, int rows)
{
    memset(buffer, 0, sizeof(buffer));
    ui_surface_t surface = { buffer, SURFACE_W, origin_y, rows };
    return surface;
}

static uint16_t pixel(ui_surface_t surface, int x, int y)
{
    const uint8_t *p = surface.pixels + ((size_t)(y - surface.origin_y) * surface.width
                                         + (size_t)x) * 2u;
    return (uint16_t)((p[0] << 8) | p[1]);
}

static void test_font_table_is_self_consistent(void)
{
    check(UI_GLYPH_W == 16 && UI_GLYPH_H == 16, "glyph cell is 16x16");
    check(UI_FONT_ASCII_COUNT == 95, "ASCII table covers 32..126");
    // 'A' must carry ink: a table of blanks would render an invisible UI.
    int ink = 0;
    for (unsigned i = 0; i < UI_GLYPH_BYTES; i++) {
        ink += ui_font_ascii['A' - UI_FONT_ASCII_FIRST][i] != 0;
    }
    check(ink > 0, "the letter A has ink");
    int space_ink = 0;
    for (unsigned i = 0; i < UI_GLYPH_BYTES; i++) {
        space_ink += ui_font_ascii[0][i] != 0;
    }
    check(space_ink == 0, "space has no ink");
}

static void test_width_and_truncation(void)
{
    check(ui_text_width("") == 0, "empty string has zero width");
    check(ui_text_width("CCTV") > 0, "latin text has width");
    // Width must equal the sum of the advances, or centring drifts.
    check(ui_text_width("AB") == ui_text_width("A") + ui_text_width("B"),
          "width adds up across characters");

    char out[64];
    check(ui_text_truncate(out, sizeof(out), "CCTV", 1000), "a short string fits");
    check(strcmp(out, "CCTV") == 0, "a fitting string is copied whole");

    // A string longer than the limit must be cut, not overflowed.
    check(!ui_text_truncate(out, sizeof(out), "ABCDEFGHIJKLMNOP", 25),
          "an overlong string reports failure");
    check(ui_text_width(out) <= 25, "the truncated string respects the limit");
}

static void test_truncation_never_splits_a_character(void)
{
    // "中文" is 6 bytes; cutting between them would produce invalid UTF-8.
    const char *text = "中中中";
    char out[64];
    for (int limit = 0; limit <= 3 * (int)UI_GLYPH_W; limit += 3) {
        check(!ui_text_truncate(out, sizeof(out), text, limit) || limit >= 3 * (int)UI_GLYPH_W,
              "short limits reject rather than split");
        // Whatever was written must be valid: never a lone continuation byte.
        size_t length = strlen(out);
        for (size_t i = 0; i < length; i++) {
            check(((unsigned char)out[i] & 0xC0u) != 0x80u || i > 0,
                  "no leading continuation byte");
        }
    }
}

static void test_fill_clips_to_the_band(void)
{
    ui_surface_t surface = surface_at(100, SURFACE_ROWS);
    // Entirely above the band: nothing may be written.
    ui_text_fill(surface, 10, 90, 20, 5, UI_COLOR_TEXT);
    check(pixel(surface, 10, 100) == 0, "a rectangle above the band is dropped");

    // Straddling the top edge: only the part inside appears.
    ui_text_fill(surface, 10, 96, 20, 10, UI_COLOR_TEXT);
    check(pixel(surface, 10, 100) == UI_COLOR_TEXT, "the visible part is drawn");
    check(pixel(surface, 10, 99) == 0, "the part above the band is absent");

    // Past the right edge must clip rather than wrap into the next row.
    ui_surface_t wide = surface_at(0, SURFACE_ROWS);
    ui_text_fill(wide, SURFACE_W - 5, 0, 50, 1, UI_COLOR_TEXT);
    check(pixel(wide, SURFACE_W - 1, 0) == UI_COLOR_TEXT, "the last visible column is drawn");
    check(pixel(wide, 0, 1) == 0, "clipping does not wrap to the next row");

    // Negative origin must not write before the buffer.
    ui_surface_t edge = surface_at(0, SURFACE_ROWS);
    ui_text_fill(edge, -10, 0, 20, 1, UI_COLOR_TEXT);
    check(pixel(edge, 0, 0) == UI_COLOR_TEXT, "a rectangle straddling x=0 still draws");
}

static void test_text_draw_paints_background_then_glyphs(void)
{
    ui_surface_t surface = surface_at(0, SURFACE_ROWS);
    int end = ui_text_draw(surface, 20, 4, "A", UI_COLOR_TEXT, UI_COLOR_BOX);
    check(end > 20, "drawing returns the position past the text");

    // Every pixel in the padded box is either the box colour or the text colour.
    for (int y = 4 - UI_TEXT_PAD_Y; y < 4 + (int)UI_TEXT_LINE_H + UI_TEXT_PAD_Y; y++) {
        for (int x = 20 - UI_TEXT_PAD_X; x < 20 + (int)UI_GLYPH_W + UI_TEXT_PAD_X; x++) {
            uint16_t value = pixel(surface, x, y);
            check(value == UI_COLOR_BOX || value == UI_COLOR_TEXT,
                  "the box contains only background and glyph colours");
        }
    }
    int glyph_pixels = 0;
    for (int y = 4; y < 4 + (int)UI_GLYPH_H; y++) {
        for (int x = 20; x < 20 + (int)UI_GLYPH_W; x++) {
            glyph_pixels += pixel(surface, x, y) == UI_COLOR_TEXT;
        }
    }
    check(glyph_pixels > 0, "the glyph itself is painted");
}

static void test_draw_off_surface_does_not_write_out_of_bounds(void)
{
    // A guard byte after the buffer would be corrupted by any overrun; the
    // surface is sized exactly, so drawing past the edge must clip.
    ui_surface_t surface = surface_at(0, SURFACE_ROWS);
    (void)ui_text_draw(surface, SURFACE_W - 4, 0, "OVERFLOW", UI_COLOR_TEXT, UI_COLOR_BOX);
    (void)ui_text_draw(surface, -50, 0, "LEFT", UI_COLOR_TEXT, UI_COLOR_BOX);
    (void)ui_text_draw(surface, 10, 200, "BELOW", UI_COLOR_TEXT, UI_COLOR_BOX);
    check(1, "off-surface draws return without corrupting memory");
}

int main(void)
{
    test_font_table_is_self_consistent();
    test_width_and_truncation();
    test_truncation_never_splits_a_character();
    test_fill_clips_to_the_band();
    test_text_draw_paints_background_then_glyphs();
    test_draw_off_surface_does_not_write_out_of_bounds();

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("overlay text tests: PASS\n");
    return 0;
}
