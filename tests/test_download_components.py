from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import download_components as dc

VERSIONS = {"yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0", "bgutil-ytdlp-pot-provider": "1.3.1"}


def make_bundle(root: Path, name: str):
    path = root / "versions" / name
    for item in ("python/yt_dlp/__init__.py", "python/yt_dlp_ejs/__init__.py",
                 "provider/server/build/generate_once.js", "provider/server/package.json",
                 "python/yt_dlp_plugins/extractor/getpot_bgutil_script.py"):
        target = path / item
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    dc.write_page_state(path / "manifest.json", {"packages": VERSIONS})
    return path


def test_schedule_respects_frequency_and_failed_attempt(tmp_path):
    dc._save(tmp_path, {"last_attempt": 1000})
    assert not dc.check_due("关闭", tmp_path, 10**9)
    assert not dc.check_due("每天", tmp_path, 1001)
    assert dc.check_due("每天", tmp_path, 87400)
    assert not dc.check_due("每周", tmp_path, 87400)


def test_ejs_version_comes_from_downloader_metadata(monkeypatch):
    calls = []
    def release(name, proxy, constraint):
        calls.append((name, constraint))
        return {"version": VERSIONS[name], "requires_dist": ["yt-dlp-ejs==0.8.0; extra == 'default'"]}
    monkeypatch.setattr(dc, "_release", release)
    assert dc._plan("") == VERSIONS
    assert ("yt-dlp-ejs", "==0.8.0") in calls


def test_failed_check_records_backoff_but_preserves_selection(tmp_path, monkeypatch):
    dc._save(tmp_path, {"current": "old", "installed": VERSIONS})
    monkeypatch.setattr(dc, "_plan", Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        dc.check_updates(root=tmp_path)
    state = dc._state(tmp_path)
    assert state["current"] == "old"
    assert state["last_attempt"] > 0
    assert "last_checked" not in state


def test_failed_install_keeps_previous_and_cleans_staging(tmp_path, monkeypatch):
    make_bundle(tmp_path, "old")
    dc._save(tmp_path, {"current": "old", "installed": VERSIONS})
    monkeypatch.setattr(dc, "_plan", lambda _: VERSIONS)
    monkeypatch.setattr(dc, "_run", Mock(side_effect=RuntimeError("pip failed")))
    with pytest.raises(RuntimeError, match="pip failed"):
        dc.install_updates(root=tmp_path)
    assert dc._state(tmp_path)["current"] == "old"
    assert [p.name for p in (tmp_path / "versions").iterdir()] == ["old"]


def test_success_is_published_only_after_validation_and_can_rollback(tmp_path, monkeypatch):
    old = make_bundle(tmp_path, "old")
    dc._save(tmp_path, {"current": "old", "installed": VERSIONS})
    monkeypatch.setattr(dc, "_plan", lambda _: VERSIONS)
    def run(args, env, log, cwd=None):
        assert dc._state(tmp_path)["current"] == "old"
        if "--target" in args:
            target = Path(args[args.index("--target") + 1])
            make_bundle(tmp_path, target.parent.name)
    monkeypatch.setattr(dc, "_run", run)
    monkeypatch.setattr(dc, "_prepare_provider", lambda *args: None)
    dc.install_updates(root=tmp_path)
    assert dc._state(tmp_path)["current"] != "old"
    assert old.exists()
    dc.rollback(tmp_path)
    assert dc._state(tmp_path)["current"] == "old"


def test_first_update_can_roll_back_to_bundled_dependencies(tmp_path):
    make_bundle(tmp_path, "new")
    dc._save(tmp_path, {"current": "new", "previous": "", "installed": VERSIONS})
    dc.rollback(tmp_path)
    assert dc._state(tmp_path)["current"] == ""
    assert dc._state(tmp_path)["installed"] == {}


def test_corrupt_bundle_and_traversal_rejected(tmp_path):
    assert dc._bundle(tmp_path, "../outside") is None
    assert dc._bundle(tmp_path, "missing") is None
    make_bundle(tmp_path, "broken")
    (tmp_path / "versions/broken/python/yt_dlp_ejs/__init__.py").unlink()
    assert dc._bundle(tmp_path, "broken") is None


def test_bootstrap_keeps_process_on_immutable_generation(tmp_path, monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr(dc, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(dc, "_ACTIVE", "")
    monkeypatch.setenv("VIDEOCAPTIONER_COMPONENT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv(dc.PROVIDER_ENV, "old")
    first = make_bundle(tmp_path, "first")
    make_bundle(tmp_path, "second")
    dc._save(tmp_path, {"current": "first"})
    dc.activate_download_components()
    dc._save(tmp_path, {"current": "second", "previous": "first"})
    dc.activate_download_components()
    assert sys.path[0] == str(first / "python")
    assert dc._ACTIVE == "first"


def test_concurrent_update_is_rejected(tmp_path):
    from filelock import FileLock, Timeout
    with FileLock(str(tmp_path / "update.lock")):
        with pytest.raises(Timeout):
            dc.check_updates(root=tmp_path)


def test_bootstrap_respects_explicit_rollback_to_bundled(tmp_path, monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr(dc, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(dc, "_ACTIVE", "")
    monkeypatch.setenv("VIDEOCAPTIONER_COMPONENT_ROOT", str(tmp_path))
    old = make_bundle(tmp_path, "old")
    monkeypatch.setenv(dc.PROVIDER_ENV, str(old / "provider/server"))
    original = list(sys.path)
    dc._save(tmp_path, {"current": "", "previous": "old"})
    dc.activate_download_components()
    assert sys.path == original
    assert dc._ACTIVE == ""
    assert dc.PROVIDER_ENV not in dc.os.environ


def test_bootstrap_falls_back_when_current_bundle_is_broken(tmp_path, monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr(dc, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(dc, "_ACTIVE", "")
    monkeypatch.setenv("VIDEOCAPTIONER_COMPONENT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv(dc.PROVIDER_ENV, "old")
    previous = make_bundle(tmp_path, "previous")
    dc._save(tmp_path, {"current": "missing", "previous": "previous"})
    dc.activate_download_components()
    assert sys.path[0] == str(previous / "python")
    assert dc._ACTIVE == "previous"
