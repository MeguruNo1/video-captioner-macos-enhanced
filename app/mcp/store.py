"""Atomic task snapshots and process-shared locks on POSIX and Windows."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import time

from app.core.utils.platform_utils import app_data_dir, default_work_dir

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_ROOT = app_data_dir("VideoCaptioner") / "mcp"
DEFAULT_OUTPUT = default_work_dir("VideoCaptioner")


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    import errno
                    if not blocking or exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("VIDEOCAPTIONER_MCP_ROOT", DEFAULT_ROOT)).expanduser().resolve()

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        return self.root / "jobs" / f"{job_id}.json"

    def read(self, job_id):
        try:
            return json.loads(self.path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("Unknown job ID") from None

    def save(self, state):
        state["event_id"] = state.get("event_id", 0) + 1
        state["updated_at"] = time.time()
        atomic_json(self.path(state["job_id"]), state)

    @contextmanager
    def edit(self, job_id):
        with file_lock(self.path(job_id).with_suffix(".lock")):
            state = self.read(job_id)
            before = deepcopy(state)
            yield state
            if state != before:
                self.save(state)
