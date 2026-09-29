"""Versioned download-only updates. A running process keeps its original bundle."""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable
from uuid import uuid4

from app.core.storage.page_state import read_page_state, write_page_state
from app.core.utils.platform_utils import app_data_dir, IS_WINDOWS

PACKAGES = ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider")
PROVIDER_ENV = "VIDEO_CAPTIONER_BGUTIL_SERVER_HOME"
_ACTIVE = ""
_BOOTSTRAPPED = False


def component_root() -> Path:
    configured = os.environ.get("VIDEOCAPTIONER_COMPONENT_ROOT")
    return Path(configured) if configured else app_data_dir("VideoCaptioner") / "download-components"


def _state(root: Path) -> dict:
    return read_page_state(root / "state.json")


def _save(root: Path, state: dict) -> None:
    write_page_state(root / "state.json", state)
    if _state(root) != {**state, "version": 1}:
        raise OSError("无法保存下载组件状态；原版本保持不变")


def _bundle(root: Path, name: str) -> Path | None:
    if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
        return None
    path = root / "versions" / name
    required = ("python/yt_dlp/__init__.py", "python/yt_dlp_ejs/__init__.py",
                "provider/server/build/generate_once.js", "python/yt_dlp_plugins/extractor/getpot_bgutil_script.py",
                "provider/server/package.json", "manifest.json")
    if all((path / item).is_file() for item in required):
        return path
    return None


def activate_download_components() -> None:
    """Called at app package initialization, before any yt-dlp import (GUI or MCP)."""
    global _ACTIVE, _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    if "yt_dlp" in sys.modules:
        return  # Never mix an already imported downloader with a new plugin.
    root = component_root()
    state = _state(root)
    current = state.get("current", "")
    # Empty current explicitly means the application's original dependencies.
    candidates = (current, state.get("previous", "")) if current else ()
    inherited = os.environ.get(PROVIDER_ENV, "")
    if inherited and Path(inherited).is_relative_to(root / "versions"):
        os.environ.pop(PROVIDER_ENV, None)
    for name in candidates:
        path = _bundle(root, name)
        if path is not None:
            sys.path.insert(0, str(path / "python"))
            # Bind the matching server to this immutable generation too.
            os.environ[PROVIDER_ENV] = str(path / "provider" / "server")
            _ACTIVE = name
            break


def status(root: Path | None = None) -> dict:
    root = root or component_root()
    state = _state(root)
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "未安装"
    return {**state, "running": versions, "restart_required": state.get("current", "") != _ACTIVE,
            "can_install": not bool(getattr(sys, "frozen", False))}


def check_due(frequency: str, root: Path | None = None, now: float | None = None) -> bool:
    seconds = {"每天": 86400, "每周": 604800}.get(frequency)
    if seconds is None:
        return False
    try:
        last = float(_state(root or component_root()).get("last_attempt", 0))
    except (TypeError, ValueError):
        last = 0
    return (time.time() if now is None else now) - last >= seconds


def _release(name: str, proxy: str, constraint: str = "") -> dict:
    import requests
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    proxies = {"http": proxy, "https": proxy} if proxy else {}
    with requests.Session() as session:
        session.trust_env = False

        def fetch(suffix: str) -> dict:
            response = session.get(f"https://pypi.org/pypi/{name}/{suffix}json", proxies=proxies, timeout=30)
            response.raise_for_status()
            return response.json()

        data = fetch("")
        spec = SpecifierSet(constraint)
        candidates = [Version(v) for v, files in data["releases"].items()
                      if files and not Version(v).is_prerelease and Version(v) in spec
                      and any(not f.get("yanked") for f in files)]
        if not candidates:
            raise ValueError(f"找不到兼容的稳定版：{name}{constraint}")
        selected = str(max(candidates))
        if Version(data["info"]["version"]) != Version(selected):
            data = fetch(selected + "/")
        requirement = data["info"].get("requires_python") or ""
        if Version(".".join(map(str, sys.version_info[:3]))) not in SpecifierSet(requirement):
            raise ValueError(f"{name} {selected} 需要 Python {requirement}，请先更新应用运行环境")
        return data["info"]


def _plan(proxy: str) -> dict[str, str]:
    from packaging.requirements import Requirement

    downloader = _release("yt-dlp", proxy, ">=2026.8.19")
    ejs = next((Requirement(r) for r in downloader.get("requires_dist", [])
                if Requirement(r).name == "yt-dlp-ejs"), None)
    if ejs is None:
        raise ValueError("新版 yt-dlp 未声明 EJS 配套版本，暂不自动更新")
    engine = _release("yt-dlp-ejs", proxy, str(ejs.specifier))
    provider = _release("bgutil-ytdlp-pot-provider", proxy, ">=1.3.1,<2")
    return {"yt-dlp": downloader["version"], "yt-dlp-ejs": engine["version"],
            "bgutil-ytdlp-pot-provider": provider["version"]}


def check_updates(proxy: str = "", root: Path | None = None) -> dict:
    from filelock import FileLock

    root = root or component_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "update.lock"), timeout=0):
        state = _state(root)
        state["last_attempt"] = time.time()
        _save(root, state)
        versions = _plan(proxy)
        current = state.get("installed") or status(root)["running"]
        from packaging.version import Version
        available = any(current.get(k) in (None, "未安装") or Version(v) > Version(current[k])
                        for k, v in versions.items())
        state.update(last_checked=time.time(), available=available, candidate=versions)
        _save(root, state)
        return {"message": "发现下载组件更新" if available else "下载组件已是最新兼容稳定版",
                "available": available, "versions": versions}


def _run(args: list[str], env: dict[str, str], log: Path, cwd: Path | None = None) -> None:
    with log.open("ab") as output:
        result = subprocess.run(args, env=env, cwd=cwd, stdout=output, stderr=output,
                                timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
    if result.returncode:
        raise RuntimeError(f"组件准备失败（退出码 {result.returncode}），详情见 {log}")


def _tools_env(proxy: str) -> dict[str, str]:
    from app.core.utils.proxy_utils import build_download_proxy_env
    env = build_download_proxy_env("手动设置" if proxy else "不使用代理", proxy)
    # A previous generation may have been injected by the launcher. Explicitly
    # choose the staged generation only in the validation subprocess below.
    env.pop("PYTHONPATH", None)
    if not IS_WINDOWS:
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    return env


def _prepare_provider(stage: Path, version: str, env: dict[str, str], log: Path) -> None:
    from app.core.utils.youtube_pot_provider import find_bgutil_server_home
    node = shutil.which("node", path=env.get("PATH"))
    if not node:
        raise RuntimeError("未找到 Node.js；更新 YouTube 校验组件需要 Node.js 20 或更新版本")
    node_version = subprocess.run([node, "--version"], env=env, capture_output=True, text=True, timeout=15)
    if node_version.returncode or int(node_version.stdout.strip().lstrip("v").split(".")[0]) < 20:
        raise RuntimeError("YouTube 校验组件需要 Node.js 20 或更新版本")
    current = find_bgutil_server_home()
    if current:
        try:
            same = json.loads((current / "package.json").read_text())["version"] == version
        except (OSError, ValueError, KeyError):
            same = False
        if same:
            shutil.copytree(current, stage / "provider" / "server")
    if not (stage / "provider").exists():
        git = shutil.which("git", path=env.get("PATH"))
        npm = shutil.which("npm", path=env.get("PATH"))
        if not git or not npm:
            raise RuntimeError("更新 PO Token 本地服务需要 Git 和 npm；原版本保持不变")
        _run([git, "clone", "--depth", "1", "--branch", version,
              "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git", str(stage / "provider")], env, log)
        server = stage / "provider" / "server"
        # Use npm's JS entry point so Windows does not need shell=True for npm.cmd.
        npm_path = Path(npm).resolve()
        npm_cli = npm_path if npm_path.suffix == ".js" else npm_path.parent / "node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file():
            npm_cli = npm_path.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file():
            raise RuntimeError("无法定位 npm-cli.js，请检查 Node.js/npm 安装")
        _run([node, str(npm_cli), "ci"], env, log, server)
        _run([node, str(server / "node_modules/typescript/bin/tsc")], env, log, server)
        _run([node, str(npm_cli), "prune", "--omit=dev"], env, log, server)
    server = stage / "provider" / "server"
    result = subprocess.run([node, str(server / "build/generate_once.js"), "--version"],
                            env=env, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != version:
        raise RuntimeError("PO Token 插件与本地服务版本校验失败；原版本保持不变")


def install_updates(proxy: str = "", root: Path | None = None,
                    progress: Callable[[str], None] = lambda _: None) -> dict:
    from filelock import FileLock

    if getattr(sys, "frozen", False):
        raise RuntimeError("独立安装包目前仅支持检查更新；请更新应用安装包。源码版和 App 启动器支持组件更新")
    root = root or component_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "update.lock"), timeout=0):
        versions = _plan(proxy)
        generation = uuid4().hex
        stage = root / "versions" / generation
        stage.mkdir(parents=True)
        log = root / f"update-{generation}.log"
        env = _tools_env(proxy)
        try:
            progress("正在准备 yt-dlp 和匹配的校验组件…")
            _run([sys.executable, "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                  "--index-url", "https://pypi.org/simple", "--no-deps", "--only-binary=:all:",
                  "--target", str(stage / "python"),
                  *(["--proxy", proxy] if proxy else []),
                  *[f"{k}=={v}" for k, v in versions.items()]], env, log)
            progress("正在匹配 PO Token 插件和本地服务…")
            _prepare_provider(stage, versions["bgutil-ytdlp-pot-provider"], env, log)
            progress("正在验证组件兼容性…")
            validation_env = {**env, "PYTHONPATH": str(stage / "python")}
            _run([sys.executable, "-c", _VALIDATE], validation_env, log, stage)
            write_page_state(stage / "manifest.json", {"packages": versions})
            if _bundle(root, generation) is None:
                raise RuntimeError("组件文件不完整；原版本保持不变")
            state = _state(root)
            state.update(previous=state.get("current", ""), current=generation,
                         installed=versions, available=False, last_checked=time.time())
            _save(root, state)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return {"message": "下载组件已准备好，重启软件后生效；新启动的 MCP 工作进程会使用新版", "versions": versions}


def rollback(root: Path | None = None) -> dict:
    from filelock import FileLock

    root = root or component_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "update.lock"), timeout=0):
        state = _state(root)
        if not state.get("current"):
            raise ValueError("没有可以恢复的上一版")
        previous = state.get("previous", "")
        bundle = _bundle(root, previous) if previous else None
        if previous and bundle is None:
            raise ValueError("上一版文件缺失，无法恢复")
        installed = read_page_state(bundle / "manifest.json").get("packages", {}) if bundle else {}
        state.update(current=previous, previous=state["current"], installed=installed, available=False)
        _save(root, state)
    return {"message": "已恢复上一版选择，重启软件后生效"}


_VALIDATE = r'''
from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement
import yt_dlp, yt_dlp_ejs
from yt_dlp.plugins import load_all_plugins
assert Path(yt_dlp.__file__).resolve().is_relative_to(Path.cwd())
assert Path(yt_dlp_ejs.__file__).resolve().is_relative_to(Path.cwd())
for name in ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider"):
    for raw in metadata.requires(name) or []:
        req = Requirement(raw)
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        assert metadata.version(req.name) in req.specifier, str(req)
load_all_plugins()
import yt_dlp_plugins.extractor.getpot_bgutil_script as provider
assert Path(provider.__file__).resolve().is_relative_to(Path.cwd())
assert hasattr(provider, "BgUtilScriptNodePTP")
print("Download component imports and provider plugin OK")
'''
