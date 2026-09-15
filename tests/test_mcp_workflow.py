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

from app.mcp.captions import anchor_captions, fill_short_display_gaps, make_batches, validate, words_from_result
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


def test_fill_short_display_gaps_uses_half_second_boundary():
    captions = [
        {"start_ms": 0, "end_ms": 1000, "source": "A", "translation": "甲"},
        {"start_ms": 1500, "end_ms": 2000, "source": "B", "translation": "乙"},
        {"start_ms": 2501, "end_ms": 3000, "source": "C", "translation": "丙"},
    ]
    result = fill_short_display_gaps(captions)
    assert result[0]["end_ms"] == 1500
    assert result[1]["end_ms"] == 2000
    assert captions[0]["end_ms"] == 1000


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
    assert "00:00:00,000 --> 00:00:01,900" in original
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
    result = manager.start_job("https://example.com/video")
    state = manager.store.read(result["job_id"])
    options = state["options"]
    assert options["download_engine_strategy"] == "多线程"
    assert options["native_hevc_preset"] == "balanced_4k"
    assert options["initial_prompt"] == "configured prompt"
    assert options["mlx_hotwords"] == "Aria, Sigrid"
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
