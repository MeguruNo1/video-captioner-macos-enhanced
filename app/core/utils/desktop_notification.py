from __future__ import annotations

from threading import Event
import uuid

from app.core.utils.platform_utils import IS_MACOS

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon

from app.common.config import cfg
from app.common.signal_bus import signalBus
from app.config import APP_NAME, RESOURCE_PATH
from app.core.utils.logger import setup_logger

logger = setup_logger("desktop_notification")

_tray_icon = None
_last_notification_target = None
_mac_notification_center = None
_mac_notification_delegate = None
_mac_authorization_requested = False
_mac_authorized = None
_mac_authorization_event = None


def _notification_icon() -> QIcon:
    app = QApplication.instance()
    if app is not None:
        app_icon = app.windowIcon()
        if not app_icon.isNull():
            return app_icon
    return QIcon(str(RESOURCE_PATH / "assets" / "logo.png"))


def _on_message_clicked():
    _emit_notification_click(_last_notification_target)


def _ensure_mac_notification_center():
    global _mac_notification_center, _mac_notification_delegate
    if not IS_MACOS:
        return None
    if _mac_notification_center is not None:
        return _mac_notification_center

    try:
        from Foundation import NSObject
        from UserNotifications import UNUserNotificationCenter
    except Exception as exc:
        logger.warning("macOS 原生通知不可用: %s", exc)
        return None

    class MacNotificationDelegate(NSObject):
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self, _center, _notification, completion_handler
        ):
            try:
                from UserNotifications import (
                    UNNotificationPresentationOptionBanner,
                    UNNotificationPresentationOptionList,
                    UNNotificationPresentationOptionSound,
                )

                options = (
                    UNNotificationPresentationOptionBanner
                    | UNNotificationPresentationOptionList
                    | UNNotificationPresentationOptionSound
                )
            except Exception:
                options = 0
            completion_handler(options)

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self, _center, response, completion_handler
        ):
            try:
                user_info = (
                    response.notification()
                    .request()
                    .content()
                    .userInfo()
                )
                target = str(user_info.get("target", "") or "").strip()
                _emit_notification_click(target)
            except Exception as exc:
                logger.warning("处理 macOS 通知点击失败: %s", exc)
            finally:
                completion_handler()

    _mac_notification_center = UNUserNotificationCenter.currentNotificationCenter()
    _mac_notification_delegate = MacNotificationDelegate.alloc().init()
    _mac_notification_center.setDelegate_(_mac_notification_delegate)
    return _mac_notification_center


def _request_mac_notification_authorization(center, wait: bool = False) -> bool | None:
    global _mac_authorization_requested, _mac_authorization_event
    if _mac_authorization_requested:
        if wait and _mac_authorization_event is not None:
            _mac_authorization_event.wait(4)
        return _mac_authorized

    try:
        from UserNotifications import (
            UNAuthorizationOptionAlert,
            UNAuthorizationOptionSound,
        )

        _mac_authorization_event = Event()

        def _completion(granted, error):
            global _mac_authorized
            _mac_authorized = bool(granted)
            if error:
                logger.warning("macOS 通知授权失败: %s", error)
            if _mac_authorization_event is not None:
                _mac_authorization_event.set()

        _mac_authorization_requested = True
        center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
            _completion,
        )
        if wait and _mac_authorization_event is not None:
            _mac_authorization_event.wait(4)
        return _mac_authorized
    except Exception as exc:
        logger.warning("请求 macOS 通知授权失败: %s", exc)
        return None


def request_desktop_notification_authorization() -> bool:
    if not IS_MACOS:
        if QApplication.instance() is None:
            return False
        return QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages()
    center = _ensure_mac_notification_center()
    if center is None:
        return False
    return bool(_request_mac_notification_authorization(center, wait=True))


def get_desktop_notification_status() -> dict:
    if not bool(cfg.get(cfg.desktop_notifications_enabled)):
        return {"status": "disabled", "message": "应用内通知开关已关闭"}

    if not IS_MACOS:
        available = request_desktop_notification_authorization()
        return {
            "status": "authorized" if available else "unavailable",
            "message": "使用系统托盘通知" if available else "系统托盘通知不可用",
        }
    center = _ensure_mac_notification_center()
    if center is None:
        return {"status": "unavailable", "message": "macOS 原生通知不可用"}

    try:
        from UserNotifications import (
            UNAuthorizationStatusAuthorized,
            UNAuthorizationStatusDenied,
            UNAuthorizationStatusNotDetermined,
            UNAuthorizationStatusProvisional,
        )

        done = Event()
        result = {"status": "unknown", "message": "无法读取通知权限状态"}

        def _completion(settings):
            authorization_status = settings.authorizationStatus()
            if authorization_status == UNAuthorizationStatusAuthorized:
                result.update({"status": "authorized", "message": "系统通知权限已开启"})
            elif authorization_status == UNAuthorizationStatusDenied:
                result.update({
                    "status": "denied",
                    "message": "系统通知权限已关闭，请在 macOS 系统设置中开启",
                })
            elif authorization_status == UNAuthorizationStatusNotDetermined:
                result.update({"status": "not_determined", "message": "尚未请求系统通知权限"})
            elif authorization_status == UNAuthorizationStatusProvisional:
                result.update({"status": "provisional", "message": "系统通知为临时授权状态"})
            done.set()

        center.getNotificationSettingsWithCompletionHandler_(_completion)
        done.wait(2)
        return result
    except Exception as exc:
        logger.warning("读取 macOS 通知权限状态失败: %s", exc)
        return {"status": "unknown", "message": "读取通知权限状态失败"}


def _send_macos_native_notification(
    title: str,
    message: str,
    timeout_ms: int = 5000,
    target: str | None = None,
) -> bool:
    del timeout_ms
    center = _ensure_mac_notification_center()
    if center is None:
        return False
    if _mac_authorized is False:
        return False

    try:
        from Foundation import NSDictionary
        from UserNotifications import (
            UNMutableNotificationContent,
            UNNotificationRequest,
        )

        authorization = _request_mac_notification_authorization(center, wait=True)
        if authorization is not True:
            return False

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(str(title or APP_NAME))
        content.setBody_(str(message or ""))
        content.setSound_(None)

        normalized_target = str(target or "").strip()
        if normalized_target:
            content.setUserInfo_(NSDictionary.dictionaryWithDictionary_({
                "target": normalized_target
            }))

        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            f"videocaptioner-{uuid.uuid4()}",
            content,
            None,
        )

        def _completion(error):
            if error:
                logger.warning("发送 macOS 原生通知失败: %s", error)

        center.addNotificationRequest_withCompletionHandler_(request, _completion)
        return True
    except Exception as exc:
        logger.warning("发送 macOS 原生通知失败: %s", exc)
        return False


def _send_qt_tray_notification(
    title: str,
    message: str,
    timeout_ms: int = 5000,
    target: str | None = None,
) -> bool:
    app = QApplication.instance()
    if app is None:
        return False

    try:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False

        global _tray_icon, _last_notification_target
        if _tray_icon is None:
            _tray_icon = QSystemTrayIcon(_notification_icon(), app)
            _tray_icon.setToolTip(APP_NAME)
            _tray_icon.messageClicked.connect(_on_message_clicked)

        if not _tray_icon.isVisible():
            _tray_icon.show()

        _last_notification_target = str(target).strip() if target else None
        _tray_icon.showMessage(
            title,
            message,
            QSystemTrayIcon.Information,
            timeout_ms,
        )
        return True
    except Exception as exc:
        logger.warning("发送 Qt 桌面通知失败: %s", exc)
        return False


def send_desktop_notification(
    title: str,
    message: str,
    timeout_ms: int = 5000,
    target: str | None = None,
) -> bool:
    if not bool(cfg.get(cfg.desktop_notifications_enabled)):
        return False

    try:
        if _send_macos_native_notification(title, message, timeout_ms, target):
            return True
        return _send_qt_tray_notification(title, message, timeout_ms, target)
    except Exception as exc:
        logger.warning("发送桌面通知失败: %s", exc)
        return _send_qt_tray_notification(title, message, timeout_ms, target)


def _emit_notification_click(target: str | None) -> None:
    normalized_target = str(target or "").strip()
    if normalized_target:
        signalBus.notification_clicked.emit(normalized_target)


def _reset_notification_state_for_tests() -> None:
    global _tray_icon, _last_notification_target
    global _mac_notification_center, _mac_notification_delegate
    global _mac_authorization_requested, _mac_authorized, _mac_authorization_event
    _tray_icon = None
    _last_notification_target = None
    _mac_notification_center = None
    _mac_notification_delegate = None
    _mac_authorization_requested = False
    _mac_authorized = None
    _mac_authorization_event = None
