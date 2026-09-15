from __future__ import annotations

from pathlib import Path

from app.config import MODEL_PATH

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_LOCAL_MLX_MODEL_DIR = MODEL_PATH / "mlx-whisper-large-v3-turbo"
REQUIRED_MLX_MODEL_FILES = ("config.json",)
MLX_WEIGHT_FILES = ("weights.safetensors", "weights.npz")


def discover_local_mlx_models() -> list[str]:
    models = []
    if not MODEL_PATH.exists():
        return models

    for path in sorted(MODEL_PATH.iterdir()):
        if path.is_dir() and is_valid_local_mlx_model(path):
            models.append(str(path))
    return models


def is_local_model_reference(model: str | Path | None) -> bool:
    value = str(model or "").strip()
    if not value:
        return False
    return value.startswith(("/", "~", "."))


def is_valid_local_mlx_model(path: str | Path) -> bool:
    model_path = Path(path).expanduser()
    return model_path.is_dir() and all(
        (model_path / file_name).is_file()
        for file_name in REQUIRED_MLX_MODEL_FILES
    ) and any((model_path / name).is_file() for name in MLX_WEIGHT_FILES)


def preferred_mlx_model(current_model: str | None = None) -> str:
    current = str(current_model or "").strip()
    if current and current != DEFAULT_MLX_MODEL:
        return current
    if is_valid_local_mlx_model(DEFAULT_LOCAL_MLX_MODEL_DIR):
        return str(DEFAULT_LOCAL_MLX_MODEL_DIR)
    return current or DEFAULT_MLX_MODEL


def validate_mlx_model(model: str | None) -> tuple[bool, str]:
    value = str(model or "").strip()
    if not value:
        return False, "未配置 MLX Whisper 模型"

    if is_local_model_reference(value):
        model_path = Path(value).expanduser()
        if not model_path.exists():
            return False, f"MLX Whisper 本地模型目录不存在：{model_path}"
        if not model_path.is_dir():
            return False, f"MLX Whisper 本地模型不是目录：{model_path}"

        missing_files = [
            file_name
            for file_name in REQUIRED_MLX_MODEL_FILES
            if not (model_path / file_name).is_file()
        ]
        if not any((model_path / name).is_file() for name in MLX_WEIGHT_FILES):
            missing_files.append("weights.safetensors 或 weights.npz")
        if missing_files:
            return (
                False,
                "MLX Whisper 本地模型目录缺少文件："
                + ", ".join(missing_files),
            )
        return True, f"已接入本地模型：{model_path}"

    return True, "将使用 HuggingFace 模型名称；首次使用会读取缓存或下载模型"
