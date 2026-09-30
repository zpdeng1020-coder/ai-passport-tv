<p align="right">
  <a href="tcp-delta-progress-20260930.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# TCP path optimisation: progress and open problems (2026-09-30)

> **Update, end of day.** The device side is considered done for now, and the server has moved on from what sections 2, 5 and 6 below describe. The firmware now ships the 64 KB window and a faster expander; the server holds a **fixed frame rate** per channel and fits each frame to a byte target instead of stepping the frame rate. What was built and what is left is in [server-optimisation-handoff-20260930.md](server-optimisation-handoff-20260930.md), which is the file to read next. Sections 1, 3 and 4 below are still the measurements they were.
>
> **Not in the repository, on purpose:** the measurement instruments these figures were taken with (`main/av_demo.c`, `main/net_demo.c`, `main/jpeg_bench.c`, the `tools/sdkconfig.*` overlays for them, `tools/frame_size_lab.py`, the clip generators) and the three result write-ups that describe them (device playback limit, network playback results, network validation handoff). Where this document refers to one of them it is referring to something that only exists on the machine that ran it.

The UDP transport rewrite (the network playback results write-up, which is not in the repository) is **shelved**: it means changing the device receive task, the server's send path, the strict `seq` check in `av_stream_accept` and the audio flow control, and the viewer judged its picture and sound unusable (tearing and stuttering sound, even where it reached 20+ fps). This file records what has been done on the **product's TCP path**, what it measured and what is still wrong, so further TCP work starts here. Every figure is a **single run**.

## 1. Where things stand

- **The frame-rate cap on the product path was the server's rate controller, not the link, the TCP window or the device.** `server/rate.py` derives its ceiling as `VIDEO_BUDGET_BYTES / bytes per frame`, and the budget (185000 B/s) is, by its own comment, a working point that was never measured as a link limit. With live CCTV1 on the original server and the controller replaced by a fixed rate (`TV_ADAPTIVE=0 TV_FPS=25`), the device drew **24.3 fps (median)** at about 349 KB/s with no audio gaps, no dropped frames and no session reset (90 s, one run). That is the source's own frame rate.
- Raising the budget to 280000 gave 18.6-20.0 fps; the default gave 12.0. Every controller-on run ended a window with `sent 18x kB in the window` and a write time of 16-78 ms, far under the 250 ms slow-write line: the controller was stepping down on its own byte count while the link was not congested.
- **A 64 KB TCP window (with the Wi-Fi receive pool and mailbox enlarged to match) made playback smoother, not faster**: 0 audio gaps and 0 session resets in both runs against 3 and 9 gaps and one reset each at 32 KB, with the same median fps. The viewer also saw less stutter and tearing.
- **The streaming-stripe receive path freed about 28 KB of heap** (free heap 43 KB to 71 KB, largest block 25.6 KB to 53 KB) and removed the "no buffer, drop the packet" loss (`nobuf` 10-12 per 10 s to 0), with no measurable change in frame rate.
- What this overturns from earlier notes: the "TCP carries about 100 KB/s", "TCP steady state is 8-10 fps" and "the stalls come from the radio, TCP only turns them into resets" conclusions. The 120-150 KB/s plateau seen in every run was the controller's budget. Delta coding's apparent lack of gain (section 3) is explained the same way: the controller spent whatever delta saved on staying at the same bytes per second.
- Not yet shown: that a controller-free session holds up over minutes, on busy channels, or on a weak network. See section 5. **Later the same day:** with the new fixed-rate server on live CCTV1, one session ran 747 s with no failure at a 25 fps target and a byte rate that climbed to 583 kB/s (write times 31-78 ms); two 10 s device windows gave 24.5 and 24.1 fps with 4 dropped, no audio gaps, no reset. The viewer saw no stutter and no tearing at that rate. Still one channel, one link.

## 2. What was changed (working tree, nothing committed)

| File | Change |
|---|---|
| `main/av_player.c` | **Streaming stripe receive.** The receiver reads a packet's length table, then each stripe straight into a 16 KB byte ring (`AV_VIDEO_RING_BYTES`); one queue item per stripe (`AV_VIDEO_ITEMS` = 32); the picture task inflates from the ring and releases it (`ring_alloc`, `ring_release`). Replaces two 22 KB whole-packet buffers (45 KB). The receiver waits up to 100 ms for ring or queue space, then discards that stripe and counts the frame as dropped (`nobuf`). Stripes above 4096 bytes or tables that do not add up end the session |
| `sdkconfig.defaults`, `sdkconfig.av-prototype` | **Now merged (was `tools/sdkconfig.win64`):** `CONFIG_LWIP_TCP_WND_DEFAULT=65535`, `CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`, `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`. The overlay file is now redundant |
| `main/av_protocol.c/.h`, `main/av_player.c` | **`av_expand_indexed_wire`** (four pixels a step, palette held byte-swapped by `av_palette_wire_order`) replaces `av_expand_indexed` on the product path; host test `wire_expansion_tests` added, and an on-device equality check in the hardware bench (`cpu_expand_wire`). The old function stays for the demos and tests |
| `server/frames.py`, `server/live.py`, `server/rate.py`, `server/tv_server.py`, `server/media.py` | **Superseded by the server rework**, see the handoff. Delta coding is now the default (`TV_DELTA=1`, tolerance 1%), the frame rate is fixed per channel, and each frame is fitted to a byte target. A zero-length stripe is accepted by the device. Old firmware with the new server ends the session at the first zero-length stripe |
| `components/bsp/include/bsp_pins.h`, `main/av_demo.c`, `main/net_demo.c`, `main/jpeg_bench.c` and others | Earlier measurement work; none of it changes the default product path. Only `bsp_pins.h` (panel clock 80 MHz) is committed; the instruments are not |

## 3. Measured

All on live CCTV1 (`ch000`), device counters from the 10-second `interval_frames` lines (windows with under one frame dropped). `rx` is the device's received bytes per second. Audio gaps are `AUDIO_EMPTY` lines; resets are `RX_EXIT` lines.

**Baseline.** Original commit `c61b764` firmware and server, defaults, office Wi-Fi (ping mean 57 ms, max 165 ms): 7.7 fps median, 18.7 KB/frame, 128 KB/s, no reset in 150 s. The working tree's own baseline with delta off was 7.7 as well, so the work so far has not made the product path worse.

**Streaming stripes against whole-packet buffers** (computer's hotspot, ping mean 34 ms, 100 s each, alternating):

| Firmware | fps median | rx KB/s | audio gaps | resets | free heap | `nobuf` per 10 s |
|---|---|---|---|---|---|---|
| whole-packet (old) | 9.5 / 10.8 | 143 / 140 | 8 / 5 | 1 / 0 | 43 KB | 10-12 |
| streaming (new) | 9.5 / 7.4 | 128 / 110 | 11 / 17 | 0 / 0 | 71 KB | 0 |

No frame-rate difference can be told from this; the office-Wi-Fi attempt was worse (both 2-4 fps) because the network itself degraded that hour.

**TCP window** (new firmware, hotspot, ping mean 28 ms, 100 s each, alternating):

| Window | fps median | rx KB/s | audio gaps | resets |
|---|---|---|---|---|
| 32 KB | 11.9 / 11.8 | 143 / 129 | 3 / 9 | 1 / 1 |
| 64 KB | 11.2 / 12.0 | 149 / 131 | 0 / 0 | 0 / 0 |

**Rate-controller budget** (64 KB firmware, hotspot, original server code, 100 s each; the one reset in each run is me resetting the board to start the capture):

| `TV_VIDEO_BUDGET` / `TV_MAX_FPS` | fps median (mean) | rx KB/s | audio gaps | device-dropped frames |
|---|---|---|---|---|
| 185000 / 12 | 12.0 (10.8) / 12.0 (10.5) | 128 / 133 | 0 / 0 | 0 / 0 |
| 280000 / 20 | 18.6 (15.6) / 20.0 (16.2) | 224 / 194 | 0 / 0 | 0 / 0 |
| controller off, fixed 25 fps | 24.3 (23.3) | 349 | 0 | 0 |

The controller-on means are below the medians because of the start-up climb from `START_FPS=5`. In the controller-off run one window fell to 18.7 and the rest were 21-25.

**Delta coding, earlier today (office Wi-Fi, controller on):** 7.8 fps off against 10.7 and 7.1 on, in two runs. It gave no steady gain; see section 1 for why.

## 4. Mistakes made on the way (so they are not repeated)

- **First A/B was invalid.** `idf.py flash` rebuilds before flashing, so the "old" build was the new source; those captures were discarded. Flash saved binaries with `esptool write_flash 0x10000 <bin>` instead.
- **The unlimited-TCP `bulk` test was not a ceiling.** With the PC's Wi-Fi adapter still on the office network the hotspot's ping went to 300 ms and 33-100% loss while `bulk` ran, the device showed 12-39 KB/s with its CPU 98% idle. That measured a saturated hotspot, not a limit. Do not cite it.
- **The claim that a fixed 32 KB window caps throughput was wrong**: the 64 KB run shows the same rate. The window mattered for smoothness, not speed.
- Comparing runs taken at different times is unreliable on Wi-Fi (the same firmware gave 4.0 and 2.2 fps an hour apart); alternate the settings.

## 5. Open problems

1. **Controller-free operation is one 90 s run on one channel.** Needs minutes of runtime, several channels including fast-moving ones, and a weak network. Without a controller nothing slows the sender when the link degrades; what to replace the 185000 budget with (a measured value, or a controller driven by the write time and device-side signals rather than a byte count) is undecided. Deleting it outright is not proposed.
2. **Start-up climb.** *(Answered for the default sender: there is no frame-rate climb any more; the byte rate starts at 250 kB/s and takes a 5% step every two windows.)*
3. **The ring's 100 ms wait.** The receiver waits up to 100 ms for ring space with `vTaskDelay(1)`; while it waits it is not reading the socket, which is the audio path. No measurement yet shows it hurts; a fast-moving channel would.
4. **The 64 KB overlay** *(merged into `sdkconfig.defaults` and `sdkconfig.av-prototype`; public build passes, runtime free heap 71 KB)*. Not yet exercised from an erased device.
5. **Smoothness judged by eye** (less stutter and tearing at 64 KB) is the viewer's observation; the audio-gap and reset counts support it, but nothing else was instrumented.
6. **Delta coding is not evaluated with the controller off**, and nobody has looked at its picture quality.
7. **Verification gaps.** `tools/validate.sh` and the host C tests were not run (no native C compiler on this machine). `ring_alloc` and `ring_release` are pure logic with no host test. The device change was built as the public build only.
8. **Failing tests that predate this work:** one case in `tests/test_live_transcode.py`, two errors in `tests/test_tv_server.py`, and `test_heavy_video_preserves_audio_and_frame_protocol_on_slow_reader` in `tests/test_live_sender_v2.py`; they fail the same with the changes stashed. Tests on this machine need `PYTHONPATH=.`.

## 6. Current environment

- **The board is on the computer's hotspot, not the office network.** NVS holds the hotspot's name and the computer's hotspot address on port 8096; the office credentials are backed up in `build-delta/ab/nvs_office_backup.bin` (ignored directory, not in the repository). To go back, restore it or run `tools/set_wifi_cred.py` with the office values (`tools/set_wifi_cred.py --list` shows what is stored).
- The board runs `build-fixed` (streaming stripes, the 64 KB window as a default, the wire-order expander, product public build), flashed 2026-09-30.
- The server last left running was the **working-tree code**: `TV_ADAPTIVE=0 TV_RATE_START=500000 TV_RATE_MAX=500000 python -m server.tv_server live --channel ch000 --bind <this machine's address>`. **Check `netstat -ano | grep :8096` before starting another. A stale server from an earlier session was silently holding the port and the device was connecting to that one, not to the new one, while every log looked plausible.** The worktree `../ai-passport-tv-head` (commit `c61b764`) still exists; remove it with `git worktree remove ../ai-passport-tv-head` when done.
- Saved old-firmware binaries are in `build-prev-saved/`; build directories and logs (`build-*`, `build-delta/ab/`) are not tracked.

## 7. Reproduce

```text
# Firmware: product public build (the 64 KB window is in the defaults now)
idf.py -B build-fixed -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype" -D SDKCONFIG=build-fixed/sdkconfig build
# On this machine run through tools/idf-run.ps1. Flash with: python -m esptool --chip esp32c3 -p COM4 -b 460800 write_flash 0x10000 build-fixed/FoloToy-AI-Passport.bin
# (segmented only, never erase-flash). If an overlay is added later, delete <build>/sdkconfig first or it is not applied.

# Server: the current knobs are in the handoff. Fixed 500 kB/s on a live channel:
TV_ADAPTIVE=0 TV_RATE_START=500000 TV_RATE_MAX=500000 python -m server.tv_server live --channel ch000 --bind <this machine's address>

# Tests
PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py && PYTHONPATH=. python tests/test_rate.py
```

```text
Build: PASS (product public build, streaming stripes; 64 KB window; wire-order expander)
Host tests: 12 delta tests PASS earlier; 4 other Python tests fail the same on the unmodified tree; C host tests NOT RUN; tools/validate.sh NOT RUN
Device tests: single 90-150 s runs on live CCTV1, sections 3 and 4
Unverified: controller-free operation over minutes, other channels, fast-moving content and weak network; the ring's effect on audio under load; delta picture quality; the 64 KB overlay in a shipped config
```
