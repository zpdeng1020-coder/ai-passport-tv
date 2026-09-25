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
// Video packet ceiling.
//
// Large enough that one frame of live television crosses as one packet. That is
// the whole point of the number, and it took a sweep to see: the device's
// receive path tops out near 72 packets a second, and it is packets that are
// scarce, not bytes. At fourteen kilobytes a frame, twelve frames a second cost
// twelve video packets; at twelve kilobytes they cost twenty-four, because a
// frame that does not fit is split, and forty-two per cent of the packets were
// being spent on the split rather than on the picture. Measured by pushing the
// rate up until the link refused: asking for sixteen frames a second delivered
// *less* than asking for twelve -- 83 kB/s against 156 -- because the extra
// packets crowded out the sound, which then underran and took the session down.
//
// It is also what the protocol can carry: the length field is sixteen bits, so
// nothing above 65535 can be expressed, and the sender packs to this figure
// rather than to a frame.
//
// Twenty-two kilobytes, because a native 320x240 frame does not fit in less and
// a frame that does not fit is worse than a frame that does.
//
// Measured on live content: a 320x240 frame compresses to 20.4 kB at the median
// and 22.0 kB at the worst, so **every one of thirty sampled frames exceeded the
// old sixteen-kilobyte ceiling and was split in two**. A split frame occupies two
// places in the video queue, whose depth is two -- so the pair fills it, the
// second packet arrives with nowhere to go, the frame is left incomplete, and the
// device discards an incomplete frame by rule. The picture therefore loses
// whole frames at exactly the moment there are more pixels in them, which is why
// the native setting looked *worse* than the enlarged ones rather than better.
//
// The ceiling and the buffer count are one decision. Two of these is 45056
// bytes, less than the three of sixteen kilobytes it replaces, so this costs no
// memory -- it gives some back.
#ifndef AV_VIDEO_MAX
#define AV_VIDEO_MAX 22528u
#endif

// Two buffers, which is one frame being drawn and one being received.
//
// The number and the packet size are one decision: two of twenty-two kilobytes
// is 45056, less than the three of sixteen it replaces. Two is enough because a
// frame is drawn in 34.7 ms at the native geometry against a frame interval of
// 112 ms at nine frames a second -- the buffer is returned long before the next
// frame needs it.
//
// Four was tried twice and failed twice, and the failure does not look like
// memory: the session is allocated before the first CONFIG arrives, the channel
// list is parsed out of that CONFIG and needs about 31 KB of heap, and a fourth
// buffer leaves too little. What the device reports is not "out of memory" but a
// rejected configuration -- it connects, authenticates, and drops having sent
// nothing, which reads as a broken link, an unstable network, or a firmware
// fault. Measured, instrumenting the check: every CONFIG field came back
// not-found, because cJSON failed to allocate and returned NULL.
#ifndef AV_VIDEO_BUFFERS
#define AV_VIDEO_BUFFERS 2u
#endif
#define AV_AUDIO_BYTES 1280u
#define AV_WIDTH 320u
#define AV_HEIGHT 240u
// How much picture arrives, and how much the device makes of it.
//
// The ratio in force is 1/1: the picture arrives at the panel's own size and
// nothing is enlarged. Every screen pixel is a pixel the source produced.
//
// This is not a preference, it is the only setting without an artefact. Any
// other ratio spreads N source columns across 320 screen columns and, because
// the ratio is not whole, some source pixels are copied once and their
// neighbours twice -- a pattern that reads as a grid of dots over the whole
// picture, and the more visible the closer the ratio is to one. Measured by the
// viewer, who rejected both 8/7 and 4/3 on sight and asked for native.
//
// The cost is real and is paid in frame rate: a native frame is 20.4 kB against
// 12.1 kB at 240x180, so the same link carries about half as many of them.
#define AV_VIDEO_WIDTH 320u
#define AV_VIDEO_HEIGHT 180u
// Rows the picture supplies per stripe. Distinct from AV_STRIPE_ROWS, which is
// what the panel takes: one is the input to the enlargement and the other is its
// output, and they are equal exactly when the ratio is one.
#define AV_VIDEO_STRIPE_ROWS 12u
// How the picture reaches the panel: AV_ENLARGE_NUM picture rows become
// AV_ENLARGE_DEN panel rows. 8/5 means the device enlarges by that ratio; 1/1
// means the picture is already panel-sized and nothing is enlarged.
//
// Kept as a pair rather than folded into the geometry, so that the static
// assertions in av_player.c check whichever ratio is in force instead of
// hard-coding one, and so that enlarge_stripe reads the same two numbers. The
// two cannot be changed independently of the sizes above without the build
// failing, which is the point.
#define AV_ENLARGE_NUM 1u
#define AV_ENLARGE_DEN 1u
// Rows the panel takes, unchanged. Every use of this name outside the decoder is
// about the panel.
#define AV_STRIPE_ROWS 12u
// A frame is cut into stripes of AV_VIDEO_STRIPE_ROWS rows, compressed one at a
// time. Stripes rather than one stream so the panel starts updating while the
// rest of the frame is still crossing the network, and so no buffer larger than
// one stripe is ever needed.
#define AV_STRIPES (AV_VIDEO_HEIGHT / AV_VIDEO_STRIPE_ROWS)
#define AV_STRIPE_PIXELS (AV_VIDEO_WIDTH * AV_VIDEO_STRIPE_ROWS)
#define AV_STRIPES_PER_PACKET AV_STRIPES
#define AV_PALETTE_ENTRIES 256u
#define AV_PALETTE_BYTES (AV_PALETTE_ENTRIES * 2u)
#define AV_AUDIO_MS 40u
// Frame rate the server must announce in CONFIG. The device only checks it, it
// does not pace by it: every frame carries its own pts and is scheduled against
// the audio clock, so frames are dropped under load rather than delaying audio,
// which is the intended trade: audio owns the timeline.
//
// This used to say 89 ms per frame against an 83 ms budget, from when the
// device decoded JPEG. inflate and a table lookup replaced that; the figure has
// not been re-measured on hardware since, so treat the budget as the target it
// was chosen for rather than as a measurement.
#define AV_FPS 4u
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
// How much silence the sound may play before the source is judged dead.
//
// This is the difference between a hiccup and a corpse, and it exists because
// the queue is emptied by the picture rather than by anything being wrong with
// the sound. One task reads both streams, so a picture packet in flight is time
// in which nothing feeds the audio queue -- measured at `elapsed_ms=612` in this
// device's own log, against the 300 ms above. Treating that as a fault ended the
// session and sent the screen back to the colour bars, which is the "most
// channels turn into bars after a while" the viewer reported on nine channels
// out of ten.
//
// So an empty queue now feeds silence and carries on, exactly as the picture
// drops a frame instead of tearing down the stream. Silence is inaudible and
// costs the timeline nothing: the clock advances by what was played, which is
// what actually happened, so the picture -- whose timestamps follow the sound's
// -- stays with it. What it buys is a cushion: the sound that piled up in the
// socket while the picture was crossing is read afterwards, and the queue that
// results rides out the next stall.
//
// The figure is set well above one picture packet at its worst so an ordinary
// stall is absorbed, and well below RX_INACTIVE (5000 ms) so a channel that has
// genuinely gone quiet is still noticed -- by the receiver's own inactivity
// timeout, which tears the session down and reconnects as before.
#define AUDIO_SILENCE_MAX_MS 3000u
// How long any one read may take before the packet is abandoned.
//
// One figure for every read, where there used to be 1500 written out at four
// call sites. It has to exceed the worst a picture packet can take on this link,
// which is what the sender's own stalls show: 614 ms measured, on a device busy
// drawing, and 378 ms on a slice of one. Fifteen hundred covered those and was
// still tight enough to expire on an ordinary retransmission.
//
// Raising it is safe in the direction that matters. A late packet arrives whole
// and is drawn late, which costs a fraction of a second of picture; abandoning
// it ends the session, which costs the channel. The two are not comparable, so
// the bound is set to the same patience the sound now has -- past that the
// session is over anyway, and there is nothing left to protect.
#define AV_READ_DEADLINE_MS 3000u
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
enum { AV_HELLO=1, AV_CONFIG, AV_AUDIO, AV_VIDEO, AV_END, AV_ERROR, AV_PALETTE };
// The only flag in use, and only on AV_VIDEO: this packet carries further
// stripes of the frame the previous one started, rather than a new frame. A
// frame cut into several packets sends them all with the same timestamp, and
// the timestamp must otherwise advance, so a continuation has to say so
// explicitly or it is indistinguishable from a repeat.
#define AV_VIDEO_CONTINUES 0x01u
typedef struct { uint8_t type, flags; uint32_t session, seq, pts_ms, length; } av_header_t;
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

// --- The indexed picture -------------------------------------------------
//
// A video payload is a length table followed by the compressed stripes it
// describes:
//
//   [u8 first stripe][u8 count][u16 length * count][compressed stripes]
//
// with the lengths big-endian and counting the compressed bytes that follow.
// The stripes are consecutive, so a packet describes exactly one run of the
// frame and the device can work out which panel rows to update from `first`
// alone.
typedef struct {
    unsigned first, count;
    // The length table, two big-endian bytes per stripe, and the compressed
    // bytes it describes. Two pointers rather than one because the table sits
    // before the data in the payload and the two are read at different times.
    const uint8_t *table;
    const uint8_t *data;
    size_t data_length;
} av_video_t;

// Read a payload's stripe table. False on any malformation: a count that runs
// past the end of the frame, a table that does not fit the payload, or lengths
// that do not add up to exactly the data present. The device ends the session
// on false, so a wrong picture can never be a partial one.
bool av_video_decode(const uint8_t *payload, size_t length, av_video_t *v);

// Where stripe `n` of this packet starts and how long it is, n counting from 0
// within the packet rather than within the frame.
bool av_video_stripe(const av_video_t *v, unsigned n,
                     const uint8_t **data, size_t *length);

// Turn the 512 bytes of a palette packet, 256 big-endian RGB565 entries, into
// the form the expander reads.
void av_palette_decode(const uint8_t raw[AV_PALETTE_BYTES],
                       uint16_t out[AV_PALETTE_ENTRIES]);

// One palette entry as RGB565, from a 3-3-2 index: three bits of red, three of
// green, two of blue, red in the high bits. This is the palette ffmpeg produces
// for `-pix_fmt rgb8` and it is fixed, so no palette need be sent for it --
// av_palette_decode is for the adaptive palette, which does vary per channel.
uint16_t av_palette_rgb565(uint8_t index);

// Expand `pixels` index bytes at the front of `buf` into 2*pixels big-endian
// RGB565 bytes, still at the front of `buf`.
//
// Backwards, and that is the whole trick: step i reads buf[i] and writes buf[2i]
// and buf[2i+1], and everything written so far sits at or above 2i+2, which is
// always greater than i. So no write can land on an index not yet read, and one
// buffer serves both as the decompressed stripe and as the bytes handed to the
// panel. Forwards, the first step would overwrite buf[1] before it was read.
void av_expand_indexed(uint8_t *buf, size_t pixels, const uint16_t *palette);
void av_expand_indexed(uint8_t *buf, size_t pixels, const uint16_t *palette);
