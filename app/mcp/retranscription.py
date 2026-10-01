"""Conservative acoustic boundaries for contextual retranscription."""
from typing import TypedDict
import re


class SpliceWord(TypedDict):
    id: str
    text: str
    start_ms: int
    end_ms: int


def _same_neighbor(candidate: list[SpliceWord], original: list[SpliceWord]) -> bool:
    """Require two exact tokens and nearby acoustic anchors, not soundalikes."""
    if len(candidate) != 2 or len(original) != 2:
        return False
    for new, old in zip(candidate, original):
        new_text = re.sub(r"^\W+|\W+$", "", new["text"].casefold())
        old_text = re.sub(r"^\W+|\W+$", "", old["text"].casefold())
        if (not new_text or new_text != old_text
                or abs(new["start_ms"] - old["start_ms"]) > 250
                or abs(new["end_ms"] - old["end_ms"]) > 250):
            return False
    return True


def select_splice_words(
    words: list[SpliceWord], start_ms: int, end_ms: int,
    context_start_ms: int, context_end_ms: int,
    *, before: list[SpliceWord] | None = None, after: list[SpliceWord] | None = None,
) -> list[SpliceWord]:
    """Keep whole words within the original splice, or refuse an unsafe edit.

    A boundary-straddling word may be the previously clipped neighbor or real
    target speech. Only an exact pair of preserved neighbors with close acoustic
    anchors can establish its identity; otherwise require a wider batch range.
    Never clamp or rewrite timestamps.
    """
    selected: list[SpliceWord] = []
    previous_start = context_start_ms
    previous_end = context_start_ms
    for index, word in enumerate(words):
        start, end = word["start_ms"], word["end_ms"]
        if (start < previous_start or end < previous_end or end < start
                or start < context_start_ms or end > context_end_ms):
            raise ValueError("Retranscription returned invalid or out-of-order acoustic anchors")
        previous_start, previous_end = start, end
        # A zero-length word on either boundary is ambiguous, not safe context.
        if start == end and start in (start_ms, end_ms):
            raise ValueError("Retranscription returned an ambiguous zero-duration boundary word")
        if end <= start_ms or start >= end_ms:
            continue
        if start < start_ms or end > end_ms:
            # Permit a small re-alignment of an intact preserved neighbor only
            # when both its identity and adjacent acoustic anchors agree. This
            # never removes repeated target words wholly inside the splice.
            if (start < start_ms < end <= end_ms and before
                    and _same_neighbor(words[max(0, index - 1):index + 1], before[-2:])):
                continue
            if (start_ms <= start < end_ms < end and after
                    and _same_neighbor(words[index:index + 2], after[:2])):
                continue
            raise ValueError(f"Retranscribed word {word['text']!r} crosses a splice boundary")
        selected.append(word)
    if not selected:
        raise ValueError("Retranscription contains no speech inside the replacement window")
    return selected
