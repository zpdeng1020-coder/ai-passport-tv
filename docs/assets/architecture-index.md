<p align="right">
  <a href="architecture-index.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Architecture Index (fork-only navigation aid)

**Purpose.** This file is a map, not a rulebook: it exists so that a new feature
request in this fork can be routed to the right file in one pass, instead of
re-reading the whole tree. It does not add or change any rule -- [`AGENTS.md`](../../AGENTS.md)
stays the single source of truth for conventions, build commands, and safety
baseline. Read this file first to find *where*, then read `AGENTS.md`'s own
routing table and the neighboring code for *how*.

This is fork-private content per [`docs/fork-guide.md`](../fork-guide.md) (the
"architecture notes" use of `docs/assets/`) and is not proposed upstream.

**Maintenance rule for this file itself.** Update it whenever a module is added
to `main/` or `server/`, or a task type shows up that the routing table below
does not cover. Keep every entry to one line with a link; put rationale in the
code comment or doc it points to, never here -- a duplicated explanation is a
second source of truth that will drift. If this file and the code disagree,
the code is right; fix this file.

## 1. Two firmware personalities behind one entry point

[`main/main.c`](../../main/main.c) picks one of two completely different
programs at `app_main()` (around line 108) based on `CONFIG_AV_RAW_PROTOTYPE`
(declared in [`main/Kconfig.projbuild`](../../main/Kconfig.projbuild)):

| Personality | Enabled by | Entry | What it is |
| --- | --- | --- | --- |
| BSP demo menu (upstream baseline) | default (`CONFIG_AV_RAW_PROTOTYPE=n`) | `app_main()` builds the LVGL menu directly | Board capability demo: Display/Button/Audio/Battery/Wi-Fi/BLE/Low-Power pages. This fork barely touches it; see `AGENTS.md`'s own routing table for it. |
| TV playback prototype (this fork's product) | `sdkconfig.av-prototype` (`CONFIG_AV_RAW_PROTOTYPE=y`) | `av_player_main()` in [`main/av_player.c`](../../main/av_player.c) | The network-television firmware this fork actually ships. Almost every fork feature request lands here or in `server/`. |

Almost all of this fork's own development is in the second row. The rest of
this index is organized around it.

## 2. Device firmware: `main/av_player.c` (~3600 lines -- grep, do not full-read)

One file owns the whole session: socket, three FreeRTOS workers, the setup
flow and the overlay menu. Jump straight to the function, do not scroll:

| Concern | Function / symbol | Notes |
| --- | --- | --- |
| Wire format constants, header struct, indexed-picture decode | [`main/av_protocol.h`](../../main/av_protocol.h) / `.c` | No ESP-IDF dependency; host-testable. Every magic number has a "why" comment with the measurement behind it -- read the comment before changing the number. |
| Wi-Fi bring-up | `wifi_init()` | Initializes NVS + `av_provision_*`. |
| Boot flow, mode decision, main loop | `av_player_main()` | Owns setup-vs-play decision (`av_boot_mode()`), volume/brightness restore, per-session reset, task spawn order. |
| Setup / captive portal flow | `run_setup_mode()` | Draws the setup screen; talks to `av_provision_*`. |
| Handshake (HELLO/CONFIG) | `hello()`, `config_valid()` | Validates the server's CONFIG JSON against the constants in `av_protocol.h`. |
| Socket receive loop | `receive_task()` | Highest task priority (7) on purpose -- see the comment above the `xTaskCreate` calls near the end of the file for why the ordering vs. audio/video is load-bearing. |
| Audio playback | `audio_task()` | Owns I2S; audio is the session's clock (`estimated_pts()`-equivalent). |
| Video decode + draw | `video_task()`, `push_stripe()`, `enlarge_stripe()`, `overlay_stripe()` | Owns the raw panel; inflate → index→RGB565 expand → optional enlarge → overlay → DMA submit. |
| Channel switch request | `request_switch()`, `process_key()` | Button gestures → pending channel id; applied once per session restart in the main loop. |
| Session lifecycle | `allocate_session()`, `free_session()`, `session_drain()` | Buffer ownership and the drain/timeout rules described in `docs/local-tv-prototype.md`. |
| On-device screenshot (debug) | `shot_take()`/`shot_print()` (guarded by `CONFIG_AV_SCREEN_CAPTURE`) | Off by default; see the Kconfig help text. |

Supporting modules, each already host-testable and already covered by
`tools/validate.sh --static`:

| File | Owns |
| --- | --- |
| [`main/av_channel_policy.c`/`.h`](../../main/av_channel_policy.h) | "Has this channel ever shown a picture" bookkeeping and the skip-to-next-channel decision. |
| [`main/av_adpcm.c`/`.h`](../../main/av_adpcm.h) | IMA ADPCM block decode for the audio packet (324 bytes → 640 samples); block layout is documented in the header and in [`docs/local-tv-prototype.md`](../local-tv-prototype.md). Called from `audio_task()`. |
| [`main/av_provision.cpp`/`.h`](../../main/av_provision.h) | Wi-Fi station/AP lifecycle (C++, wraps `esp-wifi-connect`). |
| [`main/av_provision_policy.c`/`.h`](../../main/av_provision_policy.h) | Pure boot-mode decision (`AV_BOOT_PLAY` vs `AV_BOOT_SETUP`), no ESP-IDF. |
| [`main/av_settings.c`/`.h`](../../main/av_settings.h) | Brightness/volume step tables (fixed levels, not continuous). |
| [`main/av_store.c`/`.h`](../../main/av_store.h) | NVS persistence for volume/brightness/server address. |
| [`main/av_server_addr.c`/`.h`](../../main/av_server_addr.h) | Parses the "host[:port]" text typed on the setup page. |
| [`main/ui_menu.c`/`.h`](../../main/ui_menu.h) | Channel list / status / brightness overlay state machine -- pure logic, no LVGL. |
| [`main/ui_text.c`/`.h`](../../main/ui_text.h) | Bitmap text drawn directly onto a display stripe (not LVGL); CJK glyphs come from `ui_font_cjk.bin`, regenerated by `tools/make_font.py`. |
| [`main/ui_pixel.c`/`.h`](../../main/ui_pixel.h) | Shared visual theme (sky/grass/mascot/panels), also used by the BSP demo menu. Keep intact per `docs/development/ai-guide.md`. |

## 3. Server: `server/` (Python, standard library only)

| File | Owns |
| --- | --- |
| [`server/tv_server.py`](../../server/tv_server.py) | Entry point; `live`/`run`/`prepare`/`import-video` subcommands; per-connection session loop. |
| [`server/live.py`](../../server/live.py) | Live-channel ffmpeg pipelines, `CHANNELS` table (loaded from `channels.txt`), palette handling. |
| [`server/protocol.py`](../../server/protocol.py) | Python mirror of `main/av_protocol.h`'s wire framing. Keep the two in sync; a test cross-checks the shared constants. |
| [`server/frames.py`](../../server/frames.py) | Indexed-picture stripe cutting/compression -- the server-side counterpart of `av_expand_indexed`/`push_stripe`. |
| [`server/timeline.py`](../../server/timeline.py) | Content-time vs. arrival-time, `SessionClock`. **Read first** for any A/V sync or timing bug; see `docs/development/state-20260916.md` for the current known-good/known-broken state of this area. |
| [`server/pts.py`](../../server/pts.py) | Derives both streams' timestamps from one decode pass rather than counting frames. |
| [`server/rate.py`](../../server/rate.py) | Adaptive video bitrate/frame-rate choice during a session. |
| [`server/media.py`](../../server/media.py) | `prepare`/`import-video`: ffmpeg invocations, geometry/letterbox rules. |
| [`server/netident.py`](../../server/netident.py) | Works out this computer's own reachable address to print for the device. |
| [`server/usb_link.py`](../../server/usb_link.py) | Socket-shaped adapter over the USB-serial transport (measurement builds only). |
| [`server/live_sender.py`](../../server/live_sender.py) | Opt-in v2 sender (`TV_LIVE_ENGINE=v2`); see [`docs/development/live-sender-v2.md`](../development/live-sender-v2.md) for scope/limits before touching it. |
| [`server/fault.py`](../../server/fault.py) | Fault-injection harness for controlled device experiments (B02-R); not part of the shipped server path. |

## 4. Tools you will actually touch for a feature

The full list is `tools/`; these are the ones a feature request usually needs:

| Need | Tool |
| --- | --- |
| Edit/reorder the channel list from a browser | [`tools/channel_config.py`](../../tools/channel_config.py) (writes `channels.txt`) |
| Start server + channel page together | [`tools/launch.py`](../../tools/launch.py) (`run.sh`/`run.bat` wrap this) |
| Package the server into one executable | [`tools/build_server.py`](../../tools/build_server.py), [`packaging/tv-server.spec`](../../packaging/tv-server.spec), [`.github/workflows/build-server.yml`](../../.github/workflows/build-server.yml) |
| Build/verify the firmware image | [`tools/validate.sh`](../../tools/validate.sh) `--prototype`, [`tools/verify_firmware.py`](../../tools/verify_firmware.py), [`sdkconfig.av-prototype`](../../sdkconfig.av-prototype) |
| Inject Wi-Fi/server-address into a bench device's NVS | [`tools/set_wifi_cred.py`](../../tools/set_wifi_cred.py), [`tools/set_server_addr.py`](../../tools/set_server_addr.py) |

## 5. Wire contract -- one contract, four places it is written down

Changing anything here means updating all four, in this order (device struct
first, since it is what a flashed device cannot renegotiate):

1. [`main/av_protocol.h`](../../main/av_protocol.h) -- authoritative device-side struct/constants.
2. [`server/protocol.py`](../../server/protocol.py) + [`server/frames.py`](../../server/frames.py) -- server-side mirror.
3. [`docs/local-tv-prototype.md`](../local-tv-prototype.md) -- device-side narrative (ownership, lifecycle, timing budget).
4. [`server/README.md`](../../server/README.md) -- server-side narrative (scheduling, bounds, shutdown).

## 6. Docs already curated for context -- open these, do not re-derive

| Doc | When to read it |
| --- | --- |
| [`docs/development/handoff-20260928.md`](../development/handoff-20260928.md) | **Before performance or architecture work.** Hardware-derived ceilings and the on-device benchmark plan. |
| [`docs/development/tcp-delta-progress-20260930.md`](../development/tcp-delta-progress-20260930.md) | **Before further work on the product's TCP path.** The product's frame-rate cap is the rate controller's budget, not the link; streaming-stripe receive, the 64 KB window, delta coding, the open problems. The UDP rewrite is shelved. |
| [`docs/development/server-optimisation-handoff-20260930.md`](../development/server-optimisation-handoff-20260930.md) | **Before server or picture-quality work.** Fixed frame rate per channel, per-frame fit to a byte target, what is measured and what is not, next steps in order. |
| [`docs/development/state-20260916.md`](../development/state-20260916.md) | **Before touching timing/session/quality.** Current fixed/broken/measured state; check it against the code first. |
| [`docs/development/metrics-dictionary.md`](../development/metrics-dictionary.md) | Before trusting or logging any counter -- what it counts and what it must not be read as. |
| [`docs/development/live-sender-v2.md`](../development/live-sender-v2.md) | Before touching `server/live_sender.py`. |
| [`docs/local-tv-prototype.md`](../local-tv-prototype.md) / [`server/README.md`](../../server/README.md) | Protocol, ownership, memory, and lifecycle narrative for device and server respectively. |
| [`docs/CHANGELOG.md`](../CHANGELOG.md) | What has already shipped, in the project's own words. |

## 7. Fast task routing (extends `AGENTS.md`'s table with this fork's TV surface)

| New requirement mentions... | Start at |
| --- | --- |
| Channel list, adding/removing/reordering channels | `channels.txt`, `tools/channel_config.py`, `server/live.py` (`CHANNELS`), `main/av_channel_policy.c` |
| Wire protocol / packet format change | `main/av_protocol.h`, `server/protocol.py`, `server/frames.py`, §5 above |
| Picture quality, frame rate, stripe/geometry | `main/av_protocol.h` (`AV_VIDEO_*`, `AV_FPS`), `server/frames.py`, `server/rate.py`, `main/av_player.c` `video_task`/`push_stripe` |
| A/V sync, drift, stutter, session reset | `server/timeline.py`, `server/pts.py`, `main/av_player.c` `audio_task`/`video_task`, `docs/development/state-20260916.md` |
| Device buttons, on-screen menu, banners, brightness/volume HUD | `main/ui_menu.c`/`.h`, `main/av_player.c` `process_key()`, `main/av_settings.c` |
| Wi-Fi / server-address setup flow, captive portal | `main/av_provision.cpp`, `main/av_provision_policy.c`, `main/av_player.c` `run_setup_mode()` |
| Server packaging / distribution / release | `tools/build_server.py`, `packaging/tv-server.spec`, `.github/workflows/build-server.yml` |
| Firmware build, CI, release artifact | `tools/validate.sh`, `sdkconfig.av-prototype`, `.github/workflows/build-firmware.yml` |
| BSP-level hardware (pins, buses, display, audio, battery) | unchanged from `AGENTS.md` -- `components/bsp/include/bsp_pins.h` first |
| Anything not in this table | Fall back to `AGENTS.md`'s own routing table, then `docs/development/ai-guide.md` |

## 8. Constraints inherited from `AGENTS.md` -- not repeated here

Flash layout (`cardid@0x356000`, 3 MB app cap), public-build-only validation,
`bsp_lvgl_lock()`, non-blocking button callbacks, and the language-pairing rule
for docs all still apply unchanged. See [`AGENTS.md`](../../AGENTS.md) for the
current text -- this file only adds a map on top of it, never a second copy.
