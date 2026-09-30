<p align="right">
  <a href="device-playback-limit-20260929.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# On-device playback limit: measurement and optimisation (2026-09-29)

> **Status note.** The panel clock in the product is now 80 MHz (upstream commit `20668230`; `BSP_LCD_PCLK_HZ`). The headings below that call 80 MHz "over rating" and "out of rating" describe how it was classified when these numbers were taken and are kept as the record of that run; they are no longer the project's position. The 40 MHz results were not re-measured at 80 MHz for this local demo. `CONFIG_AV_HW_BENCH_SPI80` and the `*-spi80` overlays this file mentions have been removed, because they doubled the constant and would have requested 160 MHz; the 80 MHz tier below was produced with them and cannot be rebuilt as written -- use `tools/sdkconfig.playback-demo`, which is now 80 MHz.

[`hardware-benchmark-results-20260929.md`](hardware-benchmark-results-20260929.md) measured the parts: panel bus, per-item CPU cost, receive path. This file measures the **whole chain running together**: a device with no network loops a clip stored in the firmware, using the product's own decode and draw steps, plus one loop-unrolling optimisation.

**Every number is a single run**; no repeats, so run-to-run spread is unknown. Device: ESP32-C3 rev v1.1, 8 MB XMC Flash, MAC `4c:11:ae:30:f5:4c`, COM4.

## 1. The instrument: a network-free playback demo

`CONFIG_AV_DEMO_PLAYBACK` (`main/av_demo.c`, `tools/sdkconfig.playback-demo`). The clip `main/demo_clip.bin` is written by `tools/make_demo_clip.py` in exactly the wire format (`server/frames.py`: stripe table plus one zlib stream per stripe), so the device parses it with `av_video_decode()` and runs the product's inflate, palette expand and `bsp_display_raw_submit*` with one DMA kept in flight. The frame rate and per-stage costs are drawn in the letterbox bars (not over the picture) and one `DEMO` log line is printed per tier.

Why it exists: the benchmark measures parts before the network starts, and every earlier end-to-end conclusion was either paced by the sender or read by hand from logs. Here double buffering, one transfer in flight, 12-row stripes and the 3-3-2 palette are all unchanged; only the network is removed.

Three tiers, 32 frames each (96 per loop, one line per tier; the tier count lives in the container header's reserved field):

| Tier | Content | Avg compressed frame | Max |
|---|---|---|---|
| LOW | `testsrc2` (bars, blocky motion) | 4.8 KB | 5.0 KB |
| MID | Mandelbrot zoom, slow (`end_scale=0.3`) | 13.6 KB | 17.1 KB |
| HIGH | Mandelbrot zoom, fast (`end_scale=0.003`) | 18.0 KB | **34.9 KB** |

HIGH's 34.9 KB exceeds the protocol's 22.5 KB single-packet ceiling (`AV_VIDEO_MAX`). It is a stress tier only; **the product cannot send a frame that large**, so its absolute numbers do not extrapolate to the product.

## 2. 40 MHz (within rating)

| Tier | fps | Frame | Inflate | Expand (optimised) | Panel |
|---|---|---|---|---|---|
| LOW | 40.3 | 24.8 ms | 10.7 ms | 2.6 ms | 11.3 ms |
| MID | 40.2 | 24.9 ms | 13.4 ms | 2.6 ms | 8.6 ms |
| HIGH | 40.1 | 24.9 ms | 14.5 ms | 2.6 ms | 7.5 ms |

1. **The frame rate is independent of content: 40.2 fps.** The simpler the content, the longer the `panel` segment: CPU time saved becomes waiting in front of the DMA. The panel bus is the wall.
2. The measured floor is 23.04 ms (115200 bytes at 40 MHz, `PANEL_FLOOR_MS`), i.e. 43.4 fps. Actual is 24.9 ms, 1.9 ms above it. The product path in the benchmark file was 30 ms (7 ms above); why this path is closer is **not verified**, since the differences between the two paths were not isolated one by one.
3. **A correction.** An earlier verbal version said "CPU is already the bottleneck at 40 MHz". That read `panel` as the panel's true cost and was wrong. At 40 MHz the CPU always has slack: 16.0 ms of 24.9 ms on MID.

## 3. 80 MHz (**28% over rating**)

`CONFIG_AV_HW_BENCH_SPI80` (`tools/sdkconfig.playback-demo-spi80`). The datasheet's minimum write cycle is 16 ns (62.5 MHz), and this board routes SCLK/MOSI through the GPIO matrix (GPIO 8/9) rather than the IOMUX pins (6/7), which Espressif only guarantees equivalent at or below 40 MHz.

| Tier | fps | Frame | Inflate | Expand | Panel |
|---|---|---|---|---|---|
| LOW | 65.4 | 15.3 ms | 10.7 ms | 2.7 ms | 1.7 ms |
| MID | 54.9 | 18.2 ms | 13.6 ms | 2.6 ms | 1.8 ms |
| HIGH | 51.6 | 19.4 ms | 14.7 ms | 2.6 ms | 1.8 ms |

- The panel segment fell from 8-11 ms to about 1.8 ms; **the panel no longer limits any tier**.
- The rate varies with content because the wall is now the CPU.
- **A person looked at the screen and the picture was normal** (2026-09-29), but on the 80 MHz build **before** the optimised expander (single clip, about 50 fps). After the three-tier build with `expand_fast` was flashed, the picture was not separately re-confirmed. This removes part of the benchmark file's most important unverified item, scoped to this board, this power-up, about a dozen seconds. See section 6.

## 4. Inflate cost tracks output pixels, not compressed size

The most useful finding here, and one the benchmark did not surface.

Compressed size spans 4.8 to 18.0 KB (**3.8x**), inflate time only 10.7 to 14.7 ms (**1.4x**). Each frame inflates to 57600 index bytes, at about **18-21 cycles per output byte**, only weakly dependent on input size. This matches the benchmark's tinfl figures (18.3-33.9 cycles per output byte across ratios 2.3-7.8).

**Consequence: a better-compressing encoding saves bandwidth, not CPU.** It cannot raise the 80 MHz frame-rate ceiling unless its decoder is itself faster.

## 5. Expander optimisation: implemented and checked on the device

`expand_fast()` in `main/av_demo.c`. Four pixels per step, backwards: one read of four indices, two 32-bit stores writing four pixels; the palette is pre-swapped (`__builtin_bswap16`) so a 16-bit store yields wire byte order. Backwards for the same reason as the original: group k writes from byte 8k and never over an unread index.

On-device self-check (`EXPAND_BENCH`, one stripe of 3840 varied indices):

```text
EXPAND_BENCH same=1 orig_cyc_per_px_x100=1415 fast_cyc_per_px_x100=761
```

- Output is **byte-identical** to the original (a mismatch halts the demo);
- 14.15 -> 7.61 cycles/pixel, i.e. expand 4.7 -> 2.6 ms per frame, about **1.9x**;
- Frame rate: before the change, a different single clip (avg 13.1 KB/frame) gave about 50 fps; after it, the MID tier (avg 13.6 KB/frame) gives 54.9 fps. **The clips differ, so this is similar in scale but not a strict before/after.**

At 40 MHz this does **not change the frame rate**, since the panel is the wall there. It pays off only when the CPU binds.

## 6. What was not done, and what does not extrapolate

- **80 MHz was seen on one device, for about a dozen seconds.** There is no margin guarantee outside the rating; unit variation, temperature, long runs and cable length can all change the result. It is not a product configuration.
- **HIGH's 34.9 KB exceeds the product packet ceiling**; its absolute numbers do not describe content the product can receive.
- **Single runs, spread not measured**, 32 frames per tier.
- **The optimised expander is in the demo only.** The product path (`push_stripe` in `main/av_player.c`) still uses `av_expand_indexed`.
- `tools/validate.sh` was not run (no native C compiler here); `tools/check_repo.py` passed before these changes.
- **The network was not measured.** Every number here is the no-network path; see the handoff.

## 7. Two ceilings

| Panel clock | Wall | Ceiling |
|---|---|---|
| 40 MHz (within rating) | panel bus | **40.2 fps**, independent of content; floor 43.4 fps |
| 80 MHz (28% over) | CPU (inflate) | **51.6-65.4 fps**, varies with content |

The product runs at 12 fps, **3.3x below the in-rating 40 fps**, with about 36% CPU slack at 40 MHz. The reason the product does not reach 40 is therefore not this segment; it is the network and the server, which is what the next handoff is for.

## 8. New instruments

| File | Purpose |
|---|---|
| `main/av_demo.c` / `av_demo.h` | Network-free demo: three-tier loop, per-stage costs, `expand_fast` and its self-check |
| `main/demo_clip.bin` | 96 frames, 1.17 MB, 32 per tier; written by the script below |
| `tools/make_demo_clip.py` | Generates it and prints each tier's average/max compressed frame |
| `tools/sdkconfig.playback-demo` | Build overlay enabling the demo (40 MHz) |
| `tools/sdkconfig.playback-demo-spi80` | Same at 80 MHz (**out of rating**) |

`CONFIG_AV_DEMO_PLAYBACK` **cannot** be enabled with `idf.py -D CONFIG_AV_DEMO_PLAYBACK=y`: that sets a CMake cache variable, not a Kconfig symbol, and the build succeeds with the option still off. Use the overlay. Enabling it alongside `CONFIG_AV_HW_BENCH` is allowed (the SPI80 tier requires it); the demo never returns, so the boot benchmark does not run and only the clock override takes effect.
