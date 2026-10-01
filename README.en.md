<div align="center">

# VideoCaptioner

Local transcription, subtitle editing and translation · Windows / macOS

**Language:** [简体中文](./README.md) | English

</div>

An enhanced fork of [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) for video downloading, local transcription, subtitle proofreading and translation. Use the desktop interface or automate a single-video workflow through Codex MCP + Skill.

## Distribution Plan and Status

**Windows EXE distribution is planned; macOS users build the app themselves.**

| Platform | Distribution | Current status |
| --- | --- | --- |
| Windows | A packaged EXE is planned | Desktop source entry point restored; EXE packaging and native Windows acceptance testing remain incomplete. Use the source setup below for now. |
| macOS (Apple Silicon) | Install dependencies and build locally | Source setup, a local App launcher, and standalone App / DMG build scripts are available. |

Future Windows EXE downloads will appear on this repository's [Releases page](https://github.com/MeguruNo1/video-captioner-macos-enhanced/releases). Dependencies and installation steps will be documented with that release. Historical macOS DMGs are no longer the primary installation path going forward.

This README documents this fork. The package of the same name on PyPI belongs to upstream and does not install these enhancements.

## Features

- **Video downloads:** Dedicated download center, browser cookies, pause/resume, MP4 normalization and HEVC conversion.
- **Local transcription:** MLX Whisper on Apple Silicon; WhisperX on CUDA / CPU with hardware-based device and precision selection.
- **Subtitle processing:** Word timestamps, alignment, semantic segmentation, repeated-segment cleanup, terminology/hotwords, editing and export.
- **Editing safeguards:** Merge consecutive subtitle rows, process current edits, and prevent file replacement during active tasks. API keys are masked by default.
- **Translation:** The desktop app uses your own cloud or local LLM service; the bundled public model has been removed; the Codex workflow proofreads and translates in the current conversation.
- Import paired corrected SRT files as style references for future Codex translations. New jobs snapshot the reference and return relevant bilingual examples per batch without changing timestamps.
- **Automation:** Local MCP + Skill delivers video, subtitles, transcripts, descriptions and covers, with event waiting, compact chained caption batches, adaptive boundaries, pre-translation checks, persistent review and resumable jobs. Covers can be prepared during transcription; partial re-transcription preserves other batches.

## macOS: Build It Yourself

The current build scripts target **Apple Silicon (arm64)**. Install Homebrew and Xcode Command Line Tools (which provide the `clang` compiler), then follow these steps.

### 1. Install Dependencies and Run

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

- `xcode-select --install` opens the developer tools installer. Skip it if already installed; complete installation before continuing.
- `brew install` installs Python 3.12, FFmpeg and Git. FFmpeg provides the `ffmpeg` and `ffprobe` media tools.
- `git clone` downloads the source; `cd` (change directory) enters the project root, where subsequent commands run.
- `-m` (module) runs `venv` to create the `.venv` virtual environment or `pip` to install dependencies. `.venv/bin/python` is its interpreter; activation is unnecessary.
- `--upgrade` updates packaging tools; `-r` (requirements) reads the macOS dependency file. The last command starts the desktop app.

### 2. Build the App Launcher

After confirming that the source app starts successfully, run:

```bash
scripts/build_macos_app.sh --install
```

`--install` copies the launcher to `/Applications/VideoCaptioner.app`, replacing any existing app there. The build output is `dist/VideoCaptioner.app` inside the project.

This launcher runs `.venv/bin/python main.py` from the current checkout. Keep the source and `.venv` in place, and rebuild after moving the project. Source changes take effect when you restart the app.

### 3. Optional: Build a Standalone App / DMG

To bundle the Python runtime and Python dependencies, run from the same project root:

```bash
scripts/build_macos_release.sh
```

The script uses PyInstaller, included in the dependencies, and produces:

- `dist/pyinstaller/VideoCaptioner.app`: Standalone application.
- `dist/release/`: DMG image and SHA-256 checksum file.

The filename version comes from the script's `DEFAULT_VERSION`; it does not identify the latest published release. The bundle still requires system FFmpeg and does not include transcription models or user configuration. The script applies an ad-hoc signature; the app is not Apple-notarized.

## Windows: Run From Source for Now

Until the EXE is released, install **64-bit Python 3.12, Git and FFmpeg**, with `ffmpeg` and `ffprobe` on `PATH` (the executable search path). Run in PowerShell:

```powershell
git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe main.py
```

`git clone` downloads the source; `cd` (change directory) enters it. `py -3.12` selects Python 3.12; `-m` (module) runs a module, and `venv` creates the virtual environment. `--upgrade` updates packaging tools; `-r` (requirements) reads the dependency file. `.\.venv\Scripts\python.exe` is the project's interpreter. Later, double-click `VideoCaptioner.bat` in the project to launch the app.

Windows uses WhisperX with `auto / cuda / cpu` device selection and automatic precision selection. CUDA requires compatible hardware, drivers and Python dependencies; see [hardware and Windows setup](docs/codex-mcp.md#cpu--gpu-检查与-windows). Windows dependencies exclude MLX and Apple frameworks.

## First Use and Data Locations

### Models and Media Tools

Download or configure compatible local models before use, allowing time and disk space for downloads. WhisperX defaults to `large-v3-turbo`; MLX Whisper defaults to `mlx-community/whisper-large-v3-turbo`. The Codex workflow requires the transcription model to be cached first; see the [MCP guide](docs/codex-mcp.md).

| Content | macOS | Windows |
| --- | --- | --- |
| App data | `~/Library/Application Support/VideoCaptioner` | `%LOCALAPPDATA%\VideoCaptioner` |
| Models | `models` under app data | `models` under app data |
| Default work output | `~/Movies/VideoCaptioner` | `Videos\VideoCaptioner` under the user directory |

`~` means the current user's home directory; `%LOCALAPPDATA%` is the Windows environment variable for local user app data.

### YouTube Cookies and PO Tokens

For downloads requiring authentication, select browser cookies in the app or import `cookies.txt`. Edge is the default source on Windows; browser encryption policies may prevent automatic extraction.

Some YouTube automatic captions require a PO Token. macOS users can run this optional setup from the project root:

```bash
scripts/setup_youtube_pot_provider.sh
```

It requires Node.js 20+, npm and Git, installs the provider under `~/Library/Application Support/VideoCaptioner/youtube-pot-provider`, and does not start a persistent background service. On Windows, configure the provider separately and set the `VIDEO_CAPTIONER_BGUTIL_SERVER_HOME` environment variable to its `server` directory; this macOS script cannot be run directly.

### Download component updates

Under **Settings → Download component updates**, choose daily, weekly, or disabled checks and notification-only (default) or automatic updates. Scheduled checks run while the desktop application is open. Manual check, install, and rollback actions are also available.

Updates manage only yt-dlp, the yt-dlp-ejs version required by its metadata, and matching versions of the bgutil PO Token plugin and local server. Transcription dependencies are unchanged. A new generation is prepared in the application data directory under `download-components`, validated, and then selected atomically. Failed updates keep the previous selection; running tasks keep their original generation. Restart the desktop application to activate it; newly launched MCP processes use the same selection. Import checks do not guarantee live website compatibility; rollback remains available.

Source installs and `.venv`-based app launchers can install updates with pip and Node.js 20+. Updating the local provider server also requires Git and npm 9+. Standalone bundles currently support version checks only; update the application package to upgrade their components. Logs are saved as `download-components/update-*.log`. Previous generations are retained for rollback and are not deleted while processes may still use them.

## Codex subtitle workflow

A local MCP server and Skill can download a video, transcribe with hardware-selected local MLX / WhisperX, and let the current Codex conversation proofread, segment, and translate captions. Each task uses a video-title directory and exports the final video, source/translated SRT, a proofread source transcript, and a template description. Highest-quality VP9/AV1 downloads are converted to HEVC. Jobs are resumable and support local re-transcription; no computer control or separate translation API is used. See the [Codex MCP guide](docs/codex-mcp.md).

New MLX caption jobs use local WhisperX CPU alignment after Metal transcription. Existing jobs can use `realign_job` to realign timing while preserving translations.

MLX transcription does not condition decoding on the previous recognition window's output, reducing context-driven omissions in mixed-language clips while retaining configured initial prompts and hotwords. This applies to both native timestamps and WhisperX alignment; existing captions require re-transcription to use the new policy.

When word timestamps are requested, MLX first uses native word timing to refine recognition intervals and decoder continuation points. In WhisperX mode, the independent acoustic model still recalculates the final word times. This reduces misalignment caused by coarse segments extending into other-language dialogue, but does not guarantee recovery of all omitted speech.

### Roles of MCP, the Skill, and Codex

- **MCP** (Model Context Protocol) exposes local tools for downloading, transcription, recovery, and export.
- **The Skill** is the [workflow instruction file](skills/videocaptioner/SKILL.md) shipped with this source tree. It guides hardware checks, proofreading, segmentation, translation, and delivery.
- **Codex** processes subtitle text in the current conversation without a separate translation API. Audio stays local; subtitle text enters the conversation and normal Codex usage applies.

This fork currently distributes MCP + Skill through a **source installation**. Desktop packaging and MCP installation are separate steps; copying only `SKILL.md` does not register the MCP server. The upstream PyPI package does not contain this fork's integration.

### Install MCP and the Skill

Complete the platform-specific source setup above first. Keep the checkout and `.venv` in place. You need configured Codex access, the `codex` CLI on your terminal's `PATH`, and `ffmpeg`/`ffprobe`. Run these commands from the **project root**.

**macOS:**

```sh
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/install_codex_mcp.py --dry-run
.venv/bin/python scripts/install_codex_mcp.py
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py --dry-run
.\.venv\Scripts\python.exe scripts/install_codex_mcp.py
```

The first line installs MCP dependencies using the project's Python: `-m` runs a module, and `-r` reads a requirements file. `--dry-run` previews registration; the last line registers the server and copies the Skill without starting a media job. `bin/python` and `Scripts\python.exe` are the macOS and Windows virtual-environment interpreters respectively.

The installer writes the server entry into Codex's `config.toml` and copies the Skill into `skills/videocaptioner` under `CODEX_HOME`, or `.codex` in your home directory when unset. It backs up existing configuration and preserves other services. A conflicting server path or an unmanaged Skill of the same name stops installation with an explanation. Reload MCP and Skills after installation or updates, or restart Codex and open a new task. The server uses local standard input/output, with no listening network port; see the [official OpenAI MCP documentation](https://developers.openai.com/codex/mcp/).

### Use it

Ask Codex, replacing `VIDEO_URL` with one actual video URL:

> Use videocaptioner for VIDEO_URL. Translate English into Simplified Chinese. Check local acceleration first, then deliver the video, bilingual subtitle files, and covers.

You can also select the `/videocaptioner` Skill and provide a link. To pin a device, say “use WhisperX CUDA,” “CPU only,” or “use MLX.” Codex first calls `check_environment` and reports its selection and missing dependencies. Cache the transcription model locally before starting; WhisperX may download VAD/alignment models on first use.

- **Acceleration:** Auto prefers usable Apple Silicon MLX Metal, otherwise usable WhisperX CUDA or CPU. Video encoding separately follows the Auto / NVIDIA NVENC / Intel QSV / AMD AMF / CPU setting.
- **Resume/cancel:** Ask “continue job ID …” or “cancel job ID …,” using the ID returned at startup. If lost, ask Codex to list recent jobs. Existing jobs retain their saved settings.
- **Delivery:** The task's `output` directory contains the final video, source/translated SRT, source transcript, description, and two covers; `flow` holds intermediate files. The default cover workflow also requires image editing tools in the current Codex environment.
- **After closing Codex:** Local downloading/transcription can continue; proofreading and translation require returning to a Codex conversation.
- **Updates:** After obtaining newer source in the same checkout, repeat the three platform-specific commands to update dependencies, preview registration, and sync the Skill. Reload MCP afterwards. Moving or deleting the checkout or virtual environment breaks the registered absolute paths.

See the [MCP guide](docs/codex-mcp.md) for parameters, directories, timestamp validation, and recovery rules.

## Supported Formats

| Type | Formats |
| --- | --- |
| Video | MP4, MKV, MOV, AVI, WebM, WMV, FLV, TS and more |
| Audio | MP3, WAV, AAC, FLAC, OGG, OPUS, M4A, WMA and more |
| Subtitle | SRT, VTT, JSON, TXT |

## Credits and License

The original project was created by [@WEIFENG2333](https://github.com/WEIFENG2333). This fork follows upstream's GPL-3.0 license terms. Preserve the original copyright and license information when redistributing.

The download core draws on [FluentYTDL](https://github.com/SakuraForgot/FluentYTDL) practices for retries, authentication recovery and cookie validation.
