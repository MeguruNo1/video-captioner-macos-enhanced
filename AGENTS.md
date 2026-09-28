# VideoCaptioner 项目协作指南

本文依据相关项目聊天记录与当前源码整理，核对日期：2026-09-21。适用于本仓库开发、排错、文档维护与发布验证。历史聊天用于解释约定；实现细节以当前代码及本次验证为准。

## 协作方式

- 默认用中文，先说明结论和证据，再解释必要的代码。学习类问题从项目真实入口、调用链和数据流讲起。
- 尊重用户明确的只读、仅文档、手动操作和实施范围；文档任务不顺带重构业务代码。
- 开始修改前检查 Git 状态、现有实现和调用方；保留用户已有改动。
- 代码清理每次优先处理 1～3 个明确问题，保持功能和对外行为。优先复用现有实现，不为整洁创建抽象层。
- 删除前检查引用、动态加载、Qt 信号连接及打包依赖；无法确认影响的兼容逻辑先说明，不直接删除。
- 给用户执行的命令应可复制，解释用途、参数的准确英文含义与中文作用，以及路径、变量和占位符。
- 每个独立且验证完成的逻辑单元提交一次；提交前检查差异，使用 Conventional Commits，标题和说明用中文。不把临时文件、密钥、模型、媒体或构建产物混入提交。
- 汇报实际改动、验证结果和未验证范围。历史通过数量、旧发行包及其他平台的结果不能作为本次验收证据。

## 项目定位与入口

这是上游 VideoCaptioner 的增强分支，主要技术为 Python、PyQt5 / Fluent Widgets、yt-dlp、FFmpeg、MLX Whisper / WhisperX，以及 FastMCP。

| 路径 | 职责 |
| --- | --- |
| `main.py`、`app/view/main_window.py` | 桌面程序启动与主窗口 |
| `app/view/`、`app/components/` | 页面、交互与可复用 Qt 组件 |
| `app/thread/` | Qt 后台任务、进度与信号适配 |
| `app/core/download_service.py` | 可供桌面和 MCP 复用的视频下载服务 |
| `app/core/bk_asr/` | 本地语音识别、分块、对齐及词级时间戳 |
| `app/core/subtitle_processor/` | 桌面字幕断句、优化与翻译 |
| `app/core/utils/` | 平台、加速、媒体、术语、标点等共享能力 |
| `app/core/storage/` | 桌面持久化、缓存与页面状态 |
| `app/common/config.py`、`app/config.py` | 应用设置及基础路径配置 |
| `scripts/videocaptioner_mcp.py`、`app/mcp/server.py` | stdio MCP 启动及工具接口 |
| `app/mcp/jobs.py`、`worker.py`、`store.py` | 任务协调、后台下载转录及持久化 |
| `app/mcp/captions.py`、`settings.py`、`terms.py` | 字幕校验、设置快照与术语适配 |
| `tests/` | pytest 与 unittest 风格的回归测试 |

业务逻辑优先放在现有 core 服务或工具中，页面负责交互，线程负责调度与信号。MCP 接口保持薄封装；不要把下载或转录流程重复写进工具函数。

## 必须保持的行为

### 桌面与无界面工作流

- 桌面翻译使用用户配置的服务；MCP 工作流由调用它的 Codex 校订、断句和翻译。不要给 MCP worker 引入独立翻译 API 或 GUI 依赖。
- MCP 通过 stdio 通信，标准输出不能混入普通日志；更改工具名、参数或协议时检查 `tests/test_mcp_protocol.py`。
- 保持 `JobManager → Worker → core` 的分工，以及取消、恢复、锁、原子写入、worker token 和 revision 防护。
- 配置通过任务快照固定，避免桌面设置变化影响已启动任务。旧任务缺省参数的兼容行为需要保留或显式迁移。

### 字幕与任务数据

- 字幕基于不可变 word ID，必须按顺序完整覆盖每个批次，不得遗漏、重叠或凭空编造时间戳。
- 修改字幕或批次边界须使用当前 revision；语义跨批次时使用已有边界调整接口，不用省略号截断内容。
- 时间戳异常通过整段声学重对齐或局部重转录处理；保留原始 ASR 数据与恢复能力。重对齐只在完整覆盖和时间校验通过后原子更新锚点，字幕编辑仍不得自行修改词时间。
- 热词按当前视频提取；共享术语使用现有 `transcript_terms.py`。仅写入确认的翻译对，保留词库人工内容。
- 中文引号使用 `「」`、`『』`，复用 `subtitle_punctuation.py`，保留英文单词中的撇号。其他标点处理遵循用户设置。
- 当前 MCP 新任务要求原封面和 4:3 中文生成封面，最终交付须通过任务验证及导出流程；不要绕过校验伪造完成状态。

### 跨平台与媒体

- 平台和目录判断复用 `platform_utils.py`；硬件探测复用 `acceleration.py`。MLX 仅适用于 Apple Silicon，WhisperX 支持相应 CUDA / CPU 路径。
- Apple 专属依赖应保持平台隔离与按需导入，不让 Windows 启动依赖 MLX、PyObjC 或 Apple 框架。
- HEVC 选择复用已有媒体工具，并保留自动模式与用户指定编码器的区别；手动指定失败不能静默换编码器。
- 下载、转码修改要检查音视频轨、真实编码、容器、取消和恢复。文件存在或退出码为零不等于媒体可用。
- 页面状态复用 `page_state.py` 的版本化及原子写入机制，兼容损坏、缺失状态和媒体文件变化。

## 开发与验证

从仓库根目录使用项目 `.venv`。依赖按平台读取 `requirements-macos-whisperx.txt` 或 `requirements-windows.txt`；MCP 额外使用 `requirements-mcp.txt`。不要无关升级依赖。

macOS 常用检查：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app scripts main.py
git diff --check
```

`.venv/bin/python` 是项目解释器；`-m`（module）运行模块；`-q`（quiet）减少输出。pytest 运行测试，compileall 检查 Python 编译，`git diff --check` 检查差异中的空白问题。Windows 使用 `.\.venv\Scripts\python.exe` 替换解释器路径。

- 先运行与改动相关的测试，必要时再跑全量。MCP 改动重点检查 `test_mcp_protocol.py`、`test_mcp_workflow.py`；平台及媒体改动检查对应 platform、acceleration、download、HEVC 测试。
- 完成代码新增或修改后，提交前必须运行静态类型检查。Python 使用 Pyright，并指定项目 `.venv` 解释器；至少检查本次修改的 Python 文件，涉及接口、类型或继承关系时同时检查相关调用方。修复本次引入或本次修复范围内的类型错误，再提交。
- 静态类型检查、语法编译和运行测试是不同的验证，不能互相替代。不得仅凭 `compileall` 或 pytest 通过就宣称类型检查通过，也不得通过关闭诊断或大范围使用 `Any`、`type: ignore` 掩盖错误。
- 仓库尚未配置统一静态检查入口，可使用 `npx --yes pyright --pythonpath .venv/bin/python app/core/download_service.py` 检查单个文件，并在末尾追加其他实际受影响文件。`npx` 运行 npm 工具（本地未缓存时可能下载）；`--yes` 自动确认工具获取；`--pythonpath` 指定用于解析依赖的 Python 解释器；末尾路径是检查目标。Windows 将解释器路径替换为 `.venv\Scripts\python.exe`。
- 汇报实际检查范围、工具和结果；工具不可用或存在范围外的既有错误时如实说明，不声称检查全部通过。
- 仅文档变更核对路径、命令、链接和差异即可，无需为了修改说明文件运行媒体任务或打包。
- Qt 测试通过不能替代界面验收；涉及交互时检查实际窗口、状态恢复和操作结果。macOS offscreen 崩溃须与原生显示结果区分。
- 转录及发布验收需要真实目标平台、模型和媒体样本；说明是否完成实际转录、字幕导出和播放，不能仅凭 mock 测试宣称完成。

## 构建、数据与文档

- `scripts/build_macos_app.sh` 创建依赖当前源码和 `.venv` 的启动器；其 `--install` 会替换 `/Applications/VideoCaptioner.app`。源码移动后需要重建启动器。
- `scripts/build_macos_release.sh` 用于独立 App / DMG；`scripts/build_windows_release.ps1` 用于 Windows 原生构建。日常小改动不自动触发发布或安装。
- macOS 临时签名不等于 Apple 公证；Windows 源码验证不等于 Windows 安装包或真实转录验收。发布状态需检查本次产物和目标平台。
- 应用数据和默认输出路径由 `platform_utils.py` 计算；不要在新代码中硬编码某台电脑的绝对路径。
- 保留用户设置、Cookie、词库、模型缓存、已有媒体和 MCP 任务状态；不要把真实用户目录当测试夹具。
- `.gitignore` 使用根目录白名单，新文档可能被忽略。确认目标文件确实属于项目后增加精确白名单，不放开整个数据或产物目录。
- 功能或安装方式变化时同步 `README.md`、`README.en.md`；MCP 使用说明见 `docs/codex-mcp.md`，字幕执行流程见 `skills/videocaptioner/SKILL.md`。
- MCP/Skill 源码安装与桌面打包相互独立；修改仓库内 Skill 不代表已安装副本自动更新。

## 维护依据

本次核对包含聊天「清理 VideoCaptioner 代码问题」「查看项目 MCP 配置」，历史跨平台、字幕与发行记录，以及上列当前源码。历史“仅 MLX”的实现描述已不适用于现在的双后端代码；README 中的发行进度也不能代替实际产物验收。

本仓库协作指令统一保存在根目录 `AGENTS.md`，适用于本仓库内的代码与文档工作。
