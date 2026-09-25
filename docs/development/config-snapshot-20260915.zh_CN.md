<p align="right">
  <strong>简体中文</strong> · <a href="config-snapshot-20260915.md">English</a>
</p>

# 配置快照（2026-09-15，自适应帧率动工之前）

这份快照是为了让自适应帧率的改动可以退回去，不必猜。下面每个值都是拍照那一刻
实际生效的，都标了它在哪个文件哪一行。

## 怎么退回

工作区是"脏"的——整个画面通路重写都还没提交——所以 `git checkout` 回不到现在这个
状态，它只会退到上一个提交，而那是另一个程序。

两种退回方式都可以：

```bash
# 整棵树，含画面通路重写：
cp -a ~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/. \
      ~/Desktop/LLMtopic/ai-passport-tv/

# 或者只退自适应改动会碰的那四个文件：
cp ~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/server/{media,live,tv_server,frames}.py \
   ~/Desktop/LLMtopic/ai-passport-tv/server/
```

这一状态的四个服务端文件另有一份副本，在
`~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/`。

## 真正起作用的设置

| 设置 | 当前值 | 位置 | 作用 |
| --- | --- | --- | --- |
| `TV_FPS` | **10**（文件里的默认值） | `server/media.py:38` | 服务端每秒送多少幅画面 |
| `TV_PREBUFFER_S` | **8** | `server/live.py:154` | 开始播放前先攒够多少秒 |
| `TV_STALL_S` | 未设置 | —— | 已删除；停源超时改回常量 |
| `AUDIO_LEAD_MS` | 200 | `server/media.py:47` | 声音领先墙上时钟多少 |
| `AUDIO_MAX_LOOKAHEAD_MS` | 160 | `server/live.py:109` | 声音最多再往前跑多少 |
| `PCM_QUEUE_CHUNKS` | 3000（60 秒） | `server/live.py:118` | 音频队列容量 |
| `VIDEO_QUEUE_FRAMES` | 180（15 秒） | `server/live.py:124` | 画面队列容量 |
| `PREBUFFER_CHUNKS` | 推导得 8 秒 | `server/live.py:155` | 垫层的声音那一半 |
| `PREBUFFER_FRAMES` | 推导得 8 秒 | `server/live.py:156` | 垫层的画面那一半 |
| `PREBUFFER_TIMEOUT_S` | 60 | `server/live.py:161` | 源始终不出内容时的兜底 |
| `PACKET_TARGET_BYTES` | 12288 | `server/frames.py:87` | 每个画面包的字节预算 |
| `VIDEO_MAX` | 12288 | `server/frames.py:82` | 包的硬上限；必须等于 `main/av_protocol.h` 里的 `AV_VIDEO_MAX` |
| `MIN_LINK_BYTES_PER_SEC` | 32768 | `server/tv_server.py:71` | 估算"一帧允许多久"用的悲观速率 |
| `VIDEO_SLICE_BYTES` | 4096 | `server/tv_server.py:85` | 写多少字节就让声音插一次队 |

## 设备

固件是 2026-09-15 刷的 `sdkconfig.av-prototype` 构建，接收任务优先级 6、视频任务 5。
地址 192.168.0.117。

**这项工作不需要刷固件。** 帧率、垫层深度、包大小全在服务端；设备接受 `TV_FPS`
宣告的 1 到 30 之间任何值。

## 为什么 10 是不对的

实测这条链路能过约 227 kB/s。声音被协议定死在每秒 50 包 × 640 字节，即 32 kB/s。
留给画面的约 195 kB/s，而 320×240 的一帧实测中位 21 kB、最坏 28 kB。每秒十帧就是
210 kB/s——还没算最坏的那一帧就已经超了。八帧有 14% 余量；九帧对中位帧只剩 3%，
而那 3% 在频道一播到激烈画面时就没了。
