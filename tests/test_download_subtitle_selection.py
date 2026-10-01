from unittest.mock import patch

import requests
import pytest

from app.core.download_service import VideoDownloadService, _subtitle_candidates


def test_manual_original_language_precedes_native_auto_and_ignores_translation():
    info = {"language": "en", "subtitles": {"fr": [{"url": "https://x/fr"}],
             "en-US": [{"url": "https://x/manual", "ext": "vtt"}]},
            "automatic_captions": {"en": [{"url": "https://x/?lang=uk&tlang=en"}],
             "en-orig": [{"url": "https://x/?lang=en", "ext": "vtt"}]}}
    selected = _subtitle_candidates(info, "prefer_manual", "auto")
    assert [(x["kind"], x["language"]) for x in selected] == [("manual", "en-us"), ("auto", "en")]


def test_original_track_detects_language_without_metadata():
    info = {"automatic_captions": {"en": [{"url": "https://x/?lang=uk&tlang=en"}],
                                   "uk-orig": [{"url": "https://x/?lang=uk"}]}}
    assert _subtitle_candidates(info, "prefer_manual", "auto")[0]["language"] == "uk"
    assert _subtitle_candidates(info, "auto", "en") == []


def test_explicit_manual_and_auto_remain_separate_and_no_unrelated_fallback():
    info = {"language": "en", "subtitles": {"en": [{"url": "https://x/manual"}]},
            "automatic_captions": {"en": [{"url": "https://x/auto"}]}}
    assert _subtitle_candidates(info, "auto", "en")[0]["kind"] == "auto"
    assert _subtitle_candidates(info, "manual", "en")[0]["kind"] == "manual"
    assert _subtitle_candidates(info, "auto", "zh") == []
    assert _subtitle_candidates({"subtitles": info["subtitles"]}, "prefer_manual", "auto")[0]["kind"] == "manual"


def test_manual_failure_falls_back_to_native_auto_without_blocking_video(tmp_path):
    info = {"title": "sample", "language": "en", "subtitles": {"en": [{"url": "https://x/manual"}]},
            "automatic_captions": {"en-orig": [{"url": "https://x/auto"}]}}
    service = VideoDownloadService("https://x/video", str(tmp_path), proxy_url="", cookie_file="")
    with (patch("app.core.download_service._extract_metadata_info", return_value=info),
          patch("app.core.download_service._download_subtitle_fallback", side_effect=[requests.HTTPError("429"), "/tmp/native.vtt"]) as download,
          patch("app.core.download_service.yt_dlp.YoutubeDL") as ydl):
        result = service.download(need_video=False, subtitle_mode="prefer_manual", subtitle_language="auto")
    assert download.call_count == 2
    assert result["subtitle_kind"] == "auto"
    assert result["subtitle_language"] == "en"
    assert result["subtitle_track"] == "en-orig"
    assert ydl.call_args.args[0]["writeautomaticsub"] is False


def test_reference_failure_is_optional_and_resume_does_not_reuse_legacy_track(tmp_path):
    work = tmp_path / "sample" / "subtitle"
    work.mkdir(parents=True)
    (work / "【下载字幕】.en.vtt").write_text("wrong legacy reference")
    info = {"title": "sample", "language": "en", "subtitles": {"en": [{"url": "https://x/manual"}]}}
    service = VideoDownloadService("https://x/video", str(tmp_path), proxy_url="", cookie_file="")
    with (patch("app.core.download_service._extract_metadata_info", return_value=info),
          patch("app.core.download_service._download_subtitle_fallback", side_effect=requests.HTTPError("429")),
          patch("app.core.download_service.yt_dlp.YoutubeDL")):
        result = service.download(need_video=False, subtitle_mode="prefer_manual", subtitle_language="auto", resume_existing=True)
    assert result["subtitle_path"] is None
    assert result["subtitle_kind"] is None


def test_resume_reuses_reference_with_known_language_and_source(tmp_path):
    work = tmp_path / "sample" / "subtitle"
    work.mkdir(parents=True)
    saved = work / "【下载字幕】_en_manual.vtt"
    saved.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello")
    info = {"title": "sample", "language": "en", "subtitles": {"en": [{"url": "https://x/manual"}]}}
    service = VideoDownloadService("https://x/video", str(tmp_path), proxy_url="", cookie_file="")
    with (patch("app.core.download_service._extract_metadata_info", return_value=info),
          patch("app.core.download_service._download_subtitle_fallback") as download,
          patch("app.core.download_service.yt_dlp.YoutubeDL")):
        result = service.download(need_video=False, subtitle_mode="prefer_manual", subtitle_language="auto", resume_existing=True)
    download.assert_not_called()
    assert result["subtitle_path"] == str(saved)
    assert result["subtitle_kind"] == "manual"


@pytest.mark.parametrize("policy", [None, "prefer_manual"])
def test_worker_persists_actual_reference_metadata_and_preserves_resumed_legacy_state(tmp_path, policy):
    import pytest
    from app.mcp.worker import Worker, _same_reference_language

    worker = Worker(tmp_path / "registry", "a" * 32, "token")
    flow = tmp_path / "flow"
    flow.mkdir()
    media = tmp_path / "download.mp4"
    media.write_bytes(b"media")
    worker.store.save({"job_id": "a" * 32, "directory": str(tmp_path), "flow_dir": str(flow),
        "worker": {"token": "token"}, "options": {"url": "https://x/video", "source_language": "auto",
        "format_selector": "best", "proxy_url": "", "cookie_file": ""}})

    if policy:
        with worker.store.edit("a" * 32) as state:
            state["options"]["source_subtitle_policy"] = policy

    class StopAfterDownload(Exception):
        pass

    def stage(name, **kwargs):
        if name == "extracting":
            raise StopAfterDownload()

    with (patch.object(worker, "stage", side_effect=stage),
          patch("app.core.download_service.VideoDownloadService") as service):
        service.return_value.download.return_value = {"media_path": str(media), "info_dict": {"title": "Sample"},
            "subtitle_path": "/tmp/reference.vtt", "subtitle_language": "en-us", "subtitle_kind": "manual",
            "subtitle_track": "en-US"}
        with pytest.raises(StopAfterDownload):
            worker.run()
        state = worker.store.read("a" * 32)
        assert state["source_subtitle_language"] == "en-us"
        assert state["source_subtitle_kind"] == "manual"
        assert state["source_subtitle_track"] == "en-US"
        assert service.call_args.kwargs["subtitle_language"] == "auto"
        assert service.call_args.kwargs["subtitle_mode"] == (policy or "auto")
        assert _same_reference_language("en", state["source_subtitle_language"])
        assert not _same_reference_language("uk", state["source_subtitle_language"])
        assert not _same_reference_language("auto", "auto")
        assert not _same_reference_language("en", None)
        # Resuming downloaded legacy media does not fabricate reference metadata.
        for key in ("source_subtitle_language", "source_subtitle_kind", "source_subtitle_track"):
            state.pop(key)
        worker.store.save(state)
        service.reset_mock()
        with pytest.raises(StopAfterDownload):
            worker.run()
        service.assert_not_called()
        state = worker.store.read("a" * 32)
        assert "source_subtitle_language" not in state
        assert "source_subtitle_kind" not in state
        assert "source_subtitle_track" not in state


def test_multi_dub_metadata_selects_original_audio_language_then_manual():
    info = {"language": None, "formats": [
        {"language": "uk", "language_preference": -1, "format_note": "Ukrainian, medium", "abr": 300},
        {"language": "en-US", "language_preference": 10, "format_note": "English (US) original (default), low", "abr": 48}],
        "subtitles": {"en": [{"url": "https://x/manual", "ext": "vtt"}]},
        "automatic_captions": {"uk-orig": [{"url": "https://x/?lang=uk"}],
                               "en": [{"url": "https://x/?lang=en"}],
                               "en-orig": [{"url": "https://x/?lang=en"}]}}
    selected = _subtitle_candidates(info, "prefer_manual", "auto")
    assert [(x["kind"], x["language"]) for x in selected] == [("manual", "en"), ("auto", "en")]
    assert selected[1]["track"] == "en-orig"
    info["subtitles"] = {}
    assert _subtitle_candidates(info, "prefer_manual", "auto")[0]["language"] == "en"


def test_unknown_language_does_not_infer_from_machine_translation():
    info = {"subtitles": {"en": [{"url": "https://x/?lang=uk&tlang=en"}]}}
    assert _subtitle_candidates(info, "prefer_manual", "auto") == []
