English | [简体中文](live-sender-v2.zh_CN.md)

# Live sender v2 candidate

This candidate replaces the live media scheduling and socket writer. It keeps
the single FFmpeg input, source timestamp mapping, FAV1 protocol, native
320x240 indexed picture and 16 kHz mono PCM. It has host validation; it has
not been tested on the ESP32-C3. The ordinary entry point still selects the
legacy sender. Use the explicit candidate launcher below.

## Why change the sender

The supplied real-channel recording contains 79 complete device frames in
39.324 seconds (2.01 fps) and 208000 software silence samples (13 seconds).
Already-transcoded audio grows from 92 to 394 chunks while socket writes
stall. These observations support investigating downstream congestion and
scheduling; they do not establish a slow decoder as the sole cause.

The earlier fixed-12-fps trial profile established a controlled baseline; it
is unsuitable as a promise of sustainable performance for every live source.
This candidate changes scheduling rather than reducing picture resolution.

## Runtime boundaries

- One nonblocking writer owns one partially written packet. A write resumes
  at its saved offset. A partial packet timeout closes the connection; it
  never appends a replacement packet to an incomplete payload.
- Video packets are regrouped at stripe boundaries to at most 6144 payload
  bytes. Compressed stripe bytes and all 15 stripes remain intact. Audio
  takes priority between complete packets, never inside a video payload.
- A local 280 ms audio cushion limits catch-up to at most eight 40 ms chunks.
  Local scheduling resets do not rewrite media timestamps or claim that the
  device played the bytes. Fragmented END control headers are retained and
  read without blocking the media writer.
- Video admission is paced by both bytes and frames. The candidate starts
  at 3 fps with a 64000 B/s video budget, adapts from 1 to 12 fps, and caps its
  budget at 120000 B/s. These are conservative trial settings, not measured
  hardware limits. The minimum is a congestion fallback, not acceptance.
- If the host PCM backlog exceeds 8 seconds, retain approximately 4 seconds
  and discard video before the same content edge, between complete frames.
  Wire PCM timestamps remain contiguous. This intentionally skips programme
  content under overload; repeated trims are a defect indicator, not success.
- No audio available for 3 seconds closes the session for a reconnect. This
  bounded fallback does not prove seamless source recovery.

The writer infers pressure from completed local writes; it has no device
buffer feedback. The 5 ms per-stripe pacing allowance and 1-second packet
timeout need real-device validation. Small packets alone cannot fix a
receiver which permanently cannot sustain the payload or render rate.

## Run and measure

From the existing repository root, use the actual local channel file:

```bash
python3 tools/run_live_v2.py --repo . --channels /absolute/path/channels.txt --channel ch000 --dry-run
python3 tools/run_live_v2.py --repo . --channels /absolute/path/channels.txt --channel ch000 --output-dir /absolute/path/new-run
```

The launcher clears inherited `TV_*` experiments, installs the candidate
profile, checks the imported configuration, and records source hashes and a
new server log. It does not collect serial logs or declare a device PASS.
Omit `--loop` for live IPTV. Stop the existing server before taking its port.
For a controlled comparison, add `--engine legacy` to the same command and
use a separate output directory. This tests the alternate sender under the
same source and profile, not the old fixed-rate launcher.

Measure the actual source on the actual host without a device or sender:

```bash
TV_GEOMETRY=320x240 TV_FPS=12 TV_PACKET_TARGET=6144 TV_STREAM_LOOP=0 python3 tools/probe_live_source.py --channels /absolute/path/channels.txt --channel ch000 --seconds 20 --output /absolute/path/new-source-result.json
```

Use a new output filename. Audio supply near 25 chunks/s and video near
12 frames/s show realtime production at this profile. The paced probe is not
a peak CPU benchmark; `pack_wall_s` is elapsed packing time, not CPU usage.
If production is adequate but device playback stalls, investigate sending,
networking and receiving. If production is inadequate, measure source gaps
and FFmpeg CPU/progress before attributing the deficit to hardware.

`LIVE2` distinguishes `frames_tx`, `video_packets`, audio packet rate and
producer queues. Even `frames_tx` means complete frames written by the host,
not complete frames displayed by the device. Pair this log with serial
complete-frame counters and a recording of screen and speaker.

## Firmware and acceptance

No firmware source or wire format changes are included. Check the running
application identity and CONFIG compatibility before testing; firmware
changes and flashing remain possible if receiver defects are confirmed.
The supplied root `sdkconfig` and `sdkconfig.defaults` differ in TCP buffers
(5760 versus 32768 bytes) and receive mailbox depth (6 versus 16). Neither
proves what is running on the board. Inspect the actual build configuration
and internal-RAM budget; do not blindly increase buffers on a no-PSRAM board.

First run the failing real channel for 60 seconds with raw serial capture.
If healthy, continue that version for 30 minutes and test channel changes.
If it fails, preserve the first failure and source-only result before
changing another variable. Do not replace failed intervals with reconnections
when computing frame rate. Do not accept a slideshow merely because audio
is continuous. For the next trial release, use one real-channel 30-minute observation at
native resolution with the existing approximately 11-12 complete-device-fps
target, followed by channel switching and reconnect checks. Repeating the
controlled-baseline endurance run is no longer a prerequisite for this
trial. Precise output A/V alignment and the full repeated fault matrix
remain explicitly unverified until measured; trial readiness does not close
those requirements.

The default video budget cannot deliver 12 fps for a 20000-byte frame: even
without overhead its budget quotient is only 6 fps. Do not call that a
hardware ceiling or a completed requirement. After device audio and queues
are healthy, `--video-budget BYTES_PER_SECOND` supports one recorded
capacity trial using the measured frame cost. Allow time for the gradual
ramp and stop raising it if device underruns, drops or write stalls return.

## Validation scope

`python3 -m unittest discover -s tests -p 'test_live_sender_v2.py' -v` checks
packet integrity, partial I/O, bounded audio catch-up, joint timeline trims,
fragmented control input and bandwidth-limited real host TCP. A local
720p25 HLS fixture has also traversed the real FFmpeg and candidate launcher.
Neither test emulates ESP32 memory pressure, Wi-Fi, LCD or speaker output.

Build: NOT RUN (ESP-IDF unavailable in the review environment).
Host tests: see the candidate evidence bundle; excluded tests are named.
Device tests: NOT RUN for this candidate.
Unverified: sustainable real-channel fps, visible/audio continuity, output
sync, long-run recovery, actual firmware configuration and macOS behavior.

The supplied archive has no Git metadata, so the standard repository gate
stops at `git ls-files`. Run `./tools/validate.sh --static` in the executing
repository. Report firmware compilation and device results separately.

## Device capture and corrected summaries

`tools/run_live_v2_trial.py` wraps the same sender with serial capture. Provide
`--repo`, `--channels`, `--channel`, `--serial-port`, `--output-dir`, and the
chosen `--video-budget`. `--seconds` now counts observation after media start
and `--warmup-seconds`; use 1800 observation seconds for a 30-minute trial.
Use `--loop` only for a finite baseline. The tool requires pyserial, uses a
new directory each time, preserves terminal counters, and records monotonic
start/observation/stop markers in `capture_events.jsonl`.

Capture completion is not playback acceptance. Missing serial data, startup
failure and reader failure return nonzero. Raw resets after a requested stop
are reported separately. Without a stop marker they remain unclassified.
`tools/summarize_live_trial.py --run-dir RUN --output NEW_JSON` also audits old
logs: empty reconnect sessions cannot erase previous silence; cumulative
`dropped` counters are not added per window; `nobuf` remains a window sum.
Missing final counters mean unknown, not zero. Silence totals are software
counters for captured sessions, not acoustic or phase-local measurements.
Heap at periodic observations and the firmware's boot low watermark have
separate fields. The 13 new summary/capture tests use synthetic fixtures;
real board acceptance remains a separate task.
