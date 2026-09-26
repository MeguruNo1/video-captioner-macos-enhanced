import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QBoxLayout
from qfluentwidgets import CheckBox

from app.common.config import cfg
from app.view.download_center_interface import DownloadCenterInterface


class DownloadCenterLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.settings_file_patch = patch.object(
            cfg._cfg,
            "file",
            Path(cls.temp_dir.name) / "settings.json",
        )
        cls.download_state_patch = patch.object(
            DownloadCenterInterface,
            "DOWNLOAD_STATE_PATH",
            Path(cls.temp_dir.name) / "download_center_state.json",
        )
        cls.settings_file_patch.start()
        cls.download_state_patch.start()
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.download_state_patch.stop()
        cls.settings_file_patch.stop()
        cls.temp_dir.cleanup()

    def test_professional_download_requires_streams_but_muxed_video_needs_no_extra_audio(self):
        interface = DownloadCenterInterface()
        interface.url_input.setText("https://example.test/video")
        interface.parsed_url = interface.url_input.text()
        interface.preview_data = {"title": "Test", "video_formats": [], "audio_formats": []}
        interface.current_mode_key = "professional"
        interface.professional_mode_combo.setCurrentIndex(0)
        interface.selected_video_format = None
        interface.selected_audio_format = None
        interface._refresh_start_button_state()
        self.assertFalse(interface.start_button.isEnabled())
        self.assertIn("视频流", interface.start_button.text())
        interface.selected_video_format = {"format_id": "v", "has_audio": False}
        interface._refresh_start_button_state()
        self.assertFalse(interface.start_button.isEnabled())
        self.assertIn("音频流", interface.start_button.text())
        interface.selected_video_format["has_audio"] = True
        interface._refresh_start_button_state()
        self.assertTrue(interface.start_button.isEnabled())
        interface.deleteLater()

    def test_restored_request_resumes_on_first_click_without_live_worker(self):
        interface = DownloadCenterInterface()
        interface._pending_download_request = {"need_video": True}
        interface.download_action_state = "idle"
        interface.download_thread = None
        interface._refresh_start_button_state()
        self.assertEqual(interface.start_button.text(), "继续下载")
        self.assertTrue(interface.start_button.isEnabled())
        with patch.object(interface, "start_download") as start:
            interface._on_start_button_clicked()
            start.assert_called_once()
        interface.deleteLater()

    def test_missing_subtitles_still_allows_explicit_extra_downloads(self):
        interface = DownloadCenterInterface()
        interface.url_input.setText("https://example.test/video")
        interface.parsed_url = interface.url_input.text()
        interface.preview_data = {"title": "Test"}
        interface.current_mode_key = "simple"
        index = next(i for i in range(interface.simple_preset_combo.count())
                     if interface.simple_preset_combo.itemData(i) == "subtitle_only")
        interface.simple_preset_combo.setCurrentIndex(index)
        for box in (interface.thumbnail_checkbox, interface.metadata_checkbox, interface.description_txt_checkbox):
            box.setChecked(False)
        with patch.object(interface, "_has_available_subtitle_choice", return_value=False):
            self.assertTrue(interface._download_readiness_issue())
            interface.thumbnail_checkbox.setChecked(True)
            self.assertEqual(interface._download_readiness_issue(), "")
        interface.deleteLater()

    def test_mode_panel_hides_inactive_page_instead_of_reserving_its_height(self):
        interface = DownloadCenterInterface()
        interface.resize(1120, 680)
        interface.show()
        self.app.processEvents()

        with patch.object(interface, "_save_download_preferences"):
            interface._switch_download_mode("simple")
            self.app.processEvents()
            self.assertFalse(interface.simple_panel.isHidden())
            self.assertTrue(interface.professional_panel.isHidden())
            self.assertLess(interface.mode_panel_container.height(), 220)

            interface._switch_download_mode("professional")
            self.app.processEvents()
            self.assertTrue(interface.simple_panel.isHidden())
            self.assertFalse(interface.professional_panel.isHidden())
        interface.deleteLater()

    def test_compact_layout_stacks_preview_and_wraps_download_options(self):
        interface = DownloadCenterInterface()
        interface.resize(700, 760)
        interface.show()
        interface._set_preview_visible(True)
        self.app.processEvents()

        self.assertEqual(
            interface.preview_card_layout.direction(), QBoxLayout.TopToBottom
        )
        compact_positions = [
            interface.download_options_grid.getItemPosition(
                interface.download_options_grid.indexOf(checkbox)
            )[:2]
            for checkbox in interface.download_option_checkboxes
        ]
        self.assertEqual(compact_positions, [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertEqual(interface.output_dir_layout.direction(), QBoxLayout.TopToBottom)
        compact_custom_positions = [
            (
                interface.custom_preferences_grid.getItemPosition(
                    interface.custom_preferences_grid.indexOf(label)
                )[:2],
                interface.custom_preferences_grid.getItemPosition(
                    interface.custom_preferences_grid.indexOf(combo)
                )[:2],
            )
            for label, combo in interface.custom_preference_fields
        ]
        self.assertEqual(
            compact_custom_positions,
            [((0, 0), (0, 1)), ((1, 0), (1, 1)), ((2, 0), (2, 1))],
        )

        interface.resize(1000, 760)
        self.app.processEvents()
        self.assertEqual(
            interface.preview_card_layout.direction(), QBoxLayout.LeftToRight
        )
        wide_positions = [
            interface.download_options_grid.getItemPosition(
                interface.download_options_grid.indexOf(checkbox)
            )[:2]
            for checkbox in interface.download_option_checkboxes
        ]
        self.assertEqual(wide_positions, [(0, 0), (0, 1), (0, 2), (0, 3)])
        self.assertEqual(interface.output_dir_layout.direction(), QBoxLayout.LeftToRight)
        wide_custom_positions = [
            (
                interface.custom_preferences_grid.getItemPosition(
                    interface.custom_preferences_grid.indexOf(label)
                )[:2],
                interface.custom_preferences_grid.getItemPosition(
                    interface.custom_preferences_grid.indexOf(combo)
                )[:2],
            )
            for label, combo in interface.custom_preference_fields
        ]
        self.assertEqual(
            wide_custom_positions,
            [((0, 0), (0, 1)), ((0, 2), (0, 3)), ((0, 4), (0, 5))],
        )
        interface.deleteLater()

    def test_download_options_use_fluent_checkboxes_with_larger_hit_targets(self):
        interface = DownloadCenterInterface()

        self.assertEqual(len(interface.download_checkboxes), 9)
        for checkbox in interface.download_checkboxes:
            self.assertIsInstance(checkbox, CheckBox)
            self.assertGreaterEqual(checkbox.minimumHeight(), 28)
        interface.deleteLater()

    def test_preview_summarizes_large_subtitle_language_lists(self):
        interface = DownloadCenterInterface()
        preview = {
            "title": "Example",
            "uploader": "Uploader",
            "duration_text": "01:00",
            "manual_subtitle_languages": [],
            "auto_subtitle_languages": [f"lang-{index}" for index in range(20)],
        }

        interface._render_preview_card(preview)

        self.assertIn("自动 20 种", interface.preview_subtitle_label.text())
        self.assertIn("lang-4…", interface.preview_subtitle_label.text())
        self.assertNotIn("lang-19", interface.preview_subtitle_label.text())
        self.assertIn("lang-19", interface.preview_subtitle_label.toolTip())
        interface.deleteLater()

    def test_time_range_progress_switches_between_indeterminate_and_determinate(self):
        interface = DownloadCenterInterface()

        interface._render_download_detail_panel(
            {
                "phase": "locating",
                "indeterminate": True,
                "percent": "",
                "elapsed": "00:07",
                "section_index": 1,
                "section_count": 2,
                "status": "正在定位片段起点",
            }
        )
        self.assertIs(interface.progress_stack.currentWidget(), interface.indeterminate_progress_bar)
        self.assertEqual(interface.status_label.text(), "正在定位片段起点")
        self.assertNotIn("进度：0", interface.download_detail_panel.text())

        interface._render_download_detail_panel(
            {
                "phase": "processing",
                "indeterminate": False,
                "percent": "42.5",
                "speed": "1.3x",
                "eta": "01:10",
                "elapsed": "00:12",
                "section_index": 1,
                "section_count": 2,
                "status": "正在下载并生成片段",
            }
        )
        self.assertIs(interface.progress_stack.currentWidget(), interface.progress_bar)
        self.assertEqual(interface.progress_bar.value(), 42)
        self.assertIn("进度：42.5%", interface.download_detail_panel.text())
        interface.deleteLater()

    def test_paused_download_shows_resume_and_adjacent_terminate_actions(self):
        interface = DownloadCenterInterface()
        interface.download_thread = MagicMock()
        interface.download_thread.isRunning.return_value = True
        interface._set_download_action_state("downloading")

        interface._on_start_button_clicked()

        interface.download_thread.request_pause.assert_called_once_with()
        self.assertEqual(interface.start_button.text(), "继续下载")
        self.assertEqual(interface.terminate_button.text(), "终止下载")
        self.assertFalse(interface.terminate_button.isHidden())

        interface._on_start_button_clicked()

        interface.download_thread.request_resume.assert_called_once_with()
        self.assertEqual(interface.start_button.text(), "暂停下载")
        self.assertTrue(interface.terminate_button.isHidden())

        interface._set_download_action_state("paused")
        interface._on_terminate_button_clicked()

        interface.download_thread.request_terminate.assert_called_once_with()
        self.assertEqual(interface.download_action_state, "terminating")
        self.assertEqual(interface.terminate_button.text(), "终止中…")
        self.assertFalse(interface.start_button.isEnabled())
        interface.deleteLater()

    def test_restores_preview_and_interrupted_download_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "download_center_state.json"
            with patch.object(DownloadCenterInterface, "DOWNLOAD_STATE_PATH", state_path):
                interface = DownloadCenterInterface()
                interface.parsed_url = "https://example.test/video"
                interface.preview_data = {
                    "url": interface.parsed_url,
                    "title": "Resume me",
                    "uploader": "Tester",
                    "duration_text": "01:00",
                    "upload_date": "",
                    "view_count_text": "",
                    "thumbnail_bytes": b"thumbnail",
                    "video_formats": [],
                    "audio_formats": [],
                    "manual_subtitle_languages": [],
                    "auto_subtitle_languages": [],
                    "has_manual_subtitles": False,
                    "has_auto_subtitles": False,
                    "info_dict": {"duration": 60},
                }
                interface._pending_subtitle_mode = "auto"
                interface._pending_work_dir = temp_dir
                interface._save_download_state(
                    status="interrupted",
                    request={"need_video": True},
                    progress=37,
                    detail={"percent": "37", "status": "网络中断"},
                )
                interface.deleteLater()

                restored = DownloadCenterInterface()
                self.assertEqual(restored.parsed_url, "https://example.test/video")
                self.assertEqual(restored.preview_data["title"], "Resume me")
                self.assertEqual(restored.progress_bar.value(), 37)
                self.assertEqual(restored._pending_download_request, {"need_video": True})
                self.assertEqual(restored.start_button.text(), "继续下载")
                self.assertIn("恢复上次下载进度", restored.status_label.text())
                restored.deleteLater()

    def test_subtitle_choices_merge_source_and_language_with_group_separator(self):
        interface = DownloadCenterInterface()
        interface.preview_data = {
            "manual_subtitle_languages": ["zh-Hans"],
            "auto_subtitle_languages": ["ru", "en", "ja"],
        }
        from app.common.config import cfg

        original_get = cfg.get
        with patch.object(
            cfg,
            "get",
            side_effect=lambda item: (
                "en" if item is cfg.download_center_subtitle_language else original_get(item)
            ),
        ):
            interface._populate_subtitle_choices()

        self.assertEqual(interface.subtitle_source_combo.count(), 4)
        self.assertEqual(interface.subtitle_source_combo.separator_before_indices, {1})
        self.assertEqual(
            interface.subtitle_source_combo.itemData(0), ("manual", "zh-Hans")
        )
        self.assertIn("人工字幕", interface.subtitle_source_combo.itemText(0))
        self.assertEqual(interface.subtitle_source_combo.itemData(2), ("auto", "en"))
        self.assertIn("自动字幕", interface.subtitle_source_combo.itemText(2))
        interface.deleteLater()

    def test_auto_only_subtitles_hide_manual_group(self):
        interface = DownloadCenterInterface()
        interface.preview_data = {
            "manual_subtitle_languages": [],
            "auto_subtitle_languages": ["ru", "en", "ja"],
        }

        interface._populate_subtitle_choices()

        self.assertEqual(interface.subtitle_source_combo.count(), 3)
        self.assertEqual(interface.subtitle_source_combo.separator_before_indices, set())
        for index in range(interface.subtitle_source_combo.count()):
            self.assertEqual(interface.subtitle_source_combo.itemData(index)[0], "auto")
            self.assertNotIn("人工字幕", interface.subtitle_source_combo.itemText(index))
        self.assertEqual(interface._selected_subtitle_mode(), "auto")
        interface.deleteLater()

    def test_no_subtitles_disables_unified_subtitle_choice(self):
        interface = DownloadCenterInterface()
        interface.preview_data = {
            "manual_subtitle_languages": [],
            "auto_subtitle_languages": [],
        }

        interface._populate_subtitle_choices()

        self.assertEqual(interface.subtitle_source_combo.count(), 1)
        self.assertIsNone(interface.subtitle_source_combo.currentData())
        self.assertIn("未检测到", interface.subtitle_source_combo.currentText())
        self.assertFalse(interface.subtitle_source_combo.isEnabled())
        interface.deleteLater()


if __name__ == "__main__":
    unittest.main()
