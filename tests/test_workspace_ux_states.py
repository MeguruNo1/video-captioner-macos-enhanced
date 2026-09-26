import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QApplication

from app.view.subtitle_interface import SubtitleInterface
from app.view.transcription_interface import TranscriptionInterface


class WorkspaceUxStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for page, field in ((TranscriptionInterface, "TRANSCRIPTION_STATE_PATH"),
                            (SubtitleInterface, "SUBTITLE_STATE_PATH")):
            patcher = patch.object(page, field, Path(temporary.name) / (field + ".json"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete))

    def test_transcription_handoff_waits_for_generated_subtitles(self):
        interface = TranscriptionInterface()

        self.assertFalse(interface.send_to_translate_action.isEnabled())
        self.assertIn("请先完成转录", interface.send_to_translate_action.toolTip())

        model_combo = (
            interface.transcription_setting_card.mlx_whisper_widget.model_card.comboBox
        )
        self.assertEqual(model_combo.cursorPosition(), 0)
        self.assertEqual(model_combo.toolTip(), model_combo.text())
        interface.deleteLater()

    def test_subtitle_actions_follow_the_loaded_file_state(self):
        interface = SubtitleInterface()

        self.assertFalse(interface.start_button.isEnabled())
        self.assertFalse(interface.save_button.isEnabled())
        self.assertIn("先打开或拖入", interface.log_text.placeholderText())

        fixture = Path(__file__).parent / "fixtures" / "transcript.srt"
        interface.load_subtitle_file(str(fixture))

        self.assertTrue(interface.start_button.isEnabled())
        self.assertTrue(interface.save_button.isEnabled())
        interface.deleteLater()

    def test_subtitle_layout_stacks_panels_on_compact_width(self):
        interface = SubtitleInterface()
        interface.resize(700, 680)
        interface.show()
        self.app.processEvents()

        self.assertEqual(interface.content_splitter.orientation(), Qt.Vertical)

        interface.resize(1000, 680)
        self.app.processEvents()
        self.assertEqual(interface.content_splitter.orientation(), Qt.Horizontal)
        interface.deleteLater()

    def test_dark_subtitle_tables_have_readable_alternating_rows(self):
        interface = SubtitleInterface()

        with patch("app.view.subtitle_interface.isDarkTheme", return_value=True):
            interface._apply_theme_styles()

        for table in (interface.original_table, interface.subtitle_table):
            palette = table.palette()
            base = palette.color(QPalette.Base)
            alternate = palette.color(QPalette.AlternateBase)
            text = palette.color(QPalette.Text)
            self.assertTrue(table.alternatingRowColors())
            self.assertLess(base.lightness(), 90)
            self.assertLess(alternate.lightness(), 90)
            self.assertNotEqual(base.name(), alternate.name())
            self.assertGreater(text.lightness() - base.lightness(), 120)
            self.assertGreater(text.lightness() - alternate.lightness(), 120)
        interface.deleteLater()

    def test_subtitle_finish_and_error_leave_persistent_status(self):
        interface = SubtitleInterface()
        interface.task = SimpleNamespace(need_next_task=False)

        with patch("app.view.subtitle_interface.InfoBar.success"), patch(
            "app.view.subtitle_interface.InfoBar.error"
        ), patch(
            "app.view.subtitle_interface.send_desktop_notification"
        ):
            interface.on_subtitle_optimization_finished(
                "video.mp4", "/tmp/processed.srt"
            )
            self.assertEqual(interface.status_label.text(), "处理完成")
            self.assertEqual(interface.status_label.toolTip(), "/tmp/processed.srt")
            self.assertEqual(interface.progress_bar.value(), 100)
            self.assertEqual(interface.start_button.text(), "再次处理")

            interface.on_subtitle_optimization_error("服务暂时不可用")
            self.assertEqual(interface.status_label.text(), "处理失败")
            self.assertEqual(interface.status_label.toolTip(), "服务暂时不可用")
            self.assertEqual(interface.start_button.text(), "重试")
        interface.deleteLater()

    @staticmethod
    def subtitle_rows():
        return {str(i + 1): {"start_time": i * 1000, "end_time": i * 1000 + 900,
                            "original_subtitle": f"Line {i}", "translated_subtitle": f"第{i}行"}
                for i in range(5)}

    def test_noncontiguous_merge_preserves_every_unselected_row(self):
        page = SubtitleInterface()
        rows = self.subtitle_rows()
        page._set_subtitle_data(rows)
        with patch("app.view.subtitle_interface.InfoBar.warning") as warning:
            page.merge_selected_rows([0, 2])
        self.assertEqual(page.model._data, rows)
        warning.assert_called_once()
        page.deleteLater()

    def test_merge_middle_and_tail_preserves_order_and_joined_text(self):
        page = SubtitleInterface()
        page._set_subtitle_data(self.subtitle_rows())
        with patch("app.view.subtitle_interface.InfoBar.success"):
            page.merge_selected_rows([1, 2])
            self.assertEqual([r["original_subtitle"] for r in page.model._data.values()],
                             ["Line 0", "Line 1 Line 2", "Line 3", "Line 4"])
            self.assertEqual(page.model._data["2"]["translated_subtitle"], "第1行第2行")
            page.merge_selected_rows([2, 3])
            self.assertEqual(page.model.rowCount(), 3)
            self.assertEqual(page.model._data["3"]["end_time"], 4900)
        page.deleteLater()

    def test_running_subtitle_task_cannot_replace_or_merge_its_rows(self):
        page = SubtitleInterface()
        page._set_subtitle_data(self.subtitle_rows())
        original = dict(page.model._data)
        page._subtitle_status = "processing"
        page.subtitle_optimization_thread = Mock()
        page.subtitle_optimization_thread.isRunning.return_value = True
        with patch("app.view.subtitle_interface.InfoBar.warning"), patch(
                "app.view.subtitle_interface.QFileDialog.getOpenFileName") as dialog:
            self.assertFalse(page.load_subtitle_file("must-not-be-opened.srt"))
            page.on_file_select()
            page.merge_selected_rows([0, 1])
            dialog.assert_not_called()
        self.assertEqual(page.model._data, original)
        page._set_processing_controls(True)
        self.assertFalse(page.open_file_action.isEnabled())
        page._set_processing_controls(False)
        self.assertTrue(page.open_file_action.isEnabled())
        page.deleteLater()

    def test_edited_rows_are_submitted_to_worker_without_overwriting_source(self):
        page = SubtitleInterface()
        fixture = Path(__file__).parent / "fixtures" / "transcript.srt"
        original = fixture.read_bytes()
        page.load_subtitle_file(str(fixture))
        page.original_model.setData(page.original_model.index(0, 0), "Edited source", Qt.EditRole)
        with patch("app.thread.subtitle_thread.SubtitleThread") as worker:
            page.start_subtitle_optimization()
            draft = worker.return_value.set_input_data.call_args.args[0]
            self.assertEqual(draft["1"]["original_subtitle"], "Edited source")
            worker.return_value.start.assert_called_once()
        self.assertEqual(fixture.read_bytes(), original)
        page.deleteLater()

    def test_empty_state_disappears_after_opening_subtitle(self):
        page = SubtitleInterface()
        self.assertFalse(page.empty_state.isHidden())
        page.load_subtitle_file(str(Path(__file__).parent / "fixtures" / "transcript.srt"))
        self.assertTrue(page.empty_state.isHidden())
        self.assertIn("条字幕", page.status_label.text())
        page.deleteLater()

    def test_transcription_busy_state_disables_import_and_rejects_replacement(self):
        page = TranscriptionInterface()
        page.task = sentinel = object()
        page._set_processing(True)
        self.assertFalse(page.open_file_action.isEnabled())
        with patch("app.view.transcription_interface.InfoBar.warning"), patch(
                "app.view.transcription_interface.QFileDialog.getOpenFileName") as dialog:
            page._on_file_select()
            self.assertFalse(page.set_task(object()))
            self.assertFalse(page.update_info("another.mp4"))
            dialog.assert_not_called()
        self.assertIs(page.task, sentinel)
        page._set_processing(False)
        self.assertTrue(page.open_file_action.isEnabled())
        page.task = None
        page.deleteLater()

    def test_automatic_transcription_can_start_while_metadata_is_loading(self):
        page = TranscriptionInterface()
        page._metadata_loading = True
        page.video_info_card.task = SimpleNamespace(output_path="", transcribe_config=SimpleNamespace(use_asr_cache=True))
        with patch("app.core.utils.transcription_model_utils.validate_transcription_model_ready", return_value=(True, "ready")), \
             patch("app.thread.transcript_thread_clean.TranscriptThread") as worker, \
             patch.object(page, "_save_transcription_state"):
            self.assertTrue(page.video_info_card.start_transcription(need_create_task=False))
            worker.return_value.start.assert_called_once()
            self.assertFalse(page.open_file_action.isEnabled())
        page._metadata_loading = False
        page._set_processing(False)
        page.deleteLater()

    def test_rejected_handoff_never_starts_the_previous_task(self):
        from app.view.home_interface import HomeInterface
        page = Mock()
        page.set_task.return_value = False
        home = SimpleNamespace(transcription_interface=page, subtitle_optimization_interface=page)
        with patch("app.view.home_interface.TaskFactory.create_transcribe_task"), patch(
                "app.view.home_interface.TaskFactory.create_subtitle_task"):
            self.assertFalse(HomeInterface.switch_to_transcription(home, "new.mp4"))
            self.assertFalse(HomeInterface.switch_to_subtitle_optimization(home, "new.srt", "new.mp4"))
        page.process.assert_not_called()
        page.update_info.assert_not_called()

    def test_metadata_failure_does_not_unlock_a_running_transcription(self):
        page = TranscriptionInterface()
        page._metadata_loading = True
        page._set_processing(True)
        with patch("app.view.transcription_interface.InfoBar.error"):
            page._on_video_info_error("metadata failed")
        self.assertTrue(page.is_processing)
        self.assertFalse(page.open_file_action.isEnabled())
        page._set_processing(False)
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
