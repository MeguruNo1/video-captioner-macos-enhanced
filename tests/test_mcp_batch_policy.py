from copy import deepcopy

import pytest

from app.mcp.captions import ADAPTIVE_BATCH_POLICY, batch_words, make_batches


def words(count=400, text='word'):
    return [{'id': f'w{i:06}', 'text': text, 'start_ms': i*200, 'end_ms': i*200+180} for i in range(count)]


def test_adaptive_extends_to_nearby_sentence_and_preserves_legacy_behavior():
    data = words()
    data[179]['text'] = 'end.'
    original = deepcopy(data)
    assert make_batches(data)[0]['end_word_id'] == 'w000159'
    batches = make_batches(data, ADAPTIVE_BATCH_POLICY)
    assert batches[0]['end_word_id'] == 'w000179'
    assert batches[0]['boundary_reason'] == 'sentence'
    assert data == original
    state = {'words': data}
    assert [w for b in batches for w in batch_words(state, b)] == data
    assert max(len(batch_words(state, b)) for b in batches) <= 240


def test_adaptive_prefers_pause_to_cutting_continuous_speech():
    data = words()
    for word in data[150:]:
        word['start_ms'] += 800
        word['end_ms'] += 800
    batch = make_batches(data, ADAPTIVE_BATCH_POLICY)[0]
    assert batch['end_word_id'] == 'w000149' and batch['boundary_reason'] == 'pause'


def test_adaptive_bounds_long_tokens_and_slow_speech():
    dense = words(text='x'*600)
    batches = make_batches(dense, ADAPTIVE_BATCH_POLICY)
    assert all(len(batch_words({'words': dense}, b)) <= 16 for b in batches)
    slow = words()
    for i, word in enumerate(slow):
        word.update(start_ms=i*4000, end_ms=i*4000+3000)
    batches = make_batches(slow, ADAPTIVE_BATCH_POLICY)
    for b in batches:
        selected = batch_words({'words': slow}, b)
        assert selected[-1]['end_ms'] - selected[0]['start_ms'] <= 90000
    assert [w for b in batches for w in batch_words({'words': slow}, b)] == slow


def test_adaptive_keeps_last_sentence_together_and_rejects_unknown_policy():
    data = words(190)
    assert len(make_batches(data, ADAPTIVE_BATCH_POLICY)) == 1
    with pytest.raises(ValueError, match='Unsupported'):
        make_batches(data, dict(ADAPTIVE_BATCH_POLICY, version=2))
