"""Exercise the real yt-dlp selector without downloading any media."""
import pytest
from app.core.download_service import VideoDownloadService, _create_youtube_dl, _resolution_first_format_selector


def video(codec, height=1080, fps=60, combined=False):
    return {"format_id": f"{codec}-{height}-{fps}", "height": height,
            "fps": fps, "ext": "mp4", "vcodec": codec,
            "acodec": "mp4a.40.2" if combined else "none",
            "url": f"https://example.invalid/{codec}/{height}/{fps}"}


def selected(formats, mode="video_audio", audio=True):
    formats = list(formats)
    if audio:
        formats.append({"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2",
                        "ext": "m4a", "url": "https://example.invalid/audio"})
    selector = _resolution_first_format_selector({"formats": formats}, mode)
    with _create_youtube_dl({"format": selector, "quiet": True, "skip_download": True}) as ydl:
        result = ydl.process_ie_result({"id": "sample", "title": "sample", "formats": formats}, download=False)
    return str(result.get("format_id"))


@pytest.mark.parametrize("hevc", ["hvc1.1.6", "hev1.1.6", "hevc", "h265"])
def test_same_resolution_prefers_hevc_then_avc(hevc):
    assert selected([video("av01"), video("avc1"), video(hevc)]).startswith(hevc)
    assert selected([video("av01"), video("vp9"), video("avc1")]).startswith("avc1")


def test_higher_resolution_wins_over_codec_and_highest_fps_wins_within_codec():
    assert selected([video("hvc1"), video("avc1"), video("av01", 2160)]).startswith("av01-2160")
    assert selected([video("avc1", fps=30), video("avc1", fps=60)]).startswith("avc1-1080-60")


def test_combined_stream_and_video_only_work_without_separate_audio():
    assert selected([video("avc1", combined=True)], audio=False).startswith("avc1")
    assert selected([video("vp9")], mode="video", audio=False).startswith("vp9")


def test_explicit_choices_and_audio_only_are_preserved(tmp_path):
    info = {"formats": [video("av01", 2160), video("avc1")]}
    service = VideoDownloadService("https://example.invalid", str(tmp_path), format_selector="custom")
    assert service._effective_format_selector_for_info(info) == "custom"
    service.format_selector = ""
    service.selected_video_format_id = "avc1-1080-60"
    assert service._effective_format_selector_for_info(info) == "avc1-1080-60"
    assert _resolution_first_format_selector(info, "audio") == "bestaudio/best"


def test_missing_resolution_uses_general_selector():
    assert _resolution_first_format_selector({"formats": []}, "video_audio") == "bv*+ba/bestvideo+bestaudio/best"


def test_legacy_download_policy_can_keep_general_selection(tmp_path):
    service = VideoDownloadService("https://example.invalid", str(tmp_path), prefer_compatible_codecs=False)
    assert service._effective_format_selector_for_info({"formats": [video("avc1")]}) == "bv*+ba/bestvideo+bestaudio/best"
