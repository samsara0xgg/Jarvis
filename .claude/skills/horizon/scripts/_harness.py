"""Shared helpers for the horizon harness scripts. Stdlib only."""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import subprocess

SUBDIRS = ("context", "sessions", "manifest", "inbox/done", "lease")


def harness_dir(cwd: str | None = None) -> pathlib.Path:
    common = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=cwd,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    root = pathlib.Path(common) / "claude-harness"
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path: pathlib.Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: pathlib.Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: pathlib.Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def meta(root: pathlib.Path, sid: str) -> dict:
    return read_json(root / "sessions" / f"{sid}.meta.json")


def context_snapshot(root: pathlib.Path, sid: str) -> dict:
    return read_json(root / "context" / f"{sid}.json")


def role_label(m: dict) -> str:
    if not m:
        return "unknown"
    base = m["role"] if m.get("role") != "lane" else f"lane-{m.get('lane')}"
    return f"{base}-g{m.get('generation', '?')}"
