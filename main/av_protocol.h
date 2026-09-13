// FAV1 wire format and receive state: no ESP-IDF dependencies.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define AV_HEADER_BYTES 24u
// Control payload ceiling. It has to hold the channel list, which the server
// sends in the CONFIG packet: about 47 bytes of JSON per channel plus the fixed
// fields, so 128 channels is roughly 6.3 KB.
// The parsing cost is what actually bounds the list, not this buffer: cJSON
// holds roughly 4.7 bytes of heap per byte of JSON, measured at 23.5 KB of heap
// for 100 channels. See AV_CHANNEL_MAX for the budget that follows from it.
#define AV_CONTROL_MAX 7168u
#define AV_VIDEO_MAX 24576u
#define AV_AUDIO_BYTES 640u
#define AV_WIDTH 320u
#define AV_HEIGHT 240u
#define AV_VIDEO_WIDTH 160u
#define AV_VIDEO_HEIGHT 120u
#define AV_STRIPE_ROWS 16u
#define AV_MCU_ROWS 16u
#define AV_AUDIO_MS 20u
// Frame rate the server must announce in CONFIG. The device only checks it, it
// does not pace by it: each JPEG carries its own pts and is scheduled against
// the audio clock. Measured decode cost is about 89 ms per frame against a 83 ms
// budget at 12 fps, so frames are dropped under load rather than delaying audio,
// which is the intended trade: audio owns the timeline.
#define AV_FPS 12u
// Extra allowance for the first media packet after CONFIG. A live server opens
// the handshake immediately and then fills a playback reserve before sending
// anything, so the wait is deliberate: the prebuffer target is a few seconds and
// this sits well above it. It used to be 90 s, which meant a channel whose
// source is dead left the screen on the test pattern for a minute and a half
// before the device noticed the server had already given up.
#define AV_FIRST_MEDIA_TIMEOUT_MS 30000u
// How long an empty PCM queue may last before it counts as a real underrun.
// Must exceed the I2S DMA depth (~90 ms at 16 kHz) plus network burst jitter,
// or routine Wi-Fi gaps reset the session.
#define AUDIO_UNDERRUN_MS 300u
// Upper bound on the interval between two consecutive I2S writes. This is a
// different measurement from AUDIO_UNDERRUN_MS, not another use of the same
// number: the gap includes the chunk that was still being written when the
// queue was found empty, so it starts one chunk earlier and must therefore be
// one chunk more generous. Reusing AUDIO_UNDERRUN_MS here made a packet that
// arrived just inside the queue-empty tolerance fail the gap check anyway.
#define AUDIO_FEED_GAP_MS (AUDIO_UNDERRUN_MS + AV_AUDIO_MS)
// Channels the device will accept from CONFIG. This is a RAM budget, set from
// measured cost rather than picked: parsing N channels holds roughly 49.7*N
// bytes of JSON, 235*N of cJSON heap and 40*N of staging at once, and the free
// heap while a session runs is about 68 KB. 128 channels comes to 41.6 KB and
// leaves roughly 12 KB spare; 200 does not fit at all.
//
// The server reads the same number from server/live.py. Keep them equal: the
// device treats "more than this" as a malformed list and ends the session, so a
// server willing to send one extra turns a boundary case into a dead session.
#define AV_CHANNEL_MAX 128u
#define AV_CHANNEL_ID_MAX 16u
enum { AV_HELLO=1, AV_CONFIG, AV_AUDIO, AV_VIDEO, AV_END, AV_ERROR };
typedef struct { uint8_t type; uint32_t session, seq, pts_ms, length; } av_header_t;
typedef struct {
    uint32_t session, next_seq, audio_next_pts, video_pts;
    bool configured, audio_seen, video_seen, ended;
} av_stream_t;
bool av_header_decode(const uint8_t wire[AV_HEADER_BYTES], av_header_t *h);
void av_header_encode(uint8_t wire[AV_HEADER_BYTES], const av_header_t *h);
// Validate direction, session, global sequence and independent media timelines.
// Call once per complete header; errors are terminal (never scan for magic).
bool av_stream_accept(av_stream_t *s, const av_header_t *h);
// Wrap-safe elapsed milliseconds; a timestamp sampled after now has age zero.
uint32_t av_elapsed_ms(uint32_t now, uint32_t last);
// Preflight control JSON before a recursive parser: bounded nesting, strings/escapes.
bool av_json_depth_safe(const char *json, size_t length, unsigned max_depth);
// Step through the server's channel list. `delta` is +1 for next, -1 for
// previous; with no list the id is left untouched so the server default wins.
// Writes the new index to *index and copies the id into out (always terminated).
bool av_channel_step(const char *const *ids, unsigned count, unsigned *index,
                     int delta, char *out, size_t out_size);
// Locate the id the device is currently playing. Returns count when the id is
// not listed, so callers can tell "absent" apart from "index 0".
unsigned av_channel_index_of(const char *const *ids, unsigned count, const char *id);
// RGB888 MCU rectangle -> big-endian RGB565 stripe, rejecting invalid geometry.
bool av_pack_rgb888(uint8_t *stripe, unsigned stripe_y, unsigned left,
                    unsigned top, unsigned right, unsigned bottom,
                    const uint8_t *rgb);
// 160x120 RGB888 rectangle -> 2x nearest-neighbor RGB565, clipped vertically
// to one 16-row destination stripe. rgb starts at (left, top), tightly packed.
// Full source geometry is checked before clipping; no overlap is rejected.
// A 16-source-row MCU must be packed into BOTH destination stripes before DMA.
// stripe_y is in destination coordinates. The last source MCU row has 8 rows.
bool av_pack_rgb888_x2(uint8_t *stripe, unsigned stripe_y, unsigned left,
                       unsigned top, unsigned right, unsigned bottom,
                       const uint8_t *rgb);
