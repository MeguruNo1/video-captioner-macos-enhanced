import copy
import json
import subprocess
from pathlib import Path
from typing import Iterable

from ..utils.logger import setup_logger
from app.config import MODEL_PATH

logger = setup_logger("mlx_workflow")
LOCAL_SILERO_REPO = MODEL_PATH / "silero-vad"

MLX_SAMPLE_RATE = 16000


def build_chunk_windows(
    duration_seconds: float,
    chunk_duration_seconds: int = 600,
    overlap_seconds: int = 30,
) -> list[tuple[float, float, float, float]]:
    duration = max(0.0, float(duration_seconds or 0))
    if duration <= 0:
        return []

    chunk_duration = max(1.0, float(chunk_duration_seconds or 600))
    overlap = max(0.0, float(overlap_seconds or 0))
    if overlap >= chunk_duration:
        overlap = max(0.0, chunk_duration - 1.0)

    if duration <= chunk_duration:
        return [(0.0, duration, 0.0, duration)]

    step = chunk_duration - overlap
    windows = []
    start = 0.0
    while start < duration:
        end = min(duration, start + chunk_duration)
        next_start = start + step
        keep_start = 0.0 if not windows else min(end, start + overlap / 2)
        keep_end = (
            duration
            if next_start >= duration
            else min(end, next_start + overlap / 2)
        )
        windows.append(
            (
                round(start, 3),
                round(end, 3),
                round(keep_start, 3),
                round(keep_end, 3),
            )
        )
        if end >= duration:
            break
        start = next_start
    return windows


def _is_inside_keep_range(
    start: float,
    end: float,
    keep_start_seconds: float | None,
    keep_end_seconds: float | None,
) -> bool:
    center = (float(start) + float(end)) / 2
    if keep_start_seconds is not None and center < keep_start_seconds:
        return False
    if keep_end_seconds is not None and center >= keep_end_seconds:
        return False
    return True


def offset_transcription_result(
    result: dict,
    offset_seconds: float,
    keep_start_seconds: float | None = None,
    keep_end_seconds: float | None = None,
) -> dict:
    shifted = {"segments": []}
    offset = float(offset_seconds or 0)

    for raw_segment in result.get("segments", []) or []:
        segment = copy.deepcopy(raw_segment)
        start = float(segment.get("start") or 0) + offset
        end = float(segment.get("end") or start) + offset

        segment["start"] = round(start, 3)
        segment["end"] = round(end, 3)
        words = []
        for raw_word in segment.get("words", []) or []:
            word = copy.deepcopy(raw_word)
            word_start = float(word.get("start") or 0) + offset
            word_end = float(word.get("end") or word_start) + offset
            if not _is_inside_keep_range(
                word_start, word_end, keep_start_seconds, keep_end_seconds
            ):
                continue
            word["start"] = round(word_start, 3)
            word["end"] = round(word_end, 3)
            words.append(word)
        if segment.get("words") is not None:
            if not words:
                continue
            segment["words"] = words
            segment["start"] = round(
                min(float(word.get("start") or 0) for word in words), 3
            )
            segment["end"] = round(
                max(float(word.get("end") or 0) for word in words), 3
            )
            segment["text"] = " ".join(
                str(word.get("word") or word.get("text") or "").strip()
                for word in words
                if str(word.get("word") or word.get("text") or "").strip()
            )
        elif not _is_inside_keep_range(
            start, end, keep_start_seconds, keep_end_seconds
        ):
            continue
        shifted["segments"].append(segment)

    shifted["text"] = " ".join(
        (segment.get("text") or "").strip()
        for segment in shifted["segments"]
        if (segment.get("text") or "").strip()
    )
    return shifted


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_duplicate_segment(segment: dict, existing: Iterable[dict]) -> bool:
    text = _normalize_text(segment.get("text") or "")
    if not text:
        return False
    for item in existing:
        if _normalize_text(item.get("text") or "") != text:
            continue
        if _word_interval_overlap_ratio(item, segment) >= 0.5:
            return True
    return False


def _dedupe_words(words: list[dict]) -> list[dict]:
    deduped = []
    for word in sorted(words, key=lambda item: float(item.get("start") or 0)):
        text = _normalize_text(word.get("word") or word.get("text") or "")
        if any(
            _normalize_text(item.get("word") or item.get("text") or "") == text
            and _word_interval_overlap_ratio(item, word) >= 0.5
            for item in deduped
        ):
            continue
        deduped.append(word)
    return deduped


def _word_interval_overlap_ratio(left: dict, right: dict) -> float:
    left_start = float(left.get("start") or 0)
    left_end = float(left.get("end") or left_start)
    right_start = float(right.get("start") or 0)
    right_end = float(right.get("end") or right_start)
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    shorter_duration = min(left_end - left_start, right_end - right_start)
    if shorter_duration <= 0:
        return 0.0
    return overlap / shorter_duration


def _dedupe_words_across_segments(segments: list[dict]) -> list[dict]:
    """Remove duplicate words emitted by overlapping transcription windows.

    A repeated word is removed only when its timestamp substantially overlaps an
    already accepted occurrence. Sequential repetitions such as "very very"
    remain intact.
    """
    accepted_words: list[dict] = []
    cleaned_segments = []

    for raw_segment in sorted(segments, key=lambda item: float(item.get("start") or 0)):
        segment = copy.deepcopy(raw_segment)
        raw_words = segment.get("words")
        if not isinstance(raw_words, list):
            cleaned_segments.append(segment)
            continue

        kept_words = []
        for word in sorted(raw_words, key=lambda item: float(item.get("start") or 0)):
            normalized = _normalize_text(word.get("word") or word.get("text") or "")
            if not normalized:
                continue
            duplicate = any(
                _normalize_text(existing.get("word") or existing.get("text") or "")
                == normalized
                and _word_interval_overlap_ratio(existing, word) >= 0.5
                for existing in reversed(accepted_words)
                if float(existing.get("end") or existing.get("start") or 0)
                >= float(word.get("start") or 0) - 1.0
            )
            if duplicate:
                continue
            kept_words.append(word)
            accepted_words.append(word)

        if not kept_words:
            continue
        segment["words"] = kept_words
        segment["start"] = round(
            min(float(word.get("start") or 0) for word in kept_words), 3
        )
        segment["end"] = round(
            max(float(word.get("end") or 0) for word in kept_words), 3
        )
        segment["text"] = " ".join(
            str(word.get("word") or word.get("text") or "").strip()
            for word in kept_words
            if str(word.get("word") or word.get("text") or "").strip()
        )
        cleaned_segments.append(segment)

    return cleaned_segments


def merge_transcription_results(results: list[dict]) -> dict:
    segments = []
    for result in results:
        for segment in result.get("segments", []) or []:
            if not _is_duplicate_segment(segment, segments):
                segments.append(copy.deepcopy(segment))

    segments.sort(key=lambda item: float(item.get("start") or 0))
    for segment in segments:
        if isinstance(segment.get("words"), list):
            segment["words"] = _dedupe_words(segment["words"])
    segments = _dedupe_words_across_segments(segments)

    return {
        "text": " ".join(
            (segment.get("text") or "").strip()
            for segment in segments
            if (segment.get("text") or "").strip()
        ),
        "segments": segments,
    }


def probe_audio_duration(audio_path: str | Path) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(completed.stdout or "{}")
        return float(data.get("format", {}).get("duration") or 0)
    except Exception as exc:
        logger.warning("读取音频时长失败，回退到单段转录: %s", exc)
        return None


def extract_audio_chunk(
    audio_path: str | Path,
    output_path: str | Path,
    start_seconds: float,
    end_seconds: float,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-ss",
        f"{float(start_seconds):.3f}",
        "-to",
        f"{float(end_seconds):.3f}",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        str(MLX_SAMPLE_RATE),
        "-vn",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return output


def find_local_silero_repository(preferred: str | None = None) -> Path | None:
    """Find an existing checkout without torch.hub's remote validation/download."""
    candidates = [Path(preferred).expanduser()] if preferred else []
    candidates.append(LOCAL_SILERO_REPO)
    for candidate in list(candidates):
        if candidate.is_dir() and not (candidate / "hubconf.py").is_file():
            nested = [p for p in candidate.iterdir() if p.is_dir() and (p / "hubconf.py").is_file()]
            if len(nested) == 1:
                candidates.extend(nested)
    for candidate in candidates:
        if (candidate / "hubconf.py").is_file():
            return candidate
    try:
        import torch
        for candidate in sorted(Path(torch.hub.get_dir()).glob("snakers4_silero-vad_*")):
            if (candidate / "hubconf.py").is_file():
                return candidate
    except ImportError:
        pass
    return None


def detect_speech_ranges(
    audio_path: str | Path,
    threshold: float = 0.5,
    strict: bool = False,
    local_repo: str | Path | None = None,
) -> list[tuple[float, float]]:
    try:
        import torch
        import torchaudio

        waveform, sample_rate = torchaudio.load(str(audio_path))
        if waveform.ndim > 1:
            waveform = waveform.mean(dim=0)
        if sample_rate != MLX_SAMPLE_RATE:
            waveform = torchaudio.functional.resample(
                waveform, sample_rate, MLX_SAMPLE_RATE
            )
        waveform = waveform.float()
        repository = Path(local_repo) if local_repo is not None else LOCAL_SILERO_REPO
        if local_repo is not None and not (repository / "hubconf.py").is_file():
            raise RuntimeError("Configured local Silero repository is unavailable")
        if repository.is_dir() and (repository / "hubconf.py").is_file():
            logger.info("使用本地 Silero VAD: %s", repository)
            bundle = torch.hub.load(
                repo_or_dir=str(repository),
                model="silero_vad",
                source="local",
                force_reload=False,
                onnx=True,
                trust_repo=True,
            )
        else:
            bundle = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                onnx=True,
                trust_repo=True,
            )
        if not isinstance(bundle, (tuple, list)) or len(bundle) != 2:
            raise RuntimeError("Silero VAD must return a model and utilities")
        model, utils = bundle
        if not isinstance(utils, (tuple, list)) or not utils or not callable(utils[0]):
            raise RuntimeError("Silero VAD speech timestamp function is unavailable")
        get_speech_timestamps = utils[0]
        timestamps = get_speech_timestamps(
            waveform,
            model,
            sampling_rate=MLX_SAMPLE_RATE,
            threshold=float(threshold),
        )
        if not isinstance(timestamps, (list, tuple)):
            raise RuntimeError("Silero VAD timestamps must be a sequence")
        ranges = []
        for item in timestamps:
            if not isinstance(item, dict):
                raise RuntimeError("Invalid Silero VAD interval")
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise RuntimeError("Silero VAD interval must have numeric endpoints")
            if end > start:
                ranges.append((start / MLX_SAMPLE_RATE, end / MLX_SAMPLE_RATE))
        return ranges
    except Exception as exc:
        if strict:
            raise
        logger.warning("MLX VAD 检测失败，回退到普通分块: %s", exc)
        return []


def split_ranges_to_windows(
    ranges: list[tuple[float, float]],
    duration_seconds: float,
    chunk_duration_seconds: int,
    overlap_seconds: int,
    pad_seconds: float = 1.0,
) -> list[tuple[float, float, float, float]]:
    if not ranges:
        return build_chunk_windows(
            duration_seconds, chunk_duration_seconds, overlap_seconds
        )

    duration = max(0.0, float(duration_seconds or 0))
    padded_ranges = sorted(
        (
            max(0.0, float(start) - pad_seconds),
            min(duration, float(end) + pad_seconds),
        )
        for start, end in ranges
        if float(end) > float(start)
    )
    merged_ranges: list[list[float]] = []
    for start, end in padded_ranges:
        if not merged_ranges or start > merged_ranges[-1][1]:
            merged_ranges.append([start, end])
        else:
            merged_ranges[-1][1] = max(merged_ranges[-1][1], end)

    windows = []
    for padded_start, padded_end in merged_ranges:
        for local_start, local_end, keep_start, keep_end in build_chunk_windows(
            padded_end - padded_start,
            chunk_duration_seconds,
            overlap_seconds,
        ):
            windows.append(
                (
                    round(padded_start + local_start, 3),
                    round(padded_start + local_end, 3),
                    round(padded_start + keep_start, 3),
                    round(padded_start + keep_end, 3),
                )
            )
    return windows
