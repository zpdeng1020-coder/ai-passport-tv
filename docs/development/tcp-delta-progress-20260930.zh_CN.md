<p align="right">
  <strong>简体中文</strong> · <a href="tcp-delta-progress-20260930.md">English</a>
</p>

# TCP 方案优化进度与当前问题（2026-09-30）

> **更新（2026-10-01，按代码核对）。**本文按提交 `208357b` 的代码重新核对过：设备端改动（流式条带、64 KB 窗口、线序展开函数）已在 `ed79a05` 提交；服务端已改成按频道固定帧率、每帧拟合字节目标（`e92e422`、`a4510d7`），不再是第 2、5、6 节原来描述的样子。服务端的做法与未决问题见 [server-optimisation-handoff-20260930.zh_CN.md](server-optimisation-handoff-20260930.zh_CN.md)。第 1、3、4 节的测量是当时的单次运行，没有重测。
>
> **测量仪器已入库**（`c74b1a6`）：`main/av_demo.c`、`main/net_demo.c`、`main/jpeg_bench.c`、`tools/sdkconfig.*` overlay、`tools/frame_size_lab.py` 和素材生成脚本都在仓库里，Kconfig 里默认关闭（`AV_HW_BENCH`、`AV_DEMO_PLAYBACK`、`AV_NET_DEMO`、`AV_JPEG_BENCH` 均为 `default n`）。生成出来的素材（`main/demo_clip.bin`、`main/jpeg_test.bin`、`tools/clips/`）不入库，由脚本重新生成。三份结果文档也已入库：[本机播放上限](device-playback-limit-20260929.zh_CN.md)、[网络播放实测结果](network-playback-results-20260929.zh_CN.md)、[网络验证交接](handoff-network-validation-20260929.zh_CN.md)。

UDP 传输改造（见[网络播放实测结果](network-playback-results-20260929.zh_CN.md)）**已搁置**：它要同时改设备接收任务、服务端发送路径、`av_stream_accept` 的严格 `seq` 检查和音频流控，而且观看者认为它的画面和声音不可用（即使帧率到 20 以上也有撕裂和断续）。本文记录**产品 TCP 路径**上已经做了什么、测到什么、还有什么问题，后续 TCP 方向的优化以本文为起点。每个数字都是**单次运行**。

## 1. 目前的结论

本节和第 3 节里的“限速器”指旧的 `rate.py` 控制器（按 `VIDEO_BUDGET_BYTES` 推帧率上限）。默认发送器现在不用它：`TV_ADAPTIVE` 默认关（`server/rate.py:187`），每帧的字节目标默认是 `TV_FRAME_BYTES=20000`（`server/rate.py:615`）；旧控制器只剩 `TV_LIVE_ENGINE=v2` 的发送器还在用（见 `server/rate.py:601` 的注释）。

- **产品路径上的帧率上限是服务端的限速器，不是链路、TCP 窗口或设备。**`server/rate.py` 的天花板按 `VIDEO_BUDGET_BYTES ÷ 每帧字节数` 推出，而预算（185000 B/s）按它自己的注释，只是一个从未作为链路上限测过的工作点。用原版服务端跑真实的 CCTV1 直播，把限速器换成固定速率（`TV_ADAPTIVE=0 TV_FPS=25`），设备画出 **24.3 fps（中位）**，接收约 349 KB/s，无音频中断、无丢帧、无会话重置（90 秒，单次运行）。这就是素材本身的帧率。
- 预算提到 280000 得到 18.6–20.0 fps，默认值是 12.0。开着限速器的每次运行，窗口结尾都是 `sent 18x kB in the window`，写入耗时只有 16–78 ms，远低于 250 ms 的慢写线：限速器是在链路并不拥堵时，按自己数的字节数降速。
- **64 KB 的 TCP 窗口（同时加大 Wi-Fi 接收池和邮箱）让播放更流畅，但没有更快**：两次运行都是 0 次音频中断、0 次会话重置，而 32 KB 是 3 次和 9 次音频中断、各 1 次会话重置，帧率中位数相同。观看者也看到卡顿和撕裂更少。
- **流式条带接收路径腾出约 28 KB 堆**（空闲堆 43 KB 到 71 KB，最大连续块 25.6 KB 到 53 KB），并消除了"没有缓冲就丢包"的损失（`nobuf` 每 10 秒 10–12 次到 0），帧率没有可测的变化。
- 这推翻了此前记录的几条：“TCP 只能跑约 100 KB/s”“TCP 稳态约 8–10 fps”“停顿来自无线电，TCP 只是把它变成会话重置”。每次运行里出现的 120–150 KB/s 平台，就是限速器的预算。delta 编码看起来没有收益（第 3 节）也是同一个原因：限速器把 delta 省下来的字节，又花在了同样的每秒字节数上。
- 还没有证明：没有限速器的会话能跑过几分钟、在画面变化剧烈的频道上、在弱网下不出问题。见第 5 节。**当天晚些时候：**新的固定帧率服务端在真实 CCTV1 直播上，一个会话跑了 747 秒无故障，目标 25 fps，字节速率爬到 583 kB/s（写入耗时 31–78 ms）；设备两个 10 秒窗口是 24.5 和 24.1 fps，丢 4 帧，无音频中断、无重置。观看者在该码率下没有看到卡顿和撕裂。仍然只是一个频道、一条链路。

## 2. 已做的改动（均已提交）

| 文件 | 改动 |
|---|---|
| `main/av_player.c` | **流式条带接收。**接收任务先读包的长度表，再把每个条带直接读进 16 KB 字节环形缓冲（`AV_VIDEO_RING_BYTES`）；队列里每个条带一项（`AV_VIDEO_ITEMS` = 32）；画面任务从环里解压并释放（`ring_alloc`、`ring_release`）。替换掉两块 22 KB 整包缓冲（45 KB）。接收任务最多等 100 ms 取环或队列空位，取不到就丢弃这个条带并把该帧记为丢失（`nobuf`）。条带超过 4096 字节或长度表对不上会结束会话；队列满 200 ms 仍放不进去也会结束会话（`video_enqueue`） |
| `sdkconfig.defaults`、`sdkconfig.av-prototype` | **现已合并（原 `tools/sdkconfig.win64`）：**`CONFIG_LWIP_TCP_WND_DEFAULT=65535`、`CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`、`CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`。该 overlay 文件已经删除 |
| `main/av_protocol.c/.h`、`main/av_player.c` | 产品路径用 **`av_expand_indexed_wire`**（每步四个像素，调色板由 `av_palette_wire_order` 按字节交换后存放）取代 `av_expand_indexed`；新增宿主机测试 `wire_expansion_tests`（`tests/test_av_protocol.c`），硬件基准里加了设备上的等价性检查（`cpu_expand_wire`）。旧函数留给 demo 和测试 |
| `server/frames.py`、`server/live.py`、`server/rate.py`、`server/tv_server.py`、`server/media.py` | **已被服务端重构取代**，见交接文档。delta 现在默认开启（`TV_DELTA=1`，容差 1%），帧率按频道固定，每帧按目标字节数拟合。设备接受长度为 0 的条带（`av_player.c` 里长度为 0 的条带直接入队）。旧固件配新服务端，会在第一个零长度条带上结束会话（原文记录，没有在旧固件上重测）；当前固件没有这个问题 |
| `components/bsp/include/bsp_pins.h`、`main/av_demo.c`、`main/net_demo.c`、`main/jpeg_bench.c` 等 | 面板时钟 80 MHz（`BSP_LCD_PCLK_HZ`）在 `ed79a05` 提交。测量仪器在 `c74b1a6` 提交，Kconfig 默认关闭，按提交说明不改变产品路径 |

## 3. 实测

全部是真实的 CCTV1（`ch000`）直播，设备计数来自每 10 秒的 `interval_frames` 行（不足一帧的窗口已剔除）。`rx` 是设备每秒收到的字节。音频中断是 `AUDIO_EMPTY` 行，重置是 `RX_EXIT` 行。

本节的数字都是在固定字节目标的服务端之前取得的，不代表当前默认发送路径，也没有重测。

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

按提交 `208357b` 的代码核对过。标“原文记录”的是旧文档的说法，本次没有重新验证。

1. **链路变差时没有东西让发送端慢下来，设备也没有任何反馈。**默认发送器按固定字节目标发送（`TV_FRAME_BYTES` 默认 20000，`TV_ADAPTIVE` 默认关，`server/rate.py:187`、`:615`）；自适应控制器要 `TV_ADAPTIVE=1` 才启用（步长 ×1.05 / ×0.85，`rate.py:630-631`）。设备侧整个 `av_player.c` 只有一处 send，就是会话开头的 HELLO（`av_player.c:1134`），之后不回报丢帧、积压或接收速率，服务端只能从写入耗时和丢帧数推断。用什么信号替代原来的预算，仍然没有定。
2. **起步爬坡。**默认（固定）模式没有爬坡；字节速率只在 `TV_ADAPTIVE=1` 时升降。
3. **环形缓冲的等待会挡住音频读取。**`av_player.c:1290-1294`：接收任务给一个非空条带取环空位时，最多等 `AV_VIDEO_WAIT_US`（100 ms，`:560`），每次用 `vTaskDelay(1)` 轮询；等待期间它不读 socket，而音频与视频走同一条 TCP 流。等不到就丢弃这个条带的字节，该帧记为丢失并计入 `nobuf`。另外队列满时 `video_enqueue` 有 200 ms 的上限，超时会结束会话（`:583-591`、`:1319`）。这是从代码读出来的，没有测量显示它在真实负载下造成过问题；要看只能读设备日志里的 `nobuf` 和 `AUDIO_EMPTY`。
4. **设备端帧率上限是 30，没有被探过。**`config_valid` 拒绝 `fps` 超出 1–30 的 CONFIG（`av_player.c:946`），按它的注释是为了防止服务端宣布面板画不出来的帧率。已有的运行都在这个上限之内，设备能不能更快没有测过。
5. **64 KB 窗口的配置**已合并：`sdkconfig.defaults` 里是 `CONFIG_LWIP_TCP_WND_DEFAULT=65535` 和 `CONFIG_LWIP_TCP_RECVMBOX_SIZE=48`，`sdkconfig.av-prototype` 里是 `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=36`；`tools/sdkconfig.win64` 已不存在。原文记录公开构建通过、运行时剩余堆 71 KB；没有在擦除后的干净设备上跑过。
6. **流畅度是肉眼判断**（64 KB 下卡顿和撕裂更少）是观看者的观察；音频中断和重置次数支持它，但没有别的量化。
7. **delta 编码默认开启**（`TV_DELTA` 默认 `1`，容差 0.01，`server/frames.py:197-198`）。原文记录没有人看过它的画质，本次也没有看。
8. **验证状况。**本次在当前提交上重跑：`test_rate` 45 项、`test_delta_encoding` 25 项、`test_fixed_rate` 26 项、`test_perceptual` 8 项，全部通过。没有运行 `tools/validate.sh`；这台机器上找不到 `gcc`、`cc`、`clang`，所以宿主机 C 测试没有跑（`tests/test_av_protocol.c` 第 191 行有 `wire_expansion_tests`）。`ring_alloc` 和 `ring_release` 是 `static` 的纯逻辑，没有宿主机测试。
9. **此前就有的失败测试，今天重跑仍是 4 项：**`tests/test_live_transcode.py` 的 `test_live_mode_signal_handler_stops_the_accept_loop`；`tests/test_tv_server.py` 2 项报错（原文记为 `test_environment_and_restricted_token_file`、`test_fragmented_and_coalesced_stream`，本次只看到输出末尾，没有逐项核对名字）；`tests/test_live_sender_v2.py` 的 `test_heavy_video_preserves_audio_and_frame_protocol_on_slow_reader`。原文说还原改动后同样失败，这一点本次没有重新验证。在这台机器上运行测试需要 `PYTHONPATH=.`。

## 6. 环境注意事项

- 此前这一节记录的是某一台机器上的状态（板子连的是哪个 Wi-Fi、NVS 里存的是什么、最后留着运行的服务端、`../ai-passport-tv-head` worktree），不是仓库里的事实，已删除。`git worktree list` 现在只有主工作区。
- 仍然成立的一条教训：启动服务端之前先查端口（`netstat -ano | grep :8096`）。上一个会话遗留的旧服务端曾悄悄占着这个端口，设备连的是它而不是新的那个，而两边的日志看起来都正常。
- 写入设备的 Wi-Fi 配置用 `tools/set_wifi_cred.py`。构建目录与日志（`build-*`）不入库。

## 7. 复现

```text
# 固件：产品公开构建（64 KB 窗口现在就在默认配置里）
idf.py -B build-fixed -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype" -D SDKCONFIG=build-fixed/sdkconfig build
# 在这台机器上通过 tools/idf-run.ps1 运行。刷写用：python -m esptool --chip esp32c3 -p COM4 -b 460800 write_flash 0x10000 build-fixed/FoloToy-AI-Passport.bin
# （只用分段刷写，不要 erase-flash）。之后若新增 overlay，要先删掉 <构建目录>/sdkconfig，否则不会生效。

# 服务端：默认就是固定字节目标（每帧 TV_FRAME_BYTES，默认 20000）。选一个当前能播的频道。其余开关见交接文档。
python -m server.tv_server live --channel <频道 id> --bind <本机地址>

# 测试
PYTHONPATH=. python tests/test_rate.py && PYTHONPATH=. python tests/test_delta_encoding.py && PYTHONPATH=. python tests/test_fixed_rate.py && PYTHONPATH=. python tests/test_perceptual.py
```

```text
Build: NOT RUN（这次只改文档）；原文记录产品公开构建 PASS（流式条带；64 KB 窗口；线序展开函数）
Host tests: 2026-10-01 重跑 test_rate 45、test_delta_encoding 25、test_fixed_rate 26、test_perceptual 8，全部 PASS；test_live_transcode 1 项失败、test_tv_server 2 项报错、test_live_sender_v2 1 项失败，与原文记录的此前失败一致；C 宿主机测试 NOT RUN（没有 C 编译器）；tools/validate.sh NOT RUN
Device tests: 这次没有上板；第 3、4 节是原文记录的真实 CCTV1 直播 90–150 秒单次运行
Unverified: 环形缓冲的 100 ms 等待在真实负载下对音频的影响；设备帧率高于 30 的表现；delta 画质；64 KB 配置在擦除后的干净设备上；弱网和画面变化剧烈的内容
```
