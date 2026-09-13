English | [简体中文](local-tv-prototype.zh_CN.md)

# Standalone local audio/video prototype

This optional boot entry plays live or pre-generated 160x120 baseline YUV420 JPEG and
16 kHz mono s16le PCM over one authenticated LAN TCP connection. The server's
`live` subcommand transcodes a channel in real time; `run` replays prepared media.
The channel list and the current channel are always chosen by the server, and the
allowlisted addresses are unverified third-party relays. The original
menu, demos and pixel theme remain unchanged. `CONFIG_AV_RAW_PROTOTYPE` defaults
to disabled; raw mode does not start LVGL or BLE. This is not an Internet URL
proxy or a production player. TCP/token pairing is not encryption; use a trusted
LAN only.

## Private configuration and builds

Create **only locally**, with mode `0600`, the ignored `main/av_private_config.h`.
Define `AV_WIFI_SSID`, `AV_WIFI_PASSWORD`, `AV_SERVER_IPV4` (an IPv4 literal),
`AV_SERVER_PORT` (integer, normally 8096), and `AV_PAIRING_TOKEN`. Do not paste
values into tracked files, logs or commands. No private header means compilation
still succeeds and raw boot reports missing configuration without networking.
The application never erases NVS and configures Wi-Fi storage as RAM.

Activate ESP-IDF 5.5.3, then run from the repository:

```bash
./tools/validate.sh --static
./tools/validate.sh --firmware   # original demo, public/no private header
./tools/validate.sh --prototype  # RAW overlay, public/no private header

# Actual local device image; contains secrets, do NOT publish build-av files.
idf.py -B build-av -D SDKCONFIG=build-av/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype' \
  -D AV_PUBLIC_BUILD=OFF build
idf.py -B build-av merge-bin -o build-av/FoloToy-AI-Passport-full.bin
python3 tools/verify_firmware.py build-av
```

The public validation modes explicitly set `AV_PUBLIC_BUILD=ON` so they never
include a developer's private header. Public raw firmware is a compile/layout
check and reports missing configuration at boot. `--prototype` writes its public
image to `build/FoloToy-AI-Passport-prototype-public.bin`, not the demo image.
Private `build-av` retains the segmented bootloader, partition table and app for
an authorized operator. Flash is never performed by these commands. Preserve the
8 MB/3 MB-app layout and `cardid@0x356000`; take two identical complete backups
before any provisioned-device write. Never erase the device or publish private
merged images. Restore demo mode with a separate default build, not by deleting
the original UI.

## Wire contract

The fixed 24-byte network-order header is Python `!4sBBHIIII`: `FAV1`, version 1,
type, zero flags, session, global sequence, PTS milliseconds, payload length.
HELLO (1) has header session/seq/PTS zero and JSON `{token,version:1}`, plus an
optional `channel` naming the channel to play (omit or send an unknown id to get
the server default). CONFIG (2) has a nonzero session, seq/PTS zero, and matching
JSON session. Required values: width 160, height 120, fps 12, sample_rate 16000,
channels 1, sample_bits 16, audio_chunk_ms 20, video_max_bytes 24576. Optional
`start_delay_ms` must be 200; additional server timing fields are tolerated.

`channels` is the audio channel count and must stay a number. The selectable
channel list travels in the separate `channel_list` array of `{id,name}` objects,
which the device steps through on UP/DOWN clicks. A server that omits it leaves
the device on its current channel. Keep the two keys distinct: sending the list
as `channels` fails the device's CONFIG validation and ends every session. The startup target is CONFIG
receipt +200 ms, but audio reset/prebuffer can delay it (see estimated clock).

AUDIO (3) is exactly 640 bytes/20 ms. VIDEO (4) is a complete baseline YUV420 JPEG,
1..24576 bytes; the ROM validates actual JPEG geometry/subsampling. END (5) has
zero payload. ERROR (6) and other control JSON are at most 1024 bytes. Bad magic,
version/type/flags, sizes, session, sequence, config, JPEG or truncated TCP input
terminates the session; there is no magic-search recovery. Before recursive JSON
parsing, a linear string/escape-aware scan limits nesting depth to four. A pre-CONFIG ERROR is
a terminal handshake failure, not accepted media. Remote JSON is never logged.
Sequences increase globally, but media PTS is checked independently: audio starts
at zero and advances by 20; video strictly increases and may skip frames. Video
PTS can be lower than an immediately preceding audio PTS. Loops must maintain
continuous PTS/sequence or start a new session; `duration_ms` describes server
material, not a firmware-enforced session timeout.

## Media conversion and compatibility

Both `prepare` and `import-video` generate actual baseline 160x120 YUV420 JPEGs;
manifest and CONFIG dimensions match. Existing 320x240 media/configs are rejected:
regenerate into a new directory rather than overwrite old material. Import fits
source display aspect ratio (including sample aspect ratio) into 16:12 (4:3), with
centered black letterbox/pillarbox, no cropping/stretching, and even YUV420 rounding
(up to two pixels of border asymmetry). PCM, 12 fps timestamps, limits and private
network configuration are unchanged. See [server instructions](../server/README.md).

## Ownership, memory and lifecycle

- Socket worker: sole descriptor owner, nonblocking connect/read/write with
  bounded polling/deadlines. Complete PCM/JPEG is queued; no consumer reads TCP.
- Audio worker: existing BSP I2S/codec only; 100 ms maximum per write, partial
  submission treated as failure. A 20-entry PCM queue provides 400 ms/12800 bytes,
  plus one worker/receiver chunk. Startup waits for five chunks. Queue full,
  starvation or feed gaps reset the connection and synchronization origin.
- Video worker: sole raw panel owner, no LVGL callback. Exactly two 24 KiB JPEG
  buffers, two 320x16x2 internal DMA strips (20480 bytes) and 4096-byte decoder
  workspace. A full JPEG pool drops a whole incoming frame without holding audio
  behind decoding. C3 ROM tjpgd still outputs RGB888 (not LEO's LVGL BGR decoder),
  decoding 160x120 at scale=0, followed by x2 nearest-neighbor RGB565 conversion
  to the 320x240 panel. Each 16-source-row MCU row fills both 16-target-row strips;
  source y=112..119 fills only the final target y=224..239 strip. The packer clips
  each MCU to a strip using source-rectangle row stride. Both strips are filled
  before sequential DMA submission/wait; buffers are never reused before completion.
  This retains 20480 bytes of strip RAM, without claiming decode/DMA overlap or
  faster playback. This is a LEO-style source-size change, not a 20 fps replication.
- Worker stacks: receiver 5120, audio/video 4096 bytes each; queues, driver DMA,
  Wi-Fi and BSP overhead are additional. The overlay uses 160 MHz and bounded
  Wi-Fi RX/TX pools. Runtime free/minimum/largest heap is authoritative, not a
  build-size estimate. Allocation failure leaves the test stopped.
- Buttons enqueue an eight-event queue of key+gesture pairs without blocking. OK
  toggles stop/restart; UP/DOWN clicks step to the next/previous channel by
  reconnecting with the new id; UP/DOWN long presses adjust volume 10..100% in
  5-point steps, initially 55%. Startup displays raw red/green/blue bars; a
  stopped image remains on screen.
- Stop/error sets cancellation, socket producer closes, consumers finish bounded
  operations and signal final resource access; only then are session queues and
  buffers freed. No worker is forcibly deleted. LCD DMA timeout deliberately
  retains buffers and waits for completion rather than causing use-after-free;
  ten failed 200 ms waits cause an explicit fault-only reboot without freeing
  DMA buffers. The standalone control,
  buttons, Wi-Fi event loop and BSP remain owned for the boot lifetime, while
  per-session allocations are released. There is no return-to-LVGL transition.
- Five seconds without receive progress triggers reset; no fixed total session
  timeout is imposed. Normal END drains queued work; reconnect backs off one
  second, clears all state and starts a new handshake. Continuous-PTS server
  material loops do not themselves reset the firmware.

## Timing and measurements: explicitly estimated

Logs use `CLOCK_ESTIMATED`. Submitted sample count means bytes accepted by the
I2S driver, **not DMA-completed samples and not acoustic output**. The monotonic
origin adds an assumed 90 ms (six 240-frame descriptors at 16 kHz) to the first
write; playback position is capped by submitted duration. This budget is not an
observed delay. Startup/reset flushes silence muted before establishing a new
origin. Physical codec/speaker delay, actual DMA fill, sample-clock drift and
network latency remain unmeasured. Video currently waits for estimated PTS before
decoding, so display can lag by decoding/strip transfer time; frames already over
100 ms late are dropped. This is a bounded-latency MVP, not synchronization
acceptance. Measure flash/beep markers before tuning clock offset or decode lead.

Every ten seconds logs report actual interval rendered frames and duration,
drops, queue high-water marks, decode maximum and heap/largest block. Session-end
logs add observed rendered FPS (including startup/drain), submitted samples,
maximum feed gap, minimum heap, and explicit unmeasured DMA/acoustic fields.
Target 12 fps is never reported as achieved frame rate. Logs contain no app-level
SSID/password/token/remote JSON; stock Wi-Fi driver logs may reveal network
identifiers, so device logs must be sanitized before sharing.

## Validation and remaining device checks

`tests/test_av_protocol.c` is pure C: header endian/length/type limits, malformed
headers, reassembly boundaries, sequence/session rejection, independent media
PTS, reconnect reset and RGB stripe geometry/byte order. Full-frame MCU tests
cover x2 duplication, source stride, seams, guard bytes and the final 8-row MCU. Static checks run these
alongside server and actual ffmpeg import/preparation tests when ffmpeg is available. These tests do not emulate ROM JPEG, LCD DMA,
I2S, Wi-Fi, concurrency or acoustic timing.

Report Build, Host tests and Device tests separately. Before hardware acceptance,
check RGB bars/orientation, JPEG rendering speed, mono PCM rate and volume,
flash/beep offset/drift, >30-minute continuous playback and server material loops,
queue/heap bounds, missing/slow server, bad/truncated JPEG, Wi-Fi loss, audio
underrun, repeated OK stop/restart and post-stop DMA ownership. A successful
build/merged-layout validation does not establish any of those physical results.
