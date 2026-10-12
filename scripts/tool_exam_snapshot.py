r"""Build the tool exam's sandbox from a live runtime root. Run it ON the brain's host.

    python3 scripts/tool_exam_snapshot.py [--live ~/.jarvis] [--out ~/tool-exam] \
        [--gmail ~/Projects/external/gemini-workspace]

``OUT/root-pristine`` is a runtime root ``tool_exam_steps run-steps`` copies and restores per case;
``OUT/gmail`` is a copy of the Gmail MCP server with its own token, so the exam never touches the
live one. Nothing under ``--live`` (or ``--gmail``) is written: the two SQLite files are copied with
the online backup API from a read-only connection; every other file is copied byte for byte and
never read here (``env`` and the Gmail token included).

Never copied: daemon.lock, apns/, push-tokens.json, plugin-access.json, mcp/, inherent-v2.token,
jev/, logs/, lifelog/, memory/. ``settings.yaml`` is the live one with the push block, the
listeners, the notion/github/hue plugins and the microsoft/linear MCP servers removed and the
``gmail`` server pointed at the copy (a direct entry replaces the plugin's spec).
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
_DBS = ("mac_events.db", "memory.db")
_FILES = (
    "devices.json", "terminal-tools.json", "voice-settings.json", "quiet-mode.json",
    "settings.json", "env",
)
_DIRS = ("cache", "artifacts/attachments")
_GMAIL_FILES = (
    "gemini-extension.json", "workspace-server/dist/index.js",
    "gemini-cli-workspace-token.json", ".gemini-cli-workspace-master-key",
)
_PLUGINS_OFF = ("notion", "github", "hue")
# microsoft: its refresh tokens may rotate; linear: no login here
_MCP_OFF = ("microsoft", "linear")


def _backup(live: Path, copy_to: Path) -> int:
    """Copy one SQLite file (online backup, read-only source); its events or records count."""
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    dst = sqlite3.connect(copy_to)
    try:
        src.backup(dst)
        dst.execute("pragma journal_mode=delete")  # one self-contained file
        if dst.execute("pragma quick_check").fetchone()[0] != "ok":
            msg = f"quick_check failed on the copy of {live.name}"
            raise SystemExit(msg)
        table = "events" if live.name == "mac_events.db" else "records"
        return int(dst.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
    finally:
        dst.close()
        src.close()


def sandbox_settings(live: dict[str, Any], gmail_index: Path) -> dict[str, Any]:
    """The live settings with everything that reaches outside the sandbox removed."""
    out = copy.deepcopy(live)
    out.pop("push", None)
    runtime = out.setdefault("runtime", {})
    for key in ("listen_addresses", "listen_hosts"):
        runtime.pop(key, None)
    tools = out.setdefault("tools", {})
    plugins = tools.setdefault("plugins", {})
    for name in _PLUGINS_OFF:
        plugins[name] = {**(plugins.get(name) or {}), "enabled": False}
    servers = tools.setdefault("mcp", {}).setdefault("servers", {})
    for name in _MCP_OFF:
        servers.pop(name, None)
    spec = json.loads((REPO / "plugins/gmail/.mcp.json").read_text(encoding="utf-8"))
    one = spec["mcpServers"]["gmail"]
    servers["gmail"] = {"command": one["command"], "args": [str(gmail_index)], "env": one["env"]}
    out.setdefault("diagnostics", {})["log_llm_io"] = True
    return out


def _inside(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def snapshot(live: Path, out: Path, gmail_src: Path) -> list[str]:
    """Rebuild ``out/root-pristine`` and ``out/gmail``; one line per thing made."""
    live, out, gmail_src = live.resolve(), out.resolve(), gmail_src.resolve()
    if _inside(out, live) or _inside(live, out) or _inside(out, gmail_src):
        msg = "--out must be a folder of its own, outside --live and --gmail"
        raise SystemExit(msg)
    root, gmail = out / "root-pristine", out / "gmail"
    for old in (root, gmail):
        shutil.rmtree(old, ignore_errors=True)
    root.mkdir(parents=True, mode=0o700)
    made = [f"{name}: backup, {_backup(live / name, root / name)} rows" for name in _DBS]
    for name in _DIRS:
        if (live / name).is_dir():
            shutil.copytree(live / name, root / name)
            made.append(f"{name}/: copied")
    for name in _FILES:
        if (live / name).is_file():
            shutil.copy2(live / name, root / name)
            made.append(f"{name}: copied")
    for name in _GMAIL_FILES:
        target = gmail / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gmail_src / name, target)
    made.append(f"gmail/: {len(_GMAIL_FILES)} files")
    live_settings = yaml.safe_load((live / "settings.yaml").read_text(encoding="utf-8")) or {}
    edited = sandbox_settings(live_settings, gmail / "workspace-server/dist/index.js")
    (root / "settings.yaml").write_text(
        yaml.safe_dump(edited, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    made.append("settings.yaml: edited copy")
    return made


def main() -> int:
    """Parse arguments and build the sandbox."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", type=Path, default=Path.home() / ".jarvis")
    parser.add_argument("--out", type=Path, default=Path.home() / "tool-exam")
    parser.add_argument(
        "--gmail", type=Path, default=Path.home() / "Projects/external/gemini-workspace",
    )
    args = parser.parse_args()
    sys.stdout.write("\n".join(snapshot(args.live, args.out, args.gmail)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
