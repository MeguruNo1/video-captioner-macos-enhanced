"""Deterministic word anchoring and subtitle validation; no language model calls."""
import math
import re
from uuid import uuid4

from app.core.utils.profanity_filter import mask_english_profanity
from app.core.utils.subtitle_punctuation import normalize_cjk_quotes

BATCH_WORDS = 160


def has_collapsed_word_run(words):
    """Distinguish consecutive collapsed timings from isolated zero-length words."""
    previous_zero = False
    for word in words:
        zero = word["start_ms"] == word["end_ms"]
        if zero and previous_zero:
            return True
        previous_zero = zero
    return False


def words_from_result(result, prefix="w", offset=0):
    words = []
    for segment in result.get("segments", []):
        raw_words = segment.get("words") or []
        if not raw_words and str(segment.get("text", "")).strip():
            raise ValueError("ASR returned speech without word timestamps; retranscribe before captioning")
        for raw in raw_words:
            text = str(raw.get("word") or raw.get("text") or "").strip()
            if not text:
                continue
            start, end = raw.get("start"), raw.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise ValueError("ASR word is missing timestamps")
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("ASR word has non-finite timestamps")
            words.append({"id": f"{prefix}{len(words):06d}", "text": text,
                          "start_ms": round((start + offset) * 1000),
                          "end_ms": round((end + offset) * 1000),
                          **({"alignment_score": raw["score"]} if "score" in raw else {})})
    if not words:
        raise ValueError("No speech detected; check the audio or choose a different source language")
    return words


def make_batches(words):
    batches = []
    cursor = 0
    while cursor < len(words):
        stop = min(cursor + BATCH_WORDS, len(words))
        if stop < len(words):
            for index in range(stop - 1, cursor + BATCH_WORDS // 2, -1):
                if re.search(r"(?<!\.)[.!?。！？][\"'”’」』]*$", words[index]["text"]):
                    stop = index + 1
                    break
        batches.append({"id": uuid4().hex, "start_word_id": words[cursor]["id"],
                        "end_word_id": words[stop - 1]["id"], "captions": None,
                        "glossary": {}, "notes": []})
        cursor = stop
    return batches


def batch_words(state, batch):
    ids = [word["id"] for word in state["words"]]
    first, last = ids.index(batch["start_word_id"]), ids.index(batch["end_word_id"])
    return state["words"][first:last + 1]


def anchor_captions(words, captions):
    if not captions:
        raise ValueError("A batch must contain captions")
    index = {word["id"]: i for i, word in enumerate(words)}
    cursor = 0
    anchored = []
    for caption in captions:
        start = index.get(caption.get("start_word_id"))
        end = index.get(caption.get("end_word_id"))
        if start != cursor or end is None or end < start:
            raise ValueError("Caption word ranges must cover this batch exactly once, in order")
        source, translation = caption.get("source", ""), caption.get("translation", "")
        if not isinstance(source, str) or not source.strip() or not isinstance(translation, str) or not translation.strip():
            raise ValueError("Every caption requires nonempty source and translation")
        selected = words[start:end + 1]
        item = {"start_word_id": words[start]["id"], "end_word_id": words[end]["id"],
                "source": " ".join(source.split()), "translation": " ".join(translation.split()),
                "start_ms": selected[0]["start_ms"], "end_ms": selected[-1]["end_ms"]}
        if item["start_ms"] < 0 or item["end_ms"] <= item["start_ms"]:
            raise ValueError("Invalid caption timing; use retranscribe_range")
        anchored.append(item)
        cursor = end + 1
    if cursor != len(words):
        raise ValueError("Batch has uncovered words")
    return anchored


def apply_text_settings(captions, settings):
    """Apply the same final text switches used by the desktop subtitle flow."""
    subtitle = (settings or {}).get("subtitle") or {}
    target = str(subtitle.get("target_language_code") or subtitle.get("target_language") or "").lower()
    chinese_target = target.startswith("zh") or target in {"中文", "简体中文", "繁体中文", "粤语"}
    result = []
    for caption in captions:
        item = dict(caption)
        if subtitle.get("mask_original_profanity"):
            item["source"] = mask_english_profanity(item["source"])
        if chinese_target and subtitle.get("remove_translated_chinese_commas"):
            item["translation"] = re.sub(
                r"\s+", " ", item["translation"].replace("，", " ").replace(",", " ")
            ).strip()
        if subtitle.get("remove_translated_periods"):
            item["translation"] = item["translation"].replace("。", "")
        if chinese_target:
            item["translation"] = normalize_cjk_quotes(item["translation"])
        result.append(item)
    return result


def extend_export_captions(captions, duration_ms=None, extension_ms=500):
    """Extend display only; never move starts, shrink ends, or mutate anchors."""
    result = [dict(caption) for caption in captions]
    for index, caption in enumerate(result):
        limit = result[index + 1]["start_ms"] if index + 1 < len(result) else duration_ms
        if duration_ms is not None:
            limit = min(limit, duration_ms) if limit is not None else duration_ms
        end = caption["end_ms"] + extension_ms
        caption["end_ms"] = max(caption["end_ms"], min(end, limit) if limit is not None else end)
    return result


def validate(state, limit=100):
    errors, warnings = [], []
    if not state.get("words"):
        errors.append("No transcript available")
    duration = state.get("duration_ms")
    previous_start = -1
    for word in state.get("words", []):
        start, end = word["start_ms"], word["end_ms"]
        if start < 0 or end < start or start < previous_start or (duration and end > duration + 100):
            errors.append(f"Invalid word timing: {word['id']}")
        elif end == start:
            warnings.append(f"Zero-duration ASR word; review its containing caption: {word['id']}")
        if end - start > 3000:
            warnings.append(f"Unusually long word: {word['id']}")
        score = word.get("alignment_score")
        if isinstance(score, (int, float)) and score < 0.3:
            warnings.append(f"Low alignment confidence; review audio: {word['id']}")
        previous_start = start
    word_index = {word["id"]: word for word in state.get("words", [])}
    previous_end = -1
    previous_caption = None
    for batch in state.get("batches", []):
        if not batch["captions"]:
            errors.append(f"Untranslated batch: {batch['id']}")
            continue
        try:
            captions = anchor_captions(batch_words(state, batch), batch["captions"])
        except ValueError as exc:
            errors.append(f"{batch['id']}: {exc}")
            continue
        for caption in captions:
            start, end = caption["start_ms"], caption["end_ms"]
            first_word = word_index[caption["start_word_id"]]
            if first_word["end_ms"] - first_word["start_ms"] > 800:
                warnings.append(f"Long caption-initial word; check early onset: {first_word['id']}")
            if start < previous_end:
                errors.append(f"Overlapping captions at {caption['start_word_id']}")
            if previous_caption and 0 <= start - previous_end <= 300:
                for field in ("source", "translation"):
                    if (re.search(r"(?:\.{3,}|…+)\s*$", previous_caption[field])
                            and re.match(r"\s*(?:\.{3,}|…+)", caption[field])):
                        warnings.append(
                            f"Review artificial ellipsis split: {previous_caption['start_word_id']} -> "
                            f"{caption['start_word_id']}; continuous speech may need one caption. "
                            "Batch boundaries do not justify ellipses."
                        )
                        break
            text = caption["translation"]
            cjk = bool(re.search(r"[\u3400-\u9fff]", text))
            units = len(re.sub(r"\s", "", text))
            if end - start > 7000 or units > (42 if cjk else 84) or units / ((end-start)/1000) > (12 if cjk else 22):
                warnings.append(f"Review reading length/speed: {caption['start_word_id']}")
            previous_end = end
            previous_caption = caption
        warnings.extend(f"{batch['id']}: {note}" for note in batch.get("notes", []))
    return {"valid": not errors, "errors": errors[:limit], "warnings": warnings[:limit],
            "error_count": len(errors), "warning_count": len(warnings),
            "truncated": limit is not None and (len(errors) > limit or len(warnings) > limit)}


def timestamp(ms):
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render_srt(captions, layout):
    blocks = []
    for index, caption in enumerate(captions, 1):
        text = "\n".join(caption[key] for key in layout)
        blocks.append(f"{index}\n{timestamp(caption['start_ms'])} --> {timestamp(caption['end_ms'])}\n{text}\n")
    return "\n".join(blocks)
