<p align="right">
  <strong>简体中文</strong> · <a href="handoff-network-validation-20260929.md">English</a>
</p>

# 交接（2026-09-29）：网络播放性能验证 demo 方案

前置阅读：[本机播放性能实测](device-playback-limit-20260929.zh_CN.md)（无网络的整链路天花板）与[基准结果](hardware-benchmark-results-20260929.zh_CN.md)（部件级）。本文只做一件事：给出**下一步——在网络上验证同样的性能**——的方案。

## 1. 前提，以及一个更正

产品当前 12 fps，**这不是需求，是未优化下被迫设的上限**：`server/rate.py` 的 `MAX_FPS`（默认 12，环境变量 `TV_MAX_FPS`）与 `VIDEO_BUDGET_BYTES`（默认 185000 字节/秒，`TV_VIDEO_BUDGET`）。本机实测规格内（40 MHz）设备一侧能画 **40.2 fps**，与内容无关。所以目标不是"保住 12"，而是**找出网络路径上真实的天花板**，再据此重设这两个数。

## 2. 已知的事实（引用，不重述推导）

- 本机整链路：40 MHz 下面板是墙（40.2 fps）；CPU 每帧 13–18 ms，占 24.9 ms 的约 64%。
- 链路：产品参数下 iperf TCP 接收 31.6 Mbit/s（约 3.9 MB/s）。40 fps × 18 KB/帧 ≈ 720 KB/s ≈ 5.8 Mbit/s，链路不是首要嫌疑。
- 接收：实测约 29 周期/字节（基准文件第 3 节）。18 KB/帧 × 29 = 0.52 M 周期 ≈ **3.3 ms/帧**（160 MHz）。
- 把三段加起来估算 40 fps 的 CPU 需求：解压 10–15 + 展开 2.6 + 接收 ≤3.3 ≈ 16–21 ms，对 24.9 ms 的预算只有几毫秒的余量。**这是算术，没有测过**；音频 I2S 写入与 Wi-Fi 驱动的时间还没算进去。所以网络上的实际天花板很可能低于 40，值得测出来，而不是假设。
- 之前的端到端结果：9 fps 稳住时设备只忙了 23.5%；12 fps 那次失败是**探针的**（探针日志 `packet deadline expired`，设备端是 `EOF`），不是设备极限。所以**网络路径的上限至今没有被测出来**，只知道"至少 9 fps"。

## 3. 一个必须先看的约束：预算数字本身就封顶

`VIDEO_BUDGET_BYTES = 185000` 字节/秒。在 40 fps 下每帧只能有 **约 4.6 KB**——低于本机 MID 档（13.6 KB）和 HIGH 档（18.0 KB）。不改这个数，服务端的自适应会先把帧率压下去，网络永远测不到设备的极限。所以验证时两个环境变量要同时抬：

```text
TV_MAX_FPS=40   TV_VIDEO_BUDGET=<fps × 单帧上限>   # 例：24 fps × 22528 ≈ 540000
```

单帧上限是协议给的：`AV_VIDEO_MAX = 22528` 字节，一帧一个包，超了会被拆包，而拆包的帧会因视频队列深度只有 2（`AV_VIDEO_BUFFERS`）被整帧丢弃（`main/av_protocol.h` 的注释里有实测）。**本机 HIGH 档最大帧 34.9 KB 就在这条线之外**，不能直接拿来当网络负载。

## 4. 方案：三个阶段，逐步把变量放进来

每阶段只引入一个新变量，前一阶段不过就不进下一阶段。

### 阶段 A：固定负载，只测链路与接收（首选起点）

复用 `tools/transport_probe.py`（它已经说同一套线协议、自己生成合法的帧，并有 `--ramp` 阶梯）。新增的一点：让它**读 `main/demo_clip.bin`**——该文件里每一帧本来就是线上的视频载荷，`server/frames.py` 的格式，无需再压缩。只发 LOW 与 MID 两档（丢掉超过 22528 字节的帧，并在输出里报告丢了几帧）。

这样得到的对照是干净的：**同样的字节，本机 demo 画 40.2 fps；网络上画几 fps？差多少就是网络路径的代价**，且没有 ffmpeg、没有调度器、没有内容变化夹在中间。

- 阶梯：6 → 9 → 12 → 16 → 20 → 24 → 30 → 40 fps，每档保持 ≥40 秒（沿用之前的做法）。
- 用 `--audio`（必须，否则测的是握手不是链路），音频在包之间插入而不是只在帧之间（见探针文件头注释）。
- 每档读设备端的 10 秒区间行：`rx_pkts`、`rx_bps`、`io_ms`、`iters`，以及画出的帧数、`nobuf`、丢帧与迟到、面板+解码占用率。**含义以 `metrics-dictionary.zh_CN.md` 为准，不要凭字段名猜。**
- 通过标准：某一档连续 ≥40 秒 `interval_frames` 等于该档 fps、`nobuf`≈0、无会话重置。最高的通过档就是这一阶段的天花板。

### 阶段 B：真实服务端，真实内容

在 A 有了天花板之后，用 `server/tv_server.py` 起真实频道，用第 3 节的两个环境变量把上限抬到 A 测出的天花板附近，测**送达帧率、丢帧、会话重置**。A 与 B 的差就是服务端（ffmpeg 转码、限速器、时间线）的代价。

- B 不通过而 A 通过：问题在服务端一侧。看 `server/rate.py` 的自适应是否又把帧率压了回去，以及 `docs/development/state-20260916.md` 里已知的音画同步问题。
- 每次测量都要记下当时的 `TV_*` 环境变量，否则数字无法复现。

### 阶段 C（可选）：设备侧优化，只在 A 显示 CPU 是瓶颈时做

- 把 `expand_fast` 换进 `main/av_player.c` 的 `push_stripe`（本机 demo 已在设备上核对与原版逐字节一致，14.15 → 7.61 周期/像素）。改动小，但**产品路径还没换过，也没有网络下的数据**。
- 单窗口送屏（`bsp_display_raw_window_*`）：基准文件里只在测量构建里用过，产品路径没有；改动是侵入性的，需要先看 A 的 `panel` 等待段是否真的在挡路。

## 5. 什么读数意味着什么（假设，等 A 验证）

以下是推测，不是结论，写下来是为了让读数出来时有地方对：

| 读数 | 更可能的指向 |
|---|---|
| `nobuf` 上升，`io_ms` 大 | 接收跟不上：视频队列（深度 2）在等；查接收任务优先级与包大小 |
| 音频欠载 / `AUDIO_EMPTY` 增加 | 读音频被图像包占住（一个任务读两路），见 `AUDIO_SILENCE_MAX_MS` 的注释 |
| 面板占用率接近 100% 而帧率低 | 面板总线（40 MHz 下 40.2 fps 是硬顶） |
| 会话重置（`RX_EXIT`） | 先分清是谁先挂断：探针日志与设备日志要对读，本项目在这里犯过一次错 |

## 6. 不要做的事

- 面板时钟现在是 80 MHz。上游已经采用（提交 `20668230`，`components/bsp/include/bsp_pins.h` 里的 `BSP_LCD_PCLK_HZ`），本 fork 已与之一致。本文前面和 [device-playback-limit-20260929.zh_CN.md](device-playback-limit-20260929.zh_CN.md) 曾把它当作超规格、未验证的配置，这一点已不再成立。那两份文档里的 40 MHz 数字（40.2 fps 的面板墙、24.9 ms 一帧）描述的是旧时钟，本机 demo 没有在 80 MHz 下重测；80 MHz 下的网络路径数字见 [network-playback-results-20260929.zh_CN.md](network-playback-results-20260929.zh_CN.md)。
- 不要用 HIGH 档原始帧做网络负载（超过单包上限）。
- 不要在单次运行上下结论；每档至少 40 秒，关键结论重复一次。
- 不要在"设备离极限还差得远"时把失败归给设备——先验证是不是仪器（探针、Wi-Fi 环境、服务端限速）。

## 7. 环境与命令（这台 Windows 机器上）

- 激活 ESP-IDF 只能走 `tools/idf-run.ps1`（Git Bash 下 `idf.py` 直接跑不起来，原因在脚本头注释里）。
- 构建 demo：`idf.py -B build-demo -D AV_PUBLIC_BUILD=ON "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype;tools/sdkconfig.playback-demo" -D SDKCONFIG=build-demo/sdkconfig build`。`BSP_LCD_PCLK_HZ` 现在就是 80 MHz，所以这样构建出来就是 80 MHz。`CONFIG_AV_HW_BENCH_SPI80` 与 `*-spi80` overlay 已不存在（它们会把这个常量翻倍，要求 160 MHz）。
- 刷写只用分段的 `idf.py flash`，不要 `erase-flash`，不要把合并镜像原始写到 `0x0`（会碰 `cardid`，见 `protected-flash-layout.zh_CN.md`）。
- 板子在 COM4（USB Serial/JTAG）。
- 本机没有原生 C 编译器，`tools/validate.sh --static` 跑不了；我装过一个 ziglang 放在 `build-hosttools/`（已被 `.gitignore` 覆盖），能编译 C，但**没有用它跑过宿主测试**。

## 8. 尚未做的事（诚实清单）

- **已被 [network-playback-results-20260929.zh_CN.md](network-playback-results-20260929.zh_CN.md) 取代，那里记录了实际测到的结果和仍然存在的瓶颈。**上面的方案没有按原样执行：用一个最小的接收程序和服务端（`main/net_demo.c`、`tools/net_demo_server.py`）替换了产品播放器和探针，并用 UDP 替换了 TCP。
- `tools/transport_probe.py` 现在能读 `demo_clip.bin`（`--clip`）。阶段 B（真实服务端）没有运行。
- 设备端 `CONFIG` 接受 1 到 30 的 `fps`（`main/av_player.c` 里的 `json_between(j,"fps",1,30)`），所以服务端不能向产品播放器宣告超过 30 fps。
- 未跑 `tools/validate.sh`；本次改动未更新 `docs/CHANGELOG.md`（内部测量仪器，按规则可不记）；本次所有改动尚未提交。

```text
Build: PASS（demo 在 40 MHz 与 80 MHz 下，公开构建，均在 3 MB 上限内；构建时常量还不是 80 MHz）
Host tests: NOT RUN（本机无原生 C 编译器）
Device tests: PASS 仅限本机 demo 的帧率与展开自检；网络路径 NOT RUN
Unverified: 见第 8 节；80 MHz 带优化展开后的画面未单独确认
```
