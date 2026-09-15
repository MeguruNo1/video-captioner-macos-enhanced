import logging
import os
import sys
from pathlib import Path

from app.core.utils.platform_utils import app_data_dir, default_work_dir, bundled_bin_dir

YEAR = 2025
APP_NAME = "VideoCaptioner"
AUTHOR = "Weifeng"

WHISPERX_ONLY_MODE = True

HELP_URL = "https://github.com/WEIFENG2333/VideoCaptioner"
GITHUB_REPO_URL = "https://github.com/WEIFENG2333/VideoCaptioner"
FEEDBACK_URL = "https://github.com/WEIFENG2333/VideoCaptioner/issues"

# 路径
ROOT_PATH = Path(__file__).parent

RESOURCE_PATH = ROOT_PATH.parent / "resource"
APP_DATA_PATH = app_data_dir(APP_NAME)
WORK_PATH = default_work_dir(APP_NAME)


BIN_PATH = RESOURCE_PATH / "bin" / bundled_bin_dir()
ASSETS_PATH = RESOURCE_PATH / "assets"

LOG_PATH = APP_DATA_PATH / "logs"
SETTINGS_PATH = APP_DATA_PATH / "settings.json"
CACHE_PATH = APP_DATA_PATH / "cache"
MODEL_PATH = APP_DATA_PATH / "models"

# 日志配置
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# 环境变量添加平台对应 bin 路径，添加到 PATH 开头以优先使用
for path in (BIN_PATH,):
    if path.exists():
        os.environ["PATH"] = str(path) + os.pathsep + os.environ["PATH"]

# 添加 VLC 路径
vlc_path = BIN_PATH / "vlc"
if vlc_path.exists():
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc_path)

# 创建路径
for p in [CACHE_PATH, LOG_PATH, WORK_PATH, MODEL_PATH]:
    p.mkdir(parents=True, exist_ok=True)
