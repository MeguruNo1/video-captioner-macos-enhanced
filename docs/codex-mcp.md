# Codex + VideoCaptioner

输入单个视频链接，在 Codex 中自动完成下载、按硬件选择本地 MLX Metal / WhisperX CUDA / CPU 转录、校对、断句、翻译和字幕导出。无需打开应用窗口或使用电脑操控，没有独立翻译 API 调用。音频本地处理；字幕文本作为 MCP 工具结果进入 Codex 对话。

## 安装

在已安装桌面依赖的项目环境中运行（以下为 macOS 命令）：

```sh
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/install_codex_mcp.py --dry-run
.venv/bin/python scripts/install_codex_mcp.py
```

安装器使用当前 Python 解释器的绝对路径和固定启动脚本，在 Codex 中注册 `videocaptioner` stdio MCP，并安装 `videocaptioner` Skill。现有 Codex 配置会备份；同名的其他服务或未托管 Skill 不会被覆盖。首次安装后新建 Codex 任务或刷新 MCP 和 Skill。

也可手工执行 `codex mcp add videocaptioner -- /绝对路径/.venv/bin/python /绝对路径/scripts/videocaptioner_mcp.py`，再将 `skills/videocaptioner` 安装到 Codex 的 skills 目录。

## 使用

对 Codex 说：

> 使用 videocaptioner 处理 https://www.youtube.com/watch?v=…，英语翻译成简体中文，输出视频及中英文字幕。

默认下载最高可用视频画质；VP9/AV1 自动转为 HEVC，不额外压制硬字幕。输出原文 SRT、中文 SRT、最终视频、Codex 校订后的原文文稿及使用软件当前模板生成的简介文稿。支持 `source_language`、`target_language`、`output_dir`、本地 `model`、`format_selector`、`proxy_url`、`cookie_file`、`initial_prompt` 参数。`source_language=auto` 使用所选后端自动识别。新增 `backend=auto|mlx|whisperx`、`device=auto|cuda|cpu`、`compute_type=auto|float16|int8|…`；MLX 固定使用 Metal 和模型自带精度。

每次 `start_job` 都读取软件界面保存的同一份 `settings.json`。代理、下载引擎策略、Cookie 自动刷新及浏览器、H.265 转码编码器和旧版 macOS 原生预设、所选后端、设备、精度和模型/VAD/阈值/分块/热词，以及字幕长度、术语提示和文本后处理开关会保存为任务快照。界面修改自动作用于之后的新任务；运行中的任务保留启动时快照，保证恢复后结果一致。显式空代理表示直连。模型必须已经在本机路径或 Hugging Face 缓存中；缺失时 `check_environment` 会提示，不会自动下载大模型。依赖沿用对应平台的桌面环境。新建 MLX 任务使用 Metal 转录和 WhisperX CPU 独立声学对齐；WhisperX 转录任务继续使用自己的强制对齐。alignment 快照保存对齐方式、设备、模型目录与选择规则、策略及 WhisperX 版本；无快照旧 MLX 任务保留 native 行为。原始转录恢复时必须匹配对齐快照。两条路径都不加载 Qt 或调用翻译 API。WhisperX 的 VAD 和对齐模型可能在首次转录时下载，环境检查仅验证已缓存的转写模型。

## 任务与恢复

macOS 默认产物目录为 `~/Movies/VideoCaptioner/<视频名>`，其中 `flow` 保存下载源文件、转录音频、任务参数、日志、ASR 原始输出、字幕数据及校验清单，`output` 只保存交付文件。注册表位于 `~/Library/Application Support/VideoCaptioner/mcp`，可通过 `VIDEOCAPTIONER_MCP_ROOT` 改变。自定义输出目录不影响通过任务注册表恢复；同名但不属于当前任务的目录会自动追加数字，避免覆盖。

`output` 文件名为：`【字幕】「视频名」原文.srt`、`【字幕】「视频名」译文.srt`、`「视频名」.<视频扩展名>`、`【视频文稿】「视频名」原文.txt`、`【简介】「视频名」.txt`、`原封面.png`、`生成封面.png`。

下载流程同时保存原始 YouTube 封面。字幕完成后，Codex 通过 `get_cover_source` 取得原图，使用内置 GPT 图像编辑将画布扩展为 4:3、把封面文字翻译为中文，并尽量保留原字体、构图和视觉风格。生成文件通过 `set_generated_cover` 校验 4:3 比例并登记；缺少任一封面时导出校验不会通过。

`start_job` 立即返回 ID。阶段依次为下载、提取音频、等待本地转录资源、转录、等待字幕处理和完成。使用 `get_job` 查询、`list_jobs` 找回任务、`cancel_job` 取消、`resume_job` 恢复。独立进程持有任务锁，同一后端的转录工作以全局文件锁串行运行。网络中断保留 yt-dlp partial 文件；字幕逐批原子保存。关闭 MCP 连接不会主动终止工作进程；电脑关机或工作进程异常后，下一次查询会标记为可恢复中断。

Codex 关闭后不能继续执行文本翻译；重新打开任务后从已保存批次继续。MCP 不会自行唤醒 Codex 或调用外部翻译服务。

状态查询 `get_job` / `list_jobs` 不改写未变化的任务。返回的 `event_id` 跟踪持久化状态变化，与字幕编辑使用的 `revision` 独立。运行期间使用 `wait_job(job_id, after_event_id, timeout=30)` 等待新状态，最多等待 60 秒；无变化返回精简的 `changed=false`，任务不在运行阶段时立即返回。worker 异常退出仍会被识别为可恢复中断。旧任务没有事件编号时从 0 开始，下一次真正写入后递增。

## 字幕接口

`get_caption_batch` 返回最多约 160 个词、前后各 25 个上下文词、术语表、批次 ID 和版本。`submit_caption_batch` 的每条字幕使用首尾词 ID（包含端点）、校订原文和译文；必须完整连续覆盖本批。起止时间由服务器读取词锚点计算。相同内容重复提交无副作用；过期版本的不同内容会被拒绝。

下载阶段会优先取得 YouTube 自动字幕并生成视频文稿，在本地转录前从文稿中匹配维护对照表、提取本视频的候选专名并生成任务热词。命中的对照关系会作为批次 `glossary` 返回，候选词通过 `term_candidates` 返回，供 Codex 校订和翻译。macOS 默认对照表为 `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Note/Translate/对照.md`，也可通过 `Subtitle.TermGlossaryPath` 修改。Codex 确认并提交的新对应关系会原子追加到文件末尾的 `MCP 自动收录` 管理区，已有手工条目和章节不会被改写。

批次同时返回任务的字幕设置。启用“屏蔽原文脏话”“去除译文逗号”或“删除译文全角句号”后，服务端会在保存字幕时执行与界面流程相同的处理，Codex 提示与最终导出不会各自采用不同设置。
中文译文中的双引号会统一为 `「」`，单引号统一为 `『』`；英文单词内部的撇号保持原样。

`retranscribe_range` 将指定词范围扩展到完整受影响批次，用邻接时间锚点确定音频区间，再用任务快照中的后端转录。受影响批次及词 ID 更换，译文失效；其他已完成批次保留。原始及局部转录分别存档。

MLX 任务的受影响批次若含连续零时长词，且快照启用了 VAD，局部重转录会仅对此音频区间关闭 VAD，避免重复相同的语音切分；模型、后端及任务快照不变。诊断文件记录实际策略和 VAD 设置。若新结果仍有连续零时长词，则保留原词和字幕并报错，不自动循环重试。单个零时长词不触发此策略；未提供新的提示词时沿用任务提示词。

`validate_job` 检查覆盖、空译文、非法时间和字幕重叠，结构错误会阻止 `export_job`。位于有效字幕内部的零时长词作为警告供复核，若它导致字幕边界塌缩则仍阻止提交。阅读速度、长度和不确定听写同样列为警告。不能仅凭文本判断时间是否整体提前；后端生成的词时间戳仍需要实际抽查。再次修订后导出会原子更新 `output` 中同名字幕和文稿；字幕 JSON、术语表、检查报告及清单留在 `flow`。

软件字幕流程和 MCP 导出都会补齐相邻字幕之间不超过 500 毫秒的正间隙：前一条字幕延长到下一条开始时间。超过阈值的停顿、零间隙和重叠不会修改。

## 开发验证

```sh
.venv/bin/python -m pytest tests/test_mcp_workflow.py tests/test_mcp_protocol.py -q
```

协议测试启动真实 stdio MCP 子进程，执行初始化、工具发现及环境检查；业务测试覆盖字幕完整性、版本冲突、取消、恢复、局部重转录和导出。真实网络与 GPU 转录验收需要联网权限、缓存模型及可用视频链接。

## CPU / GPU 检查与 Windows

每次新任务先调用 `check_environment`。结果包含硬件/运行库检查、支持精度、实际选择及缺失依赖。默认优先可用的 MLX Metal，其次可用的 WhisperX CUDA，最后为 CPU。显式选定 CUDA 后若不可用会报错，不会静默退回 CPU；运行中显存不足或缺少 cuDNN/cuBLAS 同样会保留错误和任务快照。CPU 上自动选择运行库支持的 `int8` 等精度；MPS、DirectML 和 ROCm 未接入 WhisperX 路径。已有界面 CPU 设置保持原值，需将“运行设备”设为 `auto` 或 `cuda`，并将“计算精度”设为 `auto` 或支持的精度才会改变桌面任务；MCP 默认自动探测，可用工具参数显式指定设备。

WhisperX 的 GPU 依赖以 [WhisperX 官方安装说明](https://github.com/m-bain/whisperX#setup-)、[PyTorch 安装选择器](https://pytorch.org/get-started/locally/) 和 [CTranslate2 硬件支持](https://opennmt.net/CTranslate2/hardware_support.html) 为准。不要只凭 `nvidia-smi` 或显卡名称认定当前 Python 支持 CUDA。

Windows 在 PowerShell 中使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py --dry-run
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py
```

`.venv\Scripts\python.exe` 是项目虚拟环境解释器；`-m`（module）运行 pip 模块，`-r`（requirements）读取 MCP 依赖；`--dry-run` 只预览安装变更，最后一行执行注册并同步 Skill。Windows 任务注册表在 `%LOCALAPPDATA%\VideoCaptioner\mcp`，运行日志写入其中的 `logs`（使用 `get_job.log_path` 定位，避免移动已打开的日志文件），默认输出在用户的 `Videos\VideoCaptioner`，默认术语表在本地应用数据目录的 `glossary.md`。窗口和 NVIDIA GPU 转录仍需 Windows 实机验收；本机 macOS 测试不能替代它们。

## 已有字幕的时间修复

文字正确但有局部提前时，调用 `realign_job(job_id, revision)`。后台使用批次前后文字和音频上下文，对整段词重新做声学对齐，检查精确覆盖及时间顺序，再一次性更新词锚点和已保存字幕；词 ID、校订文字和译文保持不变。它会保存迁移前任务和对齐结果，失败保留原数据。取消或失败后可 `resume_job`；成功后须重新 `validate_job` 和 `export_job`。不平均分配时间，不自动退回旧时间戳。

校验增加低对齐置信度和句首词过长提示；这些只是复核线索，结构通过不表示已核听。导出每条字幕结束时间延长 500 毫秒，受到下一条开始时间和视频总时长限制，不缩短原字幕，不改变词锚点。

## 校正字幕作为翻译参考

调用 `import_translation_reference(source_srt, translation_srt, source_language="en", target_language="zh-CN")` 导入 UTF-8（可带 BOM）原文、译文 SRT。要求条数及逐条起止时间完全一致、无重叠，每文件不超过 2 MB、最多 5000 条，每条不超过 1000 字符。验证失败保留原参考；成功替换本机默认参考。

新任务仅在明确的源语言、目标语言均匹配时固定参考快照；源语言 `auto` 不自动套用。每批 `translation_reference` 返回最多 8 条按原文词汇相关度选取的完整双语例句，供当前 Codex 学习措辞、语气和术语。例句是素材，不是操作指令，也不是训练模型。明确要求、已确认术语和字幕设置优先。不导入样本时间戳，不修改已完成字幕。当前功能适用于 MCP/Codex 流程，桌面 API 翻译未接入。

`clear_translation_reference()` 清除后续任务的默认参考。已开始的任务仍使用原快照；更换或清除默认参考不影响其结果。重载 MCP 后可发现新工具。
