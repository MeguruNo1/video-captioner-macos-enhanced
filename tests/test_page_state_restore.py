import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from app.common.config import cfg
from app.core.entities import TranscribeTask, VideoInfo
from app.core.storage.page_state import read_page_state, write_page_state
from app.view.subtitle_interface import SubtitleInterface
from app.view.transcription_interface import TranscriptionInterface


class PageStateRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pages = []
        for target, field, filename in (
            (TranscriptionInterface, "TRANSCRIPTION_STATE_PATH", "transcription.json"),
            (SubtitleInterface, "SUBTITLE_STATE_PATH", "subtitle.json"),
            (cfg._cfg, "file", "settings.json"),
        ):
            patcher = patch.object(target, field, self.root / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.destroy_pages)
        self.media = self.root / "video.mp4"
        self.media.write_bytes(b"media")
        self.subtitle = self.root / "video.srt"
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello world\n", encoding="utf-8")

    def destroy_pages(self):
        for page in self.pages:
            if hasattr(page, "_state_timer"):
                page._state_timer.stop()
            page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def page(self, cls):
        page = cls()
        self.pages.append(page)
        return page

    def media_info(self):
        return VideoInfo("video.mp4", str(self.media), 1920, 1080, 30, 1, 1000,
                         "h264", "aac", 48000, "")

    def test_completed_transcription_restores_handoff_without_running_worker(self):
        first = self.page(TranscriptionInterface)
        first.video_info_card.update_info(self.media_info())
        task = TranscribeTask(file_path=str(self.media), output_path=str(self.subtitle))
        with patch("app.view.transcription_interface.send_desktop_notification"):
            first._on_transcript_finished(task)
        restored = self.page(TranscriptionInterface)
        self.assertEqual(restored.video_info_card.video_info.file_path, str(self.media))
        self.assertTrue(restored.send_to_translate_action.isEnabled())
        self.assertEqual(restored.task.output_path, str(self.subtitle))
        self.assertFalse(restored.task.need_next_task)
        self.assertFalse(restored.is_processing)
        self.assertFalse(hasattr(restored.video_info_card, "transcript_thread"))

    def test_interrupted_transcription_restores_progress_but_not_completion(self):
        first = self.page(TranscriptionInterface)
        first.video_info_card.update_info(self.media_info())
        first._transcription_status = "processing"
        first.video_info_card.on_transcript_progress(42, "语音转录中")
        restored = self.page(TranscriptionInterface)
        self.assertEqual(restored.video_info_card.progress_ring.value(), 42)
        self.assertEqual(restored.video_info_card.start_button.text(), "重新转录")
        self.assertTrue(restored.video_info_card.start_button.isEnabled())
        self.assertFalse(restored.send_to_translate_action.isEnabled())
        self.assertFalse(restored.is_processing)

    def test_replaced_media_does_not_reuse_previous_result(self):
        first = self.page(TranscriptionInterface)
        first.video_info_card.update_info(self.media_info())
        self.media.write_bytes(b"different media")
        first._save_transcription_state()
        restored = self.page(TranscriptionInterface)
        self.assertIsNone(restored.video_info_card.video_info)
        self.assertFalse(restored.send_to_translate_action.isEnabled())

    def test_subtitle_edits_and_partial_progress_survive_recreation(self):
        first = self.page(SubtitleInterface)
        first.load_subtitle_file(str(self.subtitle))
        first._subtitle_status = "processing"
        first.update_data({"1": "Hello world||你好，世界"})
        first.original_model.setData(first.original_model.index(0, 0), "Hello, world!", Qt.EditRole)
        first.on_subtitle_optimization_progress(45, "正在翻译")
        QTest.qWait(350)
        restored = self.page(SubtitleInterface)
        self.assertEqual(restored.model._data["1"]["original_subtitle"], "Hello, world!")
        self.assertEqual(restored.model._data["1"]["translated_subtitle"], "你好，世界")
        self.assertEqual(restored.progress_bar.value(), 45)
        self.assertEqual(restored.start_button.text(), "重试")
        self.assertTrue(restored.start_button.isEnabled())
        self.assertTrue(restored.save_button.isEnabled())
        self.assertIn("正在翻译", restored.log_text.toPlainText())
        self.assertFalse(hasattr(restored, "subtitle_optimization_thread"))

    def test_missing_source_keeps_editable_subtitle_draft(self):
        first = self.page(SubtitleInterface)
        first.load_subtitle_file(str(self.subtitle))
        self.subtitle.unlink()
        restored = self.page(SubtitleInterface)
        self.assertEqual(restored.model.rowCount(), 1)
        self.assertFalse(restored.start_button.isEnabled())
        self.assertTrue(restored.save_button.isEnabled())
        self.assertIn("原文件不存在", restored.status_label.text())

    def test_quit_flushes_pending_manual_edit(self):
        first = self.page(SubtitleInterface)
        first.load_subtitle_file(str(self.subtitle))
        first.translation_model.setData(first.translation_model.index(0, 0), "退出前的修改")
        self.assertTrue(first._state_timer.isActive())
        self.app.aboutToQuit.emit()
        restored = self.page(SubtitleInterface)
        self.assertEqual(restored.model._data["1"]["translated_subtitle"], "退出前的修改")

    def test_completed_subtitle_restores_output_and_video_association(self):
        from app.core.entities import SubtitleTask

        first = self.page(SubtitleInterface)
        first.load_subtitle_file(str(self.subtitle))
        first.task = SubtitleTask(subtitle_path=str(self.subtitle),
                                  video_path=str(self.media), need_next_task=False)
        with patch("app.view.subtitle_interface.InfoBar.success"), patch(
            "app.view.subtitle_interface.send_desktop_notification"
        ):
            first.on_subtitle_optimization_finished(str(self.media), str(self.root / "output.srt"))
        restored = self.page(SubtitleInterface)
        self.assertEqual(restored.start_button.text(), "再次处理")
        self.assertEqual(restored.progress_bar.value(), 100)
        self.assertEqual(restored.task.video_path, str(self.media))
        self.assertEqual(restored.status_label.toolTip(), str(self.root / "output.srt"))

    def test_new_subtitle_replaces_previous_draft_and_progress(self):
        first = self.page(SubtitleInterface)
        first.load_subtitle_file(str(self.subtitle))
        first.update_data({"1": "old translation"})
        first._subtitle_status = "processing"
        first.progress_bar.setValue(80)
        other = self.root / "other.srt"
        other.write_text("1\n00:00:00,000 --> 00:00:02,000\nNew text\n", encoding="utf-8")
        first.load_subtitle_file(str(other))
        restored = self.page(SubtitleInterface)
        self.assertEqual(restored.subtitle_path, str(other))
        self.assertEqual(restored.model._data["1"]["original_subtitle"], "New text")
        self.assertFalse(restored.model._data["1"].get("translated_subtitle"))
        self.assertEqual(restored.progress_bar.value(), 0)

    def test_corrupt_and_incompatible_cache_are_ignored(self):
        for content in ("{broken", "[]", '{"version": 999}',
                        '{"version": 1, "subtitle_path": "file", "data": {"1": null}}'):
            with self.subTest(content=content):
                for path in (SubtitleInterface.SUBTITLE_STATE_PATH, TranscriptionInterface.TRANSCRIPTION_STATE_PATH):
                    path.write_text(content, encoding="utf-8")
                self.assertFalse(self.page(SubtitleInterface).start_button.isEnabled())
                self.assertFalse(self.page(TranscriptionInterface).send_to_translate_action.isEnabled())

    def test_failed_atomic_write_preserves_previous_snapshot(self):
        path = self.root / "atomic.json"
        write_page_state(path, {"status": "ready"})
        with patch("app.core.storage.page_state.os.replace", side_effect=OSError("disk full")):
            write_page_state(path, {"status": "processing"})
        self.assertEqual(read_page_state(path)["status"], "ready")
        self.assertFalse(path.with_suffix(".tmp").exists())
