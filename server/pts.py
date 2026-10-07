"""音视频各自的时间戳，取自产出它们的同一个解码进程。

一个 ffmpeg 进程解码一次并同时输出两路载荷，`showinfo` 与 `ashowinfo` 在其滤镜图中
逐条记录每个条目的 `pts_time`。时间戳与载荷出自同一次解码、处于同一时间轴，无需推算偏移，
源中的断档也能被看到。

每个条目一行日志，按序号校验。`showinfo` 在写出前记录，读完载荷时对应行必已就绪。
每行带 `n`，读取方与已读条目数比对；不符说明丢了行，此后所有时间戳都会错位一个条目，
此时必须拒绝继续。`asetnsamples=n=640` 与强制单声道使一行日志恰好对应一个设备块。
"""

from __future__ import annotations

import collections
import re
import threading

# ffmpeg 为每个条目输出：
#   [Parsed_showinfo_3 @ 0x...] n:   0 pts:      0 pts_time:0       duration: ...
#   [Parsed_ashowinfo_6 @ 0x...] n:0 pts:0 pts_time:0 fmt:s16 channels:1 ...
# 两行字段形状相同，只能按滤镜名区分；`Parsed_ashowinfo` 不含 `Parsed_showinfo`。
VIDEO_FILTER = b"Parsed_showinfo"
AUDIO_FILTER = b"Parsed_ashowinfo"
_FIELDS = re.compile(rb"\] n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([-\d.eE+]+)")


def parse(line: bytes) -> tuple[str, int, float] | None:
    """返回（流类型，序号，时间）；不是目标行时返回 None。

    `showinfo` 还会输出同前缀但没有 `n:` 的行，要求完整字段序列以排除它们。
    """
    if AUDIO_FILTER in line:
        kind = "audio"
    elif VIDEO_FILTER in line:
        kind = "video"
    else:
        return None
    found = _FIELDS.search(line)
    if found is None:
        return None
    return kind, int(found.group(1)), float(found.group(3))


class Misaligned(RuntimeError):
    """解码器给出的序号与已读条目对不上；此后的时间戳都会错位，只能拒绝而不修复。"""


class Timestamps:
    """按解码器产出顺序保存的时间戳；读完第 N 个条目即取第 N 个时间戳，并校验序号。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._video: collections.deque[tuple[int, float]] = collections.deque()
        self._audio: collections.deque[tuple[int, float]] = collections.deque()
        self._expected = {"video": 0, "audio": 0}
        self.received = {"video": 0, "audio": 0}
        self._closed = False
        # 保留解码器最近的日志行，会话异常结束时用于排查。
        self.tail: collections.deque[bytes] = collections.deque(maxlen=64)

    def note(self, line: bytes) -> None:
        """记录一条日志；在 stderr 读取线程中调用。"""
        self.tail.append(line)
        found = parse(line)
        if found is None:
            return
        kind, index, seconds = found
        with self._condition:
            queue = self._video if kind == "video" else self._audio
            queue.append((index, seconds))
            self.received[kind] += 1
            self._condition.notify_all()

    def take(self, kind: str, timeout: float = 5.0) -> float | None:
        """返回 `kind` 的下一个时间戳（秒）；超时或已关闭且无数据时返回 None。

        滤镜先于载荷写出日志，正常情况下读完载荷时本行已到，不会阻塞；
        超时只用于解码器已停止的情况，避免源停供拖死服务端。
        """
        with self._condition:
            queue = self._video if kind == "video" else self._audio
            deadline = _now() + timeout
            while not queue and not self._closed:
                remaining = deadline - _now()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if not queue:
                return None
            index, seconds = queue.popleft()
            expected = self._expected[kind]
            if index != expected:
                raise Misaligned(
                    f"{kind} timestamp index {index} arrived where {expected} "
                    f"was due; a decoder log line was lost and every later "
                    f"timestamp would be one item out")
            self._expected[kind] += 1
            return seconds

    def close(self) -> None:
        """标记关闭并唤醒在 take() 中等待的线程。"""
        with self._condition:
            self._closed = True
            self._condition.notify_all()



def _now() -> float:
    import time
    return time.monotonic()


class Reader(threading.Thread):
    """持续读取解码器 stderr 并写入 `Timestamps`。

    必须独立线程：没人读日志时 ffmpeg 会阻塞，表现与源停供无法区分。
    """

    def __init__(self, stream, timestamps: Timestamps):
        super().__init__(daemon=True, name="decoder-log")
        self._stream = stream
        self._timestamps = timestamps
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            for line in iter(self._stream.readline, b""):
                self._timestamps.note(line)
        except (OSError, ValueError) as error:
            self.error = error
