// Baseline JPEG decode benchmark for the ESP32-C3 (CONFIG_AV_JPEG_BENCH).
//
// Question it answers: how long does one 320x180 4:2:0 JPEG take to decode to
// RGB565 on this chip, at about 5.5 KB a frame? The decoder is the ROM's TJpgDec
// (esp_rom/tjpgd.h), which is small and old. It stands for "a simple decoder";
// an optimised library may be faster, and this does not measure one.
//
// Two passes over the same frames:
//   decode   decode + RGB888 -> RGB565 into a 320x16 strip buffer, no panel
//   display  the same, and every finished MCU row is sent to the panel by DMA,
//            so the picture can be seen and the DMA overlap is included
// Both print per-tier mean and worst frame time and the frame rate that implies
// if nothing else ran. Audio, network and the receive path are not running.
#include "sdkconfig.h"
#ifdef CONFIG_AV_JPEG_BENCH
#include "jpeg_bench.h"
#include "bsp_display.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "rom/tjpgd.h"
#include <stdio.h>
#include <string.h>

static const char *TAG = "jpeg_bench";

extern const uint8_t jpg_start[] asm("_binary_jpeg_test_bin_start");
extern const uint8_t jpg_end[]   asm("_binary_jpeg_test_bin_end");

#define PANEL_H 240
#define IMG_W 320
#define IMG_H 180
#define STRIP_ROWS 16
#define VIDEO_Y ((PANEL_H - IMG_H) / 2)

typedef struct {
    const uint8_t *p;
    size_t left;
    uint16_t *strip[2];
    int cur;
    bool display;
    unsigned live;
    int64_t submit_us;
} ctx_t;

static uint32_t rd32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

static UINT in_cb(JDEC *jd, BYTE *buf, UINT n) {
    ctx_t *c = (ctx_t *)jd->device;
    if (n > c->left) n = (UINT)c->left;
    if (buf) memcpy(buf, c->p, n);
    c->p += n; c->left -= n;
    return n;
}

// Output arrives as MCU rectangles, left to right, top to bottom, in RGB888.
static UINT out_cb(JDEC *jd, void *bitmap, JRECT *r) {
    ctx_t *c = (ctx_t *)jd->device;
    const uint8_t *s = (const uint8_t *)bitmap;
    uint16_t *strip = c->strip[c->cur];
    const int w = r->right - r->left + 1, h = r->bottom - r->top + 1;
    const int y0 = r->top - (r->top / STRIP_ROWS) * STRIP_ROWS;
    for (int y = 0; y < h; y++) {
        uint16_t *d = strip + (size_t)(y0 + y) * IMG_W + r->left;
        for (int x = 0; x < w; x++, s += 3) {
            uint16_t v = (uint16_t)(((s[0] & 0xF8) << 8) | ((s[1] & 0xFC) << 3) | (s[2] >> 3));
            d[x] = __builtin_bswap16(v);
        }
    }
    if (c->display && r->right == IMG_W - 1) {                  // last block of this MCU row
        int64_t t0 = esp_timer_get_time();
        const int top = (r->top / STRIP_ROWS) * STRIP_ROWS;
        const int rows = r->bottom - top + 1;
        if (bsp_display_raw_submit_nowait(VIDEO_Y + top, rows, strip) == ESP_OK) c->live++;
        if (c->live >= 2) { bsp_display_raw_drain(1, 200); c->live--; }
        c->cur ^= 1;
        c->submit_us += esp_timer_get_time() - t0;
    }
    return 1;
}

static void halt(const char *why) { ESP_LOGE(TAG, "%s", why); vTaskDelay(portMAX_DELAY); }

void jpeg_bench_main(void) {
    const size_t len = (size_t)(jpg_end - jpg_start);
    if (len < 8 || memcmp(jpg_start, "JPG1", 4) != 0) halt("bad jpeg_test.bin");
    const unsigned frames = jpg_start[4] | (jpg_start[5] << 8);
    const unsigned tiers = jpg_start[6] | (jpg_start[7] << 8);
    const unsigned per = frames / (tiers ? tiers : 1);
    const uint8_t *lens = jpg_start + 8, *body = lens + 4u * frames;
    uint32_t *off = heap_caps_malloc(4u * (frames + 1u), MALLOC_CAP_8BIT);
    ctx_t c = {0};
    for (int i = 0; i < 2; i++) c.strip[i] = heap_caps_malloc((size_t)IMG_W * STRIP_ROWS * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    void *pool = heap_caps_malloc(8192, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!off || !c.strip[0] || !c.strip[1] || !pool) halt("out of memory");
    off[0] = 0;
    for (unsigned i = 0; i < frames; i++) off[i + 1] = off[i] + rd32(lens + 4u * i);
    if ((size_t)(body - jpg_start) + off[frames] != len) halt("clip size disagrees with its table");
    ESP_LOGW(TAG, "clip: %u frames, %u tiers, %u bytes; heap=%u", frames, tiers, (unsigned)len, (unsigned)esp_get_free_heap_size());

    if (bsp_display_init() != ESP_OK || bsp_display_raw_claim() != ESP_OK) halt("display init/claim failed");
    bsp_display_backlight(80);

    for (int pass = 0; pass < 2; pass++) {
        c.display = pass == 1;
        for (unsigned t = 0; t < tiers; t++) {
            uint64_t sum = 0, sub = 0; uint32_t worst = 0, bytes = 0; unsigned ok = 0;
            const int REPEAT = 10;
            for (int rep = 0; rep < REPEAT; rep++) {
                for (unsigned f = t * per; f < (t + 1) * per; f++) {
                    c.p = body + off[f]; c.left = off[f + 1] - off[f]; c.cur = 0; c.live = 0; c.submit_us = 0;
                    JDEC jd;
                    int64_t t0 = esp_timer_get_time();
                    JRESULT r = jd_prepare(&jd, in_cb, pool, 8192, &c);
                    if (r == JDR_OK) r = jd_decomp(&jd, out_cb, 0);
                    if (c.display) bsp_display_raw_drain(c.live, 200);
                    uint32_t us = (uint32_t)(esp_timer_get_time() - t0);
                    if (r != JDR_OK) { ESP_LOGE(TAG, "frame %u failed r=%d", f, (int)r); continue; }
                    sum += us; sub += (uint64_t)c.submit_us; if (us > worst) worst = us; ok++;
                    if (rep == 0) bytes += off[f + 1] - off[f];
                    vTaskDelay(1);   // let IDLE feed the watchdog; outside the timed region
                }
            }
            if (!ok) continue;
            const unsigned mean = (unsigned)(sum / ok);
            ESP_LOGW(TAG, "JPEG_BENCH pass=%s tier=%u frames=%u avg_bytes=%u mean_ms_x10=%u worst_ms_x10=%u fps_x10=%u panel_wait_ms_x10=%u",
                     c.display ? "display" : "decode", t, ok, bytes / per, mean / 100u, worst / 100u,
                     (unsigned)(10000000ull / mean), (unsigned)(sub / ok / 100u));
        }
    }
    ESP_LOGW(TAG, "JPEG_BENCH done");
    vTaskDelay(portMAX_DELAY);
}
#endif
