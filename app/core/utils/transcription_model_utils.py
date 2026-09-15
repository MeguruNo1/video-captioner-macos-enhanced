from __future__ import annotations

from pathlib import Path

from app.core.entities import TranscribeConfig, TranscribeModelEnum
from app.core.utils.mlx_model_utils import (
    REQUIRED_MLX_MODEL_FILES,
    is_local_model_reference,
    is_valid_local_mlx_model,
    validate_mlx_model,
)


WHISPERX_LOCAL_MODEL_DIRS = {
    "tiny": "faster-whisper-tiny",
    "base": "faster-whisper-base",
    "small": "faster-whisper-small",
    "medium": "faster-whisper-medium",
    "large-v1": "faster-whisper-large-v1",
    "large-v2": "faster-whisper-large-v2",
    "large-v3": "faster-whisper-large-v3",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
}


def _has_required_files(path: Path, required_files: tuple[str, ...]) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in required_files)


def _cached_huggingface_snapshot(
    repo_id: str, required_files: tuple[str, ...]
) -> Path | None:
    try:
        from huggingface_hub import snapshot_download

        snapshot = Path(snapshot_download(repo_id, local_files_only=True))
    except Exception:
        return None
    return snapshot if _has_required_files(snapshot, required_files) else None


def resolve_available_mlx_model(model: str | None) -> Path | None:
    value = str(model or "").strip()
    if not value:
        return None
    if is_local_model_reference(value):
        path = Path(value).expanduser()
        return path if is_valid_local_mlx_model(path) else None
    snapshot = _cached_huggingface_snapshot(value, REQUIRED_MLX_MODEL_FILES)
    return snapshot if snapshot and is_valid_local_mlx_model(snapshot) else None


def resolve_available_whisperx_model(config: TranscribeConfig) -> Path | None:
    model = str(config.whisperx_model or "").strip()
    if not model:
        return None

    configured_path = Path(model).expanduser()
    if configured_path.is_absolute() or model.startswith(("~", ".")):
        return configured_path if (configured_path / "model.bin").is_file() else None

    model_root = Path(config.whisperx_model_dir or "").expanduser()
    local_name = WHISPERX_LOCAL_MODEL_DIRS.get(model, f"faster-whisper-{model}")
    candidates = [model_root / local_name, model_root / model]
    for candidate in candidates:
        if (candidate / "model.bin").is_file():
            return candidate

    hub_root = model_root / f"models--Systran--faster-whisper-{model}" / "snapshots"
    if hub_root.is_dir():
        for snapshot in hub_root.iterdir():
            if (snapshot / "model.bin").is_file():
                return snapshot
    return None


def validate_transcription_model_ready(
    config: TranscribeConfig | None,
) -> tuple[bool, str]:
    if config is None or config.transcribe_model is None:
        return False, "未选择转录引擎，请先选择并配置转录模型"

    if config.transcribe_model == TranscribeModelEnum.MLX_WHISPER:
        is_valid, message = validate_mlx_model(config.mlx_model)
        if not is_valid:
            return False, message
        if resolve_available_mlx_model(config.mlx_model) is None:
            return (
                False,
                "MLX Whisper 模型尚未下载完成。请先在模型设置中选择包含 "
                "config.json 和权重文件（weights.safetensors 或 weights.npz）的本地模型目录",
            )
        return True, "MLX Whisper 模型可用"

    if config.transcribe_model == TranscribeModelEnum.WHISPER_X:
        if not str(config.whisperx_model or "").strip():
            return False, "未配置 WhisperX 模型"
        if resolve_available_whisperx_model(config) is None:
            return (
                False,
                "WhisperX 模型尚未安装。请先将所选模型下载到应用模型目录，"
                "再开始转录",
            )
        return True, "WhisperX 模型可用"

    return False, f"不支持的转录引擎：{config.transcribe_model}"
