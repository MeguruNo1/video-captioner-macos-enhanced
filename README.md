<div align="center">

# VideoCaptioner

本地视频转写、字幕编辑与翻译 · Windows / macOS

**语言 / Language:** 简体中文 | [English](./README.en.md)

</div>

基于 [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) 的增强分支，提供视频下载、本地语音转写、字幕校对与翻译工作流。支持桌面界面，也可通过 Codex MCP + Skill 自动处理单个视频。

## 发行计划与当前状态

**Windows 计划提供打包 EXE；macOS 采用用户自行构建的方式。**

| 平台 | 发行方式 | 当前状态 |
| --- | --- | --- |
| Windows | 计划提供可直接使用的 EXE 分发包 | 已恢复桌面源码入口；EXE 打包与 Windows 实机验收尚未完成，目前按下文从源码运行 |
| macOS（Apple Silicon） | 用户安装依赖后，自行构建 App | 已有源码运行、本地 App 启动器和独立 App / DMG 构建脚本 |

后续 Windows EXE 将通过本仓库的 [Releases 页面](https://github.com/MeguruNo1/video-captioner-macos-enhanced/releases)发布，具体依赖和使用步骤以届时的发行说明为准。历史 macOS DMG 不作为后续主要安装入口。

本 README 面向本分支。PyPI 上的同名包属于上游，安装它不会获得这里的增强功能。

## 主要功能

- **视频下载**：独立下载中心，支持浏览器 Cookie、暂停与恢复、MP4 归一化和 HEVC 转码。
- **本地转写**：Apple Silicon 可用 MLX Whisper；WhisperX 支持 CUDA / CPU，并按硬件选择设备与精度。
- **字幕处理**：词级时间戳、对齐、语义断句、重复片段清理、术语与热词，以及字幕编辑和导出。
- **字幕翻译**：桌面端使用配置的翻译服务；Codex 工作流由当前对话完成校对和翻译。
- **自动工作流**：通过本地 MCP + Skill 下载、转写并交付视频、字幕、文稿、简介与封面，支持任务恢复和局部重转录。

## macOS：自行构建

当前构建脚本面向 **Apple Silicon（arm64）**。先安装 Homebrew 和 Xcode Command Line Tools（命令行开发工具，提供 `clang` 编译器），再完成以下步骤。

### 1. 安装依赖并运行

```bash
xcode-select --install
brew install python@3.12 ffmpeg git

git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements-macos-whisperx.txt
.venv/bin/python main.py
```

- `xcode-select --install` 打开命令行开发工具安装窗口；已安装时跳过，安装完成后再继续。
- `brew install` 安装 Python 3.12、FFmpeg 和 Git；FFmpeg 提供媒体处理所需的 `ffmpeg`、`ffprobe`。
- `git clone` 下载源码；`cd`（change directory，切换目录）进入项目根目录，后续命令在这里执行。
- `-m`（module，以模块运行）调用 `venv` 创建 `.venv` 虚拟环境，或调用 `pip` 安装依赖；`.venv/bin/python` 是该环境的解释器，无需激活环境。
- `--upgrade` 升级安装工具；`-r`（requirements，依赖清单）读取 macOS 依赖文件；最后一行启动桌面应用。

### 2. 构建日常使用的 App 启动器

确认源码可以正常启动后执行：

```bash
scripts/build_macos_app.sh --install
```

`--install` 将生成的启动器安装到 `/Applications/VideoCaptioner.app`，替换该位置已有的同名 App；构建产物位于项目内的 `dist/VideoCaptioner.app`。

这个 App 会调用当前源码目录中的 `.venv/bin/python main.py`，因此需要保留源码和 `.venv`，移动项目后需重新构建。修改源码后，重启 App 即可生效。

### 3. 可选：构建独立 App / DMG

需要包含 Python 运行时与 Python 依赖的独立应用时，在同一项目根目录执行：

```bash
scripts/build_macos_release.sh
```

脚本使用依赖中已包含的 PyInstaller 打包，输出：

- `dist/pyinstaller/VideoCaptioner.app`：独立应用。
- `dist/release/`：DMG 安装镜像与 SHA-256 校验文件。

文件名版本取自脚本的 `DEFAULT_VERSION`，不表示当前最新发行版本。产物仍需要系统 FFmpeg，且不内置转写模型或用户配置。脚本使用临时签名（ad-hoc），未进行 Apple 公证。

## Windows：目前从源码运行

EXE 发布前，先安装 **Python 3.12（64 位）、Git 和 FFmpeg**，确认 `ffmpeg`、`ffprobe` 已加入 `PATH`（可执行程序搜索路径）。在 PowerShell 中执行：

```powershell
git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe main.py
```

`git clone` 下载源码，`cd`（change directory）进入项目；`py -3.12` 选择 Python 3.12。`-m`（module）运行模块，`venv` 创建虚拟环境；`--upgrade` 升级安装工具，`-r`（requirements）读取依赖清单。`.\.venv\Scripts\python.exe` 是项目虚拟环境的解释器；后续也可双击项目内的 `VideoCaptioner.bat` 启动。

Windows 使用 WhisperX，支持 `auto / cuda / cpu` 转写设备及自动精度选择；CUDA 需要兼容的显卡、驱动和 Python 依赖，详见 [硬件与 Windows 配置](docs/codex-mcp.md#cpu--gpu-检查与-windows)。Windows 依赖不包含 MLX 或 Apple 框架。

## 首次使用与数据目录

### 模型与媒体工具

首次使用需要下载或配置兼容的本地模型，预留下载时间和磁盘空间。WhisperX 默认转写模型为 `large-v3-turbo`；MLX Whisper 默认为 `mlx-community/whisper-large-v3-turbo`。Codex 工作流的转写模型需先缓存，详见 [MCP 指南](docs/codex-mcp.md)。

| 内容 | macOS | Windows |
| --- | --- | --- |
| 应用数据 | `~/Library/Application Support/VideoCaptioner` | `%LOCALAPPDATA%\VideoCaptioner` |
| 模型 | 应用数据目录下的 `models` | 应用数据目录下的 `models` |
| 默认工作输出 | `~/Movies/VideoCaptioner` | 用户目录下的 `Videos\VideoCaptioner` |

`~` 表示当前用户主目录；`%LOCALAPPDATA%` 是 Windows 当前用户的本地应用数据目录环境变量。

### YouTube Cookie 与 PO Token

下载遇到认证要求时，可在应用中选择浏览器 Cookie 或导入 `cookies.txt`。Windows 默认浏览器来源为 Edge；浏览器加密策略可能影响自动提取。

部分 YouTube 自动字幕需要 PO Token。macOS 用户可按需在项目根目录执行：

```bash
scripts/setup_youtube_pot_provider.sh
```

该脚本需要 Node.js 20+、npm 和 Git，将提供器安装到 `~/Library/Application Support/VideoCaptioner/youtube-pot-provider`，不会启动常驻后台服务。Windows 需另行配置提供器，通过 `VIDEO_CAPTIONER_BGUTIL_SERVER_HOME` 环境变量指定其 `server` 目录；不能直接运行此 macOS 脚本。

## Codex 自动字幕工作流

新增本地 MCP + Skill 接入：提供视频链接，由硬件适配的本地 MLX / WhisperX 转录、当前 Codex 校对断句与翻译；按视频名建立任务目录，输出最终视频、原文/中文字幕、原文文稿及模板简介。最高画质下载遇到 VP9/AV1 时自动转为 HEVC，无需电脑操控或独立翻译 API。支持任务恢复和局部重转录。安装与使用见 [Codex MCP 指南](docs/codex-mcp.md)。

新建 MLX 字幕任务会在 Metal 识别后使用本地 WhisperX CPU 独立对齐时间；旧任务可通过 `realign_job` 保留译文并重新对齐。

### MCP、Skill 和 Codex 分别做什么

- **MCP**（Model Context Protocol，模型上下文协议）：提供本机下载、转录、任务恢复和字幕导出工具。
- **Skill**：随源码分发的 [工作流说明](skills/videocaptioner/SKILL.md)，指导 Codex 检查硬件、校对断句、翻译和交付。
- **Codex**：在当前对话中处理字幕文本；不调用软件另配的翻译 API。音频留在本机，字幕文本会进入 Codex 对话，仍使用当前 Codex 账户额度。

本分支的 MCP + Skill 目前采用**源码安装**。桌面应用打包与 MCP 安装是独立步骤；仅复制 `SKILL.md` 不会完成 MCP 注册；它们也不是 PyPI 上同名包的一部分。

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

## 支持格式

| 类型 | 格式 |
| --- | --- |
| 视频 | MP4、MKV、MOV、AVI、WebM、WMV、FLV、TS 等 |
| 音频 | MP3、WAV、AAC、FLAC、OGG、OPUS、M4A、WMA 等 |
| 字幕 | SRT、VTT、JSON、TXT |

## 致谢与许可

原项目由 [@WEIFENG2333](https://github.com/WEIFENG2333) 创建，本分支沿用上游 GPL-3.0 许可条款。再分发时请保留原项目版权与许可证信息。

下载核心参考了 [FluentYTDL](https://github.com/SakuraForgot/FluentYTDL) 的下载重试、认证恢复和 Cookie 校验实践。
