<p align="right">
  <a href="tcp-delta-progress-20260930.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# TCP path optimisation: progress and open problems (2026-09-30)

> **Update (2026-10-01, checked against the code).** This file was re-checked against commit `208357b`. The device-side changes (streaming stripes, the 64 KB window, the wire-order expander) are committed in `ed79a05`; the server now holds a fixed frame rate per channel and fits each frame to a byte target (`e92e422`, `a4510d7`), so it is no longer what sections 2, 5 and 6 below originally described. What the server does and what is open there is in [server-optimisation-handoff-20260930.md](server-optimisation-handoff-20260930.md). The measurements in sections 1, 3 and 4 are the single runs they were; none was repeated.
>
> **The measurement instruments are in the repository** (`c74b1a6`): `main/av_demo.c`, `main/net_demo.c`, `main/jpeg_bench.c`, the `tools/sdkconfig.*` overlays, `tools/frame_size_lab.py` and the clip generators. Each is off by default in Kconfig (`AV_HW_BENCH`, `AV_DEMO_PLAYBACK`, `AV_NET_DEMO` and `AV_JPEG_BENCH` are all `default n`). The generated clips (`main/demo_clip.bin`, `main/jpeg_test.bin`, `tools/clips/`) are not committed; the scripts regenerate them. The three result write-ups are committed too: [device playback limit](device-playback-limit-20260929.md), [network playback results](network-playback-results-20260929.md), [network validation handoff](handoff-network-validation-20260929.md).

The UDP transport rewrite (see [network playback results](network-playback-results-20260929.md)) is **shelved**: it means changing the device receive task, the server's send path, the strict `seq` check in `av_stream_accept` and the audio flow control, and the viewer judged its picture and sound unusable (tearing and stuttering sound, even where it reached 20+ fps). This file records what has been done on the **product's TCP path**, what it measured and what is still wrong, so further TCP work starts here. Every figure is a **single run**.

## 1. Where things stand

"The controller" in this section and section 3 means the old `rate.py` controller, which derived a frame-rate ceiling from `VIDEO_BUDGET_BYTES`. The default sender no longer uses it: `TV_ADAPTIVE` is off by default (`server/rate.py:187`) and the per-frame byte target defaults to `TV_FRAME_BYTES=20000` (`server/rate.py:615`). Only the `TV_LIVE_ENGINE=v2` sender still drives the old controller (see the comment at `server/rate.py:601`).

- **The frame-rate cap on the product path was the server's rate controller, not the link, the TCP window or the device.** `server/rate.py` derives its ceiling as `VIDEO_BUDGET_BYTES / bytes per frame`, and the budget (185000 B/s) is, by its own comment, a working point that was never measured as a link limit. With live CCTV1 on the original server and the controller replaced by a fixed rate (`TV_ADAPTIVE=0 TV_FPS=25`), the device drew **24.3 fps (median)** at about 349 KB/s with no audio gaps, no dropped frames and no session reset (90 s, one run). That is the source's own frame rate.
- Raising the budget to 280000 gave 18.6-20.0 fps; the default gave 12.0. Every controller-on run ended a window with `sent 18x kB in the window` and a write time of 16-78 ms, far under the 250 ms slow-write line: the controller was stepping down on its own byte count while the link was not congested.
- **A 64 KB TCP window (with the Wi-Fi receive pool and mailbox enlarged to match) made playback smoother, not faster**: 0 audio gaps and 0 session resets in both runs against 3 and 9 gaps and one reset each at 32 KB, with the same median fps. The viewer also saw less stutter and tearing.
- **The streaming-stripe receive path freed about 28 KB of heap** (free heap 43 KB to 71 KB, largest block 25.6 KB to 53 KB) and removed the "no buffer, drop the packet" loss (`nobuf` 10-12 per 10 s to 0), with no measurable change in frame rate.
- What this overturns from earlier notes: the "TCP carries about 100 KB/s", "TCP steady state is 8-10 fps" and "the stalls come from the radio, TCP only turns them into resets" conclusions. The 120-150 KB/s plateau seen in every run was the controller's budget. Delta coding's apparent lack of gain (section 3) is explained the same way: the controller spent whatever delta saved on staying at the same bytes per second.
- Not yet shown: that a controller-free session holds up over minutes, on busy channels, or on a weak network. See section 5. **Later the same day:** with the new fixed-rate server on live CCTV1, one session ran 747 s with no failure at a 25 fps target and a byte rate that climbed to 583 kB/s (write times 31-78 ms); two 10 s device windows gave 24.5 and 24.1 fps with 4 dropped, no audio gaps, no reset. The viewer saw no stutter and no tearing at that rate. Still one channel, one link.

## 2. What was changed (all committed)

| File | Change |
|---|---|
| `main/av_player.c` | **Streaming stripe receive.** The receiver reads a packet's length table, then each stripe straight into a 16 KB byte ring (`AV_VIDEO_RING_BYTES`); one queue item per stripe (`AV_VIDEO_ITEMS` = 32); the picture task inflates from the ring and releases it (`ring_alloc`, `ring_release`). Replaces two 22 KB whole-packet buffers (45 KB). The receiver waits up to 100 ms for ring or queue space, then discards that stripe and counts the frame as dropped (`nobuf`). Stripes above 4096 bytes or tables that do not add up end the session, and so does a queue that stays full for 200 ms (`video_enqueue`) |
| `sdkconfig.defaults`, `sdkconfig.av-prototype` | **Now merged (was `tools/sdkconfig.win64`):** `CONFIG_LWIP_TCP_WND_DEFAULT=65535`, `CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`, `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`. The overlay file has been deleted |
| `main/av_protocol.c/.h`, `main/av_player.c` | **`av_expand_indexed_wire`** (four pixels a step, palette held byte-swapped by `av_palette_wire_order`) replaces `av_expand_indexed` on the product path; host test `wire_expansion_tests` added (`tests/test_av_protocol.c`), and an on-device equality check in the hardware bench (`cpu_expand_wire`). The old function stays for the demos and tests |
| `server/frames.py`, `server/live.py`, `server/rate.py`, `server/tv_server.py`, `server/media.py` | **Superseded by the server rework**, see the handoff. Delta coding is now the default (`TV_DELTA=1`, tolerance 1%), the frame rate is fixed per channel, and each frame is fitted to a byte target. A zero-length stripe is accepted by the device (`av_player.c` queues a zero-length stripe as-is). Old firmware with the new server ends the session at the first zero-length stripe (as recorded originally, not re-tested on old firmware); current firmware does not have this problem |
| `components/bsp/include/bsp_pins.h`, `main/av_demo.c`, `main/net_demo.c`, `main/jpeg_bench.c` and others | The 80 MHz panel clock (`BSP_LCD_PCLK_HZ`) is committed in `ed79a05`. The instruments are committed in `c74b1a6`, off by default in Kconfig; per the commit message none of it changes the product path |

## 3. Measured

All on live CCTV1 (`ch000`), device counters from the 10-second `interval_frames` lines (windows with under one frame dropped). `rx` is the device's received bytes per second. Audio gaps are `AUDIO_EMPTY` lines; resets are `RX_EXIT` lines.

The figures in this section were taken before the fixed byte target existed. They do not describe the current default sender and were not repeated.

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

Checked against commit `208357b`. Items marked "as recorded" are the earlier document's statement and were not re-verified.

1. **Nothing slows the sender when the link degrades, and the device reports nothing back.** The default sender holds a fixed byte target (`TV_FRAME_BYTES`, default 20000; `TV_ADAPTIVE` off by default, `server/rate.py:187`, `:615`); the adaptive controller needs `TV_ADAPTIVE=1` (steps x1.05 up and x0.85 down, `rate.py:630-631`). On the device, `av_player.c` contains exactly one send, the HELLO at session start (`av_player.c:1134`); after that it reports no drops, backlog or receive rate, so the server can only infer from write time and its own drop count. What should replace the old budget is still undecided.
2. **Start-up climb.** The default (fixed) mode has none; the byte rate only moves with `TV_ADAPTIVE=1`.
3. **The ring wait blocks audio reads.** `av_player.c:1290-1294`: when the receiver cannot get ring space for a non-empty stripe it waits up to `AV_VIDEO_WAIT_US` (100 ms, `:560`), polling with `vTaskDelay(1)`. While it waits it is not reading the socket, and audio shares the TCP stream with video. If no space appears it discards that stripe's bytes, marks the frame lost and counts `nobuf`. Separately, `video_enqueue` gives up after 200 ms of a full queue and the session ends (`:583-591`, `:1319`). This is read from the code; no measurement shows it hurting under real load. The way to see it is `nobuf` and `AUDIO_EMPTY` in the device log.
4. **The frame-rate ceiling of the device is 30 and has not been probed.** `config_valid` rejects a CONFIG whose `fps` is outside 1-30 (`av_player.c:946`); its comment says this is to stop a server announcing a rate the panel could never draw. Every run so far was inside it, and whether the device can go faster has not been measured.
5. **The 64 KB window configuration** is merged: `sdkconfig.defaults` has `CONFIG_LWIP_TCP_WND_DEFAULT=65535` and `CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`, `sdkconfig.av-prototype` has `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`; `tools/sdkconfig.win64` no longer exists. As recorded, the public build passed with 71 KB of runtime free heap; it has not been run from an erased device.
6. **Smoothness judged by eye** (less stutter and tearing at 64 KB) is the viewer's observation; the audio-gap and reset counts support it, but nothing else was instrumented.
7. **Delta coding is on by default** (`TV_DELTA` defaults to `1`, tolerance 0.01, `server/frames.py:197-198`). As recorded, nobody has looked at its picture quality, and that is still the case.
8. **Verification.** Re-run on the current commit: `test_rate` 45, `test_delta_encoding` 25, `test_fixed_rate` 26 and `test_perceptual` 8 tests all pass. `tools/validate.sh` was not run, and this machine has no `gcc`, `cc` or `clang`, so the host C tests did not run (`tests/test_av_protocol.c` has `wire_expansion_tests` at line 191). `ring_alloc` and `ring_release` are `static` pure logic with no host test.
9. **Failing tests that predate this work still fail, 4 of them, today:** `test_live_mode_signal_handler_stops_the_accept_loop` in `tests/test_live_transcode.py`; 2 errors in `tests/test_tv_server.py` (recorded originally as `test_environment_and_restricted_token_file` and `test_fragmented_and_coalesced_stream`; this run only showed the tail of the output and the names were not re-checked one by one); and `test_heavy_video_preserves_audio_and_frame_protocol_on_slow_reader` in `tests/test_live_sender_v2.py`. The earlier statement that they fail the same with the changes stashed was not re-verified. Tests on this machine need `PYTHONPATH=.`.

## 6. Environment notes

- This section used to record the state of one machine (which Wi-Fi the board was on, what NVS held, the server last left running, the `../ai-passport-tv-head` worktree). That is not a fact about the repository and has been removed. `git worktree list` now shows only the main working tree.
- One lesson still holds: check the port before starting a server (`netstat -ano | grep :8096`). A stale server from an earlier session once held it silently, the device connected to that one, and every log on both sides looked plausible.
- Device Wi-Fi credentials are written with `tools/set_wifi_cred.py`. Build directories and logs (`build-*`) are not tracked.

## 7. Reproduce

```text
# Firmware: product public build (the 64 KB window is in the defaults now)
idf.py -B build-fixed -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype" -D SDKCONFIG=build-fixed/sdkconfig build
# On this machine run through tools/idf-run.ps1. Flash with: python -m esptool --chip esp32c3 -p COM4 -b 460800 write_flash 0x10000 build-fixed/FoloToy-AI-Passport.bin
# (segmented only, never erase-flash). If an overlay is added later, delete <build>/sdkconfig first or it is not applied.

# Server: the default is a fixed byte target (TV_FRAME_BYTES a frame, default 20000). Pick a channel that currently plays. Other knobs are in the handoff.
python -m server.tv_server live --channel <channel id> --bind <local address of this machine>

# Tests
PYTHONPATH=. python tests/test_rate.py && PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py && PYTHONPATH=. python tests/test_perceptual.py
```

```text
Build: NOT RUN (documentation change only); as recorded, product public build PASS (streaming stripes; 64 KB window; wire-order expander)
Host tests: re-run 2026-10-01: test_rate 45, test_delta_encoding 25, test_fixed_rate 26, test_perceptual 8, all PASS; test_live_transcode 1 failure, test_tv_server 2 errors and test_live_sender_v2 1 failure match the earlier record of pre-existing failures; C host tests NOT RUN (no C compiler); tools/validate.sh NOT RUN
Device tests: none this time; sections 3 and 4 are the originally recorded 90-150 s single runs on live CCTV1
Unverified: the ring's 100 ms wait against audio under real load; the device above 30 fps; delta picture quality; the 64 KB configuration from an erased device; a weak network and fast-moving content
```
