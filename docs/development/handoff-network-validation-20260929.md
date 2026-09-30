<p align="right">
  <a href="handoff-network-validation-20260929.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Handoff (2026-09-29): network playback validation demo plan

Read first: [on-device playback limit](device-playback-limit-20260929.md) (whole-chain ceiling without a network) and the [benchmark results](hardware-benchmark-results-20260929.md) (per-part). This file does one thing: plan **the next step, validating the same performance over the network**.

## 1. Premise, and a correction

The product's 12 fps **is not a requirement; it is a cap forced by the unoptimised state**: `MAX_FPS` in `server/rate.py` (default 12, env `TV_MAX_FPS`) and `VIDEO_BUDGET_BYTES` (default 185000 bytes/s, `TV_VIDEO_BUDGET`). Locally, in rating (40 MHz), the device draws **40.2 fps** regardless of content. The goal is therefore not to hold 12 but to **find the real ceiling of the network path** and reset those two numbers accordingly.

## 2. Known facts (cited, not re-derived)

- Local whole chain: at 40 MHz the panel is the wall (40.2 fps); CPU takes 13-18 ms of the 24.9 ms frame (about 64%).
- Link: iperf TCP receive with product parameters was 31.6 Mbit/s (about 3.9 MB/s). 40 fps x 18 KB = about 720 KB/s = 5.8 Mbit/s, so the link is not the first suspect.
- Receive: measured about 29 cycles/byte (benchmark file, section 3). 18 KB/frame x 29 = 0.52 M cycles = about **3.3 ms/frame** at 160 MHz.
- CPU need at 40 fps by arithmetic: inflate 10-15 + expand 2.6 + receive up to 3.3 = 16-21 ms against 24.9 ms, a few ms of slack. **This is arithmetic, not measured**; audio I2S writes and Wi-Fi driver time are not included. The real network ceiling may well be below 40, which is why it should be measured, not assumed.
- Earlier end-to-end: at 9 fps stable the device was 23.5% busy; the 12 fps failure was **the probe's** (probe log `packet deadline expired`, device log `EOF`), not the device's limit. **The network path's ceiling has never been measured**; only "at least 9 fps" is known.

## 3. A constraint to read first: the budget itself caps the rate

`VIDEO_BUDGET_BYTES = 185000` bytes/s. At 40 fps that is **about 4.6 KB per frame**, below the local MID (13.6 KB) and HIGH (18.0 KB) tiers. Unchanged, the server's adaptation will pull the frame rate down first and the network will never reach the device's limit. Raise both variables together:

```text
TV_MAX_FPS=40   TV_VIDEO_BUDGET=<fps x per-frame limit>   # e.g. 24 fps x 22528 ~ 540000
```

The per-frame limit comes from the protocol: `AV_VIDEO_MAX = 22528` bytes, one packet per frame; a split frame is dropped whole because the video queue is two deep (`AV_VIDEO_BUFFERS`; see the measurements in `main/av_protocol.h`). **The local HIGH tier's 34.9 KB maximum is beyond that line** and cannot be used as network load as is.

## 4. Plan: three stages, one new variable each

Do not enter a stage until the previous one passes.

### Stage A: fixed load, link and receive only (start here)

Reuse `tools/transport_probe.py` (it already speaks the wire protocol, builds valid frames itself, and has a `--ramp` ladder). Add one thing: have it **read `main/demo_clip.bin`**, whose frames are already wire video payloads in the `server/frames.py` format, with no re-compression. Send only LOW and MID, dropping frames over 22528 bytes and reporting how many were dropped.

That gives a clean comparison: **the same bytes draw 40.2 fps locally; how many over the network? The difference is the network path's cost**, with no ffmpeg, no scheduler and no content change in between.

- Ladder: 6, 9, 12, 16, 20, 24, 30, 40 fps, at least 40 s each (as before).
- Use `--audio` (required, otherwise it measures the handshake, not the link), with sound interleaved between packets, not only between frames (see the probe's header comment).
- Read the device's 10 s interval lines per step: `rx_pkts`, `rx_bps`, `io_ms`, `iters`, plus frames drawn, `nobuf`, dropped and late, panel+decode utilisation. **Meanings follow `metrics-dictionary.md`; do not guess from field names.**
- Pass: for at least 40 s, `interval_frames` equals the step's fps, `nobuf` about 0, no session reset. The highest passing step is this stage's ceiling.

### Stage B: real server, real content

With A's ceiling known, run `server/tv_server.py` on real channels, raise the limits per section 3 to around A's ceiling, and measure delivered rate, drops and session resets. The A-to-B gap is the server's cost (ffmpeg transcode, rate limiter, timeline).

- B fails where A passes: the problem is server-side. Check whether `server/rate.py`'s adaptation pulled the rate back, and the known A/V sync issues in `docs/development/state-20260916.md`.
- Record the `TV_*` environment for every run, or the numbers cannot be reproduced.

### Stage C (optional): device-side optimisation, only if A shows the CPU binds

- Swap `expand_fast` into `push_stripe` in `main/av_player.c` (checked byte-identical on the device in the demo; 14.15 -> 7.61 cycles/pixel). Small change, but **the product path has not been changed and has no network data**.
- Single-window drawing (`bsp_display_raw_window_*`): used only in the measurement build so far, not the product path; invasive, so first see whether A's `panel` wait is really in the way.

## 5. What readings probably mean (hypotheses, pending A)

Guesses, not conclusions, written so readings have somewhere to land:

| Reading | More likely points to |
|---|---|
| `nobuf` rising, large `io_ms` | Receive cannot keep up: the video queue (depth 2) is waiting; check receive task priority and packet size |
| Audio underruns / more `AUDIO_EMPTY` | Audio reads held up by picture packets (one task reads both); see the `AUDIO_SILENCE_MAX_MS` comment |
| Panel utilisation near 100% with a low rate | Panel bus (40.2 fps is a hard ceiling at 40 MHz) |
| Session reset (`RX_EXIT`) | First work out who hung up: read the probe and device logs together; this project got that wrong once |

## 6. Do not

- The panel clock is 80 MHz. Upstream adopted it (commit `20668230`, `BSP_LCD_PCLK_HZ` in `components/bsp/include/bsp_pins.h`), and this fork now matches. Earlier in this file and in [device-playback-limit-20260929.md](device-playback-limit-20260929.md) it was treated as out of rating and unverified; that no longer applies. The 40 MHz figures in those files (the 40.2 fps panel wall, the 24.9 ms frame) describe the old clock and were not re-measured for the local demo at 80 MHz; network-path figures at 80 MHz are in [network-playback-results-20260929.md](network-playback-results-20260929.md).
- Do not use raw HIGH-tier frames as network load (over the packet ceiling).
- Do not conclude from a single run; at least 40 s per step, and repeat the key result.
- Do not blame the device while it is far from its limit; first verify the instrument (probe, Wi-Fi environment, server rate limiting).

## 7. Environment and commands (this Windows machine)

- Activate ESP-IDF only through `tools/idf-run.ps1` (bare `idf.py` under Git Bash does not work; the reason is in the script's header).
- Build the demo: `idf.py -B build-demo -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype;tools/sdkconfig.playback-demo" -D SDKCONFIG=build-demo/sdkconfig build`. `BSP_LCD_PCLK_HZ` is now 80 MHz, so this builds at 80 MHz. `CONFIG_AV_HW_BENCH_SPI80` and the `*-spi80` overlays no longer exist (they doubled the constant, which would have requested 160 MHz).
- Flash only with segmented `idf.py flash`; no `erase-flash`, and never write the merged image raw at `0x0` (it would touch `cardid`; see `protected-flash-layout.md`).
- The board is on COM4 (USB Serial/JTAG).
- There is no native C compiler, so `tools/validate.sh --static` cannot run; a ziglang install sits in `build-hosttools/` (covered by `.gitignore`) and compiles C, but **host tests were not run with it**.

## 8. Not done (honest list)

- **Superseded by [network-playback-results-20260929.md](network-playback-results-20260929.md), which records what was actually measured and which bottlenecks remain.** The plan above was not followed as written: a minimal receiver and server (`main/net_demo.c`, `tools/net_demo_server.py`) replaced the product player and probe, and UDP replaced TCP.
- `tools/transport_probe.py` now reads `demo_clip.bin` (`--clip`). Stage B (the real server) was not run.
- The device's `CONFIG` accepts `fps` from 1 to 30 (`json_between(j,"fps",1,30)` in `main/av_player.c`), so a server must not announce more than 30 fps to the product player.
- `tools/validate.sh` was not run; `docs/CHANGELOG.md` was not updated (internal measurement tooling, which the rules exempt); nothing from this work is committed.

```text
Build: PASS (demo at 40 MHz and at 80 MHz, public build, within the 3 MB limit; built before the constant became 80 MHz)
Host tests: NOT RUN (no native C compiler)
Device tests: PASS only for the local demo's frame rate and expander self-check; network path NOT RUN
Unverified: see section 8; the picture at 80 MHz with the optimised expander was not separately confirmed
```
