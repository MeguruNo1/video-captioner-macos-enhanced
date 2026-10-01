import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, overload

import psutil
import requests
import yt_dlp
from yt_dlp.utils import DownloadError

if TYPE_CHECKING:
    from yt_dlp import _Params

from app.config import APP_DATA_PATH
from app.core.utils.edge_cookie_utils import export_browser_cookies, harden_cookie_file
from app.core.utils.logger import setup_logger
from app.core.utils.download_description import write_description_txt_file
from app.core.utils.subtitle_transcript import write_transcript_txt_file
from app.core.utils.proxy_utils import (
    apply_download_proxy_environment,
    get_effective_download_proxy_url,
)
from app.core.utils.macos_video_transcoder import (
    get_native_video_codec,
    is_native_hevc_transcode_supported,
    transcode_video_to_hevc_native,
)
from app.core.utils.video_utils import (
    get_video_codec,
    normalize_video_to_mp4,
    transcode_video_to_hevc,
)
from app.core.utils.youtube_pot_provider import add_bgutil_extractor_args

logger = setup_logger("video_download_thread")

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
    ".m4v",
}
AUDIO_EXTENSIONS = {
    ".m4a",
    ".mp3",
    ".aac",
    ".wav",
    ".flac",
    ".opus",
    ".ogg",
    ".weba",
    ".webm",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUBTITLE_EXTENSIONS = {
    ".srt",
    ".vtt",
    ".lrc",
    ".json3",
    ".srv1",
    ".srv2",
    ".srv3",
}
NO_COOKIE_FILE_PATH = APP_DATA_PATH / "__no_cookie__.txt"
YOUTUBE_HIGH_RESOLUTION_FALLBACK_CLIENT = "default,web_safari"
SUBTITLE_DOWNLOAD_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
SUBTITLE_DOWNLOAD_RETRY_DELAYS = (0.5, 1.5)


class DownloadCancelledError(Exception):
    """Raised when the user explicitly terminates an in-flight download."""


class FfmpegProgressMonitor:
    """Receive FFmpeg ``-progress`` frames over a local UDP socket."""

    def __init__(self, section_durations: list[float], callback):
        self.section_durations = [max(0.0, float(value)) for value in section_durations]
        self.callback = callback
        self.socket = None
        self.thread = None
        self.stop_event = threading.Event()
        self.started_at = time.monotonic()
        self.section_index = 0
        self.completed_duration = 0.0
        self.processing_started = False

    @property
    def progress_url(self) -> str | None:
        if self.socket is None:
            return None
        return f"udp://127.0.0.1:{self.socket.getsockname()[1]}"

    def start(self) -> bool:
        listening = True
        try:
            progress_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            progress_socket.bind(("127.0.0.1", 0))
            progress_socket.settimeout(0.25)
        except OSError as exc:
            logger.warning("FFmpeg 进度监听不可用，将只显示阶段状态: %s", exc)
            progress_socket = None
            listening = False

        self.socket = progress_socket
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ffmpeg-progress-monitor",
        )
        self.thread.start()
        return listening

    def stop(self):
        self.stop_event.set()
        progress_socket = self.socket
        self.socket = None
        if progress_socket is not None:
            try:
                progress_socket.close()
            except OSError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        self.thread = None

    def _run(self):
        last_heartbeat = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if not self.processing_started and now - last_heartbeat >= 1.0:
                self._emit_locating()
                last_heartbeat = now

            progress_socket = self.socket
            if progress_socket is None:
                self.stop_event.wait(0.25)
                continue
            try:
                payload, _address = progress_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            frame = {}
            for line in payload.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    frame[key.strip()] = value.strip()
            if frame:
                self._handle_frame(frame)

    def _section_duration(self) -> float:
        if not self.section_durations:
            return 0.0
        index = min(self.section_index, len(self.section_durations) - 1)
        return self.section_durations[index]

    def _emit_locating(self):
        section_count = max(1, len(self.section_durations))
        self.callback(
            {
                "phase": "locating",
                "indeterminate": True,
                "percent": "",
                "speed": "",
                "eta": "",
                "downloaded": "",
                "total": "",
                "elapsed": _format_duration(time.monotonic() - self.started_at),
                "filename": "",
                "section_index": min(self.section_index + 1, section_count),
                "section_count": section_count,
                "status": "正在定位片段起点",
            }
        )

    @staticmethod
    def _parse_speed_factor(value: str) -> float:
        try:
            return float(str(value or "").strip().removesuffix("x"))
        except ValueError:
            return 0.0

    def _handle_frame(self, frame: dict):
        try:
            out_time = max(0.0, float(frame.get("out_time_us") or 0) / 1_000_000)
        except (TypeError, ValueError):
            out_time = 0.0

        if out_time <= 0 and not self.processing_started:
            self._emit_locating()
            return

        self.processing_started = self.processing_started or out_time > 0
        duration = self._section_duration()
        total_duration = sum(self.section_durations)
        processed_duration = self.completed_duration + min(out_time, duration or out_time)
        percent = min(100.0, processed_duration * 100 / total_duration) if total_duration else 0.0
        speed_text = str(frame.get("speed") or "").strip()
        speed_factor = self._parse_speed_factor(speed_text)
        remaining_media = max(0.0, total_duration - processed_duration)
        eta = _format_duration(remaining_media / speed_factor) if speed_factor > 0 else ""
        total_size = _safe_size(frame.get("total_size"))
        section_count = max(1, len(self.section_durations))

        self.callback(
            {
                "phase": "processing",
                "indeterminate": False,
                "percent": f"{percent:.1f}",
                "speed": speed_text,
                "eta": eta,
                "downloaded": _format_bytes(total_size) if total_size else "",
                "total": "",
                "elapsed": _format_duration(time.monotonic() - self.started_at),
                "filename": "",
                "section_index": min(self.section_index + 1, section_count),
                "section_count": section_count,
                "status": "正在下载并生成片段",
            }
        )

        if frame.get("progress") == "end":
            self.completed_duration += duration
            self.section_index += 1
            self.processing_started = False
            if self.section_index < len(self.section_durations):
                self._emit_locating()


def sanitize_filename(name: str, replacement: str = "_") -> str:
    forbidden_chars = r'<>:"/\\|?*'
    sanitized = re.sub(f"[{re.escape(forbidden_chars)}]", replacement, name)
    sanitized = re.sub(r"[\0-\31]", "", sanitized)
    sanitized = sanitized.rstrip(" .")

    max_length = 255
    if len(sanitized) > max_length:
        base, ext = os.path.splitext(sanitized)
        base_max_length = max_length - len(ext)
        sanitized = base[:base_max_length] + ext

    return sanitized or "default_filename"


def _requests_kwargs(proxy_url: str) -> dict:
    if not proxy_url:
        return {}
    return {"proxies": {"http": proxy_url, "https": proxy_url}}


def _safe_size(value) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_bytes(size: int | None) -> str:
    if not size:
        return "未知大小"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.1f}{units[unit_index]}"


def _format_duration(seconds) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "未知时长"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_number(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "未知"


def _friendly_codec(codec: str | None) -> str:
    if not codec or codec == "none":
        return ""
    return str(codec).replace(".", " ").upper()


_QUALITY_TIER_BY_LONG_EDGE = (
    (3840, "2160p档"),
    (2560, "1440p档"),
    (1920, "1080p档"),
    (1280, "720p档"),
    (854, "480p档"),
    (640, "360p档"),
    (426, "240p档"),
    (256, "144p档"),
)


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_fps(fps) -> str:
    try:
        fps_value = float(fps)
    except (TypeError, ValueError):
        return ""
    if fps_value <= 0:
        return ""
    if fps_value.is_integer():
        return f"{int(fps_value)}FPS"
    return f"{fps_value:.2f}".rstrip("0").rstrip(".") + "FPS"


def _quality_tier_label(width: int, height: int) -> str:
    long_edge = max(_int_or_zero(width), _int_or_zero(height))
    for threshold, label in _QUALITY_TIER_BY_LONG_EDGE:
        if long_edge >= threshold:
            return label
    return ""


def _video_resolution_rank(item: dict) -> tuple[int, int]:
    width = _int_or_zero(item.get("width"))
    height = _int_or_zero(item.get("height"))
    return max(width, height), min(width, height)


def _format_resolution(item: dict) -> str:
    width = _int_or_zero(item.get("width"))
    height = _int_or_zero(item.get("height"))
    if width and height:
        label = f"{width}x{height}"
        tier = _quality_tier_label(width, height)
        return f"{label} · {tier}" if tier else label
    if height:
        return f"{height}p"
    if width:
        return f"{width}w"
    return item.get("resolution") or "未知"


def _extract_language(item: dict) -> str:
    language = item.get("language") or item.get("language_preference")
    if language in (None, "", -1):
        return ""
    return str(language).upper()


def _build_format_entry(item: dict) -> dict | None:
    vcodec = item.get("vcodec")
    acodec = item.get("acodec")
    has_video = bool(vcodec and vcodec != "none")
    has_audio = bool(acodec and acodec != "none")
    if not has_video and not has_audio:
        return None

    filesize = _safe_size(item.get("filesize")) or _safe_size(item.get("filesize_approx"))
    fps = item.get("fps")
    abr = item.get("abr") or item.get("tbr")
    ext = item.get("ext") or ""
    dynamic_range = item.get("dynamic_range") or ""

    detail_parts = []
    language = _extract_language(item)
    if language:
        detail_parts.append(language)
    if dynamic_range and str(dynamic_range).upper() not in {"SDR", "UNKNOWN"}:
        detail_parts.append(str(dynamic_range).upper())
    if has_video and _friendly_codec(vcodec):
        detail_parts.append(_friendly_codec(vcodec))
    if has_audio and not has_video and _friendly_codec(acodec):
        detail_parts.append(_friendly_codec(acodec))
    if has_video and has_audio:
        detail_parts.append("含音频")
    if ext:
        detail_parts.append(ext)
    detail_parts.append(_format_bytes(filesize))

    if has_video:
        quality_parts = [_format_resolution(item)]
        fps_text = _format_fps(fps)
        if fps_text:
            quality_parts.append(fps_text)
        quality = " · ".join(part for part in quality_parts if part)
    else:
        quality = f"{int(abr)}kbps" if abr else "音频"

    return {
        "format_id": str(item.get("format_id", "")),
        "ext": ext,
        "quality": quality,
        "details": " · ".join(part for part in detail_parts if part),
        "filesize": filesize,
        "fps": fps or 0,
        "height": item.get("height") or 0,
        "width": item.get("width") or 0,
        "abr": abr or 0,
        "language": language,
        "vcodec": vcodec or "",
        "acodec": acodec or "",
        "dynamic_range": dynamic_range or "",
        "has_video": has_video,
        "has_audio": has_audio,
        "channels": item.get("audio_channels") or item.get("channels") or 0,
        "protocol": item.get("protocol") or "",
    }


def normalize_preview_data(url: str, info_dict: dict, thumbnail_bytes: bytes | None = None) -> dict:
    video_formats = []
    audio_formats = []
    for item in info_dict.get("formats") or []:
        entry = _build_format_entry(item)
        if not entry:
            continue
        if entry["has_video"]:
            video_formats.append(entry)
        elif entry["has_audio"]:
            audio_formats.append(entry)

    video_formats.sort(
        key=lambda item: (
            *_video_resolution_rank(item),
            item.get("fps", 0),
            item.get("filesize", 0) or 0,
        ),
        reverse=True,
    )
    if video_formats:
        top_video = video_formats[0]
        logger.info(
            "解析到 %s 个视频格式，最高候选: %s / %s / %s",
            len(video_formats),
            top_video.get("format_id") or "unknown",
            top_video.get("quality") or "unknown",
            top_video.get("vcodec") or "unknown",
        )
    audio_formats.sort(
        key=lambda item: (item.get("abr", 0), item.get("filesize", 0) or 0, item.get("channels", 0)),
        reverse=True,
    )

    subtitles = info_dict.get("subtitles") or {}
    automatic_captions = info_dict.get("automatic_captions") or {}
    return {
        "url": url,
        "title": info_dict.get("title") or "未命名视频",
        "uploader": info_dict.get("uploader") or info_dict.get("channel") or "未知作者",
        "duration_text": _format_duration(info_dict.get("duration")),
        "upload_date": info_dict.get("upload_date") or "",
        "view_count_text": _format_number(info_dict.get("view_count")),
        "thumbnail_url": info_dict.get("thumbnail") or "",
        "thumbnail_bytes": thumbnail_bytes,
        "video_formats": video_formats,
        "audio_formats": audio_formats,
        "manual_subtitle_languages": sorted(subtitles.keys()),
        "auto_subtitle_languages": sorted(automatic_captions.keys()),
        "has_manual_subtitles": bool(subtitles),
        "has_auto_subtitles": bool(automatic_captions),
        "info_dict": info_dict,
    }


def _subtitle_language(value: object) -> str:
    return str(value or "").strip().lower().replace("_", "-").removesuffix("-orig")


def _subtitle_candidates(info_dict: dict, mode: str, language: str | None) -> list[dict]:
    """Select source-language tracks; never use auto-translated caption URLs."""
    automatic = info_dict.get("automatic_captions") or {}
    requested = _subtitle_language(language)
    if requested in {"", "auto", "und"}:
        requested = _subtitle_language(info_dict.get("language"))
        if requested in {"", "auto", "und"}:
            # Multi-dub videos expose many xx-orig caption tracks: use the
            # original audio marker, never bitrate or dictionary ordering.
            original_audio = set()
            for item in info_dict.get("formats") or []:
                note = str(item.get("format_note") or "").lower()
                if item.get("language_preference") == 10 or ("original" in note and "dubbed" not in note):
                    value = _subtitle_language(item.get("language"))
                    if value and value not in {"auto", "und"}:
                        original_audio.add(value)
            requested = next(iter(original_audio)) if len(original_audio) == 1 else ""
        if not requested:
            # YouTube labels the original ASR track xx-orig even when translated
            # tracks for hundreds of languages precede it in the dictionary.
            originals = {_subtitle_language(key) for key in automatic if key.endswith("-orig")}
            requested = next(iter(originals)) if len(originals) == 1 else ""
        if not requested:
            native_languages = set()
            for key, entries in automatic.items():
                for item in entries:
                    query = parse_qs(urlparse(item.get("url", "")).query)
                    if query.get("lang") and not query.get("tlang"):
                        native_languages.add(_subtitle_language(query["lang"][0]))
            requested = next(iter(native_languages)) if len(native_languages) == 1 else ""
    if not requested:
        manual_languages = {
            _subtitle_language(key) for key, entries in (info_dict.get("subtitles") or {}).items()
            if any(item.get("url") and not parse_qs(urlparse(item["url"]).query).get("tlang") for item in entries)
        }
        requested = next(iter(manual_languages)) if len(manual_languages) == 1 else ""
    if not requested:
        return []  # Ambiguous language is safer than a reference in another language.
    candidates = []
    seen_urls = set()
    seen_tracks = set()
    for kind in (["manual", "auto"] if mode == "prefer_manual" else [mode]):
        source = info_dict.get("subtitles" if kind == "manual" else "automatic_captions") or {}
        keys = sorted(source, key=lambda key: (
            _subtitle_language(key) != requested, not key.endswith("-orig"), key
        ))
        for key in keys:
            normalized = _subtitle_language(key)
            if (kind, normalized) in seen_tracks:
                continue
            if normalized != requested and normalized.split("-")[0] != requested.split("-")[0]:
                continue
            entries = sorted(source[key] or [], key=lambda item: (
                {"vtt": 0, "srt": 1, "ttml": 2}.get(item.get("ext"), 9)
            ))
            for item in entries:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                # HLS entries are playlists, not standalone subtitle text.
                if "m3u8" in str(item.get("protocol") or "") or urlparse(url).path.endswith(".m3u8"):
                    continue
                query = parse_qs(urlparse(url).query)
                if query.get("tlang"):
                    continue
                if kind == "auto" and query.get("lang"):
                    original = _subtitle_language(query["lang"][0])
                    if original.split("-")[0] != requested.split("-")[0]:
                        continue
                seen_urls.add(url)
                seen_tracks.add((kind, normalized))
                candidates.append({"url": url, "ext": item.get("ext") or "vtt",
                                   "language": normalized, "track": key, "kind": kind})
                break  # A different encoding of the same track is not a useful 429 retry.
    return candidates


def _pick_subtitle_item(
    info_dict: dict, subtitle_mode: str, subtitle_language: str | None
) -> tuple[str | None, str]:
    candidates = _subtitle_candidates(info_dict, subtitle_mode, subtitle_language)
    return (candidates[0]["url"], candidates[0]["ext"]) if candidates else (None, "vtt")


def _download_subtitle_fallback(
    subtitle_download_link: str | None,
    subtitle_ext: str,
    subtitle_path: Path,
    proxy_url: str,
) -> str | None:
    if not subtitle_download_link:
        return None

    attempts = len(SUBTITLE_DOWNLOAD_RETRY_DELAYS) + 1
    last_error: Exception | None = None
    response = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                subtitle_download_link, timeout=30, **_requests_kwargs(proxy_url)
            )
            if response.status_code not in SUBTITLE_DOWNLOAD_RETRY_STATUS:
                response.raise_for_status()
                break
            response.raise_for_status()
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise
            delay = SUBTITLE_DOWNLOAD_RETRY_DELAYS[attempt]
            logger.warning(
                "字幕直链下载失败，%.1f 秒后重试（%s/%s）: %s",
                delay,
                attempt + 1,
                attempts,
                exc,
            )
            time.sleep(delay)
    if response is None:
        raise RuntimeError(f"字幕直链下载失败: {last_error}")

    subtitle_path = subtitle_path.with_suffix(f".{subtitle_ext or 'vtt'}")
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = subtitle_path.with_name(f".{subtitle_path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(response.text)
    os.replace(temp_path, subtitle_path)
    return str(subtitle_path)


def _find_reusable_subtitle_path(
    work_dir: Path, subtitle_language: str | None = None
) -> str | None:
    prefixes = []
    language = str(subtitle_language or "").strip().lower()
    if language:
        prefixes.append(f"【下载字幕】_{language}")
    prefixes.append("【下载字幕】")

    for prefix in prefixes:
        try:
            candidates = sorted(
                work_dir.rglob(f"{prefix}.*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for candidate in candidates:
            try:
                if (
                    candidate.is_file()
                    and candidate.suffix.lower() in SUBTITLE_EXTENSIONS
                    and candidate.stat().st_size > 0
                ):
                    return str(candidate)
            except OSError:
                continue
    return None


def _find_reusable_transcript_path(work_dir: Path, title: str) -> str | None:
    transcript_path = work_dir / f"【视频文稿】{title}.txt"
    try:
        if transcript_path.is_file() and transcript_path.stat().st_size > 0:
            return str(transcript_path)
    except OSError:
        pass
    return None


def _is_format_selection_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if not message:
        return False
    markers = (
        "requested format is not available",
        "requested format not available",
        "requested format unavailable",
        "format is not available",
        "format not available",
        "format unavailable",
        "requested formats are incompatible",
    )
    if any(marker in message for marker in markers):
        return True
    return bool(re.search(r"\bformat(s)?\b.*\b(not available|unavailable)\b", message))


def _is_stale_auth_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "page needs to be reloaded",
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "login_required",
        "cookies are no longer valid",
        "cookies have expired",
    ))


def _is_expired_media_url_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "http error 403" in message or "forbidden" in message


def _refresh_configured_browser_cookies(cookiefile_path: Path) -> bool:
    if cookiefile_path.resolve() != (APP_DATA_PATH / "cookies.txt").resolve():
        return False
    try:
        settings = json.loads((APP_DATA_PATH / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    download = settings.get("Download") or {}
    if not download.get("AutoRefreshEdgeCookies", False):
        return False
    result = export_browser_cookies(
        cookiefile_path, browser=download.get("CookieBrowser", "Safari")
    )
    if not result.get("success"):
        logger.warning("认证失败后自动刷新 Cookie 未通过校验: %s", result.get("message"))
        return False
    logger.info("认证失败后已从配置的浏览器刷新 Cookie，正在重试一次")
    return True


def _clean_stale_partial_files(work_dir: Path) -> int:
    removed = 0
    for path in work_dir.rglob("*"):
        if not path.is_file() or not (path.name.endswith(".part") or path.name.endswith(".ytdl")):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _download_thumbnail_fallback(
    thumbnail_url: str | None, thumbnail_path: Path, proxy_url: str
) -> str | None:
    if not thumbnail_url:
        return None
    response = requests.get(thumbnail_url, timeout=30, **_requests_kwargs(proxy_url))
    response.raise_for_status()
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    with open(thumbnail_path, "wb") as file:
        file.write(response.content)
    return str(thumbnail_path)


def _normalize_thumbnail_to_png(thumbnail_path: str | Path | None) -> str | None:
    if not thumbnail_path:
        return None

    path = Path(thumbnail_path)
    if not path.exists():
        return None

    png_path = path.with_suffix(".png")
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.save(png_path, format="PNG")
        if path.resolve() != png_path.resolve() and path.exists():
            path.unlink()
        return str(png_path)
    except Exception as exc:
        logger.warning("封面转换 PNG 失败，保留原文件: %s", exc)
        return str(path)


def _resolve_download_engine_strategy(
    strategy: str | None, proxy_url: str, cookiefile_path: Path
) -> str | None:
    if strategy not in {"单线程", "多线程", "智能选择"}:
        return None
    if strategy != "智能选择":
        return strategy
    # Four fragment workers materially improve DASH/HLS throughput. yt-dlp's
    # own retry controls handle transient proxy/cookie failures; forcing every
    # authenticated task to one fragment made healthy downloads unnecessarily slow.
    return "多线程"


def _build_strategy_options(strategy: str | None) -> dict:
    if strategy == "单线程":
        return {
            "concurrent_fragment_downloads": 1,
            "retries": 10,
            "fragment_retries": 10,
        }
    if strategy == "多线程":
        return {
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
        }
    return {}


def _build_youtube_challenge_options() -> dict:
    node_path = shutil.which("node")
    if not node_path:
        for candidate in ("/usr/local/bin/node", "/opt/homebrew/bin/node"):
            if Path(candidate).is_file():
                node_path = candidate
                break
    if not node_path:
        return {}
    return {
        "js_runtimes": {
            "node": {
                "path": node_path,
            }
        },
        "remote_components": {"ejs:github"},
    }


def _resolve_cookiefile_path(configured_cookie_file: str | None) -> Path:
    default_path = APP_DATA_PATH / "cookies.txt"
    if configured_cookie_file is None:
        return default_path
    configured = Path(configured_cookie_file)
    if configured.is_file():
        return configured
    logger.warning(
        "任务指定的 Cookie 文件已不存在，回退到软件 Cookie 文件: %s",
        default_path,
    )
    return default_path


def _robust_format_selector(selector: str, download_mode: str = "video_audio") -> str:
    selector = str(selector or "").strip()
    if not selector:
        if download_mode == "audio":
            return "bestaudio/best"
        if download_mode == "video":
            return "bv*/bestvideo/best"
        return "bv*+ba/bestvideo+bestaudio/best"

    if "bv*+ba" in selector:
        return selector

    replacements = {
        "bestvideo+bestaudio/best": "bv*+ba/bestvideo+bestaudio/best",
        "bestvideo/best": "bv*/bestvideo/best",
    }
    if selector in replacements:
        return replacements[selector]

    if download_mode == "video_audio" and "bestvideo+bestaudio/best" in selector:
        return selector.replace(
            "bestvideo+bestaudio/best",
            "bv*+ba/bestvideo+bestaudio/best",
        )
    return selector


def _resolution_first_format_selector(info_dict: dict, download_mode: str) -> str:
    """Prefer HEVC/AVC only within the highest available resolution tier."""
    fallback = _robust_format_selector("", download_mode)
    if download_mode == "audio":
        return fallback
    heights = [float(item.get("height") or 0) for item in info_dict.get("formats", [])
               if item.get("vcodec") not in (None, "none")]
    height = max(heights, default=0)
    if height <= 0:
        return fallback
    tier = f"[height={height:g}]"
    selectors = []
    for codec in ("[vcodec~='^(hevc|h265|hvc1|hev1)']", "[vcodec~='^(avc|h264)']", ""):
        video = f"bv*{tier}{codec}"
        if download_mode == "video":
            selectors.append(video)
        else:
            selectors.extend((f"{video}+ba", f"b{tier}{codec}"))
    return "/".join([*selectors, fallback])


def _create_youtube_dl(options: dict[str, Any]) -> yt_dlp.YoutubeDL:
    # Options are assembled dynamically; the upstream TypedDict exists only in
    # type stubs, so keep this assertion at the third-party API boundary.
    return yt_dlp.YoutubeDL(cast("_Params", options))


def _build_ydl_options(proxy_url: str, cookiefile_path: Path, progress_hooks=None) -> dict:
    options = {
        "quiet": True,
        "noplaylist": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        # Keep yt-dlp's partial files after transient failures. Starting the same
        # download again will reuse them instead of downloading completed bytes.
        "continuedl": True,
        "nopart": False,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if progress_hooks:
        options["progress_hooks"] = progress_hooks
    if proxy_url:
        options["proxy"] = proxy_url
    if cookiefile_path.exists():
        try:
            harden_cookie_file(cookiefile_path)
        except Exception as exc:
            logger.warning("Cookie 文件安全处理失败，将跳过 cookiefile: %s", exc)
            cookiefile_path = NO_COOKIE_FILE_PATH
    if cookiefile_path.exists():
        logger.info("使用 cookiefile: %s", cookiefile_path)
        options["cookiefile"] = str(cookiefile_path)
    # Do not pin a simulated YouTube client. yt-dlp's default client ladder is
    # updated alongside extractor changes and is more robust than a static list.
    if add_bgutil_extractor_args(options):
        logger.info("已启用本地 YouTube PO Token provider")
    else:
        logger.warning(
            "未找到本地 YouTube PO Token provider；部分 YouTube 字幕可能不会显示。"
            "请运行 scripts/setup_youtube_pot_provider.sh"
        )
    options.update(_build_youtube_challenge_options())
    return options


def _has_downloadable_media_formats(info_dict: dict) -> bool:
    for item in info_dict.get("formats") or []:
        vcodec = item.get("vcodec")
        acodec = item.get("acodec")
        if (vcodec and vcodec != "none") or (acodec and acodec != "none"):
            return True
    return False


def _max_video_height(info_dict: dict) -> int:
    return max(
        (
            _int_or_zero(item.get("height"))
            for item in info_dict.get("formats") or []
            if item.get("vcodec") not in (None, "", "none")
        ),
        default=0,
    )


def _is_youtube_info(info_dict: dict) -> bool:
    extractor = str(
        info_dict.get("extractor_key") or info_dict.get("extractor") or ""
    ).lower()
    return extractor == "youtube"


def _set_youtube_player_client(options: dict, client: str) -> None:
    youtube_args = options.setdefault("extractor_args", {}).setdefault("youtube", {})
    youtube_args["player_client"] = [part for part in client.split(",") if part]


def _stable_selected_format_selector(
    info_dict: dict, format_id: str, stream_kind: str
) -> str:
    """Select a raw format ID even after yt-dlp suffixes duplicate IDs.

    YouTube's high-resolution web responses can contain one audio format per
    dubbed language,
    all sharing an itag such as ``140``. During ``process=True`` yt-dlp renames
    them to ``140-0``, ``140-1``, etc., so the raw preview ID is no longer an
    exact match at download time.
    """
    format_id = str(format_id or "").strip()
    if not format_id:
        return ""

    matching_formats = [
        item
        for item in info_dict.get("formats") or []
        if str(item.get("format_id") or "") == format_id
    ]
    if len(matching_formats) <= 1:
        return format_id

    selector = "ba" if stream_kind == "audio" else "bv"
    return f"{selector}[format_id^={format_id}]"


def _extract_metadata_info(
    url: str,
    proxy_url: str,
    cookiefile_path: Path,
    effective_strategy: str | None,
) -> dict:
    def extract_once(
        active_cookiefile_path: Path, youtube_player_client: str | None = None
    ) -> dict:
        options = _build_ydl_options(proxy_url, active_cookiefile_path)
        if youtube_player_client:
            _set_youtube_player_client(options, youtube_player_client)
        options.update(_build_strategy_options(effective_strategy))
        options.update(
            {
                "skip_download": True,
                "extract_flat": False,
                "lazy_playlist": False,
            }
        )

        with _create_youtube_dl(options) as ydl:
            # yt-dlp returns a mutable dict; our metadata also carries app keys.
            return cast(dict[str, Any], ydl.extract_info(url, download=False, process=False))

    try:
        info_dict = extract_once(cookiefile_path)
    except DownloadError as exc:
        if not cookiefile_path.exists() or not _is_stale_auth_error(exc):
            raise
        if _refresh_configured_browser_cookies(cookiefile_path):
            try:
                info_dict = extract_once(cookiefile_path)
            except DownloadError as refreshed_exc:
                if not _is_stale_auth_error(refreshed_exc):
                    raise
                logger.warning("刷新 Cookie 后仍被认证拦截，改用无 Cookie 默认客户端重试")
                try:
                    info_dict = extract_once(NO_COOKIE_FILE_PATH)
                except DownloadError as anonymous_exc:
                    if _is_stale_auth_error(anonymous_exc):
                        raise RuntimeError(
                            "YouTube 同时拒绝了登录与匿名请求，当前代理出口很可能触发风控；"
                            "请更换代理节点后重试"
                        ) from anonymous_exc
                    raise
        else:
            logger.warning("现有 Cookie 被认证拦截，改用无 Cookie 默认客户端重试")
            try:
                info_dict = extract_once(NO_COOKIE_FILE_PATH)
            except DownloadError as anonymous_exc:
                if _is_stale_auth_error(anonymous_exc):
                    raise RuntimeError(
                        "YouTube 同时拒绝了登录与匿名请求，当前代理出口很可能触发风控；"
                        "请更换代理节点后重试"
                    ) from anonymous_exc
                raise

    if not isinstance(info_dict, dict) or not info_dict.get("formats"):
        raise RuntimeError("无法解析可用格式列表")

    if cookiefile_path.exists() and not _has_downloadable_media_formats(info_dict):
        logger.warning("使用 cookies 仅解析到图片格式，改为不带 cookies 重试")
        fallback_info_dict = extract_once(NO_COOKIE_FILE_PATH)
        if isinstance(fallback_info_dict, dict) and _has_downloadable_media_formats(fallback_info_dict):
            fallback_info_dict["_videocaptioner_disable_cookiefile"] = True
            return fallback_info_dict

    # Authenticated desktop Safari responses may expose only an HLS manifest up
    # to 1080p while YouTube keeps its 1440p/2160p streams behind SABR. The
    # creator player currently provides ordinary, PO-token-authorized URLs for
    # those video-only streams. Unlike mweb, it also remains valid across the
    # multiple range requests needed for large 4K downloads.
    initial_height = _max_video_height(info_dict)
    if (
        cookiefile_path.exists()
        and _is_youtube_info(info_dict)
        and 0 < initial_height <= 1080
    ):
        try:
            high_resolution_info = extract_once(
                cookiefile_path, YOUTUBE_HIGH_RESOLUTION_FALLBACK_CLIENT
            )
        except Exception as exc:
            logger.warning(
                "YouTube 高分辨率客户端重试失败，保留原解析结果: %s", exc
            )
        else:
            fallback_height = _max_video_height(high_resolution_info)
            if fallback_height > initial_height:
                for key in ("subtitles", "automatic_captions"):
                    if not high_resolution_info.get(key) and info_dict.get(key):
                        high_resolution_info[key] = info_dict[key]
                high_resolution_info[
                    "_videocaptioner_youtube_player_client"
                ] = YOUTUBE_HIGH_RESOLUTION_FALLBACK_CLIENT
                logger.info(
                    "YouTube 高分辨率客户端解析成功: %sp -> %sp",
                    initial_height,
                    fallback_height,
                )
                return high_resolution_info

    return info_dict


def _time_text_to_seconds(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return None
    try:
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            hours = 0
        else:
            hours, minutes, seconds = map(int, parts)
    except ValueError:
        return None
    if minutes < 0 or seconds < 0 or seconds >= 60 or (len(parts) == 3 and minutes >= 60):
        return None
    return hours * 3600 + minutes * 60 + seconds


def _parse_download_section(section: str) -> dict | None:
    value = str(section or "").strip()
    if not value.startswith("*") or "-" not in value:
        return None
    time_range = value[1:]
    start_text, end_text = time_range.split("-", 1)
    start_seconds = _time_text_to_seconds(start_text)
    end_seconds = _time_text_to_seconds(end_text)
    if start_seconds is None or end_seconds is None or start_seconds >= end_seconds:
        return None
    return {
        "start_time": start_seconds,
        "end_time": end_seconds,
    }


def _build_download_ranges_callback(download_sections: list[str]):
    parsed_sections = []
    for index, section in enumerate(download_sections or [], start=1):
        parsed = _parse_download_section(section)
        if not parsed:
            raise RuntimeError(f"无效的时间段配置: {section}")
        parsed["title"] = f"section_{index:02d}"
        parsed["index"] = index
        parsed_sections.append(parsed)

    def _callback(_info_dict, _ydl):
        return parsed_sections or [{}]

    return _callback


def _seconds_to_time_text(seconds: float | int) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _cut_video_segment(
    source_path: str, start_seconds: float, end_seconds: float, output_path: str
) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-ss",
        str(start_seconds),
        "-to",
        str(end_seconds),
        "-i",
        str(source_path),
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, encoding="utf-8", errors="replace")


def _probe_media_streams(media_path: str | Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,duration:stream_tags=DURATION",
            "-of",
            "json",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return json.loads(completed.stdout or "{}")


def _duration_text_to_seconds(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if ":" not in text:
            return float(text)
        hours, minutes, seconds = text.split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _media_has_valid_audio(media_path: str | Path) -> bool:
    try:
        probe = _probe_media_streams(media_path)
    except Exception as exc:
        logger.warning("无法验证下载结果音轨: %s", exc)
        return False

    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "audio":
            continue
        tag_duration = _duration_text_to_seconds((stream.get("tags") or {}).get("DURATION"))
        stream_duration = _duration_text_to_seconds(stream.get("duration"))
        if tag_duration is not None:
            return tag_duration > 0
        if stream_duration is not None:
            return stream_duration > 0
        # Some valid containers omit both duration fields. An audio stream is
        # still better evidence than rejecting a download we cannot disprove.
        return True
    return False


def _audio_repair_window(
    media_path: str | Path, download_sections: list[str]
) -> tuple[float, float] | None:
    if len(download_sections) != 1:
        return None
    section = _parse_download_section(download_sections[0])
    if not section:
        return None

    probe = _probe_media_streams(media_path)
    media_duration = _duration_text_to_seconds((probe.get("format") or {}).get("duration"))
    requested_duration = float(section["end_time"] - section["start_time"])
    if not media_duration or media_duration <= 0:
        media_duration = requested_duration

    # Stream-copy range downloads can begin at the preceding video keyframe.
    # Extend the audio backwards by the same small pre-roll so A/V stays aligned.
    extra_duration = max(0.0, media_duration - requested_duration)
    pre_roll = min(float(section["start_time"]), extra_duration, 30.0)
    return max(0.0, float(section["start_time"]) - pre_roll), media_duration


def _remux_recovery_audio(
    media_path: str | Path,
    audio_path: str | Path,
    download_sections: list[str],
) -> None:
    media = Path(media_path)
    repaired = media.with_name(f".{media.stem}.audio-repaired{media.suffix}")
    window = _audio_repair_window(media, download_sections)

    command = ["ffmpeg", "-nostdin", "-y", "-i", str(media)]
    if window:
        start_seconds, duration_seconds = window
        command.extend(
            [
                "-ss",
                f"{start_seconds:.3f}",
                "-t",
                f"{duration_seconds:.3f}",
            ]
        )
    command.extend(
        [
            "-i",
            str(audio_path),
            "-map",
            "0",
            "-map",
            "-0:a?",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            str(repaired),
        ]
    )

    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if not _media_has_valid_audio(repaired):
            raise RuntimeError("补下载的音频仍为空")
        os.replace(repaired, media)
    finally:
        if repaired.exists():
            repaired.unlink()


def extract_preview(url: str, download_engine_strategy: str | None = None) -> dict:
    proxy_url = apply_download_proxy_environment()
    cookiefile_path = APP_DATA_PATH / "cookies.txt"
    effective_strategy = _resolve_download_engine_strategy(
        download_engine_strategy, proxy_url, cookiefile_path
    )
    info_dict = _extract_metadata_info(
        url, proxy_url, cookiefile_path, effective_strategy
    )

    thumbnail_bytes = None
    thumbnail_url = info_dict.get("thumbnail")
    if thumbnail_url:
        try:
            response = requests.get(
                thumbnail_url, timeout=20, **_requests_kwargs(proxy_url)
            )
            response.raise_for_status()
            thumbnail_bytes = response.content
        except Exception:
            logger.warning("封面预览下载失败: %s", thumbnail_url, exc_info=True)

    return normalize_preview_data(url, info_dict, thumbnail_bytes)


class SignalEmitter(Protocol):
    """Instance interface shared by callbacks and bound Qt signals."""

    def emit(self, *args: Any) -> None: ...


class SignalDescriptor(Protocol):
    """A Qt signal binds to an emitter without importing Qt into this service."""

    @overload
    def __get__(self, instance: None, owner: Any) -> Self: ...

    @overload
    def __get__(self, instance: Any, owner: Any) -> SignalEmitter: ...


class CallbackSignal:
    """Small callback adapter shared by the headless service and Qt wrapper."""
    def __init__(self, callback=None):
        self.callback = callback

    def emit(self, *args):
        if self.callback:
            self.callback(*args)


class VideoDownloadService:
    finished: SignalEmitter | SignalDescriptor
    detailed_finished: SignalEmitter | SignalDescriptor
    progress: SignalEmitter | SignalDescriptor
    progress_detail: SignalEmitter | SignalDescriptor
    error: SignalEmitter | SignalDescriptor
    cancelled: SignalEmitter | SignalDescriptor

    @staticmethod
    def _message_text(text: str) -> str:
        return text

    def __init__(
        self,
        url: str,
        work_dir: str,
        need_video: bool = True,
        need_subtitle: bool = True,
        need_thumbnail: bool = False,
        subtitle_mode: str = "auto",
        subtitle_language: str = "en",
        download_engine_strategy: str | None = None,
        download_mode: str = "video_audio",
        selected_video_format_id: str = "",
        selected_audio_format_id: str = "",
        format_selector: str = "",
        need_metadata: bool = False,
        need_description_txt: bool = False,
        need_transcript_txt: bool = False,
        enable_time_ranges: bool = False,
        download_sections: list[str] | None = None,
        pr_smart_transcode_hevc_on_av1: bool = False,
        ensure_mp4_output: bool = False,
        description_txt_template: str | None = None,
        resume_existing: bool = False,
        proxy_url: str | None = None,
        cookie_file: str | None = None,
        progress_callback=None,
        cancel_event=None,
        native_hevc_preset: str = "highest_quality",
        hevc_encoder: str = "auto",
        prefer_compatible_codecs: bool = True,
    ):
        self.proxy_url = proxy_url
        self.cookie_file = cookie_file
        self.native_hevc_preset = native_hevc_preset
        self.hevc_encoder = hevc_encoder
        self.prefer_compatible_codecs = prefer_compatible_codecs
        # Qt's signals are provided by the wrapper; plain services use callbacks.
        for name in ("finished", "detailed_finished", "progress", "progress_detail", "error", "cancelled"):
            if not hasattr(self, name):
                setattr(self, name, CallbackSignal(progress_callback if name == "progress" else None))
        self.url = url
        self.work_dir = work_dir
        self.need_video = need_video
        self.need_subtitle = need_subtitle
        self.need_thumbnail = need_thumbnail
        self.subtitle_mode = subtitle_mode if subtitle_mode in {"manual", "auto", "prefer_manual"} else "auto"
        self.subtitle_language = str(subtitle_language or "en").strip().lower()
        self.download_engine_strategy = download_engine_strategy
        self.download_mode = download_mode
        self.selected_video_format_id = selected_video_format_id
        self.selected_audio_format_id = selected_audio_format_id
        self.format_selector = format_selector
        self.need_metadata = need_metadata
        self.need_description_txt = need_description_txt
        self.description_txt_template = description_txt_template
        self.need_transcript_txt = need_transcript_txt
        self.enable_time_ranges = enable_time_ranges
        self.download_sections = list(download_sections or [])
        self.pr_smart_transcode_hevc_on_av1 = pr_smart_transcode_hevc_on_av1
        self.ensure_mp4_output = ensure_mp4_output
        self.resume_existing = resume_existing
        self._pause_event = threading.Event()
        self._terminate_event = cancel_event if cancel_event is not None else threading.Event()
        self._pause_notice_emitted = False
        self._current_progress = 0
        self._download_root = Path(work_dir)
        self._download_dir = None
        self._download_dir_existed = False
        self._download_started_at = None
        self._download_processes_done = threading.Event()
        self._download_processes_done.set()
        self._cleanup_lock = threading.Lock()
        self._cleanup_completed = False
        self._cleanup_message = ""

    def run(self):
        try:
            result = self.download(
                need_video=self.need_video,
                need_subtitle=self.need_subtitle,
                need_thumbnail=self.need_thumbnail,
                subtitle_mode=self.subtitle_mode,
                subtitle_language=self.subtitle_language,
                need_metadata=self.need_metadata,
                need_description_txt=self.need_description_txt,
                need_transcript_txt=self.need_transcript_txt,
                enable_time_ranges=self.enable_time_ranges,
                download_sections=self.download_sections,
                pr_smart_transcode_hevc_on_av1=self.pr_smart_transcode_hevc_on_av1,
                ensure_mp4_output=self.ensure_mp4_output,
                resume_existing=self.resume_existing,
            )
            self.detailed_finished.emit(result)
            self.finished.emit(result.get("video_path") or "")
        except DownloadCancelledError as exc:
            self._wait_for_download_subprocesses()
            message = self._cleanup_partial_download(str(exc))
            logger.info(message)
            self.cancelled.emit(message)
        except Exception as exc:
            if self._terminate_event.is_set():
                self._wait_for_download_subprocesses()
                message = self._cleanup_partial_download(self._message_text("下载已终止，已清理当前下载数据。"))
                logger.info("%s", message)
                self.cancelled.emit(message)
                return
            logger.exception("下载资源失败: %s", exc)
            self.error.emit(str(exc))

    def request_pause(self):
        if not self._terminate_event.is_set():
            self._pause_event.set()

    def request_resume(self):
        if not self._terminate_event.is_set():
            self._pause_notice_emitted = False
            self._pause_event.clear()

    def request_terminate(self):
        self._terminate_event.set()
        self._pause_event.clear()
        self._terminate_download_subprocesses()

    def _terminate_download_subprocesses(self):
        """Stop FFmpeg processes that yt-dlp started for this download.

        Time-range downloads are handed off to FFmpeg. While FFmpeg is blocked in
        network I/O, yt-dlp does not invoke progress hooks, so setting the cancel
        event alone cannot interrupt the download thread.
        """
        target = self._download_dir
        if not target:
            self._download_processes_done.set()
            return

        try:
            target_path = str(Path(target).resolve(strict=False))
            target_prefix = target_path.rstrip(os.sep) + os.sep
            children = psutil.Process(os.getpid()).children(recursive=True)
        except (OSError, psutil.Error) as exc:
            logger.warning("无法枚举下载子进程: %s", exc)
            self._download_processes_done.set()
            return

        processes = []
        for process in children:
            try:
                process_name = process.name().lower()
                command = [str(arg) for arg in process.cmdline()]
                executable_name = Path(command[0]).name.lower() if command else ""
                is_ffmpeg = process_name in {"ffmpeg", "ffmpeg.exe"} or executable_name in {
                    "ffmpeg",
                    "ffmpeg.exe",
                }
                targets_download = any(target_prefix in arg for arg in command[1:])
                if not is_ffmpeg or not targets_download:
                    continue

                processes.append(process)
            except (OSError, psutil.Error):
                continue

        if not processes:
            self._download_processes_done.set()
            return

        self._download_processes_done.clear()
        for process in processes:
            try:
                process.terminate()
                logger.info("已请求终止下载 FFmpeg 子进程: pid=%s", process.pid)
            except (OSError, psutil.Error):
                continue
        try:
            threading.Thread(
                target=self._finish_download_subprocess_termination,
                args=(processes,),
                daemon=True,
                name="video-download-process-cleanup",
            ).start()
        except Exception as exc:
            logger.warning("无法启动下载子进程清理线程，将同步等待: %s", exc)
            self._finish_download_subprocess_termination(processes)

    def _finish_download_subprocess_termination(self, processes):
        try:
            self._kill_download_subprocesses_after_timeout(processes)
        finally:
            self._download_processes_done.set()

    def _wait_for_download_subprocesses(self):
        event = getattr(self, "_download_processes_done", None)
        if event is not None:
            event.wait(timeout=4.0)

    @staticmethod
    def _kill_download_subprocesses_after_timeout(processes):
        try:
            _, alive = psutil.wait_procs(processes, timeout=1.5)
        except psutil.Error as exc:
            logger.warning("等待下载子进程退出失败: %s", exc)
            alive = processes

        for process in alive:
            try:
                process.kill()
                logger.warning("强制结束未响应的下载 FFmpeg 子进程: pid=%s", process.pid)
            except (OSError, psutil.Error):
                continue

    def _raise_if_terminated(self):
        if self._terminate_event.is_set():
            raise DownloadCancelledError("下载已终止，正在清理当前下载数据。")

    def _cut_downloaded_segments(
        self, media_path: str, download_sections: list[str], work_dir: Path
    ) -> None:
        parsed_sections = []
        for section in download_sections:
            parsed = _parse_download_section(section)
            if not parsed:
                raise RuntimeError(f"无效的时间段配置: {section}")
            parsed_sections.append(parsed)

        source = Path(media_path)
        if len(parsed_sections) == 1:
            section = parsed_sections[0]
            temp = source.with_name(f".cut_temp{source.suffix}")
            label = f"{_seconds_to_time_text(section['start_time'])} - {_seconds_to_time_text(section['end_time'])}"
            self.progress.emit(92, self._message_text(f"正在裁剪片段 {label}..."))
            _cut_video_segment(
                str(source), section["start_time"], section["end_time"], str(temp)
            )
            os.replace(temp, source)
        else:
            for idx, section in enumerate(parsed_sections, start=1):
                self._raise_if_terminated()
                segment_name = f"{source.stem}.section_{idx:02d}{source.suffix}"
                output = work_dir / segment_name
                label = f"{_seconds_to_time_text(section['start_time'])} - {_seconds_to_time_text(section['end_time'])}"
                self.progress.emit(
                    90 + idx * 5 // len(parsed_sections),
                    self._message_text(f"正在裁剪片段 {idx}/{len(parsed_sections)}: {label}..."),
                )
                _cut_video_segment(
                    str(source), section["start_time"], section["end_time"], str(output)
                )
            source.unlink(missing_ok=True)

    def _cleanup_partial_download(self, default_message: str) -> str:
        cleanup_lock = getattr(self, "_cleanup_lock", None)
        if cleanup_lock is None:
            cleanup_lock = threading.Lock()
            self._cleanup_lock = cleanup_lock
        with cleanup_lock:
            if getattr(self, "_cleanup_completed", False):
                return getattr(self, "_cleanup_message", "") or default_message
            message = self._cleanup_partial_download_once(default_message)
            self._cleanup_completed = True
            self._cleanup_message = message
            return message

    def _cleanup_partial_download_once(self, default_message: str) -> str:
        target = self._download_dir
        base_dir = self._download_root
        if not target:
            return default_message

        try:
            base_resolved = base_dir.resolve(strict=False)
            target_resolved = target.resolve(strict=False)
            target_resolved.relative_to(base_resolved)
        except (OSError, ValueError):
            logger.warning("跳过清理下载目录，路径校验失败: base=%s target=%s", base_dir, target)
            return default_message

        if target_resolved == base_resolved:
            logger.warning("跳过清理下载目录，目标目录与输出根目录相同: %s", target_resolved)
            return default_message

        try:
            if not target_resolved.exists():
                return default_message

            if not self._download_dir_existed:
                shutil.rmtree(target_resolved)
                return "下载已终止，已清理当前下载目录。"

            cutoff = (self._download_started_at or time.time()) - 1
            files = sorted(
                (path for path in target_resolved.rglob("*") if path.is_file()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            removed_count = 0
            for path in files:
                try:
                    if path.stat().st_mtime >= cutoff:
                        path.unlink(missing_ok=True)
                        removed_count += 1
                except FileNotFoundError:
                    continue

            directories = sorted(
                (path for path in target_resolved.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    continue

            return (
                "下载已终止，已清理当前下载产生的数据。"
                if removed_count
                else default_message
            )
        except Exception as exc:
            logger.warning("清理下载目录失败: %s", exc)
            return f"{default_message} 清理残留时遇到问题: {exc}"

    def progress_hook(self, data):
        if data.get("status") != "downloading":
            return
        percent = data.get("_percent_str", "0")
        speed = data.get("_speed_str", "0")
        eta = data.get("_eta_str", "")
        downloaded_bytes = data.get("_downloaded_bytes_str", "")
        total_bytes = data.get("_total_bytes_str", "") or data.get("_total_bytes_estimate_str", "")
        elapsed = data.get("_elapsed_str", "")
        filename = data.get("filename") or data.get("info_dict", {}).get("title") or ""
        clean_percent = (
            str(percent).replace("\x1b[0;94m", "").replace("\x1b[0m", "").strip().replace("%", "")
        )
        clean_speed = str(speed).replace("\x1b[0;32m", "").replace("\x1b[0m", "").strip()
        clean_eta = str(eta).strip()
        clean_downloaded = str(downloaded_bytes).strip()
        clean_total = str(total_bytes).strip()
        clean_elapsed = str(elapsed).strip()

        try:
            progress_value = int(float(clean_percent))
        except ValueError:
            progress_value = 0
        self._current_progress = progress_value
        detail_payload = {
            "percent": clean_percent,
            "speed": clean_speed,
            "eta": clean_eta,
            "downloaded": clean_downloaded,
            "total": clean_total,
            "elapsed": clean_elapsed,
            "filename": str(filename),
        }
        self.progress_detail.emit(detail_payload)

        self._raise_if_terminated()

        if self._pause_event.is_set():
            if not self._pause_notice_emitted:
                self._pause_notice_emitted = True
                self.progress.emit(
                    self._current_progress,
                    "下载已暂停，可点击继续下载，或终止下载并清理当前数据。",
                )
            while self._pause_event.is_set():
                self._raise_if_terminated()
                time.sleep(0.2)

        self.progress.emit(progress_value, f"下载进度: {clean_percent}%  速度: {clean_speed}")

    def _emit_range_progress_detail(self, detail: dict):
        if not self._terminate_event.is_set():
            self.progress_detail.emit(detail)

    def _range_section_durations(self) -> list[float]:
        durations = []
        for section in self.download_sections:
            parsed = _parse_download_section(section)
            if parsed:
                durations.append(float(parsed["end_time"] - parsed["start_time"]))
        return durations

    def _range_phase_detail(self, phase: str, status: str, *, indeterminate: bool) -> dict:
        return {
            "phase": phase,
            "indeterminate": indeterminate,
            "percent": "",
            "speed": "",
            "eta": "",
            "downloaded": "",
            "total": "",
            "elapsed": "00:00",
            "filename": "",
            "section_index": 1,
            "section_count": max(1, len(self.download_sections)),
            "status": status,
        }

    def _default_format_selector(self) -> str:
        return _robust_format_selector("", self.download_mode)

    def _fallback_format_selector(self) -> str:
        if self.ensure_mp4_output and self.download_mode == "video_audio":
            return (
                "bv*[ext=mp4]+ba[ext=m4a]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/"
                "bv*+ba/bestvideo+bestaudio/best"
            )
        return self._default_format_selector()

    def _effective_format_selector(self) -> str:
        if self.format_selector:
            return _robust_format_selector(self.format_selector, self.download_mode)
        if self.download_mode == "audio":
            return _robust_format_selector(
                self.selected_audio_format_id or self._default_format_selector(),
                self.download_mode,
            )
        if self.download_mode == "video":
            return _robust_format_selector(
                self.selected_video_format_id or self._default_format_selector(),
                self.download_mode,
            )
        if self.selected_video_format_id and self.selected_audio_format_id:
            return f"{self.selected_video_format_id}+{self.selected_audio_format_id}"
        if self.selected_video_format_id:
            return self.selected_video_format_id
        return self._default_format_selector()

    def _effective_format_selector_for_info(self, info_dict: dict) -> str:
        if (self.prefer_compatible_codecs and not self.format_selector
                and not self.selected_video_format_id and not self.selected_audio_format_id):
            return _resolution_first_format_selector(info_dict, self.download_mode)
        if not info_dict.get("_videocaptioner_youtube_player_client"):
            return self._effective_format_selector()

        video_selector = _stable_selected_format_selector(
            info_dict, self.selected_video_format_id, "video"
        )
        audio_selector = _stable_selected_format_selector(
            info_dict, self.selected_audio_format_id, "audio"
        )
        if self.download_mode == "video_audio" and video_selector and audio_selector:
            return f"{video_selector}+{audio_selector}"
        if self.download_mode == "video" and video_selector:
            return video_selector
        if self.download_mode == "audio" and audio_selector:
            return audio_selector
        if video_selector:
            return video_selector
        return self._effective_format_selector()

    def _find_main_media_files(self, work_dir: Path) -> list[str]:
        candidates = []
        for file in work_dir.rglob("*"):
            if not file.is_file():
                continue
            if "subtitle" in {part.lower() for part in file.parts}:
                continue
            suffix = file.suffix.lower()
            if suffix in IMAGE_EXTENSIONS or suffix in SUBTITLE_EXTENSIONS:
                continue
            if suffix in {".json", ".part", ".ytdl", ".tmp", ".txt"}:
                continue
            candidates.append(file)

        if not candidates:
            return []

        if self.download_mode == "audio":
            audio_candidates = [file for file in candidates if file.suffix.lower() in AUDIO_EXTENSIONS]
            if audio_candidates:
                candidates = audio_candidates
        else:
            video_candidates = [file for file in candidates if file.suffix.lower() in VIDEO_EXTENSIONS]
            if video_candidates:
                candidates = video_candidates

        candidates.sort(key=lambda file: (file.stat().st_size, file.stat().st_mtime), reverse=True)
        return [str(file) for file in candidates]

    def _find_main_media_file(self, work_dir: Path) -> str | None:
        media_files = self._find_main_media_files(work_dir)
        return media_files[0] if media_files else None

    def _recover_missing_audio(
        self,
        media_path: str,
        proxy_url: str,
        cookiefile_path: Path,
        work_dir: Path,
        download_sections: list[str],
    ) -> None:
        audio_selector = (
            f"ba[format_id^={self.selected_audio_format_id}]"
            if self.selected_audio_format_id
            else "bestaudio[ext=m4a]/bestaudio"
        )
        self.progress.emit(96, self._message_text("检测到空音轨，正在仅补下载音频..."))
        logger.warning(
            "下载结果缺少有效音频，开始仅补下载音频: media=%s format=%s",
            media_path,
            audio_selector,
        )

        with tempfile.TemporaryDirectory(
            prefix=".videocaptioner-audio-recovery-", dir=work_dir
        ) as temp_dir:
            recovery_options = _build_ydl_options(proxy_url, cookiefile_path)
            recovery_options.update(_build_strategy_options("单线程"))
            recovery_options.update(
                {
                    "format": audio_selector,
                    "outtmpl": {"default": "audio.%(ext)s"},
                    "paths": {"home": temp_dir},
                    "noplaylist": True,
                }
            )
            with _create_youtube_dl(recovery_options) as ydl:
                ydl.download([self.url])

            audio_files = [
                path
                for path in Path(temp_dir).iterdir()
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            ]
            if not audio_files:
                raise RuntimeError("音频补下载完成，但没有找到音频文件")
            audio_files.sort(key=lambda path: path.stat().st_size, reverse=True)
            _remux_recovery_audio(media_path, audio_files[0], download_sections)
        logger.info("空音轨已自动修复: %s", media_path)

    def _write_metadata_file(self, info_dict: dict, work_dir: Path) -> str:
        metadata_path = work_dir / f"{sanitize_filename(info_dict.get('title', 'video'))}.info.json"
        work_dir.mkdir(parents=True, exist_ok=True)
        with open(metadata_path, "w", encoding="utf-8") as file:
            json.dump(info_dict, file, ensure_ascii=False, indent=2, default=str)
        return str(metadata_path)

    def _write_description_txt_file(self, info_dict: dict, work_dir: Path) -> str:
        return write_description_txt_file(
            info_dict,
            work_dir,
            sanitize_filename(info_dict.get("title", "video")),
            template=self.description_txt_template,
        )

    def _write_transcript_txt_file(self, subtitle_path: str, info_dict: dict, work_dir: Path) -> str:
        return write_transcript_txt_file(
            subtitle_path,
            work_dir,
            sanitize_filename(info_dict.get("title", "video")),
        )

    def _postprocess_pr_smart_hevc(
        self, video_path: str | None
    ) -> tuple[str | None, str | None, str, bool, str | None, str | None]:
        if not self.pr_smart_transcode_hevc_on_av1:
            return None, None, "未启用", False, None, None
        if not video_path:
            return None, None, "未找到可转码的视频", False, None, None

        source_path = Path(video_path)
        target_path = source_path.with_name(f"{source_path.stem}-hevc.mp4")

        encoder_preference = getattr(self, "hevc_encoder", "auto")
        native_supported = encoder_preference == "auto" and is_native_hevc_transcode_supported()
        codec = ""
        if native_supported:
            try:
                codec = get_native_video_codec(str(source_path))
            except Exception as exc:
                logger.warning("macOS 原生编码检测失败，改用 FFmpeg 探测: %s", exc)
        if not codec:
            codec = get_video_codec(str(source_path))
        if codec not in {"av1", "vp9"}:
            return None, None, f"未触发，当前编码为 {codec or '未知'}", False, None, None

        native_error: Exception | None = None
        if native_supported:
            try:
                encoder = transcode_video_to_hevc_native(
                    str(source_path),
                    str(target_path),
                    progress_callback=self.progress.emit,
                    preset_name=self.native_hevc_preset,
                    cancel_check=self._raise_if_terminated,
                )
                return (
                    str(target_path),
                    encoder,
                    f"已使用 macOS 原生 API 转码为 H.265",
                    False,
                    None,
                    None,
                )
            except DownloadCancelledError:
                raise
            except Exception as exc:
                native_error = exc
                logger.exception("macOS 原生 H.265 后处理失败，改用 FFmpeg 重试: %s", exc)
                self.progress.emit(0, "macOS 原生 H.265 失败，正在使用 FFmpeg 重试...")

        try:
            encoder = transcode_video_to_hevc(
                str(source_path),
                str(target_path),
                progress_callback=self.progress.emit,
                transcode_audio_to_aac=True,
                encoder_preference=encoder_preference,
            )
        except Exception as exc:
            logger.exception("FFmpeg H.265 后处理失败: %s", exc)
            message = f"FFmpeg H.265 后处理失败: {exc}"
            if native_error is not None:
                message = (
                    f"macOS 原生 H.265 后处理失败，FFmpeg 重试也失败: "
                    f"{native_error}; {exc}"
                )
            return (
                None,
                None,
                message,
                True,
                str(source_path),
                str(target_path),
            )

        if native_error is not None:
            message = f"macOS 原生失败，已使用 FFmpeg 转码为 H.265（{encoder}）"
        else:
            message = f"已使用 FFmpeg 转码为 H.265（{encoder}）"
        return str(target_path), encoder, message, False, None, None

    def download(
        self,
        need_video: bool = True,
        need_subtitle: bool = True,
        need_thumbnail: bool = False,
        subtitle_mode: str = "auto",
        subtitle_language: str = "en",
        need_metadata: bool = False,
        need_description_txt: bool = False,
        need_transcript_txt: bool = False,
        enable_time_ranges: bool = False,
        download_sections: list[str] | None = None,
        pr_smart_transcode_hevc_on_av1: bool = False,
        ensure_mp4_output: bool = False,
        resume_existing: bool = False,
    ) -> dict:
        logger.info("开始下载资源: %s", self.url)
        self._download_started_at = time.time()
        self._raise_if_terminated()
        proxy_url = (apply_download_proxy_environment() if self.proxy_url is None
                     else apply_download_proxy_environment("手动设置" if self.proxy_url else "不使用代理", self.proxy_url))
        cookiefile_path = _resolve_cookiefile_path(self.cookie_file)
        effective_strategy = _resolve_download_engine_strategy(
            self.download_engine_strategy, proxy_url, cookiefile_path
        )
        if effective_strategy:
            logger.info("下载引擎策略: %s", effective_strategy)

        info_dict = _extract_metadata_info(
            self.url, proxy_url, cookiefile_path, effective_strategy
        )
        active_cookiefile_path = (
            NO_COOKIE_FILE_PATH
            if info_dict.get("_videocaptioner_disable_cookiefile")
            else cookiefile_path
        )
        self._raise_if_terminated()
        title = sanitize_filename(info_dict.get("title", "MyVideo"))
        work_dir = Path(self.work_dir) / title
        self._download_dir = work_dir
        self._download_dir_existed = work_dir.exists()
        work_dir.mkdir(parents=True, exist_ok=True)
        self._raise_if_terminated()

        subtitle_language = str(subtitle_language or "auto").strip().lower()
        subtitle_candidates = _subtitle_candidates(info_dict, subtitle_mode, subtitle_language)
        selected_subtitle = None
        subtitle_attempted = False

        subtitle_download_link = None
        subtitle_ext = "vtt"
        effective_need_subtitle = need_subtitle or need_transcript_txt
        subtitle_path = (
            _find_reusable_subtitle_path(work_dir, subtitle_language)
            if resume_existing and effective_need_subtitle
            else None
        )
        if subtitle_mode == "prefer_manual":
            # A legacy filename does not prove which language/source produced it.
            subtitle_path = None
        if subtitle_path:
            logger.info("继续下载时复用已完成字幕: %s", subtitle_path)

        if effective_need_subtitle and not subtitle_path:
            subtitle_download_link, subtitle_ext = _pick_subtitle_item(
                info_dict, subtitle_mode, subtitle_language
            )

        fallback_proxy = proxy_url if self.proxy_url is not None else (proxy_url or get_effective_download_proxy_url())
        if subtitle_mode == "prefer_manual" and effective_need_subtitle:
            subtitle_attempted = True
            for candidate in subtitle_candidates:
                self._raise_if_terminated()
                try:
                    reference_path = work_dir / "subtitle" / f"【下载字幕】_{candidate['language']}_{candidate['kind']}"
                    saved_path = reference_path.with_suffix(f".{candidate['ext']}")
                    if resume_existing and saved_path.is_file() and saved_path.stat().st_size:
                        subtitle_path = str(saved_path)
                    else:
                        subtitle_path = _download_subtitle_fallback(
                            candidate["url"], candidate["ext"], reference_path, fallback_proxy,
                        )
                    if subtitle_path:
                        selected_subtitle = candidate
                        break
                except requests.RequestException as exc:
                    logger.warning("%s %s 字幕下载失败，尝试下一条原语言轨: %s",
                                   candidate["kind"], candidate["language"], exc)
        transcript_txt_path = (
            _find_reusable_transcript_path(work_dir, title)
            if resume_existing and need_transcript_txt and subtitle_mode != "prefer_manual"
            else None
        )
        transcript_message = "已复用" if transcript_txt_path else "未触发"
        terms_txt_path = None
        terms_message = "请在 WhisperX 热词管理中手动生成"
        if need_transcript_txt and not transcript_txt_path:
            if subtitle_path:
                self.progress.emit(2, self._message_text("复用已下载字幕生成视频文稿..."))
                try:
                    transcript_txt_path = self._write_transcript_txt_file(
                        subtitle_path, info_dict, work_dir
                    )
                    transcript_message = "已复用字幕生成"
                except Exception as exc:
                    logger.exception("复用字幕生成视频文稿失败: %s", exc)
                    transcript_message = f"复用字幕生成视频文稿失败，稍后重试: {exc}"
            elif not subtitle_attempted:
                self.progress.emit(2, self._message_text("提前下载字幕并生成视频文稿..."))
                try:
                    subtitle_path = _download_subtitle_fallback(
                        subtitle_download_link,
                        subtitle_ext,
                        work_dir / "subtitle" / f"【下载字幕】_{subtitle_language or subtitle_mode}",
                        fallback_proxy,
                    )
                    if subtitle_path:
                        transcript_txt_path = self._write_transcript_txt_file(
                            subtitle_path, info_dict, work_dir
                        )
                        transcript_message = "已提前生成"
                    else:
                        transcript_message = "未找到可提前下载的字幕，稍后尝试生成视频文稿"
                except Exception as exc:
                    logger.exception("提前生成视频文稿失败: %s", exc)
                    transcript_message = f"提前生成视频文稿失败，稍后重试: {exc}"
            self._raise_if_terminated()

        ydl_need_subtitle = effective_need_subtitle and not subtitle_path and not subtitle_attempted and bool(subtitle_candidates)
        options = _build_ydl_options(
            proxy_url, active_cookiefile_path, progress_hooks=[self.progress_hook]
        )
        if player_client := info_dict.get(
            "_videocaptioner_youtube_player_client"
        ):
            _set_youtube_player_client(options, str(player_client))
        options.update(_build_strategy_options(effective_strategy))
        options.update(
            {
                "outtmpl": {
                    "default": "%(title)s.%(ext)s",
                    "subtitle": "【下载字幕】.%(ext)s",
                    "thumbnail": "thumbnail",
                },
                "writesubtitles": ydl_need_subtitle and subtitle_mode == "manual",
                "writeautomaticsub": ydl_need_subtitle and subtitle_mode == "auto",
                "subtitleslangs": [subtitle_candidates[0]["track"]] if subtitle_candidates else [],
                "writethumbnail": need_thumbnail,
                "thumbnail_format": "png",
                "skip_download": not need_video,
            }
        )
        if need_video:
            options["format"] = self._effective_format_selector_for_info(info_dict)
            logger.info("使用 format 选择器: %s", options["format"])
            if enable_time_ranges and download_sections:
                logger.info("使用时间段下载（先完整下载再本地裁剪）: %s", " | ".join(download_sections))

        options["paths"] = {
            "home": str(work_dir),
            "subtitle": str(work_dir / "subtitle"),
            "thumbnail": str(work_dir),
        }

        ffmpeg_progress_monitor = None

        try:
            try:
                with _create_youtube_dl(options) as ydl:
                    ydl.download([self.url])
            except DownloadError as exc:
                fallback_selector = self._fallback_format_selector()
                current_selector = str(options.get("format") or "")
                if need_video and _is_expired_media_url_error(exc):
                    removed = _clean_stale_partial_files(work_dir)
                    logger.warning(
                        "下载地址返回 403，已清理 %s 个过期断点文件并重新解析下载一次",
                        removed,
                    )
                    with _create_youtube_dl(options) as ydl:
                        ydl.download([self.url])
                elif (
                    need_video
                    and fallback_selector
                    and fallback_selector != current_selector
                    and _is_format_selection_error(exc)
                ):
                    logger.warning(
                        "指定格式不可用，改用默认格式选择器重试: %s -> %s",
                        current_selector,
                        fallback_selector,
                    )
                    options["format"] = fallback_selector
                    with _create_youtube_dl(options) as ydl:
                        ydl.download([self.url])
                else:
                    raise
        finally:
            if ffmpeg_progress_monitor is not None:
                ffmpeg_progress_monitor.stop()
        self._raise_if_terminated()

        if need_video and enable_time_ranges and download_sections:
            raw_files = self._find_main_media_files(work_dir)
            if len(raw_files) == 1:
                self._cut_downloaded_segments(raw_files[0], download_sections, work_dir)
            elif raw_files:
                logger.warning("多文件下载暂不支持本地时间段裁剪，已保留完整文件")

        media_files = self._find_main_media_files(work_dir) if need_video else []
        if need_video and self.download_mode == "video_audio" and media_files:
            invalid_audio_files = [
                path for path in media_files if not _media_has_valid_audio(path)
            ]
            if len(media_files) == 1 and invalid_audio_files:
                self._recover_missing_audio(
                    media_files[0],
                    proxy_url,
                    active_cookiefile_path,
                    work_dir,
                    list(download_sections or []),
                )
            elif invalid_audio_files:
                raise RuntimeError(
                    "下载结果中有文件缺少有效音频轨，多个片段无法自动配对修复"
                )
        mp4_normalization_message = None
        if (
            ensure_mp4_output
            and self.download_mode != "audio"
            and len(media_files) == 1
            and Path(media_files[0]).suffix.lower() != ".mp4"
        ):
            source_path = Path(media_files[0])
            target_path = source_path.with_suffix(".mp4")
            self.progress.emit(97, self._message_text("正在将回退格式转换为 MP4..."))
            encoder = normalize_video_to_mp4(
                str(source_path),
                str(target_path),
                encoder_preference=getattr(self, "hevc_encoder", "auto"),
                progress_callback=self.progress.emit,
                force_hevc_for_codecs={"av1", "vp9"},
            )
            source_path.unlink()
            media_files = [str(target_path)]
            mp4_normalization_message = f"已将回退格式转换为 MP4（{encoder}）"
        media_path = media_files[0] if len(media_files) == 1 else (str(work_dir) if media_files else None)
        if not subtitle_path and not subtitle_attempted:
            subtitle_path = _find_reusable_subtitle_path(work_dir, subtitle_language)

        if effective_need_subtitle and not subtitle_path and not subtitle_attempted:
            subtitle_path = _download_subtitle_fallback(
                subtitle_download_link,
                subtitle_ext,
                work_dir / "subtitle" / f"【下载字幕】_{subtitle_language or subtitle_mode}",
                fallback_proxy,
            )

        thumbnail_path = None
        for file in work_dir.glob("**/thumbnail*"):
            thumbnail_path = str(file)
            break

        if need_thumbnail and not thumbnail_path:
            thumbnail_path = _download_thumbnail_fallback(
                info_dict.get("thumbnail"),
                work_dir / "thumbnail.jpg",
                fallback_proxy,
            )
        thumbnail_path = _normalize_thumbnail_to_png(thumbnail_path)

        metadata_path = self._write_metadata_file(info_dict, work_dir) if need_metadata else None
        description_txt_path = (
            self._write_description_txt_file(info_dict, work_dir)
            if need_video and need_description_txt
            else None
        )
        if need_transcript_txt and not transcript_txt_path:
            if subtitle_path:
                try:
                    transcript_txt_path = self._write_transcript_txt_file(
                        subtitle_path, info_dict, work_dir
                    )
                    transcript_message = "已生成"
                except Exception as exc:
                    logger.exception("视频文稿生成失败: %s", exc)
                    transcript_message = f"视频文稿生成失败: {exc}"
            else:
                transcript_message = "未下载到字幕，无法生成视频文稿"
        multi_media = len(media_files) > 1
        original_video_path = media_files[0] if self.download_mode != "audio" and len(media_files) == 1 else None
        transcoded_video_path = None
        transcoded_video_codec = None
        postprocess_message = "未触发"
        postprocess_failed = False
        postprocess_fallback_source_path = None
        postprocess_fallback_target_path = None
        preferred_media_path = media_path
        if need_video and not multi_media and self.download_mode != "audio" and pr_smart_transcode_hevc_on_av1:
            if enable_time_ranges and download_sections:
                self._emit_range_progress_detail(
                    self._range_phase_detail("postprocessing", "正在后处理", indeterminate=True)
                )
            try:
                (
                    transcoded_video_path,
                    transcoded_video_codec,
                    postprocess_message,
                    postprocess_failed,
                    postprocess_fallback_source_path,
                    postprocess_fallback_target_path,
                ) = self._postprocess_pr_smart_hevc(
                    original_video_path
                )
                if transcoded_video_path:
                    preferred_media_path = transcoded_video_path
                elif mp4_normalization_message:
                    postprocess_message = mp4_normalization_message
            except DownloadCancelledError:
                raise
            except Exception as exc:
                logger.exception("PR智能预设后处理失败: %s", exc)
                postprocess_message = f"H.265 后处理失败: {exc}"
                postprocess_failed = True
                if original_video_path:
                    source_path = Path(original_video_path)
                    postprocess_fallback_source_path = str(source_path)
                    postprocess_fallback_target_path = str(
                        source_path.with_name(f"{source_path.stem}-hevc.mp4")
                    )
        elif mp4_normalization_message:
            postprocess_message = mp4_normalization_message

        result = {
            "video_path": transcoded_video_path or original_video_path,
            "audio_path": media_files[0] if self.download_mode == "audio" and len(media_files) == 1 else None,
            "media_path": preferred_media_path,
            "media_paths": media_files,
            "original_video_path": original_video_path,
            "transcoded_video_path": transcoded_video_path,
            "transcoded_video_codec": transcoded_video_codec,
            "postprocess_message": postprocess_message,
            "postprocess_failed": postprocess_failed,
            "postprocess_fallback_source_path": postprocess_fallback_source_path,
            "postprocess_fallback_target_path": postprocess_fallback_target_path,
            "subtitle_path": subtitle_path,
            "subtitle_language": selected_subtitle["language"] if selected_subtitle else None,
            "subtitle_kind": selected_subtitle["kind"] if selected_subtitle else None,
            "subtitle_track": selected_subtitle["track"] if selected_subtitle else None,
            "thumbnail_path": thumbnail_path,
            "metadata_path": metadata_path,
            "description_txt_path": description_txt_path,
            "transcript_txt_path": transcript_txt_path,
            "transcript_message": transcript_message,
            "terms_txt_path": terms_txt_path,
            "terms_message": terms_message,
            "info_dict": info_dict,
            "work_dir": str(work_dir),
            "url": self.url,
            "download_mode": self.download_mode,
            "format_selector": str(options.get("format") or "") if need_video else "",
            "download_sections": list(download_sections or []),
            "has_multiple_media_files": multi_media,
        }
        logger.info(
            "下载完成: media=%s media_count=%s subtitle=%s thumbnail=%s metadata=%s description_txt=%s transcript_txt=%s terms_txt=%s",
            result["media_path"],
            len(media_files),
            subtitle_path,
            thumbnail_path,
            metadata_path,
            description_txt_path,
            transcript_txt_path,
            terms_txt_path,
        )
        return result


def __getattr__(name):
    # Load Qt only for GUI callers. The legacy module aliases this module so
    # existing imports and monkeypatches continue to address the same globals.
    if name == "cfg":
        from app.common.config import cfg
        return cfg
    if name not in {"VideoDownloadThread", "VideoPreviewThread"}:
        raise AttributeError(name)
    from PyQt5.QtCore import QThread, pyqtSignal
    from app.common.config import cfg

    class VideoDownloadThread(VideoDownloadService, QThread):
        native_hevc_preset = str(cfg.get(cfg.download_native_hevc_preset))
        finished = pyqtSignal(str)
        detailed_finished = pyqtSignal(dict)
        progress = pyqtSignal(int, str)
        progress_detail = pyqtSignal(dict)
        error = pyqtSignal(str)
        cancelled = pyqtSignal(str)

        def __init__(self, *args, **kwargs):
            QThread.__init__(self)
            from app.common.config import cfg
            kwargs.setdefault("native_hevc_preset", str(cfg.get(cfg.download_native_hevc_preset)))
            kwargs.setdefault("hevc_encoder", str(cfg.get(cfg.download_hevc_encoder)))
            VideoDownloadService.__init__(self, *args, **kwargs)

        def run(self):
            VideoDownloadService.run(self)

    class VideoPreviewThread(QThread):
        finished = pyqtSignal(dict)
        error = pyqtSignal(str)

        def __init__(self, url, download_engine_strategy=None):
            super().__init__()
            self.url = url
            self.download_engine_strategy = download_engine_strategy

        def run(self):
            try:
                self.finished.emit(extract_preview(self.url, self.download_engine_strategy))
            except Exception as exc:
                logger.exception("解析下载资源失败: %s", exc)
                self.error.emit(str(exc))

    globals().update(VideoDownloadThread=VideoDownloadThread, VideoPreviewThread=VideoPreviewThread)
    return globals()[name]
