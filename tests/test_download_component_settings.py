import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication

from app.common.config import cfg
from app.thread.download_component_update_thread import DownloadComponentUpdateThread
from app.view.setting_interface import SettingInterface

_APP = QApplication.instance() or QApplication([])


def test_schedule_uses_mode_and_does_not_repeat_while_busy():
    page = SettingInterface()
    def value(item):
        return "每天" if item is cfg.download_component_frequency else "自动更新"
    with patch.object(cfg, "get", side_effect=value), patch(
        "app.view.setting_interface.check_due", return_value=True
    ), patch.object(page, "_start_component_action") as start:
        page._scheduled_component_check()
        start.assert_called_once_with("auto", scheduled=True)
        page.componentUpdateThread = DownloadComponentUpdateThread("check", "", page)
        page._scheduled_component_check()
        assert start.call_count == 1
    page.componentUpdateThread = None
    page.close()


def test_auto_action_only_installs_when_check_reports_update():
    results = []
    thread = DownloadComponentUpdateThread("auto", "")
    thread.completed.connect(results.append)
    with patch("app.thread.download_component_update_thread.check_updates",
               return_value={"available": False, "message": "current"}), patch(
               "app.thread.download_component_update_thread.install_updates") as install:
        thread.run()
        install.assert_not_called()
    assert results == [{"success": True, "available": False, "message": "current"}]


def test_worker_failure_is_reported_to_ui():
    results = []
    thread = DownloadComponentUpdateThread("install", "")
    thread.completed.connect(results.append)
    with patch("app.thread.download_component_update_thread.install_updates",
               side_effect=RuntimeError("incompatible")):
        thread.run()
    assert results == [{"success": False, "message": "incompatible"}]
