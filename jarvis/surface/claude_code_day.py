"""The Mac cuts each day's Claude Code dialogue into material for the day's report (ADR 0299).

``python -m jarvis.surface.claude_code_day material`` writes, for every closed day of the last
``--days`` that has no report ``<out>/<date>.md`` yet, one file per Claude Code session under
``<out>/material/<date>/``: his typed words and Claude's text replies of that day, timed, with the
session's title, project folder and account, and an ``index.md`` over them. Tool calls and results,
thinking, subagent transcripts, command output and injected reminders are never read into it, and
neither is a session of the report task itself (it carries ``MARKER`` in a message he typed).

His day ends when he falls asleep: the start of the longest sleep block on the brain's day line
that begins between 18:00 that day and 14:00 the next, else 05:00 the next day. The brain is asked
for ``GET /inherent/day`` with this device's token (read from the token file, never logged); when
it cannot be reached every day is cut at 05:00 and stderr says so.

Layer rules: L5 surface; stdlib, httpx and yaml; reads only the files its config names.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

import httpx
import yaml

from jarvis.surface.mac_ship import ship_config
from jarvis.surface.mac_ship_days import DIRNAME
from jarvis.surface.mac_ship_sessions import desktop_titles

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

USER: Final = "User"
MARKER: Final = "jarvis-claude-code-daily-run"
"""A session in which he (or the task's prompt) typed this is the report task's own: left out."""
DEFAULT_BRAIN: Final = "http://jarvis:8006"
_FALLBACK_CUT: Final = dtime(5)
_SLEEP_FROM: Final = dtime(18)
_SLEEP_UNTIL: Final = dtime(14)
_HTTP_S: Final = 20.0
_INJECTED: Final = (
    "<command-",
    "<local-command-",
    "<system-reminder>",
    "<task-notification>",
    "<ide_",
    "<user-prompt-submit-hook>",
    "<bash-",
    "<relay",
    "<agent",
    "Caveat:",
    "[Request interrupted",
)
_REMINDER: Final = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_REPLAY_MS: Final = 10 * 60 * 1000
_SKIP_LINE: Final = (b'"type":"tool_result"', b'"isSidechain":true')
_SAID_BY_TOOL: Final = frozenset({"mcp__hearthbot__reply", "mcp__hearthbot__post_message"})
_TITLES: Final = {"custom-title": "customTitle", "ai-title": "aiTitle", "agent-name": "agentName"}


# --- his day -------------------------------------------------------------------------------


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def day_end(day: date, sleeps: Iterable[tuple[int, int | None]], zone: tzinfo, now_ms: int) -> int:
    """When ``day`` ends (ms): the start of the longest sleep begun in [day 18:00, day+1 14:00).

    ``sleeps`` are ``(start_ms, end_ms)`` with ``end_ms`` None while he still sleeps. With none,
    05:00 the next day. The longest is by its length; a tie goes to the earlier.
    """
    low = _ms(datetime.combine(day, _SLEEP_FROM, zone))
    high = _ms(datetime.combine(day + timedelta(days=1), _SLEEP_UNTIL, zone))
    held = [
        (-((end if end is not None else now_ms) - start), start)
        for start, end in sleeps
        if low <= start < high
    ]
    return (
        min(held)[1]
        if held
        else _ms(datetime.combine(day + timedelta(days=1), _FALLBACK_CUT, zone))
    )


def local_zone() -> tzinfo:
    """This machine's IANA zone (from ``/etc/localtime``), else its current offset."""
    _, _, name = os.path.realpath("/etc/localtime").partition("zoneinfo/")
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return datetime.now().astimezone().tzinfo or UTC


def _read_token(path: Path) -> str:
    """The device token in ``path``; refused when empty or readable by others (as the terminal)."""
    if path.stat().st_mode & 0o077:
        msg = f"the brain token {path} is readable by others; run chmod 600 on it"
        raise ValueError(msg)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        msg = f"the brain token {path} is empty"
        raise ValueError(msg)
    return token


def fetch_sleeps(
    brain: str,
    token_file: Path,
    days: list[date],
) -> tuple[dict[int, int | None], str | None]:
    """Sleep blocks (``{start_ms: end_ms}``) on the brain's day line of ``days``, and its zone.

    Raises:
        ValueError: the token file cannot be used.
        httpx.HTTPError: the brain did not answer well; the caller falls back.
    """
    token = _read_token(token_file)
    sleeps: dict[int, int | None] = {}
    zone: str | None = None
    with httpx.Client(
        base_url=brain.rstrip("/"),
        timeout=_HTTP_S,
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        for day in days:
            answer = http.get("/inherent/day", params={"date": day.isoformat()})
            answer.raise_for_status()
            body = answer.json()
            zone = zone or str(body.get("tz") or "") or None
            for item in body.get("items") or []:
                if item.get("kind") == "sleep" and isinstance(item.get("start_ms"), int):
                    sleeps[item["start_ms"]] = item.get("end_ms")
    return sleeps, zone


# --- the dialogue --------------------------------------------------------------------------


@dataclass
class Session:
    """One session file as read: who, where, what it is called and every kept message."""

    account: str
    session_id: str
    cwd: str = ""
    titles: dict[str, str] = field(default_factory=dict)
    messages: list[tuple[int, str, str]] = field(default_factory=list)
    """``(ms, "User" | "Claude", text)`` in file order."""
    own_run: bool = False


def typed_text(content: object) -> str | None:
    """What he typed in a user record's ``content``, or ``None`` when it is not his words."""
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(
            str(b.get("text"))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    if not isinstance(content, str):
        return None
    text = _REMINDER.sub("", content).strip()
    return text if text and not text.startswith(_INJECTED) else None


def _stamp(entry: dict[str, Any]) -> int | None:
    try:
        return _ms(datetime.fromisoformat(str(entry["timestamp"])))
    except (KeyError, ValueError):
        return None


def _take(session: Session, entry: dict[str, Any]) -> None:
    kind = entry.get("type")
    session.cwd = session.cwd or str(entry.get("cwd") or "")
    if kind in _TITLES and entry.get(_TITLES[str(kind)]):
        session.titles[str(kind)] = str(entry[_TITLES[str(kind)]])
        return
    if entry.get("isSidechain") is True:
        return
    at = _stamp(entry)
    message = entry.get("message")
    if kind == "attachment":  # a line he typed while Claude was working
        found = entry.get("attachment")
        if (
            isinstance(found, dict)
            and found.get("type") == "queued_command"
            and (found.get("origin") == {"kind": "human"})
            and (said := typed_text(found.get("prompt")))
            and at
        ):
            session.messages.append((at, USER, said))
    elif (
        kind == "user"
        and isinstance(message, dict)
        and not entry.get("isMeta")
        and not (entry.get("isCompactSummary"))
    ):
        if (said := typed_text(message.get("content"))) is not None and at:
            session.messages.append((at, USER, said))
    elif (
        kind == "assistant"
        and isinstance(message, dict)
        and at
        and (message.get("model") != "<synthetic>")
    ):
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block in blocks if isinstance(blocks, list) else []:
            if text := _said(block):
                session.messages.append((at, "Claude", text))


def _said(block: object) -> str:
    """The words of one assistant block he could read: text, or a project thread's reply."""
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        return str(block.get("text") or "").strip()
    if block.get("type") == "tool_use" and block.get("name") in _SAID_BY_TOOL:
        # a project thread answers him through a tool: its text is what he read
        said = block.get("input")
        return str(said.get("text") or "").strip() if isinstance(said, dict) else ""
    return ""


def read_session(path: Path, account: str) -> Session:
    """The kept messages and titles of one session file; whole lines only, bad lines skipped."""
    session = Session(account, path.stem)
    with path.open("rb") as log:
        for line in log:
            if any(skip in line for skip in _SKIP_LINE):
                continue
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                _take(session, entry)
    session.own_run = any(MARKER in text for _, who, text in session.messages if who == USER)
    # the same line can arrive twice (queued, then replayed): once within ten minutes
    seen: dict[tuple[str, str], int] = {}
    kept: list[tuple[int, str, str]] = []
    for at, who, text in session.messages:
        if who == USER and at - seen.get((who, text), -(10**15)) < _REPLAY_MS:
            continue
        seen[(who, text)] = at
        kept.append((at, who, text))
    session.messages = kept
    return session


# --- the material --------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One closed day: its date and the instants it runs between (ms, ``[start, end)``)."""

    day: date
    start: int
    end: int


def _title(session: Session, desktop: Mapping[str, tuple[str, str]]) -> str:
    found = desktop.get(session.session_id)
    return (
        (found[0] if found else "")
        or session.titles.get("custom-title")
        or session.titles.get("ai-title")
        or session.titles.get("agent-name")
        or "-"
    )


def _clock(ms: int, zone: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%H:%M")


def write_material(
    sessions: list[Session],
    windows: list[Window],
    out: Path,
    zone: tzinfo,
    desktop: Mapping[str, tuple[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """Write ``<out>/material/<date>/`` for each window that has messages; what each holds."""
    starts = [w.start for w in windows]
    per_day: dict[date, dict[int, list[tuple[int, str, str]]]] = {w.day: {} for w in windows}
    for number, session in enumerate(sessions):
        for message in session.messages:
            at = bisect_right(starts, message[0]) - 1
            if at >= 0 and message[0] < windows[at].end:
                per_day[windows[at].day].setdefault(number, []).append(message)
    made: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        held = per_day[window.day]
        folder = out / "material" / window.day.isoformat()
        shutil.rmtree(folder, ignore_errors=True)  # rerun = rewrite
        if not held:
            continue
        folder.mkdir(parents=True)
        rows: list[dict[str, Any]] = []
        for number, messages in sorted(held.items(), key=lambda one: min(m[0] for m in one[1])):
            session = sessions[number]
            first, last = (
                _clock(min(m[0] for m in messages), zone),
                _clock(max(m[0] for m in messages), zone),
            )
            title = _title(session, desktop)
            body = "\n\n".join(
                f"[{_clock(at, zone)}] {who}: {text}"
                for at, who, text in sorted(messages, key=lambda m: m[0])
            )
            name = f"{session.account}-{session.session_id}.md"
            head = (
                f"# Session {session.session_id}\n\n- account: {session.account}\n"
                f"- title: {title}\n"
                f"- project folder: {session.cwd or '-'}\n- first-last message: {first}-{last}\n\n"
            )
            (folder / name).write_text(head + body + "\n", encoding="utf-8")
            rows.append(
                {
                    "file": name,
                    "title": title,
                    "folder": session.cwd or "-",
                    "account": session.account,
                    "time": f"{first}-{last}",
                    "messages": len(messages),
                    "chars": sum(len(m[2]) for m in messages),
                }
            )
        lines = [
            f"# Claude Code dialogue of {window.day.isoformat()}",
            "",
            f"Window: {datetime.fromtimestamp(window.start / 1000, zone).isoformat()} to "
            f"{datetime.fromtimestamp(window.end / 1000, zone).isoformat()}",
            f"Sessions: {len(rows)}",
            "",
            *(
                f"- {r['file']} | {r['title']} | {r['folder']} | account {r['account']} | "
                f"{r['time']} | "
                f"{r['messages']} messages | {r['chars']} chars"
                for r in rows
            ),
        ]
        (folder / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        made[window.day.isoformat()] = rows
    return made


# --- the command ---------------------------------------------------------------------------


def _overlay(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in top.items():
        below = merged.get(key)
        merged[key] = (
            _overlay(below, value) if isinstance(below, dict) and isinstance(value, dict) else value
        )
    return merged


def _load_config(config: Path | None, runtime_root: Path) -> dict[str, Any]:
    """The shipped config with this machine's ``settings.yaml`` laid over it (as the terminal)."""
    shipped = config or Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"
    found: dict[str, Any] = yaml.safe_load(shipped.read_text(encoding="utf-8")) or {}
    mine = runtime_root / "settings.yaml"
    if mine.is_file():
        found = _overlay(found, yaml.safe_load(mine.read_text(encoding="utf-8")) or {})
    return found


def _windows(
    last_days: int,
    sleeps: dict[int, int | None],
    zone: tzinfo,
    now_ms: int,
    out: Path,
) -> list[Window]:
    today = datetime.fromtimestamp(now_ms / 1000, zone).date()
    held = list(sleeps.items())
    windows = []
    for back in range(last_days - 1, -1, -1):
        day = today - timedelta(days=back)
        start, end = (
            day_end(day - timedelta(days=1), held, zone, now_ms),
            day_end(day, held, zone, now_ms),
        )
        if end <= now_ms and not (out / f"{day.isoformat()}.md").exists():
            windows.append(Window(day, start, end))
    return windows


def main(argv: list[str] | None = None) -> int:
    """``material``: write the material of every closed day without a report; one JSON line out."""
    parser = argparse.ArgumentParser(prog="python -m jarvis.surface.claude_code_day")
    parser.add_argument("command", choices=["material"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--out", type=Path, default=None, help="Default: <runtime root>/claude-code-days."
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to jarvis.yaml.")
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument(
        "--brain", default=DEFAULT_BRAIN, help="The brain, e.g. http://jarvis:8006."
    )
    parser.add_argument("--brain-token-file", type=Path, default=None)
    parser.add_argument("--no-brain", action="store_true", help="Cut every day at 05:00.")
    args = parser.parse_args(argv)
    root = (
        args.runtime_root or Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis")
    ).expanduser()
    out = (args.out or root / DIRNAME).expanduser()
    config = _load_config(args.config, root)
    shipping = ship_config({"mac_ship": {**(config.get("mac_ship") or {}), "enabled": True}}, root)
    roots = shipping.session_roots if shipping else ()
    now_ms = _ms(datetime.now(UTC))
    zone: tzinfo = local_zone()
    sleeps: dict[int, int | None] = {}
    if args.no_brain:
        sys.stderr.write("claude_code_day: --no-brain; every day is cut at 05:00\n")
    else:
        today = datetime.fromtimestamp(now_ms / 1000, zone).date()
        asked = [today - timedelta(days=back) for back in range(args.days + 1, -1, -1)]
        try:
            sleeps, name = fetch_sleeps(
                args.brain, args.brain_token_file or root / "brain-token", asked
            )
            zone = ZoneInfo(name) if name else zone
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            sys.stderr.write(
                f"claude_code_day: the brain gave no sleep ({type(exc).__name__}); "
                "every day is cut at 05:00\n"
            )
            sleeps = {}
    windows = _windows(args.days, sleeps, zone, now_ms, out)
    sessions: list[Session] = []
    if windows:
        desktop = desktop_titles(shipping.desktop_titles if shipping else None, {})
        for account, folder in roots:
            for path in sorted(folder.expanduser().glob("*/*.jsonl")):
                if path.stat().st_mtime * 1000 < windows[0].start:
                    continue
                session = read_session(path, account)
                if not session.own_run:
                    sessions.append(session)
    else:
        desktop = {}
    made = write_material(sessions, windows, out, zone, desktop)
    sys.stdout.write(
        json.dumps(
            {
                "out": str(out),
                "zone": str(zone),
                "dates": {
                    day: {"sessions": len(rows), "messages": sum(r["messages"] for r in rows)}
                    for day, rows in made.items()
                },
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
