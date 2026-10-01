"""Deterministic word anchoring and subtitle validation; no language model calls."""
import hashlib
import json
import math
import re
from uuid import uuid4

from app.core.utils.profanity_filter import mask_english_profanity
from app.core.utils.subtitle_punctuation import normalize_cjk_quotes

BATCH_WORDS = 160
ADAPTIVE_BATCH_POLICY = {"version": 1, "target_words": 160, "min_words": 80,
                         "max_words": 240, "max_estimated_chars": 32000,
                         "max_duration_ms": 90000, "pause_ms": 650}
SENTENCE_END = re.compile(r"(?<!\.)[.!?。！？][\"'”’」』]*$")


def has_collapsed_word_run(words):
    """Distinguish consecutive collapsed timings from isolated zero-length words."""
    previous_zero = False
    for word in words:
        zero = word["start_ms"] == word["end_ms"]
        if zero and previous_zero:
            return True
        previous_zero = zero
    return False


def words_from_result(result, prefix="w", offset: float = 0):
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


def _adaptive_stop(words, cursor, policy):
    if policy != ADAPTIVE_BATCH_POLICY:
        raise ValueError("Unsupported caption batch policy; preserve the saved version")
    hard_stop = min(len(words), cursor + policy["max_words"])
    estimate = 0
    for index in range(cursor, hard_stop):
        estimate += 120 + 3 * len(words[index]["text"])
        duration = words[index].get("end_ms", 0) - words[cursor].get("start_ms", 0)
        if index > cursor and (estimate > policy["max_estimated_chars"] or duration > policy["max_duration_ms"]):
            hard_stop = index
            break
    candidates = []
    lower = min(hard_stop, cursor + policy["min_words"])
    for stop in range(lower, hard_stop + 1):
        count = stop - cursor
        score = -abs(count - policy["target_words"])
        if stop == len(words):
            candidates.append((score + 110, stop, "end"))
        elif SENTENCE_END.search(words[stop - 1]["text"]):
            candidates.append((score + 100, stop, "sentence"))
        elif words[stop].get("start_ms", 0) - words[stop - 1].get("end_ms", 0) >= policy["pause_ms"]:
            candidates.append((score + 50, stop, "pause"))
    if candidates:
        _, stop, reason = max(candidates)
        return stop, reason
    return min(hard_stop, cursor + policy["target_words"]), "budget"


def make_batches(words, policy=None):
    batches = []
    cursor = 0
    while cursor < len(words):
        stop, reason = _adaptive_stop(words, cursor, policy) if policy else (min(cursor + BATCH_WORDS, len(words)), "legacy")
        if not policy and stop < len(words):
            for index in range(stop - 1, cursor + BATCH_WORDS // 2, -1):
                if SENTENCE_END.search(words[index]["text"]):
                    stop = index + 1
                    break
        batches.append({"id": uuid4().hex, "start_word_id": words[cursor]["id"],
                        "end_word_id": words[stop - 1]["id"], "captions": None,
                        "glossary": {}, "notes": [], "boundary_reason": reason})
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
        if start is None or start != cursor or end is None or end < start:
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


def review_issue(code, severity, message, words=(), *, stage="transcript", batch_id=None,
                 evidence=None, start_ms=None, end_ms=None):
    """Content-addressed issues: a changed observation must be reviewed again."""
    evidence = evidence or {}
    start = start_ms if start_ms is not None else (words[0]["start_ms"] if words else None)
    end = end_ms if end_ms is not None else (words[-1]["end_ms"] if words else None)
    fingerprint = json.dumps([code, words, evidence, start, end], sort_keys=True, ensure_ascii=False)
    return {"id": hashlib.sha256(fingerprint.encode()).hexdigest()[:24], "code": code,
            "severity": severity, "stage": stage, "message": message,
            "start_word_id": words[0]["id"] if words else None,
            "end_word_id": words[-1]["id"] if words else None,
            "start_ms": start, "end_ms": end, "batch_id": batch_id,
            "suggested_action": "review_audio" if stage == "transcript" else "revise_caption",
            "evidence": evidence}


def validate(state, limit: int | None = 100, phase="delivery", batch_id=None):
    """Separate transcript, local caption and full delivery checks."""
    if phase not in {"transcript", "batch", "delivery"}:
        raise ValueError("Unknown validation phase")
    issues = []
    words = state.get("words", [])
    batches = state.get("batches", [])
    if phase == "batch":
        position = next((i for i, b in enumerate(batches) if b["id"] == batch_id), None)
        if position is None:
            raise ValueError("Unknown batch ID")
        batches = batches[max(0, position - 1):position + 2]
        # Include neighboring saved captions, so both crossing edges are checked.
        first, last = batch_words(state, batches[0])[0], batch_words(state, batches[-1])[-1]
        ids = {w["id"]: i for i, w in enumerate(words)}
        words = words[ids[first["id"]]:ids[last["id"]] + 1]
    if not words:
        issues.append(review_issue("no_transcript", "error", "No transcript available"))
    duration = state.get("duration_ms")
    previous_start = -1
    low_run = []
    def flush_low_run():
        if low_run:
            issues.append(review_issue("low_alignment", "warning",
                f"Low alignment confidence; review audio: {low_run[0]['id']} -> {low_run[-1]['id']}",
                list(low_run)))
            low_run.clear()
    for word in words:
        start, end = word["start_ms"], word["end_ms"]
        if start < 0 or end < start or start < previous_start or (duration and end > duration + 100):
            issues.append(review_issue("invalid_word_timing", "error", f"Invalid word timing: {word['id']}", [word]))
        elif end == start:
            issues.append(review_issue("zero_duration", "warning",
                f"Zero-duration ASR word; review its containing caption: {word['id']}", [word]))
        if end - start > 3000:
            issues.append(review_issue("long_word", "warning", f"Unusually long word: {word['id']}", [word]))
        score = word.get("alignment_score")
        if isinstance(score, (int, float)) and score < 0.3:
            if low_run and start - low_run[-1]["end_ms"] > 500:
                flush_low_run()
            low_run.append(word)
        else:
            flush_low_run()
        previous_start = start
    flush_low_run()
    word_index = {word["id"]: word for word in words}
    previous_end = -1
    previous_caption = None
    for batch in ([] if phase == "transcript" else batches):
        if not batch["captions"]:
            if phase == "delivery":
                issues.append(review_issue("untranslated_batch", "error", f"Untranslated batch: {batch['id']}",
                    batch_words(state, batch), stage="delivery", batch_id=batch["id"]))
            previous_end, previous_caption = -1, None
            continue
        selected = batch_words(state, batch)
        try:
            captions = anchor_captions(selected, batch["captions"])
        except ValueError as exc:
            issues.append(review_issue("invalid_caption", "error", f"{batch['id']}: {exc}", selected,
                stage="caption", batch_id=batch["id"], evidence={"captions": batch["captions"]}))
            previous_end, previous_caption = -1, None
            continue
        for caption in captions:
            start, end = caption["start_ms"], caption["end_ms"]
            first_word = word_index[caption["start_word_id"]]
            last_word = word_index[caption["end_word_id"]]
            span = [first_word, last_word] if first_word != last_word else [first_word]
            def add(code, severity, message, evidence=None):
                issues.append(review_issue(code, severity, message, span, stage="caption",
                    batch_id=batch["id"], evidence=evidence or {"caption": caption}))
            if first_word["end_ms"] - first_word["start_ms"] > 800:
                add("early_onset", "warning", f"Long caption-initial word; check early onset: {first_word['id']}")
            if start < previous_end:
                add("overlapping_captions", "error", f"Overlapping captions at {caption['start_word_id']}",
                    {"previous": previous_caption, "caption": caption})
            if previous_caption and 0 <= start - previous_end <= 300:
                for field in ("source", "translation"):
                    if (re.search(r"(?:\.{3,}|…+)\s*$", previous_caption[field])
                            and re.match(r"\s*(?:\.{3,}|…+)", caption[field])):
                        add("artificial_ellipsis", "warning",
                            f"Review artificial ellipsis split: {previous_caption['start_word_id']} -> "
                            f"{caption['start_word_id']}; continuous speech may need one caption. "
                            "Batch boundaries do not justify ellipses.",
                            {"previous": previous_caption, "caption": caption})
                        break
            text = caption["translation"]
            cjk = bool(re.search(r"[\u3400-\u9fff]", text))
            units = len(re.sub(r"\s", "", text))
            if end - start > 7000 or units > (42 if cjk else 84) or units / ((end-start)/1000) > (12 if cjk else 22):
                add("reading_speed", "warning", f"Review reading length/speed: {caption['start_word_id']}")
            previous_end, previous_caption = end, caption
        for note in batch.get("notes", []):
            issues.append(review_issue("caption_note", "warning", f"{batch['id']}: {note}", selected,
                stage="caption", batch_id=batch["id"], evidence={"note": note}))
    errors = [issue["message"] for issue in issues if issue["severity"] == "error"]
    warnings = [issue["message"] for issue in issues if issue["severity"] == "warning"]
    return {"valid": not errors, "phase": phase, "errors": errors[:limit], "warnings": warnings[:limit],
            "issues": issues[:limit], "issue_count": len(issues),
            "error_count": len(errors), "warning_count": len(warnings),
            "pending_batches": sum(not b["captions"] for b in state.get("batches", [])),
            "truncated": limit is not None and len(issues) > limit}


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
