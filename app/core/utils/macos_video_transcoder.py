import sys
import threading
from pathlib import Path
from typing import Callable

from ..utils.logger import setup_logger

logger = setup_logger("macos_video_transcoder")

NATIVE_HEVC_ENCODER_NAME = "macos_avfoundation_hevc"
NATIVE_HEVC_PRESET_FAST_1080P = "fast_1080p"
NATIVE_HEVC_PRESET_BALANCED_4K = "balanced_4k"
NATIVE_HEVC_PRESET_HIGHEST_QUALITY = "highest_quality"
NATIVE_HEVC_PRESET_ATTRIBUTES = {
    NATIVE_HEVC_PRESET_FAST_1080P: "AVAssetExportPresetHEVC1920x1080",
    NATIVE_HEVC_PRESET_BALANCED_4K: "AVAssetExportPresetHEVC3840x2160",
    NATIVE_HEVC_PRESET_HIGHEST_QUALITY: "AVAssetExportPresetHEVCHighestQuality",
}


def _load_frameworks():
    try:
        import AVFoundation
        import CoreMedia
        import Foundation
    except Exception as exc:
        raise RuntimeError(f"macOS 原生视频框架不可用: {exc}") from exc
    return AVFoundation, CoreMedia, Foundation


def _fourcc_to_string(value: int) -> str:
    try:
        return int(value).to_bytes(4, "big").decode("ascii", errors="ignore").lower()
    except Exception:
        return ""


def is_native_hevc_transcode_supported() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        av, _cm, _foundation = _load_frameworks()
        return bool(getattr(av, "AVAssetExportPresetHEVCHighestQuality", None))
    except Exception as exc:
        logger.warning("macOS 原生 HEVC 转码不可用: %s", exc)
        return False


def _asset_for_path(path: str):
    av, _cm, foundation = _load_frameworks()
    url = foundation.NSURL.fileURLWithPath_(str(Path(path).expanduser()))
    return av.AVURLAsset.URLAssetWithURL_options_(url, None)


def get_native_video_codec(path: str) -> str:
    if sys.platform != "darwin":
        return ""

    av, cm, _foundation = _load_frameworks()
    asset = _asset_for_path(path)
    tracks = asset.tracksWithMediaType_(av.AVMediaTypeVideo)
    if not tracks:
        return ""

    descriptions = tracks[0].formatDescriptions()
    if not descriptions:
        return ""

    subtype = cm.CMFormatDescriptionGetMediaSubType(descriptions[0])
    codec = _fourcc_to_string(subtype)
    if codec in {"av01"}:
        return "av1"
    if codec in {"vp09"}:
        return "vp9"
    if codec in {"hvc1", "hev1"}:
        return "hevc"
    if codec in {"avc1"}:
        return "h264"
    return codec


def _session_error_text(session) -> str:
    error = session.error()
    details = []
    # NSError's localized description alone often hides the actual decoder or
    # container failure. Limit traversal in case an error chain is cyclic.
    for _ in range(5):
        if not error:
            break
        reason = error.localizedFailureReason()
        details.append(f"{error.domain()} ({error.code()}): {error.localizedDescription()}"
                       + (f"; {reason}" if reason else ""))
        error = (error.userInfo() or {}).get("NSUnderlyingError")
    return " <- ".join(details)


def _native_hevc_export_preset(av, preset_name: str):
    attribute = NATIVE_HEVC_PRESET_ATTRIBUTES.get(
        preset_name,
        NATIVE_HEVC_PRESET_ATTRIBUTES[NATIVE_HEVC_PRESET_HIGHEST_QUALITY],
    )
    preset = getattr(av, attribute, None)
    if preset:
        return preset

    logger.warning("macOS 不支持所选 HEVC 预设 %s，回退到最高质量", preset_name)
    fallback = getattr(av, "AVAssetExportPresetHEVCHighestQuality", None)
    if not fallback:
        raise RuntimeError("macOS 原生 HEVC 最高质量预设不可用")
    return fallback


def transcode_video_to_hevc_native(
    input_file: str,
    output_file: str,
    progress_callback: Callable[[int, str], None] | None = None,
    preset_name: str = NATIVE_HEVC_PRESET_HIGHEST_QUALITY,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    input_path = Path(input_file)
    output_path = Path(output_file)
    if not input_path.is_file():
        raise FileNotFoundError(f"输入视频不存在: {input_file}")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("原生 HEVC 输出不能覆盖输入文件")
    if not is_native_hevc_transcode_supported():
        raise RuntimeError("当前环境不支持 macOS 原生 HEVC 转码")

    av, _cm, foundation = _load_frameworks()
    asset = _asset_for_path(str(input_path))
    video_tracks = asset.tracksWithMediaType_(av.AVMediaTypeVideo)
    if not asset.isReadable() or not asset.isExportable() or not video_tracks:
        raise RuntimeError(
            f"AVFoundation 无法读取或导出输入媒体（容器={input_path.suffix.lower()}，"
            f"readable={bool(asset.isReadable())}，exportable={bool(asset.isExportable())}，"
            f"video_tracks={len(video_tracks or [])}）；请使用 FFmpeg 转码"
        )
    if cancel_check:
        cancel_check()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    preset = _native_hevc_export_preset(av, preset_name)
    session = av.AVAssetExportSession.alloc().initWithAsset_presetName_(asset, preset)
    if session is None:
        raise RuntimeError("创建 macOS 原生 HEVC 导出会话失败")

    supported_file_types = list(session.supportedFileTypes() or [])
    if av.AVFileTypeMPEG4 not in supported_file_types:
        raise RuntimeError(
            "macOS 原生 HEVC 导出不支持 MP4 输出，支持类型: "
            + ", ".join(map(str, supported_file_types))
        )

    output_url = foundation.NSURL.fileURLWithPath_(str(output_path))
    session.setOutputURL_(output_url)
    session.setOutputFileType_(av.AVFileTypeMPEG4)

    completed = threading.Event()
    session.exportAsynchronouslyWithCompletionHandler_(lambda: completed.set())

    last_progress = -1
    try:
        while not completed.wait(0.25):
            if cancel_check:
                cancel_check()
            progress = max(0, min(99, int(float(session.progress()) * 100)))
            if progress_callback and progress != last_progress:
                progress_callback(progress, "正在使用 macOS 原生 API 转码为 H.265")
                last_progress = progress
        if cancel_check:
            cancel_check()
    except Exception:
        session.cancelExport()
        raise

    status = session.status()
    if status != av.AVAssetExportSessionStatusCompleted or not output_path.is_file():
        error_text = _session_error_text(session)
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("清理失败的 macOS 原生 HEVC 输出文件失败: %s", output_path)
        raise RuntimeError(
            f"macOS 原生 HEVC 转码失败，状态={status}"
            + (f"，错误={error_text}" if error_text else "")
        )

    if progress_callback:
        progress_callback(100, "H.265 转码完成")
    logger.info(
        "macOS 原生 HEVC 转码完成: %s，预设=%s", output_path, preset_name
    )
    return NATIVE_HEVC_ENCODER_NAME
