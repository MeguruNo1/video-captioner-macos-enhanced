import unittest
import types
from pathlib import Path
from unittest.mock import Mock, patch

from app.core.bk_asr.mlx_whisper import MLXWhisperASR, build_mlx_initial_prompt
from app.core.bk_asr.transcribe import transcribe
from app.core.entities import TranscribeConfig, TranscribeModelEnum


class MLXWhisperASRTests(unittest.TestCase):
    def test_converts_word_timestamp_result_to_asr_segments(self):
        asr = MLXWhisperASR(
            b"audio",
            model="mlx-community/whisper-large-v3-turbo",
            language="en",
            need_word_time_stamp=True,
        )

        segments = asr._make_segments(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "hello world",
                        "words": [
                            {"word": "hello", "start": 0.1, "end": 0.4},
                            {"word": "world", "start": 0.5, "end": 0.9},
                        ],
                    }
                ]
            }
        )

        self.assertEqual([segment.text for segment in segments], ["hello", "world"])
        self.assertEqual(
            [(segment.start_time, segment.end_time) for segment in segments],
            [(100, 400), (500, 900)],
        )

    def test_run_calls_mlx_whisper_with_configured_model_and_language(self):
        asr = MLXWhisperASR(
            b"audio",
            model="mlx-community/whisper-large-v3-turbo",
            language="zh",
            need_word_time_stamp=True,
            initial_prompt="请优先识别：VideoCaptioner, MLX Whisper",
        )

        mocked_transcribe = Mock(return_value={"segments": []})
        fake_mlx_whisper = types.SimpleNamespace(transcribe=mocked_transcribe)
        with patch.dict("sys.modules", {"mlx_whisper": fake_mlx_whisper}):
            result = asr._run()

        self.assertEqual(result, {"segments": []})
        mocked_transcribe.assert_called_once_with(
            b"audio",
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="zh",
            word_timestamps=False,
            initial_prompt="请优先识别：VideoCaptioner, MLX Whisper",
            condition_on_previous_text=False,
        )

    def test_run_rejects_missing_local_model_before_import(self):
        asr = MLXWhisperASR(
            b"audio",
            model="/tmp/not-a-videocaptioner-mlx-model",
            language="zh",
            need_word_time_stamp=True,
        )

        with self.assertRaisesRegex(RuntimeError, "本地模型目录不存在"):
            asr._run()

    def test_transcribe_once_passes_path_as_string(self):
        for alignment_method in ("native", "whisperx"):
            with self.subTest(alignment_method=alignment_method):
                asr = MLXWhisperASR(
                    b"audio",
                    model="mlx-community/whisper-large-v3-turbo",
                    language="en",
                    need_word_time_stamp=True,
                    alignment_method=alignment_method,
                )
                mocked_transcribe = Mock(return_value={"segments": []})
                fake_mlx_whisper = types.SimpleNamespace(transcribe=mocked_transcribe)
                audio_path = Path("/tmp/videocaptioner-mlx/chunk-0001.wav")

                self.assertEqual(asr._transcribe_once(fake_mlx_whisper, audio_path), {"segments": []})
                mocked_transcribe.assert_called_once_with(
                    str(audio_path),
                    path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
                    language="en",
                    word_timestamps=alignment_method == "native",
                    initial_prompt=None,
                    condition_on_previous_text=False,
                )

    def test_word_timestamps_use_whisperx_forced_alignment(self):
        asr = MLXWhisperASR(
            b"audio",
            model="mlx-community/whisper-large-v3-turbo",
            language="en",
            need_word_time_stamp=True,
            align_device="cpu",
            align_model_dir="/models",
        )
        transcription = {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello world"},
            ]
        }
        aligned = {
            "segments": [
                {
                    "start": 0.2,
                    "end": 0.9,
                    "text": "hello world",
                    "words": [
                        {"word": "hello", "start": 0.2, "end": 0.5},
                        {"word": "world", "start": 0.6, "end": 0.9},
                    ],
                }
            ]
        }
        fake_mlx_whisper = types.SimpleNamespace()

        with (
            patch.dict("sys.modules", {"mlx_whisper": fake_mlx_whisper}),
            patch.object(asr, "_run_workflow", return_value=transcription),
            patch(
                "app.core.bk_asr.mlx_whisper.align_transcription_with_whisperx",
                return_value=aligned,
            ) as mocked_align,
        ):
            result = asr._run(callback=Mock())

        self.assertEqual(result, aligned)
        mocked_align.assert_called_once_with(
            b"audio",
            [{"start": 0.0, "end": 1.0, "text": "hello world"}],
            "en",
            device="cpu",
            model_dir="/models",
        )

    def test_builds_initial_prompt_from_mlx_prompt_and_hotwords(self):
        self.assertEqual(
            build_mlx_initial_prompt(
                "这是一段技术播客。",
                "VideoCaptioner\nMLX Whisper, large-v3-turbo",
            ),
            (
                "这是一段技术播客。\n"
                "以下专有名词或短语可能出现在音频中，请优先按这些写法识别："
                "VideoCaptioner, MLX Whisper, large-v3-turbo"
            ),
        )

    def test_alignment_failure_stops_transcription(self):
        asr = MLXWhisperASR(
            b"audio",
            model="mlx-community/whisper-large-v3-turbo",
            language="en",
            need_word_time_stamp=True,
        )
        transcription = {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello world"},
            ]
        }
        fake_mlx_whisper = types.SimpleNamespace()

        with (
            patch.dict("sys.modules", {"mlx_whisper": fake_mlx_whisper}),
            patch.object(asr, "_run_workflow", return_value=transcription),
            patch(
                "app.core.bk_asr.mlx_whisper.align_transcription_with_whisperx",
                side_effect=RuntimeError("alignment failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "alignment failed"):
                asr._run(callback=Mock())

    def test_cache_key_changes_with_workflow_version(self):
        asr = MLXWhisperASR(
            b"audio",
            model="mlx-community/whisper-large-v3-turbo",
            language="en",
            need_word_time_stamp=True,
        )
        original_key = asr._get_key()

        with patch("app.core.bk_asr.mlx_whisper.MLX_WORKFLOW_VERSION", "next-version"):
            updated_key = asr._get_key()

        self.assertNotEqual(original_key, updated_key)

    def test_transcribe_dispatches_to_mlx_backend(self):
        config = TranscribeConfig(
            transcribe_model=TranscribeModelEnum.MLX_WHISPER,
            transcribe_language="en",
            use_asr_cache=False,
            need_word_time_stamp=True,
            mlx_model="mlx-community/whisper-large-v3-turbo",
            mlx_word_timestamps=True,
            mlx_hotwords="VideoCaptioner\nMLX Whisper",
            mlx_initial_prompt="这是一段技术播客。",
            mlx_vad_enabled=True,
            mlx_vad_threshold=0.5,
            mlx_chunk_duration=600,
            mlx_chunk_overlap=30,
        )

        with patch("app.core.bk_asr.mlx_whisper.MLXWhisperASR") as mocked_mlx_asr:
            mocked_mlx_asr.return_value.run.return_value = "mlx-result"
            result = transcribe("sample.wav", config)

        self.assertEqual(result, "mlx-result")
        mocked_mlx_asr.assert_called_once_with(
            "sample.wav",
            use_cache=False,
            need_word_time_stamp=True,
            model="mlx-community/whisper-large-v3-turbo",
            language="en",
            initial_prompt=(
                "这是一段技术播客。\n"
                "以下专有名词或短语可能出现在音频中，请优先按这些写法识别："
                "VideoCaptioner, MLX Whisper"
            ),
            vad_enabled=True,
            vad_threshold=0.5,
            chunk_duration=600,
            chunk_overlap=30,
            align_device="cpu",
            align_model_dir=None,
        )


if __name__ == "__main__":
    unittest.main()
