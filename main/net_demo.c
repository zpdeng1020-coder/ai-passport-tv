// Minimal network playback benchmark. It does not use the product's session,
// receive task or packet protocol: those are what is being measured against.
//
// Three steps, each visible on the panel:
//   1. On boot, colour bars and a status line -- the panel works, no network.
//   2. Server mode "bulk": the server sends bytes as fast as TCP allows, the
//      device only counts them. That is the link's ceiling.
//   3. Server mode "frames": the server sends demo_clip.bin frames with no
//      pacing. The device receives, inflates, expands and draws each one;
//      TCP back-pressure makes the server exactly as fast as the device. The
//      frame rate on screen is the ceiling of the whole network path.
//
// Wire (server -> device only):
//   "NDM1" | u8 mode (1 bulk, 2 frames)
//   bulk:   raw bytes until the server closes
//   frames: repeat { u32 le length | payload }, payload as in demo_clip.bin:
//           u8 first | u8 count | u16 be length[count] | zlib stripe * count
//
// Two numbers on the bottom bar say which side is the wall, every second:
//   net-wait  ms the draw task sat idle with no frame to work on (network or
//             server is slow)
//   dev-wait  ms the receiver sat idle with no free buffer (the device is slow)
#include "sdkconfig.h"
#ifdef CONFIG_AV_NET_DEMO
#include "net_demo.h"
#include "av_protocol.h"
#include "av_provision.h"
#include "av_server_addr.h"
#include "av_store.h"
#include "bsp_display.h"
#include "bsp_audio.h"
#include "ui_text.h"
#include "miniz.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include <stdlib.h>
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "esp_wifi.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "net_demo";

#define PANEL_W 320
#define PANEL_H 240
#define BAR_ROWS ((PANEL_H - (int)AV_VIDEO_HEIGHT) / 2)
#define VIDEO_Y BAR_ROWS
#define STRIPE_BYTES (PANEL_W * (int)AV_STRIPE_ROWS * 2)
#define FRAME_MAX AV_VIDEO_MAX
#define FRAME_BUFS 1   // TCP modes only; the UDP path uses the ring
// Receive ring: one block holding whole frames back to back. A frame is copied
// in as it arrives and stays until drawn; the block is reused in FIFO order, so
// the same memory holds about twice as many average frames as fixed 22 KB slots.
#ifndef RING_BYTES
#define RING_BYTES (48 * 1024)
#endif
#define RING_SLOTS 16
#ifndef REPORT_US
#define REPORT_US 5000000
#endif

enum { MODE_IDLE = 0, MODE_BULK = 1, MODE_FRAMES = 2, MODE_UDP = 3, MODE_UDPF = 4 };

static uint8_t *fb[FRAME_BUFS];
static uint32_t fb_len[FRAME_BUFS];
static QueueHandle_t free_q, full_q;
static uint8_t *ring;
typedef struct { uint32_t off, len; } ring_ent_t;
static ring_ent_t ring_q[RING_SLOTS];
static volatile uint32_t ring_head, ring_tail;      // entries: producer writes head, consumer reads tail
static volatile uint32_t ring_wr, ring_rd;          // byte cursors (producer / consumer)
static volatile uint32_t g_ring_high, g_ring_wrap;

static volatile int g_mode = MODE_IDLE;
static volatile const char *g_status = "starting";
static volatile uint32_t g_rx_bytes, g_rx_frames, g_rx_block_us;
static volatile uint32_t g_udp_bytes, g_udp_pkts, g_udp_lost, g_udp_ooo, g_udp_level;
static volatile uint32_t g_uf_incomplete, g_uf_nobuf;
static volatile uint32_t g_arr_min_us = 0xFFFFFFFFu, g_arr_max_us, g_arr_burst;
static int64_t g_arr_last;
static struct sockaddr_in g_peer;

// Same result as av_expand_indexed, four pixels per step (see main/av_demo.c).
static void expand_fast(uint8_t *buf, size_t pixels, const uint16_t *swapped) {
    uint32_t *out = (uint32_t *)buf;
    const uint32_t *in = (const uint32_t *)buf;
    for (size_t k = pixels / 4; k-- > 0;) {
        uint32_t w = in[k];
        out[2 * k + 1] = (uint32_t)swapped[(w >> 16) & 0xff] | ((uint32_t)swapped[w >> 24] << 16);
        out[2 * k]     = (uint32_t)swapped[w & 0xff] | ((uint32_t)swapped[(w >> 8) & 0xff] << 16);
    }
}

static void draw_bar(uint8_t *buf, int y, const char *text, uint16_t colour) {
    ui_surface_t s = { .pixels = buf, .width = PANEL_W, .origin_y = y, .rows = BAR_ROWS };
    ui_text_fill(s, 0, y, PANEL_W, BAR_ROWS, 0x0000u);
    ui_text_draw(s, 8, y + (BAR_ROWS - (int)UI_TEXT_LINE_H) / 2, text, colour, 0x0000u);
    esp_err_t a = bsp_display_raw_submit(y, BAR_ROWS, buf, 200);
    esp_err_t b = bsp_display_raw_wait(200);
    if (a != ESP_OK || b != ESP_OK) ESP_LOGE(TAG, "bar y=%d submit=%s wait=%s", y, esp_err_to_name(a), esp_err_to_name(b));
}

// ---------------------------------------------------------------- receiver

static bool read_full(int fd, uint8_t *dst, size_t n) {
    size_t got = 0;
    while (got < n) {
        int r = recv(fd, dst + got, n - got, 0);
        if (r <= 0) return false;
        got += (size_t)r;
        g_rx_bytes += (uint32_t)r;
    }
    return true;
}

static int dial(void) {
    av_server_addr_t target;
    if (!av_store_server_addr_load(&target)) { g_status = "no server address stored"; return -1; }
    struct addrinfo hints = {0}, *found = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char service[6];
    snprintf(service, sizeof service, "%u", (unsigned)target.port);
    if (getaddrinfo(target.host, service, &hints, &found) != 0 || !found) { g_status = "cannot resolve server"; return -1; }
    int fd = socket(found->ai_family, found->ai_socktype, found->ai_protocol);
    if (fd >= 0 && connect(fd, found->ai_addr, found->ai_addrlen) < 0) { close(fd); fd = -1; }
    if (fd >= 0) memcpy(&g_peer, found->ai_addr, sizeof g_peer);
    freeaddrinfo(found);
    if (fd < 0) { g_status = "connecting to server..."; return -1; }
    struct timeval tv = { .tv_sec = 5 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    int rcvbuf = 65535;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    return fd;
}

// Step 4: datagram receive. Server sends 1400-byte datagrams { u32 seq, u32
// offered KB/s, padding } at a stepped rate; we count bytes, sequence gaps
// (lost) and late arrivals (ooo). Ends when nothing arrives for 3 s.
static void udp_session(void) {
    int u = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (u < 0) return;
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(8096), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (bind(u, (struct sockaddr *)&me, sizeof me) < 0) { ESP_LOGE(TAG, "udp bind failed errno=%d", errno); close(u); return; }
    int rcvbuf = 16384;
    setsockopt(u, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    struct timeval tv = { .tv_usec = 200000 };
    setsockopt(u, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    static uint8_t pkt[1500];
    bool started = false;
    uint32_t expected = 0;
    int64_t last_rx = esp_timer_get_time();
    g_mode = MODE_UDP;
    g_status = "udp hello...";
    for (int hello = 0; hello < 50 || started; ) {
        if (!started) hello++;   // the server sends to us; no hello (inbound UDP may be firewalled)
        int r = recv(u, pkt, sizeof pkt, 0);
        int64_t now = esp_timer_get_time();
        if (r >= 8) {
            uint32_t seq = pkt[0] | (pkt[1] << 8) | (pkt[2] << 16) | ((uint32_t)pkt[3] << 24);
            g_udp_level = pkt[4] | (pkt[5] << 8) | (pkt[6] << 16) | ((uint32_t)pkt[7] << 24);
            if (!started) { started = true; expected = seq; g_status = "udp streaming"; }
            if (seq >= expected) { g_udp_lost += seq - expected; expected = seq + 1; }
            else g_udp_ooo++;
            g_udp_bytes += (uint32_t)r;
            g_udp_pkts++;
            last_rx = now;
        } else if (started && now - last_rx > 8000000) break;
    }
    close(u);
    g_mode = MODE_IDLE;
    g_status = "udp ended";
}

// ------------------------------------------------------------------- audio
// The product's audio, at the product's numbers: 16 kHz s16 mono, one 1280-byte
// chunk per 40 ms (32 KB/s), arriving on the SAME socket as the picture and read
// by the SAME task, so a picture datagram burst delays sound exactly as it does
// in the product. Audio is the device's clock: the I2S DMA consumes a chunk every
// 40 ms whatever the network does, and an empty queue means silence is played.
//
// Datagram: the 16-byte picture header with frame = AUD_MARK and total = the
// audio sequence number, then 1280 bytes of PCM.
#ifndef NET_AUDIO
#define NET_AUDIO 1
#endif
#ifndef AUD_QUEUE
#define AUD_QUEUE 8      // chunks of 40 ms; the product keeps 24 (30 KB) -- memory is the question
#endif
#ifndef AUD_PREBUF
#define AUD_PREBUF 4
#endif
#define AUD_MARK 0xFFFFFFF0u
#if NET_AUDIO
static QueueHandle_t aud_q;
static volatile bool g_aud_active;          // a session is delivering audio
static volatile uint32_t g_a_rx, g_a_lost, g_a_drop, g_a_silent, g_a_episodes, g_a_write_fail;
static volatile uint32_t g_a_feed_max_ms, g_a_qmin = 0xFFFFFFFFu, g_a_arr_max_ms;
static uint32_t a_expected; static int64_t a_last_arr;

static void audio_rx(const uint8_t *pkt, int r, int64_t now) {
    if (r != 16 + (int)AV_AUDIO_BYTES || !aud_q) return;
    uint32_t seq = pkt[8] | (pkt[9] << 8) | (pkt[10] << 16) | ((uint32_t)pkt[11] << 24);
    if (!g_aud_active) { a_expected = seq; a_last_arr = 0; }
    if (seq >= a_expected) { g_a_lost += seq - a_expected; a_expected = seq + 1; }
    if (a_last_arr) {
        uint32_t gap = (uint32_t)((now - a_last_arr) / 1000);
        if (gap > g_a_arr_max_ms) g_a_arr_max_ms = gap;
    }
    a_last_arr = now;
    g_aud_active = true;
    g_a_rx++;
    if (xQueueSend(aud_q, pkt + 16, 0) != pdTRUE) g_a_drop++;   // full: the newest chunk is lost
}

static void audio_task(void *arg) {
    (void)arg;
    static uint8_t pcm[AV_AUDIO_BYTES], silence[AV_AUDIO_BYTES];
    for (;;) {
        while (!g_aud_active) vTaskDelay(pdMS_TO_TICKS(20));
        // Prebuffer, as the product does, then open the output.
        int64_t give_up = esp_timer_get_time() + 2000000;
        while (uxQueueMessagesWaiting(aud_q) < AUD_PREBUF && esp_timer_get_time() < give_up) vTaskDelay(pdMS_TO_TICKS(5));
        bsp_audio_set_mute(false);
        int64_t last_feed = 0;
        bool was_silent = false;
        int64_t idle_since = 0;
        while (g_aud_active || uxQueueMessagesWaiting(aud_q)) {
            bool real = xQueueReceive(aud_q, pcm, pdMS_TO_TICKS(AV_AUDIO_MS)) == pdTRUE;
            if (!real) {
                if (!g_aud_active) break;
                // No audio for a chunk period. If the source has been gone for a while, stop.
                if (!idle_since) idle_since = esp_timer_get_time();
                if (esp_timer_get_time() - idle_since > 3000000) { g_aud_active = false; break; }
                g_a_silent++;
                if (!was_silent) g_a_episodes++;
                was_silent = true;
            } else { idle_since = 0; was_silent = false; }
            uint32_t q = uxQueueMessagesWaiting(aud_q);
            if (q < g_a_qmin) g_a_qmin = q;
            int64_t t0 = esp_timer_get_time();
            if (last_feed) {
                uint32_t gap = (uint32_t)((t0 - last_feed) / 1000);
                if (gap > g_a_feed_max_ms) g_a_feed_max_ms = gap;
            }
            size_t written = 0;
            esp_err_t e = bsp_audio_write_timeout(real ? pcm : silence, AV_AUDIO_BYTES, &written, 100);
            if (e != ESP_OK || written != AV_AUDIO_BYTES) g_a_write_fail++;
            last_feed = esp_timer_get_time();
        }
        bsp_audio_set_mute(true);
        xQueueReset(aud_q);
    }
}

static void audio_start(void) {
    aud_q = xQueueCreate(AUD_QUEUE, AV_AUDIO_BYTES);
    if (!aud_q || bsp_audio_init() != ESP_OK || bsp_audio_set_format(16000, 16, 1) != ESP_OK) {
        ESP_LOGE(TAG, "audio init failed"); return;
    }
    bsp_audio_set_mute(true);
    bsp_audio_set_volume(25);
    xTaskCreate(audio_task, "nd_audio", 3072, NULL, 7, NULL);
    ESP_LOGW(TAG, "audio ready queue=%d chunks (%d ms) heap=%u", AUD_QUEUE, AUD_QUEUE * (int)AV_AUDIO_MS,
             (unsigned)esp_get_free_heap_size());
}

static void audio_report(void) {
    static uint32_t l_rx, l_lost, l_drop, l_silent, l_ep, l_fail;
    ESP_LOGW(TAG, "AUDIO rx=%u lost=%u drop=%u silent_chunks=%u episodes=%u write_fail=%u "
             "arr_gap_max_ms=%u feed_gap_max_ms=%u q_min=%u/%d heap_free=%u heap_min=%u",
             (unsigned)(g_a_rx - l_rx), (unsigned)(g_a_lost - l_lost), (unsigned)(g_a_drop - l_drop),
             (unsigned)(g_a_silent - l_silent), (unsigned)(g_a_episodes - l_ep), (unsigned)(g_a_write_fail - l_fail),
             (unsigned)g_a_arr_max_ms, (unsigned)g_a_feed_max_ms,
             (unsigned)(g_a_qmin == 0xFFFFFFFFu ? 0 : g_a_qmin), AUD_QUEUE,
             (unsigned)esp_get_free_heap_size(), (unsigned)esp_get_minimum_free_heap_size());
    l_rx = g_a_rx; l_lost = g_a_lost; l_drop = g_a_drop; l_silent = g_a_silent; l_ep = g_a_episodes; l_fail = g_a_write_fail;
    g_a_arr_max_ms = g_a_feed_max_ms = 0; g_a_qmin = 0xFFFFFFFFu;
}
#else
static void audio_rx(const uint8_t *pkt, int r, int64_t now) { (void)pkt; (void)r; (void)now; }
static void audio_start(void) {}
static void audio_report(void) {}
#endif

// ---------------------------------------------------------------- layer probe
// Where does a gap between frames appear? Three timestamps per datagram path:
//   air  the PHY receive timestamp of each data frame addressed to this station
//        (promiscuous callback, rx_ctrl.timestamp, microseconds, MAC timer)
//   cb   when the driver delivered that same frame to the callback
//   rx   when recv() returned each UDP datagram to the application
// Gap histograms (ms): <2, <10, <30, <60, <120, >=120. A gap that is in `air`
// is the air or the access point; one that appears in `cb` but not `air` is the
// driver holding frames; one only in `rx` is the stack or the scheduler.
#ifndef NET_PROMISC
#define NET_PROMISC 1
#endif
#define GAPB 6
static volatile uint32_t h_air[GAPB], h_cb[GAPB], h_rx[GAPB];
static volatile uint32_t p_frames, p_retry, p_agg, p_lag_min = 0xFFFFFFFFu, p_lag_max;
static uint32_t p_last_air; static int64_t p_last_cb, p_last_rx;
static uint8_t s_mac[6];
// What the air did around a gap in OUR frames: other stations' data frames and
// beacons/management seen since our last frame, sampled when our next one arrives.
static volatile uint32_t p_oth_data, p_oth_mgmt;             // since our last frame
// Beacon alignment: where in the AP's beacon interval (10 ms buckets, 0..109) our
// frames arrive, for frames that END a >=60 ms gap versus all of our frames.
#define GAPLOG 24
static uint32_t gl_start[GAPLOG], gl_len[GAPLOG]; static uint8_t gl_rssi[GAPLOG], gl_n; static int8_t p_rssi;
static uint8_t s_bssid[6]; static uint32_t p_last_beacon_air; static bool p_have_beacon;
static volatile uint32_t h_bea_gap[11], h_bea_all[11], c_dtim_seen, c_beacons, c_bea_ps_bit;
static volatile uint32_t c_gaps, c_quiet, c_dataframes, c_mgmt, c_all_oth_data;
static inline int gap_bucket(uint32_t us) {
    return us < 2000 ? 0 : us < 10000 ? 1 : us < 30000 ? 2 : us < 60000 ? 3 : us < 120000 ? 4 : 5;
}
#if NET_PROMISC
static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
    const wifi_promiscuous_pkt_t *pk = (const wifi_promiscuous_pkt_t *)buf;
    const uint8_t *h = pk->payload;
    if (type == WIFI_PKT_MGMT) {
        p_oth_mgmt++;
        if (h[0] == 0x80 && memcmp(h + 16, s_bssid, 6) == 0) {      // beacon of OUR access point
            p_last_beacon_air = pk->rx_ctrl.timestamp; p_have_beacon = true; c_beacons++;
        }
        return;
    }
    if (type != WIFI_PKT_DATA) return;
    if (memcmp(h + 4, s_mac, 6) != 0) { p_oth_data++; c_all_oth_data++; return; }   // someone else's frame
    int64_t now = esp_timer_get_time();
    uint32_t air = pk->rx_ctrl.timestamp;
    if (p_have_beacon) {
        uint32_t off = (air - p_last_beacon_air) / 10000u; if (off > 10) off = 10;
        h_bea_all[off]++;
        if (p_last_cb && air - p_last_air >= 60000u) h_bea_gap[off]++;
    }
    if (p_last_cb) {
        if (air - p_last_air >= 60000u) {
            if (gl_n < GAPLOG) { gl_start[gl_n] = p_last_air; gl_len[gl_n] = air - p_last_air; gl_rssi[gl_n] = (uint8_t)(-pk->rx_ctrl.rssi); gl_n++; }
            c_gaps++;
            c_dataframes += p_oth_data;
            c_mgmt += p_oth_mgmt;
            if (p_oth_data == 0) c_quiet++;
        }
        h_air[gap_bucket(air - p_last_air)]++;
        h_cb[gap_bucket((uint32_t)(now - p_last_cb))]++;
    }
    p_last_air = air; p_last_cb = now;
    p_oth_data = 0; p_oth_mgmt = 0;
    uint32_t lag = (uint32_t)now - air;                 // clock offset plus driver delay
    if (lag < p_lag_min) p_lag_min = lag;
    if (lag > p_lag_max) p_lag_max = lag;
    p_frames++;
    if (h[1] & 0x08) p_retry++;                         // frame control: retry bit
    if (pk->rx_ctrl.aggregation) p_agg++;
}
#endif
static void probe_start(void) {
#if NET_PROMISC
    esp_wifi_get_mac(WIFI_IF_STA, s_mac);
    { wifi_ap_record_t ap; if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) memcpy(s_bssid, ap.bssid, 6);
      p_have_beacon = false; }
    wifi_promiscuous_filter_t f = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA | WIFI_PROMIS_FILTER_MASK_MGMT };
    esp_wifi_set_promiscuous_filter(&f);
    esp_wifi_set_promiscuous_rx_cb(promisc_cb);
    esp_wifi_set_promiscuous(true);
#endif
}
static void probe_stop(void) {
#if NET_PROMISC
    esp_wifi_set_promiscuous(false);
#endif
}
static void probe_report(void) {
    uint32_t a[GAPB], c[GAPB], r[GAPB];
    for (int i = 0; i < GAPB; i++) { a[i] = h_air[i]; c[i] = h_cb[i]; r[i] = h_rx[i]; h_air[i] = h_cb[i] = h_rx[i] = 0; }
    uint32_t lo = p_lag_min, hi = p_lag_max;
    p_lag_min = 0xFFFFFFFFu; p_lag_max = 0;
    ESP_LOGW(TAG, "PROBE gaps(<2,<10,<30,<60,<120,>=120ms) air=%u/%u/%u/%u/%u/%u cb=%u/%u/%u/%u/%u/%u rx=%u/%u/%u/%u/%u/%u "
             "wifi_frames=%u retry=%u agg=%u driver_hold_ms=%u",
             (unsigned)a[0], (unsigned)a[1], (unsigned)a[2], (unsigned)a[3], (unsigned)a[4], (unsigned)a[5],
             (unsigned)c[0], (unsigned)c[1], (unsigned)c[2], (unsigned)c[3], (unsigned)c[4], (unsigned)c[5],
             (unsigned)r[0], (unsigned)r[1], (unsigned)r[2], (unsigned)r[3], (unsigned)r[4], (unsigned)r[5],
             (unsigned)p_frames, (unsigned)p_retry, (unsigned)p_agg, (unsigned)(hi >= lo ? (hi - lo) / 1000u : 0));
    ESP_LOGW(TAG, "GAPCTX gaps60=%u quiet_no_other_data=%u other_data_in_gaps=%u mgmt_in_gaps=%u other_data_total=%u",
             (unsigned)c_gaps, (unsigned)c_quiet, (unsigned)c_dataframes, (unsigned)c_mgmt, (unsigned)c_all_oth_data);
    { char b[300]; int at = 0;
      for (int i = 0; i < gl_n && at < 260; i++) at += snprintf(b + at, sizeof b - (size_t)at, " %u:%u", (unsigned)((gl_start[i] - gl_start[0]) / 1000u), (unsigned)(gl_len[i] / 1000u));
      ESP_LOGW(TAG, "GAPLIST n=%u start_ms:len_ms%s", (unsigned)gl_n, b); gl_n = 0; }
    ESP_LOGW(TAG, "BEACON off10ms gap_end=%u/%u/%u/%u/%u/%u/%u/%u/%u/%u/%u all=%u/%u/%u/%u/%u/%u/%u/%u/%u/%u/%u beacons=%u",
             (unsigned)h_bea_gap[0], (unsigned)h_bea_gap[1], (unsigned)h_bea_gap[2], (unsigned)h_bea_gap[3], (unsigned)h_bea_gap[4], (unsigned)h_bea_gap[5],
             (unsigned)h_bea_gap[6], (unsigned)h_bea_gap[7], (unsigned)h_bea_gap[8], (unsigned)h_bea_gap[9], (unsigned)h_bea_gap[10],
             (unsigned)h_bea_all[0], (unsigned)h_bea_all[1], (unsigned)h_bea_all[2], (unsigned)h_bea_all[3], (unsigned)h_bea_all[4], (unsigned)h_bea_all[5],
             (unsigned)h_bea_all[6], (unsigned)h_bea_all[7], (unsigned)h_bea_all[8], (unsigned)h_bea_all[9], (unsigned)h_bea_all[10], (unsigned)c_beacons);
    for (int i = 0; i < 11; i++) h_bea_gap[i] = h_bea_all[i] = 0;
    c_beacons = 0;
    c_gaps = c_quiet = c_dataframes = c_mgmt = c_all_oth_data = 0;
    p_frames = p_retry = p_agg = 0;
}

// ------------------------------------------------------- latest-stripe slots
// Mode 5. There is no "frame" on the device: each of the 15 stripes has ONE slot
// holding the newest complete compressed version of it. A datagram for a newer
// frame replaces an undrawn older one, so a 100-400 ms gap in arrival is followed
// by at most one pending update per stripe, not by a backlog of whole frames, and
// the memory needed is 15 slots however long the gap was. The panel keeps what it
// last drew (GRAM), so a stripe that is not refreshed is stale, never blank.
//
// Datagram: u32 frame | u16 stripe | u16 (fragment << 8 | fragments) | u32 stripe
// bytes | u32 fps | compressed stripe (<= 1384 B a fragment, at most 2 fragments).
#define UF_HDR 16
#define SL_N ((int)AV_STRIPES)
#define SL_MAX 1800
enum { SL_EMPTY = 0, SL_ASM = 1, SL_READY = 2 };
static volatile bool g_slots;
static uint8_t sl_state[SL_N], sl_cnt[SL_N], sl_got[SL_N];
static uint32_t sl_frame[SL_N], sl_len[SL_N];
static volatile uint32_t g_us_latest, g_us_rx, g_us_super, g_us_inc, g_us_stale;
static volatile uint32_t g_us_age_sum, g_us_age_n, g_us_age_max, g_us_pkts, g_us_seqlost, g_us_reject;
static uint32_t us_seq_next; static bool us_seq_have;
static portMUX_TYPE sl_mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t cbuf[SL_MAX];

static void slots_reset(void) {
    taskENTER_CRITICAL(&sl_mux);
    for (int i = 0; i < SL_N; i++) { sl_state[i] = SL_EMPTY; sl_frame[i] = 0xFFFFFFFFu; sl_got[i] = 0; }
    g_us_latest = 0; us_seq_have = false;
    taskEXIT_CRITICAL(&sl_mux);
}

static void slots_rx(const uint8_t *pkt, int r) {
    uint32_t fr = pkt[0] | (pkt[1] << 8) | (pkt[2] << 16) | ((uint32_t)pkt[3] << 24);
    uint32_t s = pkt[4] | (pkt[5] << 8), fc = pkt[6] | (pkt[7] << 8);
    uint32_t total = pkt[8] | (pkt[9] << 8) | (pkt[10] << 16) | ((uint32_t)pkt[11] << 24);
    uint32_t fi = fc >> 8, cnt = fc & 0xff, n = (uint32_t)(r - UF_HDR);
    { uint32_t q = pkt[12] | (pkt[13] << 8) | (pkt[14] << 16) | ((uint32_t)pkt[15] << 24);
      g_us_pkts++; if (us_seq_have && (int32_t)(q - us_seq_next) > 0) g_us_seqlost += q - us_seq_next;
      if (!us_seq_have || (int32_t)(q - us_seq_next) >= 0) us_seq_next = q + 1;
      us_seq_have = true; }
    if (s >= (uint32_t)SL_N || cnt == 0 || cnt > 2 || fi >= cnt || total == 0 || total > SL_MAX || fi * 1384u + n > total) { g_us_reject++; return; }
    bool ready = false;
    taskENTER_CRITICAL(&sl_mux);
    if ((int32_t)(fr - g_us_latest) > 0 || g_us_latest == 0) g_us_latest = fr;
    int32_t d = (int32_t)(fr - sl_frame[s]);
    if (d < 0 || (d == 0 && sl_state[s] != SL_ASM)) { g_us_stale++; }           // older, or a repeat of what we have
    else {
        if (d > 0) {                                                            // a newer version starts here
            if (sl_state[s] == SL_READY) g_us_super++;                          // the undrawn one is replaced
            else if (sl_state[s] == SL_ASM) g_us_inc++;                         // it never completed
            sl_state[s] = SL_ASM; sl_frame[s] = fr; sl_got[s] = 0; sl_cnt[s] = (uint8_t)cnt; sl_len[s] = total;
        }
        memcpy(ring + (size_t)s * SL_MAX + fi * 1384u, pkt + UF_HDR, n);
        sl_got[s] |= (uint8_t)(1u << fi);
        if (sl_got[s] == (uint8_t)((1u << sl_cnt[s]) - 1u)) { sl_state[s] = SL_READY; g_us_rx++; ready = true; }
    }
    taskEXIT_CRITICAL(&sl_mux);
    if (ready) { uint8_t tick = 1; xQueueSend(full_q, &tick, 0); }
}

// Oldest ready stripe first, so the picture is never further behind than it must be.
static bool slot_take(unsigned *s_out, uint32_t *len, uint32_t *fr_out) {
    bool found = false;
    taskENTER_CRITICAL(&sl_mux);
    int best = -1;
    for (int i = 0; i < SL_N; i++)
        if (sl_state[i] == SL_READY && (best < 0 || (int32_t)(sl_frame[i] - sl_frame[best]) < 0)) best = i;
    if (best >= 0) {
        memcpy(cbuf, ring + (size_t)best * SL_MAX, sl_len[best]);
        *s_out = (unsigned)best; *len = sl_len[best]; *fr_out = sl_frame[best];
        sl_state[best] = SL_EMPTY;                                              // sl_frame stays: it rejects stragglers
        int32_t age = (int32_t)(g_us_latest - sl_frame[best]); if (age < 0) age = 0;
        g_us_age_sum += (uint32_t)age; g_us_age_n++; if ((uint32_t)age > g_us_age_max) g_us_age_max = (uint32_t)age;
        found = true;
    }
    taskEXIT_CRITICAL(&sl_mux);
    return found;
}

// Step 5: frames over UDP. Datagram = u32 frame | u16 idx | u16 count |
// u32 total_len | u32 offered_fps | payload. A frame missing any datagram when
// the next frame starts is dropped whole; with no free buffer the frame is
// dropped too (the receiver never blocks, so device slowness shows as nobuf).
static void udp_frames_session(void) {
    int u = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (u < 0) return;
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(8096), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (bind(u, (struct sockaddr *)&me, sizeof me) < 0) { ESP_LOGE(TAG, "udp bind failed errno=%d", errno); close(u); return; }
    int rcvbuf = 32768;
    setsockopt(u, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    struct timeval tv = { .tv_usec = 200000 };
    setsockopt(u, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    static uint8_t pkt[1500];
    bool started = false;
    bool open_frame = false;
    uint32_t cur_frame = 0xFFFFFFFFu, got = 0, count = 0, total = 0, cur_off = 0, skip_frame = 0xFFFFFFFFu;
    ring_head = ring_tail = ring_wr = ring_rd = 0;
    if (g_slots) slots_reset();
    p_last_rx = 0; p_last_cb = 0;
    probe_start();
    int64_t last_rx = esp_timer_get_time();
    g_mode = MODE_UDPF; g_status = "udp hello...";
    for (int hello = 0; hello < 50 || started; ) {
        if (!started) hello++;   // the server sends to us; no hello (inbound UDP may be firewalled)
        int r = recv(u, pkt, sizeof pkt, 0);
        int64_t now = esp_timer_get_time();
        if (r <= UF_HDR) { if (started && now - last_rx > 8000000) break; continue; }
        if ((pkt[0] | (pkt[1] << 8) | (pkt[2] << 16) | ((uint32_t)pkt[3] << 24)) == AUD_MARK) {
            last_rx = now; started = true; g_status = "udp frames";
            audio_rx(pkt, r, now);
            continue;
        }
        if (p_last_rx) h_rx[gap_bucket((uint32_t)(now - p_last_rx))]++;
        p_last_rx = now;
        last_rx = now; started = true; g_status = "udp frames";
        if (g_slots) { slots_rx(pkt, r); continue; }
        uint32_t fr = pkt[0] | (pkt[1] << 8) | (pkt[2] << 16) | ((uint32_t)pkt[3] << 24);
        uint32_t idx = pkt[4] | (pkt[5] << 8), cnt = pkt[6] | (pkt[7] << 8);
        uint32_t tot = pkt[8] | (pkt[9] << 8) | (pkt[10] << 16) | ((uint32_t)pkt[11] << 24);
        g_udp_level = pkt[12] | (pkt[13] << 8) | (pkt[14] << 16) | ((uint32_t)pkt[15] << 24);
        g_udp_bytes += (uint32_t)r;
        if (fr == skip_frame) continue;            // late duplicate/straggler of a frame already delivered
        if (!open_frame || fr != cur_frame) {
            if (open_frame) g_uf_incomplete++;     // previous frame never completed; its space is reused
            open_frame = false; cur_frame = fr; got = 0; count = cnt; total = tot;
            if (idx != 0) continue;                // missed the frame's start
            if (total > FRAME_MAX || total == 0) continue;
            // Find room for `total` bytes at the write cursor, wrapping if it does not fit before the end.
            uint32_t off = ring_wr;
            if (off + total > RING_BYTES) { off = 0; g_ring_wrap++; }
            uint32_t used_from = ring_rd;
            bool empty = (ring_head == ring_tail);
            bool overlaps = !empty && ((off < used_from) ? (off + total > used_from)
                                                        : (off + total > used_from + 0 && off < used_from + 0));
            // Region [off, off+total) is free if the oldest unread frame does not intersect it.
            if (!empty) {
                uint32_t r0 = ring_q[ring_tail % RING_SLOTS].off, r1 = r0 + ring_q[ring_tail % RING_SLOTS].len;
                uint32_t w0 = off, w1 = off + total;
                // Walk every queued frame; any intersection means no room.
                overlaps = false;
                for (uint32_t k = ring_tail; k != ring_head; k++) {
                    r0 = ring_q[k % RING_SLOTS].off; r1 = r0 + ring_q[k % RING_SLOTS].len;
                    if (w0 < r1 && r0 < w1) { overlaps = true; break; }
                }
            }
            if (overlaps || (uint32_t)(ring_head - ring_tail) >= RING_SLOTS - 1) { g_uf_nobuf++; continue; }
            cur_off = off; open_frame = true;
        }
        if (!open_frame || idx >= count) continue;
        if (idx * 1384u + (uint32_t)(r - UF_HDR) > total) continue;
        memcpy(ring + cur_off + idx * 1384u, pkt + UF_HDR, (size_t)r - UF_HDR);
        if (++got == count) {
            ring_q[ring_head % RING_SLOTS] = (ring_ent_t){ cur_off, total };
            ring_wr = cur_off + total;
            ring_head++;
            uint32_t depth = ring_head - ring_tail;
            if (depth > g_ring_high) g_ring_high = depth;
            g_rx_frames++;
            uint8_t tick = 1; xQueueSend(full_q, &tick, 0);
            open_frame = false;
            cur_frame = fr;                        // ignore stragglers of this frame
            got = 0; count = 0;
            cur_off = 0;
            skip_frame = fr;
        }
    }
    probe_stop();
#if NET_AUDIO
    g_aud_active = false;
#endif
    close(u); g_mode = MODE_IDLE; g_status = "udp ended";
}

static void rx_task(void *arg) {
    (void)arg;
    for (;;) {
        if (!av_provision_connected()) { g_status = "waiting for Wi-Fi..."; vTaskDelay(pdMS_TO_TICKS(500)); continue; }
        int fd = dial();
        if (fd < 0) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        uint8_t head[5];
        // The server may still be finishing a previous run; wait for it.
        struct timeval hw = { .tv_sec = 70 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &hw, sizeof hw);
        if (!read_full(fd, head, 5) || memcmp(head, "NDM1", 4) != 0) {
            g_status = "bad header from server";
            close(fd); vTaskDelay(pdMS_TO_TICKS(1000)); continue;
        }
        struct timeval dw = { .tv_sec = 5 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &dw, sizeof dw);
        ESP_LOGW(TAG, "session start mode=%u heap=%u", head[4], (unsigned)esp_get_free_heap_size());
        if (head[4] == 4 || head[4] == 5) { close(fd); g_slots = head[4] == 5; udp_frames_session(); continue; }
        if (head[4] == 3) { close(fd); udp_session(); continue; }
        g_mode = head[4] == 1 ? MODE_BULK : MODE_FRAMES;
        g_status = "streaming";
        for (;;) {
            uint8_t idx;
            int64_t t0 = esp_timer_get_time();
            xQueueReceive(free_q, &idx, portMAX_DELAY);
            g_rx_block_us += (uint32_t)(esp_timer_get_time() - t0);
            if (g_mode == MODE_BULK) {
                // Read into a free buffer and give it straight back.
                int r = recv(fd, fb[idx], FRAME_MAX, 0);
                xQueueSend(free_q, &idx, 0);
                if (r <= 0) break;
                g_rx_bytes += (uint32_t)r;
                continue;
            }
            uint8_t len4[4];
            bool ok = read_full(fd, len4, 4);
            uint32_t len = len4[0] | (len4[1] << 8) | (len4[2] << 16) | ((uint32_t)len4[3] << 24);
            ok = ok && len >= 4 && len <= FRAME_MAX && read_full(fd, fb[idx], len);
            if (!ok) { xQueueSend(free_q, &idx, 0); break; }
            fb_len[idx] = len;
            g_rx_frames++;
            xQueueSend(full_q, &idx, portMAX_DELAY);
        }
        ESP_LOGW(TAG, "session end errno=%d", errno);
        g_mode = MODE_IDLE;
        g_status = "session ended";
        close(fd);
    }
}

// ------------------------------------------------------------ draw + report

typedef struct { uint64_t inflate, expand, submit; unsigned frames, bad, skipped; uint32_t max_us, slow; } draw_stats_t;

// One stripe from a slot: inflate, expand, submit with one transfer in flight.
static bool draw_slot(unsigned s, uint32_t len, tinfl_decompressor *infl, uint8_t *stripe[2],
                      unsigned *next, unsigned *live, const uint16_t *swapped, draw_stats_t *st) {
    uint8_t *buf = stripe[*next]; *next ^= 1u;
    size_t produced = AV_STRIPE_PIXELS, consumed = len;
    int64_t t0 = esp_timer_get_time();
    tinfl_init(infl);
    tinfl_status ts = tinfl_decompress(infl, cbuf, &consumed, buf, buf, &produced,
        TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
    int64_t t1 = esp_timer_get_time();
    if (ts != TINFL_STATUS_DONE || produced != AV_STRIPE_PIXELS) return false;
    expand_fast(buf, AV_STRIPE_PIXELS, swapped);
    int64_t t2 = esp_timer_get_time();
    if (bsp_display_raw_submit_nowait(VIDEO_Y + (int)(s * AV_STRIPE_ROWS), (int)AV_STRIPE_ROWS, buf) == ESP_OK) (*live)++;
    if (*live >= 2) { bsp_display_raw_drain(1, 200); (*live)--; }
    int64_t t3 = esp_timer_get_time();
    st->inflate += (uint64_t)(t1 - t0); st->expand += (uint64_t)(t2 - t1); st->submit += (uint64_t)(t3 - t2);
    return true;
}

// One frame: 15 stripes, each inflated, expanded and submitted with one
// transfer kept in flight while the next stripe is prepared.
static bool draw_frame(const uint8_t *p, uint32_t len, tinfl_decompressor *infl,
                       uint8_t *stripe[2], unsigned *next, unsigned *live,
                       const uint16_t *swapped, draw_stats_t *st) {
    if (p[0] != 0 || p[1] != AV_STRIPES) return false;
    const unsigned count = p[1];
    const uint8_t *table = p + 2;
    const uint8_t *data = table + 2u * count;
    uint32_t total = 2u + 2u * count;
    for (unsigned n = 0; n < count; n++) total += (table[2 * n] << 8) | table[2 * n + 1];
    if (total != len) return false;
    for (unsigned n = 0; n < count; n++) {
        size_t avail = (table[2 * n] << 8) | table[2 * n + 1];
        // Length 0: this stripe is unchanged, the panel already shows it. It
        // costs no inflate, no expand and no bus time.
        if (avail == 0) { st->skipped++; continue; }
        uint8_t *buf = stripe[*next]; *next ^= 1u;
        size_t produced = AV_STRIPE_PIXELS, consumed = avail;
        int64_t t0 = esp_timer_get_time();
        tinfl_init(infl);
        tinfl_status s = tinfl_decompress(infl, data, &consumed, buf, buf, &produced,
            TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
        data += avail;
        int64_t t1 = esp_timer_get_time();
        if (s != TINFL_STATUS_DONE || produced != AV_STRIPE_PIXELS) return false;
        expand_fast(buf, AV_STRIPE_PIXELS, swapped);
        int64_t t2 = esp_timer_get_time();
        if (bsp_display_raw_submit_nowait(VIDEO_Y + (int)(n * AV_STRIPE_ROWS), (int)AV_STRIPE_ROWS, buf) == ESP_OK) (*live)++;
        if (*live >= 2) { bsp_display_raw_drain(1, 200); (*live)--; }
        int64_t t3 = esp_timer_get_time();
        st->inflate += (uint64_t)(t1 - t0);
        st->expand += (uint64_t)(t2 - t1);
        st->submit += (uint64_t)(t3 - t2);
    }
    return true;
}

// Step 1: eight colour bars across the whole panel.
static void colour_bars(uint8_t *stripe) {
    static const uint16_t c[8] = { 0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000 };
    for (int y = 0; y < (int)AV_STRIPE_ROWS; y++)
        for (int x = 0; x < PANEL_W; x++) {
            uint16_t v = c[x / (PANEL_W / 8)];
            uint8_t *q = stripe + ((size_t)y * PANEL_W + (size_t)x) * 2;
            q[0] = (uint8_t)(v >> 8); q[1] = (uint8_t)v;
        }
    for (int y = 0; y < PANEL_H; y += (int)AV_STRIPE_ROWS)
    {
        esp_err_t a = bsp_display_raw_submit(y, (int)AV_STRIPE_ROWS, stripe, 200);
        if (a != ESP_OK) ESP_LOGE(TAG, "bars y=%d submit=%s", y, esp_err_to_name(a));
    }
    bsp_display_raw_wait(200);
}

// Per-task CPU share over the last interval, from FreeRTOS run-time stats.
#define STAT_MAX 32
static TaskStatus_t st_prev[STAT_MAX];
static UBaseType_t st_prev_n;
static uint32_t st_prev_total;
static void print_cpu_stats(void) {
    TaskStatus_t cur[STAT_MAX];
    uint32_t total = 0;
    UBaseType_t n = uxTaskGetSystemState(cur, STAT_MAX, &total);
    uint32_t dt = total - st_prev_total;
    if (st_prev_n && dt) {
        char line[240]; int at = 0;
        for (UBaseType_t i = 0; i < n; i++) {
            uint32_t before = 0;
            for (UBaseType_t j = 0; j < st_prev_n; j++)
                if (st_prev[j].xHandle == cur[i].xHandle) { before = st_prev[j].ulRunTimeCounter; break; }
            unsigned pct10 = (unsigned)((uint64_t)(cur[i].ulRunTimeCounter - before) * 1000u / dt);
            if (pct10 < 10 || at > 200) continue;   // hide under 1%
            at += snprintf(line + at, sizeof line - (size_t)at, " %s=%u.%u", cur[i].pcTaskName, pct10 / 10, pct10 % 10);
        }
        ESP_LOGW(TAG, "CPU%%%s", line);
    }
    memcpy(st_prev, cur, sizeof cur);
    st_prev_n = n; st_prev_total = total;
}

// Panel bus cost of a whole picture including DMA completion: 15 stripes are
// submitted with two in flight and then all drained, which is what a frame costs
// when nothing else runs. Reported once at boot.
static void panel_bench(uint8_t *stripe[2]) {
    memset(stripe[0], 0x55, STRIPE_BYTES); memset(stripe[1], 0xAA, STRIPE_BYTES);
    unsigned live = 0;
    const int N = 20;
    int64_t t0 = esp_timer_get_time();
    for (int f = 0; f < N; f++) {
        for (unsigned n = 0; n < AV_STRIPES; n++) {
            if (bsp_display_raw_submit_nowait(VIDEO_Y + (int)(n * AV_STRIPE_ROWS), (int)AV_STRIPE_ROWS, stripe[n & 1]) == ESP_OK) live++;
            if (live >= 2) { bsp_display_raw_drain(1, 200); live--; }
        }
        bsp_display_raw_drain(live, 200); live = 0;
    }
    int64_t t1 = esp_timer_get_time();
    ESP_LOGW(TAG, "PANEL_BENCH frame_us=%u fps_x10=%u (15 stripes, DMA drained, no CPU work)",
             (unsigned)((t1 - t0) / N), (unsigned)(N * 10000000ll / (t1 - t0)));
}

static void halt(const char *why) {
    ESP_LOGE(TAG, "%s", why);
    vTaskDelay(portMAX_DELAY);
}

static void draw_task(void *arg) {
    (void)arg;
    // Large blocks first, before the heap fragments.
    tinfl_decompressor *infl = heap_caps_malloc(sizeof(*infl), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ring = heap_caps_malloc(RING_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    for (int i = 0; i < FRAME_BUFS; i++) fb[i] = heap_caps_malloc(FRAME_MAX, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *bar = heap_caps_malloc((size_t)PANEL_W * BAR_ROWS * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    uint8_t *stripe[2] = {
        heap_caps_malloc(STRIPE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
        heap_caps_malloc(STRIPE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA) };
    uint16_t *swapped = heap_caps_malloc(2 * AV_PALETTE_ENTRIES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!ring) halt("out of memory (ring)");
    for (int i = 0; i < FRAME_BUFS; i++) if (!fb[i]) halt("out of memory (frame buffers)");
    if (!bar || !stripe[0] || !stripe[1] || !infl || !swapped) {
        ESP_LOGE(TAG, "alloc bar=%p stripe=%p/%p infl=%p swapped=%p heap=%u largest=%u", bar, stripe[0], stripe[1], infl, swapped,
                 (unsigned)esp_get_free_heap_size(), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        halt("out of memory");
    }
    for (unsigned i = 0; i < AV_PALETTE_ENTRIES; i++) swapped[i] = __builtin_bswap16(av_palette_rgb565((uint8_t)i));
    ESP_LOGW(TAG, "sizeof(tinfl_decompressor)=%u", (unsigned)sizeof(tinfl_decompressor));
    ESP_LOGW(TAG, "allocated; heap=%u largest=%u", (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));

    if (bsp_display_init() != ESP_OK || bsp_display_raw_claim() != ESP_OK) halt("display init/claim failed");
    bsp_display_backlight(80);
    colour_bars(stripe[0]);                               // step 1
    panel_bench(stripe);
    draw_bar(bar, 0, "NET DEMO  step 1: panel OK", UI_COLOR_TEXT);
    draw_bar(bar, PANEL_H - BAR_ROWS, "starting", UI_COLOR_DIM);

    for (int i = 0; i < FRAME_BUFS; i++) { uint8_t idx = (uint8_t)i; xQueueSend(free_q, &idx, 0); }
    audio_start();
    xTaskCreate(rx_task, "nd_rx", 4096, NULL, 6, NULL);

    draw_stats_t st = {0};
    unsigned next = 0, live = 0;
    uint64_t starve_us = 0;
    uint32_t last_bytes = 0, last_frames = 0, last_block = 0;
    uint32_t lu_bytes = 0, lu_pkts = 0, lu_lost = 0, lu_ooo = 0;
    int64_t window = esp_timer_get_time();
    for (;;) {
        uint8_t idx;
        int64_t w0 = esp_timer_get_time();
        bool got = xQueueReceive(full_q, &idx, pdMS_TO_TICKS(100)) == pdTRUE;
        if (g_slots) {
            unsigned ss; uint32_t sl, sf;
            for (int k = 0; k < SL_N * 2 && slot_take(&ss, &sl, &sf); k++) {
                int64_t f0 = esp_timer_get_time();
                if (draw_slot(ss, sl, infl, stripe, &next, &live, swapped, &st)) st.frames++; else st.bad++;
                uint32_t fd = (uint32_t)(esp_timer_get_time() - f0);
                if (fd > st.max_us) st.max_us = fd;
            }
            got = false;
        }
        const uint8_t *fptr = NULL; uint32_t flen = 0;
        if (got && g_mode == MODE_UDPF) { ring_ent_t e = ring_q[ring_tail % RING_SLOTS]; fptr = ring + e.off; flen = e.len; }
        else if (got) { fptr = fb[idx]; flen = fb_len[idx]; }
        if (g_mode == MODE_FRAMES || g_mode == MODE_UDPF) starve_us += (uint64_t)(esp_timer_get_time() - w0);
        if (got) {
            int64_t f0 = esp_timer_get_time();
            if (draw_frame(fptr, flen, infl, stripe, &next, &live, swapped, &st)) st.frames++;
            else st.bad++;
            uint32_t fd = (uint32_t)(esp_timer_get_time() - f0);
            if (fd > st.max_us) st.max_us = fd;
            if (fd > 35000) st.slow++;
            if (g_mode == MODE_UDPF) ring_tail++;
            else xQueueSend(free_q, &idx, 0);
        }
        int64_t now = esp_timer_get_time();
        if (now - window < REPORT_US) continue;
        const int64_t rep0 = now;

        const uint32_t span = (uint32_t)(now - window);
        const uint32_t bytes = g_rx_bytes - last_bytes, frames = g_rx_frames - last_frames;
        const uint32_t block_ms = (g_rx_block_us - last_block) / 1000u;
        const unsigned kbps = (unsigned)((uint64_t)bytes * 1000000ull / span / 1024u);
        const unsigned fps10 = (unsigned)((uint64_t)st.frames * 10000000ull / span);
        char l1[48], l2[64];
        const int mode = g_mode;
        if (mode == MODE_BULK) {
            snprintf(l1, sizeof l1, "step 2 BULK  rx %u KB/s", kbps);
            snprintf(l2, sizeof l2, "%.24s", (const char *)g_status);
            ESP_LOGW(TAG, "BULK rx_kBps=%u", kbps);
        } else if (mode == MODE_FRAMES || mode == MODE_UDPF) {
            snprintf(l1, sizeof l1, "step 3  FPS %u.%u  rx %u KB/s", fps10 / 10, fps10 % 10, kbps);
            if (mode == MODE_UDPF) snprintf(l2, sizeof l2, "offer %u inc %u nobuf %u", (unsigned)g_udp_level, (unsigned)g_uf_incomplete, (unsigned)g_uf_nobuf);
            else snprintf(l2, sizeof l2, "net-wait %u dev-wait %u ms", (unsigned)(starve_us / 1000u), (unsigned)block_ms);
            unsigned n = st.frames ? st.frames : 1;
            if (g_slots) {
                uint32_t an = g_us_age_n ? g_us_age_n : 1;
                ESP_LOGW(TAG, "SLOTS pkts=%u seqlost=%u reject=%u drawn_per_s_x10=%u rx=%u superseded=%u incomplete=%u stale=%u bad=%u age_mean_x10=%u age_max=%u "
                         "inf_x100=%u exp_x100=%u sub_x100=%u max_draw_ms=%u net_wait_ms=%u",
                         (unsigned)g_us_pkts, (unsigned)g_us_seqlost, (unsigned)g_us_reject,
                         (unsigned)((uint64_t)st.frames * 10000000ull / span), (unsigned)g_us_rx, (unsigned)g_us_super, (unsigned)g_us_inc,
                         (unsigned)g_us_stale, st.bad, (unsigned)(g_us_age_sum * 10u / an), (unsigned)g_us_age_max,
                         (unsigned)(st.inflate / n / 10), (unsigned)(st.expand / n / 10), (unsigned)(st.submit / n / 10),
                         (unsigned)(st.max_us / 1000u), (unsigned)(starve_us / 1000u));
                g_us_pkts = g_us_seqlost = g_us_reject = 0; g_us_rx = g_us_super = g_us_inc = g_us_stale = 0; g_us_age_sum = g_us_age_n = g_us_age_max = 0;
            } else
            ESP_LOGW(TAG, "FRAMES fps_x10=%u rx_frames=%u rx_kBps=%u bad=%u net_wait_ms=%u dev_wait_ms=%u "
                     "inf_x100=%u exp_x100=%u sub_x100=%u offer=%u inc=%u nobuf=%u skip_stripes=%u ring_high=%u wrap=%u",
                     fps10, (unsigned)frames, kbps, st.bad, (unsigned)(starve_us / 1000u), (unsigned)block_ms,
                     (unsigned)(st.inflate / n / 10), (unsigned)(st.expand / n / 10), (unsigned)(st.submit / n / 10), (unsigned)g_udp_level, (unsigned)g_uf_incomplete, (unsigned)g_uf_nobuf,
                     st.skipped, (unsigned)g_ring_high, (unsigned)g_ring_wrap);
        } else if (mode == MODE_UDP) {
            const unsigned ub = g_udp_bytes - lu_bytes, up = g_udp_pkts - lu_pkts,
                           ul = g_udp_lost - lu_lost, uo = g_udp_ooo - lu_ooo;
            const unsigned ukb = (unsigned)((uint64_t)ub * 1000000ull / span / 1024u);
            const unsigned loss10 = (up + ul) ? ul * 1000u / (up + ul) : 0;
            snprintf(l1, sizeof l1, "step 4 UDP  rx %u KB/s", ukb);
            snprintf(l2, sizeof l2, "sent %u loss %u.%u%% ooo %u", (unsigned)g_udp_level, loss10 / 10, loss10 % 10, uo);
            ESP_LOGW(TAG, "UDP offered_kBps=%u rx_kBps=%u pkts=%u lost=%u loss_x10=%u ooo=%u",
                     (unsigned)g_udp_level, ukb, up, ul, loss10, uo);
        } else {
            snprintf(l1, sizeof l1, "NET DEMO  panel OK");
            snprintf(l2, sizeof l2, "%.30s", (const char *)g_status);
        }
        bsp_display_raw_drain(live, 200); live = 0;   // the bars share the bus
        draw_bar(bar, 0, l1, UI_COLOR_TEXT);
        draw_bar(bar, PANEL_H - BAR_ROWS, l2, UI_COLOR_DIM);
        ESP_LOGW(TAG, "JITTER arr_min_ms=%u arr_max_ms=%u burst=%u draw_max_ms=%u draw_slow35=%u",
                 (unsigned)(g_arr_min_us == 0xFFFFFFFFu ? 0 : g_arr_min_us / 1000u), (unsigned)(g_arr_max_us / 1000u),
                 (unsigned)g_arr_burst, (unsigned)(st.max_us / 1000u), (unsigned)st.slow);
        g_arr_min_us = 0xFFFFFFFFu; g_arr_max_us = 0; g_arr_burst = 0;
        if (mode == MODE_UDPF) { probe_report(); audio_report(); }
        print_cpu_stats();
        memset(&st, 0, sizeof st);
        starve_us = 0;
        lu_bytes = g_udp_bytes; lu_pkts = g_udp_pkts; lu_lost = g_udp_lost; lu_ooo = g_udp_ooo;
        last_bytes = g_rx_bytes; last_frames = g_rx_frames; last_block = g_rx_block_us;
        window = esp_timer_get_time();
        ESP_LOGW(TAG, "REPORT_COST_MS=%u", (unsigned)((window - rep0) / 1000));
    }
}

void net_demo_main(void) {
    ESP_LOGW(TAG, "network benchmark demo");
    free_q = xQueueCreate(FRAME_BUFS, 1);
    full_q = xQueueCreate(RING_SLOTS, 1);
    bool has_network = false;
    if (!av_provision_init(&has_network)) g_status = "Wi-Fi init failed";
    else if (!has_network) g_status = "no Wi-Fi stored";
    else { av_provision_join_stored(); av_provision_keep_radio_awake(); }
    xTaskCreate(draw_task, "nd_draw", 6144, NULL, 5, NULL);
    for (;;) vTaskDelay(portMAX_DELAY);
}
#endif // CONFIG_AV_NET_DEMO
