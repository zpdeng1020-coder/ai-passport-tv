<p align="right">
  <a href="round-10-changes.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Round 10 Changes Description

This round modifies only the server. Firmware, BSP, protocol fields, and device-side code remain byte-for-byte untouched; no reflashing was performed. Existing networking and NVS fixes, User-Agent, continuous frame timestamping, and acceptance scripts are retained without redo.

## 1. F22: Audio, Video, and Time Derived from Single Source Read

### Prior Behavior

Two separate ffmpeg processes decoded the same source independently, one outputting indexed frames and the other outputting PCM. Content time for each stream was obtained by counting (n-th frame at `n / fps`, n-th audio chunk at `n * 40 ms`), and the relationship between the two streams was measured only once at session startup using `ffprobe stream=start_time`. When probing produced no answer, it fell back to process launch times.

Counting measures rate, not clock. It holds for averages, fails for individual items, and hides gaps: when a decoder skips 300 ms of audio and continues emitting chunks every 40 ms, counting flattens the hole and shifts everything forward.

### Current Behavior

A single ffmpeg process reads the source once, uses a single filtergraph producing both streams, and reports real `pts_time` via `showinfo` and `ashowinfo` in that filtergraph:

```
[0:v]fps=12,<fit>,format=rgb8,showinfo[v];
[0:a]aresample=16000,aformat=channel_layouts=mono,asetnsamples=n=640,ashowinfo[a]
```

Both streams share a single timeline without offset inference or measurement. `server/pts.py` retrieves these timestamps from stderr and matches the logged `n` against read counts; mismatches raise errors to end the session rather than shifting all subsequent items.

### Verification

Captured 12 seconds on real HLS channel: 144 `showinfo` lines matched 144 frames, 300 `ashowinfo` lines matched 300 chunks (384,000 bytes), with `n` starting consecutively from 0. The first video frame PTS was 0.0833333 and audio first chunk was 0, differing by 83 ms—a property of the media itself hidden by previous schemes.

## 2. F25: Short Audio Gaps

### Prior Defect

Segmented mapping only closed an old segment when the audio chunk after the gap was retrieved. Prior to retrieval, the old segment extended forward, extrapolating frames into session timestamps unoccupied by sound and driving up subsequent frames via clamping.

### Current Approach

`SessionClock.placement()` accepts `pending_audio_ms` to evaluate gaps before the recovery audio is pulled, returning one of four verdicts: `placed`, `in-dropped-sound`, `sound-not-taken-yet`, or `no-sound-taken`. `pop_video()` filters frames accordingly.

### Verification

Frames inside gaps are rejected, recovery frames receive honest timestamps (137 ms), and both calling orders produce identical results.

## 3. Calibration Status

Distinguishes three states: `unknown`, `approximate` (derived from process launch times), and `from media timestamps`. Only the third marks `ContentTimeline.calibrated` as true.

## 4. Hardware Results and Key Conclusion

Testing on real hardware confirmed decoder timestamps were reported correctly with `basis=(one decode, the decoder's own timestamps)`. However, frame rate remained low (3-5 fps on that earlier unoptimized run), confirming that low frame rate was not caused by timeline calculation.

## 5. Unfinished Items

D3 silence re-alignment, 30-minute endurance testing, and performance optimization remain for subsequent batches.
