#!/usr/bin/env python3
"""Offline deterministic MCP interaction benchmark; no ASR/model/network calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.mcp.captions import ADAPTIVE_BATCH_POLICY, batch_words, make_batches
from app.mcp.jobs import JobManager


def sample_words(count):
    return [{"id": f"w{i:06}", "text": "sentence." if i % 23 == 22 else f"term{i % 40}",
             "start_ms": i * 400, "end_ms": i * 400 + 350} for i in range(count)]


def run_case(root, words, optimized):
    manager = JobManager(root)
    job_id = uuid4().hex
    state = {"job_id": job_id, "directory": str(root), "status": "awaiting_captions", "stage": "awaiting_captions",
             "revision": 1, "progress": 100, "message": "", "error": None, "created_at": 0,
             "words": words, "batches": make_batches(words), "artifacts": {}, "glossary": {},
             "base_glossary": {f"term{i}": f"术语{i}" for i in range(200)},
             "term_candidates": [f"term{i}" for i in range(200)],
             "duration_ms": len(words) * 400, "metadata": {"title": "Offline benchmark", "description": "Background. " * 160},
             "options": {"source_language": "en", "target_language": "zh-CN"}}
    state['glossary'] = dict(state['base_glossary'])
    manager.store.save(state)
    calls, response_bytes = 0, 0
    def call(method, *args, **kwargs):
        nonlocal calls, response_bytes
        result = method(*args, **kwargs)
        calls += 1
        response_bytes += len(json.dumps(result, ensure_ascii=False).encode())
        return result
    start = time.perf_counter()
    if optimized:
        call(manager.get_job_context, job_id)
    batch = call(manager.get_caption_batch, job_id, compact=optimized)
    while not batch.get('done'):
        selected = batch['words']
        captions = []
        for cursor in range(0, len(selected), 8):
            span = selected[cursor:cursor+8]
            captions.append({'start_word_id': span[0]['id'], 'end_word_id': span[-1]['id'],
                             'source': ' '.join(w['text'] for w in span), 'translation': '用于检查接口的示例译文'})
        result = call(manager.submit_caption_batch, job_id, batch['batch_id'], batch['revision'], captions,
                      return_next_batch=optimized, compact=optimized)
        batch = result['next_batch'] if optimized else call(manager.get_caption_batch, job_id)
    saved = manager.store.read(job_id)
    return {"calls": calls, "response_bytes": response_bytes, "batches": len(saved['batches']),
            "fixture_elapsed_seconds": round(time.perf_counter() - start, 3),
            "captions": [c for b in saved['batches'] for c in b['captions']]}


def benchmark(word_count=3000):
    words = sample_words(word_count)
    with tempfile.TemporaryDirectory(prefix='videocaptioner-benchmark-') as temporary:
        old = run_case(Path(temporary)/'separate', words, False)
        new = run_case(Path(temporary)/'chained', words, True)
    assert old.pop('captions') == new.pop('captions'), 'Interaction optimization changed captions'
    boundary = {}
    for name, policy in [('legacy', None), ('adaptive', ADAPTIVE_BATCH_POLICY)]:
        batches = make_batches(words, policy)
        assert [w for b in batches for w in batch_words({'words': words}, b)] == words
        stops = {words[-1]['id']} | {w['id'] for w in words if w['text'].endswith('.')}
        boundary[name] = {'batches': len(batches), 'cuts_inside_sentences': sum(b['end_word_id'] not in stops for b in batches)}
    return {'scope': 'Offline fixture; excludes ASR, Codex inference, image generation and network latency',
            'words': word_count, 'separate_full_context': old, 'chained_compact_context': new,
            'response_reduction_percent': round(100 * (1 - new['response_bytes']/old['response_bytes']), 1),
            'adaptive_boundaries': boundary, 'captions_identical': True, 'word_coverage_exact': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--words', type=int, default=3000, help='Number of deterministic fixture words (at least 240)')
    args = parser.parse_args()
    if args.words < 240:
        parser.error('--words must be at least 240')
    print(json.dumps(benchmark(args.words), ensure_ascii=False, indent=2))
