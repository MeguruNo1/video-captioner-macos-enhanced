#!/usr/bin/env python3
"""Stable absolute-path entrypoint, independent of the caller's working directory."""
from pathlib import Path
import os
import runpy
import sys

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
if sys.platform == "darwin":
    os.environ["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
runpy.run_module("app.mcp.server", run_name="__main__")
