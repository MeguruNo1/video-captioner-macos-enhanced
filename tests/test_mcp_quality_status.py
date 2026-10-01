"""Delivery quality is separate from file export and whole-video listening."""
import json
from pathlib import Path

from app.mcp.captions import make_batches, validate
from app.mcp.review import quality_status, reconcile_review
from test_mcp_workflow import job, payload


def caption_state(text, end=1000, next_start=None):
    words = [{"id": "w0", "text": "Alternatively", "start_ms": 0, "end_ms": end}]
    if next_start is not None:
        words.append({"id": "w1", "text": "next", "start_ms": next_start, "end_ms": next_start + 400})
    batches = []
    for word in words:
        batch = make_batches([word])[0]
        batch['captions'] = [{"start_word_id": word['id'], "end_word_id": word['id'],
                              "source": word['text'], "translation": text if not batches else "好"}]
        batches.append(batch)
    return {"words": words, "batches": batches, "duration_ms": 10000,
            "revision": 1, "status": "awaiting_captions"}


def reading_issues(state):
    return [i for i in validate(state)['issues'] if i['code'] == 'reading_speed']


def test_reading_speed_matches_export_extension_and_next_batch():
    state = caption_state('中' * 16)
    assert not reading_issues(state)  # 16 / 1.5 sec, not 16 / 1 sec.
    state = caption_state('中' * 16, next_start=1100)
    issue = reading_issues(state)[0]
    assert issue['evidence']['display_duration_ms'] == 1100
    assert issue['evidence']['reasons'] == ['fast_reading']
    state['batches'][1]['captions'] = None
    assert reading_issues(state)[0]['id'] == issue['id']
    state = caption_state('中' * 16)
    state['duration_ms'] = 1100
    assert reading_issues(state)[0]['evidence']['display_end_ms'] == 1100


def test_mixed_latin_and_punctuation_do_not_count_as_full_cjk():
    assert not reading_issues(caption_state('测试，Alternatively！'))
    assert reading_issues(caption_state('中' * 40, end=400))


def test_onset_allows_long_words_but_keeps_weak_or_extreme_alignment():
    state = caption_state('好')
    assert 'early_onset' not in {i['code'] for i in validate(state)['issues']}
    state['words'][0]['alignment_score'] = .1
    assert 'early_onset' in {i['code'] for i in validate(state)['issues']}
    state['words'][0].update(alignment_score=.9, end_ms=2500)
    assert 'early_onset' in {i['code'] for i in validate(state)['issues']}


def test_summary_uses_all_observations_even_when_paginated():
    state = caption_state('中' * 60)
    state['words'][0]['alignment_score'] = .1
    report = validate(state, limit=1)
    assert len(report['issues']) == 1
    assert sum(report['summary']['by_type'].values()) == report['issue_count']
    reconcile_review(state)
    status = quality_status(state)
    assert not status['review_complete']
    assert status['pending_issue_summary']['by_type']['low_alignment'] == 1
    assert status['full_audio_review'] == 'not_recorded'


def test_export_persists_pending_review_and_explicit_audio_limit(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['words'][0]['alignment_score'] = .1
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch['batch_id'], batch['revision'], payload(batch))
    result = manager.export_job(job_id)
    assert result['exported']
    assert result['quality_status']['exported']
    assert not result['quality_status']['review_complete']
    assert result['quality_status']['full_audio_review'] == 'not_recorded'
    state = manager.store.read(job_id)
    saved = json.loads((Path(state['flow_dir']) / 'validation.json').read_text())
    assert saved['quality_status'] == result['quality_status']
    assert manager.get_review_issues(job_id)['total'] > 0


def test_legacy_and_clean_detector_do_not_imply_listening():
    state = caption_state('好')
    assert not quality_status(state)['review_complete']
    reconcile_review(state)
    assert quality_status(state)['review_complete']
    assert quality_status(state)['full_audio_review'] == 'not_recorded'


def test_policy_refresh_same_revision_is_read_only_and_preserves_audio_review(job):
    from app.mcp.captions import review_issue
    from app.mcp.review import REVIEW_POLICY_VERSION, review_is_current

    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch['batch_id'], batch['revision'], payload(batch))
    with manager.store.edit(job_id) as state:
        first = state['words'][0]
        first.update(text='Alternatively', end_ms=900)
        state['words'][2]['alignment_score'] = .1
        reconcile_review(state)
        acoustic = next(i for i in state['review_issues'].values() if i['code'] == 'low_alignment')
        acoustic.update(status='retained', review_method='audio', review_note='Listened to this exact span',
                        reviewed_revision=state['revision'])
        old = review_issue('early_onset', 'warning', 'Old fixed 800 ms detector', [first],
                           stage='caption', batch_id=batch['batch_id'])
        old['status'] = 'pending'
        state['review_issues'][old['id']] = old
        state['review_policy_version'] = REVIEW_POLICY_VERSION - 1
        assert state['review_revision'] == state['revision']
        assert not review_is_current(state)
    before = manager.store.path(job_id).read_bytes()
    issues = manager.get_review_issues(job_id, status='all')['issues']
    assert next(i for i in issues if i['id'] == old['id'])['status'] == 'resolved'
    assert next(i for i in issues if i['id'] == acoustic['id'])['status'] == 'retained'
    current = manager.get_job(job_id)['checkpoint']['quality_status']
    report = manager.validate_job(job_id)
    assert current == report['quality_status']
    assert current['review_current']
    assert 'early_onset' not in current['active_issue_summary']['by_type']
    assert current['audio_reviewed_issue_count'] == 1
    assert manager.store.path(job_id).read_bytes() == before
    result = manager.export_job(job_id)
    assert result['quality_status']['active_issue_summary'] == current['active_issue_summary']
    assert review_is_current(manager.store.read(job_id))


def test_partial_reconcile_refreshes_all_batches_on_policy_change():
    from app.mcp.captions import review_issue
    from app.mcp.review import REVIEW_POLICY_VERSION

    state = caption_state('好', next_start=2000)
    reconcile_review(state)
    other = state['batches'][1]
    stale = review_issue('early_onset', 'warning', 'Old detector', [state['words'][1]],
                         stage='caption', batch_id=other['id'])
    stale['status'] = 'pending'
    state['review_issues'][stale['id']] = stale
    state['review_policy_version'] = REVIEW_POLICY_VERSION - 1
    reconcile_review(state, batch_id=state['batches'][0]['id'])
    assert state['review_issues'][stale['id']]['status'] == 'resolved'
