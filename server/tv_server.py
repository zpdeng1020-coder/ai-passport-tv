"""面向直播频道的单客户端局域网服务器，仅依赖标准库，由 ffmpeg 转码。"""

from __future__ import annotations

import argparse
import errno
import hmac
import ipaddress
import os
import secrets
import select
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import enum
from typing import Any, Dict, List

class SessionState(str, enum.Enum):
    PLAYING = "PLAYING"
    STARVED_REBUFFERING = "STARVED_REBUFFERING"
    RECOVERING = "RECOVERING"
    CLOSED = "CLOSED"

from . import frames, netident
from .timeline import SourceState
from .rate import ADAPTIVE, ByteRate
from .live import (CHANNELS, CHANNEL_AGENTS, DEFAULT_CHANNEL, LiveChannel, LiveError,
                   AUDIO_MAX_LOOKAHEAD_MS, PREBUFFER_TIMEOUT_S, channel_list)
# 时序常量统一来自 media.py，live.py 同样从那里导入。
from .media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, DURATION_MS, FPS,
                    START_DELAY_MS, VIDEO_LEAD_MS, schedule)
from .protocol import (AUDIO_PCM_BYTES, IO_TIMEOUT, VIDEO_CONTINUES, Kind, Packet,
                       ProtocolError, json_bytes, json_object, receive_packet,
                       send_packet, _write_slice)
from . import reconnect

# 替代 --token-file 的环境变量名；只有令牌值需与固件一致，变量名无需一致。
TOKEN_ENV = "TV_PAIRING_TOKEN"
# 取值均为发送端实际使用的常量，不另存副本。
CONFIG = {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": FPS,
          "sample_rate": 16000,
          "channels": 1, "sample_bits": 16, "audio_codec": "ima_adpcm",
          "audio_chunk_ms": AUDIO_CHUNK_MS,
          "video_max_bytes": frames.VIDEO_MAX,
          # 设备据此校验自身几何；两端不一致时拒绝通信，避免条带画到错误的行。
          "stripe_rows": frames.STRIPE_ROWS,
          "duration_ms": DURATION_MS,
          "start_delay_ms": START_DELAY_MS, "audio_lead_ms": AUDIO_LEAD_MS,
          "video_lead_ms": VIDEO_LEAD_MS}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))

# 单帧写入时限（秒），超时即放弃会话；须小于设备约 3 秒无数据即断开的阈值。
FRAME_WRITE_DEADLINE_S = float(os.environ.get("TV_FRAME_DEADLINE_S", "2.5"))

# 套接字发送缓冲区大小；见 CLAUDE.md 关键设计决策 4。
SEND_BUFFER_BYTES = int(os.environ.get("TV_SEND_BUFFER", "16384"))

# 画面包每次写入的分片大小；分片之间让音频先发，见 CLAUDE.md 核心设计原则 2。
VIDEO_SLICE_BYTES = 4096

# 单次写入超过该秒数时记录其大小与所在位置；0 表示每次都记录。
PROBE_SLOW_S = float(os.environ.get("TV_PROBE_SLOW_S", "0.05"))

# 新会话等待首批媒体的时长，超时后设备停留在重连提示画面。
STARTUP_WAIT_S = min(PREBUFFER_TIMEOUT_S, 10)


WILDCARD_BIND = "0.0.0.0"


def local_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return any(ip in network for network in PRIVATE_NETWORKS)


def validate_token(token: str) -> bytes:
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    if any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    return token.encode("ascii")


def load_token(path: Path | None = None) -> bytes | None:
    """返回配对令牌；未配置时返回 None（不校验 HELLO 内容，由调用方提示）。"""
    environment = os.environ.get(TOKEN_ENV)
    if path is not None and environment is not None:
        raise ValueError("choose either token environment or token file")
    if path is None:
        if environment is None:
            return None
        return validate_token(environment)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ValueError("token file must be owner-only, regular, mode 0400 or 0600")
        raw = stream.read(130)
    # 允许一个末尾 LF，读取长度有上限。
    try:
        return validate_token(raw.removesuffix(b"\n").decode("ascii"))
    except UnicodeError:
        raise ValueError("token file must contain printable ASCII") from None


def authenticate(packet: Packet, token: bytes | None) -> None:
    """校验首个 HELLO；配置了令牌时同时校验令牌。"""
    if (packet.kind != Kind.HELLO or packet.session != 0 or
            packet.seq != 0 or packet.pts_ms != 0):
        raise ProtocolError("expected initial HELLO")
    hello = json_object(packet.payload)
    if type(hello.get("version")) is not int or hello["version"] != 1:
        raise ProtocolError("unsupported HELLO version")
    if token is None:
        # 未配置令牌：不检查 HELLO 内容，但上面已校验 HELLO 形状。
        return
    supplied = hello.get("token")
    if not isinstance(supplied, str):
        raise ProtocolError("authentication failed")
    try:
        supplied_bytes = validate_token(supplied)
    except ValueError:
        raise ProtocolError("authentication failed") from None
    if not hmac.compare_digest(supplied_bytes, token):
        raise ProtocolError("authentication failed")


class _MediaChannel:
    def __init__(self, media, *args, **kwargs):
        from .timeline import ContentTimeline, SessionClock, BASIS_COMMON_DECODE
        self.media = media
        self.palette = media.palette if hasattr(media, "palette") else bytes(768)
        self.fps = FPS
        self.url = ""
        self.timeline = ContentTimeline()
        self.timeline.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)
        self.failed = False
        self.dropped_video = 0
        self.skipped_audio = 0
        self.audio = [media.pcm[i:i+AUDIO_PCM_BYTES] for i in range(0, len(media.pcm), AUDIO_PCM_BYTES)] if hasattr(media, "pcm") else []
        self.video = list(media.frames) if hasattr(media, "frames") else []
        self.audio_content = [i * AUDIO_CHUNK_MS for i in range(len(self.audio))]
        self.video_content = [i * (1000.0 / FPS) for i in range(len(self.video))]
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        self.lock = threading.Lock()

    def build_palette(self, timeout=None):
        return self.palette

    def start(self):
        pass

    def stop(self):
        pass

    def has_data(self):
        return bool(self.audio or self.video)

    def prebuffered(self):
        return True

    def audio_pending(self):
        return bool(self.audio)

    def video_pending(self):
        return bool(self.video)

    def pop_audio(self):
        if self.audio:
            from . import adpcm
            chunk = self.audio.pop(0)
            if not hasattr(self, "_encoder"):
                self._encoder = adpcm.Encoder()
            wire = self._encoder.encode_pcm(chunk)
            ts = self.audio_content.pop(0)
            return wire, int(ts)
        return None

    def pop_video(self, keep=0):
        if self.video:
            chunk = self.video.pop(0)
            ts = self.video_content.pop(0)
            p = frames.frame_packets(chunk) if isinstance(chunk, bytes) else chunk
            return p, int(ts)
        return None

    def failure(self):
        return None

    def close(self):
        pass

    def picture_lag_s(self):
        return 0.0

    def diagnostics(self):
        return ""


class AVServer:
    """单线程负责接入与收发，多余连接直接关闭；阻塞操作均带时限，每 50 ms 轮询停止标志。"""

    def __init__(self, *args, **kwargs):
        is_legacy = False
        if len(args) >= 1:
            if not isinstance(args[0], (bytes, bytearray, memoryview)) and args[0] is not None:
                is_legacy = True
            elif args[0] is None and len(args) >= 4:
                is_legacy = True
            elif args[0] is None and len(args) >= 2 and isinstance(args[1], (bytes, bytearray)):
                is_legacy = True

        if is_legacy:
            self.media = args[0] if len(args) > 0 else kwargs.get("media")
            token = args[1] if len(args) > 1 else kwargs.get("token")
            bind = args[2] if len(args) > 2 else kwargs.get("bind", "127.0.0.1")
            port = kwargs.get("port", args[3] if len(args) > 3 else 8096)
            self.duration_ms = kwargs.get("duration_ms", args[4] if len(args) > 4 else 1800000)
            logger = kwargs.get("logger") or (args[5] if len(args) > 5 else None)
            self.live_enabled = self.media is None
        else:
            self.media = kwargs.get("media")
            token = args[0] if len(args) > 0 else kwargs.get("token")
            bind = args[1] if len(args) > 1 else kwargs.get("bind", "127.0.0.1")
            port = kwargs.get("port", args[2] if len(args) > 2 else 8096)
            self.duration_ms = kwargs.get("duration_ms", 1800000)
            logger = kwargs.get("logger") or (args[3] if len(args) > 3 else None)
            self.live_enabled = self.media is None
        if bind != WILDCARD_BIND and not local_ipv4(bind):
            raise ValueError("bind must be 0.0.0.0 or a loopback or RFC1918 IPv4 address")
        if token is not None:
            # 构造时即校验，格式错误的令牌在此报错；None 表示不校验。
            validate_token(token.decode("ascii"))
        self.token = token
        self.bind, self.port = bind, port
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.listener: socket.socket | None = None
        self.completed = self.rejected = self.failed = self.dropped_video = 0
        # 握手阶段即关闭的连接（换频道、设备休眠、端口探测）单独计数，不计入 failed。
        self.abandoned = 0
        self.audio_sent = self.video_sent = 0
        self.session_id = 0
        self.phase = "idle"
        self.live_channel: LiveChannel | None = None
        # 空串发送音视频；audio 或 video 仅用于诊断，见 _pace_live。
        self.media_filter = ""
        self.channel_name = ""
        self.live_note = ""
        self.ffmpeg = "ffmpeg"
        # 可注入，便于不依赖 ffmpeg 和网络测试发送循环。
        if is_legacy and args[0] is not None:
            self.channel_factory = lambda *a, **k: _MediaChannel(args[0], *a, **k)
        else:
            self.channel_factory = LiveChannel
        self.frame_deadline_s = FRAME_WRITE_DEADLINE_S
        self.logger = logger or (lambda message: None)
        self.session_state: SessionState = SessionState.CLOSED
        self.state_history: List[Dict[str, Any]] = []
        self.session_limit_s: float | None = None
        self.fault_injector: Any | None = None

    def set_session_state(self, state: SessionState, session: int, reason: str = "", **kwargs: Any) -> None:
        """记录会话状态迁移及当时的发送计数。"""
        old_state = getattr(self, "session_state", SessionState.CLOSED)
        self.session_state = state
        now_m = time.monotonic()
        rec = {
            "timestamp": round(now_m, 4),
            "session": session,
            "from_state": old_state.value if isinstance(old_state, SessionState) else str(old_state),
            "to_state": state.value if isinstance(state, SessionState) else str(state),
            "reason": reason,
            "audio_sent": getattr(self, "audio_sent", 0),
            "video_sent": getattr(self, "video_sent", 0),
            "dropped_video": getattr(self, "dropped_video", 0),
            **kwargs
        }
        if not hasattr(self, "state_history") or self.state_history is None:
            self.state_history = []
        self.state_history.append(rec)
        detail_str = " ".join(f"{k}={v}" for k, v in kwargs.items())
        self.logger(
            f"session={session} STATE_CHANGE {rec['from_state']} -> {rec['to_state']} "
            f"reason='{reason}' {detail_str}".strip()
        )

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.port))
            self.port = listener.getsockname()[1]
            # 设备每秒重试一次；服务单个连接期间需留有积压队列，否则重试被直接拒绝。
            listener.listen(8)
            listener.setblocking(False)
            self.ready.set()
            while not self.stop.is_set():
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                connection, peer = listener.accept()
                with connection:
                    if not local_ipv4(peer[0]):
                        self.rejected += 1
                        continue
                    connection.setblocking(False)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    # 发送缓冲区只需容纳一个画面包；见 CLAUDE.md 关键设计决策 4。
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                          SEND_BUFFER_BYTES)

                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    self.session_id = self.audio_sent = self.video_sent = 0
                    outcome, reason = "closed", "none"
                    self.phase = "hello_receive"
                    try:
                        self._session(connection)
                        self.completed += 1
                    except (OSError, EOFError, ProtocolError, LiveError) as error:
                        # 写入可能不完整，必须关闭连接；日志只含异常类名，不含载荷、令牌、对端地址。
                        # 鉴权之前断开计为放弃，之后断开计为失败。
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        # 不在此补采 ffmpeg 诊断：_live_session 的 finally 已记录并清空 live_channel。
                    except Exception as error:
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        import traceback
                        tb = traceback.extract_tb(error.__traceback__)
                        sanitized_tb = " -> ".join([f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in tb[-3:]])
                        self.live_note = f"unexpected={type(error).__name__}[{sanitized_tb}]"
                    finally:
                        # 仅记录代码内定义的阶段名与异常类名，不记录 str/repr(error)。
                        note = f" note={self.live_note}" if self.live_note else ""
                        self.logger(f"session={self.session_id} state={outcome} "
                                    f"reason={reason} phase={self.phase} "
                                    f"authenticated={int(self.session_id != 0)} "
                                    f"audio_sent={self.audio_sent} video_sent={self.video_sent}{note}")
                        self.live_note = ""
                # 拒绝旧会话期间积压的连接，不影响后续会话。
                self._reject_waiting()
        self.listener = None

    def _reject_waiting(self) -> None:
        assert self.listener is not None
        # 即使连接洪泛也限定处理次数。
        for _ in range(8):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            connection.close()
            self.rejected += 1

    def _device_left(self, connection: socket.socket, session: int) -> bool:
        """设备已结束本会话时返回 True：发来 END，或直接关闭连接。

        写入前及一帧的多次写入之间调用，使正常换频道不被计为错误。收到 END 以外的包抛 ProtocolError；
        无 END 的断开（EOFError）原样抛出，由 serve() 按握手是否完成分类。
        """
        if not select.select([connection], [], [], 0)[0]:
            return False
        try:
            packet = receive_packet(connection, IO_TIMEOUT, expected_session=session)
        except TimeoutError:
            # select 报告可读但数据不足一个完整包：设备仍在，继续会话。
            return False
        except EOFError:
            # 无 END 的断开原样抛出，由 serve() 分类。
            raise
        if packet.kind != Kind.END:
            raise ProtocolError("only END allowed while playing")
        return True

    def _wait_until(self, connection: socket.socket, target: float, session: int) -> bool:
        self.phase = "schedule_wait"
        while not self.stop.is_set():
            delay = max(0.0, min(0.05, target - time.monotonic()))
            watch = [connection]
            if self.listener is not None:
                watch.append(self.listener)
            readable = select.select(watch, [], [], delay)[0]
            if self.listener is not None and self.listener in readable:
                self._reject_waiting()
            if connection in readable:
                self.phase = "control_receive"
                try:
                    packet = receive_packet(connection, expected_session=session)
                except TimeoutError:
                    # select 报告可读但数据尚不可取：不是对端错误，继续等待。
                    self.phase = "schedule_wait"
                    continue
                if packet.kind != Kind.END:
                    raise ProtocolError("only END allowed after HELLO")
                return False
            if time.monotonic() >= target:
                return True
        return False

    def make_channel(self) -> LiveChannel:
        """为会话的频道 id 创建新的未启动频道。"""
        return self.channel_factory(CHANNELS[self.channel_name], self.ffmpeg,
                                    CHANNEL_AGENTS.get(self.channel_name, ""))

    def _live_session(self, connection: socket.socket, channel_id: str) -> None:
        """以单一单调时钟起点调度一个直播频道。

        换频道即新会话，每个会话独占一个转码进程与一个时间起点；
        视频晚于呈现时刻则丢弃，不得为视频阻塞音频。
        """
        self.phase = "live_start"
        # 频道对应的 User-Agent 一并传入：不少镜像只响应抓取时的播放器。
        channel = self.channel_factory(CHANNELS[channel_id], self.ffmpeg,
                                       CHANNEL_AGENTS.get(channel_id, ""))
        self.channel_name = channel_id
        try:
            session = secrets.randbelow(0xFFFFFFFF) + 1
            self.session_id = session
            # 先发 CONFIG 再向源请求数据：设备握手只给约 2 秒，先采样调色板会使握手超时。
            # CONFIG 只描述格式，与调色板无关。
            send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                           json_bytes(dict(CONFIG, session=session,
                                                           channel=channel_id,
                                                           channel_list=channel_list()))))
            self.phase = "live_palette"
            # 首帧前必须有调色板，缺失则设备无色可用，画面全黑。
            try:
                channel.build_palette()
            except LiveError as error:
                # 明确告知设备失败，否则它会一直等待调色板。
                try:
                    send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                   json_bytes({"reason": "source unavailable"})))
                except OSError:
                    pass
                raise LiveError(str(error)) from None
            if not channel.palette:
                raise LiveError("channel has no palette")
            # start() 放在 try 内，Popen 失败时不泄漏管道与 stderr 文件。
            # 源起播失败不结束会话，设备停留在重连提示画面并重试。
            failed = ""
            try:
                channel.start()
            except (LiveError, OSError) as error:
                self.logger(f"session={session} source start failed: {type(error).__name__}")
                failed = "start_failed"
            if not failed:
                self.live_channel = channel
                # 校准失败只告警，不拒绝会话。
                if not channel.timeline.calibrated:
                    self.logger(
                        f"session={session} WARNING no clock calibration: "
                        f"{channel.timeline.calibration_note}; the picture will not "
                        f"be paired and no frames will be sent")
            # PALETTE 紧随 CONFIG、先于所有帧；每个会话按频道各发一次。
            send_packet(connection, Packet(Kind.PALETTE, session, 1, 0,
                                           channel.palette))
            self.logger(f"session={session} state=live channel={channel_id}")
            self.phase = "live_prebuffer"
            deadline = time.monotonic() + STARTUP_WAIT_S
            while not failed and not channel.prebuffered() and time.monotonic() < deadline:
                if channel.failure():
                    failed = "decoder_stopped"
                    break
                # 轮询连接与停止标志，不整段休眠：否则停止请求被忽略，设备重连也要排队等待。
                if self.stop.is_set():
                    return
                # _wait_until 会改写 phase，调用后恢复，否则此处结束的会话会被记为调度等待。
                waiting = self._wait_until(connection, min(deadline, time.monotonic() + 0.02), session)
                self.phase = "live_prebuffer"
                if not waiting:
                    return
            if not failed and not channel.prebuffered():
                failed = "no_media"
            resume = None
            if failed:
                wire = reconnect.WireState(seq=2, audio_pts=0)
                now = time.monotonic()
                channel = reconnect.hold(self, connection, session, channel, wire,
                                         now + AUDIO_LEAD_MS / 1000, now,
                                         restart_reason=failed)
                if channel is None:
                    return
                self.live_channel = channel
                resume = wire
            self.set_session_state(SessionState.PLAYING, session, reason="session_started")
            self._pace_live(connection, channel, session, resume)
        finally:
            self.set_session_state(SessionState.CLOSED, session, reason="session_closed",
                                   audio_sent=self.audio_sent, video_sent=self.video_sent)
            try:
                if channel.failure() is not None:
                    try:
                        self.live_note = channel.diagnostics()
                    except Exception as diag_err:
                        self.logger(f"session={session} WARNING: channel diagnostics failed: {diag_err}")
            finally:
                try:
                    channel.close()
                except Exception as close_err:
                    self.logger(f"session={session} WARNING: channel close failed: {close_err}")
                finally:
                    live, self.live_channel = self.live_channel, None
                    if live is not None and live is not channel:
                        try:
                            live.close()
                        except Exception as close_err:
                            self.logger(f"session={session} WARNING: channel close failed: {close_err}")

    def _pace_live(self, connection: socket.socket, channel: LiveChannel,
                   session: int, resume: reconnect.WireState | None = None) -> None:
        engine = os.environ.get("TV_LIVE_ENGINE", "legacy")
        if engine == "v2":
            from .live_sender import LiveSender
            LiveSender(self, connection, channel, session).run()
            return
        if engine != "legacy":
            raise ValueError("TV_LIVE_ENGINE must be legacy or v2")
        # 起点不得早于现在超过音频提前量，否则 ffmpeg 预填的数据会立即到期并突发发送。
        # 预缓冲深度不计入起点；开头的突发由 AUDIO_MAX_LOOKAHEAD_MS 与每帧一个时隙限制。
        # 起点取 START_DELAY_MS 与 AUDIO_LEAD_MS 的较大者，决定音频时间戳可领先时钟的幅度。
        origin = time.monotonic() + max(START_DELAY_MS, AUDIO_LEAD_MS) / 1000
        audio_pts = 0
        # 视频时间戳随帧由 pop_video() 给出，此处不维护。
        # 上一个画面发出时所在的时隙，-1 为尚无；时钟所在时隙超过它才可再发一帧。
        # 用时隙而非到期时间，慢速循环、暂停或突发都无法使其与当前时钟脱节。
        last_slot = -1
        # 帧率固定为源帧率；链路变慢时降低的是每帧字节目标（frames.encode_within），不是帧率；见 server/rate.py。
        # 无 fps 属性的替身频道按名义帧率调度。
        # TV_ADAPTIVE=0 时字节率也固定。
        fps = getattr(channel, "fps", FPS)
        controller = ByteRate(fps, adaptive=ADAPTIVE)
        # 同时保存在 server 上，供测试读取会话最终的字节率。
        self._last_controller = controller
        # 控制器每秒窗口的累计量：画面字节数、实际发出的帧数、最长单次写入、丢弃帧数。
        window_video_bytes = 0
        window_frames = 0
        window_worst_write = 0.0
        window_dropped = 0
        window_started = time.monotonic()
        self._recovery_start = 0.0
        # CONFIG 占序号 0，PALETTE 占 1，首个媒体包为 2；设备拒绝非连续序号。
        seq = 2
        last_report = 0.0
        last_audio = last_video = 0
        # 发送时间戳 = 频道时间戳 + stamp_base；断流期间提示帧与静音推进了设备时钟，
        # 换频道后的新进程靠它接续。
        stamp_base = 0
        last_audio_wire = -AUDIO_CHUNK_MS
        last_video_wire = -1
        if resume is not None:
            seq, audio_pts = resume.seq, resume.audio_pts
            last_audio_wire, last_video_wire = resume.audio_stamp, resume.video_stamp
            stamp_base = (last_audio_wire + AUDIO_CHUNK_MS
                          - int(round(channel.session_clock.audio_items * AUDIO_CHUNK_MS)))
            origin = time.monotonic() - (audio_pts - AUDIO_LEAD_MS) / 1000.0
        # 本循环最长无输出间隔及其间写入耗时；与设备侧报告对照可判断是哪一端停顿。
        gap_start = time.monotonic()
        max_send_gap = 0.0
        busy_us = 0.0

        def note_send(took: float) -> None:
            nonlocal gap_start, max_send_gap, busy_us
            at = time.monotonic()
            idle = at - gap_start
            if idle > max_send_gap:
                max_send_gap = idle
            busy_us += took * 1e6
            gap_start = at

        def hold_on_notice(empty_since: float) -> bool:
            """停留在重连提示画面，直到源重新有数据。

            会话结束（设备离开或要求停止）时返回 False。
            """
            nonlocal channel, seq, audio_pts, origin, now, slot_now, last_slot, stamp_base
            nonlocal last_audio_wire, last_video_wire, window_started, window_worst_write
            nonlocal window_video_bytes, window_frames, window_dropped
            self.set_session_state(
                SessionState.STARVED_REBUFFERING,
                session,
                reason="channel_audio_empty",
                empty_since=round(empty_since, 4)
            )
            wire = reconnect.WireState(seq, audio_pts, last_audio_wire, last_video_wire)
            held = reconnect.hold(self, connection, session, channel, wire, origin, empty_since)
            if held is None:
                return False
            if held is not channel:
                channel = held
                self.live_channel = held
                if hasattr(channel, "set_video_rate"):
                    channel.set_video_rate(controller.rate)
            seq, audio_pts = wire.seq, wire.audio_pts
            last_audio_wire, last_video_wire = wire.audio_stamp, wire.video_stamp
            stamp_base = (last_audio_wire + AUDIO_CHUNK_MS
                          - int(round(channel.session_clock.audio_items * AUDIO_CHUNK_MS)))
            now = time.monotonic()
            origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
            slot_now = int((now - origin) * fps)
            last_slot = slot_now - 1
            self._recovery_start = now
            self.set_session_state(
                SessionState.RECOVERING,
                session,
                reason="channel_data_resumed",
                starve_duration_s=round(now - empty_since, 4),
                realigned_origin=round(origin, 4),
                audio_pts=audio_pts,
                slot_now=slot_now
            )
            window_started = now
            window_worst_write = 0.0
            window_video_bytes = 0
            window_frames = 0
            window_dropped = 0
            return True

        self.wire_bytes = 0
        while not self.stop.is_set():
            if (channel.failure() is not None and not channel.audio_pending()
                    and self.media_filter != "video"):
                if not hold_on_notice(time.monotonic()):
                    return
                continue
            now = time.monotonic()
            self._loops = getattr(self, "_loops", 0) + 1
            # 画面时间戳随帧由 pop_video() 给出（内容时间轴，见 server/timeline.py），不在本循环推算。
            # 设备按音频时钟呈现画面，时间戳超前的帧会占用接收缓冲直至音频欠载。
            audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            # 画面按自己的时隙发送，一个时隙为一个帧间隔；时钟所在时隙大于上次发送所在时隙即可发。
            # 不能与音频到期时刻绑定：音频优先，画面永远轮不到。
            # 也不用到期时间变量：把到期时间拉向当前时刻的写法会使判定每轮恒真。
            slot_now = int((now - origin) * fps)
            # 音频掌管时间轴且不丢弃；视频须在自己的时隙到期时发送，等音频队列排空会使视频饿死。
            # 限制音频领先墙钟的幅度，避免单次循环变慢后突发发送设备放不下的数据。
            audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000
            if audio_lookahead_ms < -400.0:
                # 写入背压使发送落后实时：重新锚定起点，消除负的领先量。
                origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
                slot_now = int((now - origin) * fps)
                last_slot = min(last_slot, slot_now - 1)
                audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000.0
            # 每秒一条跟踪日志，须放在其所用变量计算完成之后。
            if now - getattr(self, "_last_trace", now) >= 1.0:
                self._last_trace = now
                self.logger(
                    f"SEND t={now-origin:6.1f}s loops={self._loops} "
                    f"a={self.audio_sent} v={self.video_sent} "
                    f"qa={len(channel.audio)} qv={len(channel.video)} "
                    f"look={audio_lookahead_ms:7.0f} "
                    f"a_due={int(audio_at<=now)} "
                    f"v_due={int(slot_now > last_slot)}")
            # socket 须当前可写：阻塞表示设备队列已满，这是让发送跟随设备时钟的背压。
            # 到期的音频写不进去就留到下一轮，音频不丢，audio_pts 与 seq 都不前进。
            watch_read = [connection]
            if self.listener is not None:
                watch_read.append(self.listener)
            readable, writable_now, _ = select.select(watch_read, [connection], [], 0)
            if self.listener is not None and self.listener in readable:
                self._reject_waiting()
            if connection in readable:
                self.phase = "control_receive"
                self._device_left(connection, session)
                return
            if getattr(self, "duration_ms", None) is not None and (now - origin) * 1000 >= self.duration_ms:
                send_packet(connection, Packet(Kind.END, session, seq, 0))
                return
            writable = bool(writable_now)
            # 音频到期时严格优先于画面。让音频向画面让步的宽松规则会使音频近乎永远发不出去。
            # 不会饿死画面：循环频率远高于音频到期频率，每块音频至多让画面晚约 1 ms。
            self.wire_bytes = getattr(self, "wire_bytes", 0)
            # 是否允许音频早于其对应时刻发送。TV_AUDIO_FILL 默认开启：发送端把设备队列补到
            # AUDIO_MAX_LOOKAHEAD_MS 并保持；关闭则音频恰按实时放出，画面包占线期间设备队列
            # 得不到补充而欠载。
            audio_fill = os.environ.get("TV_AUDIO_FILL", "1") != "0"
            audio_due = (channel.audio_pending()
                         and audio_lookahead_ms <= AUDIO_MAX_LOOKAHEAD_MS
                         and (audio_fill or now >= audio_at))
            # 音频到期不阻塞画面：两者不用 if/elif，同一轮内先音频、后画面；
            # 否则画面只能在两块音频的空隙发送，而该空隙不存在。
            send_audio = audio_due and writable
            send_video = slot_now > last_slot and writable and channel.video_pending()
            # 仅诊断用：--media audio 或 video 关闭其中一路，以定位故障；默认两路都开。
            if self.media_filter == "audio":
                send_video = False
            elif self.media_filter == "video":
                # 画面按音频位置调度：手动推进音频时间轴而非冻结，否则所有帧同时到期。
                send_audio = False
                audio_pts += AUDIO_CHUNK_MS
            self._trace = getattr(self, "_trace", 0) + 1
            if os.environ.get("TV_TRACE") == "1":
                self.logger(
                    f"TRACE n={self._trace} t={(now-origin):6.2f}s "
                    f"due={int(audio_due)} wr={int(writable)} qa={len(channel.audio)} "
                    f"qv={len(channel.video)} look={audio_lookahead_ms:7.0f} "
                    f"sa={self.audio_sent} sv={self.video_sent} "
                    f"late={self.dropped_video} "
                    f"frameK={sum(self._frame_sizes[-3:]) // 1024 if getattr(self,'_frame_sizes',None) else 0} "
                    f"wire_kbps={(self.wire_bytes*8/1000)/max(0.001, now-origin):7.1f}")
            if send_audio:
                # 发送所有已到期的音频块，而非每轮一块：写画面包期间会积欠多个音频时隙，
                # 逐轮补发会使领先量持续为负。上限 32 块（640 ms）仅防止病态队列独占循环；
                # 同时受时钟和 AUDIO_MAX_LOOKAHEAD_MS 约束。
                for _ in range(32):
                    if not ((audio_fill or audio_at <= now)
                            and channel.audio_pending()
                            and audio_pts - AUDIO_LEAD_MS - (now - origin) * 1000
                                <= AUDIO_MAX_LOOKAHEAD_MS
                            and select.select([], [connection], [], 0)[1]):
                        break
                    taken = channel.pop_audio()
                    if taken is None:
                        break
                    block, stamp = taken
                    stamp += stamp_base
                    last_audio_wire = stamp
                    self.phase = "live_pcm_send"
                    send_packet(connection, Packet(Kind.PCM, session, seq, stamp, block))
                    self.wire_bytes += len(block) + 24
                    # stamp 是线上时间戳（来自声音自身的内容时间轴）；audio_pts 是发送端按已发块数
                    # 推算的下一块到期时刻。两者不合并。
                    audio_pts += AUDIO_CHUNK_MS
                    seq += 1
                    self.audio_sent += 1
                    audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            sent_something = send_audio
            if send_video:
                sent_something = True
                # 画面队列深度与音频一致，见 live.py 的 VIDEO_QUEUE_SECONDS。
                chosen = channel.pop_video()
                frame, video_pts = chosen if chosen is not None else (None, None)
                # 没有与队首声音匹配的帧不等于帧迟到：此时不消耗时隙，下一轮再取。
                if frame is None:
                    # 本轮无帧可发，仍须执行下面的等待、控制器更新与周期报告，不能 continue。
                    sent_something = False
                else:
                    # 时隙无论帧是否发出都已消耗：拥塞丢帧不能把时隙让给下一帧，否则积压会一次性放出。
                    # 取决策时的值，不重读时钟。
                    last_slot = slot_now
                    self._frame_sizes = getattr(self, "_frame_sizes", [])
                    self._frame_sizes.append(sum(len(p) for p in frame))
                    # 不做迟到否决：设备按到达即绘，略迟的帧仍值得发；丢弃会使索引跳进，引发持续丢帧。
                    # 陈旧度由队列限定：队列定长，满时丢最旧。
                    if writable:
                        # 不可写时丢弃整帧而非尝试：发不完的写入会阻塞单线程循环及音频，也不得在线上留下半个包。
                        # 同一帧的各包要么全发要么不发。
                        self.phase = "live_video_send"
                        # 整帧一个写入时限 max(帧间隔, FRAME_WRITE_DEADLINE_S)，是失败上限而非调度；
                        # 须小于设备约 3 秒的断开阈值，且不能短到让正常帧在传输期间超时。
                        # 过期帧由调度器丢弃，慢帧只应损失该帧，不应结束会话。
                        frame_deadline = time.monotonic() + max(1.0 / fps, FRAME_WRITE_DEADLINE_S)
                        video_pts = max(video_pts + stamp_base, last_video_wire + 1)
                        last_video_wire = video_pts
                        for n, part in enumerate(frame):
                            # 帧内各次写入之间检查设备是否已离开，避免正常换频道表现为 broken pipe。
                            self.phase = "control_receive"
                            if self._device_left(connection, session):
                                return
                            # 画面包之间让音频通过：画面包被设备窗口卡住时会阻塞整帧时限，期间无音频发出而欠载。
                            # 各包自带长度与时间戳，格式不要求同一帧的包相邻，只要求单个包的字节连续。
                            # 补发已到期的全部音频块（上限 32），逐块补发会使欠额持续增长。
                            for _ in range(32):
                                now_in_frame = time.monotonic()
                                audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
                                lookahead = ((audio_pts - AUDIO_LEAD_MS)
                                             - (now_in_frame - origin) * 1000)
                                if not ((audio_fill or now_in_frame >= audio_at)
                                        and channel.audio_pending()
                                        and lookahead <= AUDIO_MAX_LOOKAHEAD_MS
                                        and select.select([], [connection], [], 0)[1]):
                                    break
                                taken = channel.pop_audio()
                                if taken is None:
                                    break
                                block, stamp = taken
                                stamp += stamp_base
                                last_audio_wire = stamp
                                self.phase = "live_pcm_send"
                                _audio_t0 = time.monotonic()
                                send_packet(connection, Packet(Kind.PCM, session, seq,
                                                               stamp, block),
                                            deadline=frame_deadline)
                                note_send(time.monotonic() - _audio_t0)
                                audio_pts += AUDIO_CHUNK_MS
                                seq += 1
                                self.audio_sent += 1
                            self.phase = "live_video_send"
                            # 同一帧各包时间戳相同，除首包外以 VIDEO_CONTINUES 标志声明，否则设备按时间戳不前进而拒绝。
                            # 单个画面包必须连续写出，中间不能插入音频包：设备读完包头后按 length 读取负载，
                            # 插入的音频字节会被当作画面数据而结束会话。因此音频只在包之间发送，
                            # 包内用 _write_slice 分片写入，分片仅为让循环不被整包占住并能响应停止请求。
                            wire = Packet(Kind.JPEG, session, seq, video_pts, part,
                                          VIDEO_CONTINUES if n else 0).encode()
                            sent = 0
                            while sent < len(wire):
                                self.phase = "live_video_send"
                                data = memoryview(wire)[sent:sent + VIDEO_SLICE_BYTES]
                                _probe_t0 = time.monotonic()
                                if not _write_slice(connection, data, frame_deadline):
                                    raise TimeoutError("video packet deadline expired")
                                _probe_dt = time.monotonic() - _probe_t0
                                note_send(_probe_dt)
                                # 速率控制器按分片判断窗口：这是 socket 一次接受的单位，其耗时反映链路拥塞程度。
                                if getattr(self, "session_state", None) != SessionState.RECOVERING and _probe_dt > window_worst_write:
                                    window_worst_write = _probe_dt
                                if _probe_dt > PROBE_SLOW_S:
                                    self.logger(
                                        f"PROBE slice n={n} bytes={len(data)} "
                                        f"took_ms={_probe_dt * 1000:.0f} phase={self.phase}")
                                sent += len(data)
                            self.wire_bytes += len(part) + 24
                            # 只统计画面字节：声音固定占 32 kB/s，计入会使各频道看起来一样重。
                            window_video_bytes += len(part) + 24
                            seq += 1
                            self.video_sent += 1
                        # 每帧只计一次（在包循环之外）：控制器用窗口字节数除以该值得到每帧成本。
                        window_frames += 1
                        if getattr(self, "session_state", None) == SessionState.RECOVERING:
                            rec_start = getattr(self, "_recovery_start", window_started)
                            # 至少 1.5 秒且 10 帧后才退出 RECOVERING，等待链路波动平息。
                            if (now - rec_start >= 1.5) and window_frames >= 10:
                                self.set_session_state(
                                    SessionState.PLAYING,
                                    session,
                                    reason="recovered_to_steady_state",
                                    video_pts=video_pts,
                                    realigned_origin=round(origin, 4),
                                    current_fps=fps
                                )
                                window_started = time.monotonic()
                                window_worst_write = 0.0
                                window_video_bytes = 0
                                window_frames = 0
                                window_dropped = 0
                    else:
                        # 拥塞：放弃该帧以保住音频时间轴；时间戳取自时钟，丢帧只损失一张画面。
                        self.dropped_video += 1
                        window_dropped += 1

            if not sent_something:
                # 暂无到期内容：短暂休眠而非阻塞到下个时刻，以便及时发现 END 或断开。
                if not channel.has_data():
                    empty_since = time.monotonic()
                    empty_threshold = 0.5 if getattr(self, "session_state", None) == SessionState.RECOVERING else 0.3
                    # 先等待正常的块间到达，再判定断流。
                    while not channel.audio_pending() and self.media_filter != "video" and (time.monotonic() - empty_since < empty_threshold):
                        time.sleep(0.005)

                    if not channel.audio_pending() and self.media_filter != "video":
                        # 持续无数据：设备停留在提示画面。
                        if not hold_on_notice(empty_since):
                            return
                    else:
                        time.sleep(0.005)
                    window_frames = 0
                    window_dropped = 0
                else:
                    time.sleep(0.005)
            # 每秒决策一次，依据刚结束的窗口；窗口须长到容纳数帧，又短到能在设备放弃前多次决策。
            if now - window_started >= 1.0:
                # 先判断源是否仍在产出，再通知控制器：源停止与发送快于链路读数相同而应对相反。
                # 替身频道可能没有 source_state，缺省视为 FLOWING。
                if hasattr(channel, "source_state"):
                    source = channel.source_state(
                        queue_over_bound=len(channel.video) >= channel.video.maxlen)
                else:
                    source = SourceState.FLOWING
                # 仅用于周期报告，不作为控制器的总开关：停止的源仍可能有排队内容，
                # 其写入仍是下游容量的有效证据。
                self._last_source_state = source
                if getattr(self, "session_state", None) != SessionState.RECOVERING:
                    before = controller.rate
                    controller.observe(window_video_bytes,
                                       window_worst_write * 1000,
                                       window_dropped,
                                       frames=window_frames,
                                       window_s=now - window_started)
                    if controller.rate != before:
                        if hasattr(channel, "set_video_rate"):
                            channel.set_video_rate(controller.rate)
                        self.logger(f"RATE {controller.rate // 1000} kB/s: {controller.describe()}")
                window_video_bytes, window_worst_write = 0, 0.0
                window_dropped = window_frames = 0
                window_started = now
            if now - last_report >= 5:
                elapsed = now - last_report if last_report else 5.0
                self.logger(
                    f"live t={now-origin:6.1f}s fps={fps} "
                    f"rate={controller.rate // 1000}kB/s ({controller.reason}) source={getattr(self, '_last_source_state', '?')} "
                    # 画面与声音内容时钟在配对点的偏差；a_lag、v_lag 只是队列深度。
                    f"clock=({channel.session_clock.diagnostics()}) "
                    # 对齐所用的基准；getattr 防止报告本身结束会话（测试替身没有 timeline）。
                    f"basis=({getattr(getattr(channel, 'timeline', None), 'basis', None) or 'none'}) "
                    f"audio_q={len(channel.audio):4d} "
                    f"video_q={len(channel.video):4d} "
                    # 下一个包落后直播的内容秒数，音画两者应一致。
                    f"a_lag={len(channel.audio) * AUDIO_CHUNK_MS / 1000:5.1f}s "
                    f"v_lag={channel.picture_lag_s():5.1f}s "
                    f"lookahead_ms={audio_lookahead_ms:6.0f} "
                    f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                    f"audio_pps={(self.audio_sent - last_audio) / elapsed:5.1f} "
                    f"video_pps={(self.video_sent - last_video) / elapsed:5.1f} "
                    f"late_video={self.dropped_video} prod_drop={channel.dropped_video} "
                    f"send_gap_max_ms={max_send_gap * 1000:5.0f} "
                    f"busy={busy_us / 1e6:4.1f}s")
                last_report = now
                last_audio, last_video = self.audio_sent, self.video_sent
                max_send_gap = 0.0
                busy_us = 0.0
        # 服务端停止是正常结束，不是流故障。
        if self.stop.is_set():
            return
        raise TimeoutError("live session cancelled")

    def _session(self, connection: socket.socket) -> None:
        self.phase = "hello_receive"
        hello = receive_packet(connection, expected_session=0)
        self.phase = "authenticate"
        authenticate(hello, self.token)
        if self.live_enabled:
            # 设备按名选择频道，换频道即新连接；名称缺失或不在表内时回退到 --channel 指定的默认频道。
            requested = json_object(hello.payload).get("channel")
            if isinstance(requested, str) and requested in CHANNELS:
                channel_id = requested
            else:
                channel_id = self.channel_name or DEFAULT_CHANNEL
            self._live_session(connection, channel_id)
            return

        session = secrets.randbelow(0xFFFFFFFF) + 1
        self.session_id = session
        self.logger(f"session={session} state=authenticated")
        self.phase = "config_send"
        send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                       json_bytes(dict(CONFIG, session=session, duration_ms=self.media.duration_ms))))
        send_packet(connection, Packet(Kind.PALETTE, session, 1, 0, self.media.palette))
        origin = time.monotonic() + 0.2
        seq = 2
        for due_ms, kind, pts, index in schedule(self.duration_ms, self.media.duration_ms):
            if not self._wait_until(connection, origin + due_ms / 1000, session):
                return
            self.phase = "control_receive"
            if self._device_left(connection, session):
                return
            now = time.monotonic()
            if kind == Kind.PCM and now > origin + pts / 1000 + 0.1:
                self.phase = "audio_schedule_late"
                raise TimeoutError("audio schedule stalled; reconnect for a new origin")
            if kind == Kind.JPEG and now > origin + (pts + 83) / 1000:
                self.dropped_video += 1
                continue
            if kind == Kind.PCM:
                self.phase = "pcm_send"
                send_packet(connection, Packet(Kind.PCM, session, seq, pts,
                                               self.media.audio_at(index)))
                seq += 1
                self.audio_sent += 1
            else:
                self.phase = "video_send"
                frame_deadline = time.monotonic() + IO_TIMEOUT
                for n, part in enumerate(self.media.frame_at(index)):
                    if n and not select.select([], [connection], [], frame_deadline - time.monotonic())[1]:
                        self.dropped_video += 1
                        break
                    if n and self._device_left(connection, session):
                        return
                    flags = VIDEO_CONTINUES if n else 0
                    send_packet(connection, Packet(Kind.JPEG, session, seq, pts, part, flags))
                    seq += 1
                self.video_sent += 1
        self.phase = "end_send"
        send_packet(connection, Packet(Kind.END, session, seq, 0))

def print_where_to_connect(bind: str, port: int, token: bytes | None) -> None:
    """启动后最后打印的内容：先给设备应填的地址，再给两条提示。"""
    shown = bind
    if bind == WILDCARD_BIND:
        detected = netident.lan_address()
        shown = detected if detected and local_ipv4(detected) else None
    for line in netident.describe(shown, port):
        print(line, flush=True)

    # 无令牌时同网段设备均可收看，提示之。
    if token is None:
        print("提示：本网络上的其他设备也能收看这台电脑转发的频道。", flush=True)

    # 关闭窗口即停止服务；窗口看起来像日志，用户不易联想到。
    print("提示：关掉这个窗口，服务就停止了，电视会中断。", flush=True)


def main(argv: list[str] | None = None) -> int:
    # 输出固定为 UTF-8：管道下默认取本地编码（Windows 中文为 GBK），log_stamp 按 UTF-8 解码会乱码。
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live_parser = commands.add_parser("live", help="transcode one allowlisted public channel in real time")
    live_parser.add_argument("--channel", required=True, choices=sorted(CHANNELS))
    live_parser.add_argument("--ffmpeg", default="ffmpeg")
    # 仅诊断用：关闭一路以判断是画面还是声音的问题。
    live_parser.add_argument("--media", choices=("both", "audio", "video"),
                             default="both")
    live_parser.add_argument("--bind", default=WILDCARD_BIND,
                             help="address to listen on (default: all interfaces; peers outside private networks are refused)")
    live_parser.add_argument("--port", type=int, default=8096)
    live_parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    try:
        token = load_token(args.token_file)
        server = AVServer(token, args.bind, args.port,
                          logger=lambda message: print(message, flush=True))
        server.ffmpeg = args.ffmpeg
        server.media_filter = "" if args.media == "both" else args.media
        server.channel_name = args.channel
        # 信号处理器须设置接入循环实际读取的标志。
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: server.stop.set())
        # 每个设备连接启动独立转码进程；ffmpeg 退出只结束该会话，接入循环无需重启。
        print_where_to_connect(args.bind, args.port, token)
        server.serve()
        # 仅在出现异常时打印计数；握手期放弃的连接不计，换频道每次都会产生一个。
        if server.failed or server.rejected or server.dropped_video:
            print(f"已停止。出错 {server.failed} 次，拒绝 {server.rejected} 次，"
                  f"丢帧 {server.dropped_video} 次。", flush=True)
        else:
            print("已停止。", flush=True)
        return 0
    except OSError as error:
        # 绑定失败与令牌、媒体问题难以区分，端口被占用又像静默无操作；errno 不含路径与凭据。
        # 用 errno.EADDRINUSE 而非数值：各平台数值不同。
        if error.errno == errno.EADDRINUSE:
            print("端口 8096 已被占用——多半是上一次的程序还没关干净。", file=sys.stderr)
            print("把之前的窗口关掉，或重启电脑后再试。", file=sys.stderr)
        else:
            print(f"网络端口打不开（errno={error.errno}）。", file=sys.stderr)
        return 1
    except Exception:
        # 子进程错误、路径、环境变量可能含敏感值，不输出。
        print("程序没能启动。常见原因是 ffmpeg 缺失或频道表有问题；", file=sys.stderr)
        print("把上面最后几行输出发给项目的维护者可以定位。", file=sys.stderr)
        if os.environ.get("TV_TRACEBACK") == "1":
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
