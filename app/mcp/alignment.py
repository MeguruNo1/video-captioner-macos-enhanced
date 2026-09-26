"""Whole-transcript timing migration without changing word IDs or translations."""
from copy import deepcopy

from .captions import anchor_captions, batch_words, validate, words_from_result


def alignment_segments(state):
    """Use batch-sized acoustic windows, allowing pauses before spoken words."""
    segments = []
    words = state["words"]
    indices = {word["id"]: i for i, word in enumerate(words)}
    for batch in state["batches"]:
        first = indices[batch["start_word_id"]]
        last = indices[batch["end_word_id"]] + 1
        # Include spoken context, not just extra audio. Otherwise the aligner can
        # assign the previous phrase's sounds to the first word of this batch.
        left, right = max(0, first - 12), min(len(words), last + 12)
        context = words[left:right]
        segments.append({"start": max(0, context[0]["start_ms"] / 1000 - .5),
                         "end": min(state["duration_ms"] / 1000, context[-1]["end_ms"] / 1000 + .5),
                         "text": " ".join(word["text"] for word in context),
                         "word_count": len(context), "global_start": left, "split_index": first})
    return segments


def remove_alignment_context(result, segments):
    """Drop repeated context words, never blend or average their timestamps."""
    words = [word for segment in result.get("segments", []) for word in segment.get("words", [])]
    if len(words) != sum(segment["word_count"] for segment in segments):
        raise ValueError("Alignment changed context coverage; original captions retained")
    cursor = 0
    kept = []
    for segment in segments:
        chunk = words[cursor:cursor + segment["word_count"]]
        cursor += segment["word_count"]
        if not kept:
            kept = chunk
            continue
        left = segment["global_start"]
        # Both windows contain the seam's words. Choose an actual non-overlapping
        # acoustic boundary near the batch edge; never clamp or average timings.
        choices = [i for i in range(max(1, left), min(len(kept), left + len(chunk)) + 1)
                   if i - left < len(chunk) and kept[i-1]["end"] <= chunk[i-left]["start"]]
        if not choices:
            raise ValueError("No acoustic splice boundary; original captions retained")
        split = min(choices, key=lambda i: abs(i - segment["split_index"]))
        kept = kept[:split] + chunk[split-left:]
    return {"segments": [{"words": kept}], "language": result.get("language")}


def reanchor_alignment(state, result):
    """Reject incomplete/reordered alignment before modifying any saved caption."""
    aligned = words_from_result(result)
    original = state["words"]
    if len(aligned) != len(original) or any(a["text"] != b["text"] for a, b in zip(aligned, original)):
        raise ValueError("Alignment changed word coverage; original words and captions retained")
    candidate = deepcopy(state)
    candidate["words"] = [dict(word, id=old["id"]) for old, word in zip(original, aligned)]
    for batch in candidate["batches"]:
        if batch["captions"]:
            batch["captions"] = anchor_captions(batch_words(candidate, batch), batch["captions"])
    # Validate timing separately from pending translations so a long list of
    # untranslated batches cannot hide structural errors behind response limits.
    timing_state = dict(candidate, batches=[b for b in candidate["batches"] if b["captions"]])
    report = validate(timing_state)
    errors = report["errors"]
    if errors:
        raise ValueError("Alignment failed validation; original captions retained: " + "; ".join(errors[:5]))
    return candidate
