"""Review lifecycle, omission evidence, pagination and native clip extraction."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import wave

import pytest

from app.mcp.captions import make_batches
from app.mcp.review import coverage_issues, prepare_evidence, reconcile_review
from app.mcp.jobs import JobManager
from test_mcp_workflow import job, payload


def test_missing_speech_is_detected_without_inventing_words(job):
    manager, job_id = job
    state = manager.store.read(job_id)
    words = deepcopy(state['words'])
    state['review_evidence'] = {'speech_ranges': [{'start_ms': 3000, 'end_ms': 6500}],
                              'source_cues': [{'start_ms': 3000, 'end_ms': 6500, 'text': 'A whole missing sentence here.'}]}
    issues = coverage_issues(state)
    assert {i['code'] for i in issues} == {'suspected_missing_speech', 'source_caption_disagreement'}
    assert state['words'] == words
    assert all(i['start_ms'] == 3000 and i['end_ms'] == 6500 for i in issues)
    state['words'].append({'id': 'new', 'text': 'A whole missing sentence here.', 'start_ms': 3000, 'end_ms': 6500})
    assert not coverage_issues(state)


def test_small_pauses_and_matching_source_captions_are_not_missing_speech(job):
    manager, job_id = job
    state = manager.store.read(job_id)
    state['review_evidence'] = {'speech_ranges': [{'start_ms': 0, 'end_ms': 2000}],
                              'source_cues': [{'start_ms': 0, 'end_ms': 2000, 'text': 'Hello world. Good morning.'}]}
    assert not coverage_issues(state)


def test_missing_evidence_is_explicit_and_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr('app.core.bk_asr.mlx_workflow.LOCAL_SILERO_REPO', tmp_path)
    with patch('app.core.bk_asr.mlx_workflow.detect_speech_ranges') as detect:
        evidence = prepare_evidence(tmp_path/'audio.wav')
    detect.assert_not_called()
    assert all(c['status'] == 'unavailable' for c in evidence['checks'].values())


def test_local_evidence_handles_vad_failure_and_reads_real_srt(tmp_path, monkeypatch):
    (tmp_path/'hubconf.py').touch()
    monkeypatch.setattr('app.core.bk_asr.mlx_workflow.LOCAL_SILERO_REPO', tmp_path)
    source = tmp_path/'captions.srt'
    source.write_text('1\n00:00:03,000 --> 00:00:05,000\nThis sentence is a reference.\n')
    with patch('app.core.bk_asr.mlx_workflow.detect_speech_ranges', side_effect=RuntimeError('unavailable')):
        evidence = prepare_evidence(tmp_path/'audio.wav', source)
    assert evidence['checks']['speech_activity']['status'] == 'unavailable'
    assert evidence['checks']['source_subtitles']['status'] == 'checked'
    assert evidence['source_cues'][0]['start_ms'] == 3000
    with patch('app.core.bk_asr.mlx_workflow.detect_speech_ranges', return_value=[(.2, 1.4)]):
        evidence = prepare_evidence(tmp_path/'audio.wav', source, compare_subtitles=False)
    assert evidence['speech_ranges'] == [{'start_ms': 200, 'end_ms': 1400}]
    assert evidence['checks']['source_subtitles']['status'] == 'skipped'


def test_review_persists_reopens_on_changed_evidence_and_resolves(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['words'][0]['alignment_score'] = .1
        reconcile_review(state)
    issue = manager.get_review_issues(job_id)['issues'][0]
    assert manager.get_job(job_id)['checkpoint']['next_action'] == 'review_transcript'
    with pytest.raises(ValueError, match='audio or source'):
        manager.review_issue(job_id, issue['id'], 1, 'retain', 'Guessed from text')
    manager.review_issue(job_id, issue['id'], 1, 'retain', 'Reviewed reference confirms wording; timing remains a stated limitation', 'reference')
    reconnected = JobManager(manager.store.root)
    assert not reconnected.get_review_issues(job_id)['issues']
    assert reconnected.get_review_issues(job_id, status='retained')['issues'][0]['review_method'] == 'reference'
    with manager.store.edit(job_id) as state:
        state['words'][0]['start_ms'] = 50
        state['revision'] += 1
        reconcile_review(state)
    new_issue = manager.get_review_issues(job_id)['issues'][0]
    assert new_issue['id'] != issue['id']
    assert manager.get_review_issues(job_id, status='resolved')['issues'][0]['id'] == issue['id']
    with pytest.raises(ValueError, match='Stale'):
        manager.review_issue(job_id, new_issue['id'], 1, 'retain', 'Old evidence', 'audio')
    with manager.store.edit(job_id) as state:
        state['words'][0]['alignment_score'] = .9
        state['revision'] += 1
        reconcile_review(state)
    assert not manager.get_review_issues(job_id)['issues']


def test_cannot_waive_structural_error(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['words'][0]['start_ms'] = -10
        reconcile_review(state)
    issue = manager.get_review_issues(job_id)['issues'][0]
    with pytest.raises(ValueError, match='Structural'):
        manager.review_issue(job_id, issue['id'], 1, 'retain', 'Ignore the error', 'audio')


def test_review_pagination_detects_concurrent_changes_and_remains_read_only(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        for word in state['words']:
            word['end_ms'] = word['start_ms']
        reconcile_review(state)
    path = manager.store.path(job_id)
    before = path.read_bytes()
    first = manager.get_review_issues(job_id, limit=2)
    second = manager.get_review_issues(job_id, offset=first['next_offset'], limit=2, event_id=first['event_id'])
    assert len(first['issues']) == len(second['issues']) == 2
    assert second['next_offset'] is None
    assert path.read_bytes() == before
    manager.review_issue(job_id, first['issues'][0]['id'], 1, 'retain', 'Fixture check', 'reference')
    with pytest.raises(ValueError, match='pagination'):
        manager.get_review_issues(job_id, offset=2, event_id=first['event_id'])


def test_caption_review_survives_other_batch_submissions(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['batches'] = make_batches(state['words'][:2]) + make_batches(state['words'][2:])
    first = manager.get_caption_batch(job_id)
    items = payload(first)
    items[0]['translation'] = '非常长的句子' * 15
    manager.submit_caption_batch(job_id, first['batch_id'], 1, items)
    issue = manager.get_review_issues(job_id, stage='caption')['issues'][0]
    manager.review_issue(job_id, issue['id'], 2, 'retain', 'Specific review rationale', 'text')
    second = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, second['batch_id'], 2, payload(second))
    assert manager.get_review_issues(job_id, status='retained')['issues'][0]['id'] == issue['id']
    current = manager.get_caption_batch(job_id, first['batch_id'])
    manager.submit_caption_batch(job_id, first['batch_id'], current['revision'], payload(first))
    assert issue['id'] in {i['id'] for i in manager.get_review_issues(job_id, status='resolved')['issues']}


def test_extract_real_review_audio_clip_preserves_state(job, tmp_path):
    manager, job_id = job
    audio = tmp_path/'audio.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 16000 * 3)
    with manager.store.edit(job_id) as state:
        state['audio_path'] = str(audio)
        state['words'][0]['alignment_score'] = .1
        reconcile_review(state)
    issue = manager.get_review_issues(job_id)['issues'][0]
    before = manager.store.read(job_id)
    result = manager.get_review_clip(job_id, issue['id'])
    with wave.open(result['audio_path'], 'rb') as stream:
        assert stream.getframerate() == 16000
        assert abs(stream.getnframes()/16000 - 1.4) < .02
    assert manager.store.read(job_id) == before


def test_worker_preflight_and_cover_registration_during_transcription(job, tmp_path):
    import subprocess
    import sys
    manager, job_id = job
    state = manager.store.read(job_id)
    audio = Path(state['directory'])/'audio.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 16000 * 3)
    with manager.store.edit(job_id) as state:
        state.update(video_path=None, audio_path=str(audio), generated_cover_path=None,
                     status='starting', words=[], batches=[], worker={'token': 'integration'})
        state['options'].update(workflow_version=2, format_selector='', proxy_url='', cookie_file=None,
                                initial_prompt='', model='fixture', backend='mlx')
    script = r'''
import json, sys, threading
from pathlib import Path
from unittest.mock import patch
from app.mcp.jobs import JobManager
from app.mcp.worker import Worker
manager = JobManager(sys.argv[1]); job_id = sys.argv[2]
original = manager.store.read(job_id)
worker = Worker(manager.store.root, job_id, 'integration')
def transcribe(path, options):
    current = manager.get_job(job_id)
    assert current['checkpoint']['next_action'] == 'prepare_cover'
    cover = manager.get_cover_source(job_id)
    assert Path(cover['image_path']).is_file()
    generated = Path(cover['image_path']).parent/'generated-cover.png'
    thread = threading.Thread(target=manager.set_generated_cover, args=(job_id, str(generated)))
    thread.start(); worker.update(progress=45, message='Independent progress'); thread.join()
    return {'language': 'en', 'segments': [{'words': [
        {'word': 'Hello', 'start': 0, 'end': .4, 'score': .1},
        {'word': 'world.', 'start': .5, 'end': .9, 'score': .1}]}]}
worker.transcribe = transcribe
with patch('app.core.download_service.VideoDownloadService') as service, patch('app.mcp.worker.prepare_evidence') as evidence, patch('app.mcp.jobs.owned_process', return_value=True):
    service.return_value.download.return_value = {'video_path': str(Path(original['directory'])/'video.mp4'),
        'thumbnail_path': original['thumbnail_path'], 'info_dict': {'title': 'Sample Video'}}
    evidence.return_value = {'version': 1, 'speech_ranges': [{'start_ms': 1_500, 'end_ms': 3_000}],
        'source_cues': [], 'checks': {'speech_activity': {'status': 'checked'}, 'source_subtitles': {'status': 'unavailable'}}}
    worker.run()
    assert evidence.call_count == 1
state = manager.store.read(job_id)
assert state['status'] == 'awaiting_captions'
assert Path(state['generated_cover_path']).is_file()
assert manager.get_job(job_id)['checkpoint']['next_action'] == 'review_transcript'
assert {i['code'] for i in manager.get_review_issues(job_id)['issues']} == {'low_alignment', 'suspected_missing_speech'}
assert Path(state['flow_dir'], 'review-evidence.json').is_file()
print('worker preflight and concurrent cover registration passed')
'''
    result = subprocess.run([sys.executable, '-c', script, str(manager.store.root), job_id], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_review_clip_long_observation_is_paginated(job, tmp_path, monkeypatch):
    manager, job_id = job
    audio = tmp_path/'audio.wav'
    audio.touch()
    with manager.store.edit(job_id) as state:
        state['audio_path'] = str(audio)
        state['duration_ms'] = 80000
        state['review_evidence'] = {'source_cues': [{'start_ms': 1000, 'end_ms': 70000, 'text': 'Another wholly different source sentence.'}]}
        reconcile_review(state)
    issue = manager.get_review_issues(job_id)['issues'][0]
    monkeypatch.setattr('app.core.bk_asr.mlx_workflow.extract_audio_chunk', lambda *args: args[1])
    first = manager.get_review_clip(job_id, issue['id'])
    second = manager.get_review_clip(job_id, issue['id'], first['next_offset_seconds'])
    assert first['end_seconds'] - first['start_seconds'] == 30
    assert second['start_seconds'] == first['end_seconds']


def test_delivery_error_totals_do_not_shrink_when_response_is_truncated(job):
    manager, job_id = job
    state = manager.store.read(job_id)
    state['words'] = [{'id': f'w{i}', 'text': 'x', 'start_ms': -1, 'end_ms': -2} for i in range(110)]
    state['batches'] = []
    state['thumbnail_path'] = None
    state['generated_cover_path'] = None
    report = manager._validation(state)
    assert report['error_count'] == 112 and report['truncated']
    assert not report['valid']
