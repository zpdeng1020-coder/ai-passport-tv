<p align="right">
  <strong>简体中文</strong> · <a href="CHANGELOG.md">English</a>
</p>

# Changelog

## Unreleased

- 修复下载版一律无法建立 HTTPS 连接：播放列表加载不了、ffmpeg 也取不回来，两处都报 `unable to get local issuer certificate`，而同一个地址在浏览器里正常。原因是 Python 从编译时写死的路径读取根证书，而在一台机器上构建、下载到另一台机器运行的产物会继承构建机的路径，那个路径在用户的电脑上并不存在。新增 `tools/certs.py`，改用本机确实存在的证书文件，并以"实际装载了多少个根证书"为判据，而不是"文件是否存在"——后者正是最初写错的地方。验证方式：用一个自身没有证书文件的 Python 构建，再撤掉本机证书；修复前播放列表与 ffmpeg 下载双双失败，修复后都正常。构建检查里增加一问：撤掉本机证书后问产物"你还能校验 HTTPS 吗"，答不上则不允许发布。由用户在日常使用中第一个撞上并报告。

- 修复关掉程序后两个子进程活着不走的问题。关闭终端窗口或执行 `kill` 发送的是 SIGTERM，Python 默认直接结束进程，`finally` 里的清理代码不执行，于是媒体服务器与频道配置页留在自己的会话里继续运行，8096 和 8097 端口不释放。下一次启动会报"端口已被占用"，而屏幕上没有任何线索把它和用户认为已经关掉的程序联系起来。现在 SIGTERM 等信号与 Ctrl-C 走同一条退出路径；此外每个子进程会自行检查启动它的进程是否还在，覆盖信号无法覆盖的情况（进程被强杀）。打包版还有第二层后果：被留下的子进程仍在引用其父进程已经删除的临时目录，靠已加载的模块继续工作，直到要用某个尚未导入的模块时崩溃并报 `LookupError: unknown encoding: idna`——这句话描述的是症状，与真正的原因毫无关系。

- 退出提示不再把正常事件报成故障。握手阶段就断开的连接——换台、设备休眠、端口探测都会产生——原本计入失败次数，于是一次健康的运行会以"失败 3 次"结尾，而使用者无从知道这三次是什么。现单独计数且不再出现在提示里；通过鉴权之后才中断的会话仍然计为出错并如实报告。

- 逐条审读程序打印给运行者看的每一句提示。启动提示由十六行压到四行：删去正在监听的地址（那是这台电脑自己的号码，设备拿到的是主机名），只留下要填进设备的那一个地址，下面最多一行说明连不上时换成什么。五处仍是英文的提示改为中文，程序不再说到一半换语言；Ctrl-C 退出不再报告内部失败条数；端口被占用原本是二十行 Python 堆栈，末行写着 `OSError: [Errno 48] Address already in use`，现改为两句可读的话，并按 `errno.EADDRINUSE` 判别——这个编号在 macOS 是 48、在 Linux 是 98。`--help` 也已中文化，包括 argparse 自己拼出来的 `usage` 与 `options` 两个标题。

- Release 页面不再只列文件名，而是先说明该下哪一个：哪台电脑对应哪个文件、刷设备与跑服务是两件都要做的事、以及用户最先撞上的两个拦截（Windows 防火墙、未签名程序提示）。此前两次发布出来都是空正文，原因已查明：发布任务不检出仓库，只下载构建产物，指向仓库内文件的 `body_path` 自然找不到，动作便静默退回空正文。现改为在两个工作流里各写一份同样的正文——两份必须一模一样，后运行的那个会把 Release 整个改写。

- 服务端改为下载即用：新增三平台单文件可执行程序（`tv-server-*`），用户下载后双击即可，不必安装 Python。程序首次运行时自动获取 ffmpeg（约 21–31 MB，仅此一次；已装 ffmpeg 的机器完全不联网），获取前先说明体积并校验 sha256，不符则拒绝写入与执行。不内置 ffmpeg 的原因是这些构建带 `--enable-gpl`，随包再分发会产生提供源码的义务，而用户本机按需获取不构成再分发。可执行文件走 Release 分发，不进仓库。

- 服务端数据与代码位置分离：新增 `tools/datadir.py`，可写位置依次为 `TV_DATA_DIR` 环境变量、程序所在目录（实测可写才用）、平台用户目录；启动时打印实际路径。原先两者用同一个 `__file__` 推导的路径表示，从仓库运行时恰好重合，打包后会指向运行时就删除的临时目录，导致频道表保存丢失。子进程改用 `PYTHONPATH` 而非工作目录来找到 `server` 包，两者职责由此拆开。

- 修复首次保存频道表不触发媒体服务器重启：原判据要求比较两个时间戳，即只在文件已存在且发生变化时重启；而新数据目录首次运行为空，第一次保存恰好是从无到有。另修复重启时沿用旧频道名的问题——首次保存会用用户的表整体替换内置列表，旧名必然不在其中，服务器以 `--channel` 为必选并校验取值，随即退出且无人监听端口，用户看到的是"一保存就没画面"。现改为不存在时回退到新表首项。

- Windows 控制台编码：程序的所有提示均为中文，而 Windows 控制台默认用系统代码页，英文系统（cp1252）下写第一个字符即抛 `UnicodeEncodeError` 并结束进程——用户看到的第一句话就是程序崩溃。新增 `tools/console.py` 统一在打印前设为 UTF-8，四个入口调用；测试用 cp1252 流复现该故障，并扫描所有脚本防止新增文件遗漏。

- 仓库 CI 修复：`main/ui_menu.c` 使用 POSIX 的 `strnlen` 而 glibc 需特性宏才暴露，导致 macOS 本地全绿、Linux CI 持续失败；补 `_POSIX_C_SOURCE` 后 Cross 编译到 aarch64-linux-gnu 验证对照（去掉宏可精确复现原错误）。新增服务器三平台构建工作流，构建后立即启动做冒烟测试，并与固件工作流共用一个 release 并发组以免两者同时改写同一 Release 而丢文件。

- 服务器侧命名由 `av` 统一为 `tv`：模块 `server/av_server.py` → `tv_server.py`，可执行文件、Release 产物、环境变量（`AV_DATA_DIR`/`AV_FFMPEG`/`AV_CHANNELS_FILE` 等）与文档一并更改。固件侧的 `av_` 前缀保持不变（那是音视频编解码，与本项目做的事不同），固件模块名 `av_protocol` 亦保留。`AV_PAIRING_TOKEN` 起初被一并保留，理由是同名于固件里的常量、属于跨设备约定；这个理由不成立——两者只需令牌的**值**相同，名字各归各的，故服务器的环境变量随其余一并改为 `TV_PAIRING_TOKEN`。

- README 补充下载即用路径、Windows 防火墙需放行 8096（实测发现：Windows 默认拦截入站，设备只显示"连不上服务器"，看不出是防火墙）、未签名程序的系统拦截及绕过方式，并把"没测过的部分"改写为逐项的验证清单。

- 本地原型新增实时直播频道播放：服务器新增 `live` 子命令，用 ffmpeg 实时转码白名单频道（以 `-re` 按实时速率读取），设备通过以新频道名重连来换台。频道列表随 CONFIG 下发，使用与音频声道数不同的独立键；两者不可混用，键名冲突会让设备判定 CONFIG 非法并使每个会话立即断开。频道地址来自未经核实的社区播放列表，仅用于家庭局域网测试。

- FAV1视频输入改为160x120 baseline YUV420 JPEG，x2最近邻显示为320x240，保留12fps/24KiB及PCM/时间戳。导入保显示比例居中黑边；修正MCU条带裁分和末行，不增加DMA内存，补充全帧边界及真实素材测试。旧320x240素材需重新生成，设备速度尚未测量。
- 新增可选的本地 JPEG/PCM 播放原型，包括有界 FAV1 协议、仅依赖 Python 标准库的测试媒体服务、独占液晶刷新、协作退出的播放任务及主机测试。原演示仍为默认入口。音频计时明确采用估计值，实际音画同步和持续性能须通过设备测量确认。私有网络配置及生成素材不进入版本控制。

- 加入厂家为优特利 520mAh 电芯生成的 80 字节 CW2017 profile，并实现内容与更新标志检查、写入后校验、规定的重启时序以及有上限的 SOC 就绪等待。

- 扩充环境引导文档：新增乐鑫 Git 服务镜像（`git.espressif.com.cn`）作为中国大陆首选线路，覆盖 ESP-IDF v5.5.3 及其子模块；补充子模块长等待/超时处理、原地修复，以及 `esp32-wifi-lib` 等大仓的按钉死 commit 浅取；提示按仓库残留的 Jihulab `insteadOf` 旧配置；并把官方离线 release 压缩包加入兜底方案（经验来自 `esp-mosaico/esp-mosaico-vibe`）。

- 按功能域整理文档并采用双入口：根目录 `AGENTS.md` 变为薄路由（只保留硬约束与任务路由），详细的 AI 开发工作流下沉到 `docs/development/ai-guide.md`，`agent-guide.md` 并入其中。为 `docs/development/` 增加二级分区（`engineering/`、`ci/`、`release/`），把 `plays/` 应用档案与 `experiences/` 移入带专属 README 的 `docs/reference/` 参考区；删除 `docs/software-design/`（空脚手架）；把 `assets/{fonts,images,music}/README` 三个叶子 README 并入 `assets/` README；把 `project-completion` 的六个子文档压平为单文件；并把每个目录统一为单一 README，消除所有 `INDEX` 文件与一处重复经验索引。所有交叉引用与文献链接已更新；未丢弃任何内容。

- 删除位于 `0x700000` 的旧 app/test 分区，以及相关的 bootloader、校验和
  文档要求；固定的 `cardid` 保护分区及其 CI 校验保持不变。
- 规定多应用发布的 Release 标题约定：tag 按 `v<版本>-<应用名>`（如 `v0.1.0-voice-keychain`）命名，让 Release 标题同时带版本与应用名；发布成功后核对标题，保证一眼扫 Release 列表就能区分是哪个应用。
- 新增发布后收尾流程：`issue-suggestions` skill 用于把用户反馈作为 issue 提交到上游项目；`experience-pr` skill 用于把可复用的开发经验作为文档 PR 提交；新增 `docs/experiences/` 目录保存单条经验文件；并配套 `project-completion`、`file-issues` 与经验索引文档。
- 精简仓库根目录：将 GitHub 可识别的社区治理文档迁入 `.github/`，将变更记录迁入 `docs/`，同步全部引用，并在仓库检查中加入根目录文档白名单。
- 全仓库文档语言规范：所有维护中的 Markdown 默认 `.md` 文件使用英文，简体中文使用配对的 `.zh_CN.md`，双方提供语言切换；静态检查会阻止缺失配对、缺失切换链接或英文默认页混入中文正文。
- AI 开发流程一期：精简按任务加载的上下文入口，统一本地/CI 验证脚本，新增 PR 自动构建与模板，并提交依赖锁文件以提高构建可复现性。
- PR 审查修复：GitHub Actions 固定到完整 commit SHA，构建与发布 job 按最小权限拆分，同步 checkout 关闭凭证持久化；补充 Feature Request / Usage Question issue 表单；启用并修正私密安全报告兜底说明；清理 README 路径、CI 触发条件与历史分支描述漂移。
- 语言规范变更：commit 标题、PR 标题与 body 由"默认中文"改为**使用英文**（`docs/contribution/commit-and-pr.md` 更新）；中文写作规范（全角标点）适用范围剔除 PR/MR 描述（`doc-conventions.md` 更新）。
- CI 构建改造：`build-firmware.yml` 显式传入 `SDKCONFIG_DEFAULTS=sdkconfig.defaults` 再 `idf.py build`，由 defaults 启用自定义分区表（`CONFIG_PARTITION_TABLE_CUSTOM=y`，文件名为 `partitions.csv`）；`CONFIG_ESPTOOLPY_HEADER_FLASHSIZE_UPDATE` 改为 `n`，再用 `idf.py merge-bin -o build/FoloToy-AI-Passport-full.bin` 合并可直刷完整固件；产物精简为仅 full.bin；`actions/cache` 升级到 v5 以消除 GitHub Actions Node.js 20 弃用警告；CI 文档同步更新。
- 合并上游 PR #6（wireless-low-power-demos）以解决 PR #4 冲突：引入无线/低功耗 demo（`main/demo_wifi.c`、`demo_ble.c`、`demo_radio.c`、`demo_low_power.c`）、`partitions.csv`（NVS/PHY/3 MB factory-app 分区）、`main/CMakeLists.txt`/`main.c`/`demo.h`/`sdkconfig.defaults` 更新；同步硬件指南的 Wi-Fi/BLE/低功耗章节；README 能力契约表补充 Wi-Fi/Bluetooth LE/Low power 三项（中英双语）。
- 提交规范补充：`docs/contribution/commit-and-pr.md` 明确 PR 标题与 commit 标题使用相同的 Conventional Commit 格式和英文祈使句，不用名词短语当标题。
- CI 与文档清理：`sync-main.yml` 移除 `test_mode` 残留模板注释；`docs/development/coding-conventions.md` 将「Redis TTL」条目泛化为「缓存组件」条目（当前固件无 TTL 约束需求，消除从模板带入的无关约定）。
- 补充通用规范（借鉴 Shinku）：`docs/contribution/doc-conventions.md` 新增中文全角标点规范（正文 `，`；`（`）`，代码/命令/路径保留英文原样）、凭证不入仓规范（token/密钥/私钥绝不入仓，提交前 git diff 扫描敏感前缀）、文件删除安全规范（删除走系统回收站，不用 rm -rf/git clean -fd）。
- 代码注释规范强化：`docs/development/coding-conventions.md` 补充完善注释要求——函数说明（用途/参数/返回值/副作用/线程上下文/内存所有权/初始化顺序）、变量说明（语义/取值范围/生命周期/同步要求）、逻辑注释（状态机/时序/寄存器/魔数依据），覆盖范围宁多勿少，中文注释保留英文技术术语。
- 文档去 AI 化：`docs/README.md` / `docs/README.zh_CN.md` 移除 AI 专属章节（Entry point、Source-of-truth、提需求格式、BSP 边界、Runtime invariants、验收交付格式、构建命令），README 只保留给人看的项目介绍、硬件能力契约、demo 案例与项目结构；构建命令章节删除（与 `docs/development/build-and-test.md` 重复）。
- 新增 `docs/development/agent-guide.md`：集中承载"AI 如何在本仓库工作"（上下文建立顺序、事实来源优先级、提需求格式、BSP 边界、运行时规则、交付格式），并链接 build-and-test 与硬件指南，不重复构建命令与验收矩阵。
- 同步更新索引：`AGENTS.md` 规则索引新增 agent-guide 条目；`docs/INDEX.md` 与 `docs/development/README.md` 新增 agent-guide 索引行。
- 文档补充：`docs/fork-guide.md` 说明「为什么根目录不放置 README」——根目录 README 预留给 fork 开发者自行放置（上游留空），fork 后可将自己的内容写入根目录 `README.md` 介绍 fork 后的项目；GitHub 显示优先级（根 README > docs/README.md）契合该预留意图。
- 分支合并：创建 `main-update` 分支（基于与上游一致的 main），将 `feature/repo-structure`、`ci/build-firmware`、`ci/sync-main` 三个分支合并进来，统一 docs 结构（CI 文档归入 `docs/development/`，workflow 文件随 ci 分支引入 `.github/workflows/`）；解决 development/software-design README 的 add/add 冲突。
- 合并后审查修复：`docs/INDEX.md` 补充 CI 文档索引；`docs/fork-guide.md` 修正 workflow 引用为 `.github/workflows/sync-main.yml`；`docs/README` 双语项目结构块补充 `.github/workflows/` 与 CI 文档说明。
- ci 分支 CI 文档路径调整：`ci/build-firmware` 的 `docs/software-design/CI-build-and-release.md` 与 `ci/sync-main` 的 `docs/software-design/CI-sync-main.md` 均移入各分支的 `docs/development/`（CI 属工程规范）；`docs/software-design/README.md` 保留为软件设计索引；feature 分支的 software-design 索引同步更新引用。
- fork 补充文档目录迁移：`assets/docs/` 移至 `docs/assets/`（文档素材归入 docs/ 更合理），新增 `docs/assets/.gitkeep` 空目录占位；同步更新 AGENTS.md / INDEX / doc-conventions / fork-guide 的路径引用。
- 文档结构调整：根目录不再放 README——上游英文 README 移入 `docs/README.md`、中文移入 `docs/README.zh_CN.md`（GitHub 从 docs/ 识别主 README）；原 `docs/README.md` 根总索引更名为 `docs/INDEX.md`；同步更新 AGENTS.md / CONTRIBUTING / SUPPORT / fork-guide / doc-conventions 的路径引用。
- 初始化项目文档：新增 `AGENTS.md`、`CLAUDE.md` 和 `CHANGELOG.md`。
- 仓库结构规整：上游英文 `README.md` 更名为 `README.en_US.md`，保留 `README.zh_CN.md`。
- 新增目录骨架：`docs/`（software-design / hardware-design）、`assets/`（fonts / images / music，各含 `README.md`）、`skills/`。
- 将上游硬件开发指南归位到 `docs/hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md`。
- 文档规范：子目录 readme 统一为大写 `README.md`；补充 fork 用户约定（main 只动根 README）。
- 扩展 fork 用户约定：`main` 分支允许修改根目录 `README.md` 和 `assets/docs/`（README 不足以说明项目时存放补充文档与素材）。
- 新增 `assets/docs/` 目录约定：上游 main 只保留空目录 `.gitkeep`，内容文件仅存在于 fork；使用方法规范写入 AGENTS.md「给 fork 用户」约定。
- CI 文档迁移：`docs/software-design/CI.md` 从本分支移除，迁至 `ci/build-firmware` 分支并改名为 `docs/software-design/CI-build-and-release.md`。
- 补充 `main` 分支策略说明：解释 `main` 保持干净的两大原因（与上游同步无冲突 + 多小项目按分支整理）；例外——执意 main 开发需停用 CI 自动同步；提醒 fork 用户默认 action 关闭需手动启用（此条为整个 CI 的通用要求，统一写入 AGENTS.md）。
- 文档拆分：将 `AGENTS.md` 按主题拆为公共文档——新增 `docs/contribution/`（doc-conventions.md、commit-and-pr.md）与 `docs/development/`（build-and-test.md、coding-conventions.md），新增 `docs/fork-guide.md`；`AGENTS.md` 精简为简介 + 项目概述 + 必读文档索引。
- 同步更新索引：`docs/software-design/README.md`、`README.en_US.md` / `README.zh_CN.md` 的 `docs/` 目录说明。
- 参考 cindy 仓库文档组织完善索引：新增 `docs/README.md` 根总索引；AGENTS.md 规则索引按触发场景改写（附触发条件）；`docs/contribution/` 与 `docs/development/` 的 README 补充收录标准。
- 引入社区治理文档（参照 cindy 改写，放仓库根目录）：新增 `CONTRIBUTING.md` / `.zh_CN.md`（贡献指南，针对 ESP-IDF/AI agent/fork 场景改写）、`CODE_OF_CONDUCT.md` / `.zh_CN.md`（贡献者公约）、`SECURITY.md` / `.zh_CN.md`（安全报告流程）、`SUPPORT.md` / `.zh_CN.md`（支持渠道）；AGENTS.md 与 docs/README.md 同步引用。

- 构建自带的冒烟测试在 Windows 上读不懂程序的中文输出：`subprocess.run(text=True)` 用系统代码页解码，读取线程抛 `UnicodeDecodeError`，stdout 变成 `None`，随后比较时出错。程序本身一直是正确的，坏的是检查它的那段代码。现在仓库里每一处捕获子进程输出的地方都写明编码，并由一个 AST 扫描看守以免新增遗漏——该扫描还查出了第二处，在仓库检查工具里，中文路径同样会让它出错。

- 孤儿检测改为能在 Windows 上生效的做法。检查 `os.getppid()` 在 POSIX 上可行（子进程会被重新挂到 init 下），但 Windows 上父进程编号从不改变，检查永远不触发——于是 macOS 与 Linux 通过、Windows 失败，而问题正是 Windows 上报告的。现在启动器给每个子进程一个它从不写入的 stdin 管道，并在退出时关闭；启动器无论以何种方式结束——包括被直接强杀——子进程的读取都会返回文件结束，随即退出。管道是句柄而不是取值，不会过期，也不含任何平台差异。验证方式：用 `kill -9` 强杀启动器，修复前两个子进程存活并占着两个端口，修复后什么都不剩。

- 守护线程在解释器关闭期间读取 `sys.stdin.buffer` 是致命错误而非警告：Python 在持有该锁的情况下收尾，以 `_enter_buffered_busy` 中止，把一次干净的退出变成崩溃。现改用 `os.read` 读取原始文件描述符，不涉及这把锁。这个问题是实跑发现的，不是查文档查出来的，值得记下来：它只在退出路径上出现，而那是其他测试都碰不到的路径。

- 关闭终端窗口不再打印 Python 堆栈。窗口关闭会发出两次 SIGHUP——这是用伪终端实测的，不是推断——第二次在第一次的处理过程中到达，于是中断从"处理该中断的代码"内部抛出，无人接住，用户看到的最后一行是堆栈。退出本身一直是正确的，错的只是那几行文字；而这类错误会让人以为程序坏了。现在第二次停止请求被忽略；同样的保护也覆盖了子进程尚未启动的阶段（例如获取 ffmpeg，约需半分钟），做法是在启动器顶层捕获中断，而不只是围着运行子进程的那一段。验证方式：在伪终端下关闭窗口，堆栈消失，退出码由 1 变为 0，两个端口都释放。

- 修复"检测失效"把能播的频道判为失效。页面取的是 `c.ua`，而这个字段一直叫 `agent`——播放列表解析、频道表存储、保存写回，三处都是这个名字。于是每次探测都带着空的 User-Agent，而那些只应答特定播放器的源会直接拒绝：403、取不到帧、判定为"失效"，而该频道实际能播。由用户实测发现并报告——他自己试了一个，看着它播出来了，说页面不对，页面确实不对。用真实频道表做了双向实测：带上 agent，八个 CCTV 全部在半秒左右解出画面；不带，全部在零点一秒内失败，也就是页面此前一直显示的结果。新增 `tests/test_channel_config.py`，把页面读取的字段名与后端产生的字段名做比对，两边不再毫无关联。
