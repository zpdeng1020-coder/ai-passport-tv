<p align="right">
  <a href="server-optimisation-handoff-20260930.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Server-side optimisation: handoff (2026-09-30)

**Read this first for server or picture-quality work.** It is the state after the device side was
frozen: what the server now does, what is not established, and what to do next in order. The
transport work it descends from is in [tcp-delta-progress-20260930.md](tcp-delta-progress-20260930.md);
that document's device sections are still valid, its server sections are not.

The work is committed on `feature/cjpjxjx`.

**This file deliberately records no per-channel or per-link numbers.** What a channel costs in bytes,
what a link carries, and what a frame rate measured on one run was all change with the content and
the network, so none of them is a fact about the project. Measure again on the channel and link being
worked on; the tools are in section 6.

## 1. The decision that was made

The frame rate is **fixed**, the byte rate is **fixed**, and the picture's **detail** is what gives
when a frame does not fit.

A frame rate that moves is the thing a viewer notices most; a picture that is briefly coarser mostly
is not. So a channel plays at its source's own rate for the whole session, and every frame is
compressed to fit `target byte rate / frame rate` (the default target is 20000 bytes a frame, `TV_FRAME_BYTES`, so the byte rate follows
the frame rate: 500 kB/s at 25 fps, 600 kB/s at 30). A frame that does not fit is made coarser until it does. There is no ceiling on
how coarse the picture may get to meet the target.

**The fixed mode is the default and the one to get right first.** The adaptive byte-rate controller
(`rate.ByteRate`) still exists and is enabled with `TV_ADAPTIVE=1`, together with `TV_RATE_MAX` raised
above the start. Do not consider it until the fixed mode is good: a controller that moves the target
moves the picture quality with it, and any judgement of the picture becomes a judgement of the
controller.

What this replaced: a controller that stepped the frame rate between 3 and 12 from write time and
dropped frames, with a per-channel ceiling derived as `budget / bytes per frame`. Its budget was
never measured as a link limit, and it capped the frame rate far below what the same link and
firmware carried with the controller held still. See the progress document, section 1.

## 2. What the server does now

| Where | What it does |
|---|---|
| `server/live.py` `probe_source_fps` | Asks `ffprobe` for the channel's frame rate at startup: `avg_frame_rate` first (a 25 fps programme carried as 50 fields reports `r_frame_rate` 50 and `avg_frame_rate` 25), halved while over 30, the device's own bound (`json_between(j,"fps",1,30)` in `main/av_player.c`). Cached per URL. An explicit `TV_FPS` skips the probe; a source that does not answer plays at the nominal 25 |
| `server/live.py` `LiveChannel.fps`, `source_graph`, `source_command` | The probed rate goes into the ffmpeg filter graph, the content timeline and the sender's pacing, so all three agree by construction |
| `server/rate.py` `ByteRate` | Holds the byte rate at `TV_FRAME_BYTES` x fps by default (`TV_ADAPTIVE` off, reason `fixed`). With `TV_ADAPTIVE=1` it moves the rate: down at once on a write over 250 ms or two windows running with drops, up 5% after two windows that both wrote under 120 ms **and** used at least 80% of the rate. The second condition is the one that matters: a still picture writes instantly because it is small, and a rate raised on that opens the gate for the next cut to spend it all at once |
| `server/frames.py` `encode_within` | Per frame: compress at the current quality, then progressively coarser colour steps (red and green 8→4→3→1 levels, blue 4→1), take the first that fits `rate / fps`, keep nothing between frames. Every stripe sent is from the current frame. Only if the coarsest rung still does not fit are stripes left out, biggest change first |
| `server/perceptual.py` `encode_within` | **On by default when numpy is installed** (`TV_PERCEPTUAL=0` or no numpy uses the ladder row above). A frame that fits the target goes out exactly as ffmpeg made it. One that does not is not laddered: each pixel may keep the device's value or its left neighbour when that palette colour stays within a per-frame threshold of the reference pixel (10 levels, searched from the previous frame's level; a fitting frame is tried first at level 0, then the search starts at the previous level). Same cost as before when a frame fits; about 14-18 ms when it does not |
| `server/frames.py` `choose_stripes` | Which stripes: one refreshed in rotation every frame, then the rest ranked by how many pixels differ from what the device holds, until the target is spent. A stripe that misses out ranks at least as high next frame without any bookkeeping |
| `server/tv_server.py` `_pace_live` | Reads `channel.fps`, drives `ByteRate`, pushes the rate to the channel once a second. The slot arithmetic is unchanged |
| `server/media.py` | `FPS` is a nominal 25 used for CONFIG's `fps` field and the pre-generated media. The device only range-checks it, so it can go out before the source has been looked at |

Delta coding is **on by default** (`TV_DELTA=1`, tolerance `TV_DELTA_MAX_DIFF=0.01`). A zero-length
stripe is what the device skips, so **older firmware ends the session on the first one**; either
flash matching firmware or set `TV_DELTA=0`.

The opt-in v2 sender (`TV_LIVE_ENGINE=v2`, `server/live_sender.py`) is **not** covered by any of
this: it still drives the old `RateController` and its own 64 KB/s byte budget. It was left alone on
purpose rather than half-migrated. `media.FPS` no longer being `rate.MAX_FPS` would have silently
changed its default ceiling, so that one line now reads `rate.MAX_FPS` directly.

## 3. What was learned

These are findings about the design, not figures to rely on.

**A hard per-frame size limit is wrong, and this is what settled the design.** With every frame
forced under a small fixed size, a frame in which every stripe changed cannot be sent whole, so the
new scene arrives in pieces: visible bands of the previous scene and a very low PSNR on the worst
frame. Letting a cut through whole and paying it back over the following frames (the earlier
token-bucket rule) removed the bands. The current rule instead makes the picture coarser, which
keeps every stripe current.

**Coarsening by colour ladder is not the best use of the bytes, and the perceptual choice is now implemented (`server/perceptual.py`).** At about equal bytes it measured 2 to 9 dB better than the ladder (PSNR against the original picture, 3 channels, 100 frames each, offline), and where the ladder falls to noise at the tightest targets it degrades gently. Those are still-frame PSNR figures on whatever was on air at the time, so they prove the ordering and not a number to quote. The ladder also jumps a whole rung at a time, so it can land far under its target and waste the rest, which the per-frame threshold search does not.

**A first version was wrong, and the way it was wrong is worth keeping.** It took the picture as RGB and mapped every pixel to the *nearest* palette colour, which scored about 2 dB better and 8-14% smaller by PSNR. The viewer compared it with the default and rejected it: ffmpeg's conversion is biased dark and neutral, and the nearest colour turns dark greys blue and puts coloured blotches in shadows, because blue has only four levels and counts for little in the error. **PSNR was not a proxy for how it looks.** The picture a viewer finds normal is ffmpeg's own index frame, so that is the reference now, and the give-and-take works on it in palette space.

**How often it engages depends on the target and the content, and lowering the target makes it common.** Frames of a busy channel routinely sit near the target, so a few KB either way moves the share of frames that engage from a few percent to a third or more. When it does not engage, what the viewer sees is the fixed 3-3-2 palette itself, not anything the byte budget does. The give-and-take is for bursts, scene cuts and tighter targets; improving everyday colour means changing the representation (an adaptive palette, more bits), not the budget logic.

**Encoding cost is content-dependent.** The ladder may compress a frame up to six times, so the
per-frame time on busy content is several times that of stripe selection alone. Measure it on the
busiest channel before adding more per-frame work; the margin against the frame interval is what
pays for it.

## 4. What is not established

1. **Coarsening was visible at a 20 KB-a-frame target on busy content.** This is the viewer's own
   report. It is the main open problem: the mechanism is right, the quality loss is not yet
   acceptable.
2. **The 20000-bytes-a-frame target is the operator's choice, not a measured link ceiling.** Whether a given
   link carries it is checked with the write time and the drop count, the only instruments there are
   -- there is still no device feedback. In the fixed mode nothing slows the sender when the link
   degrades; a write that stalls costs dropped frames.
3. **The device's frame-rate ceiling has not been found.** The firmware refuses any frame rate above 30 (`json_between(j,"fps",1,30)` in `main/av_player.c`), and a plain static picture held that 30 fps on the board in one run, so 30 is where the search stopped, not where the device did. Finding the ceiling means raising that bound in firmware. Separately, the byte target is a **ceiling on a frame, not the amount sent**: most frames come out well under it, so the byte rate on the wire is usually below the configured rate. A load test at the configured rate needs content that actually costs that much per frame, and a test with incompressible content measures the worst case, not the frame rate.
4. **Picture quality under pressure has not been watched as motion** on enough channels to say
   anything. Every quality number that was produced is PSNR on still frames.
5. **The frame-rate probe is not exercised against a real HLS playlist.** It was tested against local
   files and with `ffprobe` stubbed. A live HLS playlist often reports no `avg_frame_rate`.
6. **Nothing was re-tuned after the change.** `AUDIO_LEAD_MS`, `PREBUFFER_SECONDS`,
   `VIDEO_QUEUE_SECONDS` and the slot pacing were all chosen when the frame rate moved between 3 and
   12 and the picture was small. None of them has been revisited, and one suspect is already visible:
   at 25 fps a 16 s video queue is 400 frames.
7. **Verification gaps.** `tools/validate.sh` cannot run where `python3` is a Microsoft Store stub or
   there is no native C compiler or `actionlint`. Three Python test failures --
   `test_live_mode_signal_handler_stops_the_accept_loop` in `test_live_transcode.py` and
   `test_environment_and_restricted_token_file` and `test_fragmented_and_coalesced_stream` in
   `test_tv_server.py` -- fail identically with the changes stashed. The host C test for the new
   expander was syntax-checked only.
8. **Channel URLs go dead.** The channel list is public streams, and entries stop working without
   notice (the default `ch000` returned 404 when last tried). A session that fails at `live_palette`
   is usually the source, not the server; check the URL before suspecting the code.
9. **A stale server on port 8096 cost most of a day.** A server from an earlier session was still
   holding the port, so the device connected to that one while the new server logged nothing. Every
   symptom looked like a device or firmware fault. Check `netstat -ano | grep :8096` first.
10. **`tools/launch.py` does not pass `--bind`**, and the media server refuses to start without an
   explicit loopback or RFC1918 address, so the launcher exits with "the program could not start".
   The exception is swallowed on purpose, so the cause is invisible. Start the server directly with
   `--bind` (section 6).

## 5. Next steps, in order

1. **Watch the perceptual choice as motion.** It is implemented and runs on the board (a 45 s
   window read at the target frame rate with no device drops), but nobody has judged the picture.
   Compare `TV_PERCEPTUAL=0` and the default on the same channel. Then decide whether the fixed
   3-3-2 palette itself is the limit, since the byte budget rarely binds (see section 3).
   Not yet done: packaging (`tools/build_server.py`) does not bundle numpy, so a packaged build runs the ladder; the encode runs on the sending thread under the channel lock, which its typical cost tolerates and its 14-18 ms worst case leaves about half a frame of margin for.
2. **Re-tune the queue depths and leads for a fixed 25-30 fps picture.** Cheapest change with a
   possible latency win; the numbers were chosen for a 3-12 fps sender.
3. **Check that the target holds on several channels and a weak link**, including fast-moving
   content, and decide from that whether 20000 bytes a frame is the right default.
4. **Watch the picture as motion** on at least three channels, at the fixed target, before any of
   this is called an improvement. This is the acceptance test the project has been missing.
5. **Consider the 4-bit adaptive palette** (an earlier offline result: fewer bytes and better PSNR
   than 3-3-2 on quiet content) as the real fix for bytes, which would take the pressure off the
   coarsening. It needs device-side decoding changes, so it is a joint change and not a quick one.
6. **Only then, consider the adaptive controller** (`TV_ADAPTIVE=1`).

## 6. Reproduction

```text
# Server, default: fixed 20000 bytes a frame (byte rate = that x the source's frame rate). --bind must be this machine's
# own loopback or private address. Pick a channel that currently plays (ch000 may be dead).
python -m server.tv_server live --channel <channel id> --bind <this machine's address>
# A different fixed target (or TV_RATE_START=<bytes a second> to set the rate directly):
TV_FRAME_BYTES=12000 python -m server.tv_server live --channel <channel id> --bind <this machine's address>
# Adaptive controller, opt-in (raise the ceiling or it cannot climb):
TV_ADAPTIVE=1 TV_RATE_MAX=600000 python -m server.tv_server live --channel <channel id> --bind <this machine's address>

# Offline: frame sizes, stripe selection and the per-frame ladder
PYTHONPATH=. python tools/budget_lab.py /path/<channel>.ts --frames 200 --rates 500000
# Offline: quality against the original picture, all four methods (needs numpy)
PYTHONPATH=. python tools/quality_lab.py /path/<channel>.ts

# Tests
PYTHONPATH=. python tests/test_rate.py && PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py
```

```text
Build: NOT RUN for this document change (firmware untouched)
Host tests: PASS for test_rate, test_delta_encoding, test_fixed_rate, test_perceptual; the 3 failures in section 4 item 7 pre-date this work; tools/validate.sh NOT RUN
Device tests: the server was run against the board at the fixed default and with the perceptual choice on; the device's own 10 s frame-rate windows were read at the target rate with no device drops (single runs, a few minutes, one link); picture quality was not judged by anyone
Unverified: picture quality as motion on any channel, including the perceptual choice; a packaged build with numpy; busy content over a weak link; whether the 20000-byte default holds on channels and links other than the ones tried; the frame-rate probe against a real HLS playlist; queue depths re-tuned for a fixed rate
```
