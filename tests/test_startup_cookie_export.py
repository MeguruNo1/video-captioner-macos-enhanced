import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from app.common.config import cfg
from app.thread.browser_cookie_export_thread import BrowserCookieExportThread
from app.view.download_center_interface import DownloadCenterInterface
from app.view.main_window import MainWindow
from app.view.setting_interface import SettingInterface


class StartupCookieExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _window_harness():
        return SimpleNamespace(
            _closing=False,
            _startup_cookie_export_attempted=False,
            startupCookieExportThread=None,
            settingInterface=SimpleNamespace(
                set_cookie_export_in_progress=Mock()
            ),
            _on_startup_cookie_export_completed=Mock(),
            _release_startup_cookie_export_thread=Mock(),
        )

    def test_disabled_setting_does_not_start_cookie_export(self):
        window = self._window_harness()

        with patch.object(cfg, "get", return_value=False), patch(
            "app.view.main_window.BrowserCookieExportThread"
        ) as thread_class:
            started = MainWindow._start_cookie_extraction_on_startup(window)

        self.assertFalse(started)
        self.assertTrue(window._startup_cookie_export_attempted)
        thread_class.assert_not_called()

    def test_enabled_setting_starts_once_with_selected_browser(self):
        window = self._window_harness()

        def config_value(item):
            if item is cfg.download_auto_extract_cookies_on_startup:
                return True
            if item is cfg.download_cookie_browser:
                return "Chrome"
            raise AssertionError(f"unexpected config item: {item.key}")

        with patch.object(cfg, "get", side_effect=config_value), patch(
            "app.view.main_window.BrowserCookieExportThread"
        ) as thread_class:
            thread = thread_class.return_value

            self.assertTrue(
                MainWindow._start_cookie_extraction_on_startup(window)
            )
            self.assertFalse(
                MainWindow._start_cookie_extraction_on_startup(window)
            )

        thread_class.assert_called_once_with("Chrome", window)
        thread.completed.connect.assert_called_once_with(
            window._on_startup_cookie_export_completed
        )
        thread.finished.connect.assert_called_once_with(
            window._release_startup_cookie_export_thread
        )
        window.settingInterface.set_cookie_export_in_progress.assert_called_once_with(
            True
        )
        thread.start.assert_called_once_with()
        self.assertIs(window.startupCookieExportThread, thread)

    def test_worker_failure_is_reported_without_raising(self):
        thread = BrowserCookieExportThread("Safari")
        results = []
        thread.completed.connect(results.append)

        with patch(
            "app.core.utils.edge_cookie_utils.export_browser_cookies",
            side_effect=PermissionError("denied"),
        ) as export:
            thread.run()

        export.assert_called_once_with(browser="Safari")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertIn("denied", results[0]["message"])

        setting_interface = SimpleNamespace(refresh_cookie_status=Mock())
        window = SimpleNamespace(
            _closing=False,
            settingInterface=setting_interface,
            tr=lambda text: text,
        )
        with patch("app.view.main_window.InfoBar.warning") as warning:
            MainWindow._on_startup_cookie_export_completed(window, results[0])

        setting_interface.refresh_cookie_status.assert_called_once_with(results[0])
        warning.assert_called_once()

    def test_close_waits_for_running_export_before_quitting(self):
        thread = Mock()
        thread.isRunning.return_value = True
        event = Mock()
        window = SimpleNamespace(
            _closing=False,
            startupCookieExportThread=thread,
            hide=Mock(),
            settingInterface=SimpleNamespace(componentUpdateThread=None, componentUpdateTimer=Mock()),
        )

        MainWindow.closeEvent(window, event)

        self.assertTrue(window._closing)
        event.ignore.assert_called_once_with()
        window.hide.assert_called_once_with()

    def test_close_does_not_destroy_running_component_update_thread(self):
        updater = Mock()
        updater.isRunning.return_value = True
        event = Mock()
        window = SimpleNamespace(
            settingInterface=SimpleNamespace(componentUpdateThread=updater),
            tr=lambda text: text,
        )
        with patch("app.view.main_window.InfoBar.warning") as warning:
            MainWindow.closeEvent(cast(MainWindow, window), event)
        event.ignore.assert_called_once_with()
        warning.assert_called_once()

    def test_thread_release_reenables_controls_and_resumes_close(self):
        thread = Mock()
        setting_interface = SimpleNamespace(
            set_cookie_export_in_progress=Mock()
        )
        window = SimpleNamespace(
            _closing=True,
            startupCookieExportThread=thread,
            settingInterface=setting_interface,
            close=Mock(),
        )

        with patch("app.view.main_window.QTimer.singleShot") as single_shot:
            MainWindow._release_startup_cookie_export_thread(window)

        self.assertIsNone(window.startupCookieExportThread)
        thread.deleteLater.assert_called_once_with()
        setting_interface.set_cookie_export_in_progress.assert_called_once_with(
            False
        )
        single_shot.assert_called_once_with(0, window.close)

    def test_shutdown_waits_for_running_cookie_export(self):
        thread = Mock()
        thread.isRunning.return_value = True
        process = Mock()
        process.children.return_value = []
        window = SimpleNamespace(startupCookieExportThread=thread,
                                 settingInterface=SimpleNamespace(componentUpdateThread=None))

        with patch("app.view.main_window.psutil.Process", return_value=process):
            MainWindow.stop(window)

        thread.wait.assert_called_once_with()

    def test_setting_uses_startup_semantics_and_keeps_legacy_storage_key(self):
        self.assertEqual(
            cfg.download_auto_extract_cookies_on_startup.key,
            "Download.AutoRefreshEdgeCookies",
        )
        self.assertFalse(
            cfg.download_auto_extract_cookies_on_startup.defaultValue
        )

        cookie_status = {
            "success": False,
            "status": "missing",
            "message": "cookies.txt 不存在",
            "source_browser_label": "未知",
        }
        with patch(
            "app.core.utils.edge_cookie_utils.verify_cookie_file",
            return_value=cookie_status,
        ):
            interface = SettingInterface()
        card = interface.downloadAutoExtractCookiesOnStartupCard
        self.assertEqual(
            card.titleLabel.text(), "应用启动时自动提取浏览器 Cookie"
        )
        self.assertIn("下次启动应用时", card.contentLabel.text())
        self.assertNotIn(
            "刷新 cookies.txt",
            interface.downloadCookieBrowserCard.contentLabel.text(),
        )
        interface.set_cookie_export_in_progress(True)
        self.assertFalse(interface.edgeCookieExportCard.button.isEnabled())
        self.assertFalse(interface.edgeCookieStatusCard.button.isEnabled())
        self.assertFalse(interface.downloadCookieBrowserCard.comboBox.isEnabled())
        with patch(
            "app.core.utils.edge_cookie_utils.verify_cookie_file"
        ) as verify_cookie_file:
            interface.refresh_cookie_status()
        verify_cookie_file.assert_not_called()
        interface.set_cookie_export_in_progress(False)
        self.assertTrue(interface.edgeCookieExportCard.button.isEnabled())
        self.assertTrue(interface.edgeCookieStatusCard.button.isEnabled())
        self.assertTrue(interface.downloadCookieBrowserCard.comboBox.isEnabled())
        interface.deleteLater()
        self.app.processEvents()

    def test_link_parsing_no_longer_extracts_cookies(self):
        original_get = cfg.get

        def config_value(item):
            if item is cfg.download_auto_extract_cookies_on_startup:
                return True
            return original_get(item)

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            DownloadCenterInterface,
            "DOWNLOAD_STATE_PATH",
            Path(temp_dir) / "download_state.json",
        ), patch(
            "app.view.download_center_interface.APP_DATA_PATH",
            Path(temp_dir),
        ):
            interface = DownloadCenterInterface()
            interface.url_input.setText("https://example.com/video")

            with patch(
                "app.core.utils.edge_cookie_utils.export_browser_cookies"
            ) as export, patch(
                "app.thread.video_download_thread.VideoPreviewThread"
            ) as preview_thread, patch.object(
                cfg, "get", side_effect=config_value
            ):
                interface.parse_link()

        export.assert_not_called()
        preview_thread.return_value.start.assert_called_once_with()
        interface.deleteLater()
        self.app.processEvents()

    def test_link_parsing_startup_failure_restores_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            DownloadCenterInterface,
            "DOWNLOAD_STATE_PATH",
            Path(temp_dir) / "download_state.json",
        ), patch(
            "app.view.download_center_interface.APP_DATA_PATH",
            Path(temp_dir),
        ):
            interface = DownloadCenterInterface()
            interface.url_input.setText("https://example.com/video")

            with patch(
                "app.thread.video_download_thread.VideoPreviewThread",
                side_effect=FileNotFoundError("missing packaged module"),
            ), patch(
                "app.view.download_center_interface.InfoBar.warning"
            ), patch(
                "app.view.download_center_interface.InfoBar.error"
            ) as error:
                interface.parse_link()

        self.assertIsNone(interface.preview_thread)
        self.assertTrue(interface.url_input.isEnabled())
        self.assertTrue(interface.parse_button.isEnabled())
        self.assertEqual(interface.status_label.text(), "解析失败")
        error.assert_called_once()
        self.assertIn("missing packaged module", error.call_args.args[1])
        interface.deleteLater()
        self.app.processEvents()

    def test_starting_download_no_longer_extracts_cookies(self):
        original_get = cfg.get

        def config_value(item):
            if item is cfg.download_auto_extract_cookies_on_startup:
                return True
            return original_get(item)

        request = {
            "need_video": True,
            "need_subtitle": False,
            "need_thumbnail": False,
            "need_metadata": False,
            "need_description_txt": False,
            "need_transcript_txt": False,
            "download_mode": "video_audio",
            "selected_video_format_id": None,
            "selected_audio_format_id": None,
            "format_selector": "best",
            "enable_time_ranges": False,
            "download_sections": [],
            "pr_smart_transcode_hevc_on_av1": False,
            "ensure_mp4_output": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            DownloadCenterInterface,
            "DOWNLOAD_STATE_PATH",
            Path(temp_dir) / "download_state.json",
        ):
            interface = DownloadCenterInterface()
            interface.url_input.setText("https://example.com/video")
            interface.preview_data = {"title": "Example"}
            interface.parsed_url = interface.url_input.text()
            interface._pending_download_request = request
            interface._pending_subtitle_mode = "auto"
            interface._pending_work_dir = temp_dir

            with patch.object(interface, "_save_download_state"), patch(
                "app.core.utils.edge_cookie_utils.export_browser_cookies"
            ) as export, patch(
                "app.thread.video_download_thread.VideoDownloadThread"
            ) as download_thread, patch.object(
                cfg, "get", side_effect=config_value
            ):
                interface.start_download()

        export.assert_not_called()
        download_thread.return_value.start.assert_called_once_with()
        interface.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
