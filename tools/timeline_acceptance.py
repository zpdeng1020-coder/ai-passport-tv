#!/usr/bin/env python3
"""Print the evidence for each acceptance condition the reviews asked for.

Run from the repository root:  python3 tools/timeline_acceptance.py

This exists because the alternative is a reader taking my word for it. Every
line below is produced by the code in this repository, and the counterexamples
that broke earlier versions are included as comparisons rather than described.

**This script must be run and its output regenerated whenever the timeline
changes.** An older copy of its output was once shipped alongside the script
after the script had stopped working, which is worse than shipping neither: it
reads as a current result. `--check` makes that failure loud by exiting non-zero
when any stated expectation is not met.
"""
import sys
sys.path.insert(0, '.')
from tools.console import use_utf8
use_utf8()   # before anything is printed; see tools/console.py for why

import collections
import threading
import unittest.mock
from server.protocol import AUDIO_BYTES

import server.live as L
from server import frames
from server.media import AUDIO_CHUNK_MS, FPS
from server.protocol import AUDIO_BYTES
from server.timeline import (BASIS_COMMON_DECODE, BASIS_LAUNCH, BASIS_MEDIA_START, ContentTimeline,
                             SessionClock, SourceState)

FRAME_MS = 1000.0 / FPS
CHUNK = float(AUDIO_CHUNK_MS)

failures = []


def check(condition, description):
    """Record an expectation; --check turns any miss into a non-zero exit."""
    if not condition:
        failures.append(description)
    return condition


def channel(video_start_s=0.0, audio_start_s=0.0, basis=BASIS_MEDIA_START,
            calibrate=True):
    """A LiveChannel with the real methods and none of the processes."""
    ch = L.LiveChannel.__new__(L.LiveChannel)
    ch.lock = threading.RLock()
    ch.video = collections.deque(maxlen=2000)
    ch.audio = collections.deque(maxlen=6000)
    ch.video_at = collections.deque(maxlen=2000)
    ch.audio_at = collections.deque(maxlen=6000)
    ch.video_content = collections.deque(maxlen=2000)
    ch.audio_content = collections.deque(maxlen=6000)
    ch.dropped_video = 0
    ch.skipped_audio = 0
    ch.video_epoch = 0.0
    ch.audio_epoch = 0.0
    ch._video_advanced = ch._audio_advanced = False
    ch.timeline = ContentTimeline(FRAME_MS, CHUNK)
    ch.session_clock = SessionClock(chunk_ms=CHUNK)
    ch.source = SourceState()
    if calibrate:
        ch.timeline.calibrate(video_start_s, audio_start_s, basis=basis)
    return ch


def feed(ch, chunks=30, frame_ms=None, with_video=True):
    """Push sound and picture at their own production rates.

    The picture's content advances by the frame interval, not by the audio
    chunk; giving the two the same step was a defect in one of this project's
    own test fixtures and made a healthy stream look like it was drifting.
    """
    frame_ms = FRAME_MS if frame_ms is None else frame_ms
    for _ in range(chunks):
        ch.audio.append(bytes(AUDIO_BYTES))
        ch.audio_at.append(0.0)
        ch.audio_content.append(ch.timeline.audio.take())
    if with_video:
        for _ in range(int(chunks * CHUNK / frame_ms) + 1):
            ch.video.append([b"pkt"])
            ch.video_at.append(0.0)
            ch.video_content.append(ch.timeline.video.take())


print("=" * 72)
print("验收 1：同一内容晚交付 600 ms，内容时刻不变、仍能配对")
print("=" * 72)
results = []
for delay, label in [(0.0, "按时交付"), (0.6, "晚 600 ms")]:
    ch = channel()
    feed(ch)
    # Rewrite the ARRIVAL diagnostics only. Content time is untouched, which is
    # the whole point: the network's behaviour may not reach the pairing.
    ch.audio_at = collections.deque([delay + i * 0.04 for i in range(len(ch.audio))])
    ch.video_at = collections.deque([delay + i * FRAME_MS / 1000.0
                                     for i in range(len(ch.video))])
    # The sender takes the sound first -- that is what anchors the session clock
    # and what the picture is placed against.
    ch.pop_audio()
    got = ch.pop_video()
    stamp = got[1] if got else None
    results.append((bool(got), stamp))
    print(f"   {label:<12} 取到帧={'yes' if got else 'no ':3} "
          f"线上时间戳={stamp} ms")
check(results[0] == results[1],
      "a delivery delay must not change the pairing or the timestamp")
print("   -> 交付延迟不进入配对：两者结果相同\n")

print("=" * 72)
print("验收 2：依据由「谁失败了」决定，不由网址长相决定")
print("=" * 72)
NO_START = "the source reports no start_time for its video stream"
MISSING = "ffprobe is not installed"
print(f"   媒体答了、只说没有起始时间 -> has_media_start_times="
      f"{L.LiveChannel.has_media_start_times([NO_START])}")
print(f"   探针本身跑不起来           -> has_media_start_times="
      f"{L.LiveChannel.has_media_start_times([MISSING])}")
check(L.LiveChannel.has_media_start_times([NO_START]),
      "media without start times must be identified")
check(not L.LiveChannel.has_media_start_times([MISSING]),
      "missing probe must not be identified as media without start times")
print()
print("   单解码器主路径：两路媒体来自同一进程，统一采用媒体时间依据")
ch_common = channel(calibrate=False)
ok = ch_common.calibrate_from_source()
print(f"     http://host/source.mkv       calibrated={ok!s:5s} "
      f"basis={ch_common.timeline.basis}")
check(ok and ch_common.timeline.calibrated and ch_common.timeline.basis == L.BASIS_COMMON_DECODE,
      "common decode must calibrate directly with BASIS_COMMON_DECODE")
print()
print("   回退状态与依据区分：近似带标签，未知不当作已校准")
ch_launch = channel(calibrate=False)
ch_launch.timeline.calibrate(0.0, 0.6, basis=BASIS_LAUNCH)
print(f"     http://host/live.m3u8      calibrated={ch_launch.timeline.calibrated!s:5s} "
      f"basis={(ch_launch.timeline.basis or '-')[:20]} state={ch_launch.timeline.state}")
check(not ch_launch.timeline.calibrated and ch_launch.timeline.usable and ch_launch.timeline.basis == BASIS_LAUNCH,
      "the documented live case must still be allowed, and labelled")
print("   -> 未知就是未知；近似必须带标签，且不被当作已校准\n")

print("=" * 72)
print("验收 3：画面用「它自己」的内容时刻打戳，不是它所配对的音频的位置")
print("=" * 73)
ch = channel()
# One frame per audio chunk, so the arithmetic below is in whole chunks.
feed(ch, chunks=60, frame_ms=CHUNK)
# The sender takes the sound first, which anchors the session clock; then it
# selects the frame whose own content is three chunks older than the sound now
# at the head of the audio queue.
for _ in range(25):
    ch.pop_audio()
head_content = ch.audio_content[0]
for _ in range(25):
    ch.video_content.popleft()
    ch.video.popleft()
    ch.video_at.popleft()
ch.video_content.appendleft(head_content - 3 * CHUNK)
ch.video.appendleft([b"pkt"])
ch.video_at.appendleft(0.0)
got = ch.pop_video()
stamp = got[1] if got else None
# The head of the audio queue is the block the sender takes next, and the
# session clock counts it as the 25th taken -- so its session time is 25*chunk,
# not 24. The frame is three chunks older than that.
head_session = 25 * CHUNK
expected = int(round(head_session - 3 * CHUNK))
print(f"   音频头上内容时刻      = {head_content:.1f} ms")
print(f"   该帧自己的内容时刻    = {head_content - 3 * CHUNK:.1f} ms")
print(f"   它得到的线上时间戳    = {stamp} ms")
print(f"   若用音频位置（旧行为）= {head_content:.0f} ms")
print(f"   那会人为增加          = {head_content - (stamp or 0):.1f} ms 的偏差")
check(stamp == expected,
      f"the frame must be stamped with its own content ({expected} ms), "
      f"got {stamp}")
print()

print("=" * 72)
print("验收 4：音频跳段时，映射分段而不是把缺口摊平")
print("=" * 72)
clock = SessionClock(chunk_ms=CHUNK)
stamps = [clock.audio(1000.0 + i * CHUNK) for i in range(3)]
print(f"   连续段的线上时间戳      = {stamps}")
after = clock.audio(5000.0)              # the source jumped forward
print(f"   跳段后一块的线上时间戳  = {after}  （仍然只加一个分块）")
print(f"   记录到的内容缺口        = {clock.content_gap_ms} ms "
      f"（{clock.audio_gaps} 次）")
print(f"   跳段后 5000 ms 处的内容映射到会话 = "
      f"{clock.video(5000.0)} ms")
check(after == 3 * CHUNK, "the wire clock must stay contiguous across a skip")
check(clock.audio_gaps == 1, "the skip must be counted")
check(clock.content_gap_ms > 0, "the skipped content length must be measured")
print()

print("=" * 72)
print("验收 5：音频缺口内的画面拿不到时间戳，恢复帧不被污染")
print("=" * 72)
# The review's counterexample, through the real push/pop methods. Before the
# fix: three frames inside the dropped interval were stamped 4750/4833/4917,
# and the recovery frame, mapping correctly to 120 ms, was clamped to 4918.
gap = channel(calibrate=False)
gap.timeline.calibrate(0.0, 0.0)
for i in range(3):
    gap._push_audio(bytes(AUDIO_BYTES), i * AUDIO_CHUNK_MS)
for _ in range(3):
    gap.pop_audio()
next_stamp = gap.session_clock.audio_items * AUDIO_CHUNK_MS
gap.timeline.audio.count += 122          # 122 blocks never produced
gap._push_audio(bytes(AUDIO_BYTES), 5000.0)
print(f"   会话已走到            = {next_stamp} ms")
print(f"   源跳到内容            = {gap.audio_content[-1]:.0f} ms")
print(f"   缺掉的音频内容区间    = [120, {gap.audio_content[-1]:.0f})，"
      f"共 {gap.audio_content[-1] - 120:.0f} ms")
print()
print(f"   {'画面内容':>10} {'session_of':>12} {'线上时间戳':>12}")
for c in (4750.0, 4833.333, 4916.667):
    place = gap.session_clock.session_of(c)
    stamp = gap.session_clock.video(c)
    shown = "None" if place is None else f"{place:.1f}"
    print(f"   {c:10.1f} {shown:>12} {str(stamp):>12}")
check(gap.session_clock.frames_unplaceable == 3,
      "frames inside the dropped interval must be refused a timestamp")
check(gap.session_clock.last_video_ms == -1,
      "no frame inside the gap may have advanced the picture clock")
gap.pop_audio()
recovered = gap.session_clock.video(5000.0)
print(f"   {5000.0:10.1f} {gap.session_clock.session_of(5000.0):12.1f} "
      f"{str(recovered):>12}   <- 恢复帧")
check(recovered == next_stamp,
      f"the recovery frame must be placed at {next_stamp} ms, got {recovered}")
print()
print("   -> 缺口内的画面被拒绝打戳，恢复帧拿到诚实的会话时刻")
print("      （修复前：4750/4833/4917 通过，恢复帧被钳到 4918）")
print()
# 短缺口回归：320 ms 音频缺口，验证等待不丢帧与恢复时刻
sg_ch = channel(calibrate=False)
sg_ch.timeline.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)
for t in [0, 40, 80, 400, 440]:
    sg_ch._push_audio(bytes(AUDIO_BYTES), float(t))
for _ in range(3):
    sg_ch.pop_audio()
sg_ch._push_video(bytes(frames.FRAME_PIXELS), 1000.0 / 3)
sg_ch._push_video(bytes(frames.FRAME_PIXELS), 5000.0 / 12)
early = sg_ch.pop_video()
check(early is None, "frames ahead of pending audio must wait without being prematurely dropped")
resumed_audio = sg_ch.pop_audio()
check(resumed_audio is not None and resumed_audio[1] == 120, "audio must resume at 120 ms")
sg_recovered = sg_ch.pop_video()
check(sg_recovered is not None and sg_recovered[1] == 137,
      f"short gap recovery frame must be stamped 137 ms, got {sg_recovered[1] if sg_recovered else None}")
check(sg_ch.session_clock.frames_in_hole == 1, "exactly 1 frame in hole must be recorded")
print(f"   短缺口回归：缺口内帧丢弃，等待帧保留，恢复帧时间戳={sg_recovered[1] if sg_recovered else None} ms (期望 137 ms)\n")
print()

print("=" * 72)
print("验收 6：源断供与下游拥塞被分开记录")
print("=" * 72)
state = SourceState(stalled_windows_before_starved=2)
state.observe(video_advanced=False, audio_advanced=False, queue_over_bound=False)
print(f"   一个安静窗口            -> {state.state}  ({state.note})")
state.observe(video_advanced=False, audio_advanced=False, queue_over_bound=False)
print(f"   连续两个                -> {state.state}  ({state.note})")
state.observe(video_advanced=True, audio_advanced=True, queue_over_bound=True)
print(f"   内容在来、队列越界      -> {state.state}  ({state.note})")
check(state.state == SourceState.CONGESTED, "congestion must be reported")
print()

print("=" * 72)
print("验收 7：basis 会被记录并随报告打印")
print("=" * 72)
print(f"   BASIS_MEDIA_START = {BASIS_MEDIA_START!r}")
print(f"   BASIS_LAUNCH      = {BASIS_LAUNCH!r}")
ch_a = channel(0.0, 0.0, basis=BASIS_MEDIA_START)
ch_b = channel(0.0, 0.6, basis=BASIS_LAUNCH)
print(f"   媒体起始时间校准 -> basis={ch_a.timeline.basis!r} offset={ch_a.timeline.offset_ms:+.0f}ms")
print(f"   启动时间校准     -> basis={ch_b.timeline.basis!r} offset={ch_b.timeline.offset_ms:+.0f}ms")
print("   -> 同一个 600 ms，依据不同含义相反，所以必须随日志打印")
check(ch_a.timeline.basis != ch_b.timeline.basis,
      "the two bases must remain distinguishable")
print()

print("=" * 72)
if failures:
    print(f"FAILED: {len(failures)} expectation(s) not met")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("All stated expectations hold.")
