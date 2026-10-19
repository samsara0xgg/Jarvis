"""Claude Code sessions of both accounts, summarised for the brain's day record (ADR 0294).

One record per session file, revised as the file grows. A record holds what the log says about
the session as a whole (where, on which branch and repo, with which models, how long, what it
cost, what it was called, what Claude Code's own recap of it says) and nothing of what was said:
no prompt, no assistant text, no tool input or output is ever copied. A file is read once from
its start and afterwards from the byte offset it was read to, whole lines only; the running
record is kept in the shipper's state, so a restart resumes.

A growing session's record is shipped at most once per ``REVISION_MS`` (30 minutes) and always
once the file has been quiet that long.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

from jarvis.surface.mac_ship_core import (
    SET_SESSION,
    Out,
    Report,
    Sink,
    State,
    canonical,
    decide,
    digest,
)

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

SOURCE: Final = "claude-code"
REVISION_MS: Final = 30 * 60 * 1000
_GIT_S: Final = 5
_LIST_CAP: Final = 500
_TITLE_KEYS: Final = {
    "custom-title": "customTitle", "ai-title": "aiTitle", "agent-name": "agentName",
}
_TITLE_FIELDS: Final = {
    "custom-title": "custom_title", "ai-title": "ai_title", "agent-name": "agent_name",
}


def _fresh(account: str, session_id: str, path: Path) -> dict[str, Any]:
    return {
        "account": account, "session_id": session_id, "path": str(path),
        "cwds": [], "git_branches": [], "entrypoints": [], "session_kinds": [],
        "is_sidechain": False, "models": [], "first_timestamp": None, "last_timestamp": None,
        "user_prompts": 0, "assistant_messages": 0, "tool_uses": 0,
        "turn_count": 0, "turn_duration_ms": 0, "cost_state": None,
        "custom_title": [], "ai_title": [], "agent_name": [], "pr_links": [], "away_summaries": [],
        "_last_assistant": None,
    }


def _add(items: list[Any], value: object) -> None:
    if value and value not in items and len(items) < _LIST_CAP:
        items.append(value)


def _moment(text: object) -> float:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return 0.0


def _is_prompt(entry: dict[str, Any]) -> bool:
    """A user turn he typed or sent: not a tool result, not a harness-made meta line."""
    if entry.get("isMeta"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in ("text", "image") for block in content
    )


def _take(record: dict[str, Any], entry: dict[str, Any]) -> None:  # noqa: C901 - one switch over the log's line types
    """Fold one log line into ``record``; only fields, counts and titles, never a body."""
    kind = entry.get("type")
    stamp = entry.get("timestamp")
    if isinstance(stamp, str) and _moment(stamp):
        if record["first_timestamp"] is None or _moment(stamp) < _moment(record["first_timestamp"]):
            record["first_timestamp"] = stamp
        if record["last_timestamp"] is None or _moment(stamp) > _moment(record["last_timestamp"]):
            record["last_timestamp"] = stamp
    _add(record["cwds"], entry.get("cwd") or entry.get("relocatedCwd"))
    _add(record["git_branches"], entry.get("gitBranch"))
    _add(record["entrypoints"], entry.get("entrypoint"))
    _add(record["session_kinds"], entry.get("sessionKind"))
    record["is_sidechain"] = record["is_sidechain"] or entry.get("isSidechain") is True
    message = entry.get("message")
    if kind == "user" and _is_prompt(entry):
        record["user_prompts"] += 1
    elif kind == "assistant" and isinstance(message, dict):
        if message.get("id") != record["_last_assistant"]:
            record["assistant_messages"] += 1
            record["_last_assistant"] = message.get("id")
        _add(record["models"], message.get("model"))
        content = message.get("content")
        if isinstance(content, list):
            record["tool_uses"] += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            )
    elif kind == "system" and entry.get("subtype") == "turn_duration":
        record["turn_count"] += 1
        duration = entry.get("durationMs")
        record["turn_duration_ms"] += duration if isinstance(duration, (int, float)) else 0
    elif kind == "system" and entry.get("subtype") == "away_summary":
        record["away_summaries"].append({
            "timestamp": stamp, "uuid": entry.get("uuid"), "content": entry.get("content"),
        })
    elif kind == "cost-state":
        record["cost_state"] = {k: v for k, v in entry.items() if k not in ("type", "sessionId")}
    elif kind in _TITLE_KEYS:
        _add(record[_TITLE_FIELDS[str(kind)]], entry.get(_TITLE_KEYS[str(kind)]))
    elif kind == "pr-link" and entry.get("prUrl") not in {
        p.get("prUrl") for p in record["pr_links"]
    }:
        record["pr_links"].append({
            k: entry.get(k) for k in ("prNumber", "prRepository", "prUrl", "timestamp")
        })


def read_new(path: Path, record: dict[str, Any], offset: int) -> int:
    """Fold the whole lines of ``path`` after ``offset`` into ``record``; the offset reached."""
    with path.open("rb") as log:
        log.seek(offset)
        for line in log:
            if not line.endswith(b"\n"):
                break  # a line still being written
            offset += len(line)
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                _take(record, entry)
    return offset


def _public(origin: str | None) -> str | None:
    """A remote URL without any credentials in it."""
    if not origin or "@" not in origin or "://" not in origin:
        return origin
    parts = urlsplit(origin)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class Repos:
    """The git repo of each working directory, resolved once and kept."""

    def __init__(self, state: State) -> None:
        """Share the shipper's state file; misses are remembered only for this run."""
        self._state = state
        self._missed: set[str] = set()

    def of(self, cwd: str) -> tuple[str | None, str | None]:
        """``(top-level path, origin URL)`` of the repo ``cwd`` is in; ``None``s if none."""
        found = self._state.conn.execute(
            "SELECT toplevel, origin FROM repos WHERE cwd = ?", (cwd,),
        ).fetchone()
        if found is not None:
            return found[0], found[1]
        if cwd in self._missed:
            return None, None
        top = self._git(cwd, "rev-parse", "--show-toplevel")
        if top is None:
            self._missed.add(cwd)
            return None, None
        origin = _public(self._git(cwd, "remote", "get-url", "origin"))
        self._state.apply([("INSERT OR REPLACE INTO repos (cwd, toplevel, origin) VALUES (?, ?, ?)",
                            (cwd, top, origin))])
        return top, origin

    @staticmethod
    def _git(cwd: str, *args: str) -> str | None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, a directory Claude Code itself ran in
                ["/usr/bin/git", "-C", cwd, *args],
                capture_output=True, text=True, timeout=_GIT_S, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() or None


def desktop_titles(
    directory: Path | None, seen: dict[Path, tuple[int, str, str, str]],
) -> dict[str, tuple[str, str]]:
    """Claude Desktop's title and its source for each CLI session id; a file is re-read on change.

    ``seen`` is the cache between passes: path to ``(mtime_ns, session id, title, source)``.
    """
    found: dict[Path, tuple[int, str, str, str]] = {}
    files = directory.rglob("local_*.json") if directory and directory.is_dir() else ()
    for path in files:
        try:
            mtime = path.stat().st_mtime_ns
            cached = seen.get(path)
            if cached is None or cached[0] != mtime:
                data = json.loads(path.read_text(encoding="utf-8"))
                data = data if isinstance(data, dict) else {}
                cached = (
                    mtime, str(data.get("cliSessionId") or ""), str(data.get("title") or ""),
                    str(data.get("titleSource") or ""),
                )
            found[path] = cached
        except (OSError, ValueError):
            continue
    seen.clear()
    seen.update(found)
    return {sid: (title, source) for _, sid, title, source in found.values() if sid and title}


def scan(  # noqa: PLR0913 - one pass: roots, titles, state, batch, clock
    roots: tuple[tuple[str, Path], ...], desktop: dict[str, tuple[str, str]], state: State,
    sink: Sink, report: Report, now_ms: int,
) -> None:
    """One pass over every session file of every account.

    Raises:
        ShipFailed: the brain did not keep a batch; the cursor stays where it was.
    """
    out = Out(SOURCE, sink, state, report)
    repos = Repos(state)
    held = {
        row[0]: row[1:] for row in state.conn.execute(
            "SELECT path, size, mtime_ns, offset, record, desk, shipped_sha, shipped_ms, seen_sha "
            "FROM sessions",
        )
    }
    for account, root in roots:
        for path in sorted(root.expanduser().glob("*/*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            session_id = path.stem
            title = desktop.get(session_id)
            desk = digest(title)
            prev = held.get(str(path))
            if (
                prev is not None and prev[0] == stat.st_size and prev[1] == stat.st_mtime_ns
                and prev[4] == desk and prev[5] == prev[7]
            ):
                continue  # nothing new and nothing waiting
            record = json.loads(prev[3]) if prev else _fresh(account, session_id, path)
            offset = prev[2] if prev else 0
            if offset > stat.st_size:  # the file was replaced by a shorter one
                record, offset = _fresh(account, session_id, path), 0
            try:
                offset = read_new(path, record, offset)
            except OSError:
                continue
            public = {k: v for k, v in record.items() if not k.startswith("_")}
            top, origin = repos.of(record["cwds"][0]) if record["cwds"] else (None, None)
            public["cwd"] = record["cwds"][0] if record["cwds"] else None
            public.update(
                git_toplevel=top, git_origin=origin,
                desktop_title=title[0] if title else None,
                desktop_title_source=title[1] if title else None,
            )
            sha = digest(public)
            shipped = prev[5:] if prev else None
            quiet = now_ms - stat.st_mtime_ns // 1_000_000 >= REVISION_MS
            ship, after = decide(
                None if shipped is None else (shipped[0], shipped[1], shipped[2]), sha, now_ms,
                settled=quiet, interval_ms=REVISION_MS, unchanged_settles=False,
            )
            op = (SET_SESSION, (
                str(path), stat.st_size, stat.st_mtime_ns, offset, canonical(record), desk, *after,
            ))
            if not ship:
                out.note([op])
                continue
            titles = [*public["custom_title"][-1:], *public["ai_title"][-1:],
                      *public["agent_name"][-1:], *([title[0]] if title else [])]
            first = _moment(public["first_timestamp"])
            out.add({
                "key": f"session:{account}:{session_id}:{sha}", "kind": "session", "zone": "UTC",
                "at_ms": int(first * 1000) if first else int(stat.st_mtime_ns // 1_000_000),
                "text": titles[0] if titles else "", "fields": public,
            }, [op])
    out.flush()
