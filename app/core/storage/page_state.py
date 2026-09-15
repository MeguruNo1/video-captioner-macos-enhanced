"""Small, atomic snapshots for restoring local workspaces."""

import json
import os
from pathlib import Path


def read_page_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) and payload.get("version") == 1 else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_page_state(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({**payload, "version": 1}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def file_signature(path: str) -> dict | None:
    try:
        source = Path(path)
        stat = source.stat()
        if source.is_file():
            return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except (OSError, TypeError, ValueError):
        pass
    return None
