import unittest
from unittest.mock import patch

from app.common.signal_bus import signalBus
from app.core.utils import desktop_notification


class DesktopNotificationTests(unittest.TestCase):
    def setUp(self):
        desktop_notification._reset_notification_state_for_tests()

    def tearDown(self):
        desktop_notification._reset_notification_state_for_tests()

    def test_notification_disabled_skips_all_senders(self):
        with patch.object(desktop_notification.cfg, "get", return_value=False), \
            patch.object(desktop_notification, "_send_macos_native_notification") as native, \
            patch.object(desktop_notification, "_send_qt_tray_notification") as qt:
            result = desktop_notification.send_desktop_notification(
                "title", "message", target="download_center"
            )

        self.assertFalse(result)
        native.assert_not_called()
        qt.assert_not_called()

    def test_macos_native_sender_is_preferred(self):
        with patch.object(desktop_notification.cfg, "get", return_value=True), \
            patch.object(
                desktop_notification,
                "_send_macos_native_notification",
                return_value=True,
            ) as native, \
            patch.object(desktop_notification, "_send_qt_tray_notification") as qt:
            result = desktop_notification.send_desktop_notification(
                "title", "message", target="download_center"
            )

        self.assertTrue(result)
        native.assert_called_once_with("title", "message", 5000, "download_center")
        qt.assert_not_called()

    def test_macos_native_failure_falls_back_to_qt(self):
        with patch.object(desktop_notification.cfg, "get", return_value=True), \
            patch.object(
                desktop_notification,
                "_send_macos_native_notification",
                return_value=False,
            ) as native, \
            patch.object(
                desktop_notification,
                "_send_qt_tray_notification",
                return_value=True,
            ) as qt:
            result = desktop_notification.send_desktop_notification(
                "title", "message", target="subtitle"
            )

        self.assertTrue(result)
        native.assert_called_once_with("title", "message", 5000, "subtitle")
        qt.assert_called_once_with("title", "message", 5000, "subtitle")

    def test_macos_native_exception_falls_back_to_qt(self):
        with patch.object(desktop_notification.cfg, "get", return_value=True), \
            patch.object(
                desktop_notification,
                "_send_macos_native_notification",
                side_effect=RuntimeError("boom"),
            ), \
            patch.object(
                desktop_notification,
                "_send_qt_tray_notification",
                return_value=True,
            ) as qt:
            result = desktop_notification.send_desktop_notification(
                "title", "message", target="transcription"
            )

        self.assertTrue(result)
        qt.assert_called_once_with("title", "message", 5000, "transcription")

    def test_notification_click_emits_target(self):
        received = []

        def collect(target):
            received.append(target)

        signalBus.notification_clicked.connect(collect)
        try:
            desktop_notification._emit_notification_click("download_center")
        finally:
            signalBus.notification_clicked.disconnect(collect)

        self.assertEqual(received, ["download_center"])

    def test_request_authorization_returns_false_without_native_center(self):
        with patch.object(
            desktop_notification,
            "_ensure_mac_notification_center",
            return_value=None,
        ):
            self.assertFalse(
                desktop_notification.request_desktop_notification_authorization()
            )

    def test_non_macos_authorization_without_qapplication_is_safe(self):
        with patch.object(desktop_notification, "IS_MACOS", False), patch.object(
            desktop_notification.QApplication,
            "instance",
            return_value=None,
        ), patch.object(
            desktop_notification.QSystemTrayIcon,
            "isSystemTrayAvailable",
        ) as tray_available:
            self.assertFalse(
                desktop_notification.request_desktop_notification_authorization()
            )

        tray_available.assert_not_called()

    def test_status_reports_disabled_when_config_is_off(self):
        with patch.object(desktop_notification.cfg, "get", return_value=False):
            status = desktop_notification.get_desktop_notification_status()

        self.assertEqual(status["status"], "disabled")

    def test_macos_native_sender_requires_authorization(self):
        with patch.object(
            desktop_notification,
            "_ensure_mac_notification_center",
            return_value=object(),
        ), patch.object(
            desktop_notification,
            "_request_mac_notification_authorization",
            return_value=False,
        ):
            result = desktop_notification._send_macos_native_notification(
                "title", "message", target="download_center"
            )

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
