<div align="center">

# VideoCaptioner macOS Fork

**语言 / Language:** 简体中文 | [English](./README.en.md)

</div>

这是 [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) 的 macOS/Apple Silicon 本地工作流分支。它不是原项目的完整替代版，而是保留桌面端字幕处理体验，并针对 macOS 本地转写、下载、字幕切分和翻译稳定性做了较大幅度的裁剪与增强。

本 README 只说明本分支相对原项目的主要差异。原项目的完整介绍、在线文档、CLI 用法和发布版请以 [上游仓库](https://github.com/WEIFENG2333/VideoCaptioner) 为准。

## 与原项目的主要差异

| 方向 | 原项目 | 本分支 |
| --- | --- | --- |
| 项目定位 | 跨平台 CLI + GUI + PyPI 包 + 文档站 | macOS / Windows 源码运行的 GUI 分支 |
| 支持平台 | Windows、macOS、Linux | macOS；已恢复 Windows 桌面源码入口，待 Windows 实机验收 |
| 本地 ASR | 多后端：`faster-whisper`、`whisper-api`、必剪、剪映、`whisper-cpp` 等 | 聚焦 WhisperX CUDA / CPU，并新增 MLX Whisper 作为 Apple Silicon GPU 后端 |
| 时间戳策略 | 根据不同 ASR 后端能力处理 | 默认围绕词级时间戳、VAD 和 WhisperX 对齐构建后续字幕流程 |
| 下载流程 | 上游通用下载命令和桌面入口 | 独立下载中心，强化 yt-dlp、浏览器 Cookie、最终 MP4 归一化和 HEVC 兜底 |
| 字幕处理 | 上游通用字幕切分、优化、翻译和合成 | 增强严格断句、短间隙处理、重复 ASR 片段清理、术语/热词传递和 LLM 翻译稳定性 |
| 运行方式 | `pip install videocaptioner`、`uv run videocaptioner`、CLI/GUI 均可用 | 使用本仓库源码、`.venv` 和本地 `.app` 启动器运行 |
| 打包与文档 | 保留上游 PyPI、CI、VitePress 文档站和多平台构建脚本 | 删除或弱化上游发布链路，保留更小的 macOS 本地运行说明 |

## 本分支重点

- 面向 Apple Silicon Mac 的本地字幕工作流。
- WhisperX CUDA / CPU 转写，自动选择设备和适用精度，并支持 WhisperX 对齐。
- 可选 MLX Whisper 后端，通过 `mlx-whisper` 使用 Apple Silicon GPU。
- 应用数据存放在 `~/Library/Application Support/VideoCaptioner`。
- 默认工作文件存放在 `~/Movies/VideoCaptioner`。
- 本地 `.app` 启动器直接运行当前源码目录里的 `.venv/bin/python main.py`。

## 当前不作为重点

- 不再以 PyPI 包和 `videocaptioner` CLI 作为主要入口。
- 不维护上游完整文档站、Release 工作流和多平台打包链路。
- 不追求覆盖上游所有 ASR、TTS、CLI 和跨平台功能。

## 下载发布版

本 fork 的发布版面向 Apple Silicon Mac，提供 `.dmg` 安装包：

```text
https://github.com/MeguruNo1/video-captioner-macos-enhanced/releases
```

当前版本：`macos-enhanced-v0.1.2`

本版新增 Codex 本地字幕工作流、MLX Whisper 词级时间戳强制对齐、可定制下载简介模板、启动时浏览器 Cookie 更新、标题缩写断句保护，以及可恢复下载的暂停/终止控制。

安装方式：

1. 下载最新的 `VideoCaptioner-macos-enhanced-*.dmg`。
2. 打开 DMG，把 `VideoCaptioner.app` 拖到 `Applications`。
3. 如果 macOS 首次启动拦截，右键点击 `VideoCaptioner.app`，选择“打开”。

发布版会打包 Python 应用运行时和 Python 依赖，但不会内置 FFmpeg、WhisperX/MLX Whisper 模型或用户配置。处理媒体前仍建议安装 FFmpeg：

```bash
brew install ffmpeg
```

WhisperX 和 MLX Whisper 模型会在首次使用时下载，或读取已有的本地模型目录：

```text
~/Library/Application Support/VideoCaptioner/models
```

## Windows 源码运行

已恢复 Windows 桌面入口，使用 WhisperX 自动选择 CUDA / CPU 及适用精度转写，保留下载、字幕编辑和翻译功能。WhisperX 官方提供 [CPU 运行方式](https://github.com/m-bain/whisperX)。本轮在 macOS 完成回归测试，尚未在 Windows 实机完成安装、转写及界面验收，也未提供 Windows EXE 安装包。

先安装 Python 3.12（64 位）、Git 和 FFmpeg，并确认 `ffmpeg`、`ffprobe` 已加入系统 `PATH`（可执行程序搜索路径）。在 PowerShell 中执行：

```powershell
git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe main.py
```

`git clone` 下载源码；`cd`（change directory，切换目录）进入项目。`py -3.12` 选择 Python 3.12；`-m`（module，以模块运行）执行 `venv` 创建 `.venv` 虚拟环境。后续使用其 `Scripts\python.exe`，无需激活环境；`--upgrade` 升级安装工具，`-r`（requirements，依赖清单）读取 Windows 依赖文件。最后一行启动应用，后续也可双击项目内的 `VideoCaptioner.bat`。

- 数据和模型：`%LOCALAPPDATA%\VideoCaptioner`（当前用户的本地应用数据目录）。
- 默认输出：用户目录下的 `Videos\VideoCaptioner`。
- Cookie 默认来源为 Edge，也可选择 Chrome 或手动导入 `cookies.txt`。浏览器自身的加密策略可能阻止自动提取。
- Windows 不安装 MLX / PyObjC；转码走 FFmpeg，通知走系统托盘。转写已恢复 `auto / cuda / cpu`；精度支持自动检测。已有 CPU 设置不被覆盖，可在设置中切换。CUDA 依赖和 MCP 安装见 [Codex MCP 指南](docs/codex-mcp.md#cpu--gpu-检查与-windows)。
- `/videocaptioner` MCP 会检查硬件并选择 MLX Metal、WhisperX CUDA 或 CPU。macOS 的 PO Token 安装脚本也不能直接在 Windows 执行；有此需求时需另行配置提供器，并通过 `VIDEO_CAPTIONER_BGUTIL_SERVER_HOME` 指向其 `server` 目录。

## 从源码运行

```bash
brew install python@3.12 ffmpeg git

git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements-macos-whisperx.txt
python main.py
```

YouTube 现在可能要求字幕专用的 PO Token。首次从源码运行后，执行下面的脚本安装本地 Token provider；否则部分明明能在浏览器播放的自动字幕可能无法被 yt-dlp 列出：

```bash
scripts/setup_youtube_pot_provider.sh
```

该脚本需要 Node.js 20+、npm 和 Git，并将 provider 安装到
`~/Library/Application Support/VideoCaptioner/youtube-pot-provider`，不会在后台启动常驻服务。

WhisperX 首次使用时会下载转写、VAD 和对齐模型，除非已经存在兼容的本地模型：

```text
~/Library/Application Support/VideoCaptioner/models
```

`large-v3-turbo` 是这个分支默认的本地模型。建议将它放在应用模型目录下，并命名为 `faster-whisper-large-v3-turbo`。

MLX Whisper 默认使用 `mlx-community/whisper-large-v3-turbo`。也可以使用 Hugging Face 上的其他 MLX Whisper 模型，或使用通过 `mlx-examples/whisper` 转换得到的本地 MLX Whisper 模型目录。

## 安装本地 App 启动器

本地 app bundle 只是当前源码目录和虚拟环境的启动器。它不会打包 Python、依赖或模型；修改源码后，重启 app 即可运行更新后的代码。

```bash
scripts/build_macos_app.sh --install
```

## 构建发布安装包

发布用 DMG 通过 PyInstaller 构建独立 `.app`，输出到 `dist/release/`：

```bash
VIDEO_CAPTIONER_VERSION=macos-enhanced-v0.1.2 scripts/build_macos_release.sh
```

构建产物会包含 Python 运行时和 Python 依赖，但仍依赖系统可用的 FFmpeg，并会在首次使用 ASR 时下载模型。

## 支持格式

| 类型 | 格式 |
| --- | --- |
| 视频 | MP4、MKV、MOV、AVI、WebM、WMV、FLV、TS 等 |
| 音频 | MP3、WAV、AAC、FLAC、OGG、OPUS、M4A、WMA 等 |
| 字幕 | SRT、VTT、JSON、TXT |

## 上游与许可

原项目由 [@WEIFENG2333](https://github.com/WEIFENG2333) 创建。本分支基于原项目继续修改，许可证遵循原项目的 GPL-3.0 条款；如果对外发布 fork，请保留原项目版权和许可证信息。

## Codex 自动字幕工作流

新增本地 MCP + Skill 接入：提供视频链接，由硬件适配的本地 MLX / WhisperX 转录、当前 Codex 校对断句与翻译；按视频名建立任务目录，输出最终视频、原文/中文字幕、原文文稿及模板简介。最高画质下载遇到 VP9/AV1 时自动转为 HEVC，无需电脑操控或独立翻译 API。支持任务恢复和局部重转录。安装与使用见 [Codex MCP 指南](docs/codex-mcp.md)。

### MCP、Skill 和 Codex 分别做什么

- **MCP**（Model Context Protocol，模型上下文协议）：提供本机下载、转录、任务恢复和字幕导出工具。
- **Skill**：随源码分发的 [工作流说明](skills/videocaptioner/SKILL.md)，指导 Codex 检查硬件、校对断句、翻译和交付。
- **Codex**：在当前对话中处理字幕文本；不调用软件另配的翻译 API。音频留在本机，字幕文本会进入 Codex 对话，仍使用当前 Codex 账户额度。

本分支的 MCP + Skill 目前采用**源码安装**。仅安装 DMG 或复制 `SKILL.md` 不会完成 MCP 注册；它们也不是 PyPI 上同名包的一部分。

### 安装 MCP 和 Skill

先完成上面的对应平台源码安装，保留项目目录和 `.venv`。需要已配置好的 Codex、可在终端找到的 `codex` CLI、`ffmpeg` 和 `ffprobe`。以下命令均在**项目根目录**执行。

**macOS：**

```sh
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/install_codex_mcp.py --dry-run
.venv/bin/python scripts/install_codex_mcp.py
```

**Windows PowerShell：**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py --dry-run
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py
```

第一行使用项目虚拟环境安装 MCP 依赖：`-m`（module）以模块方式运行 pip，`-r`（requirements）读取依赖清单。第二行的 `--dry-run` 只预览注册位置；第三行实际注册 MCP 并复制 Skill，不启动媒体任务。`bin/python` 和 `Scripts\python.exe` 分别是 macOS、Windows 虚拟环境中的解释器。

安装器将服务写入 Codex 的 `config.toml`，将 Skill 复制到其 `skills/videocaptioner` 目录；根目录采用 `CODEX_HOME` 环境变量，未设置时为用户目录下的 `.codex`。它会备份已有配置，保留其他服务；遇到同名但不同路径的 MCP 或非本安装器托管的 Skill，会停止并提示冲突。安装和后续更新后，重新加载 MCP、Skill，或重启 Codex 并新建任务。MCP 使用本地进程的标准输入/输出通信，无需开放服务端口；相关机制见 [OpenAI 官方 MCP 文档](https://developers.openai.com/codex/mcp/)。

### 开始使用

在 Codex 中输入以下示例，把 `视频链接` 替换为一个实际视频 URL：

> 使用 videocaptioner 处理这个视频：视频链接。英语转简体中文，先检查本机加速能力，再输出视频、中英文字幕和封面。

也可以先选择 `/videocaptioner` skill，再给出链接与要求。想固定设备时直接说明“使用 WhisperX CUDA”“仅用 CPU”或“使用 MLX”。Codex 会先调用 `check_environment`，报告可用后端、精度和缺失依赖。转写模型须先缓存到本机；WhisperX 的 VAD/对齐模型可能在首次转录时下载。

- **自动加速**：可用的 Apple Silicon MLX Metal 优先，否则选择可用的 WhisperX CUDA 或 CPU。转码编码器则读取设置中的“自动 / NVIDIA NVENC / Intel QSV / AMD AMF / CPU”，与转写设备分开配置。
- **继续或取消**：对 Codex 说“继续任务 ID …”或“取消任务 ID …”。任务 ID 是首次启动返回的标识；忘记时可让 Codex 列出最近任务。已开始任务保留自己的配置快照。
- **完成结果**：任务目录的 `output` 中包含最终视频、原文/译文 SRT、原文文稿、简介和两张封面；`flow` 保存中间文件。默认封面流程还需要当前 Codex 环境可用的图像编辑工具。
- **关闭 Codex 后**：本机下载/转录进程可以继续，字幕校对和翻译须恢复 Codex 对话后继续。
- **更新**：在同一源码目录取得新版代码后，用对应平台的上述三行命令更新依赖、重新预览并同步 Skill，再重新加载 MCP。不要直接移动或删除已注册的源码和虚拟环境目录，否则绝对路径会失效。

完整参数、目录位置、时间戳校验及恢复规则见 [Codex MCP 指南](docs/codex-mcp.md)。

### 原项目如何分发

以下为 2026-09-15 核对结果，上游版本和本分支版本独立：

| 渠道 | 上游的做法 |
| --- | --- |
| Python 包 | [PyPI videocaptioner](https://pypi.org/project/videocaptioner/) 同时提供 CLI 和 GUI；安装命令是 `pip install videocaptioner`（pip 安装同名 Python 包），这是上游包，不会安装本分支的增强代码。 |
| GitHub Release | [v1.4.2](https://github.com/WEIFENG2333/VideoCaptioner/releases/tag/v1.4.2) 附件为 `.whl` 和 `.tar.gz` Python 分发包；已核对的 Windows 安装包在 [v1.3.3](https://github.com/WEIFENG2333/VideoCaptioner/releases/tag/v1.3.3)，文件名为 `VideoCaptioner-Setup-win64-v1.3.3.exe`。不要将旧 EXE 当成最新 Python 版本。 |
| macOS / 源码 | [上游 README](https://github.com/WEIFENG2333/VideoCaptioner#readme) 提供源码开发方式和 [run.sh](https://github.com/WEIFENG2333/VideoCaptioner/blob/master/scripts/run.sh) 安装/启动脚本。 |
| Skill | [上游 Skill](https://github.com/WEIFENG2333/VideoCaptioner/blob/master/skills/SKILL.md) 是仓库中的 Markdown 文件，README 指导复制到 Claude Code 的 `~/.claude/skills/videocaptioner/`（`~` 表示用户主目录），由助手调用 `videocaptioner` CLI；与本分支的 Codex MCP 工作流不同。 |

下载核心参考了 [FluentYTDL](https://github.com/SakuraForgot/FluentYTDL) 的生产经验：让 yt-dlp 使用其默认 YouTube 客户端策略、并行分片保持完整重试预算、认证失败分级恢复、403 过期断点清理，以及 Cookie 候选通过校验后再原子替换。这些规则由下载中心、预览解析、MCP 和兼容线程共同使用。
