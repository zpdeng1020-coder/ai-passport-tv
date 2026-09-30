<p align="right">
  <strong>简体中文</strong> · <a href="tcp-delta-progress-20260930.md">English</a>
</p>

# TCP 方案优化进度与当前问题（2026-09-30）

> **更新（当天结束时）。**设备端暂时认为做完，服务端已经不是下面第 2、5、6 节描述的样子。固件现在自带 64 KB 窗口和更快的展开函数；服务端按频道固定帧率，并把每一帧压到目标字节数以内，而不是去调帧率。做了什么、还剩什么见 [server-optimisation-handoff-20260930.zh_CN.md](server-optimisation-handoff-20260930.zh_CN.md)，接下来读那一份。下面第 1、3、4 节的测量仍然有效。
>
> **有意不在仓库里的：**取得这些数字所用的测量仪器（`main/av_demo.c`、`main/net_demo.c`、`main/jpeg_bench.c`、它们对应的 `tools/sdkconfig.*` overlay、`tools/frame_size_lab.py`、素材生成脚本），以及描述它们的三份结果文档（本机播放上限、网络播放结果、网络验证交接）。本文提到其中任何一个，指的都是只存在于跑过这些测量的那台机器上的东西。

UDP 传输改造（网络播放实测结果那份文档，不在仓库里）**已搁置**：它要同时改设备接收任务、服务端发送路径、`av_stream_accept` 的严格 `seq` 检查和音频流控，而且观看者认为它的画面和声音不可用（即使帧率到 20 以上也有撕裂和断续）。本文记录**产品 TCP 路径**上已经做了什么、测到什么、还有什么问题，后续 TCP 方向的优化以本文为起点。每个数字都是**单次运行**。

## 1. 目前的结论

- **产品路径上的帧率上限是服务端的限速器，不是链路、TCP 窗口或设备。**`server/rate.py` 的天花板按 `VIDEO_BUDGET_BYTES ÷ 每帧字节数` 推出，而预算（185000 B/s）按它自己的注释，只是一个从未作为链路上限测过的工作点。用原版服务端跑真实的 CCTV1 直播，把限速器换成固定速率（`TV_ADAPTIVE=0 TV_FPS=25`），设备画出 **24.3 fps（中位）**，接收约 349 KB/s，无音频中断、无丢帧、无会话重置（90 秒，单次运行）。这就是素材本身的帧率。
- 预算提到 280000 得到 18.6–20.0 fps，默认值是 12.0。开着限速器的每次运行，窗口结尾都是 `sent 18x kB in the window`，写入耗时只有 16–78 ms，远低于 250 ms 的慢写线：限速器是在链路并不拥堵时，按自己数的字节数降速。
- **64 KB 的 TCP 窗口（同时加大 Wi-Fi 接收池和邮箱）让播放更流畅，但没有更快**：两次运行都是 0 次音频中断、0 次会话重置，而 32 KB 是 3 次和 9 次音频中断、各 1 次会话重置，帧率中位数相同。观看者也看到卡顿和撕裂更少。
- **流式条带接收路径腾出约 28 KB 堆**（空闲堆 43 KB 到 71 KB，最大连续块 25.6 KB 到 53 KB），并消除了"没有缓冲就丢包"的损失（`nobuf` 每 10 秒 10–12 次到 0），帧率没有可测的变化。
- 这推翻了此前记录的几条：“TCP 只能跑约 100 KB/s”“TCP 稳态约 8–10 fps”“停顿来自无线电，TCP 只是把它变成会话重置”。每次运行里出现的 120–150 KB/s 平台，就是限速器的预算。delta 编码看起来没有收益（第 3 节）也是同一个原因：限速器把 delta 省下来的字节，又花在了同样的每秒字节数上。
- 还没有证明：没有限速器的会话能跑过几分钟、在画面变化剧烈的频道上、在弱网下不出问题。见第 5 节。**当天晚些时候：**新的固定帧率服务端在真实 CCTV1 直播上，一个会话跑了 747 秒无故障，目标 25 fps，字节速率爬到 583 kB/s（写入耗时 31–78 ms）；设备两个 10 秒窗口是 24.5 和 24.1 fps，丢 4 帧，无音频中断、无重置。观看者在该码率下没有看到卡顿和撕裂。仍然只是一个频道、一条链路。

## 2. 已做的改动（工作区，均未提交）

| 文件 | 改动 |
|---|---|
| `main/av_player.c` | **流式条带接收。**接收任务先读包的长度表，再把每个条带直接读进 16 KB 字节环形缓冲（`AV_VIDEO_RING_BYTES`）；队列里每个条带一项（`AV_VIDEO_ITEMS` = 32）；画面任务从环里解压并释放（`ring_alloc`、`ring_release`）。替换掉两块 22 KB 整包缓冲（45 KB）。接收任务最多等 100 ms 取环或队列空位，取不到就丢弃这个条带并把该帧记为丢失（`nobuf`）。条带超过 4096 字节或长度表对不上会结束会话 |
| `sdkconfig.defaults`、`sdkconfig.av-prototype` | **现已合并（原 `tools/sdkconfig.win64`）：**`CONFIG_LWIP_TCP_WND_DEFAULT=65535`、`CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`、`CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`。该 overlay 文件已经多余 |
| `main/av_protocol.c/.h`、`main/av_player.c` | 产品路径用 **`av_expand_indexed_wire`**（每步四个像素，调色板由 `av_palette_wire_order` 按字节交换后存放）取代 `av_expand_indexed`；新增宿主机测试 `wire_expansion_tests`，硬件基准里加了设备上的等价性检查（`cpu_expand_wire`）。旧函数留给 demo 和测试 |
| `server/frames.py`、`server/live.py`、`server/rate.py`、`server/tv_server.py`、`server/media.py` | **已被服务端重构取代**，见交接文档。delta 现在默认开启（`TV_DELTA=1`，容差 1%），帧率按频道固定，每帧按目标字节数拟合。设备接受长度为 0 的条带。旧固件配新服务端，会在第一个零长度条带上结束会话 |
| `components/bsp/include/bsp_pins.h`、`main/av_demo.c`、`main/net_demo.c`、`main/jpeg_bench.c` 等 | 此前的测量工作，均不改变产品默认路径。只有 `bsp_pins.h`（面板时钟 80 MHz）会提交，仪器不提交 |

## 3. 实测

全部是真实的 CCTV1（`ch000`）直播，设备计数来自每 10 秒的 `interval_frames` 行（不足一帧的窗口已剔除）。`rx` 是设备每秒收到的字节。音频中断是 `AUDIO_EMPTY` 行，重置是 `RX_EXIT` 行。

**基线。**原始提交 `c61b764` 的固件与服务端，默认参数，办公室 Wi-Fi（ping 平均 57 ms，最长 165 ms）：中位 7.7 fps，每帧 18.7 KB，128 KB/s，150 秒内无重置。工作区关掉 delta 时的基线同样是 7.7，所以目前为止的改动没有让产品路径变差。

**流式条带对比整包缓冲**（电脑热点，ping 平均 34 ms，每段 100 秒，交替进行）：

| 固件 | fps 中位 | rx KB/s | 音频中断 | 重置 | 空闲堆 | 每 10 秒 `nobuf` |
|---|---|---|---|---|---|---|
| 整包缓冲（旧） | 9.5 / 10.8 | 143 / 140 | 8 / 5 | 1 / 0 | 43 KB | 10–12 |
| 流式条带（新） | 9.5 / 7.4 | 128 / 110 | 11 / 17 | 0 / 0 | 71 KB | 0 |

从这里看不出帧率差别；办公室 Wi-Fi 那次尝试更差（两者都是 2–4 fps），因为那一小时网络本身变差了。

**TCP 窗口**（新固件，热点，ping 平均 28 ms，每段 100 秒，交替进行）：

| 窗口 | fps 中位 | rx KB/s | 音频中断 | 重置 |
|---|---|---|---|---|
| 32 KB | 11.9 / 11.8 | 143 / 129 | 3 / 9 | 1 / 1 |
| 64 KB | 11.2 / 12.0 | 149 / 131 | 0 / 0 | 0 / 0 |

**限速器预算**（64 KB 固件，热点，原版服务端代码，每段 100 秒；每次运行里的那一次重置，是我为了开始抓取而复位板子）：

| `TV_VIDEO_BUDGET` / `TV_MAX_FPS` | fps 中位（平均） | rx KB/s | 音频中断 | 设备丢帧 |
|---|---|---|---|---|
| 185000 / 12 | 12.0（10.8）/ 12.0（10.5） | 128 / 133 | 0 / 0 | 0 / 0 |
| 280000 / 20 | 18.6（15.6）/ 20.0（16.2） | 224 / 194 | 0 / 0 | 0 / 0 |
| 限速器关闭，固定 25 fps | 24.3（23.3） | 349 | 0 | 0 |

开着限速器的平均值低于中位数，是因为从 `START_FPS=5` 起步的爬坡。限速器关闭的那次里有一个窗口掉到 18.7，其余都在 21–25。

**delta 编码，今天早些时候（办公室 Wi-Fi，限速器开着）：**关 7.8 fps，开 10.7 和 7.1，共两次运行。没有稳定收益，原因见第 1 节。

## 4. 途中犯过的错误（避免重犯）

- **第一次 A/B 无效。**`idf.py flash` 会先重新编译再刷写，结果"旧"版本用的是新源码，这些抓取已作废。改用 `esptool write_flash 0x10000 <bin>` 刷保存好的二进制。
- **无限速 TCP 的 `bulk` 测试不是天花板。**当时电脑的 Wi-Fi 网卡还连着办公室网络，`bulk` 运行期间热点 ping 到 300 ms、丢包 33–100%，设备只收到 12–39 KB/s，CPU 空闲 98%。它测的是被灌爆的热点，不是上限，不要引用。
- **"固定 32 KB 窗口封住吞吐"这个说法是错的**：64 KB 那次速率相同。窗口影响的是流畅度，不是速度。
- Wi-Fi 上不同时间点的运行不可直接比较（同一固件相隔一小时是 4.0 和 2.2 fps），要交替测。

## 5. 当前问题

1. **无限速运行只有一次 90 秒、一个频道。**需要几分钟的运行时长、多个频道（包括画面变化剧烈的），以及弱网。没有限速器，链路变差时没有东西会让发送端慢下来；用什么替换 185000 的预算（测得的数值，还是改成由写入耗时和设备侧信号驱动、而不是数字节）还没有定。不建议直接删掉它。
2. **起步爬坡。** *（默认发送器已解决：不再有帧率爬坡；字节速率从 250 kB/s 起步，每两个窗口涨 5%。）*
3. **环形缓冲的 100 ms 等待。**接收任务等环空位时最多等 100 ms，用的是 `vTaskDelay(1)`；等待期间它没有在读 socket，而音频也走这条路。目前没有测量显示它有害；画面变化剧烈的频道才可能暴露。
4. **64 KB overlay** *（已合并进 `sdkconfig.defaults` 和 `sdkconfig.av-prototype`；公开构建通过，运行时剩余堆 71 KB）*。还没有在擦除后的干净设备上跑过。
5. **流畅度是肉眼判断**（64 KB 下卡顿和撕裂更少）是观看者的观察；音频中断和重置次数支持它，但没有别的量化。
6. **delta 编码没有在限速器关闭时评估过**，也没有人看过它的画质。
7. **验证缺口。**没有运行 `tools/validate.sh`，没有运行宿主机 C 测试（本机没有原生 C 编译器）。`ring_alloc` 和 `ring_release` 是纯逻辑，但没有宿主机测试。设备端改动只通过了公开构建。
8. **此前就有的失败测试：**`tests/test_live_transcode.py` 1 项，`tests/test_tv_server.py` 2 项报错，`tests/test_live_sender_v2.py` 的 `test_heavy_video_preserves_audio_and_frame_protocol_on_slow_reader` 1 项；还原改动后同样失败。在这台机器上运行测试需要 `PYTHONPATH=.`。

## 6. 当前环境

- **板子现在连的是电脑热点，不是办公室网络。**NVS 里是热点的名字和这台电脑的热点地址（端口 8096）；办公室的配置备份在 `build-delta/ab/nvs_office_backup.bin`（被忽略的目录，不在仓库里）。要切回去，恢复这份备份，或用办公室的值运行 `tools/set_wifi_cred.py`（`tools/set_wifi_cred.py --list` 可以看现在存的是什么）。
- 板子运行的是 `build-fixed`（流式条带、默认配置里的 64 KB 窗口、线序展开函数、产品公开构建），2026-09-30 刷入。
- 最后留着运行的服务端是**工作区代码**：`TV_ADAPTIVE=0 TV_RATE_START=500000 TV_RATE_MAX=500000 python -m server.tv_server live --channel ch000 --bind <本机地址>`。**再启动之前先查 `netstat -ano | grep :8096`。上一个会话遗留的旧服务端曾悄悄占着这个端口，设备连的是它而不是新的那个，而两边的日志看起来都正常。**worktree `../ai-passport-tv-head`（提交 `c61b764`）仍然存在，用完用 `git worktree remove ../ai-passport-tv-head` 删除。
- 保存的旧固件二进制在 `build-prev-saved/`；构建目录与日志（`build-*`、`build-delta/ab/`）不入库。

## 7. 复现

```text
# 固件：产品公开构建（64 KB 窗口现在就在默认配置里）
idf.py -B build-fixed -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype" -D SDKCONFIG=build-fixed/sdkconfig build
# 在这台机器上通过 tools/idf-run.ps1 运行。刷写用：python -m esptool --chip esp32c3 -p COM4 -b 460800 write_flash 0x10000 build-fixed/FoloToy-AI-Passport.bin
# （只用分段刷写，不要 erase-flash）。之后若新增 overlay，要先删掉 <构建目录>/sdkconfig，否则不会生效。

# 服务端：当前的开关见交接文档。固定 500 kB/s，真实频道：
TV_ADAPTIVE=0 TV_RATE_START=500000 TV_RATE_MAX=500000 python -m server.tv_server live --channel ch000 --bind <本机地址>

# 测试
PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py && PYTHONPATH=. python tests/test_rate.py
```

```text
Build: PASS（产品公开构建，流式条带；64 KB 窗口；线序展开函数）
Host tests: 早些时候 12 项 delta 测试 PASS；其余 4 项 Python 测试在未改动的代码上同样失败；C 宿主机测试 NOT RUN；tools/validate.sh NOT RUN
Device tests: 真实 CCTV1 直播上的 90–150 秒单次运行，见第 3、4 节
Unverified: 无限速运行的几分钟时长、其他频道、画面变化剧烈的内容与弱网；环形缓冲在负载下对音频的影响；delta 画质；64 KB overlay 进入发布配置
```
