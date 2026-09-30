<p align="right">
  <a href="server-optimisation-handoff-20260930.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Server-side optimisation: handoff (2026-09-30)

**Read this first for server or picture-quality work.** It is the state after the device side was
frozen: what the server now does, what it measured, what is not established, and what to do next in
order. The transport work it descends from is in
[tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md); that document's device sections
are still valid, its server sections are not.

Nothing here is committed. The tree holds the work of two days (delta coding, the streaming-stripe
receiver, the fixed-rate server, the firmware changes) on top of commit `c61b764`.

Everything in this file is **offline measurement on recorded footage** or **single runs against the
board**. There is no repeated run anywhere, and the picture-quality numbers are PSNR, which is a
proxy and not a viewer's judgement.

## 1. The decision that was made

The frame rate is **fixed** and the picture's **detail** is what gives when the link is short.

A frame rate that moves is the thing a viewer notices most; a picture that is briefly coarser mostly
is not. So a channel plays at its source's own rate for the whole session, and what the controller
moves is the number of **bytes a second** the picture may cost. Every frame is compressed to fit
`byte rate / frame rate`, and a frame that does not fit is made coarser until it does.

What this replaced: a controller that stepped the frame rate between 3 and 12 from write time and
dropped frames, with a per-channel ceiling derived as `budget / bytes per frame`. Its budget (185000
B/s) was never measured as a link limit, and with it in force the device drew 12 fps where the same
link and the same firmware drew 24.3 fps with the controller held at a fixed rate. See the progress
document, section 1, for that measurement.

## 2. What the server does now

| Where | What it does |
|---|---|
| `server/live.py` `probe_source_fps` | Asks `ffprobe` for the channel's frame rate at startup: `avg_frame_rate` first (a 25 fps programme carried as 50 fields reports `r_frame_rate` 50 and `avg_frame_rate` 25), halved while over 30, the device's own bound (`json_between(j,"fps",1,30)` in `main/av_player.c`). Cached per URL. An explicit `TV_FPS` skips the probe; a source that does not answer plays at the nominal 25 |
| `server/live.py` `LiveChannel.fps`, `source_graph`, `source_command` | The probed rate goes into the ffmpeg filter graph, the content timeline and the sender's pacing, so all three agree by construction |
| `server/rate.py` `ByteRate` | The controller. Fixes the frame rate and moves a byte rate: down at once on a write over 250 ms or two windows running with drops, up 5% after two windows that both wrote under 120 ms **and** used at least 80% of the rate. The second condition is the one that matters: a still picture writes instantly because it is small, and a rate raised on that opens the gate for the next cut to spend it all at once |
| `server/frames.py` `encode_within` | Per frame: compress at the current quality, then progressively coarser colour steps (red and green 8→4→3→1 levels, blue 4→1), take the first that fits `rate / fps`, keep nothing between frames. Every stripe sent is from the current frame. Only if the coarsest rung still does not fit are stripes left out, biggest change first |
| `server/frames.py` `choose_stripes` | Which stripes: one refreshed in rotation every frame, then the rest ranked by how many pixels differ from what the device holds, until the target is spent. A stripe that misses out ranks at least as high next frame without any bookkeeping |
| `server/tv_server.py` `_pace_live` | Reads `channel.fps`, drives `ByteRate`, pushes the rate to the channel once a second. The slot arithmetic is unchanged |
| `server/media.py` | `FPS` is now a nominal 25 used for CONFIG's `fps` field and the pre-generated media. The device only range-checks it, so it can go out before the source has been looked at |

Delta coding is now **on by default** (`TV_DELTA=1`, tolerance `TV_DELTA_MAX_DIFF=0.01`). A
zero-length stripe is what the device skips, so **older firmware ends the session on the first one**;
either flash matching firmware or set `TV_DELTA=0`.

The opt-in v2 sender (`TV_LIVE_ENGINE=v2`, `server/live_sender.py`) is **not** covered by any of
this: it still drives the old `RateController` and its own 64 KB/s byte budget. It was left alone on
purpose rather than half-migrated. `media.FPS` no longer being `rate.MAX_FPS` would have silently
changed its default ceiling, so that one line now reads `rate.MAX_FPS` directly.

## 3. What was measured

Offline, on recorded channels (local captures of recorded channels, 25 or 30 fps, 200 frames), on the development
machine, with no ffmpeg running at the same time.

**The controller is not the frame rate limit any more.** With the old controller the product ran at
12 fps; with the frame rate fixed at the source's own rate and the controller off, the device drew
24.3 fps median at 349 KB/s in one 90 s run. The byte rate is what moves now.

**A hard per-frame size limit is wrong, and this is the measurement that settled the design.** At a
fixed 9-12 KB a frame, the busiest channel's worst frame came out at **PSNR 11 dB** with visible
bands of the previous scene: there is no way to spend 10 KB on a frame whose every stripe changed,
so the new scene arrived in pieces. Letting the cut through whole and paying it back over the
following frames (the earlier token-bucket rule) removed the bands. The current rule instead makes
the picture coarser, which keeps every stripe current and showed 17.95 dB at 16 KB, 10.7-10.9 dB
at 9-12 KB on the same channel.

**Where the bytes are going.** Per frame, lossless, 200 frames:

| Channel | full frame | delta, 3% tolerance | ratio |
|---|---|---|---|
| CCTV6 (30 fps, mostly static) | 6.1 KB | 0.5 KB | 8% |
| DFWS | 10.2 KB | 6.2 KB | 61% |
| HEBEI (25 fps) | 12.6 KB | 10.3 KB | 82% |
| CCTV1 (25 fps) | 13.6 KB | 7.9 KB (p95 19 KB) | 58% |
| CCTV13 (25 fps, busiest) | 22.4 KB | 16.9 KB | 75% |

At 25 fps those need about 30 KB/s (CCTV6) to 421 KB/s (CCTV13). Only the busiest channel exceeds
what the device has been shown to receive (349 KB/s), so for most channels the picture is carried
losslessly at the source's own rate and the extra work never happens.

**Coarsening costs less picture than the alternatives, per byte saved.** CCTV13, 60 frames, weighted
squared RGB error against the original colour picture:

| Method | bytes/frame | PSNR |
|---|---|---|
| lossless | 13.2 KB | 21.0 dB |
| colour ladder to a 16 KB target | 8.2 KB | 18.0 dB |
| colour ladder to a 12 KB target | 4.2 KB | 10.9 dB |
| **aware snapping, threshold 4000** | 9.0 KB | **21.2 dB** |
| aware snapping, threshold 16000 | 6.1 KB | 19.1 dB |

"Aware snapping" is not implemented: for each pixel, keep the value the panel already holds or the
left neighbour when that differs from the pixel's true colour by less than a threshold, so deflate
sees more repeats. Same bytes, about 2.5 dB better, and it degrades gracefully where the ladder
collapses. It needs the original RGB (3x the frame bytes from ffmpeg) and numpy, which the server
does not depend on at all today. `tools/quality_lab.py` reproduces the table.

**Encoding cost.** `choose_stripes` alone is 0.3-2.6 ms a frame in Python (six channels). With the
ladder it is up to **8.3 ms a frame** on the busiest channel, because each frame may compress six
times. Against a 40 ms frame interval that is affordable, but the margin is now 5x rather than 20x.

**Against the device, live CCTV1** (one session, 747 s, no failure): frame rate fixed at 25, byte
rate climbed to 583 kB/s, write times 31-78 ms, two 10 s device windows at 24.5 and 24.1 fps with 4
frames dropped, no audio gaps, no reset. At that rate the viewer reported no stutter and no tearing.
With the same code at a fixed 500 kB/s the viewer found the coarsening **clearly visible**.

## 4. What is not established

1. **The coarsening is visible at the rate needed to hold 20 KB a frame.** The viewer's own report,
   at 500 kB/s on live CCTV1. This is the main open problem: the mechanism is right, the quality
   loss is not yet acceptable.
2. **The three rate defaults are guesses.** `TV_RATE_START=250000`, `TV_RATE_MAX=320000` come from
   one 90 s single-channel measurement of 349 KB/s total. The ceiling that matters is the link's, and
   the only instruments for it are the write time and the drop count -- there is still no device
   feedback.
3. **Picture quality under pressure has never been watched as motion** by anyone, on any channel.
   Every number above is PSNR on still frames.
4. **One link, one channel, one viewer.** Everything device-side in section 3 is live CCTV1 on the
   computer's hotspot over about 20 minutes total.
5. **The frame-rate probe is not exercised against a real HLS playlist.** It was tested against local
   files and with `ffprobe` stubbed. A live HLS playlist often reports no `avg_frame_rate`.
6. **Nothing was re-tuned after the change.** `AUDIO_LEAD_MS`, `PREBUFFER_SECONDS`,
   `VIDEO_QUEUE_SECONDS` and the slot pacing were all chosen when the frame rate moved between 3 and
   12 and the picture was small. None of them has been revisited, and one suspect is already visible:
   at 25 fps a 16 s video queue is 400 frames of about 13 KB.
7. **Verification gaps.** `tools/validate.sh` cannot run here (its `python3` is a Microsoft Store
   stub, and there is no native C compiler or `actionlint`). Four Python test failures and one
   firmware test predate this work and fail identically with the changes stashed. The host C test for
   the new expander was syntax-checked only.
8. **A stale server on port 8096 cost most of a day.** A server from an earlier session was still
   holding the port, so the device connected to that one while the new server logged nothing. Every
   symptom looked like a device or firmware fault. Check `netstat -ano | grep :8096` first.

## 5. Next steps, in order

1. **Fix the visible coarsening**, which is the whole of open problem 1. Implement aware snapping
   (`tools/quality_lab.py` is the reference and the measurement) and binary-search the threshold per
   frame to hit the target. It needs either the original RGB from ffmpeg (a second output on the
   existing socket, 3x the bytes to the parent) or a colour distance computed from the index, which
   is weaker. **Whether the server may depend on numpy has to be decided by the operator**; without
   it, per-pixel work at 25 fps in pure Python is not obviously affordable, and needs measuring
   before it is promised.
2. **Re-tune the queue depths and leads for a fixed 25-30 fps picture.** Cheapest change with a
   possible latency win; the numbers were chosen for a 3-12 fps sender.
3. **Re-measure the rate range on several channels and a weak link**, and decide what the ceiling
   should be. Include fast-moving content; CCTV13 is the only busy sample recorded.
4. **Watch the picture as motion** on at least three channels, at the rate the controller settles on,
   before any of this is called an improvement. This is the acceptance test the project has been
   missing.
5. **Consider the 4-bit adaptive palette** (offline, earlier: 35% fewer bytes and *better* PSNR than
   3-3-2 on quiet channels) as the real fix for bytes, which would take the pressure off the
   coarsening. It needs device-side decoding changes, so it is a joint change and not a quick one.

## 6. Reproduction

```text
# Server, fixed 500 kB/s on a live channel (no controller movement):
TV_ADAPTIVE=0 TV_RATE_START=500000 TV_RATE_MAX=500000 python -m server.tv_server live --channel ch000 --bind <this machine's address>
# Server, controller on, defaults:
python -m server.tv_server live --channel ch000 --bind <this machine's address>

# Offline: frame sizes, stripe selection and the per-frame ladder
PYTHONPATH=. python tools/budget_lab.py /path/CCTV13.ts --frames 200 --rates 320000,500000
# Offline: quality against the original picture, all four methods (needs numpy)
PYTHONPATH=. python tools/quality_lab.py /path/CCTV13.ts

# Tests
PYTHONPATH=. python tests/test_rate.py && PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py
```

```text
Build: PASS (product public build; the 64 KB window is in the defaults)
Host tests: PASS for the new and changed code; 4 Python failures and 1 skipped pre-date this work; tools/validate.sh NOT RUN (no python3, no native C compiler, no actionlint here)
Device tests: one 747 s session on live CCTV1 at a 25 fps target; two 10 s windows read; the coarsening judged by eye at 500 kB/s
Unverified: picture quality as motion on any channel; busy content over a weak link; the rate defaults on any channel but CCTV1; the frame-rate probe against a real HLS playlist; queue depths re-tuned for a fixed rate
```
