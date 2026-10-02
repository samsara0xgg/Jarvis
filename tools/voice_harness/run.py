"""Voice test harness: drive a throwaway Jarvis daemon through its real audio ingress.

    .venv/bin/python tools/voice_harness/run.py [--case NAME ...] [--budget 0.05] [--keep-daemon]

The runner builds an isolated runtime root (``~/.jarvis-voice-harness``: the live settings, a
snapshot of the live databases, her voice sent to BlackHole 16ch), boots ``jarvis serve`` on port
8026 with ``sitecustomize.py`` (same folder) on its PYTHONPATH, speaks synthesized utterances
into the daemon's spool backend, and asserts over the Event Log, the websocket feed and the
TTS notes. Nothing under ``~/.jarvis`` is written and the live daemon is never contacted.

Other switches: ``--list`` (cases), ``--asr real`` (SenseVoice decodes the speech instead of
scripted text), ``--terse`` (shorter answers, cheaper TTS; judges streaming less reliably),
``--offline`` (dummy API keys: voice input only, no spend), ``--replay RUN_DIR`` (re-evaluate a
finished run), ``--repo PATH`` (test another checkout). Every run writes ``report.md``.
"""

# ruff: noqa: T201, S603, S607, PLR0913, C901, PLR0915, D103, ANN401, D102, D107, TRY003, EM101, EM102, PLR2004, ARG002

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import pickle
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import yaml
from websockets.asyncio.client import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.voice_harness import cases as cs  # noqa: E402

HARNESS_DIR = Path(__file__).resolve().parent
LIVE = Path.home() / ".jarvis"
ROOT = Path.home() / ".jarvis-voice-harness"
KEEP = ("runs", "tts-cache")
RATE = 16_000
LEAD_S = 0.15
_LOG_MARKS = ("controls: conversation=", "were no turn", "conversation barge-in")
TTS_PRICE_FALLBACK = 0.00006  # USD per character when data/pricing.json has no row


class StepError(Exception):
    """A scenario step could not reach the state a case needs."""


class BudgetExceededError(Exception):
    """The run spent more than ``--budget``."""


def _set(cfg: dict[str, Any], dotted: str, value: object) -> None:
    node = cfg
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[leaf] = value


# ---------------------------------------------------------------------------------------------
# Runtime root
# ---------------------------------------------------------------------------------------------

_SETTINGS_OVERRIDES: dict[str, object] = {
    "realtime.single_audio_ingress.wake_input_channel": None,  # null = channel 0 only; 0 is refused
    "realtime.single_audio_ingress.echo_cancellation": False,
    "realtime.single_audio_ingress.echo_diagnostics": False,
    "diagnostics": {"record_tts_audio": True, "log_llm_io": True},
    "tools.workers.enabled": False,
    "tools.plugins.hue.enabled": False,
    "tools.plugins.gmail.enabled": False,
    "observer.timesink.enabled": False,
    "observer.usage.enabled": False,
    "observer.claude_sessions.enabled": False,
    "observer.repos": [],
    "daily_report.at": None,
    "daily_report.codex_sessions": False,
}


def prepare_root(root: Path, *, offline: bool) -> None:
    """Rebuild ``root`` from the live runtime (read-only on ``~/.jarvis``)."""
    if root.resolve() == LIVE.resolve() or LIVE.resolve() in root.resolve().parents:
        sys.exit(f"refusing to use {root}: it is the live runtime")
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.name in KEEP:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name in ("memory.db", "mac_events.db"):
        source = sqlite3.connect(f"file:{LIVE / name}?mode=ro", uri=True)
        target = sqlite3.connect(root / name)
        source.backup(target)
        target.close()
        source.close()
    settings = yaml.safe_load((LIVE / "settings.yaml").read_text(encoding="utf-8")) or {}
    for dotted, value in _SETTINGS_OVERRIDES.items():
        _set(settings, dotted, value)
    (root / "settings.yaml").write_text(
        yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    live_json = json.loads((LIVE / "settings.json").read_text(encoding="utf-8"))
    (root / "settings.json").write_text(
        json.dumps(
            {
                "output_device": "BlackHole 16ch",
                "gpt_live": False,
                "tts_voice": live_json.get("tts_voice"),
                "reply_language": live_json.get("reply_language", "follow"),
            },
        ),
        encoding="utf-8",
    )
    env = root / "env"
    # offline: dummy keys keep the TTS subsystem (so the ingress) on; every call is refused
    dummy = "OPENAI_API_KEY=offline\nMINIMAX_API_KEY=offline\n"
    env.write_text(
        dummy if offline else (LIVE / "env").read_text(encoding="utf-8"), encoding="utf-8"
    )
    env.chmod(0o600)
    (root / "models").symlink_to(LIVE / "models")
    (root / "spool").mkdir()


def tts_price(repo: Path) -> float:
    """USD per MiniMax character, from the repo's pricing table when it has the row."""
    try:
        table = json.loads((repo / "data" / "pricing.json").read_text(encoding="utf-8"))
        return float(table["tts"]["speech-2.6-turbo"]["input_per_1m_chars"]) / 1e6
    except (OSError, KeyError, ValueError):
        return TTS_PRICE_FALLBACK


def config_constants(repo: Path) -> dict[str, float]:
    """The numbers the checks compare against, read from the repo under test."""
    source = (repo / "jarvis/runtime/inherent_loop.py").read_text(encoding="utf-8")
    match = re.search(r"^_COMMENTARY_MAX_LINES\b[^=]*=\s*(\d+)", source, re.MULTILINE)
    base = yaml.safe_load((repo / "config/jarvis.yaml").read_text(encoding="utf-8"))
    ingress = base["realtime"]["single_audio_ingress"]
    return {
        "commentary_max_lines": float(match.group(1)) if match else 2.0,
        "conversation_idle_exit_s": float(ingress.get("conversation_idle_exit_s", 10.0)),
    }


# ---------------------------------------------------------------------------------------------
# Daemon, event log, websocket
# ---------------------------------------------------------------------------------------------


class Daemon:
    """``jarvis serve`` as a subprocess in its own process group."""

    def __init__(self, repo: Path, root: Path, port: int, run_dir: Path) -> None:
        self.repo, self.root, self.port, self.run_dir = repo, root, port, run_dir
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_path = run_dir / "daemon.log"

    def start(self) -> None:
        python = self.repo / ".venv/bin/python"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PYTHONPATH": f"{HARNESS_DIR}:{self.repo}",
            "PYTHONUNBUFFERED": "1",
            "JARVIS_LOG_LEVEL": "INFO",
            "JARVIS_REALTIME_TRACE_JSONL": str(self.run_dir / "trace.jsonl"),
            "JARVIS_VOICE_HARNESS_ROOT": str(self.root),
            "JARVIS_VOICE_HARNESS_PARENT": str(os.getpid()),
        }
        log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [
                str(python if python.exists() else sys.executable),
                "-m",
                "jarvis",
                "serve",
                "--runtime-root",
                str(self.root),
                "--port",
                str(self.port),
                "--force-manual",
            ],
            cwd=self.repo,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout: float = 180.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive():
                raise StepError(f"daemon exited early, see {self.log_path}")
            with contextlib.suppress(httpx.HTTPError):
                if (
                    httpx.get(f"http://127.0.0.1:{self.port}/api/health", timeout=1).status_code
                    == 200
                ):
                    break
            time.sleep(0.5)
        else:
            raise StepError(f"daemon not healthy after {timeout}s, see {self.log_path}")
        while time.time() < deadline:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            if '"input_owner": "single_ingress"' in text:
                return
            if '"input_owner":' in text:
                raise StepError(f"single audio ingress is off, see {self.log_path}")
            time.sleep(0.3)
        raise StepError(f"voice ingress never became ready, see {self.log_path}")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=20.0)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)  # stragglers (MCP servers)


class EventLog:
    """Read-only view of the test root's ``mac_events.db``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def since(self, ts: float, types: tuple[str, ...] | None = None) -> list[cs.Ev]:
        query = (
            "SELECT id, type, ts_epoch_ms, payload_json, correlation_json FROM events "
            "WHERE ts_epoch_ms >= ?"
        )
        args: list[Any] = [int(ts * 1000)]
        if types:
            query += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        else:
            query += " AND type NOT IN ('surface.playback_checkpoint','surface.playback_alignment')"
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(query + " ORDER BY id", args).fetchall()
        finally:
            conn.close()
        out = []
        for id_, type_, ms, payload, corr in rows:
            body = json.loads(payload)
            turn = (json.loads(corr) if corr else {}).get("turn_id") or body.get("turn_id")
            out.append(cs.Ev(id_, type_, ms / 1000.0, body, turn))
        return out


class WsLog:
    """A desktop-like ``/inherent/ws`` client that records every envelope with its wall time."""

    def __init__(self, port: int, key: str, path: Path) -> None:
        self.url, self.key, self.path = f"ws://127.0.0.1:{port}/inherent/ws", key, path
        self.messages: list[dict[str, Any]] = []
        self.connected = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread.start()
        if not self.connected.wait(10):
            raise StepError("websocket client could not connect")

    async def _main(self) -> None:
        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        async with connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.key}"}
        ) as ws:
            self.connected.set()

            async def reader() -> None:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        msg = {"op": "raw", "payload": str(raw)}
                    entry = {"t": time.time(), "op": msg.get("op"), "payload": msg.get("payload")}
                    self.messages.append(entry)
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

            task = asyncio.create_task(reader())
            await self._stop.wait()
            task.cancel()

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(3)


# ---------------------------------------------------------------------------------------------
# Speech synthesis
# ---------------------------------------------------------------------------------------------


def synth(text: str, cache: Path) -> np.ndarray:
    """``say`` -> 16 kHz mono PCM16, trimmed and normalized; cached by voice and text."""
    voice = cs.voice_for(text)
    path = cache / (hashlib.sha1(f"{voice}\0{text}".encode()).hexdigest() + ".wav")  # noqa: S324
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            aiff = Path(tmp) / "x.aiff"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), "--", text], check=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(aiff),
                    "-ar",
                    str(RATE),
                    "-ac",
                    "1",
                    "-sample_fmt",
                    "s16",
                    str(path),
                ],
                check=True,
            )
    with wave.open(str(path), "rb") as reader:
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(float)
    peak = float(np.max(np.abs(samples))) or 1.0
    voiced = np.flatnonzero(np.abs(samples) > 0.03 * peak)
    samples = samples[max(0, voiced[0] - 800) : voiced[-1] + 800]
    return (samples * (0.6 * 32767 / peak)).astype("<i2")


def build_wav(
    pieces: list[str | float], cache: Path
) -> tuple[np.ndarray, list[tuple[int, int, str]]]:
    """Concatenate spoken pieces and silence gaps; return the samples and each piece's span."""
    parts = [np.zeros(int(LEAD_S * RATE), dtype="<i2")]
    spans: list[tuple[int, int, str]] = []
    cursor = len(parts[0])
    for piece in pieces:
        chunk = (
            np.zeros(int(piece * RATE), dtype="<i2")
            if isinstance(piece, float | int)
            else synth(piece, cache)
        )
        if isinstance(piece, str):
            spans.append((cursor, cursor + len(chunk), piece))
        parts.append(chunk)
        cursor += len(chunk)
    return np.concatenate(parts), spans


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------


@dataclass
class State:
    """Per-scenario mutable state of the step executor."""

    t_start: float
    since: float
    says: list[cs.SayRec] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    mark_events: dict[str, cs.Ev] = field(default_factory=dict)
    aborted: str | None = None


class Harness:
    """Owns one daemon and plays scenarios against it."""

    def __init__(self, args: argparse.Namespace, run_dir: Path, daemon: Daemon) -> None:
        self.args, self.run_dir, self.daemon = args, run_dir, daemon
        self.log = EventLog(daemon.root / "mac_events.db")
        self.price = tts_price(daemon.repo)
        self.t_run = time.time()
        self.over_budget = threading.Event()
        self.counter = 0
        self.http: httpx.Client | None = None
        self.ws: WsLog | None = None
        self.constants = config_constants(daemon.repo) | {"terse": float(args.terse)}

    # -- plumbing ------------------------------------------------------------------------
    def connect(self) -> None:
        key = json.loads((self.daemon.root / "plugin-access.json").read_text())["token"]
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.daemon.port}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        self.ws = WsLog(self.daemon.port, key, self.run_dir / "ws.jsonl")
        self.ws.start()
        threading.Thread(target=self._budget_monitor, daemon=True).start()

    def spend(self) -> tuple[float, dict[str, float]]:
        """Dollars spent since the run began: LLM cost rows plus MiniMax characters."""
        parts: dict[str, float] = {}
        chars = 0
        for ev in self.log.since(self.t_run, ("cost.recorded", "tts.usage_observed")):
            if ev.type == "cost.recorded":
                kind = f"llm:{ev.p.get('kind')}"
                parts[kind] = parts.get(kind, 0.0) + float(ev.p.get("cost_usd") or 0.0)
            else:
                chars += int(ev.p.get("characters") or 0)
        parts["tts"] = chars * self.price
        parts["tts_chars"] = float(chars)
        total = sum(v for k, v in parts.items() if k != "tts_chars")
        return total, parts

    def _budget_monitor(self) -> None:
        while self.daemon.alive() and not self.over_budget.is_set():
            with contextlib.suppress(sqlite3.Error):
                if self.spend()[0] > self.args.budget:
                    self.over_budget.set()
                    self.daemon.stop()
            time.sleep(1.0)

    def _tick(self, st: State, what: str, deadline: float) -> None:
        if self.over_budget.is_set():
            raise BudgetExceededError(what)
        if not self.daemon.alive():
            raise StepError(f"the daemon died while {what}")
        if time.time() > deadline:
            raise StepError(f"timed out: {what}")
        time.sleep(0.05)

    def _spool_wait(self, st: State, path: Path, what: str, timeout: float) -> None:
        deadline = time.time() + timeout
        while not path.exists():
            self._tick(st, what, deadline)

    # -- steps ---------------------------------------------------------------------------
    def say(
        self,
        st: State,
        text: str | Any,
        asr: str,
        *,
        gap: float = 0.0,
        wait: bool = True,
        long: bool = False,
        real_ok: bool = True,
    ) -> None:
        if gap:
            time.sleep(gap)
        pieces: list[str | float] = list(text) if not isinstance(text, str) else [text]
        if self.args.terse and long:
            idx = max(i for i, p in enumerate(pieces) if isinstance(p, str))
            pieces[idx] = f"{pieces[idx]}{cs.TERSE_SUFFIX}"
        mode = "real" if self.args.asr == "real" and real_ok else asr
        samples, spans = build_wav(pieces, self.daemon.root / "tts-cache")
        self.counter += 1
        spoken = " ".join(p for p in pieces if isinstance(p, str))
        label = f"{self.counter:04d}-" + re.sub(r"\W+", "-", spoken)[:24].strip("-").lower()
        spool = self.daemon.root / "spool"
        if mode == "scripted":
            script = [{"start": a, "end": b, "text": t} for a, b, t in spans]
            (spool / f"{label}.script.json").write_text(json.dumps({"pieces": script}))
        part = spool / f"{label}.part"
        with wave.open(str(part), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(RATE)
            writer.writeframes(samples.tobytes())
        part.replace(spool / f"{label}.wav")
        duration = len(samples) / RATE
        # The backend plays one wav at a time in name order: allow for one still playing.
        self._spool_wait(st, spool / f"{label}.play.json", f"{label} to start", duration + 30)
        meta = json.loads((spool / f"{label}.play.json").read_text())
        t0 = float(meta["t0_wall"])
        rec = cs.SayRec(
            label,
            spoken,
            mode,
            t0,
            t0 + duration,
            [cs.PieceRec(t, t0 + a / RATE, t0 + b / RATE) for a, b, t in spans],
        )
        st.says.append(rec)
        st.since = t0
        if wait:
            self._spool_wait(st, spool / f"{label}.done", f"{label} to finish", duration + 30)

    def wait_for(self, st: State, event: str, where: Any, timeout: float) -> cs.Ev:
        deadline = time.time() + timeout
        what = f"waiting for {event}"
        while True:
            for ev in self.log.since(st.since, (event,)):
                if where is None or where(ev.p):
                    return ev
            self._tick(st, what, deadline)

    def idle(self, st: State, step: cs.WaitIdle) -> None:
        deadline = time.time() + step.timeout
        while True:
            events = self.log.since(st.t_start)
            started = {e.turn_id for e in events if e.type == "turn.started"}
            closed = {
                e.turn_id
                for e in events
                if e.type in {"turn.ended", "response.cancelled", "response.failed"}
            }
            begun = {
                (e.p.get("response_id"), e.p.get("playback_generation_id"))
                for e in events
                if e.type == "surface.playback_started"
            }
            ended = {
                (e.p.get("response_id"), e.p.get("playback_generation_id"))
                for e in events
                if e.type in cs._PLAYBACK_ENDS  # noqa: SLF001
            }
            last = max((e.ts for e in events), default=st.t_start)
            say_end = max((s.end for s in st.says), default=0.0)
            quiet = time.time() - max(last, say_end) >= step.quiet_s
            if quiet and started <= closed and begun <= ended:
                return
            self._tick(st, "waiting for the turn to go idle", deadline)

    def press_stop(self, st: State, kind: str) -> None:
        assert self.http is not None  # noqa: S101
        events = self.log.since(st.t_start)
        if kind == "answer":
            ended = {
                (e.p.get("response_id"), e.p.get("playback_generation_id"))
                for e in events
                if e.type in cs._PLAYBACK_ENDS  # noqa: SLF001
            }
            live = [
                e
                for e in events
                if e.type == "surface.playback_started"
                and e.p.get("phase") == "final"
                and (e.p.get("response_id"), e.p.get("playback_generation_id")) not in ended
            ]
            if not live:
                raise StepError("no answer was playing when the stop button was pressed")
            body: dict[str, Any] = {
                "response_id": live[-1].p["response_id"],
                "scope": "foreground_output",
            }
        else:
            done = {e.turn_id for e in events if e.type == "turn.ended"}
            open_turns = [e for e in events if e.type == "turn.started" and e.turn_id not in done]
            if not open_turns:
                raise StepError("no turn was thinking when the stop button was pressed")
            body = {"turn_id": open_turns[-1].turn_id, "reason": "user_stop"}
        st.marks["stop_press"] = time.time()
        reply = self.http.post("/inherent/cancel-response", json=body)
        st.marks["stop_ack"] = time.time()
        st.mark_events["stop_outcome"] = cs.Ev(
            0, "http.cancel-response", time.time(), {"request": body, **reply.json()}, None
        )

    def step(self, st: State, step: cs.Step) -> None:
        assert self.http is not None  # noqa: S101
        match step:
            case cs.Controls():
                reply = self.http.post(
                    "/inherent/controls", json={"conversation": step.conversation}
                )
                st.mark_events["controls"] = cs.Ev(
                    0, "http.controls", time.time(), reply.json(), None
                )
            case cs.Say():
                self.say(
                    st,
                    step.text,
                    step.asr,
                    gap=step.gap_before,
                    wait=step.wait,
                    long=step.long,
                    real_ok=step.real_ok,
                )
            case cs.WaitFor():
                ev = self.wait_for(st, step.event, step.where, step.timeout)
                if step.name:
                    st.mark_events[step.name] = ev
            case cs.SayWhen():
                ev = self.wait_for(st, step.event, step.where, step.timeout)
                time.sleep(max(0.0, ev.ts + step.delay_s - time.time()))
                self.say(st, step.text, step.asr, real_ok=step.real_ok)
            case cs.PressStop():
                self.press_stop(st, step.kind)
            case cs.Sleep():
                deadline = time.time() + step.s
                while time.time() < deadline:
                    self._tick(st, "sleeping", deadline + 1)
            case cs.WaitIdle():
                self.idle(st, step)

    def run_scenario(self, scenario: cs.Scenario) -> cs.Ctx:
        now = time.time()
        st = State(now, now)
        print(f"== scenario {scenario.name}", flush=True)
        for step in scenario.steps:
            try:
                self.step(st, step)
            except (StepError, BudgetExceededError) as exc:
                st.aborted = f"{type(step).__name__}: {exc}"
                print(f"   aborted: {st.aborted}", flush=True)
                break
        time.sleep(0.5)
        return self.collect(scenario.name, st)

    def collect(self, name: str, st: State) -> cs.Ctx:
        end = time.time()
        events = self.log.since(st.t_start)
        harness = self.daemon.root / "harness"

        def jsonl(file: str) -> list[dict[str, Any]]:
            path = harness / file
            rows = (
                [json.loads(line) for line in path.read_text().splitlines()]
                if path.exists()
                else []
            )
            return [r for r in rows if r["t_wall"] >= st.t_start]

        rids = {e.p["response_id"] for e in events if "response_id" in e.p}
        notes = []
        for path in (self.daemon.root / "memory" / "audio").glob("tts-*.json"):
            note = json.loads(path.read_text(encoding="utf-8"))
            if note.get("response_id") in rids:
                notes.append(note)
        lines = []
        for raw in self.daemon.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})", raw)
            if stamp and any(mark in raw for mark in _LOG_MARKS):
                moment = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S").timestamp()  # noqa: DTZ007
                if moment + int(stamp[2]) / 1000 >= st.t_start - 1:
                    lines.append(raw)
        assert self.ws is not None  # noqa: S101
        return cs.Ctx(
            name,
            st.t_start,
            end,
            events,
            st.says,
            st.marks,
            st.mark_events,
            [m for m in self.ws.messages if st.t_start <= m["t"] <= end],
            jsonl("asr.jsonl"),
            jsonl("tts-requests.jsonl"),
            notes,
            lines,
            self.constants,
            st.aborted,
        )


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------


def _clip(text: str, n: int = 260) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def write_report(
    path: Path,
    args: argparse.Namespace,
    ctxs: dict[str, cs.Ctx],
    results: list[tuple[cs.Case, cs.Result]],
    spend: tuple[float, dict[str, float]],
    env: dict[str, str],
    skipped: list[str],
    note: str,
) -> None:
    out = [
        f"# Voice harness run {path.parent.name}",
        "",
        f"- repo: `{args.repo}` @ `{env['commit']}`",
        f"- command: `{' '.join(sys.argv)}`",
        f"- asr: `{args.asr}` terse: `{args.terse}` offline: `{args.offline}`",
        f"- spend: **${spend[0]:.4f}** of ${args.budget:.2f} budget ({_parts(spend[1])})",
        f"- system volume before/after: `{env['volume_before']}` / `{env['volume_after']}` "
        f"({'unchanged' if env['volume_before'] == env['volume_after'] else 'CHANGED'})",
        f"- default devices before/after: `{env['devices_before']}` / `{env['devices_after']}` "
        f"({'unchanged' if env['devices_before'] == env['devices_after'] else 'CHANGED'})",
        f"- ducker osascript calls intercepted: {env['ducker_calls']}; "
        f"artifacts: `{path.parent}` (ws.jsonl, daemon.log, trace.jsonl)",
    ]
    if note:
        out.append(f"- **{note}**")
    out += ["", "## Results", "", "| case | status | summary | key numbers |", "|---|---|---|---|"]
    for case, res in results:
        nums = "; ".join(f"{k}={v}" for k, v in res.metrics.items())
        out.append(f"| {case.name} | **{res.status}** | {res.summary} | {_clip(nums, 200)} |")
    for name in skipped:
        out.append(f"| {name} | NOT RUN | budget exceeded or run aborted | |")
    out += ["", "## Case details"]
    for case, res in results:
        out += ["", f"### {case.name}: {res.status}", f"_{case.doc}_", "", res.summary, ""]
        out += [f"- {k}: `{v}`" for k, v in res.metrics.items()]
        if res.evidence:
            out += ["", "Evidence:"] + [f"- {e}" for e in res.evidence]
    out += ["", "## Turns per scenario"]
    for ctx in ctxs.values():
        out += ["", f"### {ctx.name}" + (f" (ABORTED: {ctx.aborted})" if ctx.aborted else "")]
        for say in ctx.says:
            out.append(f"- said [{say.asr}] {ctx.rel(say.t0)}..{ctx.rel(say.end)}: {say.text!r}")
        for turn in ctx.turns():
            eos = ctx.speech_end_before(turn.utterance) if turn.utterance else None
            first = turn.first_final_playback()
            out.append(
                f"- turn `{turn.turn_id}` heard {turn.transcript!r} [{turn.endpoint_reason}] "
                f"eos->utterance {_val(turn.utterance, eos)}s, eos->first final audio "
                f"{_val(first, eos)}s, tools {[a.p.get('tool_name') for a in turn.actions]}",
            )
            for resp in turn.responses:
                ends = "; ".join(
                    f"{p.end_type} {ctx.rel(p.ended)} {p.reason or ''}" for p in resp.playbacks
                )
                out.append(
                    f"    - {resp.phase} `{resp.response_id[:12]}` route={resp.route} "
                    f"{resp.end_type} ({resp.end_reason or ''}) playback[{ends}]: "
                    f"{_clip(resp.text or '(no text)')}",
                )
        for row in ctx.asr:
            out.append(
                f"- asr [{row['mode']}] {row['audio_s']:.1f}s decode {row['decode_s']:.2f}s: "
                f"{row['text']!r}"
            )
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _parts(parts: dict[str, float]) -> str:
    return ", ".join(
        f"tts_chars={int(v)}" if k == "tts_chars" else f"{k}={v:.4f}" for k, v in parts.items()
    )


def _val(end: float | None, start: float | None) -> str:
    return "-" if end is None or start is None else f"{end - start:.2f}"


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------


def _sh(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.strip()


def _devices() -> str:
    code = (
        "import sounddevice as sd;"
        "print(sd.query_devices(kind='input')['name'],'|',sd.query_devices(kind='output')['name'])"
    )
    return _sh(sys.executable, "-c", code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--case", action="append", default=[], help="case name (repeatable); default all"
    )
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument(
        "--budget", type=float, default=0.05, help="abort when spend exceeds this (USD)"
    )
    parser.add_argument("--keep-daemon", action="store_true", help="leave the test daemon running")
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="checkout to test (default: this one)"
    )
    parser.add_argument(
        "--asr",
        choices=("auto", "real"),
        default="auto",
        help="real: SenseVoice decodes every utterance except synthesized hums (no scripted text)",
    )
    parser.add_argument(
        "--terse",
        action="store_true",
        help="ask long-answer questions for under forty words (cheaper TTS)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="dummy API keys: every LLM and TTS call is refused, so zero spend (voice input only)",
    )
    parser.add_argument("--port", type=int, default=8026)
    parser.add_argument(
        "--replay",
        type=Path,
        help="re-evaluate the cases over a finished run directory (no daemon, no spend)",
    )
    return parser.parse_args()


def evaluate(
    selected: list[cs.Case], ctxs: dict[str, cs.Ctx]
) -> tuple[list[tuple[cs.Case, cs.Result]], list[str]]:
    """Run each selected case over the scenarios that were played."""
    results: list[tuple[cs.Case, cs.Result]] = []
    skipped = []
    for case in selected:
        mine = (
            list(ctxs.values())
            if case.all_runs
            else [ctxs[case.scenario]]
            if case.scenario in ctxs
            else []
        )
        if not mine:
            skipped.append(case.name)
            continue
        result = case.check(mine)
        stuck = None if case.all_runs else next((c for c in mine if c.aborted), None)
        if stuck is not None:
            result = cs.Result(
                "ERROR",
                f"scenario {stuck.name} aborted ({stuck.aborted}); the check read what was there: "
                f"{result.status} {result.summary}",
                result.metrics,
                result.evidence,
            )
        results.append((case, result))
    return results, skipped


def summarize(
    report: Path,
    results: list[tuple[cs.Case, cs.Result]],
    skipped: list[str],
    spend: tuple[float, dict[str, float]],
) -> int:
    print(f"\n{'case':38} status")
    for case, result in results:
        print(f"{case.name:38} {result.status:12} {result.summary}")
    for name in skipped:
        print(f"{name:38} NOT RUN")
    print(f"\nspend ${spend[0]:.4f}  report {report}")
    return 0 if all(r.status == "PASS" for _, r in results) and not skipped else 1


def main() -> int:
    args = parse_args()
    args.repo = args.repo.resolve()
    if args.list:
        for case in cs.CASES:
            print(f"{case.name:38} [{case.scenario}] {case.doc}")
        return 0
    unknown = set(args.case) - {c.name for c in cs.CASES}
    if unknown:
        print(f"unknown case(s): {sorted(unknown)}; see --list", file=sys.stderr)
        return 2
    selected = [c for c in cs.CASES if not args.case or c.name in args.case]
    if args.replay:
        saved = pickle.loads((args.replay / "run.pkl").read_bytes())  # noqa: S301 - our own file
        results, skipped = evaluate(selected, saved["ctxs"])
        report = args.replay / "report-replay.md"
        write_report(
            report,
            args,
            saved["ctxs"],
            results,
            saved["spend"],
            saved["env"],
            skipped,
            saved["note"],
        )
        return summarize(report, results, skipped, saved["spend"])
    needed = {c.scenario for c in selected}
    scenarios = [s for s in cs.SCENARIOS if s.name in needed]

    if _port_open(args.port):
        print(f"port {args.port} is already in use", file=sys.stderr)
        return 2
    prepare_root(ROOT, offline=args.offline)
    run_dir = ROOT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
    run_dir.mkdir(parents=True)
    env = {
        "commit": _sh("git", "-C", str(args.repo), "rev-parse", "--short", "HEAD"),
        "volume_before": _sh("osascript", "-e", "get volume settings"),
        "devices_before": _devices(),
    }
    daemon = Daemon(args.repo, ROOT, args.port, run_dir)
    harness = Harness(args, run_dir, daemon)

    def cleanup() -> None:
        if harness.ws is not None:
            harness.ws.stop()
        if not args.keep_daemon:
            daemon.stop()

    atexit.register(cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(130))

    ctxs: dict[str, cs.Ctx] = {}
    note = ""
    try:
        daemon.start()
        daemon.wait_ready()
        harness.connect()
        for scenario in scenarios:
            ctxs[scenario.name] = harness.run_scenario(scenario)
            if harness.over_budget.is_set():
                note = f"BUDGET EXCEEDED in {scenario.name}; remaining scenarios were not run"
                break
    except StepError as exc:
        note = f"RUN ABORTED: {exc}"
    finally:
        spend = harness.spend() if daemon.root.joinpath("mac_events.db").exists() else (0.0, {})
        cleanup()
    env["volume_after"] = _sh("osascript", "-e", "get volume settings")
    env["devices_after"] = _devices()
    ducker = daemon.root / "harness" / "ducker.jsonl"
    env["ducker_calls"] = str(len(ducker.read_text().splitlines()) if ducker.exists() else 0)

    (run_dir / "run.pkl").write_bytes(
        pickle.dumps({"ctxs": ctxs, "spend": spend, "env": env, "note": note})
    )
    results, skipped = evaluate(selected, ctxs)
    report = run_dir / "report.md"
    write_report(report, args, ctxs, results, spend, env, skipped, note)
    return summarize(report, results, skipped, spend)


def _port_open(port: int) -> bool:
    import socket  # noqa: PLC0415

    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    sys.exit(main())
