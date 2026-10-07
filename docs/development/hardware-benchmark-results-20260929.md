<p align="right">
  <a href="hardware-benchmark-results-20260929.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Hardware Benchmark Results (2026-09-29)

> **Status note.** The panel clock in the product is now 80 MHz (`BSP_LCD_PCLK_HZ`), so the passages below that call 80 MHz "out of rating" record how it was classified when these numbers were taken. `CONFIG_AV_HW_BENCH_SPI80` and `tools/sdkconfig.hw-bench-spi80`, which this file uses to produce the 80 MHz column, have since been removed; `tools/sdkconfig.hw-bench` now runs at 80 MHz and the 40 MHz column cannot be rebuilt without changing the constant.

The measurements the [handoff of 2026-09-28](handoff-20260928.md) asked for, taken on the
real board. That document's section 4 is arithmetic throughout and says so; this
one replaces it with observations, and reports where the two disagree.

Device: ESP32-C3 rev v1.1, 8 MB XMC flash, MAC `4c:11:ae:30:f5:4c`, over
COM3. Wi-Fi SSID `Link`, channel 9, RSSI −9 to −11 dBm.

**Every number here is one run on one board.** Nothing was repeated for
variance, and the spread between repeat runs is therefore unknown.

## 1. Wi-Fi link (step 1, iperf2 2.2.1)

The ESP-IDF `examples/wifi/iperf` example, run with its own defaults for the
target — which are already aggressive (`RX_BA_WIN` 32, buffers 20/40, TCP window
40960) — and only the console and flash size changed for this board.

| Direction | Protocol | Rate |
|---|---|---|
| PC → device | TCP | **33.7 Mbit/s** (4.2 MB/s) |
| PC → device | UDP, 40 Mbit target | **41.9 Mbit/s** sent, 40.0 Mbit/s received |

The handoff records the vendor figure as 35.3 Mbit/s for iperf-class TCP receive.
This board reaches 33.7 under the same class of settings, so **the board meets
its stated Wi-Fi capability** and the link is not what limits playback.

### The product's own Wi-Fi parameters give up almost nothing

The handoff asks for a second run with the shipped parameters, on the finding
that `RX_BA_WIN=4` and the smaller buffers sit below the vendor's minimum
documented tier. Measured, that expectation does not hold:

| Configuration | TCP receive |
|---|---|
| iperf-class (`RX_BA_WIN` 32, buffers 20/40/40, window 40960) | 33.7 Mbit/s |
| **product** (`RX_BA_WIN` 4, buffers 6/16/8, window 32768) | **31.6 Mbit/s** |

**6% apart, not an order of magnitude.** So the product's Wi-Fi configuration is
not what is throwing the link away — the product simply uses about 0.2 MB/s of a
31.6 Mbit/s link, and that gap is made in the application, not in the driver
tuning. The handoff's own "one tenth of capacity" figure is about the product's
usage, and it remains true; it is not evidence that the parameters are costing
anything.

This moves Wi-Fi tuning further down the list, and it means item 5 of the
handoff's candidate optimisations (raise the Wi-Fi parameters to the vendor
default) is worth about 2 Mbit/s against a 33 fps panel ceiling (section 2).
**The panel and the server's rate limit are the only two things that matter.**

## 2. Panel and CPU (step 2, `CONFIG_AV_HW_BENCH`)

Measured before any socket exists, so nothing here can be paced by a sender.
320×180 picture, 60 frames per variant, 115200 bytes a frame.

### The panel is the binding constraint

| Variant | Stripes | ms/frame | vs floor |
|---|---|---|---|
| `disp_stripes12` — **what the product does** | 15 | **30** | +30% |
| `disp_stripes30` | 6 | 24 | +4% |
| `disp_stripes60` | 3 | 24 | +4% |
| `disp_onewindow` — one window, pixels only | 15 | **23** | **−0%** |
| theoretical floor (115200 B at 40 MHz) | — | 23.04 | — |

**The product's 12-row stripe path costs 30 ms a frame against a 23.04 ms
theoretical floor.** The 7 ms difference, 23%, is esp_lcd re-sending CASET/RASET
and waiting for in-flight DMA once per stripe — fifteen times a frame. Removing
all fifteen addressing rounds (`disp_onewindow`) lands on the floor exactly,
which is the strongest evidence available that the floor is real and that this
is what stands between the current firmware and it.

Stripe height matters only up to a point: 30 and 60 rows both give 24 ms, so
most of the per-stripe cost is gone by the time stripes are 30 rows. That means
a cheap partial fix exists — coarser stripes — before the more invasive one.

### Doubling the panel clock nearly halves the frame time

`CONFIG_AV_HW_BENCH_SPI80` raises the bus from 40 MHz to 80 MHz. Same runs:

| Variant | 40 MHz | 80 MHz | Change |
|---|---|---|---|
| `disp_stripes12` | 30 ms | **16 ms** | −47% |
| `disp_stripes30` | 24 ms | 17 ms | −29% |
| `disp_stripes60` | 24 ms | 13 ms | −46% |
| `disp_onewindow` | 23 ms | **12 ms** | −48% |

The floor scales with the clock for the pixel part and not at all for the
per-stripe addressing, which is why the two coarse-stripe variants improve least
— their remaining cost is the addressing, and that is not what the clock moves.

**This exceeds what the panel is specified for, and the console cannot tell
whether the picture survived.** The datasheet's minimum write cycle is 16 ns
(62.5 MHz); the C3's 80 MHz APB divides only to 80, 40 or 26.7 MHz; and this
board drives SCLK/MOSI through the GPIO matrix (GPIO 8/9) rather than the SPI2
IOMUX pins (6/7), which Espressif only guarantees equivalent at or below 40 MHz.
Two independent reasons to expect trouble, and a bus that reports a frame rate
whether or not the pixels arrive. **A run of this configuration is only
meaningful with someone watching the screen**, and no such observation was made
here — so 16 ms is the time the bus took, not a claim that the picture was
correct.

### The CPU is not

Cycles per unit, from `esp_cpu_get_cycle_count()`:

| Measured | Cycles |
|---|---|
| `memcpy` | 0.64 / byte |
| `av_expand_indexed` as shipped | **14.17 / pixel** |
| tinfl at ratio 7.8 | 142.4 / in-byte, 18.3 / out-byte |
| tinfl at ratio 5.2 | 110.8 / in-byte, 21.4 / out-byte |
| tinfl at ratio 3.3 | 90.3 / in-byte, 27.8 / out-byte |
| tinfl at ratio 2.3 | 76.5 / in-byte, 33.9 / out-byte |

The handoff estimated 20–36 cycles per received byte, 25–40 per inflated byte and
8–14 per expanded pixel. **The measured inflate figures sit inside that range at
its optimistic end, and the expander is at its pessimistic end** (14.17 against a
top estimate of 14).

At the product's own operating ratio (~5), one frame costs:

```text
inflate   21.4 × 57600 = 1.23 M cycles
expand    14.17 × 57600 = 0.82 M cycles
                         ───────────────
                          2.05 M cycles/frame
```

Against a budget of 136 M cycles a second, that is **66 frames a second of CPU
headroom** — more than the panel can consume. **So the CPU is not the limit, and
the handoff's ranking that put "change the codec" at step 6 while "bypass
esp_lcd" sat at step 8 should be reversed.**

## 3. The receive path's CPU cost (step 3)

Counted inside the receive loop, around each `recv()` call, with
`esp_cpu_get_cycle_count()`.

**The method matters more than the number here, so it is stated first.** The
handoff proposed a lowest-priority spin task whose idling rate would be
calibrated, and the fall in that rate under load read as the receiver's share.
Two things ruled that out on this board: the task watchdog is enabled with a 5 s
timeout and checks the idle task, so a task that never blocks resets the chip
instead of measuring it; and a spin task measures *all* stolen cycles, which
then has to be attributed back to causes by inference.

What is measured instead is the syscall. These sockets are non-blocking — the
`EAGAIN` branch in `io_until()` is the proof — so a `recv()` returns promptly
having either copied bytes or found none, and the cycles it consumes are lwIP
walking the segment and copying it. That is the per-byte receive cost, directly.
Wrapping `io_until()` instead would have added the `select()` waits and every
cycle the video task ran during them, which is wall time and not receive work.

| Metric | Value |
|---|---|
| cycles per byte, median of 16 intervals | **29** |
| range | 28 – 54 |

The handoff estimated 20–36 cycles per byte and called it the least certain
figure in the plan. **The measurement lands in the middle of that range**, so
the estimate was sound — and the outstanding uncertainty is now closed.

The 54 figure is one interval, and its neighbours in the same log explain it:
`io_ms=7878` and `per_iter_us=29070` against a steady-state `io_ms=102` and
`per_iter_us=350`. That interval was a stall, not a rate.

What this does **not** include, and the firmware says so where it reports:
lwIP's work for packets this loop never asks for, and Wi-Fi driver time outside
this task's context.

### What step 3 does to the conclusion

At 29 cycles a byte, the full product bit rate of about 120 kB/s costs:

```text
29 × 122880 = 3.6 M cycles/s
```

Against 136 M available, that is **2.6% of the CPU**. So receiving is not a
constraint at any frame rate this panel could reach, and section 2's conclusion
stands with more room than before: the CPU is not the limit at any of the three
places it was suspected.

## 4. What this changes in the plan

1. **Panel first.** 30 ms → 23 ms is a 30% frame-rate gain on the critical path,
   and the floor is now measured rather than assumed. Two levers, in cost order:
   coarser stripes (a constant, ~20% for one line), then one-window streaming
   (the last 4%).
2. **De-prioritise the codec change.** CPU has 66 fps of headroom against a
   33 fps panel ceiling (30 ms a frame, section 2), and receiving costs 2.6% of
   the CPU. A 4×4 block codec would reduce cycles already not binding.
3. **The server's own rate limit is still the first thing to lift.**
   `server/rate.py:70` defaults `VIDEO_BUDGET_BYTES` to 185000, and
   `MAX_FPS` is 12 — the device's own ceiling. Nothing in the device can reach
   12 fps while the sender stops at about 9.7 for a 19 kB frame.
4. **Wi-Fi is not a candidate.** 33.7 Mbit/s against a product that uses about
   0.2 MB/s.
5. **The 80 MHz panel clock is the largest single lever found, and the one that
   cannot be taken on this evidence.** It halves the frame time — 30 ms to 16 ms,
   23 ms to 12 ms — which is worth more than items 1 through 4 combined. But it
   is outside the panel's rated envelope on two independent counts, and the
   measurement says nothing about whether the picture survived, because a bus
   reports a frame rate whether or not the pixels arrive. **Before it can be
   adopted, someone has to flash build-spi80 and look at the screen.** If the
   picture is intact, this is the first thing to do; if it is not, the ceiling in
   section 2 is real and item 1 is the path.

## 5. End to end (step 4, `tools/transport_probe.py`)

One packet a frame — 15 stripes — which is what the product's server does and
what `AV_VIDEO_MAX` exists to make possible. Ramp held at 40 s a step.

| Asked | Result |
|---|---|
| 6 fps | held, 108.2 kB/s |
| 9 fps | held, 146.3 kB/s |
| 12 fps | **session lost** |

**The 12 fps run did not establish a device ceiling, and this document
previously said it did. That claim was wrong and is retracted here.**

What the device's own counters show, per ten-second interval:

| Step | Frames drawn | Panel + decode busy | Utilisation |
|---|---|---|---|
| 6 fps | 60 | 1573 ms | **15.7%** |
| 9 fps | 90 | 2346 ms | **23.5%** |

The device was **less than a quarter busy** at the highest rate that held. It was
nowhere near its limit, so 9 fps is a floor on what it can do, not a ceiling.

The failure at 12 fps is the probe's, not the device's: its log says
`packet deadline expired` and the device's says
`RX_EXIT stage=header-read reason=EOF` — EOF is the peer closing, so the probe
hung up first. That is an instrument fault, which this file already carries a
warning about and which was still read as a device property. **The same mistake
the project has made repeatedly, made again.**

So: **the end-to-end ceiling is not established.** It is at least 9 fps and
unknown above that. What *is* established is the panel's own cost per frame,
measured in isolation in section 2 — and that is what any ceiling has to be
computed from:

| Panel path | ms/frame | Implied ceiling |
|---|---|---|
| current, 12-row stripes, 40 MHz | 30 ms | **33 fps** |
| one window, 40 MHz | 23 ms | **43 fps** |
| 12-row stripes, 80 MHz | 16 ms | **62 fps** |
| one window, 80 MHz | 12 ms | **83 fps** |

Those are the panel alone. The CPU has 66 fps of headroom (section 2) and
receiving costs 2.6% (section 3), so the panel is the only one of the three that
binds at any of these figures — but the step-4 run never pushed far enough to
show where the whole system lands.

A first run at `--stripes-per-packet 7` split every frame into three packets and
produced `interval_frames=0` with a high `nobuf` — the exact failure
`main/av_protocol.h` documents for a split frame, where the middle packet finds
no free buffer and the frame is discarded whole. **That run measures the probe's
configuration, not the device**, and is recorded here only so the wrong figure
is not mistaken for a finding later.

## 6. Instrumentation added

| File | Purpose |
|---|---|
| `main/av_player.c` — `hw_bench()` under `CONFIG_AV_HW_BENCH` | The benchmark itself; off by default, runs before the network |
| `main/bench_blobs.h`, `tools/make_bench_blobs.py` | Compressed stripes generated on the host — the ROM's `tdefl` cannot run here (`MINIZ_NO_MALLOC`, ~130 KB against 112 KB free) |
| `components/bsp/` — `bsp_display_raw_window_begin/_push` | One-window streaming, the measurement-only escape from esp_lcd's per-stripe addressing |
| `tools/bench_summary.py` | Tabulates `BENCH` and `CLOCK_ESTIMATED`, and re-derives `parts_ms` |
| `tools/iperf_bench.py` | Drives the iperf example's console (`sta_connect`, `iperf -s`) |
| `tools/idf-run.ps1` | Activates ESP-IDF 5.5.3 from a PowerShell invocation |
| `tools/sdkconfig.hw-bench` | Build overlay that turns the benchmark on |
| `tools/sdkconfig.hw-bench-spi80` | Build overlay for the benchmark with the panel clock at 80 MHz — use instead of the one above, not alongside |

`AV_HW_BENCH` cannot be enabled with `idf.py -D CONFIG_AV_HW_BENCH=y`: that sets
a CMake cache variable, not a Kconfig symbol, and the build succeeds with the
option still off. Use the overlay file. The same applies to
`CONFIG_AV_HW_BENCH_SPI80`, and the sdkconfig must be **deleted** before a
defaults change takes effect — an existing `sdkconfig` is not rewritten by new
`SDKCONFIG_DEFAULTS`, which is how the first three attempts at this produced
byte-identical binaries and looked like success.

## 7. Not done

- **`disp_rgb444`.** Not implemented. Changing the panel to a 12-bit colour mode
  needs a human looking at the screen to say whether the result is a picture or
  noise, and it buys less than the 80 MHz clock already measured: RGB444 is 25%
  fewer bytes, where doubling the clock is all of them.
- **Nobody has looked at an 80 MHz screen.** The measurement says the bus took
  16 ms a frame; it cannot say the picture was right. This is the single most
  important unverified item in this document.
- **Host tests.** Not runnable as a whole on this machine, for two independent
  reasons:
  - `tools/validate.sh --static` needs a native C compiler and there is none: no
    MSVC, no MinGW, no LLVM, and the ESP-IDF-bundled `esp-clang` registers only
    `riscv32`/`riscv64`/`xtensa` targets, so it cannot emit an x86 binary
    (verified: `unable to create targets compatible with triple
    "x86_64-pc-windows-gnu"`). It also fails before that, at
    `tools/install-actionlint.sh`, which has no Windows case and exits with
    "Unsupported actionlint platform: MINGW64_NT-10.0-19045/x86_64". WSL is not
    installed. Neither file was changed by this work.
  - Running the Python suites directly instead: **285 tests, 6 errors and 1
    failure, all pre-existing Windows/POSIX incompatibilities and none of them
    in a file this work touched.** `server/tv_server.py` uses `os.O_NONBLOCK`,
    `tests/test_datadir.py` uses `os.geteuid()`, and
    `tests/test_live_transcode.py` expects SIGINT to reach a flag. `git status`
    confirms no `server/` or `tests/` file was modified here.
  - What did pass: `tools/check_repo.py`, `tools/make_bench_blobs.py --check`,
    `tools/check_config_agreement.py`, and the eleven named Python suites other
    than the three above.
- **The C changes are compile-verified only.** No host test executes
  `av_player.c`, and none could on this machine.

## 8. Delivery status

```text
Build: PASS (prototype, bench-enabled, the 80 MHz bench variant, and the iperf
       example, all for esp32c3)
Host tests: NOT RUN (no native C compiler on this machine; see section 7)
Device tests: PASS (steps 1, 2, 3 and 4 all measured on the board)
Unverified: **nobody has looked at a screen driven at 80 MHz** -- the frame time
            is measured, the picture is not; the RGB444 variant; the
            product-Wi-Fi-parameter iperf comparison; host tests; every figure
            is a single run with no variance measured; the step-3 counter
            excludes packets this loop never asks for and Wi-Fi driver time
            outside its context
```
