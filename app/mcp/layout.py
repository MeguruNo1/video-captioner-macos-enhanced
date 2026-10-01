"""Stable on-disk layout for headless VideoCaptioner jobs."""
from pathlib import Path
import shutil

from app.core.download_service import sanitize_filename

from .store import atomic_json


def _replace_root(value, old_root: Path, new_root: Path):
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    try:
        relative = path.relative_to(old_root)
    except ValueError:
        return value
    return str(new_root / relative)


def _available_title_directory(output_root: Path, title: str, job_id: str) -> Path:
    base = output_root / sanitize_filename(title or "未命名视频")
    candidate = base
    suffix = 2
    while candidate.exists():
        marker = candidate / "flow" / "task.json"
        try:
            if marker.exists() and marker.read_text(encoding="utf-8").find(job_id) >= 0:
                return candidate
        except OSError:
            pass
        candidate = output_root / f"{base.name} ({suffix})"
        suffix += 1
    return candidate


def ensure_job_layout(state: dict) -> tuple[Path, Path, Path]:
    """Move legacy/staging files into <title>/flow and return root/flow/output."""
    current = Path(state["directory"]).expanduser().resolve()
    if state.get("flow_dir"):
        root = current
        flow = Path(state["flow_dir"])
        output = Path(state.get("output_dir") or root / "output")
        flow.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        return root, flow, output

    metadata = state.get("metadata") or {}
    title = sanitize_filename(str(metadata.get("title") or "未命名视频"))
    output_root = Path(state.get("output_root") or current.parent).expanduser().resolve()
    root = _available_title_directory(output_root, title, state["job_id"])
    flow = root / "flow"
    output = root / "output"
    flow.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)

    if current != root and current.exists():
        for child in list(current.iterdir()):
            destination = flow / child.name
            if destination.exists():
                continue
            shutil.move(str(child), str(destination))
        try:
            current.rmdir()
        except OSError:
            pass
    elif current == root:
        for child in list(root.iterdir()):
            if child.name in {"flow", "output"}:
                continue
            shutil.move(str(child), str(flow / child.name))

    for key in ("video_path", "audio_path", "thumbnail_path", "source_transcript_path", "source_subtitle_path", "generated_cover_path"):
        state[key] = _replace_root(state.get(key), current, flow)
    state.update(directory=str(root), flow_dir=str(flow), output_dir=str(output), video_title=title)
    atomic_json(flow / "task.json", {
        "job_id": state["job_id"],
        "source_url": state["options"]["url"],
        "source_language": state["options"]["source_language"],
        "target_language": state["options"]["target_language"],
        "model": state["options"].get("model"),
    })
    return root, flow, output
