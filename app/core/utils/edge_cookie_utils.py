from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

from app.config import APP_DATA_PATH
from app.core.utils.platform_utils import DEFAULT_COOKIE_BROWSER_LABEL

COOKIE_FILE_PATH = APP_DATA_PATH / "cookies.txt"
COOKIE_BROWSER_LABELS = {
    "safari": "Safari",
    "chrome": "Chrome",
    "edge": "Edge",
}
DEFAULT_COOKIE_BROWSER = DEFAULT_COOKIE_BROWSER_LABEL.lower()
SAFARI_COOKIE_DATABASES = (
    Path("~/Library/Cookies/Cookies.binarycookies"),
    Path(
        "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/"
        "Cookies.binarycookies"
    ),
)
MACOS_FULL_DISK_ACCESS_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)
YOUTUBE_COOKIE_DOMAINS = ("youtube.com", "youtu.be", "google.com", "googlevideo.com")
BILIBILI_COOKIE_DOMAINS = ("bilibili.com",)
DOWNLOAD_COOKIE_DOMAINS = YOUTUBE_COOKIE_DOMAINS + BILIBILI_COOKIE_DOMAINS
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
BILIBILI_REQUIRED_COOKIES = {
    "SESSDATA",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "bili_jct",
}
YOUTUBE_REQUIRED_COOKIES = {"SID", "HSID", "SSID", "SAPISID", "APISID"}


class _CookieLogger:
    def debug(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_cookie_browser(browser: str | None) -> str:
    value = str(browser or "").strip().lower()
    if value in COOKIE_BROWSER_LABELS:
        return value
    return DEFAULT_COOKIE_BROWSER


def cookie_browser_label(browser: str | None) -> str:
    if not str(browser or "").strip():
        return "未知"
    return COOKIE_BROWSER_LABELS[normalize_cookie_browser(browser)]


def _cookie_attr(cookie: Any, name: str, default: Any = None) -> Any:
    if isinstance(cookie, dict):
        return cookie.get(name, default)
    return getattr(cookie, name, default)


def _cookie_domain(cookie: Any) -> str:
    return str(_cookie_attr(cookie, "domain", "") or "").lstrip(".").lower()


def _cookie_name(cookie: Any) -> str:
    return str(_cookie_attr(cookie, "name", "") or "")


def _domain_matches(domain: str, target_domains: Iterable[str]) -> bool:
    normalized = str(domain or "").lstrip(".").lower()
    if not normalized:
        return False
    for target_domain in target_domains:
        target = str(target_domain or "").lstrip(".").lower()
        if normalized == target or normalized.endswith(f".{target}"):
            return True
    return False


def _is_download_cookie(cookie: Any) -> bool:
    return _domain_matches(_cookie_domain(cookie), DOWNLOAD_COOKIE_DOMAINS)


def _filtered_cookie_jar(
    cookie_jar: Iterable[Any], target_path: Path
) -> YoutubeDLCookieJar:
    export_jar = YoutubeDLCookieJar(str(target_path))
    for cookie in cookie_jar:
        if _is_download_cookie(cookie):
            export_jar.set_cookie(cookie)
    return export_jar


def _cookie_summary(cookies: Iterable[Any]) -> dict:
    cookie_list = list(cookies)
    domains = {_cookie_domain(cookie) for cookie in cookie_list}
    names_by_domain = [
        _cookie_name(cookie)
        for cookie in cookie_list
        if _domain_matches(_cookie_domain(cookie), BILIBILI_COOKIE_DOMAINS)
    ]
    bilibili_cookie_names = sorted({name for name in names_by_domain if name})
    has_youtube = any(
        _domain_matches(domain, YOUTUBE_COOKIE_DOMAINS) for domain in domains
    )
    youtube_cookie_names = {
        _cookie_name(cookie)
        for cookie in cookie_list
        if _domain_matches(_cookie_domain(cookie), YOUTUBE_COOKIE_DOMAINS)
    }
    has_bilibili = any(
        _domain_matches(domain, BILIBILI_COOKIE_DOMAINS) for domain in domains
    )
    has_bilibili_login = BILIBILI_REQUIRED_COOKIES.issubset(
        set(bilibili_cookie_names)
    )
    return {
        "cookie_count": len(cookie_list),
        "has_youtube": has_youtube,
        "has_youtube_login": YOUTUBE_REQUIRED_COOKIES.issubset(youtube_cookie_names),
        "has_bilibili": has_bilibili,
        "has_bilibili_login": has_bilibili_login,
        "bilibili_cookie_names": bilibili_cookie_names,
    }


def _cookie_result(
    *,
    success: bool,
    status: str,
    message: str,
    path: Path,
    cookie_count: int = 0,
    has_youtube: bool = False,
    has_youtube_login: bool = False,
    has_bilibili: bool = False,
    has_bilibili_login: bool = False,
    bilibili_cookie_names: list[str] | None = None,
    updated_at: str = "",
    status_code: str | None = None,
    source_browser: str | None = None,
    is_elevated: bool = False,
    needs_elevation_hint: bool = False,
) -> dict:
    normalized_browser = (
        normalize_cookie_browser(source_browser) if str(source_browser or "").strip() else ""
    )
    return {
        "success": success,
        "status": status,
        "status_code": status_code or status,
        "message": message,
        "path": str(path),
        "cookie_count": cookie_count,
        "has_youtube": has_youtube,
        "has_youtube_login": has_youtube_login,
        "has_bilibili": has_bilibili,
        "has_bilibili_login": has_bilibili_login,
        "bilibili_cookie_names": bilibili_cookie_names or [],
        "updated_at": updated_at,
        "source_browser": normalized_browser,
        "source_browser_label": cookie_browser_label(normalized_browser),
        "is_elevated": is_elevated,
        "needs_elevation_hint": needs_elevation_hint,
    }


def _describe_export_error(
    error: Exception, browser: str | None = None
) -> tuple[str, str]:
    label = cookie_browser_label(browser)
    if isinstance(error, PermissionError):
        if normalize_cookie_browser(browser) == "safari":
            return (
                "permission_denied",
                "Safari Cookie 无法访问。请前往“系统设置 > 隐私与安全性 > "
                "完全磁盘访问权限”，允许 VideoCaptioner；随后完全退出并重新"
                "打开应用，再次提取",
            )
        return "permission_denied", f"{label} Cookie 导出失败：浏览器数据当前不可访问"

    if isinstance(error, FileNotFoundError):
        if normalize_cookie_browser(browser) == "safari":
            return (
                "browser_cookie_not_found",
                "未找到 Safari Cookie 数据库，请先使用 Safari 登录 B站或 YouTube，"
                "并至少访问一次对应网站后重试",
            )
        return (
            "browser_cookie_not_found",
            f"未找到 {label} Cookie 数据库，请先启动并登录 {label} 后重试",
        )

    text = str(error).lower()
    if any(token in text for token in ("locked", "in use", "sharing violation")):
        return "browser_locked", f"{label} Cookie 导出失败：请关闭 {label} 后重试"
    if any(token in text for token in ("decrypt", "crypt", "keychain")):
        return "extract_failed", f"{label} Cookie 导出失败：本机浏览器数据解密失败"
    if "sqlite" in text or "database" in text:
        return "database_error", f"{label} Cookie 导出失败：浏览器 Cookie 数据库读取失败"
    return "extract_failed", f"{label} Cookie 导出失败：{error}"


def _find_safari_cookie_database(
    candidates: Iterable[Path] | None = None,
) -> Path:
    """Return a readable Safari cookie database and preserve TCC errors.

    yt-dlp checks Safari paths with ``isfile``. macOS privacy controls may make
    that check look exactly like a missing file, so probe the file directly to
    distinguish a missing database from denied Full Disk Access.
    """
    paths = candidates or SAFARI_COOKIE_DATABASES
    for candidate in paths:
        path = candidate.expanduser()
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise PermissionError(
                f"macOS denied access to Safari cookie database: {path}"
            ) from exc
        return path

    raise FileNotFoundError("could not find safari cookies database")


def _extract_browser_cookies_with_ytdlp(
    target_path: Path, browser: str | None = None
) -> YoutubeDLCookieJar:
    browser = normalize_cookie_browser(browser)
    profile = None
    if browser == "safari":
        profile = str(_find_safari_cookie_database())
    extracted_jar = extract_cookies_from_browser(
        browser, profile=profile, logger=_CookieLogger()
    )
    return _filtered_cookie_jar(extracted_jar, target_path)


def _prepare_private_cookie_target(target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.parent.chmod(PRIVATE_DIR_MODE)


def _write_cookie_jar(
    cookie_jar: YoutubeDLCookieJar, target_path: Path, browser: str | None = None
) -> None:
    _prepare_private_cookie_target(target_path)
    lines = [
        "# Netscape HTTP Cookie File",
        "# This file is generated by VideoCaptioner.",
        f"# Source browser: {cookie_browser_label(browser)}",
    ]
    for cookie in cookie_jar:
        domain = str(cookie.domain or "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.path or "/")
        secure = "TRUE" if bool(cookie.secure) else "FALSE"
        expires = str(int(cookie.expires or 0))
        name = str(cookie.name or "")
        value = str(cookie.value or "")
        lines.append(
            "\t".join([domain, include_subdomains, path, secure, expires, name, value])
        )

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=str(target_path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        temp_path.chmod(PRIVATE_FILE_MODE)
        os.replace(temp_path, target_path)
        target_path.chmod(PRIVATE_FILE_MODE)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _remove_cookie_file(target_path: Path) -> None:
    try:
        target_path.unlink()
    except FileNotFoundError:
        return


def _read_cookie_source_browser(target_path: Path) -> str | None:
    try:
        for line in target_path.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            if line.startswith("# Source browser:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _private_mode_needs_update(target_path: Path) -> bool:
    if os.name != "posix":
        return False
    try:
        parent_mode = target_path.parent.stat().st_mode & 0o777
        file_mode = target_path.stat().st_mode & 0o777
    except OSError:
        return True
    return parent_mode != PRIVATE_DIR_MODE or file_mode != PRIVATE_FILE_MODE


def harden_cookie_file(cookie_path: Path | None = None) -> dict:
    target_path = Path(cookie_path or COOKIE_FILE_PATH)
    source_browser = (
        _read_cookie_source_browser(target_path) if target_path.exists() else None
    )
    if not target_path.exists():
        return _cookie_result(
            success=False,
            status="missing",
            status_code="file_missing",
            message="cookies.txt 不存在",
            path=target_path,
            source_browser=source_browser,
        )

    cookie_jar = YoutubeDLCookieJar(str(target_path))
    cookie_jar.load(str(target_path), ignore_discard=True, ignore_expires=True)
    raw_cookies = list(cookie_jar)
    filtered_jar = _filtered_cookie_jar(raw_cookies, target_path)
    filtered_cookies = list(filtered_jar)

    if not filtered_cookies:
        _remove_cookie_file(target_path)
        return _cookie_result(
            success=False,
            status="empty",
            status_code="verify_empty",
            message="Cookie 文件不含可用于下载中心的站点 Cookie",
            path=target_path,
            source_browser=source_browser,
        )

    if len(filtered_cookies) != len(raw_cookies) or _private_mode_needs_update(
        target_path
    ):
        _write_cookie_jar(filtered_jar, target_path, source_browser)

    summary = _cookie_summary(filtered_cookies)
    updated_at = datetime.fromtimestamp(target_path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return _cookie_result(
        success=True,
        status="available",
        status_code="verify_ok",
        message="Cookie 文件可用",
        path=target_path,
        cookie_count=summary["cookie_count"],
        has_youtube=summary["has_youtube"],
        has_youtube_login=summary["has_youtube_login"],
        has_bilibili=summary["has_bilibili"],
        has_bilibili_login=summary["has_bilibili_login"],
        bilibili_cookie_names=summary["bilibili_cookie_names"],
        updated_at=updated_at,
        source_browser=source_browser,
    )


def export_browser_cookies(
    cookie_path: Path | None = None, browser: str | None = None
) -> dict:
    target_path = Path(cookie_path or COOKIE_FILE_PATH)
    _prepare_private_cookie_target(target_path)
    browser = normalize_cookie_browser(browser)
    browser_label = cookie_browser_label(browser)

    try:
        cookie_jar = _filtered_cookie_jar(
            _extract_browser_cookies_with_ytdlp(target_path, browser), target_path
        )
        cookies = list(cookie_jar)
        if not cookies:
            return _cookie_result(
                success=False,
                status="empty",
                status_code="browser_cookie_empty",
                message=(
                    f"未能从 {browser_label} 提取到目标站点 Cookie，"
                    "请确认该浏览器已登录 B站或 YouTube"
                ),
                path=target_path,
                updated_at=_now_text(),
                source_browser=browser,
            )

        summary = _cookie_summary(cookies)
        message = f"{browser_label} Cookie 导出完成"
        status_code = "export_ok"
        success = True
        status = "available"
        if not summary["has_youtube_login"] and not summary["has_bilibili_login"]:
            message = (
                f"{browser_label} Cookie 导出未通过登录校验，已保留原 Cookie 文件；"
                "请确认浏览器已登录 YouTube 或 B站"
            )
            status_code = "login_cookies_missing"
            success = False
            status = "failed"
        else:
            # Candidate validation is the write gate. A partial/anonymous
            # extraction must never replace a previously working cookie file.
            _write_cookie_jar(cookie_jar, target_path, browser)
        return _cookie_result(
            success=success,
            status=status,
            status_code=status_code,
            message=message,
            path=target_path,
            cookie_count=summary["cookie_count"],
            has_youtube=summary["has_youtube"],
            has_youtube_login=summary["has_youtube_login"],
            has_bilibili=summary["has_bilibili"],
            has_bilibili_login=summary["has_bilibili_login"],
            bilibili_cookie_names=summary["bilibili_cookie_names"],
            updated_at=_now_text(),
            source_browser=browser,
        )
    except Exception as exc:
        status_code, message = _describe_export_error(exc, browser)
        needs_full_disk_access = (
            browser == "safari" and status_code == "permission_denied"
        )
        return _cookie_result(
            success=False,
            status="failed",
            status_code=status_code,
            message=message,
            path=target_path,
            updated_at=_now_text(),
            source_browser=browser,
            needs_elevation_hint=needs_full_disk_access,
        )


def export_edge_cookies(cookie_path: Path | None = None) -> dict:
    return export_browser_cookies(cookie_path, "Edge")


def verify_cookie_file(cookie_path: Path | None = None) -> dict:
    target_path = Path(cookie_path or COOKIE_FILE_PATH)
    source_browser = (
        _read_cookie_source_browser(target_path) if target_path.exists() else None
    )
    if not target_path.exists():
        return _cookie_result(
            success=False,
            status="missing",
            status_code="file_missing",
            message="cookies.txt 不存在",
            path=target_path,
            source_browser=source_browser,
        )

    try:
        return harden_cookie_file(target_path)
    except Exception as exc:
        return _cookie_result(
            success=False,
            status="invalid",
            status_code="verify_invalid",
            message=f"Cookie 文件解析失败：{exc}",
            path=target_path,
            source_browser=source_browser,
        )
