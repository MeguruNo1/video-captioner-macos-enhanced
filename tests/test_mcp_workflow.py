import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch
from uuid import uuid4

import psutil
import pytest
from PIL import Image

from app.mcp.captions import anchor_captions, make_batches, validate, words_from_result
from app.mcp.jobs import JobManager, owned_process
from app.mcp.store import atomic_json
from app.mcp.worker import Worker, Revoked


@pytest.fixture
def job(tmp_path):
    manager = JobManager(tmp_path / "registry")
    job_id = uuid4().hex
    directory = tmp_path / "output"
    directory.mkdir()
    video = directory / "video.mp4"
    video.write_bytes(b"video")
    thumbnail = directory / "thumbnail.png"
    generated_cover = directory / "generated-cover.png"
    Image.new("RGB", (1600, 900), "red").save(thumbnail)
    Image.new("RGB", (1200, 900), "blue").save(generated_cover)
    words = [{"id": f"w{i:06}", "text": text, "start_ms": i*500, "end_ms": i*500+400}
             for i, text in enumerate(["Hello", "world.", "Good", "morning."])]
    state = {"job_id": job_id, "directory": str(directory), "status": "awaiting_captions",
             "stage": "awaiting_captions", "progress": 100, "message": "", "error": None,
             "revision": 1, "words": words, "batches": make_batches(words), "glossary": {},
             "created_at": time.time(), "worker": None, "artifacts": {}, "video_path": str(video),
             "cover_required": True, "thumbnail_path": str(thumbnail),
             "generated_cover_path": str(generated_cover),
             "duration_ms": 2100, "metadata": {"title": "Sample Video", "description": "About it"},
             "options": {"url": "https://example.com/video", "source_language": "en", "target_language": "zh-CN",
                         "description_txt_template": "Title: ${title}\n${description}"}}
    manager.store.save(state)
    return manager, job_id


def payload(batch):
    words = batch["words"]
    return [{"start_word_id": words[0]["id"], "end_word_id": words[-1]["id"],
             "source": "Hello world. Good morning.", "translation": "你好，世界。早上好。"}]


def test_complete_export_and_exact_retry(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    args = (job_id, batch["batch_id"], batch["revision"], payload(batch))
    manager.submit_caption_batch(*args)
    assert manager.submit_caption_batch(*args)["idempotent"]
    assert manager.get_caption_batch(job_id)["done"]
    result = manager.export_job(job_id)
    assert result["exported"]
    original = Path(result["artifacts"]["【字幕】「Sample Video」原文.srt"]).read_text()
    translated = Path(result["artifacts"]["【字幕】「Sample Video」译文.srt"]).read_text()
    assert "00:00:00,000 --> 00:00:02,100" in original
    assert "Hello world. Good morning." in original
    assert "你好，世界。早上好。" in translated
    output = Path(manager.get_job(job_id)["directory"]) / "output"
    assert {path.name for path in output.iterdir()} == {
        "【字幕】「Sample Video」原文.srt", "【字幕】「Sample Video」译文.srt",
        "【视频文稿】「Sample Video」原文.txt", "【简介】「Sample Video」.txt", "「Sample Video」.mp4",
        "原封面.png", "生成封面.png",
    }
    assert (output.parent / "flow" / "video.mp4").is_file()
    assert (output / "【简介】「Sample Video」.txt").read_text() == "Title: Sample Video\nAbout it"
    assert manager.get_job(job_id)["status"] == "completed"
    assert manager.export_job(job_id)["artifacts"] == result["artifacts"]


def test_generated_cover_requires_four_by_three(job, tmp_path):
    manager, job_id = job
    bad = tmp_path / "bad.png"
    Image.new("RGB", (1600, 900)).save(bad)
    with pytest.raises(ValueError, match="4:3"):
        manager.set_generated_cover(job_id, bad)
    good = tmp_path / "good.jpg"
    Image.new("RGB", (1200, 900), "green").save(good)
    result = manager.set_generated_cover(job_id, good)
    assert result["accepted"]
    with Image.open(result["image_path"]) as image:
        assert image.size == (1200, 900)
        assert image.format == "PNG"


def test_required_generated_cover_blocks_export(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state["generated_cover_path"] = None
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], payload(batch))
    result = manager.export_job(job_id)
    assert not result["exported"]
    assert any("Generated 4:3 Chinese cover" in error for error in result["validation"]["errors"])


def test_caption_submission_applies_desktop_text_switches(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state["options"]["workflow_settings"] = {"subtitle": {
            "target_language_code": "zh-CN",
            "mask_original_profanity": True,
            "remove_translated_chinese_commas": True,
            "remove_translated_periods": True,
        }}
    batch = manager.get_caption_batch(job_id)
    items = payload(batch)
    items[0].update(source="This fucking thing is shit.", translation='她说，“你好”，又说‘早上好’。')
    manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], items)
    saved = manager.get_caption_batch(job_id, batch["batch_id"])
    assert saved["existing_captions"][0]["source"] == "This [ _ ]ing thing is [ _ ]."
    assert saved["existing_captions"][0]["translation"] == "她说 「你好」 又说『早上好』"
    assert saved["workflow_settings"]["mask_original_profanity"] is True


def test_start_job_snapshots_current_desktop_settings(tmp_path, monkeypatch):
    manager = JobManager(tmp_path / "registry")
    settings = {
        "Download": {"EngineStrategy": "多线程", "NativeHevcPreset": "balanced_4k"},
        "MLXWhisper": {"Model": "configured-model", "Hotwords": "Aria, Sigrid",
                       "InitialPrompt": "configured prompt", "VadEnabled": False,
                       "VadThreshold": 0.7, "ChunkDuration": 720, "ChunkOverlap": 45},
        "Subtitle": {"TargetLanguage": "中文", "MaxWordCountCJK": 18,
                     "NeedMaskOriginalProfanity": True,
                     "NeedsRemoveTranslatedChineseCommas": True,
                     "NeedsRemovePunctuation": True},
    }
    monkeypatch.setattr("app.mcp.jobs.read_settings", lambda: settings)
    monkeypatch.setattr("app.mcp.jobs.check_environment", lambda *args: {
        "ready": True, "errors": [], "local_model": "/models/configured",
        "selection": {"backend": "mlx", "device": "metal", "compute_type": "model"}
    })
    monkeypatch.setattr(manager, "resume_job", lambda job_id: manager.get_job(job_id))
    result = manager.start_job("https://example.com/video", output_dir=str(tmp_path / "output"))
    state = manager.store.read(result["job_id"])
    options = state["options"]
    assert options["download_engine_strategy"] == "多线程"
    assert options["native_hevc_preset"] == "balanced_4k"
    assert options["initial_prompt"] == "configured prompt"
    assert options["mlx_hotwords"] == "Aria, Sigrid"
    assert options["alignment"]["method"] == "whisperx"
    assert options["alignment"]["device"] == "cpu"
    assert options["alignment"]["version"] == 1
    assert options["vad_enabled"] is False
    assert options["vad_threshold"] == 0.7
    assert options["chunk_duration"] == 720
    assert options["chunk_overlap"] == 45
    assert options["workflow_settings"]["subtitle"]["max_word_count_cjk"] == 18
    task = json.loads((Path(state["directory"]) / "task.json").read_text())
    assert task["workflow_settings"] == options["workflow_settings"]


def test_confirmed_batch_glossary_updates_managed_file(job, tmp_path):
    manager, job_id = job
    glossary_path = tmp_path / "对照.md"
    glossary_path.write_text("# 手工术语\nMiyabi → 雅\n", encoding="utf-8")
    with manager.store.edit(job_id) as state:
        state["options"]["workflow_settings"] = {"subtitle": {"glossary_path": str(glossary_path)}}
        state["base_glossary"] = {"Miyabi": "雅"}
        state["glossary"] = dict(state["base_glossary"])
    batch = manager.get_caption_batch(job_id)
    result = manager.submit_caption_batch(
        job_id, batch["batch_id"], batch["revision"], payload(batch),
        glossary={"New Eridu": "新艾利都"},
    )
    assert result["glossary_terms_added"] == ["New Eridu"]
    assert "New Eridu → 新艾利都" in glossary_path.read_text(encoding="utf-8")
    assert manager.get_caption_batch(job_id, batch["batch_id"])["glossary"]["Miyabi"] == "雅"


@pytest.mark.parametrize("mutation", ["gap", "overlap", "reverse", "unknown", "empty"])
def test_invalid_caption_coverage_is_atomic(job, mutation):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    items = payload(batch)
    if mutation == "gap": items[0]["start_word_id"] = "w000001"
    if mutation == "overlap": items.append(items[0].copy())
    if mutation == "reverse": items[0].update(start_word_id="w000003", end_word_id="w000001")
    if mutation == "unknown": items[0]["end_word_id"] = "unknown"
    if mutation == "empty": items[0]["translation"] = " "
    with pytest.raises(ValueError):
        manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], items)
    assert manager.get_caption_batch(job_id)["revision"] == 1
    assert not manager.export_job(job_id)["exported"]


def test_stale_changed_submission_rejected_and_output_is_updated(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], 1, payload(batch))
    first = manager.export_job(job_id)
    translated_name = "【字幕】「Sample Video」译文.srt"
    old_text = Path(first["artifacts"][translated_name]).read_text()
    changed = payload(batch)
    changed[0]["translation"] = "世界，你好。早安。"
    with pytest.raises(ValueError, match="Stale"):
        manager.submit_caption_batch(job_id, batch["batch_id"], 1, changed)
    manager.submit_caption_batch(job_id, batch["batch_id"], 2, changed)
    second = manager.export_job(job_id)
    assert first["artifacts"] == second["artifacts"]
    assert Path(second["artifacts"][translated_name]).read_text() != old_text


def test_cancel_and_resume_preserve_translation_batches(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], 1, payload(batch))
    manager.cancel_job(job_id)
    with pytest.raises(ValueError, match="not ready"):
        manager.get_caption_batch(job_id)
    manager.resume_job(job_id)
    assert manager.get_caption_batch(job_id)["done"]


def test_missing_worker_reconciles_to_interrupted(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state.update(status="transcribing", worker={"pid": 999999999, "created": 0, "token": "gone"})
    assert manager.get_job(job_id)["status"] == "interrupted"
    assert manager.resume_job(job_id)["status"] == "awaiting_captions"


def test_spawn_recreates_deleted_work_directory(job, monkeypatch):
    manager, job_id = job
    state = manager.store.read(job_id)
    shutil.rmtree(state["directory"])
    process = Mock(pid=os.getpid())
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(psutil, "Process", lambda pid: Mock(create_time=lambda: 1.0))
    monkeypatch.setattr(threading, "Thread", lambda *args, **kwargs: Mock(start=lambda: None))
    with manager.store.edit(job_id) as editable:
        manager._spawn(editable)
    assert (Path(state["directory"]) / "worker.log").is_file()


def test_revoked_worker_cannot_overwrite_cancel(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state["worker"] = {"token": "old"}
    worker = Worker(manager.store.root, job_id, "old")
    manager.cancel_job(job_id)
    with pytest.raises(Revoked):
        worker.update(status="completed")
    assert manager.get_job(job_id)["status"] == "cancelled"


def test_cancel_only_owned_process_group(job):
    manager, job_id = job
    token = uuid4().hex
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "app.mcp.worker", job_id, token], start_new_session=True)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        with manager.store.edit(job_id) as state:
            state.update(status="downloading", worker={"pid": child.pid, "created": psutil.Process(child.pid).create_time(), "token": token})
        manager.cancel_job(job_id)
        assert child.poll() is not None
        assert unrelated.poll() is None
    finally:
        for proc in (child, unrelated):
            if proc.poll() is None: proc.terminate()
            proc.wait(timeout=5)


def test_zero_duration_interior_word_warns_but_valid_caption_exports(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    items = payload(batch)
    items[0]["translation"] = "这是很长的字幕" * 15
    manager.submit_caption_batch(job_id, batch["batch_id"], 1, items)
    assert manager.validate_job(job_id)["warnings"]
    with manager.store.edit(job_id) as state:
        state["words"][1]["end_ms"] = state["words"][1]["start_ms"]
    report = manager.validate_job(job_id)
    assert report["valid"]
    assert any("Zero-duration" in warning for warning in report["warnings"])
    assert manager.export_job(job_id)["exported"]


def test_negative_word_timing_blocks_export(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], 1, payload(batch))
    with manager.store.edit(job_id) as state:
        state["words"][1]["start_ms"] = -1
    assert not manager.export_job(job_id)["exported"]


@pytest.mark.parametrize("result", [{"segments": []}, {"segments": [{"text": "speech"}]},
    {"segments": [{"words": [{"word": "bad", "start": float("nan"), "end": 2}]}]}])
def test_invalid_asr_is_not_silently_replaced_by_guessed_timestamps(result):
    with pytest.raises(ValueError): words_from_result(result)


def test_retranscribe_replaces_only_affected_batches(job, monkeypatch):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state["batches"] = make_batches(state["words"][:2]) + make_batches(state["words"][2:])
    original = manager.store.read(job_id)
    for b in original["batches"]:
        batch = manager.get_caption_batch(job_id, b["id"])
        manager.submit_caption_batch(job_id, b["id"], batch["revision"], payload(batch))
    monkeypatch.setattr(manager, "_spawn", lambda state: state.update(worker={"token": "test"}))
    state = manager.store.read(job_id)
    manager.retranscribe_range(job_id, "w000000", "w000001", state["revision"])
    state = manager.store.read(job_id)
    worker = Worker(manager.store.root, job_id, "test")
    monkeypatch.setattr(worker, "transcribe", lambda *args: {"segments": [{"words": [{"word": "Hi!", "start": .1, "end": .8}]}]})
    monkeypatch.setattr("app.core.bk_asr.mlx_workflow.extract_audio_chunk", lambda *args: Path("unused.wav"))
    worker.retranscribe(state, Path(state["directory"]), Path("unused.wav"), {})
    updated = manager.store.read(job_id)
    assert updated["words"][0]["id"] != "w000000"
    assert updated["words"][1:] == original["words"][2:]
    assert updated["batches"][0]["captions"] is None
    assert updated["batches"][1]["captions"] is not None
    assert updated["revision"] == state["revision"] + 1


def test_native_mlx_no_whisperx_and_distinct_cache():
    from app.core.bk_asr.mlx_whisper import MLXWhisperASR
    native = MLXWhisperASR(b"audio", alignment_method="native")
    old = MLXWhisperASR(b"audio")
    assert native._get_key() != old._get_key()
    mlx = Mock()
    mlx.transcribe.return_value = {"segments": [{"text": "Hello", "start": 0, "end": 1}]}
    with patch.dict(sys.modules, {"mlx_whisper": mlx}), patch("app.core.bk_asr.mlx_whisper.align_transcription_with_whisperx") as align:
        native._run()
        assert mlx.transcribe.call_args.kwargs["word_timestamps"] is True
        align.assert_not_called()


def test_headless_imports_in_fresh_process():
    result = subprocess.run([sys.executable, "-c", "from app.core.download_service import VideoDownloadService; from app.core.bk_asr.mlx_whisper import MLXWhisperASR; import sys; assert not any(n.startswith('PyQt5') or n == 'app.core.bk_asr.whisper_x_auto' or n == 'openai' for n in sys.modules)"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rejects_playlist_and_invalid_url_before_work(tmp_path):
    manager = JobManager(tmp_path)
    for url in ("file:///etc/passwd", "https://youtube.com/playlist?list=abc", "https://user:pass@youtube.com/watch?v=a"):
        with pytest.raises(ValueError): manager.start_job(url)
    assert not list(tmp_path.glob("jobs/*.json"))


def continuity_job(job):
    """Unpunctuated ASR whose 160-word boundary falls after 'I might just'."""
    manager, job_id = job
    texts = ['word'] * 157 + 'I might just keep it in the box and then'.split()
    words = [{'id': f'w{i:06}', 'text': text, 'start_ms': i * 150,
              'end_ms': (i + 1) * 150} for i, text in enumerate(texts)]
    with manager.store.edit(job_id) as state:
        state.update(words=words, batches=make_batches(words), duration_ms=30000)
    return manager.get_caption_batch(job_id)


def test_move_boundary_preserves_complete_sentence_and_word_timing(job):
    manager, job_id = job
    batch = continuity_job(job)
    before = manager.store.read(job_id)
    assert batch['words'][-1]['text'] == 'just'
    result = manager.set_caption_batch_boundary(job_id, batch['batch_id'], 1, 'w000164')
    assert result['revision'] == 2
    updated = manager.get_caption_batch(job_id)
    assert updated['words'][-1]['text'] == 'box'
    assert updated['context_after'][0]['text'] == 'and'
    items = [
        {'start_word_id': 'w000000', 'end_word_id': 'w000156',
         'source': 'Earlier speech.', 'translation': '前文'},
        {'start_word_id': 'w000157', 'end_word_id': 'w000164',
         'source': 'I might just keep it in the box.', 'translation': '我可能就把它放在盒子里吧'},
    ]
    manager.submit_caption_batch(job_id, batch['batch_id'], 2, items)
    following = manager.get_caption_batch(job_id)
    assert following['words'][0]['id'] == 'w000165'
    assert manager.store.read(job_id)['words'] == before['words']
    saved = manager.get_caption_batch(job_id, batch['batch_id'])['existing_captions'][-1]
    assert saved['start_ms'] == before['words'][157]['start_ms']
    assert saved['end_ms'] == before['words'][164]['end_ms']
    manager.submit_caption_batch(job_id, following['batch_id'], following['revision'], [{
        'start_word_id': 'w000165', 'end_word_id': 'w000166',
        'source': 'And then', 'translation': '然后',
    }])
    result = manager.export_job(job_id)
    assert result['exported']
    original = Path(result['artifacts']['【字幕】「Sample Video」原文.srt']).read_text()
    assert 'I might just keep it in the box.' in original
    assert '...' not in original


@pytest.mark.parametrize('end_id,expected_sizes', [('w000156', [157, 10]), ('w000166', [167])])
def test_move_tail_or_consume_next_batch_preserves_exact_coverage(job, end_id, expected_sizes):
    manager, job_id = job
    batch = continuity_job(job)
    before = manager.store.read(job_id)
    manager.set_caption_batch_boundary(job_id, batch['batch_id'], 1, end_id)
    state = manager.store.read(job_id)
    fetched = [manager.get_caption_batch(job_id, b['id'])['words'] for b in state['batches']]
    assert [len(words) for words in fetched] == expected_sizes
    assert [w for words in fetched for w in words] == before['words']
    assert state['batches'][0]['id'] == batch['batch_id']


@pytest.mark.parametrize('failure', ['stale', 'unknown_word', 'unknown_batch', 'current_saved', 'next_saved', 'last_batch'])
def test_boundary_rejection_is_atomic(job, failure):
    manager, job_id = job
    batch = continuity_job(job)
    state = manager.store.read(job_id)
    batch_id, revision, end_id = batch['batch_id'], 1, 'w000164'
    if failure == 'stale':
        revision = 0
    elif failure == 'unknown_word':
        end_id = 'missing'
    elif failure == 'unknown_batch':
        batch_id = 'missing'
    elif failure == 'last_batch':
        batch_id = state['batches'][-1]['id']
    else:
        index = 0 if failure == 'current_saved' else 1
        saved_batch = manager.get_caption_batch(job_id, state['batches'][index]['id'])
        manager.submit_caption_batch(job_id, saved_batch['batch_id'], 1, payload(saved_batch))
        revision = 2
    before = manager.store.read(job_id)
    with pytest.raises(ValueError):
        manager.set_caption_batch_boundary(job_id, batch_id, revision, end_id)
    assert manager.store.read(job_id) == before


def test_repeated_boundary_expansion_is_bounded(job):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        words = [{'id': f'w{i:06}', 'text': 'word', 'start_ms': i*100,
                  'end_ms': (i+1)*100} for i in range(480)]
        state.update(words=words, batches=make_batches(words), duration_ms=48000)
    batch = manager.get_caption_batch(job_id)
    manager.set_caption_batch_boundary(job_id, batch['batch_id'], 1, 'w000319')
    before = manager.store.read(job_id)
    with pytest.raises(ValueError, match='320 words'):
        manager.set_caption_batch_boundary(job_id, batch['batch_id'], 2, 'w000320')
    assert manager.store.read(job_id) == before


@pytest.mark.parametrize('cross_batch', [False, True])
@pytest.mark.parametrize('gap,expected', [(0, True), (300, True), (301, False), (1500, False)])
def test_continuation_ellipsis_warning_within_and_across_batches(job, cross_batch, gap, expected):
    manager, job_id = job
    state = manager.store.read(job_id)
    words = state['words']
    words[1]['end_ms'] = 1000
    for word in words[2:]:
        word['start_ms'] += gap
        word['end_ms'] += gap
    state['duration_ms'] += gap
    state['batches'] = (make_batches(words[:2]) + make_batches(words[2:])
                        if cross_batch else make_batches(words))
    items = [
        {'start_word_id': words[0]['id'], 'end_word_id': words[1]['id'],
         'source': 'I might just...', 'translation': '我可能就……'},
        {'start_word_id': words[2]['id'], 'end_word_id': words[3]['id'],
         'source': '...keep it in the box.', 'translation': '……放在盒子里吧'},
    ]
    for i, batch in enumerate(state['batches']):
        batch['captions'] = [items[i]] if cross_batch else items
    report = validate(state)
    assert any('artificial ellipsis split' in w for w in report['warnings']) == expected
    if expected:
        assert report['valid']  # Review warning, not a blanket ban on real hesitation.


def test_batch_sentence_break_handles_quotes_but_not_ellipsis():
    words = [{'id': f'w{i:06}', 'text': 'word'} for i in range(200)]
    words[100]['text'] = 'finished.”'
    words[150]['text'] = 'uh...'
    assert make_batches(words)[0]['end_word_id'] == 'w000100'


@pytest.mark.parametrize('backend,vad,collapsed,expected_vad', [
    ('mlx', True, True, False),
    ('mlx', True, False, True),
    ('mlx', False, True, False),
    ('whisperx', True, True, True),
])
def test_retranscribe_collapsed_timing_strategy(job, monkeypatch, backend, vad, collapsed, expected_vad):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['words'][0]['end_ms'] = state['words'][0]['start_ms']
        if collapsed:
            state['words'][1]['end_ms'] = state['words'][1]['start_ms']
        state['batches'] = make_batches(state['words'][:2]) + make_batches(state['words'][2:])
        state['batches'][1]['captions'] = [{'preserved': True}]
        state['worker'] = {'token': 'test'}
        state['retranscribe'] = {'batch_ids': [state['batches'][0]['id']], 'initial_prompt': ''}
    state = manager.store.read(job_id)
    worker = Worker(manager.store.root, job_id, 'test')
    options = {'backend': backend, 'vad_enabled': vad, 'initial_prompt': 'Saved prompt'}
    calls = []
    def transcribe(path, effective, prompt):
        calls.append((dict(effective), prompt))
        return {'segments': [{'words': [{'word': 'Hello', 'start': .1, 'end': .8}]}]}
    monkeypatch.setattr(worker, 'transcribe', transcribe)
    monkeypatch.setattr('app.core.bk_asr.mlx_workflow.extract_audio_chunk', lambda *a: Path('unused.wav'))
    worker.retranscribe(state, Path(state['directory']), Path('unused.wav'), options)
    assert calls == [({'backend': backend, 'vad_enabled': expected_vad, 'initial_prompt': 'Saved prompt'}, None)]
    assert options['vad_enabled'] is vad
    updated = manager.store.read(job_id)
    assert updated['words'][1:] == state['words'][2:]
    assert updated['batches'][1] == state['batches'][1]
    diagnostic = json.loads(next(Path(state['directory']).glob('transcript-*.json')).read_text())
    assert diagnostic['vad_enabled'] is expected_vad
    assert diagnostic['strategy'] == ('mlx_continuous_audio' if backend == 'mlx' and vad and collapsed else 'saved_settings')


def test_failed_timing_repair_retains_words_captions_and_diagnostic(job, monkeypatch):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        for w in state['words'][:2]:
            w['end_ms'] = w['start_ms']
        state['worker'] = {'token': 'test'}
        state['retranscribe'] = {'batch_ids': [state['batches'][0]['id']], 'initial_prompt': 'Custom'}
    state = manager.store.read(job_id)
    worker = Worker(manager.store.root, job_id, 'test')
    transcribe = Mock(return_value={'segments': [{'words': [
        {'word': 'Repeated', 'start': .1, 'end': .1},
        {'word': 'words', 'start': .1, 'end': .1},
    ]}]})
    monkeypatch.setattr(worker, 'transcribe', transcribe)
    monkeypatch.setattr('app.core.bk_asr.mlx_workflow.extract_audio_chunk', lambda *a: Path('unused.wav'))
    with pytest.raises(ValueError, match='original captions retained'):
        worker.retranscribe(state, Path(state['directory']), Path('unused.wav'), {'backend': 'mlx', 'vad_enabled': True})
    updated = manager.store.read(job_id)
    for key in ('words', 'batches', 'revision'):
        assert updated[key] == state[key]
    assert transcribe.call_count == 1
    assert transcribe.call_args.args[2] == 'Custom'
    assert len(list(Path(state['directory']).glob('transcript-*.json'))) == 1


def test_export_extension_clamps_without_moving_anchors():
    from app.mcp.captions import extend_export_captions
    captions = [{"start_ms": s, "end_ms": e} for s, e in
                [(0, 100), (1000, 1100), (1200, 1500), (1400, 1700), (1900, 2000)]]
    result = extend_export_captions(captions, 2200)
    assert [c["end_ms"] for c in result] == [600, 1200, 1500, 1900, 2200]
    assert [c["start_ms"] for c in result] == [c["start_ms"] for c in captions]
    assert captions[-1]["end_ms"] == 2000


def test_realignment_preserves_text_ids_and_translations(job):
    from app.mcp.alignment import reanchor_alignment
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], payload(batch))
    state = manager.store.read(job_id)
    raw = {"segments": [{"words": [{"word": w["text"], "start": (w["start_ms"] + 50)/1000,
                                    "end": (w["end_ms"] + 50)/1000, "score": .8} for w in state["words"]]}]}
    candidate = reanchor_alignment(state, raw)
    assert [w["id"] for w in candidate["words"]] == [w["id"] for w in state["words"]]
    assert candidate["batches"][0]["captions"][0]["translation"] == state["batches"][0]["captions"][0]["translation"]
    assert candidate["batches"][0]["captions"][0]["start_ms"] == 50
    assert state["words"][0]["start_ms"] == 0
    raw["segments"][0]["words"].pop()
    with pytest.raises(ValueError, match="coverage"):
        reanchor_alignment(state, raw)
    assert manager.store.read(job_id) == state


def test_worker_uses_saved_alignment_and_legacy_native(tmp_path):
    options = {"initial_prompt": "", "source_language": "en", "backend": "mlx",
               "model": "local", "vad_enabled": False}
    worker = Worker(tmp_path, "unused", "unused")
    worker.update = Mock()
    with patch("app.core.bk_asr.mlx_whisper.MLXWhisperASR") as factory:
        factory.return_value._run.return_value = {"segments": []}
        worker.transcribe("audio.wav", options)
        assert factory.call_args.kwargs["alignment_method"] == "native"
        options["alignment"] = {"method": "whisperx", "device": "cpu", "model_dir": "/models"}
        result = worker.transcribe("audio.wav", options)
        assert factory.call_args.kwargs["alignment_method"] == "whisperx"
        assert factory.call_args.kwargs["align_inherit_proxy_environment"] is True
        assert result["alignment_policy"] == options["alignment"]


def test_realign_job_revision_and_resume(job, monkeypatch):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        audio = Path(state["directory"]) / "audio.wav"
        audio.write_bytes(b"audio")
        state["audio_path"] = str(audio)
    spawn = Mock()
    monkeypatch.setattr(manager, "_spawn", spawn)
    with pytest.raises(ValueError, match="Stale"):
        manager.realign_job(job_id, 999)
    assert not spawn.called
    manager.realign_job(job_id, 1)
    assert manager.store.read(job_id)["realign"]["alignment"]["method"] == "whisperx"
    manager.resume_job(job_id)
    assert spawn.call_count == 2


def test_alignment_confidence_and_early_onset_warnings(job):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], payload(batch))
    state = manager.store.read(job_id)
    state["words"][0].update(end_ms=900, alignment_score=.1)
    report = validate(state)
    assert any("early onset" in w for w in report["warnings"])
    assert any("Low alignment confidence" in w for w in report["warnings"])


def test_realignment_context_is_removed_without_averaging():
    from app.mcp.alignment import remove_alignment_context
    first = [{"word": str(i), "start": i, "end": i+.8} for i in range(5)]
    second = [{"word": str(i), "start": i+.1, "end": i+.9} for i in range(2, 7)]
    second[0]["end"] = 2.6
    second[1]["start"] = 2.7  # Overlap at the nominal seam (word 3).
    result = {"segments": [{"words": first + second}]}
    segments = [{"global_start": 0, "split_index": 0, "word_count": 5},
                {"global_start": 2, "split_index": 3, "word_count": 5}]
    trimmed = remove_alignment_context(result, segments)["segments"][0]["words"]
    assert [w["word"] for w in trimmed] == [str(i) for i in range(7)]
    assert all(w in first + second for w in trimmed)
    assert all(a["end"] <= b["start"] for a, b in zip(trimmed, trimmed[1:]))
    result["segments"][0]["words"].pop()
    with pytest.raises(ValueError, match="context coverage"):
        remove_alignment_context(result, segments)


def test_failed_alignment_keeps_saved_words_and_translations(job, monkeypatch):
    manager, job_id = job
    batch = manager.get_caption_batch(job_id)
    manager.submit_caption_batch(job_id, batch["batch_id"], batch["revision"], payload(batch))
    state = manager.store.read(job_id)
    state["realign"] = {"alignment": {"method": "whisperx", "device": "cpu", "model_dir": "/models"}}
    worker = Worker(manager.store.root, job_id, "test")
    worker.stage = Mock()
    worker.update = Mock()
    with patch("app.core.bk_asr.whisper_x_auto.align_transcription_with_whisperx", return_value={"segments": []}):
        with pytest.raises(ValueError, match="coverage"):
            worker.realign(state, Path(state["directory"]), "audio.wav")
    worker.update.assert_not_called()
    saved = manager.store.read(job_id)
    assert saved["words"] == state["words"]
    assert saved["batches"] == state["batches"]


@pytest.mark.parametrize("bad", ["missing", "interpolated", "valid"])
def test_headless_alignment_rejects_incomplete_acoustic_evidence(monkeypatch, bad):
    import types
    from app.core.bk_asr.whisper_x_auto import align_transcription_with_whisperx
    words = [{"word": "Hello", "start": .1, "end": .3, "score": .9}]
    if bad == "missing":
        words = []
    elif bad == "interpolated":
        words[0].pop("score")
    fake = types.SimpleNamespace(load_audio=Mock(return_value=[]),
                                 load_align_model=Mock(return_value=(object(), {})),
                                 align=Mock(return_value={"segments": [{"words": words}]}))
    with patch.dict(sys.modules, {"whisperx": fake}), \
         patch("app.core.bk_asr.whisper_x_auto.apply_download_proxy_environment") as desktop_proxy, \
         patch("app.core.bk_asr.nltk_utils.ensure_punkt_tab"), \
         patch("app.core.utils.acceleration.resolve_whisperx_device", return_value={"device": "cpu"}):
        args = ("audio.wav", [{"start": 0, "end": 1, "text": "Hello"}], "en")
        if bad == "valid":
            assert align_transcription_with_whisperx(*args, inherit_proxy_environment=True)["language"] == "en"
        else:
            with pytest.raises(ValueError, match="alignment"):
                align_transcription_with_whisperx(*args, inherit_proxy_environment=True)
        desktop_proxy.assert_not_called()


def test_alignment_policy_rejects_implementation_drift():
    from app.mcp.settings import check_alignment_snapshot
    check_alignment_snapshot({})  # Old jobs retain their native behavior.
    with pytest.raises(ValueError, match="Unsupported"):
        check_alignment_snapshot({"method": "whisperx", "version": 999})
    with patch("importlib.metadata.version", return_value="new"):
        with pytest.raises(ValueError, match="version differs"):
            check_alignment_snapshot({"method": "whisperx", "version": 1, "whisperx_version": "old"})


def test_complete_validation_report_keeps_all_review_warnings():
    state = {"words": [{"id": f"w{i}", "text": "x", "start_ms": i*100,
                       "end_ms": i*100+50, "alignment_score": .1} for i in range(110)], "batches": []}
    assert validate(state)["truncated"]
    full = validate(state, limit=None)
    assert not full["truncated"]
    assert len(full["warnings"]) == 110


def test_reading_status_and_exact_retry_do_not_write(job):
    manager, job_id = job
    path = manager.store.path(job_id)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    first = manager.get_job(job_id)
    assert manager.get_job(job_id) == first
    manager.list_jobs()
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    batch = manager.get_caption_batch(job_id)
    args = (job_id, batch['batch_id'], batch['revision'], payload(batch))
    manager.submit_caption_batch(*args)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert manager.submit_caption_batch(*args)['idempotent']
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_wait_events_are_separate_from_caption_revision(job, monkeypatch):
    manager, job_id = job
    with manager.store.edit(job_id) as state:
        state['status'] = 'transcribing'
    monkeypatch.setattr('app.mcp.jobs.owned_process', lambda state: True)
    before = manager.get_job(job_id)
    def update():
        time.sleep(.05)
        with manager.store.edit(job_id) as state:
            state['progress'] = 60
    worker = threading.Thread(target=update)
    worker.start()
    result = manager.wait_job(job_id, before['event_id'], timeout=1)
    worker.join()
    assert result['changed']
    assert result['event_id'] > before['event_id']
    assert result['revision'] == before['revision']
    assert result['progress'] == 60
    path = manager.store.path(job_id)
    mtime = path.stat().st_mtime_ns
    assert not manager.wait_job(job_id, result['event_id'], timeout=.02)['changed']
    assert path.stat().st_mtime_ns == mtime
    monkeypatch.setattr('app.mcp.jobs.owned_process', lambda state: False)
    stopped = manager.wait_job(job_id, result['event_id'], timeout=1)
    assert stopped['changed'] and stopped['status'] == 'interrupted'
    assert not manager.wait_job(job_id, stopped['event_id'], timeout=60)['changed']


def test_legacy_job_without_event_id_is_read_only(job):
    manager, job_id = job
    path = manager.store.path(job_id)
    state = manager.store.read(job_id)
    state.pop('event_id')
    atomic_json(path, state)
    before = path.read_bytes()
    assert manager.get_job(job_id)['event_id'] == 0
    assert not manager.wait_job(job_id, 0)['changed']
    assert path.read_bytes() == before
    with manager.store.edit(job_id) as current:
        current['message'] = 'Changed'
    assert manager.wait_job(job_id, 0)['event_id'] == 1
