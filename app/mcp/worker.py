"""Detached worker. It never calls a translation API or starts a GUI."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import traceback
from uuid import uuid4

from .captions import make_batches, words_from_result
from .layout import ensure_job_layout
from .store import Store, atomic_json, file_lock


class Revoked(Exception):
    pass


class Worker:
    def __init__(self, root, job_id, token):
        self.store = Store(root)
        self.job_id = job_id
        self.token = token

    def update(self, **updates):
        with self.store.edit(self.job_id) as state:
            if (state.get("worker") or {}).get("token") != self.token:
                raise Revoked()
            state.update(updates)
        return state

    def stage(self, name, progress=0, message=""):
        return self.update(status=name, stage=name, progress=progress, message=message)

    def transcribe(self, path, options, prompt=None):
        initial_prompt = options["initial_prompt"] if prompt is None else prompt
        language = None if options["source_language"] == "auto" else options["source_language"]
        backend = options.get("backend", "mlx")  # Existing jobs retain MLX/native timestamps.
        if backend == "mlx":
            from app.core.bk_asr.mlx_whisper import MLXWhisperASR, build_mlx_initial_prompt
            asr = MLXWhisperASR(str(path), model=options["model"], language=language,
                               initial_prompt=build_mlx_initial_prompt(initial_prompt, options.get("mlx_hotwords")),
                               need_word_time_stamp=True, alignment_method="native", use_cache=False,
                               vad_enabled=options["vad_enabled"],
                               vad_threshold=options.get("vad_threshold", 0.5),
                               chunk_duration=options.get("chunk_duration", 600),
                               chunk_overlap=options.get("chunk_overlap", 30))
        elif backend == "whisperx":
            from app.core.bk_asr.whisper_x_auto import WhisperXASR
            from app.core.utils.platform_utils import app_data_dir
            asr = WhisperXASR(str(path), whisper_model=options["model"], language=language,
                              device=options["device"], compute_type=options["compute_type"],
                              batch_size=options.get("batch_size", 8), initial_prompt=initial_prompt,
                              hotwords=options.get("mlx_hotwords", ""),
                              vad_method=options.get("vad_method", "silero"),
                              vad_threshold=options.get("vad_threshold", 0.5),
                              local_silero_dir=options.get("local_silero_dir", ""),
                              model_dir=str(app_data_dir("VideoCaptioner") / "models"),
                              align=True, need_word_time_stamp=True, use_cache=False)
        else:
            raise ValueError(f"Unknown saved ASR backend: {backend}")
        return asr._run(lambda progress, message: self.update(progress=progress, message=message))

    def run(self):
        with file_lock(self.store.path(self.job_id).with_suffix(".worker.lock"), blocking=False):
            state = self.update(error=None)
            directory = Path(state.get("flow_dir") or state["directory"])
            options = state["options"]
            if not state.get("video_path") or not Path(state["video_path"]).is_file():
                self.stage("downloading", message="Downloading video")
                from app.core.download_service import VideoDownloadService
                service = VideoDownloadService(options["url"], str(directory / "download"),
                           need_subtitle=True, subtitle_mode="auto",
                           subtitle_language=options["source_language"] if options["source_language"] != "auto" else "en",
                           need_transcript_txt=True, need_thumbnail=True,
                           format_selector=options["format_selector"],
                           download_engine_strategy=options.get("download_engine_strategy"),
                           pr_smart_transcode_hevc_on_av1=True,
                           description_txt_template=options.get("description_txt_template"),
                           native_hevc_preset=options.get("native_hevc_preset", "highest_quality"),
                           proxy_url=options["proxy_url"], cookie_file=options["cookie_file"],
                           progress_callback=lambda p, m: self.update(progress=p, message=m))
                result = service.download(need_subtitle=True, subtitle_mode="auto",
                                          subtitle_language=options["source_language"] if options["source_language"] != "auto" else "en",
                                          need_transcript_txt=True, need_thumbnail=True, resume_existing=True,
                                          pr_smart_transcode_hevc_on_av1=True)
                if result.get("postprocess_failed"):
                    raise RuntimeError(result.get("postprocess_message") or "VP9/AV1 to HEVC conversion failed")
                video_path = result.get("media_path") or result.get("video_path")
                if not video_path or not Path(video_path).is_file():
                    raise ValueError("Download did not produce a video with audio")
                info = result.get("info_dict", {})
                metadata = {key: info.get(key) for key in ("title", "description", "uploader", "channel",
                            "uploader_url", "channel_url", "webpage_url", "original_url", "upload_date", "duration")}
                atomic_json(directory / "metadata.json", metadata)
                term_data = {"hotwords": "", "glossary": {}, "candidates": []}
                transcript_path = result.get("transcript_txt_path")
                if transcript_path and Path(transcript_path).is_file():
                    from .terms import extract_task_terms
                    transcript = Path(transcript_path).read_text(encoding="utf-8")
                    glossary_path = options.get("workflow_settings", {}).get("subtitle", {}).get("glossary_path", "")
                    term_data = extract_task_terms(transcript, glossary_path)
                    atomic_json(directory / "task-terms.json", term_data)
                    if term_data["hotwords"]:
                        options["mlx_hotwords"] = term_data["hotwords"]
                state = self.update(video_path=video_path, metadata=metadata,
                                    thumbnail_path=result.get("thumbnail_path"),
                                    source_transcript_path=transcript_path,
                                    term_candidates=term_data["candidates"],
                                    base_glossary=term_data["glossary"],
                                    glossary=term_data["glossary"], options=options)
                with self.store.edit(self.job_id) as live_state:
                    if (live_state.get("worker") or {}).get("token") != self.token:
                        raise Revoked()
                    _, directory, _ = ensure_job_layout(live_state)
                    state = live_state
            audio = directory / "audio.wav"
            if not state.get("audio_path") or not audio.is_file():
                self.stage("extracting", message="Extracting mono 16 kHz audio")
                temporary = directory / "audio.partial.wav"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", state["video_path"],
                                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-af", "aresample=async=1",
                                str(temporary)], check=True)
                temporary.replace(audio)
                from app.core.bk_asr.mlx_workflow import probe_audio_duration
                duration = probe_audio_duration(audio)
                if not duration or duration <= 0:
                    raise ValueError("Audio duration could not be determined")
                state = self.update(audio_path=str(audio), duration_ms=round(duration * 1000))
            backend = options.get("backend", "mlx")
            self.stage("waiting_for_asr", message=f"Waiting for local {backend} worker slot")
            with file_lock(self.store.root / f"{backend}.lock"):
                # Cancellation while waiting is handled by the worker's SIGTERM.
                if state.get("retranscribe"):
                    self.retranscribe(state, directory, audio, options)
                elif not state["words"]:
                    self.stage("transcribing", message=f"Transcribing with local {backend} / {options.get('device', 'metal')}")
                    raw_path = directory / "transcript-original.json"
                    words = None
                    if raw_path.exists():
                        try:
                            result = json.loads(raw_path.read_text(encoding="utf-8"))
                            words = words_from_result(result)
                        except (ValueError, KeyError):
                            pass
                    if words is None:
                        result = self.transcribe(audio, options)
                        # Never overwrite the original ASR response on retries.
                        destination = raw_path if not raw_path.exists() else directory / f"transcript-retry-{uuid4().hex}.json"
                        atomic_json(destination, result)
                        words = words_from_result(result)
                    self.update(words=words, batches=make_batches(words))
            # Record import isolation as useful diagnostic evidence.
            forbidden = [name for name in sys.modules if name.startswith("PyQt5") or name == "openai" or name.startswith("openai.")
                         or (backend == "mlx" and (name == "whisperx" or name.startswith("whisperx.") or name == "app.core.bk_asr.whisper_x_auto"))]
            atomic_json(directory / "runtime.json", {"asr": backend,
                        "device": options.get("device", "metal"), "compute_type": options.get("compute_type", "model"),
                        "alignment": "native" if backend == "mlx" else "whisperx", "unexpected_modules": forbidden})
            if forbidden:
                raise RuntimeError("Headless worker imported an unexpected GUI/API or unrelated ASR module: " + ", ".join(forbidden))
            self.update(status="awaiting_captions", stage="awaiting_captions", progress=100,
                        message="Transcription saved. Codex can now process caption batches.", worker=None)

    def retranscribe(self, state, directory, audio, options):
        from app.core.bk_asr.mlx_workflow import extract_audio_chunk
        self.stage("retranscribing", message="Retranscribing complete affected caption batches")
        request = state["retranscribe"]
        affected = [b for b in state["batches"] if b["id"] in request["batch_ids"]]
        words = state["words"]
        ids = [word["id"] for word in words]
        first = ids.index(affected[0]["start_word_id"])
        last = ids.index(affected[-1]["end_word_id"])
        # Neighbor anchors define a stable splice window; no new timing is guessed.
        start = max(0, words[first-1]["end_ms"] if first else 0) / 1000
        end = (words[last+1]["start_ms"] if last+1 < len(words) else state["duration_ms"]) / 1000
        if end <= start:
            raise ValueError("Invalid splice anchors; select a wider word range")
        run_id = uuid4().hex
        chunk = extract_audio_chunk(audio, directory / f"retranscribe-{run_id}.wav", start, end)
        result = self.transcribe(chunk, options, request["initial_prompt"])
        atomic_json(directory / f"transcript-{run_id}.json", {"start_seconds": start, "end_seconds": end, "result": result})
        replacement = words_from_result(result, prefix=f"r{run_id}-", offset=start)
        replacement_batches = make_batches(replacement)
        affected_ids = set(request["batch_ids"])
        new_batches = []
        for batch in state["batches"]:
            if batch["id"] == affected[0]["id"]:
                new_batches.extend(replacement_batches)
            if batch["id"] not in affected_ids:
                new_batches.append(batch)
        self.update(words=words[:first] + replacement + words[last+1:], batches=new_batches,
                    glossary={k: v for b in new_batches for k, v in b["glossary"].items()},
                    revision=state["revision"]+1, artifacts={}, retranscribe=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    worker = Worker(args.root, args.job, args.token)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        worker.run()
    except Revoked:
        pass
    except Exception as exc:
        traceback.print_exc()
        try:
            worker.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                          message="Inspect worker.log, correct the cause, then call resume_job.", worker=None)
        except Revoked:
            pass


if __name__ == "__main__":
    main()
