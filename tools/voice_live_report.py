"""Replay a live voice test from the durable record, read-only.

Run on the Mac right after the test:

    .venv/bin/python tools/voice_live_report.py --minutes 20

For each turn in the window: what Allen said and how his words ended, the
line that turn's prompt carried about the last spoken answer (the decision
layer's own ``_previous_answer_line`` over the same fold), and each answer
with how its run and its playback ended and how much of it was heard. Then
the daemon log's lines on conversation mode, speech over Jarvis, sounds that
were no turn, and GPT-Live in the same window. The Event Log is opened
read-only; nothing is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from jarvis.decision import _previous_answer_line
from jarvis.decision.stream_envelope import split_envelope
from jarvis.shared import Event
from jarvis.state.conversation import fold_conversation_history

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.decision.packet import SituationPacket
    from jarvis.state.conversation import ConversationHistory, ConversationTurn, PresentationRecord

_CONTEXT_MS = 60 * 60 * 1000  # earlier turns the first one in the window looks back to
_RUN_ENDS = ("response.completed", "response.cancelled", "response.failed")
_PLAYBACK_ENDS = (
    "surface.playback_completed",
    "surface.playback_interrupted",
    "surface.playback_failed",
)
_LOG_MARKS = (
    "controls: conversation=",
    "conversation barge-in:",
    "words over Jarvis were no turn",
    "realtime wake: empty utterance",
    "realtime wake: wake phrase only",
    "gpt_live",
)
_LOG_NOISE = ("gpt_live sent ", "gpt_live ack ", "gpt_live user fragment", "gpt_live input backlog")
_LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} \S+ \S+ (.*)$")
_LOG_TAIL_BYTES = 16 * 1024 * 1024  # launchd never rotates the daemon log
_CLIP = 80


def _clip(text: str, limit: int = _CLIP) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _clock(ts_epoch_ms: int) -> str:
    return datetime.fromtimestamp(ts_epoch_ms / 1000, tz=UTC).astimezone().strftime("%H:%M:%S")


def _events_since(db: Path, since_ms: int) -> list[Event]:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT event_uid, type, schema_version, ts_epoch_ms, payload_json, "
            "source_event_id, correlation_json FROM events WHERE ts_epoch_ms >= ? ORDER BY id",
            (since_ms,),
        ).fetchall()
    finally:
        conn.close()
    return [
        Event(
            event_uid=uid,
            type=type_,
            schema_version=version,
            ts_epoch_ms=ts,
            payload=json.loads(payload),
            source_event_id=source,
            correlation=None if correlation is None else json.loads(correlation),
        )
        for uid, type_, version, ts, payload, source, correlation in rows
    ]


def _ends(events: Sequence[Event], types: tuple[str, ...], key: str) -> dict[str, list[str]]:
    """Each run or playback end by turn or response id, as ``kind (reason)``."""
    ends: dict[str, list[str]] = {}
    for event in events:
        owner = event.payload.get(key)
        if event.type not in types or not isinstance(owner, str):
            continue
        kind = event.type.rsplit(".", 1)[1].removeprefix("playback_")
        reason = event.payload.get("reason")
        ends.setdefault(owner, []).append(f"{kind} ({reason})" if reason else kind)
    return ends


def _heard(record: PresentationRecord) -> str:
    prefix = record.spoken_heard
    if prefix is None:
        return "no playback cursor"
    if prefix.complete:
        return "heard whole"
    if not prefix.text.strip():
        if prefix.submitted_samples:
            return "audio submitted; no confirmed word boundary"
        return "heard nothing"
    return f'heard up to "…{prefix.text.strip()[-30:]}"'


def _turn_lines(
    turn: ConversationTurn,
    history: ConversationHistory,
    inputs: dict[str, Event],
    runs: dict[str, list[str]],
    playbacks: dict[str, list[str]],
) -> list[str]:
    event = inputs[turn.input_event_uid]
    endpoint = event.payload.get("endpoint_reason")
    said = f"{_clock(event.ts_epoch_ms)}  {turn.input_channel}"
    said += f" endpoint={endpoint}" if endpoint else ""
    lines = [f'{said}  Allen: "{_clip(turn.user_text)}"']
    packet = SimpleNamespace(conversation_history=history, current_turn_id=turn.turn_id)
    told = _previous_answer_line(cast("SituationPacket", packet))
    lines.append("    prompt: " + (told.replace("\n", " | ") if told else "(no cut reported)"))
    finals = [r for r in turn.responses if r.phase == "final" and r.panel_available.strip()]
    for record in finals:
        voice = split_envelope(record.panel_available)[0]
        played = ", ".join(playbacks.get(record.response_id, ["no playback"]))
        lines.append(f'    Jarvis [{record.channel}]: "{_clip(voice)}"')
        lines.append(f"      playback: {played}; {_heard(record)}")
    run = ", ".join(runs.get(turn.turn_id, []))
    if run or not finals:
        shown = "" if finals else "; no answer shown"
        lines.append(f"    run: {run or 'no end recorded'}{shown}")
    return lines


def _log_lines(log: Path, since: float) -> list[str]:
    if not log.is_file():
        return [f"(no daemon log at {log})"]
    with log.open("rb") as handle:
        handle.seek(max(0, log.stat().st_size - _LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", "replace")
    kept: list[str] = []
    for line in text.splitlines():
        match = _LOG_TIME.match(line)
        if match is None or not any(mark in line for mark in _LOG_MARKS):
            continue
        if any(noise in line for noise in _LOG_NOISE):
            continue
        at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()  # local, as logged
        if at.timestamp() >= since:
            kept.append(f"{match.group(1)[11:]}  {_clip(match.group(2), 150)}")
    return kept


def _live_lines(events: Sequence[Event], since_ms: int) -> list[str]:
    return [
        f"{_clock(e.ts_epoch_ms)}  {e.type} "
        + _clip(json.dumps(dict(e.payload), ensure_ascii=False), 120)
        for e in events
        if e.type.startswith("live.") and e.ts_epoch_ms >= since_ms
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the report for the last ``--minutes`` of the runtime root's record."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--minutes", type=float, default=30.0, help="how far back to report (default 30)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("JARVIS_RUNTIME_ROOT", "~/.jarvis")),
        help="runtime root holding mac_events.db and logs/ (default ~/.jarvis)",
    )
    args = parser.parse_args(argv)
    root = cast("Path", args.root).expanduser()
    since_s = time.time() - float(args.minutes) * 60
    since_ms = int(since_s * 1000)
    events = _events_since(root / "mac_events.db", since_ms - _CONTEXT_MS)
    history = fold_conversation_history(events, max_turns=1000)
    inputs = {e.event_uid: e for e in events}
    runs = _ends(events, _RUN_ENDS, "turn_id")
    playbacks = _ends(events, _PLAYBACK_ENDS, "response_id")
    out = [f"voice live report: last {args.minutes:g} min of {root}", "", "== turns"]
    for turn in history.turns:
        if inputs[turn.input_event_uid].ts_epoch_ms >= since_ms:
            out += _turn_lines(turn, history, inputs, runs, playbacks)
    out += ["", "== daemon log: conversation mode, speech over Jarvis, no-turn sounds, GPT-Live"]
    out += _log_lines(root / "logs" / "daemon.err.log", since_s) or ["(none)"]
    out += ["", "== GPT-Live events", *(_live_lines(events, since_ms) or ["(none)"])]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
