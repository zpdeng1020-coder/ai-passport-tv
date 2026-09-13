<p align="right">
  <strong>简体中文</strong> · <a href="README.md">English</a>
</p>

# 本地音画原型服务器

本模块是手动启用、单设备的**局域网直播频道、视频文件与合成素材播放器**，不是公网代理或生产级鉴权服务。
运行服务器只需 Python 3.11+ 标准库；ffmpeg 用于预生成素材，以及 `live` 子命令的实时转码，因此运行直播的主机需要安装它。频道地址是 `server/live.py` 中的固定白名单，来自社区聚合播放列表，可用性与版权状态未经核实，仅用于家庭局域网测试。设备在握手中按名称指定频道，服务器对无法识别的名称回退 `DEFAULT_CHANNEL`。
不需要 pip 依赖、设备访问或全局配置改动。以下命令从仓库根目录运行。

## 导入视频文件

```sh
python3 -m server.av_server import-video --input /path/to/video.mp4 \
  --media-dir server/.local/my-video --seconds 60 --start 0
```

输入为本地文件，不接收远程网址。可选择1～600秒，按原显示比例（含像素宽高比）适配16:12（4:3），居中留黑边，输出160×120、12FPS画面和16kHz单声道PCM。不裁切或拉伸；YUV420偶数取整可能导致两侧黑边最多相差两像素。没有声音的视频会补静音轨，JPEG单帧上限仍为24KiB。播放时逐包读取磁盘，不把整部视频载入内存。将生成目录交给下文相同的 `run --media-dir` 命令即可。这一步是文件转换后播放，尚不是IPTV实时转码。

旧320x240 JPEG/manifest应在新目录重新生成。设备要求160x120输入，通过x2最近邻放大到320x240；不据此声称设备帧率改善。协议时序与PCM不变。

## 播放直播频道

最省事的用法——启动全部服务，并打印设备上要填的地址：

```sh
./run.sh                 # macOS 与 Linux
run.bat                  # Windows
```

它同时启动媒体服务器和频道配置页，并在频道表保存后自动重启媒体服务器。
需要 Python 3.9 以上和 ffmpeg；缺哪个都会明确告诉你，以及怎么装。

> **Windows 未实测。** `run.bat` 与 `tools/launch.py` 里 Windows 分支的写法
> 来自命令的公开文档，从未在 Windows 机器上运行过——本项目只在 macOS 上开发。
> 请当作未经检验的代码对待。

两个服务也可以分开单独运行：

```sh
python3 -m server.av_server live --channel ch000      # 媒体服务器，端口 8096
python3 tools/channel_config.py                       # 频道配置页，端口 8097
```

`--bind` 可以省略，而且通常就该省略：服务器会自己找出本机的网络地址，
并同时打印该地址和这台电脑的 `.local` 名字。设备上建议填**名字**，
因为路由器重新分配 IP 之后名字不用改。多网卡机器仍可手动指定 `--bind`。

```sh
python3 -m server.av_server live --channel ch000 \
  --bind 192.168.1.20 --port 8096 --token-file <仅属主可读的文件>
```

每个设备连接都会启动自己的 ffmpeg 进程和独立时间起点，因此换台就是设备用新的频道名重新连接。转码失败只结束该次会话，服务继续监听。可用频道见 `server/live.py` 的白名单。

频道表只在启动时读取一次。`run.sh` 与 `run.bat` 会监视 `channels.txt`，
文件变化时自动重启服务器；直接运行上面的命令则要自己重启才能生效。

## 一次预生成、复制、再运行

```sh
python3 -m server.av_server prepare --media-dir server/.local/media
# Optional: --ffmpeg /absolute/path/to/ffmpeg
```

目标目录必须不存在。输出是可移动的 `manifest.json`、`audio.s16le`（320000 字节）
和 `frame-000.jpg` 到 `frame-119.jpg`。将整个目录原样复制到运行主机即可。
`server/.local/` 下生成的素材和本地配置被 Git 忽略。预生成失败时应排查原因，
用新目标目录重试，不能使用生成到一半的素材。`run` 在监听前检查全部 JPEG。

10 秒素材包含三位帧计数（000–119）、运动方块、每个整数秒起点的一帧白色闪光，
以及对应的 50 ms、1 kHz 短音。短音幅度 0.08（-22 dBFS），设备须从低音量开始。
PCM 与视频同从 t=0 起步。RGB 图由标准库生成，无字体包依赖，然后 ffmpeg 编码为
baseline 160×120 JPEG 4:2:0 和 16 kHz 单声道 s16le。
JPEG 超限时逐步降低质量（q=5 后尝试 10、18、25、31），仍超限则预生成失败，
**绝不截断 JPEG**。简单合成图不代表最坏情况 JPEG 解码或吞吐压力。

创建配对文件时，不把值放进命令参数或 shell 日志：

```sh
python3 - <<'PY'
import os
import secrets
from pathlib import Path
folder = Path('server/.local')
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(folder / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_hex(32) + '\n')
PY
python3 -m server.av_server run --media-dir server/.local/media \
  --bind 127.0.0.1 --port 8096 --token-file server/.local/token \
  --duration-seconds 1800
```

通过另外管理的设备私有配置写入相同 token，不打印、不提交。
也可由支持秘密管理的启动器提供 `AV_PAIRING_TOKEN` 环境变量；此时不能同时指定
`--token-file`。token 为 16–128 个可打印 ASCII 字符，应使用随机值而非口令。
文件必须为进程用户拥有的普通文件，权限严格为 0400 或 0600，不能是符号链接。
用非特权账户运行。比对使用 `hmac.compare_digest`，失败时不输出 token 或输入 JSON。

连接设备时，把 `127.0.0.1` 替换为服务器实际获分配的**明确 RFC1918 局域网 IPv4 地址**。
拒绝通配/公网绑定、主机名、IPv6 和公网来源；默认端口 8096。本原型无 TLS：
仅用于可信隔离局域网，使用主机已有防火墙策略限制访问，不要做公网端口转发。
仅过滤 RFC1918 不能阻止被转发的流量。仅允许一个已认证流，推流期间新客户端立即关闭。
首个 HELLO 总接收时限为 250 ms。

## 线协议

每个 TCP 包使用 24 字节网络序 `!4sBBHIIII` 包头：

| 字段 | 值 |
| --- | --- |
| magic、version、flags | `FAV1`、1、0 |
| type | HELLO=1、CONFIG=2、PCM=3、JPEG=4、END=5、ERROR=6 |
| session | uint32；初始 HELLO=0，服务端生成新的非零值 |
| seq | uint32；HELLO=0、CONFIG=0，其后服务端发送序号全局递增 |
| pts_ms | uint32，共享素材时间线上的呈现时间戳 |
| payload_length | uint32，读取载荷前校验 |

客户端先发送 HELLO（session=seq=pts=0）：

```json
{"version":1,"token":"<private pairing token>","channel":"cctv1"}
```

`channel` 可选，用于指定要播放的频道；省略或填服务器不认识的名称时使用默认频道。

CONFIG 包含 `width=160`、`height=120`、`fps=12`、`sample_rate=16000`、
`channels=1`、`sample_bits=16`、`audio_chunk_ms=20`、`video_max_bytes=24576`、
`session`，以及补充提示字段 `duration_ms=10000`、`start_delay_ms=200`、
`audio_lead_ms=200`、`video_lead_ms=50`；直播路径还会带上 `channel` 与 `channel_list`。
控制 JSON 不超过 1024 字节。

`channels` 是音频声道数，必须保持为数字；可选频道列表放在独立的 `channel_list`
数组里（每项 `{id,name}`），设备据此换台。两者不可混用：把列表当作 `channels`
发送会让设备判定 CONFIG 非法，每个会话都会立即断开。
每 20 ms 一个 PCM 包，严格 640 字节（320 个小端样本）；JPEG 不超过 24 KiB。
END 无载荷。客户端可以发送匹配会话的 END 或关闭连接停止；HELLO 后不允许上行媒体或其他控制命令。
非法版本、flags、类型、长度、会话或 JSON 都直接断开，不搜索下一个 magic 恢复。
ERROR 类型保留且支持拆包；服务器错误时关闭连接，不冒险在半包发送后插入 ERROR。

## 调度、边界与停止

- 服务端时间起点为 CONFIG 写完后 200 ms；音频在 PTS 前 200 ms 发送，视频提前 50 ms。
  发送调度不能保证网络到达时间和实际设备播放时间。
- **跨类型的线上 PTS 不保证全局排序。** 按发送截止时间合并事件，PCM 内与 JPEG 内 PTS
  各自严格递增，序号全局递增。如果两种数据统一按原始 PTS 排序，会让视频挡在需要更早
  预送的音频之前，形成队头阻塞。例如 audio PTS=140 会先于 video PTS=0 发送。
  接收端必须分别跟踪音频和视频 PTS。
- 每 10 秒素材索引循环，但同一会话 PTS 继续到 10000 及以后。整数时间戳避免累计误差：
  视频 PTS=`frame_index*1000//12`。默认持续 1800 秒（180 轮），可设 1–86400 秒。
  重连创建新会话和新时间起点。
- 预生成路径无应用发送队列、不做实时转码（直播路径由 `live` 子命令另行启动 ffmpeg）。预生成时内存固定持有 10 秒素材（最高约 3.3 MiB）和一个有界发送包。
  请求内核发送缓冲 32768 字节；操作系统可能取整或倍增。
- 非阻塞套接字读写使用**每完整包 250 ms 总时限**，不因每个碎片到来重置时限。
  操作之间每最多 50 ms 检查额外连接和取消。半包发送超时即关闭。
- JPEG 超过呈现时刻 83 ms 尚未发送则丢弃；音频迟于呈现时刻 100 ms 则关闭会话，
  不追赶无界过期积压。接收端负责重连和重缓冲。
- SIGINT/SIGTERM 停止本地服务器，无后台工作线程；有限播放在时间线结束时发送 END，
  监听器继续等待下一客户端。正常用户态取消延迟不超过约 300 ms；这不是对操作系统
  调度或主机挂起的硬实时保证。
- 日志包括认证后的会话编号、每 10 秒已发 PCM/JPEG 计数、丢视频总数及关闭/失败总结。
  从不包含凭据或收到的载荷。这些是**套接字提交计数**，不是 DAC/DMA、声学播放或音画同步验收数据。

## 主机验证

```sh
python3 tests/test_av_server.py -v
python3 tests/test_video_import.py -v
python3 tests/test_live_transcode.py -v   # 加 AV_LIVE_TEST=1 才真实拉流
```

测试覆盖包头字节、拆包/粘包、EOF、不搜索 magic 恢复、未知字段、载荷及 uint32 边界、
慢接收/发送总时限、token 来源/权限/鉴权、会话错配、额外客户端拒绝、播放结束、JPEG 元数据，
以及完整 30 分钟**模拟**调度（90000 个 PCM 包、21600 个 JPEG 帧）。
PATH 有 ffmpeg 时，还真实预生成并重新加载 10 秒素材，检查全部 10 个音频标记窗口；
无 ffmpeg 则明确跳过该预生成测试。拆包测试使用的 JPEG 元数据替身不是图片解码验证。

30 分钟真实时间设备测试、DMA/音频时序、射频性能与 ROM JPEG 解码属于另外的验收工作。
主机测试通过不意味着这些项目通过。参见[仓库测试指南](../docs/development/engineering/build-and-test.md)。

## 范围说明

保留约定的不等预送量，只明确跨类型 PTS 的排序含义。非法输入直接关闭而非回复 ERROR，
避免半包歧义；新增 CONFIG 字段为补充提示。本次允许修改范围是 `server/` 和
`tests/test_av_server.py`，故说明放在服务器旁，文档索引/changelog 由协调改动整合。
本模块不改固件或部署配置。
