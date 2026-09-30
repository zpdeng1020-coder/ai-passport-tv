// Playback demo: no network, no server. A clip embedded in the image is drawn
// by the product's own steps (inflate -> palette expand -> raw panel, one
// transfer in flight) and the measured frame rate is drawn in the letterbox
// bars above and below the picture, so it costs the picture nothing.
#include "av_demo.h"
#include "av_protocol.h"
#include "bsp_display.h"
#include "ui_text.h"
#include "miniz.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdio.h>
#include <string.h>

static const char *TAG = "av_demo";

extern const uint8_t clip_start[] asm("_binary_demo_clip_bin_start");
extern const uint8_t clip_end[]   asm("_binary_demo_clip_bin_end");

#define PANEL_W 320
#define PANEL_H 240
#define BAR_ROWS ((PANEL_H - (int)AV_VIDEO_HEIGHT) / 2)
#define VIDEO_Y BAR_ROWS
#define STRIPE_BYTES (PANEL_W * (int)AV_STRIPE_ROWS * 2)

static uint32_t rd32(const uint8_t *p) {
    return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

// Paint one letterbar: black, with one line of text.
static void draw_bar(uint8_t *buf, int y, const char *text, uint16_t colour) {
    ui_surface_t s = { .pixels = buf, .width = PANEL_W, .origin_y = y, .rows = BAR_ROWS };
    ui_text_fill(s, 0, y, PANEL_W, BAR_ROWS, 0x0000u);
    ui_text_draw(s, 8, y + (BAR_ROWS - (int)UI_TEXT_LINE_H) / 2, text, colour, 0x0000u);
    bsp_display_raw_submit(y, BAR_ROWS, buf, 200);
    bsp_display_raw_wait(200);
}

// Same result as av_expand_indexed, four pixels per step. `swapped` holds each
// palette entry with its bytes already in wire order, so one 16-bit store puts
// high-then-low on the little-endian core, and two 32-bit stores write four
// pixels. Backwards for the same reason as the original: the writes for
// indices 4k..4k+3 land at bytes 8k.. and never on an unread index below 4k.
// Needs a 4-byte-aligned buffer and a pixel count divisible by 4.
static void expand_fast(uint8_t *buf, size_t pixels, const uint16_t *swapped) {
    uint32_t *out = (uint32_t *)buf;
    const uint32_t *in = (const uint32_t *)buf;
    for (size_t k = pixels / 4; k-- > 0;) {
        uint32_t w = in[k];
        out[2 * k + 1] = (uint32_t)swapped[(w >> 16) & 0xff] | ((uint32_t)swapped[w >> 24] << 16);
        out[2 * k]     = (uint32_t)swapped[w & 0xff] | ((uint32_t)swapped[(w >> 8) & 0xff] << 16);
    }
}

static const char *TIER[3] = { "LOW", "MID", "HIGH" };

static void halt(const char *why) {
    ESP_LOGE(TAG, "%s", why);
    vTaskDelay(portMAX_DELAY);
}

void av_demo_main(void) {
    const size_t clip_len = (size_t)(clip_end - clip_start);
    ESP_LOGW(TAG, "playback demo: clip %u bytes", (unsigned)clip_len);
    if (bsp_display_init() != ESP_OK || bsp_display_raw_claim() != ESP_OK) halt("display init/claim failed");
    bsp_display_backlight(80);

    if (clip_len < 8 || memcmp(clip_start, "DCL1", 4) != 0) halt("bad clip");
    const unsigned frames = clip_start[4] | (clip_start[5] << 8);
    const uint8_t *lengths = clip_start + 8;
    const uint8_t *body = lengths + 4u * frames;
    uint32_t *offset = heap_caps_malloc(4u * (frames + 1u), MALLOC_CAP_8BIT);
    uint8_t *bar = heap_caps_malloc((size_t)PANEL_W * BAR_ROWS * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    uint8_t *stripe[2] = {
        heap_caps_malloc(STRIPE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
        heap_caps_malloc(STRIPE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA) };
    tinfl_decompressor *infl = heap_caps_malloc(sizeof(*infl), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint16_t palette[AV_PALETTE_ENTRIES];
    if (!offset || !bar || !stripe[0] || !stripe[1] || !infl) halt("out of memory");
    offset[0] = 0;
    for (unsigned i = 0; i < frames; i++) offset[i + 1] = offset[i] + rd32(lengths + 4u * i);
    if ((size_t)(body - clip_start) + offset[frames] != clip_len) halt("clip size disagrees with its table");
    for (unsigned i = 0; i < AV_PALETTE_ENTRIES; i++) palette[i] = av_palette_rgb565((uint8_t)i);

    uint16_t *swapped = heap_caps_malloc(2 * AV_PALETTE_ENTRIES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!swapped) halt("out of memory");
    for (unsigned i = 0; i < AV_PALETTE_ENTRIES; i++) swapped[i] = __builtin_bswap16(palette[i]);
    {   // Correctness and cost of the fast expander, on a full stripe of varied indices.
        for (size_t i = 0; i < AV_STRIPE_PIXELS; i++) stripe[0][i] = (uint8_t)(i * 7u + (i >> 3));
        memcpy(stripe[1], stripe[0], AV_STRIPE_PIXELS);
        uint32_t c0 = esp_cpu_get_cycle_count();
        av_expand_indexed(stripe[0], AV_STRIPE_PIXELS, palette);
        uint32_t c1 = esp_cpu_get_cycle_count();
        expand_fast(stripe[1], AV_STRIPE_PIXELS, swapped);
        uint32_t c2 = esp_cpu_get_cycle_count();
        bool same = memcmp(stripe[0], stripe[1], AV_STRIPE_PIXELS * 2) == 0;
        ESP_LOGW(TAG, "EXPAND_BENCH same=%d orig_cyc_per_px_x100=%u fast_cyc_per_px_x100=%u",
                 (int)same, (unsigned)((c1 - c0) * 100u / AV_STRIPE_PIXELS),
                 (unsigned)((c2 - c1) * 100u / AV_STRIPE_PIXELS));
        if (!same) halt("fast expander disagrees with the original");
    }

    memset(bar, 0, (size_t)PANEL_W * BAR_ROWS * 2);
    draw_bar(bar, 0, "PLAYBACK DEMO", UI_COLOR_TEXT);
    draw_bar(bar, PANEL_H - BAR_ROWS, "measuring...", UI_COLOR_DIM);

    unsigned window_frames = 0, total_frames = 0, next = 0, live = 0, fail_count = 0;
    uint64_t t_inflate = 0, t_expand = 0, t_panel = 0;
    int64_t window_start = esp_timer_get_time();

    const unsigned tiers = (clip_start[6] | (clip_start[7] << 8)) ? (clip_start[6] | (clip_start[7] << 8)) : 1;
    const unsigned per = frames / tiers;
    for (;;) {
        for (unsigned f = 0; f < frames; f++) {
            const unsigned tier = f / per;
            if (f % per == 0) { window_frames = 0; t_inflate = t_expand = t_panel = 0; window_start = esp_timer_get_time(); }
            av_video_t v;
            if (!av_video_decode(body + offset[f], offset[f + 1] - offset[f], &v) || v.count != AV_STRIPES) {
                if (++fail_count < 5) ESP_LOGE(TAG, "frame %u malformed", f);
                continue;
            }
            for (unsigned n = 0; n < v.count; n++) {
                const uint8_t *src; size_t avail;
                if (!av_video_stripe(&v, n, &src, &avail)) continue;
                uint8_t *buf = stripe[next]; next ^= 1u;
                size_t produced = AV_STRIPE_PIXELS, consumed = avail;
                int64_t t0 = esp_timer_get_time();
                tinfl_init(infl);
                tinfl_status st = tinfl_decompress(infl, src, &consumed, buf, buf, &produced,
                    TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
                int64_t t1 = esp_timer_get_time();
                if (st != TINFL_STATUS_DONE || produced != AV_STRIPE_PIXELS) {
                    if (++fail_count < 5) ESP_LOGE(TAG, "stripe %u inflate st=%d", n, (int)st);
                    continue;
                }
                expand_fast(buf, AV_STRIPE_PIXELS, swapped);
                int64_t t2 = esp_timer_get_time();
                if (bsp_display_raw_submit_nowait(VIDEO_Y + (int)(n * AV_STRIPE_ROWS),
                                                  (int)AV_STRIPE_ROWS, buf) == ESP_OK) live++;
                if (live >= 2) { bsp_display_raw_drain(1, 200); live--; }
                int64_t t3 = esp_timer_get_time();
                t_inflate += (uint64_t)(t1 - t0);
                t_expand += (uint64_t)(t2 - t1);
                t_panel += (uint64_t)(t3 - t2);
            }
            window_frames++; total_frames++;

            int64_t now = esp_timer_get_time();
            if ((f + 1) % per == 0) {
                unsigned fps10 = (unsigned)((uint64_t)window_frames * 10000000ull / (uint64_t)(now - window_start));
                unsigned n = window_frames ? window_frames : 1;
                unsigned inf = (unsigned)(t_inflate / n / 100), exp = (unsigned)(t_expand / n / 100),
                         pnl = (unsigned)(t_panel / n / 100);
                char l1[48], l2[64];
                snprintf(l1, sizeof l1, "%s  FPS %u.%u", TIER[tier % 3], fps10 / 10, fps10 % 10);
                snprintf(l2, sizeof l2, "inf %u.%u exp %u.%u pnl %u.%u ms",
                         inf / 10, inf % 10, exp / 10, exp % 10, pnl / 10, pnl % 10);
                ESP_LOGW(TAG, "DEMO tier=%s fps_x10=%u frames=%u %s", TIER[tier % 3], fps10, window_frames, l2);
                bsp_display_raw_drain(live, 200); live = 0;   // the bars share the bus
                draw_bar(bar, 0, l1, UI_COLOR_TEXT);
                draw_bar(bar, PANEL_H - BAR_ROWS, l2, UI_COLOR_DIM);
            }
        }
    }
}
