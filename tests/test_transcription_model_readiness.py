import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QWidget

from app.core.entities import TranscribeConfig, TranscribeModelEnum
from app.core.utils import transcription_model_utils as model_utils
from app.components.MLXWhisperSettingWidget import MLXWhisperSettingWidget
from app.view.transcription_interface import VideoInfoCard


class TranscriptionModelReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mlx_local_model_requires_all_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            config = TranscribeConfig(
                transcribe_model=TranscribeModelEnum.MLX_WHISPER,
                mlx_model=str(model_dir),
            )

            ready, message = model_utils.validate_transcription_model_ready(config)
            self.assertFalse(ready)
            self.assertIn("缺少文件", message)

            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "weights.safetensors").write_bytes(b"weights")
            ready, _ = model_utils.validate_transcription_model_ready(config)
            self.assertTrue(ready)

    def test_mlx_npz_model_is_ready_locally_and_in_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            config = TranscribeConfig(
                transcribe_model=TranscribeModelEnum.MLX_WHISPER,
                mlx_model=str(model_dir),
            )
            self.assertFalse(model_utils.validate_transcription_model_ready(config)[0])
            (model_dir / "weights.npz").write_bytes(b"weights")
            self.assertTrue(model_utils.validate_transcription_model_ready(config)[0])
            config.mlx_model = "mlx-community/whisper-small-mlx"
            with patch.object(model_utils, "_cached_huggingface_snapshot", return_value=model_dir):
                self.assertTrue(model_utils.validate_transcription_model_ready(config)[0])
                (model_dir / "weights.npz").unlink()
                self.assertFalse(model_utils.validate_transcription_model_ready(config)[0])

    def test_uncached_remote_mlx_model_is_rejected_before_start(self):
        config = TranscribeConfig(
            transcribe_model=TranscribeModelEnum.MLX_WHISPER,
            mlx_model="example/missing-model",
        )
        with patch.object(model_utils, "_cached_huggingface_snapshot", return_value=None):
            ready, message = model_utils.validate_transcription_model_ready(config)

        self.assertFalse(ready)
        self.assertIn("尚未下载完成", message)

    def test_whisperx_uses_installed_model_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = TranscribeConfig(
                transcribe_model=TranscribeModelEnum.WHISPER_X,
                whisperx_model="large-v3-turbo",
                whisperx_model_dir=str(root),
            )

            ready, _ = model_utils.validate_transcription_model_ready(config)
            self.assertFalse(ready)

            model_dir = root / "faster-whisper-large-v3-turbo"
            model_dir.mkdir()
            (model_dir / "model.bin").write_bytes(b"model")
            ready, _ = model_utils.validate_transcription_model_ready(config)
            self.assertTrue(ready)

    def test_missing_model_does_not_enter_processing_or_create_thread(self):
        interface = QWidget()
        interface.is_processing = False
        interface._set_translation_handoff_enabled = Mock()
        card = VideoInfoCard(interface)
        card.task = SimpleNamespace(
            transcribe_config=TranscribeConfig(
                transcribe_model=TranscribeModelEnum.WHISPER_X,
                whisperx_model="large-v3-turbo",
                whisperx_model_dir="/missing-model-root",
            ),
            output_path="",
        )

        with patch(
            "app.view.transcription_interface.InfoBar.warning"
        ) as warning:
            started = card.start_transcription(need_create_task=False)

        self.assertFalse(started)
        self.assertFalse(interface.is_processing)
        self.assertFalse(card.progress_ring.isVisible())
        self.assertTrue(card.start_button.isEnabled())
        self.assertFalse(hasattr(card, "transcript_thread"))
        warning.assert_called_once()
        card.deleteLater()
        interface.deleteLater()

    def test_explicit_model_status_check_reports_ready_model(self):
        ready_path = Path("/tmp/ready-mlx-model")
        status_card = Mock()
        widget = SimpleNamespace(
            tr=lambda text: text,
            model_status_card=status_card,
        )
        with patch(
            "app.components.MLXWhisperSettingWidget.resolve_available_mlx_model",
            return_value=ready_path,
        ), patch(
            "app.components.MLXWhisperSettingWidget.validate_mlx_model",
            return_value=(True, "configured"),
        ), patch(
            "app.components.MLXWhisperSettingWidget.InfoBar.success"
        ) as success:
            MLXWhisperSettingWidget.refresh_model_status(widget, show_warning=True)

        status_card.setContent.assert_called_once_with(
            f"模型已就绪：{ready_path}"
        )
        status_card.setToolTip.assert_called_once_with(f"模型已就绪：{ready_path}")
        success.assert_called_once()

    def test_explicit_model_status_check_warns_when_model_is_not_cached(self):
        status_card = Mock()
        widget = SimpleNamespace(
            tr=lambda text: text,
            model_status_card=status_card,
        )
        with patch(
            "app.components.MLXWhisperSettingWidget.resolve_available_mlx_model",
            return_value=None,
        ), patch(
            "app.components.MLXWhisperSettingWidget.validate_mlx_model",
            return_value=(True, "configured"),
        ), patch(
            "app.components.MLXWhisperSettingWidget.InfoBar.warning"
        ) as warning:
            MLXWhisperSettingWidget.refresh_model_status(widget, show_warning=True)

        self.assertIn("尚未下载完成", status_card.setContent.call_args.args[0])
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
