import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
