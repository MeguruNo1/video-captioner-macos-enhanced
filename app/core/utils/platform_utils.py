import os
import platform
import subprocess
import sys
from pathlib import Path

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
MLX_SUPPORTED = IS_MACOS and platform.machine().lower() in {"arm64", "aarch64"}
COOKIE_BROWSER_OPTIONS = ["Safari", "Chrome", "Edge"] if IS_MACOS else ["Edge", "Chrome"]
DEFAULT_COOKIE_BROWSER_LABEL = COOKIE_BROWSER_OPTIONS[0]


def app_data_dir(app_name: str) -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / app_name
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / app_name
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / app_name


def default_work_dir(app_name: str) -> Path:
    return Path.home() / ("Movies" if IS_MACOS else "Videos") / app_name


def bundled_bin_dir() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos-arm64" if MLX_SUPPORTED else "macos-x86_64"
    return "linux"


def open_path(path: str | os.PathLike) -> bool:
    target = str(Path(path).expanduser().resolve())
    try:
        if IS_WINDOWS:
            os.startfile(target)
            return True
        return subprocess.run(["open" if IS_MACOS else "xdg-open", target], check=False).returncode == 0
    except Exception:
        return False
