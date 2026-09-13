[English](local-av-prototype.md) | 简体中文

# 独立本地音画原型

本可选启动入口通过一条鉴权局域网 TCP 连接，播放直播或预生成的 160x120 baseline YUV420 JPEG 与 16 kHz 单声道 s16le PCM。服务器的 `live` 子命令实时转码一个频道，`run` 回放已准备好的素材。频道列表与当前频道始终由服务器决定，白名单地址是未经核实的第三方中继。原菜单、演示和像素主题全部保留。`CONFIG_AV_RAW_PROTOTYPE` 默认关闭；RAW 模式不启动 LVGL 或 BLE。本项目不是公网 URL 代理，也不是生产播放器。TCP/token 配对不提供加密，只应在可信局域网使用。

## 私有配置与构建

仅在本机创建权限 `0600`、已忽略的 `main/av_private_config.h`。定义 `AV_WIFI_SSID`、`AV_WIFI_PASSWORD`、`AV_SERVER_IPV4`（IPv4 字面量）、`AV_SERVER_PORT`（整数，通常 8096）和 `AV_PAIRING_TOKEN`。不要把值粘贴到跟踪文件、日志或命令。缺少私有头仍能编译，RAW 启动会报缺失配置且不联网。应用不擦除 NVS，Wi-Fi 配置存储使用 RAM。

激活 ESP-IDF 5.5.3 后在仓库执行：

```bash
./tools/validate.sh --static
./tools/validate.sh --firmware   # 原demo，公开构建不读取私有头
./tools/validate.sh --prototype  # RAW overlay，公开构建不读取私有头

# 真实本地设备镜像含凭据，不得发布build-av文件。
idf.py -B build-av -D SDKCONFIG=build-av/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype' \
  -D AV_PUBLIC_BUILD=OFF build
idf.py -B build-av merge-bin -o build-av/FoloToy-AI-Passport-full.bin
python3 tools/verify_firmware.py build-av
```

公开门禁显式设置 `AV_PUBLIC_BUILD=ON`，因此不会读开发者私有头。公开 RAW 镜像用于编译/布局检查，启动只报告缺失配置。`--prototype` 输出 `build/FoloToy-AI-Passport-prototype-public.bin`，不覆盖 demo 镜像。私有 `build-av` 保留分段 bootloader、分区表和 app，供授权操作人员使用；上述命令均不刷机。保持 8 MB Flash、3 MB 应用和 `cardid@0x356000` 不变；写入已配置设备前先完成两次一致的全片备份。禁止整片擦除或发布私有合并镜像。用独立默认构建恢复 demo，不要删除原 UI。

## 线协议

固定 24 字节网络序头对应 Python `!4sBBHIIII`：`FAV1`、version 1、type、零 flags、session、全局 sequence、毫秒 PTS、payload length。HELLO（1）头 session/seq/PTS 均零，JSON 为 `{token,version:1}`，并可带 `channel` 指定要播放的频道（省略或填未知 id 时由服务器回退默认）。CONFIG（2）使用非零 session，seq/PTS 零，JSON session 必须匹配。必需参数：width 160、height 120、fps 12、sample_rate 16000、channels 1、sample_bits 16、audio_chunk_ms 20、video_max_bytes 24576。可选 `start_delay_ms` 必须为 200；容许服务器附加 timing 字段。

`channels` 表示音频声道数，必须保持为数字。可选频道列表放在独立的 `channel_list` 数组里，每项为 `{id,name}`，设备按上下键短按在列表中前后切换。服务器不发送该键时，设备停留在当前频道。两个键必须分开：把列表当作 `channels` 发送会让设备判定 CONFIG 非法，每个会话都会立即断开。启动目标是收到 CONFIG 后 200ms，但音频复位/预缓冲可能使其延后（见估计时钟）。

AUDIO（3）严格为 640 字节/20ms。VIDEO（4）为完整 baseline YUV420 JPEG，1..24576 字节，实际尺寸/采样由 ROM 检查。END（5）为空载荷。ERROR（6）及其他控制 JSON 上限 1024 字节。错误 magic、版本、类型、flags、大小、会话、序号、配置、JPEG 或截断 TCP 都终止会话；不搜索 magic 恢复。递归 JSON 解析之前，先通过识别字符串/转义的线性扫描将嵌套深度限制为四。CONFIG 前收到 ERROR 视为终止握手失败，不当作媒体。远端 JSON 不写日志。

序号全局递增，但各媒体 PTS 独立检查：音频从零开始每次 +20，视频严格递增且可跳帧。视频 PTS 可以低于紧前音频 PTS。素材循环必须保持时间戳/序号连续或新建会话；`duration_ms` 表示服务器素材长度，不是固件固定会话超时。

## 素材转换与兼容性

`prepare` 和 `import-video` 均生成实际160x120 baseline YUV420 JPEG，manifest 与 CONFIG 尺寸一致。拒绝旧320x240素材/配置，应在新目录重新生成，不覆盖旧素材。导入按源显示比例（含像素宽高比）适配16:12（4:3），保比例居中黑边，不裁切、不拉伸，YUV420偶数取整可能造成最多两像素黑边不对称。PCM、12fps时间戳、大小上限及私有网络配置不变。参见[服务器说明](../server/README.zh_CN.md)。

## 所有权、内存和生命周期

- 接收任务独占套接字，非阻塞连接/读写配合有界轮询和截止时间；完整 PCM/JPEG 才入队，消费者不读 TCP。
- 音频任务仅复用 BSP I2S/codec，单次写上限 100ms，部分提交视为失败。20 项 PCM 队列提供 400ms/12800 字节，接收/消费各另有一块音频。启动至少预缓冲五块。队列满、欠载或提交间隔过长都会重建连接及同步起点。
- 视频任务独占 RAW 屏幕，不注册 LVGL 回调。两块 24 KiB JPEG、两块 320x16x2 内部 DMA 条带（20480 字节）、4096 字节解码工作区。JPEG 池满时完整丢弃来帧，不让解码阻塞音频。仍使用 C3 ROM tjpgd 输出 RGB888（不是 LEO 的 LVGL BGR 解码器），scale=0 解码 160x120，再 x2 最近邻转换为 320x240 大端 RGB565。每个16源行 MCU 行填两块16目标行条带；最后源 y=112..119 仅填目标 y=224..239 的一块条带。packer 按源矩形行跨度裁出各条带。两块填完再依次提交 DMA 并等待完成后复用，条带 RAM 仍为20480字节，不声称解码/DMA重叠或播放更快。这是 LEO 风格的小尺寸输入，不是20fps复刻。
- 栈预算：接收 5120 字节，音频/视频各 4096 字节；另计队列、驱动 DMA、Wi-Fi 与 BSP。overlay 使用 160 MHz 和有界 Wi-Fi RX/TX 池。内存依据运行时 free/minimum/largest heap，不把构建体积当堆测量。分配失败后停止测试。
- 按钮投递八项队列（按键加手势），不阻塞。OK 切换停止/重启；UP/DOWN 短按按服务器下发的频道列表前后换台，实现上是以新 id 重连；UP/DOWN 长按每次调整音量 5%，范围 10..100%，初始 55%。启动显示 RAW 红绿蓝色条；停止保留最后画面。
- 停止/错误设置取消，接收生产者关闭套接字，消费者完成有界调用并发最终资源访问结束信号，随后才释放会话队列/缓冲。不强删任务。LCD DMA 超时刻意保留缓冲等待完成，不制造 use-after-free；连续十次 200ms 等待失败会明确记录故障并重启，不先释放 DMA 缓冲。控制、按钮、Wi-Fi 事件循环和 BSP 归整个启动生命周期，会话内存每次释放；不支持运行时返回 LVGL。
- 接收无进展五秒重建会话，不设总会话固定超时。正常 END 消费完队列后清理。重连间隔一秒，清零状态重新握手。服务器连续 PTS 的素材循环本身不触发重启。

## 时钟与度量：明确是估计

日志使用 `CLOCK_ESTIMATED`。提交样本数仅代表 I2S 驱动接受的字节，**不是 DMA 已完成样本，更不是实际声学输出**。单调时钟起点设为首次写入后假定 90ms（六个 240-frame 描述符 /16kHz），播放位置上限为已提交时长。这个预算不是观测到的延迟。启动/复位先静音写零，再重建起点。codec/扬声器真实延迟、DMA 实际填充、采样时钟漂移、网络延迟均未测量。当前视频等估计 PTS 再解码，显示可能再落后解码/传输耗时；已过期 100ms 的帧丢弃。这是有界延迟 MVP，不是同步验收。调整时钟偏移或解码提前量前，应实测闪光/短音标记。

每十秒记录实际区间完成帧数和时间、丢帧、队列高水位、最长解码、heap/largest block。会话结束补充实得渲染 FPS（含启动/排空）、提交样本、最大提交间隔、最低 heap，并明确 DMA/声学未测量。目标 12fps 不当作已达帧率。应用层不输出 SSID/password/token/远端 JSON，但底层 Wi-Fi 日志可能带网络标识，分享前仍须脱敏。

## 验证与待测

`tests/test_av_protocol.c` 为纯 C，覆盖头大小端/长度/类型边界、畸形头、重组切分边界、序号/会话拒绝、独立媒体 PTS、重连清零、RGB 条带尺寸和字节序。全帧 MCU 测试覆盖 x2 像素复制、源行跨度、拼接边界、保护字节和最后8源行；静态门禁还运行服务器测试和 ffmpeg 可用时的真实生成/导入测试；不模拟 ROM JPEG、LCD DMA、I2S、Wi-Fi、并发或声学时序。

分别报告 Build、Host tests 和 Device tests。硬件验收前需检查颜色/方向、JPEG 实际速度、PCM 单声道采样率和音量、闪光/短音偏移漂移、超过 30 分钟持续播放和服务器素材循环、队列/堆边界、服务器不可达/慢速、坏/截断 JPEG、Wi-Fi 断连、音频欠载、重复 OK 停止重启及停后 DMA 所有权。构建/合并布局验证通过不能代替任何实机结果。
