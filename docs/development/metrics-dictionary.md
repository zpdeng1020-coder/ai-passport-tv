<p align="right">
  <a href="metrics-dictionary.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Data dictionary: what every counter actually counts

A figure whose meaning is misremembered is worse than no figure. Three rounds of
this project have been misled by one: `panel_frames` read as frames, `in_bps`
read as link throughput, `rx_bps` read as total throughput. This file exists so
the next reader does not repeat them.

**Read the "and not" column before using any number below.**

## Device counters

Read from the `CLOCK_ESTIMATED` line, once every ten seconds.

| Field | Counts | Accumulated at | **And not** |
|---|---|---|---|
| `interval_frames` | **complete pictures** in the interval | `s.decoded`, incremented only when all `AV_STRIPES` stripes of a frame have been drawn | the moment the picture was physically visible: the increment happens after the last stripe's submission, and that stripe's DMA may still be running |
| `interval_ms` | wall-clock length of the interval | — | exactly 10000: it is measured, and runs a little over |
| `panel_frames` | **video packets** processed | `push_stripe()` call sites | pictures. A frame that took two packets counts twice |
| `panel_ms` | wall-clock time across those packets, from before decoding to after the transfer wait | around the decode-and-draw block in `video_task` | CPU usage: it includes waiting for the display DMA. It is also not the physical completion time of the last stripe |
| `panel_wait_ms` | time blocked in `bsp_display_raw_wait()` | end of `push_stripe()` | total SPI time. It is the time the CPU was *blocked*, which with two buffers is near zero because the transfer overlaps |
| `inflate_ms` | zlib inflation of the stripes | around `tinfl_decompress` | — |
| `expand_ms` | index byte → RGB565 expansion | around `av_expand_indexed` | — |
| `enlarge_ms` | stripe enlargement | around `enlarge_stripe` | — |
| `overlay_ms` | menu and banner drawn into the stripe | around `overlay_stripe` | — |
| `submit_ms` | starting a display transfer | around `bsp_display_raw_submit_nowait` | the transfer itself: it returns once queued |
| `parts_ms` | `inflate + expand + enlarge + overlay + submit + panel_wait` | computed at print time | a measurement — it is those six, added up for the reader's convenience |
| `decode_max_ms` | the **worst** single packet's time in the interval | `video_task` | a mean. Used for margin, not for sustained rate |
| `dropped` | frames the device gave up, **from two sources** | receiver: an opening packet with no free buffer (`av_player.c:1131`, `:1172`, which also raises `nobuf`); and the drawing task judging a frame late (`:2305`) | one source. It **includes** frames lost to a full buffer, so it overlaps `nobuf` -- and the two are different units, frames against packets |
| `nobuf` | packets dropped because both packet buffers were held by the drawing task | `receive_task` | anything about the link |
| `starts` | video packets that opened a frame | `receive_task` | — |
| `late` | frames dropped because `estimated_pts() - v.pts > 100` | `video_task` | network lateness specifically; it is lateness against the playback clock |
| `stray` | **every non-first packet of a frame** | `receive_task` | an anomaly count. Normal continuation packets are counted here, so a healthy multi-packet frame raises it |
| `rx_pkts` / `rx_bps` | **video payload** bytes and packets read off the socket | `receive_task`, after a successful read | total socket throughput. It excludes audio entirely and excludes FAV1 headers, and the discard path on a full buffer is counted separately |
| `rx_audio` | audio packets read | `receive_task` | — |
| `io_ms` / `wait_ms` / `iters` / `per_iter_us` | socket read time / audio flow-control wait / loop iterations / mean read | `receive_task` | — |
| `in_bps` | compressed stripe bytes handed to the inflater | `av_player.c:1786`, **which is before** the three success tests at `:1789` (status, bytes consumed, output produced) | "only bytes that inflated successfully". On a healthy channel the two are the same; on a failing one the count happens first. It never sees a discarded packet, and it never sees audio |
| `hdrgap_max` | longest the receive loop went without a packet header | `receive_task` | — |

### The three that have actually been misread

**`panel_ms ÷ interval_frames` is the per-frame cost.** Dividing by
`panel_frames` gives the per-packet cost, which is a different number and was
used by mistake once, making the device look twice as fast as it was.

**`rx_bps` is not total throughput.** A window showing `rx_bps=106269` with
`rx_audio=250` is carrying `106269 + 250×1280/10 = 138269 B/s` of payload
before headers. Subtracting the audio from `rx_bps` double-counts its absence.

**`parts_ms` is checked against `panel_ms` on the same line.** Both are in each
line for exactly this purpose. Their relative difference runs to about 1.3% and
does not need to be under 1%: part of it is per-field truncation (each of the six
is divided by 1000 before printing, up to 5 ms in total) and part is real
untimed loop overhead, 4–18 ms per window. Neither is a fault. What *would* be a
fault is `parts_ms` exceeding `panel_ms`, which would mean a timer running twice -- **except that this is not safe to conclude either.** The parts are accumulated as the work happens, the total is accumulated when the packet finishes, and the statistics read each atomic counter separately rather than taking one snapshot at a common completion boundary, so a negative difference across windows is possible without any timer being wrong.

**So this dictionary says plainly: the counting windows are not guaranteed to align.** Aligning them means publishing the cumulative totals together at the end of a packet, or giving the snapshot a consistency mechanism. Until then, no per-field difference should be read on its own as evidence of a timing fault.

## Server counters

| Field | Counts | **And not** |
|---|---|---|
| `video_sent` / `video_pps` | **video packets** / packets per second | frames per second. A frame may take more than one packet |
| `prod_drop` | frames discarded by the video queue for **either** reason: overflow **or** being superseded while pairing | a single-cause fault count |
| `late_video` | times the sender picked a frame and then abandoned it because the socket was not writable that pass | device-side lateness. **The name is not the condition**: the increment is on the `writable` test, not on a slot expiring |
| `dropped` (to `RateController`) | frames **this sender** gave up in the window | device feedback. There is no reverse channel; the device reports nothing |
| `frame=` in the `RATE` line | `smoothed/instantaneous` bytes per frame, from `video_bytes / frames` | two measurements. They are two estimates of one quantity |
| `ceiling=` | `budget / frame_bytes`, clamped to `[MIN_FPS, MAX_FPS]` | a measured rate. It is a calculation from a measured frame cost, and the budget it divides is an assumed working point (see `rate.py`) |
| `worst_write` | the slowest single **4096-byte slice** write in the window | the time to send a whole packet |
| `prod_drop` vs `video_pps` | production runs at `media.FPS` (follows `rate.MAX_FPS`), sending runs at the controller's rate | the same rate. Measured: 12 produced, about 5.5 carried, the surplus discarded at the queue |

## What is not measured at all

These are the gaps, listed so nobody assumes otherwise:

- **The device's own playback position** is not reported to the server. The
  server knows what it sent and when it wrote it, and nothing more.
- **The device's audio buffer depth** is not reported either.
- **The physical moment a picture appears** is not measured. `interval_frames`
  counts completed submissions, not photons.
- **The time the display DMA is actually busy** is not measured before the
  change to two buffers; `panel_wait_ms` measures the wait, not the transfer.
- **Content timestamps are not preserved** from ffmpeg through to playback. The
  alignment uses arrival times. See D4 in `state-20260916.md`.
