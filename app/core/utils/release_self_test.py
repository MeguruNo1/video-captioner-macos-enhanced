"""Explicit release acceptance using the installed application's own runtime."""
import argparse
import contextlib
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path


REFERENCE = "and so my fellow americans ask not what your country can do for you ask what you can do for your country"


def _word_error_rate(text: str) -> float:
    expected = REFERENCE.split()
    actual = re.findall(r"[a-z]+", text.lower())
    row = list(range(len(actual) + 1))
    for index, word in enumerate(expected, 1):
        next_row = [index]
        for column, other in enumerate(actual, 1):
            next_row.append(min(row[column] + 1, next_row[-1] + 1,
                                row[column - 1] + (word != other)))
        row = next_row
    return row[-1] / len(expected)


def run_self_test(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["whisperx", "mlx"], default="whisperx")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args(arguments)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"backend": args.backend, "model": Path(args.model).name if Path(args.model).is_dir() else args.model,
              "frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    start = time.monotonic()
    with (args.output / "functional-test.log").open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                _exercise(args, report)
                report["status"] = "passed"
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = str(exc)
                traceback.print_exc()
            report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    (args.output / "functional-test.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


def _exercise(args: argparse.Namespace, report: dict) -> None:
    from PyQt5.QtWidgets import QApplication
    from app.common.config import cfg
    from app.config import BIN_PATH
    from app.core.bk_asr.transcribe import transcribe
    from app.core.entities import TranscribeConfig, TranscribeModelEnum
    from app.core.utils.acceleration import inspect_acceleration

    app = QApplication.instance() or QApplication([])
    cfg.set(cfg.download_auto_extract_cookies_on_startup, False)
    report["hardware"] = inspect_acceleration()
    ffmpeg = BIN_PATH / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    ffprobe = BIN_PATH / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    # Source checks may use installed tools; frozen acceptance must use bundled ones.
    ffmpeg_command = str(ffmpeg) if ffmpeg.is_file() else "ffmpeg"
    ffprobe_command = str(ffprobe) if ffprobe.is_file() else "ffprobe"
    if getattr(sys, "frozen", False) and not (ffmpeg.is_file() and ffprobe.is_file()):
        raise RuntimeError("The installed application is missing bundled media tools")
    video = args.output / "sample.mp4"
    audio = args.output / "extracted.wav"
    subprocess.run([ffmpeg_command, "-nostdin", "-y", "-f", "lavfi", "-i",
                    "color=c=navy:s=640x360:r=25", "-i", str(args.audio.resolve()),
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video)],
                   check=True, capture_output=True)
    probe = subprocess.run([ffprobe_command, "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(video)], check=True, capture_output=True, text=True)
    metadata = json.loads(probe.stdout)
    if {stream["codec_type"] for stream in metadata["streams"]} != {"video", "audio"}:
        raise RuntimeError("Generated sample does not contain both media streams")
    subprocess.run([ffmpeg_command, "-nostdin", "-y", "-i", str(video), "-vn", "-ar",
                    "16000", "-ac", "1", str(audio)], check=True, capture_output=True)
    report["bundled_media_tools"] = ffmpeg.is_file() and ffprobe.is_file()
    report["checks"].append("FFmpeg video encode, ffprobe stream inspection and audio extraction")
    config = TranscribeConfig()
    config.transcribe_language = "en"
    config.use_asr_cache = False
    config.whisperx_device = "cpu"
    config.whisperx_compute_type = "int8"
    config.whisperx_batch_size = 2
    config.whisperx_model = args.model
    config.whisperx_model_dir = str(args.model_dir)
    config.whisperx_vad_method = "silero"
    if args.backend == "mlx":
        config.transcribe_model = TranscribeModelEnum.MLX_WHISPER
        config.mlx_model = args.model
        config.mlx_word_timestamps = True
    else:
        config.transcribe_model = TranscribeModelEnum.WHISPER_X
    result = transcribe(str(audio), config)
    if not result.has_data() or not result.is_word_timestamp():
        raise RuntimeError("Transcription did not produce word timestamps")
    text = " ".join(segment.text for segment in result.segments)
    report["transcript"] = text
    report["word_error_rate"] = _word_error_rate(text)
    if report["word_error_rate"] > 0.25:
        raise RuntimeError("Reference speech recognition exceeds 25 percent word error rate")
    duration = float(metadata["format"]["duration"])
    for segment in result.segments:
        if not 0 <= segment.start_time < segment.end_time <= (duration + 0.5) * 1000:
            raise RuntimeError("Word timestamps fall outside sample duration")
    result.to_srt(save_path=str(args.output / "sample.srt"))
    if "-->" not in (args.output / "sample.srt").read_text(encoding="utf-8-sig"):
        raise RuntimeError("SRT export is missing timestamps")
    (args.output / "transcript.txt").write_text(text, encoding="utf-8")
    report["word_count"] = len(result.segments)
    report["checks"].append("Real speech transcription, forced word alignment, reference text and SRT export")
    from app.view.main_window import MainWindow
    window = MainWindow()
    window.show()
    app.processEvents()
    if not window.grab().save(str(args.output / "application.png")):
        raise RuntimeError("Unable to save application screenshot")
    report["window_title"] = window.windowTitle()
    report["checks"].append("Native Qt main window construction and screenshot")
    window.close()
    app.processEvents()
