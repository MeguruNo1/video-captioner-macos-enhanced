import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qfluentwidgets import qconfig

from app.common.config import Config
from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.subtitle_processor import translate
from app.thread.subtitle_thread import SubtitleThread


class FailingSubtitleData:
    def save(self, save_path: str, layout: str) -> None:
        Path(save_path).write_text("incomplete", encoding="utf-8")
        raise RuntimeError("save failed")


class LLMOnlyTranslationTests(unittest.TestCase):
    @staticmethod
    def _thread_for_output(output_path: Path):
        return SimpleNamespace(task=SimpleNamespace(output_path=str(output_path)))

    @staticmethod
    def _translated_data():
        return ASRData(
            [ASRDataSeg("hello", 0, 1000, translated_text="你好")]
        )

    def test_edited_input_is_frozen_and_bypasses_disk_sidecar(self):
        rows = {"1": {"start_time": 0, "end_time": 1000,
                      "original_subtitle": "Edited", "translated_subtitle": "修改后"}}
        holder = SimpleNamespace(task=SimpleNamespace(subtitle_path="missing.srt"))
        SubtitleThread.set_input_data(holder, rows)
        rows["1"]["original_subtitle"] = "Later edit"
        data = SubtitleThread._load_input_subtitles(holder)
        self.assertEqual(data.segments[0].text, "Edited")
        self.assertEqual(data.segments[0].translated_text, "修改后")

    def test_atomic_save_replaces_primary_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "subtitle.srt"
            output.write_text("old", encoding="utf-8")
            holder = self._thread_for_output(output)

            SubtitleThread._atomic_save_subtitles(
                holder, self._translated_data(), "仅译文"
            )

            self.assertIn("你好", output.read_text("utf-8"))
            self.assertFalse(list(Path(temp_dir).glob(".*.partial*")))

    def test_atomic_save_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "subtitle.srt"
            output.write_text("old", encoding="utf-8")
            holder = self._thread_for_output(output)

            with self.assertRaisesRegex(RuntimeError, "save failed"):
                SubtitleThread._atomic_save_subtitles(
                    holder, FailingSubtitleData(), "仅译文"
                )

            self.assertEqual(output.read_text("utf-8"), "old")
            self.assertFalse(list(Path(temp_dir).glob(".*.partial*")))

    def test_atomic_save_writes_separate_layout_without_partial_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "subtitle.srt"
            holder = self._thread_for_output(output)

            SubtitleThread._atomic_save_subtitles(
                holder, self._translated_data(), "单独输出原文和译文"
            )

            self.assertTrue(output.exists())
            self.assertTrue(Path(temp_dir, "subtitle-仅原文.srt").exists())
            self.assertTrue(Path(temp_dir, "subtitle-仅译文.srt").exists())
            self.assertFalse(list(Path(temp_dir).glob(".*.partial*")))

    def test_legacy_translation_service_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "Translate": {
                            "TranslatorServiceEnum": "微软翻译",
                            "DeeplxEndpoint": "https://example.invalid",
                            "BatchSize": 12,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = Config()

            qconfig.load(str(settings_path), config)

            self.assertEqual(config.batch_size.value, 12)
            self.assertFalse(hasattr(config, "translator_service"))
            self.assertFalse(hasattr(config, "deeplx_endpoint"))
            self.assertFalse(hasattr(translate, "GoogleTranslator"))
            self.assertFalse(hasattr(translate, "BingTranslator"))
            self.assertFalse(hasattr(translate, "DeepLXTranslator"))
            self.assertFalse(hasattr(translate, "TranslatorFactory"))


if __name__ == "__main__":
    unittest.main()
