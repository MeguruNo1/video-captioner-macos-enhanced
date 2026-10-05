import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..utils.logger import setup_logger
from ..utils.proxy_utils import (
    apply_download_proxy_environment,
    build_download_proxy_env,
)
from .base import BaseASR
from .nltk_utils import call_with_punkt_tab_recovery

logger = setup_logger("whisper_x_auto")

LOCAL_ALIGN_MODEL_DIRS = {
    "en": "facebook-wav2vec2-base-960h",
    "zh": "jonatasgrosman-wav2vec2-large-xlsr-53-chinese-zh-cn",
}
LOCAL_WHISPER_MODEL_DIRS = {
    "tiny": "faster-whisper-tiny",
    "base": "faster-whisper-base",
    "small": "faster-whisper-small",
    "medium": "faster-whisper-medium",
    "large-v1": "faster-whisper-large-v1",
    "large-v2": "faster-whisper-large-v2",
    "large-v3": "faster-whisper-large-v3",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
}
SYSTEM_PYTHON_ENV = "VIDEOCAPTIONER_SYSTEM_PYTHON"
RUNNER_FILE = "whisperx_runner.py"

NOISY_EXTERNAL_LOG_PATTERNS = (
    "triton not found; flop counting will not work for triton kernels",
    "torchcodec is not installed correctly so built-in audio decoding will fail",
    "could not load libtorchcodec",
    "[start of libtorchcodec loading traceback]",
    "[end of libtorchcodec loading traceback]",
    "libtorchcodec_core",
    "torchcodec._internally_replaced_utils.py",
    "ffmpeg version 8:",
    "ffmpeg version 7:",
    "ffmpeg version 6:",
    "ffmpeg version 5:",
    "ffmpeg version 4:",
    "warnings.warn(",
    "reproducibilitywarning: tensorfloat-32 (tf32) has been disabled",
    "torch.backends.cuda.matmul.allow_tf32 = true",
    "torch.backends.cudnn.allow_tf32 = true",
    "std(): degrees of freedom is <= 0.",
    "some weights of wav2vec2forctc were not initialized",
    "you should probably train this model on a down-stream task",
    "use manually assigned vad_model. vad_method is ignored.",
)


def _should_fallback_vad(exc: Exception, vad_method: str) -> bool:
    if (vad_method or "silero") != "silero":
        return False

    message = str(exc).lower()
    keywords = (
        "silero-vad",
        "torch.hub",
        "github.com/snakers4/silero-vad",
        "http error",
        "bad gateway",
        "urlopen",
        "temporary failure",
    )
    return any(keyword in message for keyword in keywords)


def _format_external_log_line(
    line: str, noise_flags: set[str]
) -> tuple[str | None, str | None]:
    if not line:
        return None, None

    lowered = line.lower()

    if line.startswith("PROGRESS\t"):
        parts = line.split("\t", 2)
        if len(parts) == 3:
            return f"WhisperX: {parts[2]}", None
        return None, None

    if "using local silero vad repository:" in lowered:
        return "WhisperX: using local Silero VAD", None

    if any(pattern in lowered for pattern in NOISY_EXTERNAL_LOG_PATTERNS):
        if "torchcodec" in lowered or "ffmpeg version " in lowered:
            if "torchcodec" not in noise_flags:
                noise_flags.add("torchcodec")
                return "WhisperX: skipped noisy torchcodec warning", None
            return None, None
        if "tf32" in lowered:
            if "tf32" not in noise_flags:
                noise_flags.add("tf32")
                return "WhisperX: pyannote disabled TF32 for reproducibility", None
            return None, None
        if "wav2vec2forctc" in lowered or "down-stream task" in lowered:
            if "align_model" not in noise_flags:
                noise_flags.add("align_model")
                return "WhisperX: alignment model loaded with benign initialization warning", None
            return None, None
        return None, None

    return line, None


def _resolve_local_silero_dir(
    local_silero_dir: str | None, strict: bool = False
) -> str | None:
    if not local_silero_dir:
        return None

    repo_dir = Path(local_silero_dir)
    if repo_dir.is_dir() and (repo_dir / "hubconf.py").is_file():
        return str(repo_dir)
    if repo_dir.is_dir():
        nested_repos = [p for p in repo_dir.iterdir() if p.is_dir() and (p / "hubconf.py").is_file()]
        if len(nested_repos) == 1:
            return str(nested_repos[0])

    if strict:
        raise RuntimeError(
            f"Invalid local Silero repository directory: {local_silero_dir}"
        )
    return None


def load_whisperx_align_model(
    whisperx,
    language_code: str,
    device: str = "cpu",
    model_dir: str | None = None,
):
    """Load the standalone acoustic model used by WhisperX forced alignment."""
    kwargs = {
        "language_code": language_code,
        "device": device,
    }
    if model_dir:
        kwargs["model_dir"] = model_dir
        local_dir_name = LOCAL_ALIGN_MODEL_DIRS.get(language_code)
        if local_dir_name:
            local_model_dir = Path(model_dir) / local_dir_name
            if local_model_dir.is_dir():
                kwargs["model_name"] = str(local_model_dir)

    try:
        return whisperx.load_align_model(**kwargs)
    except TypeError:
        kwargs.pop("model_dir", None)
        return whisperx.load_align_model(**kwargs)


def align_transcription_with_whisperx(
    audio_path: str,
    segments: list[dict],
    language_code: str,
    device: str = "cpu",
    model_dir: str | None = None,
    inherit_proxy_environment: bool = False,
) -> dict:
    """Force-align MLX Whisper text with WhisperX's independent acoustic model."""
    if not segments:
        return {"segments": [], "word_segments": [], "language": language_code}

    from app.core.utils.acceleration import resolve_whisperx_device
    device = resolve_whisperx_device(device, "auto")["device"]
    try:
        import whisperx
    except ImportError as exc:
        raise RuntimeError(
            "MLX Whisper 强制对齐需要 WhisperX，请先安装 whisperx。"
        ) from exc

    # Headless workers already receive the saved proxy environment.
    # Reading desktop proxy settings here would import Qt.
    if not inherit_proxy_environment:
        apply_download_proxy_environment()
    from .nltk_utils import ensure_punkt_tab
    ensure_punkt_tab()
    align_model = None
    try:
        audio = whisperx.load_audio(audio_path)
        align_model, metadata = load_whisperx_align_model(
            whisperx,
            language_code,
            device=device,
            model_dir=model_dir,
        )
        result = call_with_punkt_tab_recovery(
            lambda: whisperx.align(
                segments,
                align_model,
                metadata,
                audio,
                device,
                return_char_alignments=False,
            )
        )
        if inherit_proxy_environment:
            # An aligner may return empty words for failed segments or interpolate
            # missing words. Neither is an acoustic measurement for MCP anchors.
            expected = "".join("".join(seg["text"].split()) for seg in segments)
            actual = "".join("".join(word.get("word", "").split())
                             for seg in result.get("segments", []) for word in seg.get("words", []))
            if expected != actual:
                raise ValueError("Forced alignment lost transcript coverage; retranscribe before captioning")
            for seg in result.get("segments", []):
                for word in seg.get("words", []):
                    if any(word.get(key) is None for key in ("start", "end", "score")):
                        raise ValueError("Forced alignment returned an unaligned word; no estimated timestamps accepted")
        result["language"] = language_code
        return result
    finally:
        del align_model
        gc.collect()


class WhisperXASR(BaseASR):
    def __init__(
        self,
        audio_path: str,
        whisper_model: str = "large-v3",
        language: str | None = "en",
        device: str = "cuda",
        compute_type: str = "float16",
        batch_size: int = 8,
        hotwords: str | None = None,
        initial_prompt: str | None = None,
        vad_method: str = "silero",
        vad_threshold: float = 0.5,
        local_silero_dir: str | None = None,
        align: bool = True,
        model_dir: str | None = None,
        use_cache: bool = False,
        need_word_time_stamp: bool = False,
    ):
        super().__init__(audio_path, use_cache)
        self.whisper_model = whisper_model or "large-v3"
        self.language = language or None
        self.device = device or "cuda"
        self.compute_type = compute_type or "float16"
        self.batch_size = batch_size or 8
        self.hotwords = (hotwords or "").strip()
        self.initial_prompt = (initial_prompt or "").strip()
        self.vad_method = vad_method or "silero"
        self.vad_threshold = vad_threshold if vad_threshold is not None else 0.5
        self.local_silero_dir = local_silero_dir or ""
        self.align = align
        self.model_dir = model_dir
        self.need_word_time_stamp = need_word_time_stamp

    def _run(self, callback=None, **kwargs: object) -> dict:
        if callback is None:
            callback = lambda x, y: None

        try:
            import whisperx
        except ImportError:
            return self._run_external(callback)

        from app.core.utils.acceleration import resolve_whisperx_device
        selected = resolve_whisperx_device(self.device, self.compute_type)
        self.device, self.compute_type = selected["device"], selected["compute_type"]
        callback(0, f"WhisperX: {self.device} / {self.compute_type}")

        return self._run_local(whisperx, callback)

    def _run_local(self, whisperx, callback) -> dict:
        apply_download_proxy_environment()
        model = None
        align_model = None

        try:
            callback(5, "Loading WhisperX")
            audio = whisperx.load_audio(self.audio_path)

            callback(15, "Loading transcription model")
            model = self._load_transcribe_model(whisperx)

            callback(35, "Transcribing with WhisperX")
            result = self._transcribe(model, audio)

            need_align = bool(result.get("segments")) and (
                self.align or self.need_word_time_stamp
            )
            if need_align:
                callback(70, "Aligning with WhisperX")
                language = result.get("language") or self.language
                if not isinstance(language, str) or not language:
                    raise RuntimeError("WhisperX did not identify a language for alignment")
                align_model, metadata = self._load_align_model(whisperx, language)
                result = call_with_punkt_tab_recovery(
                    lambda: whisperx.align(
                        result["segments"],
                        align_model,
                        metadata,
                        audio,
                        self.device,
                        return_char_alignments=False,
                    )
                )

            callback(100, "WhisperX finished")
            return result
        finally:
            del model
            del align_model
            gc.collect()

            try:
                import torch

                if self.device == "cuda" and torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    def _run_external(self, callback) -> dict:
        python_exe = self._find_system_python()
        if not python_exe:
            raise RuntimeError(
                "WhisperX is not available in the embedded runtime, and no usable "
                "system Python with whisperx/torch/torchaudio was found."
            )

        from app.core.utils.acceleration import inspect_acceleration, resolve_whisperx_device
        selected = resolve_whisperx_device(self.device, self.compute_type, inspect_acceleration(python_exe))
        self.device, self.compute_type = selected["device"], selected["compute_type"]
        callback(0, f"WhisperX: {self.device} / {self.compute_type}")

        runner_path = Path(__file__).with_name(RUNNER_FILE)
        if not runner_path.exists():
            raise RuntimeError(f"WhisperX runner not found: {runner_path}")

        temp_root = Path(tempfile.gettempdir()) / "bk_asr"
        temp_root.mkdir(parents=True, exist_ok=True)

        callback(5, f"Using system Python: {python_exe}")

        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            temp_dir = Path(temp_dir)
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"

            request_path.write_text(
                json.dumps(
                    {
                        "audio_path": self.audio_path,
                        "whisper_model": self.whisper_model,
                        "whisper_model_path": self._resolve_local_whisper_model_path(),
                        "language": self.language,
                        "device": self.device,
                        "compute_type": self.compute_type,
                        "batch_size": self.batch_size,
                        "hotwords": self.hotwords,
                        "initial_prompt": self.initial_prompt,
                        "vad_method": self.vad_method,
                        "vad_threshold": self.vad_threshold,
                        "local_silero_dir": self.local_silero_dir,
                        "align": self.align,
                        "model_dir": self.model_dir,
                        "need_word_time_stamp": self.need_word_time_stamp,
                        "local_align_model_dirs": LOCAL_ALIGN_MODEL_DIRS,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            command = [
                python_exe,
                str(runner_path),
                str(request_path),
                str(result_path),
            ]

            logger.info("Running WhisperX via system Python")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(temp_dir),
                env=build_download_proxy_env(),
                text=True,
                encoding="utf-8",
                errors="ignore",
            )

            output_lines = []
            noise_flags: set[str] = set()
            if process.stdout is None:
                raise RuntimeError("WhisperX subprocess stdout pipe is unavailable")
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if not line:
                    continue

                line = line.strip()
                output_lines.append(line)
                self._handle_progress_line(line, callback)
                display_line, _ = _format_external_log_line(line, noise_flags)
                if display_line:
                    logger.info("[system-whisperx] %s", display_line)

            process.wait()

            if process.returncode != 0:
                message = self._extract_error_message(output_lines)
                raise RuntimeError(
                    f"System Python WhisperX failed with exit code "
                    f"{process.returncode}: {message}"
                )

            if not result_path.exists():
                raise RuntimeError("System Python WhisperX did not produce a result.")

            callback(100, "WhisperX finished")
            return json.loads(result_path.read_text(encoding="utf-8"))

    def _handle_progress_line(self, line: str, callback):
        if not line.startswith("PROGRESS\t"):
            return

        parts = line.split("\t", 2)
        if len(parts) != 3:
            return

        try:
            progress = int(parts[1])
        except ValueError:
            return

        callback(progress, parts[2])

    def _extract_error_message(self, lines: list[str]) -> str:
        for line in reversed(lines):
            if not line:
                continue
            if line.startswith("PROGRESS\t"):
                continue
            return line
        return "Unknown WhisperX error."

    def _find_system_python(self) -> str | None:
        candidates = []
        env_python = os.environ.get(SYSTEM_PYTHON_ENV)
        if env_python:
            candidates.append(env_python)

        for name in ("python", "python3"):
            path = shutil.which(name)
            if path:
                candidates.append(path)

        current_exe = str(Path(sys.executable).resolve())
        seen = set()

        for candidate in candidates:
            try:
                resolved = str(Path(candidate).resolve())
            except Exception:
                resolved = candidate

            if resolved in seen or resolved == current_exe:
                continue
            seen.add(resolved)

            if self._check_system_python(candidate):
                return candidate

        return None

    def _check_system_python(self, python_exe: str) -> bool:
        probe = (
            "import importlib.util as u; "
            "mods=('whisperx','torch','torchaudio'); "
            "ok=all(u.find_spec(m) is not None for m in mods); "
            "print('OK' if ok else 'MISSING')"
        )
        try:
            result = subprocess.run(
                [python_exe, "-c", probe],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=15,
            )
        except Exception:
            return False

        return result.returncode == 0 and result.stdout.strip() == "OK"

    def _load_transcribe_model(self, whisperx):
        model_name = self._resolve_local_whisper_model_path() or self.whisper_model
        kwargs = {
            "compute_type": self.compute_type,
        }
        if self.language:
            kwargs["language"] = self.language
        if self.model_dir:
            kwargs["download_root"] = self.model_dir
        asr_options = {}
        if self.hotwords:
            asr_options["hotwords"] = self.hotwords
        if self.initial_prompt:
            asr_options["initial_prompt"] = self.initial_prompt
        if asr_options:
            kwargs["asr_options"] = asr_options
        kwargs["vad_method"] = self.vad_method
        kwargs["vad_options"] = {"vad_onset": self.vad_threshold}
        local_silero_dir = _resolve_local_silero_dir(
            self.local_silero_dir, strict=bool(self.local_silero_dir)
        )
        if kwargs["vad_method"] == "silero" and local_silero_dir:
            kwargs["vad_model"] = self._build_local_silero_vad(
                whisperx, local_silero_dir, self.vad_threshold
            )

        def _load_once(load_kwargs: dict):
            try:
                return whisperx.load_model(model_name, self.device, **load_kwargs)
            except TypeError:
                load_kwargs = dict(load_kwargs)
                load_kwargs.pop("download_root", None)
                return whisperx.load_model(model_name, self.device, **load_kwargs)

        try:
            return _load_once(kwargs)
        except Exception as exc:
            if _should_fallback_vad(exc, str(kwargs.get("vad_method") or "silero")):
                logger.warning(
                    "WhisperX silero VAD load failed, retrying with pyannote VAD: %s",
                    exc,
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("vad_model", None)
                retry_kwargs["vad_method"] = "pyannote"
                return _load_once(retry_kwargs)
            raise

    def _transcribe(self, model, audio):
        kwargs: dict[str, int | str] = {"batch_size": self.batch_size}
        if self.language:
            kwargs["language"] = self.language

        try:
            return model.transcribe(audio, **kwargs)
        except TypeError:
            kwargs.pop("language", None)
            return model.transcribe(audio, **kwargs)

    def _build_local_silero_vad(
        self, whisperx, repo_dir: str, vad_onset: float, chunk_size: int = 30
    ):
        import torch
        from whisperx.diarize import Segment as SegmentX
        from whisperx.vads import Vad

        class LocalSileroVad(Vad):
            def __init__(self, local_repo_dir: str, **kwargs):
                super().__init__(kwargs["vad_onset"])
                self.vad_onset = kwargs["vad_onset"]
                self.chunk_size = kwargs["chunk_size"]
                loaded = torch.hub.load(
                    repo_or_dir=local_repo_dir,
                    model="silero_vad",
                    source="local",
                    force_reload=False,
                    onnx=False,
                    trust_repo=True,
                )
                if not isinstance(loaded, tuple) or len(loaded) != 2:
                    raise RuntimeError("Silero VAD did not return a model and utilities")
                self.vad_pipeline, vad_utils = loaded
                if not isinstance(vad_utils, (tuple, list)) or len(vad_utils) != 5:
                    raise RuntimeError("Silero VAD utilities have an unexpected format")
                (self.get_speech_timestamps, _, self.read_audio, _, _) = vad_utils

            def __call__(self, audio, **kwargs):
                sample_rate = audio["sample_rate"]
                if sample_rate != 16000:
                    raise ValueError("Only 16000Hz sample rate is allowed")

                timestamps = self.get_speech_timestamps(
                    audio["waveform"],
                    model=self.vad_pipeline,
                    sampling_rate=sample_rate,
                    max_speech_duration_s=self.chunk_size,
                    threshold=self.vad_onset,
                )
                return [
                    SegmentX(i["start"] / sample_rate, i["end"] / sample_rate, "UNKNOWN")
                    for i in timestamps
                ]

            @staticmethod
            def preprocess_audio(audio):
                return audio

            @staticmethod
            def merge_chunks(segments, chunk_size, onset=0.5, offset=None):
                if len(segments) == 0:
                    return []
                return Vad.merge_chunks(segments, chunk_size, onset, offset)

        logger.info("Using local Silero VAD repository: %s", repo_dir)
        return LocalSileroVad(
            repo_dir,
            vad_onset=vad_onset,
            chunk_size=chunk_size,
        )

    def _load_align_model(self, whisperx, language_code: str):
        return load_whisperx_align_model(
            whisperx,
            language_code,
            device=self.device,
            model_dir=self.model_dir,
        )

    def _get_key(self):
        payload = "|".join(
            [
                self.crc32_hex,
                self.whisper_model,
                str(self.language),
                self.device,
                self.compute_type,
                str(self.batch_size),
                self.hotwords,
                self.initial_prompt,
                self.vad_method,
                str(self.vad_threshold),
                self.local_silero_dir,
                str(self.align),
                str(self.need_word_time_stamp),
            ]
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def _resolve_local_whisper_model_path(self) -> str | None:
        if not self.model_dir:
            return None

        local_dir_name = LOCAL_WHISPER_MODEL_DIRS.get(self.whisper_model)
        if not local_dir_name:
            return None

        local_model_dir = Path(self.model_dir) / local_dir_name
        model_bin = local_model_dir / "model.bin"
        if model_bin.is_file():
            return str(local_model_dir)

        return None
