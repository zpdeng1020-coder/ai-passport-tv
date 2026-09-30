<p align="right">
  <strong>简体中文</strong> · <a href="architecture-index.md">English</a>
</p>

# 架构索引（仅限本 fork 的导航辅助文档）

**用途。** 本文件是一张地图，不是规则集：目的是让本 fork 收到一个新需求时能一次定位到该改哪个文件，而不必重新通读整棵代码树。它不新增也不改变任何规则——[`AGENTS.md`](../../AGENTS.md) 仍是约定、构建命令与安全基线的唯一事实来源。先读本文件找"在哪";再读 `AGENTS.md` 自己的路由表和相邻代码找"怎么做"。

按 [`docs/fork-guide.md`](../fork-guide.md)（`docs/assets/` 用于存放"架构笔记"的那条约定），本文件属于 fork 私有内容，不向上游提交。

**本文件自身的维护规则。** 每当 `main/` 或 `server/` 新增模块，或出现下表未覆盖的任务类型时，就更新本文件。每条条目只写一行 + 一个链接；把理由写在它指向的代码注释或文档里，绝不写在这里——重复的解释就是第二个事实来源，一定会 drift。如果本文件与代码冲突，以代码为准，回来修本文件。

## 1. 一个入口背后的两种固件形态

[`main/main.c`](../../main/main.c) 在 `app_main()`（约第 108 行）里，根据
`CONFIG_AV_RAW_PROTOTYPE`（声明于 [`main/Kconfig.projbuild`](../../main/Kconfig.projbuild)）
在两套完全不同的程序之间二选一：

| 形态 | 开启方式 | 入口 | 是什么 |
| --- | --- | --- | --- |
| BSP 能力演示菜单（上游基线） | 默认（`CONFIG_AV_RAW_PROTOTYPE=n`） | `app_main()` 直接搭建 LVGL 菜单 | 板级能力演示：Display/Button/Audio/Battery/Wi-Fi/BLE/Low-Power 各页。本 fork 基本不动它；沿用 `AGENTS.md` 自己的路由表即可。 |
| 电视播放原型（本 fork 的产品） | `sdkconfig.av-prototype`（`CONFIG_AV_RAW_PROTOTYPE=y`） | [`main/av_player.c`](../../main/av_player.c) 中的 `av_player_main()` | 本 fork 实际发布的网络电视固件。几乎所有 fork 需求都落在这里或 `server/`。 |

本 fork 自己的开发几乎全在第二行。本索引其余部分都围绕它组织。

## 2. 设备固件：`main/av_player.c`（约 3600 行——用 grep 定位，不要通读）

一个文件拥有整个会话：socket、三个 FreeRTOS worker、配网流程和叠加菜单。直接跳到对应函数，不要从头翻：

| 关注点 | 函数 / 符号 | 备注 |
| --- | --- | --- |
| 线协议常量、包头结构体、索引图解码 | [`main/av_protocol.h`](../../main/av_protocol.h) / `.c` | 无 ESP-IDF 依赖，可跑 host test。每个魔法数字都有带实测数据的"为什么"注释——改数字前先读注释。 |
| Wi-Fi 拉起 | `wifi_init()` | 初始化 NVS + `av_provision_*`。 |
| 启动流程、模式判定、主循环 | `av_player_main()` | 负责配网 vs 播放的判定（`av_boot_mode()`）、音量/亮度恢复、每会话重置、任务创建顺序。 |
| 配网 / captive portal 流程 | `run_setup_mode()` | 绘制配网屏；与 `av_provision_*` 交互。 |
| 握手（HELLO/CONFIG） | `hello()`、`config_valid()` | 按 `av_protocol.h` 里的常量校验服务端的 CONFIG JSON。 |
| socket 接收循环 | `receive_task()` | 任务优先级最高（7），是刻意的——为什么这个顺序相对 audio/video 是硬约束，见文件末尾 `xTaskCreate` 调用上方的注释。 |
| 音频播放 | `audio_task()` | 拥有 I2S；音频是本会话的时钟基准（等价于 `estimated_pts()`）。 |
| 视频解码与绘制 | `video_task()`、`push_stripe()`、`enlarge_stripe()`、`overlay_stripe()` | 拥有裸屏；解压 → 索引转 RGB565 → 可选放大 → 叠加层 → DMA 提交。 |
| 换台请求 | `request_switch()`、`process_key()` | 按键手势 → 待切换频道 id；在主循环里随会话重启时统一应用。 |
| 会话生命周期 | `allocate_session()`、`free_session()`、`session_drain()` | 缓冲区归属与排空/超时规则，详见 `docs/local-tv-prototype.md`。 |
| 设备端截图（调试用） | `shot_take()`/`shot_print()`（受 `CONFIG_AV_SCREEN_CAPTURE` 保护） | 默认关闭；见 Kconfig 帮助文字。 |

配套模块，均已可在 host 端测试，且已被 `tools/validate.sh --static` 覆盖：

| 文件 | 负责什么 |
| --- | --- |
| [`main/av_channel_policy.c`/`.h`](../../main/av_channel_policy.h) | "这个频道是否出过画面"的记录，以及跳到下一频道的决策。 |
| [`main/av_provision.cpp`/`.h`](../../main/av_provision.h) | Wi-Fi station/AP 生命周期（C++，封装 `esp-wifi-connect`）。 |
| [`main/av_provision_policy.c`/`.h`](../../main/av_provision_policy.h) | 纯启动模式判定（`AV_BOOT_PLAY` vs `AV_BOOT_SETUP`），无 ESP-IDF 依赖。 |
| [`main/av_settings.c`/`.h`](../../main/av_settings.h) | 亮度/音量的分档表（固定档位而非连续值）。 |
| [`main/av_store.c`/`.h`](../../main/av_store.h) | 音量/亮度/服务器地址的 NVS 持久化。 |
| [`main/av_server_addr.c`/`.h`](../../main/av_server_addr.h) | 解析配网页填的 "host[:port]" 文本。 |
| [`main/ui_menu.c`/`.h`](../../main/ui_menu.h) | 频道列表/状态页/亮度叠加层的状态机——纯逻辑，无 LVGL。 |
| [`main/ui_text.c`/`.h`](../../main/ui_text.h) | 直接画到屏幕条带上的位图文字（非 LVGL）；CJK 字形来自 `ui_font_cjk.bin`，由 `tools/make_font.py` 重新生成。 |
| [`main/ui_pixel.c`/`.h`](../../main/ui_pixel.h) | 共用的视觉主题（天空/草地/吉祥物/面板），BSP 演示菜单也在用。按 `docs/development/ai-guide.md` 要求保持完整。 |

## 3. 服务端：`server/`（纯 Python 标准库）

| 文件 | 负责什么 |
| --- | --- |
| [`server/tv_server.py`](../../server/tv_server.py) | 入口；`live`/`run`/`prepare`/`import-video` 子命令；单连接会话循环。 |
| [`server/live.py`](../../server/live.py) | 直播频道的 ffmpeg 管线、`CHANNELS` 表（从 `channels.txt` 加载）、调色板处理。 |
| [`server/protocol.py`](../../server/protocol.py) | `main/av_protocol.h` 线协议的 Python 镜像。两边要保持一致；有测试交叉核对共用常量。 |
| [`server/frames.py`](../../server/frames.py) | 索引图的条带切割/压缩——是 `av_expand_indexed`/`push_stripe` 的服务端对应物。 |
| [`server/timeline.py`](../../server/timeline.py) | 内容时间 vs 到达时间、`SessionClock`。**任何音画同步或时序问题先读这里**；本区域当前"已修/未修/已测"的状态见 `docs/development/state-20260916.md`。 |
| [`server/pts.py`](../../server/pts.py) | 从同一次解码中推导两路流各自的时间戳，而不是靠计帧数。 |
| [`server/rate.py`](../../server/rate.py) | 会话运行期间的自适应视频码率/帧率选择。 |
| [`server/media.py`](../../server/media.py) | `prepare`/`import-video`：ffmpeg 调用、几何/黑边规则。 |
| [`server/netident.py`](../../server/netident.py) | 推算本机自身可达地址，打印给设备用。 |
| [`server/usb_link.py`](../../server/usb_link.py) | 在 USB 串口传输上包一层 socket 形状的适配器（仅用于测量build）。 |
| [`server/live_sender.py`](../../server/live_sender.py) | opt-in 的 v2 发送器（`TV_LIVE_ENGINE=v2`）；动它之前先看 [`docs/development/live-sender-v2.md`](../development/live-sender-v2.zh_CN.md) 的范围与边界。 |
| [`server/fault.py`](../../server/fault.py) | 面向受控设备实验（B02-R）的故障注入工具；不在正式发布的服务端路径里。 |

## 4. 做需求时真正会用到的工具

`tools/` 下文件很多，这些是需求最常用到的：

| 需求 | 工具 |
| --- | --- |
| 在浏览器里增删/排序频道列表 | [`tools/channel_config.py`](../../tools/channel_config.py)（写 `channels.txt`） |
| 一起启动服务端和频道配置页 | [`tools/launch.py`](../../tools/launch.py)（`run.sh`/`run.bat` 是它的包装） |
| 把服务端打包成单个可执行文件 | [`tools/build_server.py`](../../tools/build_server.py)、[`packaging/tv-server.spec`](../../packaging/tv-server.spec)、[`.github/workflows/build-server.yml`](../../.github/workflows/build-server.yml) |
| 构建/校验固件镜像 | [`tools/validate.sh`](../../tools/validate.sh) `--prototype`、[`tools/verify_firmware.py`](../../tools/verify_firmware.py)、[`sdkconfig.av-prototype`](../../sdkconfig.av-prototype) |
| 给测试机的 NVS 写入 Wi-Fi/服务器地址 | [`tools/set_wifi_cred.py`](../../tools/set_wifi_cred.py)、[`tools/set_server_addr.py`](../../tools/set_server_addr.py) |

## 5. 线协议——一份契约，写在四个地方

改这里任何东西都要按下面顺序更新全部四处（先改设备端结构体，因为它是已刷设备无法再协商的部分）：

1. [`main/av_protocol.h`](../../main/av_protocol.h)——设备端权威结构体/常量。
2. [`server/protocol.py`](../../server/protocol.py) + [`server/frames.py`](../../server/frames.py)——服务端镜像。
3. [`docs/local-tv-prototype.md`](../local-tv-prototype.zh_CN.md)——设备侧叙述文档（归属、生命周期、时序预算）。
4. [`server/README.md`](../../server/README.zh_CN.md)——服务端叙述文档（调度、边界、关闭流程）。

## 6. 已经整理好上下文的文档——直接打开，不要重新推导

| 文档 | 什么时候读 |
| --- | --- |
| [`docs/development/handoff-20260928.md`](../development/handoff-20260928.zh_CN.md) | **做性能优化或换架构之前必读。**硬件推导的理论上限与真机基准计划。 |
| [`docs/development/tcp-delta-progress-20260930.md`](../development/tcp-delta-progress-20260930.zh_CN.md) | **继续做产品 TCP 路径优化之前读。**产品的帧率上限是限速器的预算而不是链路；流式条带接收、64 KB 窗口、delta 编码、当前问题。UDP 改造已搁置。 |
| [`docs/development/server-optimisation-handoff-20260930.md`](../development/server-optimisation-handoff-20260930.zh_CN.md) | **做服务端或画质相关工作前。**按频道固定帧率、逐帧拟合字节目标、测到了什么和没测到什么、按顺序的下一步。 |
| [`docs/development/state-20260916.md`](../development/state-20260916.zh_CN.md) | **动时序/会话/画质之前必读。** 当前已修/未修/已测的状态；动手前先跟代码核对。 |
| [`docs/development/metrics-dictionary.md`](../development/metrics-dictionary.zh_CN.md) | 使用或打印任何计数器之前——它到底在数什么，不能被读成什么。 |
| [`docs/development/live-sender-v2.md`](../development/live-sender-v2.zh_CN.md) | 动 `server/live_sender.py` 之前。 |
| [`docs/local-tv-prototype.md`](../local-tv-prototype.zh_CN.md) / [`server/README.md`](../../server/README.zh_CN.md) | 设备端与服务端各自的协议、归属、内存与生命周期叙述。 |
| [`docs/CHANGELOG.md`](../CHANGELOG.zh_CN.md) | 项目自己记录的、已经发布过什么。 |

## 7. 快速任务路由（在 `AGENTS.md` 的表基础上，补齐本 fork 的电视功能面）

| 新需求提到... | 从这里开始 |
| --- | --- |
| 频道列表，增删/排序频道 | `channels.txt`、`tools/channel_config.py`、`server/live.py`（`CHANNELS`）、`main/av_channel_policy.c` |
| 线协议/包格式改动 | `main/av_protocol.h`、`server/protocol.py`、`server/frames.py`，见上文第 5 节 |
| 画质、帧率、条带/几何参数 | `main/av_protocol.h`（`AV_VIDEO_*`、`AV_FPS`）、`server/frames.py`、`server/rate.py`、`main/av_player.c` 的 `video_task`/`push_stripe` |
| 音画同步、drift、卡顿、会话重置 | `server/timeline.py`、`server/pts.py`、`main/av_player.c` 的 `audio_task`/`video_task`、`docs/development/state-20260916.md` |
| 设备按键、屏幕菜单、横幅、音量/亮度 HUD | `main/ui_menu.c`/`.h`、`main/av_player.c` 的 `process_key()`、`main/av_settings.c` |
| Wi-Fi/服务器地址配网流程、captive portal | `main/av_provision.cpp`、`main/av_provision_policy.c`、`main/av_player.c` 的 `run_setup_mode()` |
| 服务端打包/分发/发布 | `tools/build_server.py`、`packaging/tv-server.spec`、`.github/workflows/build-server.yml` |
| 固件构建、CI、发布产物 | `tools/validate.sh`、`sdkconfig.av-prototype`、`.github/workflows/build-firmware.yml` |
| BSP 层硬件（引脚、总线、显示、音频、电池） | 与 `AGENTS.md` 一致——先看 `components/bsp/include/bsp_pins.h` |
| 不在本表里的任何需求 | 退回 `AGENTS.md` 自己的路由表，再看 `docs/development/ai-guide.md` |

## 8. 继承自 `AGENTS.md` 的约束——不在此重复

Flash 布局（`cardid@0x356000`、3 MB 应用上限）、只用公开版做验证、
`bsp_lvgl_lock()`、按键回调不阻塞、文档双语配对规则等，全部原样适用。
以 [`AGENTS.md`](../../AGENTS.md) 当前文字为准——本文件只是在它之上加一张地图，绝不是它的第二份拷贝。
