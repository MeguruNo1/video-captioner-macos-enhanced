<div align="center">

# VideoCaptioner macOS Fork

**Language:** [简体中文](./README.md) | English

</div>

This is a macOS/Apple Silicon local-workflow fork of [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner). It is not a full replacement for the upstream project. It keeps the desktop subtitle-processing experience and makes larger scoped changes around local macOS transcription, downloads, subtitle splitting, and translation reliability.

This README focuses on the differences from upstream. For the full project overview, online documentation, CLI usage, and official releases, refer to the [upstream repository](https://github.com/WEIFENG2333/VideoCaptioner).

## Main Differences From Upstream

| Area | Upstream | This fork |
| --- | --- | --- |
| Project shape | Cross-platform CLI + GUI + PyPI package + documentation site | macOS / Windows source-checkout GUI branch |
| Supported platforms | Windows, macOS, and Linux | macOS; Windows desktop source support restored, native validation pending |
| Local ASR | Multiple backends: `faster-whisper`, `whisper-api`, Bijian, Jianying, `whisper-cpp`, and others | Focused on WhisperX CUDA / CPU, with MLX Whisper added as an Apple Silicon GPU backend |
| Timestamp strategy | Depends on each ASR backend's capabilities | Built around word-level timestamps, VAD, and WhisperX alignment |
| Download flow | General upstream download command and desktop entry points | Dedicated download center with stronger yt-dlp handling, browser cookies, final MP4 normalization, and HEVC fallback |
| Subtitle processing | General upstream subtitle splitting, optimization, translation, and synthesis | Stricter splitting, short-gap handling, repeated ASR cleanup, terminology/hotword handoff, and more defensive LLM translation |
| Runtime model | `pip install videocaptioner`, `uv run videocaptioner`, and both CLI/GUI entry points | Source checkout, `.venv`, and a local `.app` launcher |
| Packaging and docs | Upstream PyPI, CI, VitePress documentation, and multi-platform build scripts | Upstream release pipeline is removed or de-emphasized in favor of smaller macOS local-run docs |

## What This Fork Focuses On

- Local subtitle workflows on Apple Silicon Macs.
- WhisperX CUDA / CPU transcription with automatic device/precision selection and WhisperX alignment.
- Optional MLX Whisper backend through `mlx-whisper` for Apple Silicon GPU use.
- App data stored under `~/Library/Application Support/VideoCaptioner`.
- Work files stored under `~/Movies/VideoCaptioner` by default.
- A local `.app` launcher that runs `.venv/bin/python main.py` from this checkout.

## Out of Scope Here

- The PyPI package and `videocaptioner` CLI are no longer the main entry points.
- The full upstream documentation site, release workflows, and multi-platform packaging pipeline are not maintained here.
- This fork does not try to cover every upstream ASR, TTS, CLI, or cross-platform feature.

## Download a Release

This fork publishes Apple Silicon macOS builds as `.dmg` installers:

```text
https://github.com/MeguruNo1/video-captioner-macos-enhanced/releases
```

Current version: `macos-enhanced-v0.1.2`

This release adds the local Codex subtitle workflow, forced alignment for MLX Whisper word timestamps, customizable download-description templates, browser-cookie refresh at startup, safer subtitle splitting around title abbreviations, and separate pause/terminate controls for resumable downloads.

Install steps:

1. Download the latest `VideoCaptioner-macos-enhanced-*.dmg`.
2. Open the DMG and drag `VideoCaptioner.app` into `Applications`.
3. If macOS blocks the first launch, right-click `VideoCaptioner.app` and choose `Open`.

The release app bundles the Python application runtime and Python dependencies, but it does not bundle FFmpeg, WhisperX/MLX Whisper models, or user settings. Install FFmpeg before processing media:

```bash
brew install ffmpeg
```

WhisperX and MLX Whisper models download on first use, or are loaded from the existing local model directory:

```text
~/Library/Application Support/VideoCaptioner/models
```

## Windows Source Setup

The desktop entry point now supports Windows with WhisperX CUDA / CPU transcription with automatic device and precision selection, downloads, subtitle editing, and translation. See the official [WhisperX CPU instructions](https://github.com/m-bain/whisperX). Regression tests were run on macOS; installation, transcription, and UI acceptance on native Windows remain unverified. No Windows EXE installer is provided.

Install 64-bit Python 3.12, Git, and FFmpeg first. Ensure both `ffmpeg` and `ffprobe` are on the system `PATH`, then run in PowerShell:

```powershell
git clone https://github.com/MeguruNo1/video-captioner-macos-enhanced.git
cd video-captioner-macos-enhanced
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe main.py
```

`git clone` downloads the source; `cd` changes directory. `py -3.12` selects Python 3.12; `-m` runs a module; `venv` creates the `.venv` environment. Calling its Python directly avoids activation. `--upgrade` updates installation tools; `-r` reads the requirements file. The last command launches the app; subsequent launches can use `VideoCaptioner.bat`.

- Data/models: `%LOCALAPPDATA%\VideoCaptioner`; output: `Videos\VideoCaptioner` under the user directory.
- Cookie source defaults to Edge; Chrome and manual `cookies.txt` import are available. Browser encryption may prevent extraction.
- Windows excludes MLX/PyObjC, uses FFmpeg and tray notifications, and supports `auto / cuda / cpu` transcription with automatic precision selection. Existing explicit CPU settings are retained; select auto or CUDA in settings to change them. See the [MCP guide](docs/codex-mcp.md) for GPU dependencies.
- The `/videocaptioner` MCP workflow inspects hardware and selects MLX Metal, WhisperX CUDA or CPU. The macOS PO Token setup script does not run on Windows; configure a provider separately if needed and set `VIDEO_CAPTIONER_BGUTIL_SERVER_HOME` to its `server` directory.

## Run From Source

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

YouTube may require a subtitle-specific PO Token. After the first source setup,
install the local token provider so yt-dlp can list affected automatic captions:

```bash
scripts/setup_youtube_pot_provider.sh
```

The script requires Node.js 20+, npm, and Git. It installs the provider under
`~/Library/Application Support/VideoCaptioner/youtube-pot-provider` and does not
run a persistent background service.

WhisperX downloads transcription, VAD, and alignment models on first use unless compatible local models already exist under:

```text
~/Library/Application Support/VideoCaptioner/models
```

`large-v3-turbo` is the default local model for this branch. It should be kept under the app model directory as `faster-whisper-large-v3-turbo`.

MLX Whisper uses `mlx-community/whisper-large-v3-turbo` by default. You can also use other Hugging Face MLX Whisper repos or a local MLX Whisper model directory converted with `mlx-examples/whisper`.

## Install Local App Launcher

The local app bundle is only a launcher for this source checkout and virtual environment. It does not bundle Python, dependencies, or models; edit the source tree and restart the app to run updated code.

```bash
scripts/build_macos_app.sh --install
```

## Build a Release Installer

Release DMGs are built with PyInstaller and written to `dist/release/`:

```bash
VIDEO_CAPTIONER_VERSION=macos-enhanced-v0.1.2 scripts/build_macos_release.sh
```

The output app includes the Python runtime and Python dependencies, but still expects system FFmpeg and downloads ASR models on first use.

## Supported Formats

| Type | Formats |
| --- | --- |
| Video | MP4, MKV, MOV, AVI, WebM, WMV, FLV, TS, and more |
| Audio | MP3, WAV, AAC, FLAC, OGG, OPUS, M4A, WMA, and more |
| Subtitle | SRT, VTT, JSON, TXT |

## Upstream and License

The original project was created by [@WEIFENG2333](https://github.com/WEIFENG2333). This fork is based on that project and follows the original GPL-3.0 license terms. If you publish this fork, keep the original copyright and license information.

## Codex subtitle workflow

A local MCP server and Skill can download a video, transcribe with hardware-selected local MLX / WhisperX, and let the current Codex conversation proofread, segment, and translate captions. Each task uses a video-title directory and exports the final video, source/translated SRT, a proofread source transcript, and a template description. Highest-quality VP9/AV1 downloads are converted to HEVC. Jobs are resumable and support local re-transcription; no computer control or separate translation API is used. See the [Codex MCP guide](docs/codex-mcp.md).

The shared download core incorporates production lessons documented by [FluentYTDL](https://github.com/SakuraForgot/FluentYTDL): yt-dlp's default YouTube client strategy, concurrent fragments with the full retry budget, staged authentication recovery, cleanup of stale partial state after 403 responses, and atomic cookie replacement only after candidate validation. Download Center, preview parsing, MCP, and compatibility threads all use these rules.
