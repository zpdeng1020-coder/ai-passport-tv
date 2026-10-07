"""直播 HLS 转码：单个 ffmpeg 进程解码一次，产出设备可绘制的调色板索引画面与 16 kHz 单声道 PCM。

画面缩放到面板几何并量化到固定 256 色调色板，音频与画面各走一条回环 TCP 连接，
解码器自身的 pts 由 `showinfo` / `ashowinfo` 记录（见 server/pts.py）。
两个读取线程负责切包，会话发送端按同一单调时钟调度。设备不做缩放，只解压条带并按调色板查色。

频道地址只来自频道表白名单，不录制流，不使用凭证。
"""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import adpcm, frames, perceptual, pts
from .timeline import (BASIS_COMMON_DECODE, ContentTimeline,
                       SessionClock, SourceState, VERDICT_IN_HOLE,
                       VERDICT_PLACED)
from .media import AUDIO_CHUNK_MS, FPS, default_palette
from .protocol import AUDIO_BYTES, AUDIO_PCM_BYTES, HEADER
# 控制包（CONFIG）字节上限，与固件 main/av_protocol.h 同步修改。
TV_CONTROL_MAX = 7168
# 频道数与频道 id 长度上限，与固件同步；设备会拒绝更长的列表，须在服务端先行限制。
TV_CHANNEL_MAX = 128
TV_CHANNEL_ID_MAX = 16
# 频道表单行的字节上限，仅用于限定 channels.txt 的读取量；留足余量以容纳四字段形式。
MAX_CHANNEL_LINE_BYTES = 512
# AUDIO_CHUNK_MS 取自 media.py，本模块不得重新定义。
AUDIO_RATE = 16000
# 音频发送端最多可领先实时多少毫秒，设备端队列深度为 AUDIO_LEAD_MS 加本值（见 CLAUDE.md 关键设计决策 5）。
# 深度须低于设备停止读取套接字的流控线，又要大于写出一个视频包的耗时，否则音频欠载；
# 没有设备反馈，只能留余量，不能填满。TV_AUDIO_LOOKAHEAD_MS 可覆盖。
AUDIO_MAX_LOOKAHEAD_MS = int(os.environ.get("TV_AUDIO_LOOKAHEAD_MS", "280"))
# 音频队列容量（块数）：HLS 源按分片突发送出数秒音频，队列需吸收突发并撑过分片间隔，
# 代价是起播落后直播边缘数秒。
PCM_QUEUE_CHUNKS = int(os.environ.get("TV_PCM_QUEUE_CHUNKS", "400"))  # 每块 40 ms，合 16 秒
# 画面队列容量，按内容时长折算为帧数。发送端取队首最旧帧，队列深度即画面落后直播的距离；
# 深度须与音频队列相当，单独压小只会使画面与声音失步（见 pop_video）。
VIDEO_QUEUE_SECONDS = float(os.environ.get("TV_VIDEO_QUEUE_S", "16"))

# 声音与画面的内容时间相差不超过该值（秒）即视为同一时刻；更旧的画面丢弃，更新的等待。
VIDEO_SYNC_TOLERANCE_S = float(os.environ.get("TV_SYNC_TOLERANCE_S", "0.15"))

VIDEO_QUEUE_FRAMES = int(os.environ.get("TV_VIDEO_QUEUE",
                                        str(int(VIDEO_QUEUE_SECONDS * FPS) + 32)))
# 首包发出前预缓冲的内容时长（秒）：它是换台的等待时间，也是应对源停供的唯一缓冲余量。
# TV_PREBUFFER_S 取值大则换台慢、更能容忍源停供。与两个队列容量同时封顶，
# 否则预缓冲永远达不到，会话以 PREBUFFER_TIMEOUT_S 结束。
PREBUFFER_SECONDS = min(float(os.environ.get("TV_PREBUFFER_S", "3")),
                        PCM_QUEUE_CHUNKS * AUDIO_CHUNK_MS / 1000,
                        VIDEO_QUEUE_FRAMES / FPS)
PREBUFFER_CHUNKS = int(PREBUFFER_SECONDS * 1000 / AUDIO_CHUNK_MS)
# 填满 PREBUFFER_SECONDS 所需的帧数，按 START_FPS 折算：预缓冲是时长，此时源帧率未知。
from .rate import START_FPS as _START_FPS, start_rate

PREBUFFER_FRAMES = int(PREBUFFER_SECONDS * _START_FPS)
# 仅为源始终不出数据时的兜底，正常起播由预缓冲目标决定。
PREBUFFER_TIMEOUT_S = 60

# 等待解码器回连两个回环套接字的时限（秒）。ffmpeg 启动后很快连接，超时说明进程没有起来，在 start() 报告。
DECODER_CONNECT_TIMEOUT_S = 10

# 频道表：TV_CHANNELS_FILE 指向文本文件，或在工作目录放 channels.txt，启动时读取一次。
# 每行 `id | 显示名 | https://... [| user-agent]`，空行与 '#' 注释行忽略。
CHANNELS_FILE = "channels.txt"
CHANNELS_ENV = "TV_CHANNELS_FILE"
# 内置兜底频道，取电视台自有的公开流。
BUILTIN_CHANNELS = {
    "cgtn": ("CGTN", "https://english-livebkali.cgtn.com/live/encgtn.m3u8"),
    "france24": ("France 24",
                 "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_500.m3u8"),
    "dw": ("DW English",
           "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/master.m3u8"),
    "tagesschau": ("Tagesschau24",
                   "https://tagesschau.akamaized.net/hls/live/2020115/tagesschau/tagesschau_1/master.m3u8"),
}

# 由 load_channels() 填充；服务端与 CONFIG 载荷直接读取这些模块级名称。
CHANNELS: dict[str, str] = {}
CHANNEL_LABELS: dict[str, str] = {}
# 频道 id 到 User-Agent 的映射。
CHANNEL_AGENTS: dict[str, str] = {}
DEFAULT_CHANNEL = ""


def parse_channels(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """解析频道表，拒绝设备会丢弃的条目。

    固件跳过空、不可打印或超长的 id，并只读取有限个频道（TV_CHANNEL_ID_MAX、TV_CHANNEL_MAX）；
    在此报错，避免频道在设备端悄悄消失。
    """
    channels: dict[str, str] = {}
    labels: dict[str, str] = {}
    agents: dict[str, str] = {}
    dropped = 0
    global CHANNEL_AGENTS
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        # 第四字段为该源要求的 User-Agent；部分镜像源只认特定播放器，其余返回 403。
        if len(parts) == 3:
            key, label, url = parts
            agent = ""
        elif len(parts) == 4:
            key, label, url, agent = parts
        else:
            raise ValueError(f"line {number}: expected 'id | name | url [| user-agent]', "
                             f"got {len(parts)} fields")
        if not key or len(key) >= TV_CHANNEL_ID_MAX:
            raise ValueError(f"line {number}: id must be 1..{TV_CHANNEL_ID_MAX - 1} characters")
        if not all("!" <= char <= "~" for char in key):
            raise ValueError(f"line {number}: id must be printable ASCII")
        if key in channels:
            raise ValueError(f"line {number}: duplicate id {key!r}")
        # 超出设备上限的频道只忽略并在末尾告警，不当作致命错误。
        if len(channels) >= TV_CHANNEL_MAX:
            dropped += 1
            continue
        if not label:
            raise ValueError(f"line {number}: missing display name")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"line {number}: url must start with http:// or https://")
        channels[key] = url
        labels[key] = label
        if agent:
            agents[key] = agent
        elif url.startswith(("http://", "https://")):
            agents[key] = frames.DEFAULT_USER_AGENT
    if not channels:
        raise ValueError("channel table contains no channels")
    # 数量合规但列表超过 CONFIG 包上限时，设备收不到任何频道（JSON 转义后每个汉字占 6 字节）；
    # 与数量超限一样只告警，不拒绝启动。
    packet = len(json.dumps({"channel_list": [{"id": k, "name": labels[k]}
                                              for k in channels]},
                            ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    if packet > TV_CONTROL_MAX or dropped:
        total = len(channels) + dropped
        # 数量超限与名称过长分别提示，避免误导操作者。
        if dropped:
            print(f"警告：channels.txt 有 {total} 个频道，设备最多接收 "
                  f"{TV_CHANNEL_MAX} 个。", flush=True)
            print(f"      超出的 {dropped} 个已被忽略，设备只会显示前 "
                  f"{len(channels)} 个。", flush=True)
        if packet > TV_CONTROL_MAX:
            if not dropped:
                print(f"警告：{total} 个频道，数量没有超，但频道名太长。", flush=True)
            print(f"      下发给设备的列表有 {packet} 字节，超过设备的 "
                  f"{TV_CONTROL_MAX} 字节上限，设备可能一个频道都收不到。", flush=True)
            print(f"      每个频道名平均 {packet // max(1, len(channels))} 字节，"
                  f"缩短频道名（尤其是中文名）是有效的办法。", flush=True)
        print("      请编辑 channels.txt。", flush=True)
    return channels, labels, agents


def load_channels(path: Path | None = None) -> None:
    """从文件（给定或查找到的）加载频道表，没有文件时用内置频道。

    导入时调用一次；main() 收到 --channels-file 时再调用。
    """
    global CHANNELS, CHANNEL_LABELS, CHANNEL_AGENTS, DEFAULT_CHANNEL
    source = path
    if source is None:
        environment = os.environ.get(CHANNELS_ENV)
        if environment:
            source = Path(environment)
        elif Path(CHANNELS_FILE).is_file():
            source = Path(CHANNELS_FILE)
        elif (Path(__file__).resolve().parent.parent / CHANNELS_FILE).is_file():
            source = Path(__file__).resolve().parent.parent / CHANNELS_FILE
    if source is None:
        channels = {key: url for key, (_, url) in BUILTIN_CHANNELS.items()}
        labels = {key: label for key, (label, _) in BUILTIN_CHANNELS.items()}
        agents = {}
    else:
        if not source.is_file():
            raise ValueError(f"channel file not found: {source}")
        # 读取上限由频道数和单行上限决定，不用 TV_CONTROL_MAX 或 VIDEO_MAX（那是单包线路上限）：
        # 合法文件不会被截断，超大文件在已知大小处停止。
        limit = TV_CHANNEL_MAX * MAX_CHANNEL_LINE_BYTES
        with source.open(encoding="utf-8", errors="strict") as handle:
            text = handle.read(limit + 1)
        if len(text) > limit:
            raise ValueError(f"{source} is larger than {limit} bytes; the device "
                             f"cannot hold more than {TV_CHANNEL_MAX} channels")
        channels, labels, agents = parse_channels(text)
    CHANNELS.clear()
    CHANNELS.update(channels)
    CHANNEL_LABELS.clear()
    CHANNEL_LABELS.update(labels)
    CHANNEL_AGENTS.clear()
    CHANNEL_AGENTS.update(agents)
    # 原默认频道仍在表中则保留，否则取表首项；默认频道不在表内会使未指定频道的连接全部失败。
    if DEFAULT_CHANNEL not in channels:
        DEFAULT_CHANNEL = next(iter(channels))


def channel_list() -> list[dict]:
    return [{"id": key, "name": CHANNEL_LABELS.get(key, key)} for key in CHANNELS]


load_channels()


class LiveError(RuntimeError):
    """转码或切包失败。"""



# 每个设备音频块的采样数；ffmpeg 的音频帧长固定为此值，使一行 `ashowinfo` 恰好对应一块。
AUDIO_SAMPLES = AUDIO_PCM_BYTES // 2


def read_frames(stream, on_frame, stop) -> None:
    """Read indexed frames from a raw pipe, one at a time."""
    while not stop.is_set():
        frame = frames.read_frame(stream, stop)
        if frame is None:
            raise LiveError("transcode video pipe closed")
        on_frame(frame)


def chunk_pcm(stream, on_audio, stop) -> None:
    """Re-block the PCM pipe into exact device chunks (1280 bytes / 40 ms)."""
    buffer = bytearray()
    while not stop.is_set():
        chunk = stream.read(AUDIO_PCM_BYTES * 8)
        if not chunk:
            raise LiveError("transcode audio pipe closed")
        buffer.extend(chunk)
        while len(buffer) >= AUDIO_PCM_BYTES:
            block = bytes(buffer[:AUDIO_PCM_BYTES])
            del buffer[:AUDIO_PCM_BYTES]
            on_audio(block)

# 追加在 frames.FIT 之后、format=rgb8 之前的 ffmpeg 视频滤镜，默认为空。
PRE_FILTER = os.environ.get("TV_PRE_FILTER", "").strip()
# 帧超出字节目标时的降质方式：perceptual.MODES 中的模式（需 numpy），或 "ladder"（逐档降色，见 frames.encode_within）。
# 放得下的帧原样发送。
DEGRADE_MODES = (*perceptual.MODES, "ladder")
_DEFAULT_DEGRADE = "perceptual" if perceptual.AVAILABLE else "ladder"
DEGRADE = os.environ.get("TV_DEGRADE", "").strip() or _DEFAULT_DEGRADE
if DEGRADE not in DEGRADE_MODES:
    raise SystemExit(f"TV_DEGRADE={DEGRADE!r} 无效，可选值：{' / '.join(DEGRADE_MODES)}")
if DEGRADE in perceptual.MODES and not perceptual.AVAILABLE:
    raise SystemExit(f"TV_DEGRADE={DEGRADE} 需要 numpy，请先安装（pip install numpy）")
_VIDEO_CHAIN = f"{frames.FIT},{PRE_FILTER}" if PRE_FILTER else frames.FIT
# 帧放入字节目标后剩余字节的用法：delta 只发变化超过 TV_DELTA_MAX_DIFF 的条带，
# fill 另按变化量从大到小补发被跳过的条带。
ENCODE_MODES = ("delta", "fill")
ENCODE = os.environ.get("TV_ENCODE", "").strip() or "fill"
if ENCODE not in ENCODE_MODES:
    raise SystemExit(f"TV_ENCODE={ENCODE!r} 无效，可选值：{' / '.join(ENCODE_MODES)}")
def source_graph(fps: int) -> str:
    """单个滤镜图，两路输出画面与音频。

    format=rgb8 在 showinfo 之前，使记录的帧即写出的帧；aformat=mono 须在 asetnsamples 之前，
    否则立体声帧含两个设备块，一行 ashowinfo 对应两块，时间戳与载荷错位。
    """
    return (
        f"[0:v]setpts=PTS-STARTPTS,fps={fps},{_VIDEO_CHAIN},format=rgb8,showinfo[v];"
        f"[0:a]asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},aformat=channel_layouts=mono,"
        f"asetnsamples=n={AUDIO_SAMPLES},ashowinfo[a]"
    )



def source_command(url: str, video_port: int, audio_port: int, ffmpeg: str,
                   user_agent: str = "", fps: int = FPS) -> list[str]:
    """构造 ffmpeg 命令：读取源一次，画面与音频经两个回环 TCP 端口输出，时间戳由 showinfo 日志给出。

    画面量化到 ffmpeg 固定 3-3-2 调色板（与固件 av_palette_rgb565 一致），关闭抖动（见 CLAUDE.md 关键设计决策 1）；
    `-sws_dither none` 须作为输出选项，放在 `-i` 之前会被忽略。
    `-loglevel info` 不可去掉，showinfo 在 INFO 级别输出 pts；`-nostats` 避免进度行混入同一路输出。
    输出走 `tcp://127.0.0.1:<port>` 而非管道（见 CLAUDE.md 关键设计决策 7），ffmpeg 为客户端，调用前父进程须已监听两个端口。
    """
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
        *frames.input_options(url, user_agent, paced=True),
        "-i", url,
        "-filter_complex", source_graph(fps),
        "-map", "[v]", "-pix_fmt", "rgb8", "-sws_dither", "none",
        "-threads", "1", "-f", "rawvideo", f"tcp://127.0.0.1:{video_port}",
        "-map", "[a]", "-f", "s16le", f"tcp://127.0.0.1:{audio_port}",
    ]



# 画面产出帧率上限，即设备的 fps 上限（main/av_player.c，1..30）；更高的源降到它之下，不拒绝。
MAX_SOURCE_FPS = 30
# ffprobe 探测帧率的时限（秒）：位于每次换台首帧之前，超时则按名义帧率播放。
FPS_PROBE_TIMEOUT_S = float(os.environ.get("TV_FPS_PROBE_TIMEOUT_S", "5"))
_FPS_CACHE: dict[str, int] = {}


def parse_rate(text: str) -> float | None:
    """解析 ffprobe 的帧率文本（如 "25/1"），无法确定时返回 None。

    "0/0" 表示未知，在直播 HLS 上常见，按 None 处理而非报错。
    """
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def choose_fps(avg: float | None, real: float | None) -> int | None:
    """由 ffprobe 报告的帧率选出产出帧率（整数）。

    优先用平均帧率：25 fps 节目按 50 场承载时，r_frame_rate 为 50，avg_frame_rate 为 25。
    只有容器帧率且超过上限时逐次减半，不截断到上限。
    """
    rate = avg if avg else real
    if not rate:
        return None
    while rate > MAX_SOURCE_FPS:
        rate /= 2
    return max(1, round(rate))


def probe_source_fps(url: str, ffmpeg: str, user_agent: str = "") -> int | None:
    """探测源的画面帧率，未知返回 None（按名义帧率播放）；结果按 URL 缓存。"""
    if url in _FPS_CACHE:
        return _FPS_CACHE[url]
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(probe):
        probe = "ffprobe"
    command = [probe, "-v", "error"]
    effective_ua = user_agent or (frames.DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
    if effective_ua:
        command += ["-user_agent", effective_ua]
    command += ["-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=FPS_PROBE_TIMEOUT_S)
        if done.returncode != 0:
            return None
        streams = json.loads(done.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not streams:
        return None
    fps = choose_fps(parse_rate(streams[0].get("avg_frame_rate", "")),
                     parse_rate(streams[0].get("r_frame_rate", "")))
    if fps is not None:
        _FPS_CACHE[url] = fps
    return fps


class _QueuedFrame(list):
    """一帧的包列表，附带切包前的原始帧（TV_DELTA）。"""
    raw: bytes | None = None


class LiveChannel:
    """单个频道会话：调色板、解码器进程，以及它与发送端之间的有界队列。"""

    def __init__(self, url: str, ffmpeg: str = "ffmpeg", user_agent: str = ""):
        if url not in CHANNELS.values():
            raise ValueError("channel URL is not in the local allowlist")
        self.url = url
        self.ffmpeg = ffmpeg
        self.user_agent = user_agent
        self.stop = threading.Event()
        self.audio = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        self.video = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        # 已发给设备的画面与强制刷新条带的轮转计数（见 frames.choose_stripes）；
        # 每个频道独立，新频道从空白面板开始。未启用增量时不用。
        self._shown: bytes | None = None
        self._delta_tick = 0
        self._snap_level = 0
        # 内容时间轴：对齐依据解码器 pts 而非到达时间（见 server/timeline.py）。
        self.timeline = ContentTimeline()
        # 内容时间映射到本会话的线上时钟；每个频道一份，设备要求每个会话的音频从零开始。
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        self.video_content: collections.deque = collections.deque(maxlen=VIDEO_QUEUE_FRAMES)
        self.audio_content: collections.deque = collections.deque(maxlen=PCM_QUEUE_CHUNKS)
        # 源是否仍在产出，与链路是否跟得上区分开；见 SourceState。
        self.source = SourceState()
        self._video_advanced = False
        self._audio_advanced = False
        # 画面帧率，会话内固定；start() 探测前为名义值 FPS，见 resolve_fps。
        self.fps = FPS
        self.fps_note = ""
        # 画面字节率，由发送端控制器设置；每帧压到 rate / fps 以内，见 frames.ByteBudget。
        self._budget = frames.ByteBudget(start_rate(FPS), FPS)
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.dropped_video = 0
        self.skipped_audio = 0
        self.produced_audio = 0
        self.produced_video = 0
        self.encoded_video_bytes = 0
        self.encode_seconds = 0.0
        self.coarse_frames = 0
        #: 唯一的解码器进程，同时产出两路载荷与时间戳。
        self.decoder: subprocess.Popen | None = None
        self.timestamps: pts.Timestamps | None = None
        self.pts_reader: pts.Reader | None = None
        self.threads: list[threading.Thread] = []
        # 解码器回连的两个回环连接：文件对象用于读取，与底层套接字一并保存，使 close() 能确定性释放。
        self._video: object | None = None
        self._audio: object | None = None
        self._video_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._adpcm = adpcm.Encoder()
        self._raw_video: queue.Queue[bytes | None] = queue.Queue(maxsize=120)
        self._raw_audio: queue.Queue[bytes | None] = queue.Queue(maxsize=300)
        # 设备绘制前需要的调色板，由 build_palette() 填充，会话在首帧前发送。
        self.palette: bytes | None = None



    def calibrate_from_source(self) -> bool:
        """记录两路内容时钟的关联。

        单个解码器产出两路时间戳，二者已在同一时钟上，偏移恒为零；仍调用是为了记录关联依据
        （BASIS_COMMON_DECODE），无法说明时间来源的会话不应被接受。
        """
        return self.timeline.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)

    def build_palette(self) -> bytes:
        """固定 3-3-2 调色板，不按源取样（见 CLAUDE.md 关键设计决策 1）。"""
        self.palette = default_palette()
        return self.palette

    def _accept_decoder(self, listener: socket.socket) -> socket.socket:
        """接受解码器的连接；解码器已退出（如源打不开）则立即失败，不等满超时。"""
        deadline = time.monotonic() + DECODER_CONNECT_TIMEOUT_S
        listener.settimeout(0.1)
        while True:
            try:
                return listener.accept()[0]
            except socket.timeout:
                pass
            if self.decoder is not None and self.decoder.poll() is not None:
                raise ConnectionAbortedError("decoder exited")
            if time.monotonic() > deadline:
                raise TimeoutError("decoder connect timeout")

    def start(self) -> None:
        if self.palette is None:
            raise LiveError("start() before build_palette()")
        self.resolve_fps()
        # 画面与音频来自单个解码器，经两个回环 TCP 连接输出（原因见 source_command）。
        # 父进程先监听两个端口再启动 ffmpeg，由 ffmpeg 作为客户端回连；
        # 失败时须关闭全部资源，否则设备反复重连会耗尽套接字。
        video_listener = audio_listener = None
        video_sock = audio_sock = None
        success = False
        try:
            video_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            video_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            video_listener.bind(("127.0.0.1", 0))
            video_listener.listen(1)
            video_port = video_listener.getsockname()[1]

            audio_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            audio_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            audio_listener.bind(("127.0.0.1", 0))
            audio_listener.listen(1)
            audio_port = audio_listener.getsockname()[1]

            # stderr 用管道承载时间戳，由专门线程持续读取：管道无人读时 ffmpeg 会阻塞，与源停供无法区分。
            self.timestamps = pts.Timestamps()
            self.decoder = subprocess.Popen(
                source_command(self.url, video_port, audio_port, self.ffmpeg,
                               self.user_agent, self.fps),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
            self.pts_reader = pts.Reader(self.decoder.stderr, self.timestamps)
            self.pts_reader.start()
            self.calibrate_from_source()

            # ffmpeg 在解码任何内容之前就会打开两路输出，此处超时说明解码器没有起来，而非频道慢。
            try:
                video_sock = self._accept_decoder(video_listener)
                audio_sock = self._accept_decoder(audio_listener)
            except OSError as error:
                raise LiveError(
                    f"decoder did not connect: {type(error).__name__}") from None
            # 监听套接字已完成任务，关闭。
            video_listener.close()
            audio_listener.close()
            video_listener = audio_listener = None

            # 关闭 Nagle 算法：小写入最多被攒 40 ms，恰为一个音频块长，会带来发送端看不到的随机延迟。
            for sock in (video_sock, audio_sock):
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            # 加大视频接收缓冲（尽力而为）：部分平台默认值容不下一帧原始画面；设置失败只是突发时余量小。
            try:
                video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
            except OSError:
                pass

            self._video_sock = video_sock
            self._audio_sock = audio_sock
            self._video = video_sock.makefile("rb", buffering=0)
            self._audio = audio_sock.makefile("rb", buffering=0)

            self.threads = [
                threading.Thread(target=self._drain_video, daemon=True, name="live-video-drain"),
                threading.Thread(target=self._process_video, daemon=True, name="live-video-process"),
                threading.Thread(target=self._drain_audio, daemon=True, name="live-audio-drain"),
                threading.Thread(target=self._process_audio, daemon=True, name="live-audio-process"),
            ]
            for thread in self.threads:
                thread.start()
            success = True
        finally:
            for listener in (video_listener, audio_listener):
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        pass
            if not success:
                for sock in (video_sock, audio_sock):
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                self.close()

    def _note(self, error: Exception) -> None:
        with self.lock:
            if self.error is None:
                self.error = error
        self.stop.set()

    def _decoder_time(self, kind: str) -> float:
        """取刚读到的条目对应的解码器时间戳（毫秒）。

        取不到时抛错，不用替代时钟：时间戳必须来自解码器 pts（CLAUDE.md 核心设计原则 5）。
        """
        seconds = self.timestamps.take(kind) if self.timestamps else None
        if seconds is None:
            if self.stop.is_set():
                return 0.0
            raise LiveError(
                f"the decoder produced a {kind} item without logging its "
                f"timestamp; the session cannot be timed and is being ended")
        return seconds * 1000.0

    def _drain_video(self) -> None:
        """立即把 ffmpeg 画面输出读进内存队列。

        读取与等待时间戳日志解耦：读取一旦停顿，ffmpeg 的写入阻塞，整个转码随之冻结。
        """
        try:
            while not self.stop.is_set():
                frame = frames.read_frame(self._video, self.stop)
                if frame is None:
                    if not self.stop.is_set():
                        self._raw_video.put(None)
                    break
                while not self.stop.is_set():
                    try:
                        self._raw_video.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError) as error:
            self._note(error)

    def _process_video(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    frame = self._raw_video.get(timeout=0.1)
                except queue.Empty:
                    continue
                if frame is None:
                    if self.stop.is_set():
                        break
                    raise LiveError("transcode video pipe closed")
                self._push_video(frame, self._decoder_time("video"))
        except (LiveError, OSError, ValueError, pts.Misaligned) as error:
            self._note(error)

    def _drain_audio(self) -> None:
        """立即把 ffmpeg 音频输出读进内存队列。"""
        try:
            while not self.stop.is_set():
                block = frames.read_exactly(self._audio, AUDIO_PCM_BYTES)
                if len(block) != AUDIO_PCM_BYTES:
                    if not self.stop.is_set():
                        self._raw_audio.put(None)
                    break
                while not self.stop.is_set():
                    try:
                        self._raw_audio.put(block, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError) as error:
            self._note(error)

    def _process_audio(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    block = self._raw_audio.get(timeout=0.1)
                except queue.Empty:
                    continue
                if block is None:
                    if self.stop.is_set():
                        break
                    raise LiveError("transcode audio pipe closed")
                self._push_audio(self._adpcm.encode_pcm(block), self._decoder_time("audio"))
        except (LiveError, OSError, ValueError, pts.Misaligned) as error:
            self._note(error)



    def _push_video(self, frame: bytes, content_ms: float) -> None:
        """入队一帧；`content_ms` 为解码器给出的该帧时间戳，随载荷入队，不重算。

        整帧处理完才入队，发送端不会看到半幅画面；未启用增量时在此压缩，以免占用发送线程。
        """
        encode_started = time.monotonic()
        if frames.DELTA:
            # 增量模式只带原始帧入队，发送时再按已发内容与剩余字节预算选条带；
            # 在此决定会以可能被队列丢弃的帧为基准。
            packets = _QueuedFrame()
            packets.raw = frame
        else:
            packets = frames.frame_packets(frame)
        with self.lock:
            self.produced_video = getattr(self, "produced_video", 0) + 1
            self.encoded_video_bytes = getattr(self, "encoded_video_bytes", 0) + sum(map(len, packets))
            self.encode_seconds = getattr(self, "encode_seconds", 0.0) + time.monotonic() - encode_started
            # 队列满时丢弃最旧帧。判断依据是队列深度而非帧间隔：源慢于目标时没有多余帧，不得限流。
            if len(self.video) == self.video.maxlen:
                self.video.popleft()
                self.dropped_video += 1
            self.video.append(packets)
            self.video_content.append(content_ms)
            self._video_advanced = True

    def _push_audio(self, block: bytes, content_ms: float) -> None:
        """入队一个音频块；`content_ms` 为解码器时间戳，而非块计数（计数反映不出解码器跳过的音频）。"""
        with self.lock:
            if len(self.audio) == self.audio.maxlen:
                self.audio.popleft()
                if self.audio_content:
                    self.audio_content.popleft()
                self.skipped_audio += 1
            self.audio.append(block)
            self.produced_audio += 1
            self.audio_content.append(content_ms)
            self._audio_advanced = True

    def pop_audio(self) -> tuple[bytes, int] | None:
        """取下一个音频块及其发送用的会话时间戳。

        设备要求每块时间戳恰为上一块加一个块长，故块的会话位置即其序号；
        内容时间交给 `SessionClock.audio` 检测并度量源的不连续。画面时间戳不得由该序号派生。
        """
        with self.lock:
            if not self.audio or not self.audio_content:
                # 两个队列同进同出，不会出现载荷非空而内容时间为空；出现时拒绝，不用计数代替时间戳。
                return None
            content = self.audio_content.popleft()
            return self.audio.popleft(), self.session_clock.audio(content)

    def picture_lag_s(self) -> float:
        """画面队首落后于声音队首的秒数，正值表示画面落后。

        两者均为内容时间；差值反映两个队列的深度差，不是观众感受到的唇音偏差。
        """
        with self.lock:
            if not self.video_content or not self.audio_content:
                return 0.0
            if not self.timeline.calibrated:
                return 0.0
            sound_at = self.timeline.audio_in_video_units(self.audio_content[0])
            return (self.video_content[0] - sound_at) / 1000.0


    def set_video_rate(self, rate: int) -> None:
        """设置画面每秒字节预算，由发送端每秒调用一次；帧率不可调，慢链路只能牺牲细节。"""
        with self.lock:
            self._budget.set_rate(rate)

    def resolve_fps(self) -> int:
        """在产出任何内容前确定一次画面帧率。

        显式设置 `TV_FPS` 时直接采用，否则探测源帧率，探测不到用名义值；
        该值同时用于 ffmpeg 产出与发送节拍。
        """
        if "TV_FPS" in os.environ:
            fps, note = FPS, "TV_FPS"
        else:
            probed = probe_source_fps(self.url, self.ffmpeg, self.user_agent)
            fps, note = (probed, "probed") if probed else (FPS, "nominal; source did not say")
        self.fps, self.fps_note = fps, note
        self.timeline = ContentTimeline(video_interval_ms=1000.0 / fps,
                                        audio_interval_ms=AUDIO_CHUNK_MS)
        with self.lock:
            self._budget.set_fps(fps)
            self._budget.set_rate(start_rate(fps))
        return fps

    def pop_video(self, keep: int = 0) -> tuple[list[bytes], int] | None:
        """取与即将发送的声音内容对齐的画面帧，返回其包与会话时间戳，没有则返回 None。

        按内容时间对齐，不按到达时间（CLAUDE.md 核心设计原则 5）：内容比声音旧的帧已被听过，丢弃；
        更新的帧未到时刻，返回 None 等待，以声音为时间主线。帧的会话时间戳由帧自身的内容位置决定，
        与配对的声音位置无关，包与时间戳在此一并决定，发送端不另行派生。
        """
        with self.lock:
            if not self.video:
                return None
            if not self.audio_content and self.session_clock._prev_audio_content is None:
                return None
            if not self.timeline.usable:
                # 两路时钟尚未建立关联时无从判断帧与块是否同一时刻，不猜测。
                # 判据是 usable 而非 calibrated：近似映射可用于配对，只是不能当作测量值上报。
                return None
            # 声音所属的节目时刻，换算到画面时钟以便直接比较。
            ref_audio = self.audio_content[0] if self.audio_content else self.session_clock._prev_audio_content
            head_audio = self.timeline.audio_in_video_units(ref_audio)
            # 不可放置的帧丢弃后继续试下一帧；每次只处理一帧会使洞内的连续帧要多次调用才能清完。
            while self.video and self.video_content:
                content = self.video_content[0]
                # 比该声音旧的内容观众已听过，丢弃。
                if content < head_audio - VIDEO_SYNC_TOLERANCE_S * 1000:
                    self._give_up_frame()
                    continue
                # 未到时刻：与该声音匹配的帧尚未到达。
                if content > head_audio + VIDEO_SYNC_TOLERANCE_S * 1000:
                    return None
                # 线上时间戳取帧自身的内容位置，不取所配对声音的位置。
                frame_at = self.timeline.video_in_audio_units(content)
                # 同时传入队首内容时间，时钟才能在取走后续声音之前发现已出现空洞。
                verdict, stamp = self.session_clock.placement(
                    frame_at, ref_audio)
                if verdict == VERDICT_IN_HOLE:
                    # 发送端丢弃了这段声音，观众没听过，没有可绘制该帧的时刻。
                    self._give_up_frame()
                    continue
                if verdict != VERDICT_PLACED:
                    # 未到时刻或会话尚无锚点，都是等待而非丢失，帧保留在队列中。
                    return None
                self.video_content.popleft()
                packets = self._delta_encode(self.video.popleft())
                self.session_clock.note_pair(frame_at, ref_audio)
                return packets, stamp
            return None

    def _delta_encode(self, packets):
        """把队列中的原始帧转成线上包。

        发送时持锁执行：选条带依赖已发内容与剩余字节预算，二者此时才确定；未启用增量时包已压缩，原样返回。
        """
        raw = getattr(packets, "raw", None)
        if raw is None:
            return packets
        started = time.monotonic()
        if DEGRADE in perceptual.MODES:
            chosen, drawn, rung = perceptual.encode_within(
                raw, self._shown, self._delta_tick, self._budget.target(), self._snap_level,
                mode=DEGRADE)
            self._snap_level = rung
        else:
            chosen, drawn, rung = frames.encode_within(raw, self._shown, self._delta_tick,
                                                       self._budget.target())
        if ENCODE == "fill":
            chosen = frames.fill_stripes(drawn, self._shown, chosen, self._budget.target())
        self._delta_tick += 1
        self._shown = frames.apply_stripes(drawn, self._shown, chosen)
        out = frames.pack_stripes(chosen)
        wire = sum(len(part) + HEADER.size for part in out)
        self.coarse_frames += rung > 0
        self.encoded_video_bytes += wire
        self.encode_seconds += time.monotonic() - started
        return out

    def _give_up_frame(self) -> None:
        """丢弃画面队首帧；调用者持锁。"""
        self.video.popleft()
        self.video_content.popleft()
        self.dropped_video += 1

    def source_state(self, queue_over_bound: bool = False) -> str:
        """给刚结束的窗口分类并清除窗口标志，由发送端在通知控制器前调用一次。

        标志按窗口消费，避免陈旧的 True 使空闲窗口显得繁忙；`starved` 使空窗口不被当作富余容量，
        `congested` 才是速率调整要应对的情形。
        """
        with self.lock:
            state = self.source.observe(self._video_advanced, self._audio_advanced,
                                        queue_over_bound)
            self._video_advanced = False
            self._audio_advanced = False
            return state

    @property
    def shown(self) -> bytes | None:
        """设备当前显示的画面，首帧前为 None。"""
        with self.lock:
            return self._shown

    @shown.setter
    def shown(self, picture: bytes | None) -> None:
        with self.lock:
            self._shown = picture

    def has_data(self) -> bool:
        with self.lock:
            # 声音队列有数据，或声音已锚定且画面队列有数据，即可配对。
            return bool(self.audio or (self.video and self.session_clock._prev_audio_content is not None))

    def audio_pending(self) -> bool:
        with self.lock:
            return bool(self.audio)

    def video_pending(self) -> bool:
        with self.lock:
            return bool(self.video)

    def failure(self) -> Exception | None:
        with self.lock:
            return self.error

    def prebuffered(self) -> bool:
        with self.lock:
            return len(self.audio) >= PREBUFFER_CHUNKS and len(self.video) >= PREBUFFER_FRAMES

    def trim_backlog(self, maximum_ms: int = 8000, retain_ms: int = 4000) -> int:
        if not 0 < retain_ms < maximum_ms:
            raise ValueError("invalid live backlog bounds")
        with self.lock:
            if len(self.audio) * AUDIO_CHUNK_MS <= maximum_ms:
                return 0
            count = len(self.audio) - max(1, retain_ms // AUDIO_CHUNK_MS)
            for _ in range(count):
                self.audio.popleft()
                if self.audio_content:
                    self.audio_content.popleft()
            self.skipped_audio = getattr(self, "skipped_audio", 0) + count
            if self.audio_content and getattr(self.timeline, "usable", False):
                edge = self.timeline.audio_in_video_units(self.audio_content[0])
                while self.video_content and self.video_content[0] < edge:
                    self._give_up_frame()
            elif self.audio_content and self.video_content:
                edge = self.audio_content[0]
                while self.video_content and self.video_content[0] < edge:
                    self._give_up_frame()
            return count

    @staticmethod
    def has_media_start_times(probe_note: list) -> bool:
        return any("reports no start_time" in note for note in probe_note)


    @staticmethod
    def _sanitize_diagnostics(text: str) -> str:
        """去除诊断文本中的令牌、密码与敏感参数。"""
        # 抹去 URL 查询参数（如 ?token=...）
        text = re.sub(r"([?&][a-zA-Z0-9_.-]+=)[^\s&'\"<>]+", r"\1<redacted>", text)
        # 抹去 URL 中的 user:pass
        text = re.sub(r"(https?://)([^:@\s/]+:[^:@\s/]+@)", r"\1<auth>@", text)
        return text


    def diagnostics(self) -> str:
        """转码错误文本，用于日志：只含 ffmpeg stderr，不含媒体内容。"""
        parts = []
        error = self.failure()
        if error is not None:
            parts.append(type(error).__name__)
        proc = getattr(self, "decoder", None)
        if proc is not None and proc.poll() is not None:
            parts.append(f"decoder_exit={proc.returncode}")
        pts_reader = getattr(self, "pts_reader", None)
        if pts_reader is not None and pts_reader.error is not None:
            parts.append(f"pts_reader_error={type(pts_reader.error).__name__}")
        ts = getattr(self, "timestamps", None)
        if ts is not None and ts.tail:
            tail_lines = [line.decode("utf-8", errors="replace").strip() for line in ts.tail]
            tail_text = " ".join(line for line in tail_lines if line)
            if tail_text:
                tail_text = self._sanitize_diagnostics(tail_text)
                parts.append(f"decoder: {tail_text.replace(chr(10), ' ')[-300:]}")
        return " | ".join(parts)


    def close(self) -> None:
        self.stop.set()
        ts = getattr(self, "timestamps", None)
        if ts is not None:
            try:
                ts.close()
            except Exception:
                pass
        # 先关文件对象，使读线程见到 EOF，再显式关套接字：只关文件对象不会释放 fd，要等垃圾回收。
        for stream in (getattr(self, "_video", None), getattr(self, "_audio", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._video = None
        self._audio = None
        for sock in (getattr(self, "_video_sock", None), getattr(self, "_audio_sock", None)):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._video_sock = None
        self._audio_sock = None
        proc = getattr(self, "decoder", None)
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except OSError:
                    pass
            except OSError:
                pass
        all_threads = list(getattr(self, "threads", []))
        pts_reader = getattr(self, "pts_reader", None)
        if pts_reader is not None:
            all_threads.append(pts_reader)
        for thread in all_threads:
            if thread.is_alive():
                try:
                    thread.join(timeout=1.0)
                except Exception:
                    pass
        if proc is not None and getattr(proc, "stderr", None) is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass


PROBE_TIMEOUT_S = float(os.environ.get("TV_PROBE_TIMEOUT_S", "20"))


def stream_start_times(url: str, ffmpeg: str, user_agent: str = "",
                       diagnostic: list | None = None) -> tuple[float, float] | None:
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(probe):
        probe = "ffprobe"

    def failed(reason: str) -> None:
        if diagnostic is not None:
            diagnostic.append(reason)

    command = [probe, "-v", "error"]
    effective_ua = user_agent or (frames.DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
    if effective_ua:
        command += ["-user_agent", effective_ua]
    command += ["-show_entries", "stream=codec_type,start_time",
                "-of", "json", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        failed("ffprobe is not installed")
        return None
    except subprocess.TimeoutExpired:
        failed(f"ffprobe timed out after {PROBE_TIMEOUT_S:g} s")
        return None
    except OSError as error:
        failed(f"ffprobe could not be run: {type(error).__name__}")
        return None
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        failed("ffprobe failed: " + (first[0] if first else f"exit {done.returncode}"))
        return None
    try:
        streams = json.loads(done.stdout).get("streams", [])
    except ValueError:
        failed("ffprobe returned something that is not JSON")
        return None
    found: dict[str, float] = {}
    for stream in streams:
        kind = stream.get("codec_type")
        if kind not in ("video", "audio") or kind in found:
            continue
        try:
            found[kind] = float(stream["start_time"])
        except (KeyError, TypeError, ValueError):
            failed(f"the source reports no start_time for its {kind} stream")
            return None
    if "video" not in found or "audio" not in found:
        failed("the source does not report both a video and an audio stream")
        return None
    return found["video"], found["audio"]

