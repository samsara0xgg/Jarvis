"""ADR 0194: this Mac's location, read on demand through CoreLocation and never kept."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LOGGER = logging.getLogger(__name__)

_DIR: Final = Path(__file__).resolve().parents[2] / "native" / "where"
_APP_NAME: Final = "JarvisWhere.app"
_IDENTIFIER: Final = "com.allen.jarvis.where"
_BUILD_TIMEOUT_S: Final = 300.0
# The helper gives up after 10 s (4 s of that at most on the place name); this is the backstop.
_READ_TIMEOUT_S: Final = 12.0


class LocationUnavailable(RuntimeError):  # noqa: N818 - reads as the reason it is raised for
    """The Mac's location could not be read; the message is the short reason."""


@dataclass(frozen=True)
class MacLocation:
    """One fix; ``place`` is Apple's short street-and-area label, absent when it had none."""

    lat: float
    lng: float
    accuracy_m: float
    place: str | None


def ensure_where_app(source_dir: Path = _DIR) -> Path:
    """Return the helper .app, building and ad-hoc signing it when missing or older than source.

    A rebuilt app has a new code signature, so macOS may ask for location permission again.
    A bare binary never gets that prompt; only an app bundle does, hence the bundle.
    """
    sources = [source_dir / "main.swift", source_dir / "Info.plist"]
    app = source_dir / ".build" / _APP_NAME
    binary = app / "Contents" / "MacOS" / "jarvis-where"
    if binary.exists() and binary.stat().st_mtime >= max(s.stat().st_mtime for s in sources):
        return app
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    scratch = source_dir / ".build" / f"{_APP_NAME}.{os.getpid()}.tmp"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        (scratch / "Contents" / "MacOS").mkdir(parents=True)
        shutil.copy(source_dir / "Info.plist", scratch / "Contents" / "Info.plist")
        for argv in (
            [
                swiftc,
                "-O",
                "-o",
                str(scratch / "Contents" / "MacOS" / "jarvis-where"),
                str(source_dir / "main.swift"),
            ],
            ["/usr/bin/codesign", "-s", "-", "-f", "--deep", "-i", _IDENTIFIER, str(scratch)],
        ):
            subprocess.run(  # noqa: S603 - fixed argv, repo-owned source
                argv, check=True, capture_output=True, timeout=_BUILD_TIMEOUT_S
            )
        shutil.rmtree(app, ignore_errors=True)
        scratch.replace(app)
    except subprocess.CalledProcessError as exc:
        LOGGER.warning("where build failed: %s", exc.stderr.decode(errors="replace")[-400:])
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    LOGGER.info("built %s", app)
    return app


def parse_helper_output(text: str) -> MacLocation:
    """The helper's one JSON line as a fix, or ``LocationUnavailable`` with the reason."""
    try:
        data = json.loads(text)
        if "error" in data:
            reason = str(data["error"])
            msg = "location access is denied" if reason == "denied" else reason
            raise LocationUnavailable(msg)
        place = str(data["place"]).strip() if data.get("place") else None
        return MacLocation(
            float(data["lat"]), float(data["lng"]), float(data["accuracy_m"]), place or None
        )
    except (ValueError, KeyError, TypeError) as exc:
        msg = "the location helper gave no usable answer"
        raise LocationUnavailable(msg) from exc


def read_mac_location() -> MacLocation:
    """One fresh fix from CoreLocation, read now; macOS only."""
    if sys.platform != "darwin":
        msg = "this machine cannot read its location"
        raise LocationUnavailable(msg)
    try:
        app = ensure_where_app()
        with tempfile.TemporaryDirectory(prefix="jarvis-where-") as tmp:
            out = Path(tmp) / "fix.json"
            subprocess.run(  # noqa: S603 - fixed argv, repo-owned app, our own temp file
                ["/usr/bin/open", "-W", "-n", str(app), "--args", str(out)],
                check=False,  # the answer is the file; the helper's exit code adds nothing
                capture_output=True,
                timeout=_READ_TIMEOUT_S,
            )
            text = out.read_text() if out.exists() else ""
    except subprocess.TimeoutExpired as exc:
        msg = "reading the location timed out"
        raise LocationUnavailable(msg) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"the location helper failed ({type(exc).__name__})"
        raise LocationUnavailable(msg) from exc
    return parse_helper_output(text)


__all__ = ["LocationUnavailable", "MacLocation", "parse_helper_output", "read_mac_location"]
