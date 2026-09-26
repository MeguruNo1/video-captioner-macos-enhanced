"""Read the desktop application's persisted settings for headless MCP jobs."""
import json
from pathlib import Path

from .terms import DEFAULT_GLOSSARY_PATH


from app.core.utils.platform_utils import app_data_dir, DEFAULT_COOKIE_BROWSER_LABEL

SETTINGS_PATH = app_data_dir("VideoCaptioner") / "settings.json"


def read_shared_settings(path=SETTINGS_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def workflow_settings_snapshot(settings):
    """Normalize a JSON-safe snapshot so one job cannot change midway through."""
    download = settings.get("Download") or {}
    mlx = settings.get("MLXWhisper") or {}
    subtitle = settings.get("Subtitle") or {}
    whisperx = settings.get("WhisperX") or {}
    strategy = download.get("EngineStrategy", "智能选择")
    if strategy not in {"单线程", "多线程", "智能选择"}:
        strategy = "智能选择"
    return {
        "schema_version": 1,
        "settings_path": str(SETTINGS_PATH),
        "download": {
            "engine_strategy": strategy,
            "hevc_encoder": download.get("HevcEncoder", "auto"),
            "native_hevc_preset": download.get("NativeHevcPreset", "highest_quality"),
            "auto_refresh_cookies": bool(download.get("AutoRefreshEdgeCookies", False)),
            "cookie_browser": download.get("CookieBrowser", DEFAULT_COOKIE_BROWSER_LABEL),
        },
        "mlx": {
            "model": mlx.get("Model") or "mlx-community/whisper-large-v3-turbo",
            "initial_prompt": str(mlx.get("InitialPrompt") or ""),
            "hotwords": str(mlx.get("Hotwords") or ""),
            "vad_enabled": bool(mlx.get("VadEnabled", True)),
            "vad_threshold": _number(mlx.get("VadThreshold"), 0.5, 0.0, 1.0),
            "chunk_duration": int(_number(mlx.get("ChunkDuration"), 600, 60, 1800)),
            "chunk_overlap": int(_number(mlx.get("ChunkOverlap"), 30, 0, 300)),
        },
        "whisperx": {
            "model": whisperx.get("Model") or "large-v3-turbo",
            "device": whisperx.get("Device") or "auto",
            "compute_type": whisperx.get("ComputeType") or "auto",
            "batch_size": int(_number(whisperx.get("BatchSize"), 8, 1, 64)),
            "initial_prompt": str(whisperx.get("InitialPrompt") or ""),
            "hotwords": str(whisperx.get("Hotwords") or ""),
            "vad_method": whisperx.get("VadMethod") or "silero",
            "vad_threshold": _number(whisperx.get("VadThreshold"), 0.5, 0, 1),
            "local_silero_dir": str(whisperx.get("LocalSileroDir") or ""),
        },
        "subtitle": {
            "glossary_path": str(Path(subtitle.get("TermGlossaryPath") or DEFAULT_GLOSSARY_PATH).expanduser()),
            "target_language": subtitle.get("TargetLanguage", "中文"),
            "split_type": subtitle.get("SplitType", "句子分段"),
            "max_word_count_cjk": int(_number(subtitle.get("MaxWordCountCJK"), 25, 1, 500)),
            "max_word_count_english": int(_number(subtitle.get("MaxWordCountEnglish"), 20, 1, 500)),
            "mask_original_profanity": bool(subtitle.get("NeedMaskOriginalProfanity", False)),
            "remove_translated_chinese_commas": bool(subtitle.get("NeedsRemoveTranslatedChineseCommas", False)),
            "remove_translated_periods": bool(subtitle.get("NeedsRemovePunctuation", True)),
            "custom_prompt_text": str(subtitle.get("CustomPromptText") or ""),
        },
    }


def alignment_snapshot(backend, device="cpu"):
    """Pin the alignment policy for new jobs; absent snapshots remain legacy."""
    from importlib.metadata import version, PackageNotFoundError
    try:
        package_version = version("whisperx")
    except PackageNotFoundError:
        package_version = None
    return {"method": "whisperx", "version": 1,
            "device": "cpu" if backend == "mlx" else device,
            "model_dir": str(app_data_dir("VideoCaptioner") / "models"),
            "model_selection": "language-default", "whisperx_version": package_version}


def check_alignment_snapshot(policy):
    """Do not silently resume a task with a different alignment implementation."""
    if not policy:
        return
    if policy.get("method") != "whisperx" or policy.get("version", 1) != 1:
        raise ValueError("Unsupported saved alignment policy; explicit migration is required")
    expected = policy.get("whisperx_version")
    if expected:
        from importlib.metadata import version
        if version("whisperx") != expected:
            raise ValueError("WhisperX version differs from the saved alignment policy; restore that version or explicitly migrate the task")
