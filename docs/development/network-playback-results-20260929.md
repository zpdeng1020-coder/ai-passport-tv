<p align="right">
  <a href="network-playback-results-20260929.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Network playback results and standing bottlenecks (2026-09-29)

This records what was measured on the board while carrying out
[the network validation handoff](handoff-network-validation-20260929.md), and
which bottlenecks are still open. Every figure is a **single run** unless it
says otherwise, and none includes audio unless section 3.12 says so. Numbers come from the demo's own
per-5-second lines and from the server's log; the meaning of each field is in
section 2.

## 1. Summary

- The product path's earlier "9 fps ceiling" was an instrument artefact plus TCP
  behaviour, not a device limit. A minimal receiver that shares nothing with the
  product session drew **26-27 fps at 30 fps offered** over UDP, on the synthetic
  `demo_clip.bin` (average 10.7 KB a frame). On real channel footage (13.4 KB a
  frame) the same path drew only 20-21 fps at 25 fps offered (sections 3.11, 3.14);
  the 26-27 must not be quoted as what real television achieves.
- **TCP is the first wall.** On the same radio link, TCP delivered about 100 KB/s
  (5-280 KB/s within a second) and UDP delivered over 1 MB/s. TCP frames reached
  about 16 fps.
- **After UDP, the wall is bursty delivery, not the device.** Datagrams arrive in
  clumps with 100-480 ms gaps between them. The server sends evenly and the CPU
  is about half idle at 24-30 fps.
- **The receive memory cannot absorb a clump.** About 49 KB of internal heap is
  left after the demo's buffers are allocated; a 150 ms clump at 30 fps is about
  4.5 frames, which is the whole buffer. Frames that arrive with no room are
  counted as `nobuf` and lost.
- The panel is no longer the limit at 80 MHz (see section 3.6), and CPU is not the
  limit at 30 fps. **Neither of those was the reason 30 fps was not reached.**
- **The gaps already exist at the radio** (section 3.7): timestamps taken at the
  PHY show the same 60-120 ms gaps as the application sees. The device's driver,
  lwIP, scheduler and memory do not create them, and the receive-side A-MPDU
  settings do not change them (section 3.8).
- **JPEG is not a way out with the ROM decoder** (section 3.10): about 82 ms to
  decode one 320x180 frame, roughly 12 fps, against a 33 ms budget.
- **Sending only the stripes that changed halves the bytes** (7.6 against 13.4 KB
  a frame) and lifts the drawn rate in the same clip from 19.9 to 25.5 fps
  (section 3.11). The 30 fps clip contained duplicated frames, so those fps are
  inflated; the sources are 25 fps.
- **Audio costs the picture nothing, but memory is the wall** (section 3.12): a
  larger audio queue starved Wi-Fi of buffers and made both worse.
- **The gaps are not the computer's adapter, not the beacon cycle, and not other
  stations** (section 3.13); they are still unexplained.
- **Steady 25 or 30 fps was not reached.** Latest-stripe slots look better in
  counters but showed visible and audible tearing (section 3.14).
- **No step of an 80 MHz sweep of the offered rate is free of loss** (section 3.15): 16 fps already loses 9-12% and 30 fps loses 17-26%; `nobuf` is most of it, and parity datagrams could recover a third at most.
- **The UDP transport rewrite is shelved and the product stays on TCP.** Delta did not give a steady frame-rate gain on the product's TCP path (section 3.18); progress and problems on the TCP side are in [tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md).

## 2. Instrument

A minimal network benchmark, deliberately not built on the product's session,
receive task or packet protocol (which is what is being measured against):
`main/net_demo.c` on the device, `tools/net_demo_server.py` on the computer.
Enabled with `tools/sdkconfig.net-demo`; the product image is unchanged when the
option is off.

| Step | What runs | What it measures |
|---|---|---|
| 1 | colour bars on the panel, status in the letterbox bars | the panel works; nothing else |
| 2 `bulk` | server sends zeros over TCP, device counts | TCP receive rate |
| 3 `frames` | server sends `demo_clip.bin` frames over TCP, unpaced; TCP back-pressure paces it | receive + inflate + expand + draw ceiling over TCP |
| 4 `udp` | server sends 1400-byte numbered datagrams at stepped rates | UDP receive rate, loss, reordering |
| 5 `udpframes` | server sends frames as datagrams at a fixed or stepped fps | UDP frame rate, and why frames are lost |

Device log fields (per 5 s window):

| Field | Meaning |
|---|---|
| `fps_x10` | frames drawn per second, times 10 |
| `rx_frames` | frames fully received in the window |
| `nobuf` (cumulative) | a frame's first datagram arrived and no buffer was free, so the frame was dropped |
| `inc` (cumulative) | a frame was still incomplete when the next one started |
| `inf/exp/sub_x100` | mean per-frame inflate / palette expand / panel submit, in units of 10 microseconds |
| `net_wait_ms` | time the draw task sat idle with no frame to draw |
| `JITTER arr_min/max_ms` | shortest and longest gap between two frames' first datagram |
| `JITTER burst` | number of frame pairs that arrived less than 15 ms apart |
| `CPU%` | share of CPU per task from FreeRTOS run-time statistics (tasks under 1% hidden) |

The server prints `max_late` (how late it started a frame against its own
schedule), `late>20ms` and `max_sendto` per step, so a sender that was itself
late can be told apart from a network that delivered late.

Two instrument faults corrected along the way, because both produced
plausible-looking numbers:

- Two copies of the probe were listening on port 8096 at once, so the device
  reached the older one; one round of readings was discarded.
- The first UDP design had the device send a hello datagram to the server. Over a
  Windows hotspot that inbound datagram never arrived (firewall), which read as
  "no data". The server now pushes to the device without needing a hello.

An earlier claim that 6 and 9 fps were "stable" through the product player and
`tools/transport_probe.py` is **withdrawn**: the probe log shows only the 6 fps
step completing and the session resetting inside the 9 fps step.

## 3. Results

Sections 3.1-3.10 use the synthetic `demo_clip.bin` (average 10.7 KB a frame, three complexity tiers). Sections 3.11 onward use real channel footage, which is larger per frame (13.4 KB full), so fps figures in the two groups are **not comparable**.

### 3.1 TCP

| Test | Result |
|---|---|
| `bulk` (device only counts bytes) | about 100 KB/s average, 5-280 KB/s second to second |
| `frames` (receive, inflate, draw) | about 16 fps average, 9-23 fps second to second |
| `frames`, TCP window 65535 and deeper queues | about 9 fps, 91 KB/s; **no improvement** (single run, so "no better", not "worse") |

The device's draw task was idle 50-100% of each second in these runs, so TCP was
starving it. The product's own comment in `sdkconfig.defaults` records that going
from a 5.7 KB to a 32 KB window was the change that mattered; 64 KB gave nothing
more.

### 3.2 UDP, raw datagrams (device only counts)

| Server sends (KB/s) | Device receives (KB/s) | Loss per second |
|---|---|---|
| 100 | about 100 | 0 to 2% |
| 200 | about 200 | 0 to 1%, one second at 9% |
| 300 | about 290 | 0 to 1%, one second at 14% |
| 400 | about 390 | 1 to 3%, one second at 11% |
| 600 | about 570 | 3 to 5%, up to 19% |
| 800 | about 750 | 3 to 6%, up to 12% |
| 1200 | about 1100 | 3 to 22% |

Reordering was zero throughout. The video needs about 200-300 KB/s at 20-30 fps,
so UDP has headroom that TCP did not.

### 3.3 UDP frames, stepped rate (panel clock was then 40 MHz)

The product's panel clock is now 80 MHz; the 40 MHz here is what the board ran when this was measured.

Ethernet-connected computer to office Wi-Fi. Each step 8 s.

| Offered fps | Drawn fps | Loss shown (cumulative) |
|---|---|---|
| 8 | about 7.5-8.9 | none |
| 12 | about 12 | `nobuf` 1 |
| 16 | about 15-17 | `inc` 2, `nobuf` 4 |
| 20 | about 20 (some seconds 15-18) | `nobuf` up to 13 |
| 24 | about 21-23 | `nobuf` up to 23 |
| 30 | about 22-31 | `inc` up to 16, `nobuf` up to 38 |
| 40 | about 24-38, very uneven | `inc` up to 28, `nobuf` up to 92 |

### 3.4 Where the time goes at 24 fps (panel clock was then 40 MHz)

As in 3.3, the panel figures below (8-10 ms a frame waiting on the panel) belong to the old clock; at 80 MHz the same step is about 1.9 ms (section 3.6).

| Item | Measured |
|---|---|
| Server lateness | `max_late` 0-1 ms, `max_sendto` 0.2-0.8 ms: the sender is on time |
| Draw task CPU | 30-45% |
| Idle | 50-65% |
| Wi-Fi task | about 2% |
| lwIP `tiT` task | about 1% |
| Receive task | 2-3% |
| Per-frame inflate | 12-14 ms (was 10-15 ms in the local demo) |
| Per-frame expand | about 2.7 ms |
| Per-frame panel wait | 8-10 ms |
| Longest single frame | 26 ms (occasional 35-46 ms) |

So the whole receive path (Wi-Fi, lwIP, receive task) costs about 6% of the CPU.
The earlier suspicion that receiving steals the CPU from inflating is **not
supported**: it costs little.

Frame arrival gaps at the device, same run: `arr_min` 4-5 ms, `arr_max` 130-480 ms,
`burst` 4-12 pairs per 5 s window. Frames stop, then several arrive within
milliseconds. That, not the draw time, is what fills the three buffers.

### 3.5 Network path comparison (24 fps, single runs)

| Path | Longest arrival gap | `inc` (cumulative) | `nobuf` per 5 s |
|---|---|---|---|
| Computer on Ethernet, device on office Wi-Fi | 140-480 ms | up to 38 | about 5-20 |
| Computer on the same office Wi-Fi (WLAN) | 130-405 ms | 0 | about 3-12 |
| Windows mobile hotspot, close range, 2.4 GHz | 105-320 ms (mostly 110-160) | 0 | about 3-11 |

The Ethernet-to-Wi-Fi hop was the source of the incomplete frames, not of the
gaps. The office access points and channel account for part of the gaps (about
300-480 ms down to about 110-160 ms on the hotspot) and **a residual of about
100-150 ms remains on a clean, close link**. Where that residual comes from is
not established.

### 3.6 80 MHz panel clock, receive ring, 30 fps offered

The panel clock was 80 MHz (upstream commit `20668230`, plus the matching
assertion in `main/av_player.c`; picture confirmed normal by eye). The UDP
receive path used a 48 KB ring that holds whole frames back to back instead of
fixed 22 KB slots.

| Item | Measured |
|---|---|
| Panel submit per frame | about 1.9 ms (was 8-10 ms at 40 MHz) |
| Inflate / expand per frame | about 13.3 ms / 2.9 ms |
| Draw task CPU | 43-45%, idle 48-50% |
| Drawn fps at 30 offered | 25-28, mostly 26-27 |
| `nobuf` | about 15-20 per 5 s window |

The 1.9 ms is only the time to *submit* stripes to the panel driver, not the time
the bus takes. A boot-time bench (`PANEL_BENCH`) that submits a full picture
and waits for DMA completion, with no CPU work, measured **13.9 ms per frame
(about 72 fps)**, close to the 11.5 ms theoretical floor for 320x180x16 bits at
80 MHz. Bus and CPU work overlap, so the pipeline is roughly 16-18 ms per frame,
which still leaves room at 30 fps, but the earlier "about 55 fps" was an
estimate from submit time and should not be quoted as a measured ceiling. The
device drew 26-27 because about 3-4 frames a
second found no room. The ring's depth counter printed garbage (a format/argument
mismatch in my code), so **how deep the ring actually got was not observed**.
48 KB holds about 4.5 average frames, less than the three fixed 22 KB slots
(66 KB) it replaced, so no improvement in `nobuf` is not surprising.

### 3.7 Where the gaps appear (layer probe)

Promiscuous-mode callback on the device, 30 fps offered, Windows hotspot on
channel 11 (20 MHz). Per 5 s window, gap buckets are <2, <10, <30, <60, <120,
>=120 ms between consecutive frames addressed to the device.

| Layer | Typical window |
|---|---|
| Air (PHY receive timestamp) | 958 / 146 / 36 / 15 / **35** / 0 |
| Driver callback | 968 / 137 / 35 / 15 / **35** / 0 |
| Application `recv()` return | 970 / 131 / 39 / 15 / **35** / 0 |

About 30-37 gaps of 60-120 ms per window are already present in the PHY
timestamps, and the three layers agree. So the stalls occur before the frames
reach the radio: air, access point, or the sender's adapter. About 15% of frames
had the retry bit set. All frames were aggregated. The statistic covers all
downlink data frames to the device, which here is almost only the video.

### 3.8 Receive block-ack window and aggregation (single runs each)

Same firmware, `CONFIG_ESP_WIFI_RX_BA_WIN` and AMPDU RX changed only. The boot
log confirmed the value in force (`rx ba win`); the `winSize:64` in the log is
what the access point negotiated, not this setting.

| Variant | Gaps of 60-120 ms per 5 s | Drawn fps | Note |
|---|---|---|---|
| BA 4 (baseline), two runs | 23-31 | about 26-27 | |
| BA 8 | 28-34 | about 26-27 | |
| BA 12 | 20-31 | about 26-27 | |
| AMPDU RX off | 20-31 | about 25-27 | `agg=0` confirmed aggregation was off |

No variant changed the gap count or the frame rate, so receive-side reordering
in the aggregation window is not the cause.

### 3.9 Office Wi-Fi with a wired computer, and sender spreading (30 fps)

The computer on Ethernet, the device on the office network (its office address).
`--spread` is the fraction of a frame period over which one frame's datagrams are
spread by the sender; each setting ran twice, alternating.

| Spread | Mean drawn fps | Lowest 5 s window | `inc` per 5 s | `nobuf` per 5 s |
|---|---|---|---|---|
| 0.4 (previous default) | 27.1 and 27.0 | 24.7-24.9 | 1.3 and 2.1 | 9.7 and 8.8 |
| 0.9 | 23.8 and 23.7 | 18.9-21.1 | 9.1 and 8.7 | 5.8 and 7.9 |

**Spreading more made it worse**, repeatably in both pairs: a frame that takes
most of a period to send is more often still incomplete when the next starts.
Sending a frame as a tight burst is better here.

Run-to-run spread is large for the same setting: an earlier 30 fps run on this
same path at spread 0.4 drew about 24-26 fps with `inc` about 9 per 5 s, against
27 fps and `inc` about 1.3-2.1 now. So differences of a fraction of an fps, or a
few `inc`, between single runs are not evidence.

### 3.10 JPEG decode speed on the C3 (single run)

Baseline 4:2:0 JPEG, 320x180, quality searched per content tier to about 5.5 KB a
frame, decoded with the ROM's TJpgDec (`esp_rom/tjpgd.h`) at 160 MHz, `-O2`,
into a 320x16 RGB565 strip buffer (the decoder emits RGB888; the conversion is in
my output callback and was not timed separately). Each row is 80 decodes (10
repeats of 8 frames). Nothing else runs: no audio, no network.

| Content (avg bytes) | Decode only | Decode plus panel DMA | Worst frame |
|---|---|---|---|
| Test pattern (5458) | 81.4 ms | 83.7 ms | 82 ms |
| Mandelbrot 1 (5451) | 81.5 ms | 84.0 ms | 84 ms |
| Mandelbrot 2 (5456) | 81.8 ms | 84.1 ms | 95 ms |

- About 12 fps, and essentially independent of content: the time is in the fixed
  dequantise, IDCT and colour conversion work, not in the entropy data.
- About 226 CPU cycles per pixel. One frame at 30 fps has about 92 cycles per
  pixel in total, so this decoder is about 2.4 times too slow before audio and
  receive are counted. The zlib path (inflate plus expand, about 16 ms) is about
  5 times faster.
- Panel DMA overlaps well: it added about 2 ms.
- This is **one simple, old decoder**, not "JPEG on the C3". An optimised library
  (for example JPEGDEC or `esp_new_jpeg`) may be several times faster; none was
  measured. To reach 33 ms it would have to be at least 2.5 times faster with no
  margin for audio or receive. Published examples found: JPEGDEC on a C3 at
  160x128 and 15 fps, and 28 fps on the dual-core original ESP32 at 320x240.
- Quality at 5.5 KB a frame was not evaluated, and neither was localised recovery
  from loss (a JPEG frame is one entropy stream; restart markers would be needed
  to bound damage to part of a frame).

### 3.11 Smaller frames: send only the stripes that changed (single runs)

Real channel footage (a local folder of captures, CCTV1), same frames and same 3-3-2 quantiser
in both variants; only the bytes per frame differ. `tools/make_real_clip.py`
builds the clips. In the `delta` variant a stripe whose coded pixels barely differ
from what the panel already shows is sent with length 0, and one stripe per frame
is refreshed regardless (rotating), so a lost stripe is repaired within 15 frames.
The device change is two lines: a zero-length stripe is skipped (no inflate, no
expand, no bus time).

| Whole-frame UDP, no audio, 30 fps clip | full | delta |
|---|---|---|
| Average frame | 13.4 KB | 7.6 KB |
| Drawn fps (mean, min) | 19.9 (14.0) | 25.5 (22.0) |
| `nobuf` per 5 s | 46.5 | 17.8 |
| Inflate per frame | 15.1 ms | 7.4 ms |

- Picture cost of delta against the full-coded frame: mean PSNR 72 dB, worst frame
  40.5 dB. **No one has looked at it as a still picture.**
- The gain depends on content: CCTV1 sends 8.3 of 15 stripes on average. Fast
  panning falls back towards a full frame (p95 stays at about 19 KB).
- **Correction to the fps figures above.** The sources are 25 fps (CCTV6 is 30;
  HEBEI averages 25). The 30 fps clip was a 25 fps source resampled to 30, and 34%
  of its frames are near-empty (at most one stripe) and count as "drawn" as soon
  as they arrive. The 25 fps clip has 17% such frames. Counted honestly, the 30 fps
  clip's 25.5 is nearer 17 real frames a second. **Comparisons inside one clip are
  fair; comparisons across the 30 fps and 25 fps clips are not.**

Tried offline only, not on the board (`tools/frame_size_lab.py`, CCTV1 and CCTV13,
192 frames; PSNR is against the full 320x180 3-3-2 frame):

| Coding | CCTV1 avg | CCTV13 avg | PSNR mean (worst) |
|---|---|---|---|
| Full 320x180 3-3-2 | 14.7 KB | 21.5 KB | reference |
| 16-colour palette per frame, 4 bits a pixel | 9.2 KB (12.5 full) | 12.9 KB (20.6 full) | better than 3-3-2: 27.0 vs 24.0 dB (CCTV1) |
| "Scanline": 320x90, rows doubled | 8.4 KB | 11.5 KB | 24.4 (22.4) / 22.3 (21.5) dB |
| Stripe interlace (odd/even stripes on alternate frames) | 7.3 KB | 10.8 KB | worst frame 14.0 / 10.0 dB |

Interlacing is not usable: on motion half the stripes are stale and the worst
frames fall to 10-14 dB. Scanline saves 43%, not 50%, and permanently loses
vertical detail. The 16-colour palette needs a device-side change and was not
tried on the board.

### 3.12 With audio (single runs)

The product's audio numbers: 16 kHz s16 mono, a 1280-byte chunk every 40 ms
(32 KB/s), on the **same UDP socket and read by the same task** as the picture; the
I2S DMA consumes a chunk every 40 ms whatever the network does and an empty queue
plays silence. Device queue: 8 chunks (320 ms); the product keeps 24.

| 30 fps clip | full | delta |
|---|---|---|
| Drawn fps, without audio | 19.9 | 25.5 |
| Drawn fps, with audio | 20.0 | 25.5 |
| Audio chunks lost | 3% | 3% |
| Silence played (share of time) | 10% | 4% |
| Silence episodes in 100 s | 98 | 43 |

Audio did not change the picture rate. It does not sound clean: 43-98 dropouts in
100 s, of which delta halves the count only because it leaves the CPU idle more.

**Memory is the hard limit.** With the 48 KB receive ring and an 8-chunk queue the
free heap was about 13 KB and `heap_min` fell to tens of bytes. Runs after that use
a 32 KB ring (heap about 30 KB free). At 25 fps, whole-frame delta clip:

| Audio queue | Drawn fps | Audio lost | Lowest free heap |
|---|---|---|---|
| 16 chunks (640 ms) | 17.4 | 15% | 172 B |
| 12 chunks | 20.3 | 8% | 68 B (ring believed 28 KB; the flag was not checked) |
| 8 chunks (control) | 21.3 | 7% | 652 B |

A larger audio queue made things worse: the Wi-Fi receive buffers could not be
allocated. **The product's 24-chunk queue will be tighter still than anything the
demo has run.**

Caveats: an earlier run with a server audio-pump bug (a one-second burst at start)
was discarded. An attempt to build with `AUD_QUEUE=6` did not take effect (only
`RING_BYTES` reached the compiler), so that variant ran with 8.

### 3.13 Where the gaps come from (extra probes, single runs)

`pktmon` was not available (no administrator rights, driver missing), so three
device-side and sender-side probes were used instead.

| Probe | Result |
|---|---|
| Sender forced onto wired Ethernet (`--ifindex`); adapter byte counters confirm the traffic left through Ethernet | gaps of 120 ms or more per 5 s: 9.1, against 9.0-9.8 with the Wi-Fi sender. Link-layer retries fall to 8% (14-17% with the Wi-Fi sender), but the gaps do not change |
| Where in the access point's beacon interval the frame that ends a 60 ms gap arrives (908 beacons) | 0.6-1.6% in every 10 ms bucket: no concentration, so not beacon/DTIM/power-save related. Power save is off (`WIFI_PS_NONE`) and the BSSID is pinned |
| Other stations' data frames between our frames, during a gap | none at all in most gaps (for example 10 of 11 gaps in one window): the channel was quiet, the access point was not busy serving others |
| Gap length | mostly 60-130 ms, sometimes 300-634 ms |

- The number of gaps of 120 ms or more was the same for the full, delta and audio
  runs (about 7-10 per 5 s), so it is independent of what is sent.
- Correlation between gaps per window and `nobuf` per window: 0.13 for full frames
  (there the CPU and buffer size dominated) and 0.61-0.68 for delta. With 16-19
  windows per run this is suggestive, not proof.
- What is left is the office access point's own queueing or aggregation. No second
  access point or second receiver was tried to confirm.

### 3.14 Latest-stripe slots, and what the user saw (single runs)

Mode 5 of the demo. There is no frame on the device: each of the 15 stripes has one
slot holding its newest complete compressed version, and a newer version replaces
an undrawn older one. One datagram carries one stripe (or a fragment of it), so a
lost datagram costs one stripe. Memory is fixed at 15 slots however long a gap
lasts.

| CCTV1 delta, 30 fps clip | Result |
|---|---|
| Datagram loss (sequence numbers) | 3.0% (3.5% with the full clip) |
| Complete stripes against offered | 1197 of 1245 per 5 s (96%); full clip 2084 of 2250 (93%) |
| With audio | 1194 stripes per 5 s; audio lost 3%, silence 5% of the time |
| Longest single draw | 2.7 ms |
| Oldest undrawn stripe behind the newest frame | 2-3 frames |

By these counters it looks better. **When the user watched it, both picture and
sound showed perceptible tearing.** The whole-frame mode, watched afterwards, was
described as matching expectation: no stripe misalignment, but dropped frames and
stutter, and holding even 30 fps looked hard. So slot mode is **not** the steadier
one to look at, and its counters do not measure what the eye objects to.

Whole-frame delta at the source rate, audio off (25 fps clip): 21.1 fps drawn
(min 14.3), 25 fps offered. With audio, 20 fps offered drew 17.6 and 25 fps drew
21.3, so lowering the rate did not lower the share of frames lost (about 12-15%).

An estimate, not a measurement: at 3% datagram loss and about 7 datagrams a frame,
1 - 0.97^7 = 19% of whole frames lose a datagram, which matches the 85-88% drawn.
Real loss comes in clumps, so this is only a rough fit. An XOR parity datagram per
frame (one lost datagram recoverable, 14% more bytes) was proposed and **not
built**.

### 3.15 Frame-rate sweep at 80 MHz: full against delta (single runs)

UDP, no audio, CCTV1's 25 fps clip, only the server's offered rate changed, a 40 s measured window per step, device `build-small`. `nobuf` and `inc` are mean increments per 5 s.

| Offered fps | delta drawn (min) | drawn/offered | `inc`+`nobuf` | full drawn (min) | drawn/offered | `inc`+`nobuf` |
|---|---|---|---|---|---|---|
| 16 | 14.5 (12.5) | 91% | 4.6 | 14.1 (12.6) | 88% | 6.2 |
| 20 | 18.5 (16.9) | 92% | 4.7 | 16.7 (14.4) | 84% | 12.9 |
| 24 | 21.0 (18.7) | 88% | 11.0 | 19.5 (16.8) | 81% | 16.8 |
| 28 | 23.6 (18.3) | 84% | 14.3 | 20.9 (16.9) | 75% | 24.5 |
| 30 | 25.0 (21.9) | 83% | 17.1 | 22.2 (19.1) | 74% | 28.0 |

- **No step is free of loss.** The loss fraction rises smoothly from about 9-12% at 16 fps to about 17-26% at 30 fps, with no knee. The earlier inference from section 3.3 (40 MHz) that 16 fps loses almost nothing **does not hold**.
- **Delta is better at every step and the gap widens with rate** (91% against 88% at 16 fps, 83% against 74% at 30 fps).
- **`nobuf` is most of the loss**: 12.1 against 5.0 (`inc`) for delta at 30 fps, 21.3 against 6.7 for full. A parity datagram per frame (which can only repair `inc`) would therefore recover about a third of the loss at most, not worth a protocol change.
- About 17% of the clip's frames are nearly empty and count as drawn the moment they arrive, so absolute rates are optimistic; comparisons between steps are fair.
- Same clip and path: delta at 30 fps drew 25.0, matching the 25.5 of section 3.11.

### 3.16 IPv6 off (single run, on the board)

`tools/sdkconfig.no-ipv6` turns off only `CONFIG_LWIP_IPV6`; the code uses only `AF_INET`. Same demo, same server arguments:

| | baseline | IPv6 off | difference |
|---|---|---|---|
| static DRAM | 164048 | 162456 | -1.6 KB |
| `allocated; heap` at boot | 60432 | 62372 | +1.9 KB |
| `heap_free`, steady, receiving | 27360 | 29432 | +2.1 KB |
| flash image | 1554402 | 1519230 | -35 KB |

The ESP-IDF guide says about 7 KB at run time; about 2 KB was measured, with no visible change in frame rate. The gain is too small: **not adopted**. `heap_min` is not comparable between the two runs (18840 against 192/420); the cause was not looked into.

### 3.17 Static memory ledger (`build-small`)

Static DRAM is 164 KB: code in IRAM 102.6 KB, `.bss` 47.8 KB, `.data` 13.6 KB. The largest `.bss` item is the application's own: `static player_t s` in `av_player.c`, 17.5 KB, of which `channel_t list[128]` is 8 KB. In the network-demo build the player never starts but `av_player.c` is still linked, so that 17.5 KB is wasted; not linking it would give it back to the heap (**not done**). In the product it is real use.

### 3.18 Delta on the product path (single runs)

With `TV_DELTA=1` on the server and firmware that skips zero-length stripes, the product's TCP path drew about 7-11 fps against a baseline of about 7.8, with no steady gain; a write stall of half a second or more every ten-odd seconds resets the session. Details, the change list and next candidates are in [tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md).

## 4. Bottlenecks that are still open

Ordered by how much they constrain a stable frame rate. The target should be the source rate, 25 fps for most channels (section 3.11), not 30; the earlier 30 was a resampling artefact. "Measured" means a number
above; "suspected" means an inference not yet tested.

1. **Bursty delivery of 60-120 ms gaps, about 6-7 a second, on every AP and path
   tried** (measured). It is present in the PHY timestamps (section 3.7), so it is
   not created by the device, and it is unaffected by the receive BA window or
   aggregation (3.8). What is left is the air, the access point, or the sender's
   adapter. Office Wi-Fi, an office WLAN sender, and a Windows hotspot all showed
   it with different shapes; no other AP or sender hardware was tried, and
   retransmission at the link layer (about 8-20% of frames retried) is a
   candidate that was not isolated.
2. **Receive memory cannot absorb a clump** (measured). About 49 KB of internal
   heap remains after the demo's buffers; a clump at 30 fps is about 4.5 frames.
   The average frame is 10.7 KB and up to 22 KB. There is no PSRAM, and Wi-Fi,
   lwIP, the display and the codec share the same internal RAM. Making the ring
   larger means taking memory from those, and **which of them can give it up was
   not investigated**.
3. **TCP is unusable at this rate on this link** (**overturned by later measurement**).
   The roughly 100 KB/s here is from a single run of the minimal demo and the
   opposite of what the product path showed: there the 120-150 KB/s plateau was the
   server rate controller's budget, and with the controller removed live video drew
   24.3 fps at about 349 KB/s. See
   [tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md).
4. **A frame is about eight datagrams and any lost datagram discards the frame**
   (measured as `inc`; small in these runs, 0-38). Cheap in a good network,
   costly in a weak one. Stripe-aligned datagrams (mode 5, section 3.14) lose only
   part of a frame but tore visibly; parity datagrams were proposed and not built.
5. **Inflate is the largest CPU item** (measured, 12-14 ms of about 18 ms). It is
   not the wall at 30 fps today (CPU about 44%) but it is where the margin would
   come from once audio and loss handling are added. Baseline JPEG was
   measured with the ROM decoder and is slower (section 3.10); a cheaper
   lossless-index codec was only estimated.
6. **The product still speaks TCP, with the 12 fps default cap in `server/rate.py`.** The UDP route is shelved; on the product side only the default-off `TV_DELTA` was added (section 3.18), and the demo's UDP results have not been carried into it.

## 5. What was changed

In the working tree (nothing committed):

- New: `main/net_demo.c`, `main/net_demo.h`, `tools/net_demo_server.py`,
  `tools/sdkconfig.net-demo`, `tools/sdkconfig.net-tcp` (TCP window experiment),
  `tools/sdkconfig.net-udp` (deeper datagram queue), `tools/sdkconfig.net-stats`
  (per-task CPU statistics).
- New: `main/jpeg_bench.c/.h`, `tools/make_jpeg_clip.py`,
  `tools/sdkconfig.jpeg-bench`, `main/jpeg_test.bin` (131 KB, generated); enabled by
  `CONFIG_AV_JPEG_BENCH`, off by default.
- `main/CMakeLists.txt`, `main/Kconfig.projbuild`, `main/main.c`: wire the
  `CONFIG_AV_NET_DEMO` option in; off by default.
- `tools/transport_probe.py`: `--clip` sends the frames of `demo_clip.bin`.
- New: `tools/make_real_clip.py` (full and delta clips from real footage),
  `tools/frame_size_lab.py` (offline coding comparison), `tools/serial_capture.py`,
  `tools/summarize_frames_log.py`. Generated clips are in `tools/clips/` (several
  MB of `.bin`/`.pcm`; not yet decided whether they belong in `.gitignore`).
- `main/net_demo.c`: zero-length stripes are skipped; the product's audio format
  on the same socket (`NET_AUDIO`, `AUD_QUEUE`); gap probes (`GAPCTX`, `BEACON`,
  `GAPLIST` lines); latest-stripe slot mode (`SLOTS` line). Two unused-variable
  warnings remain (`g_arr_last`, `p_rssi`).
- `tools/net_demo_server.py`: `--clip`, `--audio`, `--ifindex`, mode `udpslots`;
  `--fps` now holds the rate for about 1000 steps instead of 8.
- `components/bsp/include/bsp_pins.h`: `BSP_LCD_PCLK_HZ` is now 80 MHz, matching
  the upstream commit. `main/av_player.c`: the panel-floor `_Static_assert` is
  15 ms instead of 30 ms, which that assertion exists to force.
- **Hazard, fixed:** `CONFIG_AV_HW_BENCH_SPI80` doubled `BSP_LCD_PCLK_HZ`, so
  `tools/sdkconfig.playback-demo-spi80` would have asked for 160 MHz. The option, both
  `*-spi80` overlays and the override in `components/bsp/src/bsp_display.c` are removed;
  the panel clock is `BSP_LCD_PCLK_HZ` and nothing else. The stale 40 MHz / 30.7 ms
  comments beside the floor in `main/av_player.c` and `main/Kconfig.projbuild` now say 80 MHz / 15.4 ms.
- **The 40 MHz figures in [device-playback-limit-20260929.md](device-playback-limit-20260929.md)**
  (40.2 fps panel wall) describe the old clock and are not re-measured at 80 MHz
  for the local demo.

On the board (outside the repository): the stored network is the office Wi-Fi and
the server address is the office server address on port 8096, as originally. A temporary change to a
computer hotspot was made and reverted using a saved copy of the NVS. The board
currently runs the network demo with audio (`build-small`, 32 KB receive ring,
8-chunk audio queue), not the product image. The server was left running in
whole-frame mode, 25 fps, no audio.

## 6. Not done

- Audio was measured only from section 3.12 on, at the demo's 8-chunk queue, not
  the product's 24; earlier sections have no audio.
- Nobody looked at delta frames as stills, or at fast-motion content; the delta gain
  was measured on CCTV1 and HEBEI only.
- The UDP results are not in the product: it is still TCP with the 12 fps cap, and the UDP rewrite is shelved.
- No weak-network run (distance, interference, deliberate loss).
- Every figure is a single run; nothing was repeated. The acceptance definition
  proposed for "stable 30 fps" (60 s of at least 29 drawn frames per second, with
  `nobuf` and `inc` near zero, repeated once) has **not** been met.
- The receive ring's depth was not observed (section 3.6).
- The link-layer cause of the gaps was not isolated (bottleneck 1); TCP retransmit
  counters were not read (bottleneck 3).
- The size of `tinfl_decompressor` was logged at boot but its effect on memory
  (a smaller window for 3840-byte stripes) was not evaluated.
- `tools/validate.sh` was not run; the host C tests were not run.
- The ring depth is now in the log as `ring_high` (13 was read several times with a delta clip, against a slot limit of 15), but **its meaning was not verified**: with small frames it may be the slot count that fills first rather than the bytes, and the two were not separated.

## 7. Other information that may help

**What outside sources say** (searched, not reproduced on this board):

- Wi-Fi modem sleep is the usual cause of receive delays up to one beacon period
  (`esp_wifi_set_ps(WIFI_PS_NONE)`). It does not apply here: the device log shows
  `Set ps type: 0`. HT20 instead of HT40 is also suggested; the hotspot runs were
  already 20 MHz and still showed the gaps. Larger Wi-Fi RX buffer counts, BA
  window and UDP mailbox are the documented burst levers; those were tried
  (section 3.8) without effect. A forum thread describes bursty UDP on a C3 about
  every 200 ms with plain ESP-IDF and shows no fix. **No ready-made fix for the
  60-120 ms gaps was found.**
- The atomic14 `esp32-tv` player (read from its source, not run): HTTP,
  device-driven pull, one downloader task with keep-alive, a one-frame image
  buffer, always requesting the frame for the current audio time so a stall is
  skipped rather than replayed, audio fetched in 1-second chunks of 8-bit PCM. It
  reaches about 15 fps at 280x240 on a dual-core chip and has no prefetch queue.
  Nothing in it beats the current numbers. The two ideas it shares with this
  project are audio as the clock and skipping stale frames instead of replaying
  them; the product player already discards frames more than 100 ms behind the
  audio clock, and the network demo does not, because it has no audio.

**Ideas considered and not tested:**

- Make the frames smaller on the server. The zlib stripe format could carry
  5-6 KB a frame at lower quality with no device change. This is the only lever
  found that acts on the memory bottleneck without touching the link. Quality was
  not evaluated.
- Give the receive path more memory by finding what Wi-Fi, lwIP, the display and
  the codec can give up (a memory ledger, bottleneck 2). The size of
  `tinfl_decompressor` is logged at boot, and whether a smaller inflate window
  for 3840-byte stripes would free memory was not checked.
- Try a different access point, a different sender adapter, or a wired-to-AP
  sender, to see whether the 60-120 ms gaps move (bottleneck 1). A link-layer
  retransmission count was not read.
- A pull model like esp32-tv's, driven by the audio clock, would never receive a
  clump it cannot hold; it costs a round trip per request, which the 60-120 ms
  gaps would expose, so it needs several requests in flight.
- Stripe-aligned datagrams (each carries whole stripes) so loss costs part of a
  frame instead of the frame (bottleneck 4). Not built.
- Restart-marker JPEG for the same reason, only if a faster decoder makes JPEG
  viable.
- UDP transport and the product's 12 fps cap (bottleneck 6): UDP is shelved and the demo's UDP results have not been carried into the product. Progress on the TCP side is in [tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md).

**Choices made along the way that a reader may want to revisit:**

- UDP is unicast, one datagram carries 1384 bytes of payload plus a 16-byte
  header, and the server pushes without any device-to-server datagram (an inbound
  datagram was blocked on the hotspot).
- A frame missing any datagram when the next frame starts is dropped whole; the
  receiver never blocks, so a slow device shows up as `nobuf`.
- The receive ring is 48 KB and holds whole frames back to back; TCP-mode slots
  were cut to one to fit it. Its depth counter is wrong, so its behaviour under
  clumps is not established.

## 8. Reproduce

```text
# firmware (public build), with the demo overlay and the experiment overlays you want
idf.py -B build-net -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype;tools/sdkconfig.net-demo;tools/sdkconfig.net-udp;tools/sdkconfig.net-stats" -D SDKCONFIG=build-net/sdkconfig build
# run through tools/idf-run.ps1 on this machine; flash segmented, never erase-flash

# server (one at a time; check no old copy is listening on 8096)
python tools/net_demo_server.py bulk | frames | udp | udpframes | udpslots [--fps N] [--clip F] [--audio F.pcm] [--ifindex N]
```

After the board is reflashed, restart the server: it serves one session at a time
and keeps sending to the dead one. Overriding `RING_BYTES` through
`-D CMAKE_C_FLAGS=-DRING_BYTES=...` should be checked in `compile_commands.json`.

The device dials the address in its NVS; `tools/set_server_addr.py` changes it.
To read Wi-Fi and server entries use `tools/set_wifi_cred.py --list`, and pass
`--esptool "python -m esptool"` (the ESP-IDF environment has no `esptool` module).

```text
Build: PASS (net demo, ring + 80 MHz variant, public build, within the 3 MB limit)
Host tests: NOT RUN (no native C compiler)
Device tests: measured on the board, single runs; audio from section 3.12 only; see section 6
Unverified: cause of the residual 100-150 ms bursts; ring depth; audio; weak network
```
