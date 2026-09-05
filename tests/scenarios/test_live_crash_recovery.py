"""Crash-recovery live burn: SIGKILL a real ``jarvis serve`` mid-speech, reboot, read the log.

ADR-0006 D10 and ADR-0008 §4.4 / F14 describe what a restarted daemon must
do with a response the previous process abandoned mid-playback. Every
hermetic test of that contract rebuilds objects in-process; none sends a
signal to a real process. This burn does: it boots the daemon as a
subprocess on its own runtime root and port, asks a question long enough
that speech starts before the stream ends, ``SIGKILL``s the process while a
``surface.playback_started`` row exists and no response terminal does, then
boots again on the same root and asserts against the same on-disk Event Log
and the second boot's log and trace.

Isolation: the runtime root lives under ``~/.jarvis-lane-b-test``, the port
is OS-assigned, and the daemon on 8006 (``~/.jarvis-realtime-test``) is never
touched. ``realtime.single_audio_ingress`` stays off and
``JARVIS_VOICE_DISABLE_WAKE=1`` keeps the legacy wake listener from opening
the microphone: the scenario submits text. Household audio is routed to the
``BlackHole 16ch`` virtual device for the whole run and the previous default
output is restored afterwards.

Run from the lane worktree::

    set -a; source ~/.jarvis/env; set +a
    PYTHONPATH=. .venv/bin/python -m pytest -q -s --live-llm -m live_llm \
        tests/scenarios/test_live_crash_recovery.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import yaml

from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import iter_events, open_event_log
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jarvis.state.conversation import PresentationRecord

pytestmark = pytest.mark.live_llm

_TEST_ROOT_BASE = Path.home() / ".jarvis-lane-b-test"
_OWNER_ENV = Path.home() / ".jarvis" / "env"
_FORBIDDEN_PORT = 8006
_SILENT_DEVICE = "BlackHole 16ch"
_SWITCH_AUDIO = "SwitchAudioSource"
_INSTALL_HINT = "brew install switchaudio-osx blackhole-16ch"

# Every question takes the routine stream route (ADR-0008 Step 8): a plain
# knowledge request whose first sentence is a "<X>是..." explanation the
# ``routine-zh-en-v1`` classifier permits, so speech starts from the first
# permitted segment while the model is still generating. Neither the route
# nor the first sentence is under the test's control: the decision layer
# sometimes answers the same request on the full-text route
# (``emission_mode=full_text``, speech only at ``surface.response_emitted``),
# and a preamble ("好的 我用十句话...") or a plain statement without an
# explanatory marker ("温哥华位于...") is buffered, so the stream seals
# before any permit. The long questions are tried in order, twice over,
# until one opens the window; the order follows the live permit rate
# measured on 2026-09-05 (docs/live-burn-2026-09-05-crash-recovery.md).
_WARMUP_QUESTION = "用两句话介绍一下温哥华"
_LONG_QUESTIONS = (
    "什么是海岸山脉 请详细介绍 至少十句话",
    "用十句话介绍一下温哥华的气候和地理",
    "什么是温带海洋性气候 请详细解释 至少十句话",
)
_LONG_ROUNDS = 2
_RESPONSE_TERMINALS = ("response.completed", "response.failed", "response.cancelled")
_PLAYBACK_TERMINALS = (
    "surface.playback_completed",
    "surface.playback_failed",
    "surface.playback_interrupted",
)

_PORT_WAIT_S = 60.0
_TTS_READY_WAIT_S = 60.0
_WARMUP_WAIT_S = 120.0
_SPEECH_WAIT_S = 90.0
_CHECKPOINT_GRACE_S = 1.5
"""After playback starts, how long to wait for a first checkpoint before killing."""
_POLL_S = 0.02
_STOP_WAIT_S = 20.0
_SEALED_QUIET_S = 2.0
"""How long a sealed turn must append nothing, with nothing open, before the next question."""

_RECONCILE_LINE = "boot reconciliation closed 1 open response run(s)"
_WATCHER_LINE = "tts_watcher started (after_id="


# --- small helpers ---------------------------------------------------------


def _echo(message: str) -> None:
    """Write one evidence line to stdout (``-s`` shows it in the transcript)."""
    print(message, flush=True)  # noqa: T201 - the transcript is the acceptance evidence


def _pick_free_port() -> int:
    """Pick an OS-assigned ephemeral port and immediately release it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, *, deadline_s: float) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                time.sleep(0.1)
                continue
            return
    msg = f"daemon never bound port {port} within {deadline_s}s"
    raise TimeoutError(msg)


def _wait_for_log_line(log_path: Path, needle: str, *, deadline_s: float) -> str:
    """Return the first log line containing ``needle``, failing on the deadline."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if needle in line:
                return line
        time.sleep(0.1)
    tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
    pytest.fail(f"{needle!r} never appeared in {log_path} within {deadline_s}s; tail:\n{tail}")


def _rows(db: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    """Run one read-only query against the on-disk Event Log."""
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


class _Row(NamedTuple):
    id: int
    type: str
    payload: dict[str, Any]


def _events_for_response(db: Path, response_id: str) -> list[_Row]:
    rows = _rows(
        db,
        "SELECT id, type, payload_json FROM events "
        "WHERE json_extract(payload_json, '$.response_id') = ? ORDER BY id",
        (response_id,),
    )
    return [_Row(int(str(row[0])), str(row[1]), json.loads(str(row[2]))) for row in rows]


def _max_event_id(db: Path) -> int:
    (value,) = _rows(db, "SELECT COALESCE(MAX(id), 0) FROM events")[0]
    return int(str(value))


def _submit(port: int, text: str) -> str:
    """POST ``/inherent/submit`` and return the minted ``turn_id``."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/inherent/submit",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - loopback URL built here
        body = json.loads(resp.read().decode())
    assert body["status"] == "accepted", body
    return str(body["turn_id"])


def _response_id_for_turn(db: Path, turn_id: str) -> str | None:
    rows = _rows(
        db,
        "SELECT json_extract(payload_json, '$.response_id') FROM events "
        "WHERE type = 'response.started' AND json_extract(payload_json, '$.turn_id') = ? "
        "ORDER BY id LIMIT 1",
        (turn_id,),
    )
    return str(rows[0][0]) if rows else None


# --- daemon lifecycle ------------------------------------------------------


def _build_overlay(root: Path) -> Path:
    """Copy the shipped config tree and flip only the switches this burn needs.

    ``bootstrap_runtime_app`` derives the Tier-0, grammar, cue, prompt and
    pricing paths from the config file's parent and grandparent, so the
    overlay mirrors that shape byte-for-byte except for ``jarvis.yaml``.
    ``routine_streaming`` plus ``speak_from_segments`` make a ``kind="stream"``
    answer start speaking from its first permitted segment, which is what
    opens the kill window (``surface.playback_started`` before the response
    terminal). ``realtime.single_audio_ingress`` and
    ``realtime.input.intent_pump`` stay at their shipped values: the first
    would open the microphone, the second would re-adopt the killed turn's
    input and answer it a second time, and neither is the contract under test.
    """
    source = repo_root()
    overlay = root / "overlay"
    for relative in (
        Path("config") / "tier0_patterns.yaml",
        Path("config") / "confirm_grammar.yaml",
        Path("config") / "file_targets.yaml",
        Path("config") / "tool_cues.yaml",
        Path("prompts") / "jarvis_v1.md",
        Path("data") / "pricing.json",
    ):
        target = overlay / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    shipped = yaml.safe_load((source / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    realtime = shipped["realtime"]
    realtime["enabled"] = True
    for switch in (
        "transactional_event_append",
        "lifecycle_terminal_cas",
        "confirmation_dispatch_outbox",
        "exactly_once_cost_accounting",
    ):
        realtime["concurrency_safety"][switch] = True
    realtime["response"]["response_run_lifecycle"] = True
    realtime["response"]["routine_streaming"]["enabled"] = True
    realtime["streaming_output"]["enabled"] = True
    realtime["streaming_output"]["speak_from_segments"] = True
    assert realtime["single_audio_ingress"]["enabled"] is False
    path = overlay / "config" / "jarvis.yaml"
    path.write_text(yaml.safe_dump(shipped, allow_unicode=True), encoding="utf-8")
    return path


def _install_runtime_env(root: Path) -> None:
    """Give the daemon its secrets the production way: a fill-only ``<root>/env`` file.

    ``tests/conftest.py`` pops ``MINIMAX_API_KEY`` from this process for the
    whole session so hermetic tests never build TTS. The subprocess still
    needs it, and ``load_env_file`` reads it from the runtime root exactly as
    the 8006 daemon does from ``~/.jarvis-realtime-test/env``. Values are
    never printed. The owner file is shell syntax (``KEY="value"``) while
    ``load_env_file`` takes quotes literally (ADR-0009 D1), so a verbatim
    copy hands MiniMax a key wrapped in ``"`` and every playback silently
    falls back to macOS ``say``; the copy therefore drops one pair of
    matching outer quotes, exactly what ``source`` would do.
    """
    if not _OWNER_ENV.is_file():
        pytest.fail(f"{_OWNER_ENV} missing; the daemon needs MINIMAX_API_KEY from it")
    lines: list[str] = []
    for line in _OWNER_ENV.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        lines.append(f"{key.strip()}={value}")
    if not any(line.startswith("MINIMAX_API_KEY=") for line in lines):
        pytest.fail(f"{_OWNER_ENV} has no MINIMAX_API_KEY line; the daemon would run text-only")
    target = root / "env"
    target.touch(mode=0o600)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _boot(root: Path, config_path: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    """Start ``python -m jarvis serve`` from the worktree root, logging to ``log_path``."""
    env = {
        **os.environ,
        "JARVIS_RUNTIME_ROOT": str(root),
        "JARVIS_REALTIME_TRACE_JSONL": str(root / "trace.jsonl"),
        "JARVIS_LOG_LEVEL": "INFO",
        "JARVIS_VOICE_DISABLE_WAKE": "1",
        "PYTHONPATH": str(repo_root()),
    }
    argv = [
        sys.executable,
        "-m",
        "jarvis",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--runtime-root",
        str(root),
        "--config",
        str(config_path),
    ]
    _echo(f"boot: {' '.join(argv)}")
    with log_path.open("ab") as log:
        proc = subprocess.Popen(  # noqa: S603 - argv is fully built above from trusted values
            argv,
            cwd=repo_root(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    _wait_for_port(port, deadline_s=_PORT_WAIT_S)
    return proc


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Graceful stop for the daemon this test owns; never touches any other pid."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def silent_output_device() -> Iterator[str]:
    """Route the default output to ``BlackHole 16ch``; restore what was there before.

    The device to restore is captured from ``SwitchAudioSource -c -t output``
    before switching. A missing tool or device skips the burn rather than
    letting it play through the speakers.
    """
    if shutil.which(_SWITCH_AUDIO) is None:
        pytest.skip(f"{_SWITCH_AUDIO} not installed; run: {_INSTALL_HINT}")
    listed = subprocess.run(  # noqa: S603 - fixed argv
        [_SWITCH_AUDIO, "-a", "-t", "output"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if _SILENT_DEVICE not in listed:
        pytest.skip(f"{_SILENT_DEVICE!r} output device absent; run: {_INSTALL_HINT}")
    before = subprocess.run(  # noqa: S603 - fixed argv
        [_SWITCH_AUDIO, "-c", "-t", "output"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _echo(f"audio: `{_SWITCH_AUDIO} -c -t output` before = {before!r}")
    subprocess.run(  # noqa: S603 - fixed argv
        [_SWITCH_AUDIO, "-s", _SILENT_DEVICE, "-t", "output"],
        check=True,
        capture_output=True,
        text=True,
    )
    _echo(f"audio: switched default output to {_SILENT_DEVICE!r}")
    try:
        yield before
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            [_SWITCH_AUDIO, "-s", before, "-t", "output"],
            check=False,
            capture_output=True,
            text=True,
        )
        after = subprocess.run(  # noqa: S603 - fixed argv
            [_SWITCH_AUDIO, "-c", "-t", "output"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _echo(
            f"audio: `{_SWITCH_AUDIO} -c -t output` after = {after!r} "
            f"(restored={after == before})",
        )


@pytest.fixture
def burn_root() -> Path:
    """A fresh runtime root under ``~/.jarvis-lane-b-test``, kept for the burn document."""
    root = _TEST_ROOT_BASE / f"crash-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    root.mkdir(parents=True, exist_ok=False)
    return root


# --- the burn ---------------------------------------------------------------


def _wait_for_spoken_completion(db: Path, turn_id: str, label: str) -> str:
    """Let one ordinary answer complete and finish speaking before the next turn."""
    deadline = time.monotonic() + _WARMUP_WAIT_S
    while time.monotonic() < deadline:
        response_id = _response_id_for_turn(db, turn_id)
        if response_id is not None:
            types = {event.type for event in _events_for_response(db, response_id)}
            if types & set(_RESPONSE_TERMINALS) and types & set(_PLAYBACK_TERMINALS):
                provider = next(
                    (
                        event.payload.get("provider")
                        for event in _events_for_response(db, response_id)
                        if event.type in _PLAYBACK_TERMINALS
                    ),
                    None,
                )
                _echo(f"{label}: playback provider={provider!r}")
                return response_id
        time.sleep(0.1)
    pytest.fail(f"{label} turn {turn_id} did not complete and finish speaking in {_WARMUP_WAIT_S}s")


class _Sealed(NamedTuple):
    """The run reached a terminal without ever opening a stream: no early speech."""

    response_id: str
    emission_mode: str
    gate_reasons: tuple[str, ...]


def _wait_for_kill_window(db: Path, turn_id: str) -> tuple[str, bool] | _Sealed:
    """Block until playback has started and no response terminal exists.

    Returns ``(response_id, saw_checkpoint)``. Returns ``_Sealed`` when the
    run reached a terminal without a ``kind="stream"`` ``surface.response_open``
    row: the request took the full-text route, or the stream sealed before
    its first permit (completed silently, or failed as ``suffix_rejected``
    ahead of a correction run). Either way nothing was spoken early and the
    caller should try another question. Fails, naming the window, when an
    opened stream reaches a terminal before playback started or during the
    checkpoint grace period.
    """
    deadline = time.monotonic() + _SPEECH_WAIT_S
    grace_until: float | None = None
    response_id: str | None = None
    while time.monotonic() < deadline:
        if response_id is None:
            response_id = _response_id_for_turn(db, turn_id)
            if response_id is None:
                time.sleep(_POLL_S)
                continue
        events = _events_for_response(db, response_id)
        types = [event.type for event in events]
        terminal = next((t for t in types if t in _RESPONSE_TERMINALS), None)
        started = "surface.playback_started" in types
        if terminal is not None:
            stream_open = any(
                e.type == "surface.response_open" and e.payload.get("kind") == "stream"
                for e in events
            )
            if not stream_open:
                mode = next(
                    (
                        str(e.payload.get("emission_mode"))
                        for e in events
                        if e.type == "response.started"
                    ),
                    "?",
                )
                reasons = next(
                    (
                        tuple(str(r) for r in e.payload.get("reasons", ()))
                        for e in events
                        if e.type == "gate.evaluated"
                    ),
                    (),
                )
                return _Sealed(response_id, mode, reasons)
            phase = "before playback started" if not started else "during the checkpoint grace"
            pytest.fail(
                f"kill window missed: {response_id} reached {terminal} {phase}; "
                f"rows so far: {types}",
            )
        if started:
            if "surface.playback_checkpoint" in types:
                return response_id, True
            if grace_until is None:
                grace_until = time.monotonic() + _CHECKPOINT_GRACE_S
            elif time.monotonic() >= grace_until:
                return response_id, False
        time.sleep(_POLL_S)
    pytest.fail(f"turn {turn_id} never reached surface.playback_started within {_SPEECH_WAIT_S}s")


def _wait_for_turn_quiet(db: Path, turn_id: str) -> None:
    """Let a turn that opened no stream settle before the next question goes in.

    Such a turn either completes silently (zero-permit seal: no
    ``surface.response_open``, so the media owner has nothing to schedule),
    fails its run as ``suffix_rejected`` and opens a full-text correction run
    under the same ``turn_id`` that re-asks the LLM and speaks the result
    (``docs/goals/sealed-stream-full-text-fallback.md`` for both), or took
    the full-text route and speaks at ``surface.response_emitted``. A next
    response would queue behind that playback and complete before it speaks,
    so the wait ends only once the turn has appended nothing for the quiet
    window while no response run and no playback of it is open.
    """
    deadline = time.monotonic() + _WARMUP_WAIT_S
    seen = -1
    quiet_from = time.monotonic()
    while time.monotonic() < deadline:
        types = [
            str(row[0])
            for row in _rows(
                db,
                "SELECT type FROM events WHERE json_extract(payload_json, '$.turn_id') = ? "
                "ORDER BY id",
                (turn_id,),
            )
        ]
        runs = types.count("response.started")
        playbacks = types.count("surface.playback_started")
        open_runs = runs - sum(types.count(t) for t in _RESPONSE_TERMINALS)
        open_playbacks = playbacks - sum(types.count(t) for t in _PLAYBACK_TERMINALS)
        if len(types) != seen or open_runs > 0 or open_playbacks > 0:
            seen = len(types)
            quiet_from = time.monotonic()
        elif time.monotonic() - quiet_from >= _SEALED_QUIET_S:
            _echo(
                f"sealed turn {turn_id}: quiet for {_SEALED_QUIET_S}s after {seen} rows "
                f"({runs} response run(s), {playbacks} playback(s))",
            )
            return
        time.sleep(0.1)
    pytest.fail(f"sealed turn {turn_id} never went quiet in {_WARMUP_WAIT_S}s")


def _open_kill_window(db: Path, port: int) -> tuple[str, str, bool]:
    """Submit long questions in order until one opens the window.

    Returns ``(turn_id, response_id, saw_checkpoint)``.
    """
    attempts = _LONG_QUESTIONS * _LONG_ROUNDS
    for attempt, question in enumerate(attempts, start=1):
        turn_id = _submit(port, question)
        window = _wait_for_kill_window(db, turn_id)
        if not isinstance(window, _Sealed):
            _echo(f"kill window: question {attempt}/{len(attempts)} {question!r} opened it")
            return turn_id, window[0], window[1]
        _echo(
            f"retry: question {attempt}/{len(attempts)} {question!r} opened no stream as "
            f"{window.response_id} (emission_mode={window.emission_mode}, "
            f"gate reasons={window.gate_reasons})",
        )
        _wait_for_turn_quiet(db, turn_id)
    pytest.fail(f"every long question sealed before the first permit: {_LONG_QUESTIONS}")


def _record_for(db: Path, turn_id: str, response_id: str) -> PresentationRecord:
    conn = open_event_log(db)
    try:
        history = fold_conversation_history(iter_events(conn), max_turns=20)
    finally:
        conn.close()
    for turn in history.turns:
        if turn.turn_id == turn_id:
            for record in turn.responses:
                if record.response_id == response_id:
                    return record
    pytest.fail(f"fold_conversation_history has no record for {turn_id}/{response_id}")


def _boot2_trace_rows(trace_path: Path, offset: int) -> list[dict[str, Any]]:
    """Return the JSONL trace rows appended after ``offset`` (the second boot's)."""
    with trace_path.open("rb") as handle:
        handle.seek(offset)
        appended = handle.read().decode("utf-8", errors="replace")
    return [json.loads(line) for line in appended.splitlines() if line.strip()]


class _PreKill(NamedTuple):
    max_id: int
    generation: int
    last_checkpoint_text: str | None
    answer_so_far: str


def _pre_kill_facts(db: Path, response_id: str) -> _PreKill:
    """Read the killed response's rows once the process is dead, so nothing moves under us."""
    max_id = _max_event_id(db)
    pre_kill = _events_for_response(db, response_id)
    for event in pre_kill:
        brief = {
            k: v
            for k, v in event.payload.items()
            if k in {"playback_generation_id", "heard_text", "reason", "sequence", "channel"}
        }
        _echo(f"pre-kill row id={event.id} {event.type} {brief}")
    types = [e.type for e in pre_kill]
    assert "surface.playback_started" in types, types
    lost = [t for t in types if t in _RESPONSE_TERMINALS]
    assert not lost, f"kill window lost between poll and SIGKILL: {lost} landed before the kill"
    started = [e for e in pre_kill if e.type == "surface.playback_started"]
    checkpoints = [e for e in pre_kill if e.type == "surface.playback_checkpoint"]
    chunks = sorted(
        (e for e in pre_kill if e.type == "surface.response_chunk"),
        key=lambda e: int(e.payload["sequence"]),
    )
    facts = _PreKill(
        max_id=max_id,
        generation=int(started[-1].payload["playback_generation_id"]),
        last_checkpoint_text=str(checkpoints[-1].payload["heard_text"]) if checkpoints else None,
        answer_so_far="".join(str(e.payload["text"]) for e in chunks),
    )
    _echo(
        f"pre-kill: MAX(events.id) N={facts.max_id} playback_generation_id={facts.generation} "
        f"chunk_chars={len(facts.answer_so_far)} "
        f"last_checkpoint_heard_text={facts.last_checkpoint_text!r}",
    )
    return facts


def _assert_closed_once_and_silent(
    db: Path,
    facts: _PreKill,
    response_id: str,
    warm_response: str,
) -> None:
    """(a) one ``response.failed(daemon_restart)`` for the killed run; (b) nothing re-speaks."""
    failed_after = _rows(
        db,
        "SELECT id, json_extract(payload_json, '$.response_id'), "
        "json_extract(payload_json, '$.reason') FROM events "
        "WHERE type = 'response.failed' AND id > ? ORDER BY id",
        (facts.max_id,),
    )
    _echo(f"post-restart response.failed rows (id > N): {failed_after}")
    assert len(failed_after) == 1, failed_after
    assert failed_after[0][1] == response_id, failed_after
    assert failed_after[0][2] == "daemon_restart", failed_after
    killed_terminals = [
        e for e in _events_for_response(db, response_id) if e.type in _RESPONSE_TERMINALS
    ]
    assert len(killed_terminals) == 1, killed_terminals
    warm_failed = [
        e for e in _events_for_response(db, warm_response) if e.type == "response.failed"
    ]
    assert warm_failed == [], warm_failed

    respoken = _rows(
        db,
        "SELECT id FROM events WHERE type = 'surface.playback_started' AND id > ? "
        "AND json_extract(payload_json, '$.response_id') = ? "
        "AND json_extract(payload_json, '$.playback_generation_id') = ?",
        (facts.max_id, response_id, facts.generation),
    )
    any_respoken = _rows(
        db,
        "SELECT id FROM events WHERE type = 'surface.playback_started' AND id > ? "
        "AND json_extract(payload_json, '$.response_id') = ?",
        (facts.max_id, response_id),
    )
    _echo(
        f"post-restart surface.playback_started for ({response_id}, gen {facts.generation}): "
        f"{len(respoken)}; for the response_id at all: {len(any_respoken)}",
    )
    assert respoken == []
    assert any_respoken == []


def _assert_heard_prefix(db: Path, turn_id: str, response_id: str, facts: _PreKill) -> None:
    """(c) the folded heard prefix never exceeds the last durable pre-kill checkpoint."""
    record = _record_for(db, turn_id, response_id)
    heard = record.spoken_heard
    _echo(
        f"fold: spoken_heard={heard.text if heard else None!r} "
        f"vs last pre-kill checkpoint heard_text={facts.last_checkpoint_text!r} "
        f"(record.consistent={record.consistent})",
    )
    if facts.last_checkpoint_text is None:
        assert heard is None, heard
        return
    assert heard is not None, record
    assert facts.answer_so_far.startswith(heard.text), (heard.text, facts.answer_so_far[:200])
    assert len(heard.text) <= len(facts.last_checkpoint_text), (
        heard.text,
        facts.last_checkpoint_text,
    )


def _assert_boot2_anchored(trace_path: Path, trace_offset: int, boot2_log: Path, n: int) -> None:
    """(d) boot 2 anchors at the pre-kill high-water N; (e) reconcile runs before the watcher."""
    owner_rows = [
        r
        for r in _boot2_trace_rows(trace_path, trace_offset)
        if r.get("name") == "media_owner_started"
    ]
    assert len(owner_rows) == 1, owner_rows
    attributes: dict[str, Any] = owner_rows[0]["attributes"]
    _echo(f"boot2 trace media_owner_started attributes={attributes}")
    assert attributes["boot_high_water_id"] == n, (attributes, n)
    boot2_text = boot2_log.read_text(encoding="utf-8", errors="replace")
    watcher_line = _wait_for_log_line(boot2_log, _WATCHER_LINE, deadline_s=1.0)
    _echo(f"boot2 log: {watcher_line.strip()}")
    assert f"{_WATCHER_LINE}{n})" in watcher_line, (watcher_line, n)
    reconcile_at = boot2_text.find(_RECONCILE_LINE)
    watcher_at = boot2_text.find(_WATCHER_LINE)
    _echo(
        f"boot2 order: {_RECONCILE_LINE!r} at offset {reconcile_at}, "
        f"{_WATCHER_LINE!r} at offset {watcher_at}",
    )
    assert 0 <= reconcile_at < watcher_at, (reconcile_at, watcher_at)


def test_live_sigkill_mid_speech_recovers_without_respeaking(
    silent_output_device: str,
    burn_root: Path,
) -> None:
    """SIGKILL the daemon mid-speech; the reboot closes the run once and re-speaks nothing."""
    root = burn_root
    db = root / "mac_events.db"
    trace_path = root / "trace.jsonl"
    _install_runtime_env(root)
    config_path = _build_overlay(root)
    port = _pick_free_port()
    assert port != _FORBIDDEN_PORT
    _echo(f"root={root} port={port} audio_restore_target={silent_output_device!r}")

    boot1_log = root / "boot1.log"
    proc = _boot(root, config_path, port, boot1_log)
    proc2: subprocess.Popen[bytes] | None = None
    try:
        first_watcher = _wait_for_log_line(boot1_log, _WATCHER_LINE, deadline_s=_TTS_READY_WAIT_S)
        _echo(f"boot1: {first_watcher.strip()}")
        boot1_text = boot1_log.read_text(encoding="utf-8", errors="replace")
        assert "skipping TTS subsystem" not in boot1_text, boot1_text[-2000:]
        assert "downgraded" not in boot1_text, boot1_text[-2000:]

        warm_turn = _submit(port, _WARMUP_QUESTION)
        warm_response = _wait_for_spoken_completion(db, warm_turn, "warm-up")
        _echo(f"warm-up: turn_id={warm_turn} response_id={warm_response} (completed and spoken)")

        turn_id, response_id, saw_checkpoint = _open_kill_window(db, port)
        os.kill(proc.pid, signal.SIGKILL)
        kill_instant = datetime.now(UTC).isoformat(timespec="milliseconds")
        proc.wait(timeout=10)
        _echo(
            f"kill: SIGKILL pid={proc.pid} at {kill_instant} "
            f"(turn_id={turn_id} response_id={response_id} checkpoint_seen={saw_checkpoint})",
        )
        facts = _pre_kill_facts(db, response_id)

        trace_offset = trace_path.stat().st_size if trace_path.exists() else 0
        boot2_log = root / "boot2.log"
        proc2 = _boot(root, config_path, port, boot2_log)
        _wait_for_log_line(boot2_log, _WATCHER_LINE, deadline_s=_TTS_READY_WAIT_S)
        boot2_text = boot2_log.read_text(encoding="utf-8", errors="replace")
        _echo("boot2 log header:\n  " + "\n  ".join(boot2_text.splitlines()[:12]))

        _assert_closed_once_and_silent(db, facts, response_id, warm_response)
        _assert_heard_prefix(db, turn_id, response_id, facts)
        _assert_boot2_anchored(trace_path, trace_offset, boot2_log, facts.max_id)
    finally:
        if proc2 is not None:
            _stop(proc2)
        if proc.poll() is None:
            _stop(proc)
