import gc
import hashlib
import tempfile
from pathlib import Path

from ..utils.logger import setup_logger
from ..utils.mlx_model_utils import DEFAULT_MLX_MODEL, validate_mlx_model
from .base import BaseASR
from .mlx_workflow import (
    build_chunk_windows,
    detect_speech_ranges,
    extract_audio_chunk,
    merge_transcription_results,
    offset_transcription_result,
    probe_audio_duration,
    split_ranges_to_windows,
)

logger = setup_logger("mlx_whisper")
MLX_WORKFLOW_VERSION = "vad-union-global-dedupe-whisperx-align-v3"

def align_transcription_with_whisperx(*args, **kwargs):
    """Lazy compatibility entrypoint; native MLX jobs never load WhisperX."""
    from .whisper_x_auto import align_transcription_with_whisperx as align
    return align(*args, **kwargs)


def _parse_prompt_terms(text: str) -> list[str]:
    terms = []
    for raw_line in (text or "").replace("，", ",").splitlines():
        for raw_term in raw_line.split(","):
            term = raw_term.strip()
            if term and term not in terms:
                terms.append(term)
    return terms


def build_mlx_initial_prompt(initial_prompt: str | None, hotwords: str | None) -> str:
    parts = []
    initial_prompt = (initial_prompt or "").strip()
    if initial_prompt:
        parts.append(initial_prompt)

    hotword_terms = _parse_prompt_terms(hotwords or "")
    if hotword_terms:
        parts.append(
            "以下专有名词或短语可能出现在音频中，请优先按这些写法识别："
            + ", ".join(hotword_terms)
        )

    return "\n".join(parts)


class MLXWhisperASR(BaseASR):
    def __init__(
        self,
        audio_path: str,
        model: str = DEFAULT_MLX_MODEL,
        language: str = "en",
        initial_prompt: str = "",
        use_cache: bool = False,
        need_word_time_stamp: bool = True,
        vad_enabled: bool = True,
        vad_threshold: float = 0.5,
        chunk_duration: int = 600,
        chunk_overlap: int = 30,
        align_device: str = "cpu",
        align_model_dir: str | None = None,
        alignment_method: str = "whisperx",
        align_inherit_proxy_environment: bool = False,
    ):
        if alignment_method not in {"whisperx", "native"}:
            raise ValueError("alignment_method must be whisperx or native")
        self.alignment_method = alignment_method
        self.align_inherit_proxy_environment = align_inherit_proxy_environment
        super().__init__(audio_path, use_cache)
        self.model = model or DEFAULT_MLX_MODEL
        self.language = language or None
        self.initial_prompt = (initial_prompt or "").strip()
        self.need_word_time_stamp = need_word_time_stamp
        self.vad_enabled = bool(vad_enabled)
        self.vad_threshold = float(vad_threshold or 0.5)
        self.chunk_duration = max(1, int(chunk_duration or 600))
        self.chunk_overlap = max(0, int(chunk_overlap or 0))
        self.align_device = align_device or "cpu"
        self.align_model_dir = align_model_dir

    def _transcribe_once(self, mlx_whisper, audio_path: str | Path) -> dict:
        if isinstance(audio_path, Path):
            audio_path = str(audio_path)
        return mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self.model,
            language=self.language,
            # Legacy tasks can explicitly retain native timestamps.
            word_timestamps=self.need_word_time_stamp and self.alignment_method == "native",
            initial_prompt=self.initial_prompt or None,
            # History can suppress whole sentences in mixed-language clips.
            # Keep ASR decoding independent of the downstream timestamp backend.
            condition_on_previous_text=False,
        )

    def _build_workflow_windows(
        self, duration: float, vad_audio_path: str | Path | None = None
    ) -> list[tuple[float, float, float, float]]:
        if self.vad_enabled:
            speech_ranges = detect_speech_ranges(
                vad_audio_path or self.audio_path,
                threshold=self.vad_threshold,
            )
            if speech_ranges:
                return split_ranges_to_windows(
                    speech_ranges,
                    duration,
                    self.chunk_duration,
                    self.chunk_overlap,
                )
        return build_chunk_windows(duration, self.chunk_duration, self.chunk_overlap)

    def _run_workflow(self, mlx_whisper, callback) -> dict:
        duration = probe_audio_duration(self.audio_path)
        if duration is None:
            return self._transcribe_once(mlx_whisper, self.audio_path)

        with tempfile.TemporaryDirectory(prefix="videocaptioner-mlx-") as tmpdir:
            vad_audio_path = None
            if self.vad_enabled:
                try:
                    vad_audio_path = extract_audio_chunk(
                        self.audio_path,
                        Path(tmpdir) / "vad-source.wav",
                        0,
                        duration,
                    )
                except Exception as exc:
                    logger.warning("MLX VAD 预处理失败，回退到普通分块: %s", exc)

            windows = self._build_workflow_windows(duration, vad_audio_path)
            if (
                len(windows) <= 1
                and windows
                and windows[0][0] == 0
                and abs(windows[0][1] - duration) < 0.01
            ):
                return self._transcribe_once(mlx_whisper, self.audio_path)

            results = []
            total = max(1, len(windows))
            for index, (start, end, keep_start, keep_end) in enumerate(windows, 1):
                progress = min(95, 10 + int(index / total * 80))
                callback(progress, f"Transcribing MLX chunk {index}/{total}")
                chunk_path = Path(tmpdir) / f"chunk-{index:04d}.wav"
                extract_audio_chunk(self.audio_path, chunk_path, start, end)
                chunk_result = self._transcribe_once(mlx_whisper, chunk_path)
                results.append(
                    offset_transcription_result(
                        chunk_result,
                        offset_seconds=start,
                        keep_start_seconds=keep_start,
                        keep_end_seconds=keep_end,
                    )
                )

            return merge_transcription_results(results)

    def _run(self, callback=None) -> dict:
        if callback is None:
            callback = lambda x, y: None

        is_valid_model, model_message = validate_mlx_model(self.model)
        if not is_valid_model:
            raise RuntimeError(model_message)

        try:
            import mlx_whisper
        except ImportError as exc:
            raise RuntimeError(
                "MLX Whisper 未安装。请先安装 mlx-whisper 后再使用该转录模型。"
            ) from exc

        try:
            callback(5, "Loading MLX Whisper")
            callback(35, "Transcribing with MLX Whisper")
            result = self._run_workflow(mlx_whisper, callback)
            if self.need_word_time_stamp and self.alignment_method == "whisperx" and result.get("segments"):
                callback(92, "Aligning MLX transcript with WhisperX")
                alignment_segments = [
                    {
                        "start": float(segment["start"]),
                        "end": float(segment["end"]),
                        "text": str(segment.get("text") or "").strip(),
                    }
                    for segment in result["segments"]
                    if str(segment.get("text") or "").strip()
                    and segment.get("start") is not None
                    and segment.get("end") is not None
                    and float(segment["end"]) > float(segment["start"])
                ]
                result = align_transcription_with_whisperx(
                    self.audio_path,
                    alignment_segments,
                    self.language or result.get("language"),
                    device=self.align_device,
                    model_dir=self.align_model_dir,
                    **({"inherit_proxy_environment": True} if self.align_inherit_proxy_environment else {}),
                )
            callback(100, "MLX Whisper finished")
            return result
        finally:
            gc.collect()

    def _get_key(self):
        payload = "|".join(
            [
                self.crc32_hex,
                self.model,
                str(self.language),
                self.initial_prompt,
                str(self.need_word_time_stamp),
                str(self.vad_enabled),
                str(self.vad_threshold),
                str(self.chunk_duration),
                str(self.chunk_overlap),
                self.alignment_method,
                self.align_device,
                str(self.align_model_dir),
                MLX_WORKFLOW_VERSION,
            ]
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
