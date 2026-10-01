"""Splicing must retain natural repetitions and never guess boundary timing."""
import pytest
import json
from pathlib import Path

from app.mcp.retranscription import SpliceWord, select_splice_words
from app.mcp.captions import make_batches
from app.mcp.worker import Worker


def word(text: str, start: int, end: int) -> SpliceWord:
    return {"id": f"w{start}", "text": text, "start_ms": start, "end_ms": end}


def test_context_trim_preserves_repeated_target_word_and_ids():
    words = [word("very", 0, 500), word("very", 600, 900), word("good", 1000, 1400)]
    assert select_splice_words(words, 500, 1000, 0, 2000) == [words[1]]


def test_complete_neighbor_tail_is_removed_by_acoustics_only():
    words = [word("virtues.", 200, 950), word("issues.", 1100, 1300)]
    assert select_splice_words(words, 1000, 1800, 0, 2000) == [words[1]]


def test_real_sample_neighbor_virtues_tail_is_not_imported():
    before = [word("seven", 2273865, 2274167), word("virtues.", 2274228, 2274550)]
    decoded = [word("Seven", 2273851, 2274151), word("Virtues.", 2274211, 2274671),
               word("Though", 2275332, 2275512)]
    assert select_splice_words(decoded, 2274550, 2337251, 2272550, 2339251,
                               before=before) == [decoded[-1]]


@pytest.mark.parametrize("mode", ["soundalike", "far_anchor", "only_one", "inside_repeat"])
def test_neighbor_matching_cannot_silently_remove_uncertain_words(mode):
    before = [word("seven", 200, 500), word("virtues.", 550, 1000)]
    decoded = [word("Seven", 210, 510), word("Virtues.", 560, 1100), word("Though", 1200, 1500)]
    if mode == "soundalike":
        decoded[1]["text"] = "issues."
    elif mode == "far_anchor":
        before[0]["start_ms"] = 0
        before[1]["start_ms"] = 200
    elif mode == "only_one":
        before = before[1:]
    else:
        decoded[1]["start_ms"] = 1000
        assert select_splice_words(decoded, 1000, 1800, 0, 2000, before=before) == decoded[1:]
        return
    with pytest.raises(ValueError, match="crosses"):
        select_splice_words(decoded, 1000, 1800, 0, 2000, before=before)


def test_right_boundary_neighbor_matching():
    after = [word("then", 1800, 1900), word("another", 1950, 2100)]
    decoded = [word("end", 1200, 1600), word("Then", 1750, 1900), word("another", 1940, 2110)]
    assert select_splice_words(decoded, 1000, 1800, 0, 2300, after=after) == [decoded[0]]


@pytest.mark.parametrize("words", [
    [word("virtues.", 800, 1100), word("Next", 1200, 1500)],
    [word("Last", 1500, 1900)],
    [word("Zero", 1000, 1000)],
    [word("Zero", 1800, 1800)],
    [word("Negative", -1, 100)],
    [word("Outside", 1700, 2100)],
    [word("Backwards", 1500, 1300)],
    [word("Later", 1400, 1500), word("Earlier", 1100, 1300)],
    [word("Nested", 1100, 1500), word("EarlierEnd", 1200, 1400)],
    [word("ContextOnly", 100, 900)],
])
def test_ambiguous_splice_is_rejected_without_mutating_words(words):
    original = [dict(w) for w in words]
    with pytest.raises(ValueError):
        select_splice_words(words, 1000, 1800, 0, 2000)
    assert words == original


@pytest.mark.parametrize("crosses", [False, True])
def test_worker_uses_context_offset_and_retains_state_on_crossing(tmp_path, monkeypatch, crosses):
    worker = Worker(tmp_path, "a" * 32, "test")
    words = [word("before", 4000, 5000), word("target", 5100, 6000),
             word("after", 7000, 7500)]
    batches = [make_batches([w])[0] for w in words]
    state = {"job_id": "a" * 32, "worker": {"token": "test"}, "revision": 2,
             "words": words, "batches": batches, "duration_ms": 10000,
             "retranscribe": {"batch_ids": [batches[1]["id"]]}}
    worker.store.save(state)
    chunks = []
    def extract(audio, destination, start, end):
        chunks.append((start, end))
        return destination
    monkeypatch.setattr("app.core.bk_asr.mlx_workflow.extract_audio_chunk", extract)
    raw = {"segments": [{"words": [
        {"word": "before", "start": 1, "end": 2.1 if crosses else 2},
        {"word": "replacement", "start": 2.2, "end": 3},
        {"word": "after", "start": 4, "end": 4.5},
    ]}]}
    monkeypatch.setattr(worker, "transcribe", lambda *args: raw)
    if crosses:
        with pytest.raises(ValueError, match="original captions retained"):
            worker.retranscribe(state, tmp_path, Path("unused.wav"), {})
        updated = worker.store.read("a" * 32)
        for key in ("words", "batches", "revision", "retranscribe"):
            assert updated[key] == state[key]
    else:
        worker.retranscribe(state, tmp_path, Path("unused.wav"), {})
        updated = worker.store.read("a" * 32)
        assert updated["words"][0] == words[0]
        assert updated["words"][-1] == words[-1]
        assert updated["words"][1]["start_ms"] == 5200
        assert updated["words"][1]["end_ms"] == 6000
        assert updated["revision"] == 3
    assert chunks == [(3, 9)]
    diagnostic = json.loads(next(tmp_path.glob("transcript-*.json")).read_text())
    assert diagnostic["result"] == raw
    assert diagnostic["context_start_seconds"] == 3
