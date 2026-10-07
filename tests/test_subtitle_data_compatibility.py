import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.bk_asr.asr_data import ASRData
from app.view.subtitle_interface import SubtitleInterface


def legacy_subtitles() -> dict:
    return {
        "2": {
            "start_time": 1100,
            "end_time": 2000,
            "original_subtitle": "world!",
            "translated_subtitle": "世界！",
            "speaker": "SPEAKER_01",
        },
        "1": {
            "start_time": 100,
            "end_time": 1000,
            "original_subtitle": "Hello",
            "translated_subtitle": "你好，",
            "speaker": "SPEAKER_00",
        },
    }


def test_legacy_json_roundtrip_keeps_text_order_and_timing(tmp_path: Path):
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(legacy_subtitles()), encoding="utf-8")
    data = ASRData.from_subtitle_file(str(source))
    target = tmp_path / "export.json"
    data.save(str(target))

    assert json.loads(target.read_text(encoding="utf-8")) == {
        key: {field: value for field, value in row.items() if field != "speaker"}
        for key, row in legacy_subtitles().items()
    }
    assert [(seg.text, seg.start_time, seg.end_time) for seg in data] == [
        ("Hello", 100, 1000), ("world!", 1100, 2000)
    ]


@pytest.mark.parametrize("suffix", [".txt", ".srt"])
@pytest.mark.parametrize(
    ("layout", "lines"),
    [
        ("原文在上", ["Hello\n你好，", "world!\n世界！"]),
        ("译文在上", ["你好，\nHello", "世界！\nworld!"]),
        ("仅原文", ["Hello", "world!"]),
        ("仅译文", ["你好，", "世界！"]),
    ],
)
def test_legacy_exports_keep_layout_and_timestamps(tmp_path: Path, suffix, layout, lines):
    data = ASRData.from_json(legacy_subtitles())
    target = tmp_path / ("subtitles" + suffix)
    data.save(str(target), layout=layout)

    expected = "\n".join(lines)
    if suffix == ".srt":
        expected = (
            f"1\n00:00:00,100 --> 00:00:01,000\n{lines[0]}\n\n"
            f"2\n00:00:01,100 --> 00:00:02,000\n{lines[1]}\n"
        )
    assert target.read_text(encoding="utf-8") == expected


def test_separate_exports_keep_both_languages(tmp_path: Path):
    data = ASRData.from_json(legacy_subtitles())
    target = tmp_path / "subtitles.txt"
    data.save(str(target), layout="单独输出原文和译文")

    assert target.read_text(encoding="utf-8") == "你好，\n世界！"
    assert (tmp_path / "subtitles-仅原文.txt").read_text(encoding="utf-8") == "Hello\nworld!"
    assert (tmp_path / "subtitles-仅译文.txt").read_text(encoding="utf-8") == "你好，\n世界！"


@pytest.mark.parametrize(
    ("layout", "mode", "expected"),
    [
        ("原文在上", "auto", "Hello world!\n你好，世界！"),
        ("译文在上", "auto", "你好，世界！\nHello world!"),
        ("仅原文", "auto", "Hello world!"),
        ("仅译文", "auto", "你好，世界！"),
        ("单独输出原文和译文", "auto", "你好，世界！"),
        ("原文在上", "original", "Hello world!"),
        ("原文在上", "translated", "你好，世界！"),
    ],
)
def test_full_script_uses_plain_text_from_legacy_data(layout, mode, expected):
    assert SubtitleInterface._build_full_script_text(
        legacy_subtitles(), layout=layout, display_mode=mode
    ) == expected


def test_full_script_translation_fallback_and_empty_data():
    rows = legacy_subtitles()
    for row in rows.values():
        row["translated_subtitle"] = ""
    assert SubtitleInterface._build_full_script_text(
        rows, layout="仅译文", display_mode="translated"
    ) == "Hello world!"
    assert SubtitleInterface._build_full_script_text({}, layout="原文在上") == ""
