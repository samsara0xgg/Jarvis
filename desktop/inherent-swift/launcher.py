#!/usr/bin/env python3
"""Launch the Swift InherentCard app for development.

Builds the Xcode project if the .app is missing or stale, then spawns it
with JARVIS_PROJECT_ROOT and JARVIS_INHERENT_PARENT_LIFETIME set.

Usage:
    python desktop/inherent-swift/launcher.py
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("inherent-launcher")

HERE = Path(__file__).resolve().parent              # desktop/inherent-swift/
PROJECT_ROOT = HERE.parent.parent                   # repo root
XCODEPROJ = HERE / "InherentCard.xcodeproj"
PROJECT_YML = HERE / "Project.yml"
APP_BIN = HERE / "build/Build/Products/Debug/InherentCard.app/Contents/MacOS/InherentCard"


def ensure_xcodeproj() -> None:
    if XCODEPROJ.exists() and XCODEPROJ.stat().st_mtime >= PROJECT_YML.stat().st_mtime:
        return
    LOG.info("regenerating Xcode project via xcodegen")
    subprocess.check_call(["xcodegen", "generate"], cwd=HERE)


def ensure_app_built() -> None:
    if APP_BIN.exists():
        return
    LOG.info("building InherentCard (Debug)")
    subprocess.check_call(
        [
            "xcodebuild",
            "-project", str(XCODEPROJ),
            "-scheme", "InherentCard",
            "-configuration", "Debug",
            "-derivedDataPath", str(HERE / "build"),
            "build",
        ],
        cwd=HERE,
    )


def spawn() -> subprocess.Popen:
    env = {
        **os.environ,
        "JARVIS_PROJECT_ROOT": str(PROJECT_ROOT),
        "JARVIS_INHERENT_PARENT_LIFETIME": "1",
    }
    LOG.info("launching %s", APP_BIN)
    proc = subprocess.Popen([str(APP_BIN)], env=env)
    atexit.register(lambda: _terminate(proc))
    return proc


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ensure_xcodeproj()
    ensure_app_built()
    proc = spawn()
    # Forward Ctrl+C cleanly
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
