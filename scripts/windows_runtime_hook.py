"""Expose bundled FFmpeg shared libraries to Windows audio decoders."""
import os
import sys
from pathlib import Path

_dll_handles = []
if sys.platform == "win32":
    _bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    _ffmpeg_dir = _bundle_root / "resource" / "bin" / "windows"
    if _ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(_ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
        _dll_handles.append(os.add_dll_directory(str(_ffmpeg_dir)))
