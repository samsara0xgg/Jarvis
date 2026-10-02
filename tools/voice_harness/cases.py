"""Step DSL, the run data model and the seed cases of the voice harness.

A *scenario* is a list of steps the runner plays through the real audio ingress of a test daemon;
a *case* is a named assertion over the events one scenario left behind. Several cases may read
one scenario (one spoken question is paid for once). A FAIL is a finding about Jarvis; ERROR
means the scenario itself did not get where the case needed it (a step timed out).
"""

# ruff: noqa: PLR2004, FBT003, C901, PLR0912, PLR0915

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from jarvis.shared.lang import text_language

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# ---------------------------------------------------------------------------------------------
# Step DSL
# ---------------------------------------------------------------------------------------------

Asr = Literal["scripted", "real"]
Where = "Callable[[dict[str, Any]], bool] | None"


@dataclass(frozen=True)
class Say:
    """Speak ``text`` into the spool. Floats in a list are silence gaps in seconds.

    ``asr="scripted"``: final ASR returns the text (each piece, matched by audio fingerprint);
    VAD and endpointing still run on the audio. ``"real"``: SenseVoice decodes it.
    ``long`` marks a question that asks for a long answer (``--terse`` shortens those).
    """

    text: str | Sequence[str | float]
    asr: Asr = "scripted"
    gap_before: float = 0.0
    wait: bool = True
    long: bool = False
    real_ok: bool = True  # False: ``--asr real`` leaves it scripted (a synthesized hum is no hum)


@dataclass(frozen=True)
class WaitFor:
    """Block until an event of ``event`` type (matching ``where``) appears after the last Say."""

    event: str
    where: Callable[[dict[str, Any]], bool] | None = None
    timeout: float = 60.0
    name: str | None = None


@dataclass(frozen=True)
class SayWhen:
    """Wait for an event, wait ``delay_s`` more, then Say."""

    event: str
    delay_s: float
    text: str | Sequence[str | float]
    where: Callable[[dict[str, Any]], bool] | None = None
    asr: Asr = "scripted"
    timeout: float = 60.0
    real_ok: bool = True


@dataclass(frozen=True)
class PressStop:
    """The stop button: ``answer`` stops audible speech, ``turn`` cancels a thinking turn."""

    kind: Literal["answer", "turn"]


@dataclass(frozen=True)
class Sleep:
    """Wait ``s`` seconds."""

    s: float


@dataclass(frozen=True)
class Controls:
    """``POST /inherent/controls``."""

    conversation: bool


@dataclass(frozen=True)
class WaitIdle:
    """Wait until every turn has ended, nothing is playing and the log has been quiet."""

    timeout: float = 120.0
    quiet_s: float = 2.5


Step = Say | WaitFor | SayWhen | PressStop | Sleep | Controls | WaitIdle


@dataclass(frozen=True)
class Scenario:
    """A named step list; the runner plays it once however many cases read it."""

    name: str
    steps: tuple[Step, ...]


# ---------------------------------------------------------------------------------------------
# Data model the runner fills and the checks read (all times are wall-clock epoch seconds)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ev:
    """One row of ``mac_events.db``."""

    id: int
    type: str
    ts: float
    p: dict[str, Any]
    turn_id: str | None


@dataclass
class PieceRec:
    """One spoken piece of a Say: when its first and last sample were fed to the ingress."""

    text: str
    start: float
    end: float


@dataclass
class SayRec:
    """One wav played through the spool."""

    label: str
    text: str
    asr: str
    t0: float
    end: float
    pieces: list[PieceRec]


@dataclass
class Playback:
    """One ``surface.playback_started`` and how it ended."""

    started: float
    generation: Any
    ended: float | None = None
    end_type: str | None = None
    reason: str | None = None
    heard_text: str | None = None


@dataclass
class Resp:
    """One ResponseRun: its surface rows, chunks, terminal and playbacks."""

    response_id: str
    phase: str = "final"
    route: str | None = None
    emission_mode: str | None = None
    started: float | None = None
    open_ts: float | None = None
    emitted: float | None = None
    emitted_text: str = ""
    end_ts: float | None = None
    end_type: str | None = None
    end_reason: str | None = None
    chunks: list[tuple[int, float, str]] = field(default_factory=list)
    playbacks: list[Playback] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The whole spoken text (the last words may reach the surface only at the emit row)."""
        return self.emitted_text or " ".join(t for _, _, t in sorted(self.chunks))


@dataclass
class Turn:
    """One turn of Allen's words and everything the log says about it."""

    turn_id: str
    transcript: str = ""
    utterance: float | None = None
    endpoint_reason: str | None = None
    started: float | None = None
    ended: float | None = None
    responses: list[Resp] = field(default_factory=list)
    actions: list[Ev] = field(default_factory=list)
    results: list[Ev] = field(default_factory=list)

    def finals(self) -> list[Resp]:
        """Runs that are the answer (not wait lines)."""
        return [r for r in self.responses if r.phase != "commentary"]

    def first_final_playback(self) -> float | None:
        """When the answer first reached the speaker."""
        times = [pb.started for r in self.finals() for pb in r.playbacks]
        return min(times, default=None)

    def answer_text(self) -> str:
        """What the answer said."""
        return " ".join(r.text for r in self.finals() if r.text)


_TAG = re.compile(r"</?[a-z_]+>")
_NOISE = frozenset({"surface.playback_checkpoint", "surface.playback_alignment"})
_PLAYBACK_ENDS = (
    "surface.playback_completed",
    "surface.playback_interrupted",
    "surface.playback_failed",
)
_RESPONSE_ENDS = ("response.completed", "response.cancelled", "response.failed")


def build_turns(events: Sequence[Ev]) -> list[Turn]:
    """Group a slice of the Event Log into turns, their runs, chunks, playbacks and actions."""
    turns: dict[str, Turn] = {}
    resps: dict[str, Resp] = {}
    owner: dict[str, Turn] = {}

    def turn(ev: Ev) -> Turn | None:
        return turns.setdefault(ev.turn_id, Turn(ev.turn_id)) if ev.turn_id else None

    def resp(ev: Ev) -> Resp | None:
        rid = ev.p.get("response_id")
        if not isinstance(rid, str):
            return None
        if rid not in resps:
            resps[rid] = Resp(rid)
            if (t := turn(ev)) is not None:
                t.responses.append(resps[rid])
        return resps[rid]

    for ev in events:
        t, r = turn(ev), resp(ev)
        match ev.type:
            case "utterance.received" if t is not None:
                t.utterance = ev.ts
                t.transcript = str(ev.p.get("transcript", ""))
                t.endpoint_reason = ev.p.get("endpoint_reason")
            case "turn.started" if t is not None:
                t.started = ev.ts
            case "turn.ended" if t is not None:
                t.ended = ev.ts
            case "response.started" if r is not None:
                r.started = ev.ts
                r.phase = str(ev.p.get("phase", r.phase))
                r.route = ev.p.get("route")
                r.emission_mode = ev.p.get("emission_mode")
            case "surface.response_open" if r is not None:
                r.open_ts = ev.ts
                r.phase = str(ev.p.get("phase", r.phase))
            case "surface.response_chunk" if r is not None:
                words = _TAG.sub(
                    "", str(ev.p.get("text", ""))
                ).strip()  # <voice> markers are no speech
                if words:
                    r.chunks.append((int(ev.p.get("sequence", 0)), ev.ts, words))
            case "surface.response_emitted" if r is not None:
                r.emitted = ev.ts
                r.emitted_text = _TAG.sub("", str(ev.p.get("text", ""))).strip()
            case "response.completed" | "response.cancelled" | "response.failed" if r is not None:
                r.end_ts, r.end_type = ev.ts, ev.type.split(".")[1]
                r.end_reason = ev.p.get("reason")
            case "surface.playback_started" if r is not None:
                r.playbacks.append(Playback(ev.ts, ev.p.get("playback_generation_id")))
            case (
                "surface.playback_completed"
                | "surface.playback_interrupted"
                | ("surface.playback_failed")
            ) if r is not None:
                for pb in reversed(r.playbacks):
                    if pb.generation == ev.p.get("playback_generation_id") and pb.ended is None:
                        pb.ended, pb.end_type = ev.ts, ev.type.split("_")[1]
                        pb.reason, pb.heard_text = ev.p.get("reason"), ev.p.get("heard_text")
                        break
            case "action.proposed" if t is not None:
                t.actions.append(ev)
                owner[str(ev.p.get("action_id"))] = t
            case "action.result_observed" if t := owner.get(str(ev.p.get("action_id"))):
                t.results.append(ev)  # its correlation carries the action id only
    return sorted(turns.values(), key=lambda x: x.utterance or x.started or 0.0)


@dataclass
class Ctx:
    """Everything one scenario left behind, for the cases that read it."""

    name: str
    t_start: float
    t_end: float
    events: list[Ev]
    says: list[SayRec]
    marks: dict[str, float]
    mark_events: dict[str, Ev]
    ws: list[dict[str, Any]]
    asr: list[dict[str, Any]]
    tts_requests: list[dict[str, Any]]
    tts_notes: list[dict[str, Any]]
    log_lines: list[str]
    config: dict[str, float]
    aborted: str | None = None

    def of(self, *types: str) -> list[Ev]:
        """Events of the given types, in log order."""
        return [e for e in self.events if e.type in types]

    def turns(self) -> list[Turn]:
        """The scenario's turns."""
        return build_turns(self.events)

    def rel(self, ts: float | None) -> str:
        """``t+12.34`` seconds into the scenario."""
        return "-" if ts is None else f"t+{ts - self.t_start:.2f}"

    def speech_end_before(self, ts: float) -> float | None:
        """The end of the last spoken piece that ended before ``ts``."""
        ends = [p.end for s in self.says for p in s.pieces if p.end <= ts + 0.05]
        return max(ends, default=None)


@dataclass
class Result:
    """A case verdict: PASS, FAIL, INCONCLUSIVE (the property was never exercised) or ERROR."""

    status: str
    summary: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Case:
    """A named check over one scenario (or, with ``all_runs``, over every scenario played)."""

    name: str
    scenario: str
    check: Callable[[list[Ctx]], Result]
    all_runs: bool = False
    doc: str = ""


def _ev_line(ctx: Ctx, ev: Ev) -> str:
    keys = ("reason", "phase", "response_id", "heard_text", "transcript", "text")
    body = {k: ev.p[k] for k in keys if k in ev.p}
    return f"{ctx.rel(ev.ts)} {ev.type} turn={ev.turn_id} {json.dumps(body, ensure_ascii=False)}"


def _round(value: float | None, digits: int = 2) -> float | str:
    return "-" if value is None else round(value, digits)


# ---------------------------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------------------------

Q_SLOW = "How much time did I spend in Xcode today?"
Q_LONG = "Explain how a transformer model works."
Q_CALENDAR = "What's on my calendar today, and give me a tip for staying focused?"
Q_SPLIT: tuple[str | float, ...] = ("What's the weather like", 0.9, "tomorrow in Victoria?")
Q_EN = "Tell me one fun fact about octopuses."
Q_ZH = "用两句话介绍一下章鱼。"


def _final(p: dict[str, Any]) -> bool:
    return p.get("phase") == "final"


def check_wait_line(runs: list[Ctx]) -> Result:
    """No wait line may start after the answer started playing; at most the configured count."""
    ctx = runs[0]
    max_lines = int(ctx.config["commentary_max_lines"])
    evidence: list[str] = []
    lines = 0
    for turn in ctx.turns():
        comments = [r for r in turn.responses if r.phase == "commentary"]
        lines += len(comments)
        answered = turn.first_final_playback()
        if len(comments) > max_lines:
            evidence.append(f"{turn.turn_id}: {len(comments)} wait lines > max {max_lines}")
        for line in comments:
            late = [pb.started for pb in line.playbacks if answered and pb.started > answered]
            if (answered and (line.open_ts or 0) > answered) or late:
                evidence.append(
                    f"{turn.turn_id}: wait line {line.response_id} opened {ctx.rel(line.open_ts)}"
                    f" / played {[ctx.rel(x) for x in late]} after the answer began"
                    f" {ctx.rel(answered)}",
                )
    metrics = {"turns": len(ctx.turns()), "wait_lines": lines, "max_allowed": max_lines}
    if evidence:
        return Result("FAIL", "a wait line started after the answer did", metrics, evidence)
    if not lines:
        return Result("INCONCLUSIVE", "no wait line fired, the rule was not exercised", metrics)
    return Result("PASS", f"{lines} wait line(s), none after the answer began", metrics)


def check_conversation_open(runs: list[Ctx]) -> Result:
    """Conversation mode stays on from Allen's question until her answer plays."""
    ctx = runs[0]
    turns = [t for t in ctx.turns() if t.utterance]
    if not turns:
        return Result("ERROR", "no turn was spoken")
    turn = turns[0]
    answered = turn.first_final_playback()
    if answered is None or turn.utterance is None:
        return Result("INCONCLUSIVE", "the answer never played", {"turns": len(turns)})
    closed = [
        m
        for m in ctx.ws
        if m["op"] == "controls"
        and not (m["payload"] or {}).get("conversation", True)
        and turn.utterance <= m["t"] <= answered
    ]
    lines = [ln for ln in ctx.log_lines if "controls: conversation=False" in ln]
    wait = answered - turn.utterance
    idle_exit = ctx.config["conversation_idle_exit_s"]
    metrics = {"question_to_answer_s": _round(wait), "idle_exit_s": idle_exit}
    if closed:
        return Result(
            "FAIL",
            "conversation mode closed while the turn was still working",
            metrics,
            [f"{ctx.rel(m['t'])} ws controls {m['payload']}" for m in closed] + lines,
        )
    if wait < idle_exit:
        return Result(
            "INCONCLUSIVE",
            f"the answer came in {wait:.1f}s, under the {idle_exit:.0f}s idle exit",
            metrics,
        )
    return Result("PASS", f"stayed open through a {wait:.1f}s wait", metrics)


def check_backchannel(runs: list[Ctx]) -> Result:
    """Listening sounds over her voice start no turn and do not interrupt her."""
    ctx = runs[0]
    stop = ctx.says[-1].t0  # the stop word that ends the scenario is judged by another case
    spoken = ctx.says[1:-1]
    turns = [t for t in ctx.turns() if (t.utterance or 0) < stop]
    played = [pb for t in turns[:1] for r in t.finals() for pb in r.playbacks]
    if not played:
        return Result("ERROR", "her answer never played, nothing to talk over")
    start = min(pb.started for pb in played)
    end = min(max((pb.ended or ctx.t_end) for pb in played), stop)
    over = [s for s in spoken if start <= s.t0 <= end]
    interrupts = [
        e
        for e in ctx.of("surface.playback_interrupted", "response.cancelled")
        if start <= e.ts < stop
    ]
    absorbed = [
        ln
        for ln in ctx.log_lines
        if ("were no turn" in ln or "empty utterance" in ln) and _log_time(ln) < stop
    ]
    heard = [a["text"] for a in ctx.asr if a["t_wall"] < stop and a["audio_s"] < 3.0]
    metrics = {
        "turns": len(turns),
        "backchannels_over_her": len(over),
        "absorbed_by_daemon": len(absorbed),
        "asr_heard": heard,
    }
    evidence = (
        [_ev_line(ctx, e) for e in interrupts]
        + [f"{ctx.rel(t.utterance)} turn {t.turn_id} {t.transcript!r}" for t in turns[1:]]
        + absorbed
    )
    if len(over) < len(spoken):
        return Result(
            "ERROR",
            "a backchannel was said while she was not speaking",
            metrics,
            [
                f"{s.text!r} {ctx.rel(s.t0)} vs playback {ctx.rel(start)}-{ctx.rel(end)}"
                for s in spoken
            ],
        )
    if len(turns) > 1 or interrupts:
        return Result(
            "FAIL", "a listening sound became a turn or interrupted her", metrics, evidence
        )
    if not absorbed:
        return Result(
            "INCONCLUSIVE",
            "no turn and no interruption, but the daemon never logged hearing the sounds",
            metrics,
            evidence,
        )
    return Result(
        "PASS", f"{len(absorbed)} sound(s) heard and absorbed, no turn, not interrupted", metrics
    )


def _log_time(line: str) -> float:
    """Wall time of a daemon log line (``2026-10-01 21:37:20,243 ...``)."""
    return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()  # noqa: DTZ007


def check_stop_word(runs: list[Ctx]) -> Result:
    """The word stop over her voice stops her within 1.5 s of its end and is no turn."""
    ctx = runs[0]
    stop = ctx.says[-1]
    interrupted = [e for e in ctx.of("surface.playback_interrupted") if e.ts >= stop.t0]
    turns = ctx.turns()
    new_turns = [t for t in turns if (t.utterance or 0) >= stop.t0]
    heard = [a["text"] for a in ctx.asr if a["t_wall"] >= stop.t0]
    latency = interrupted[0].ts - stop.end if interrupted else None
    metrics = {
        "stop_end_to_interrupt_s": _round(latency),
        "new_turns": len(new_turns),
        "asr_heard": heard,
        "interrupt_reason": interrupted[0].p.get("reason") if interrupted else "-",
    }
    evidence = [_ev_line(ctx, e) for e in interrupted] + [
        f"{ctx.rel(t.utterance)} new turn {t.turn_id} {t.transcript!r}" for t in new_turns
    ]
    if not interrupted:
        return Result("FAIL", "she kept talking after 'stop'", metrics, evidence)
    if latency is not None and latency > 1.5:
        return Result(
            "FAIL", f"stopped {latency:.2f}s after the word ended (> 1.5s)", metrics, evidence
        )
    if new_turns:
        return Result("FAIL", "'stop' also became a turn", metrics, evidence)
    return Result("PASS", f"stopped {latency:.2f}s after the word ended, no new turn", metrics)


def check_split(runs: list[Ctx]) -> Result:
    """A mid-sentence pause of 0.9 s ends in one effective answer."""
    ctx = runs[0]
    turns = [t for t in ctx.turns() if t.utterance]
    answered = [t for t in turns if any(pb.started for r in t.finals() for pb in r.playbacks)]
    superseded = [
        e for e in ctx.of("response.cancelled") if e.p.get("reason") in {"superseded", "barge_in"}
    ]
    merged = any(
        "weather" in t.transcript.lower() and "tomorrow" in t.transcript.lower() for t in turns
    )
    metrics = {
        "turns": len(turns),
        "answers_spoken": len(answered),
        "merged_transcript": merged,
        "endpoint_reasons": [t.endpoint_reason for t in turns],
        "superseded": len(superseded),
    }
    evidence = [
        f"{ctx.rel(t.utterance)} {t.turn_id} {t.transcript!r} [{t.endpoint_reason}]" for t in turns
    ] + [_ev_line(ctx, e) for e in superseded]
    if len(answered) != 1:
        return Result(
            "FAIL", f"{len(answered)} answers were spoken for one sentence", metrics, evidence
        )
    return Result(
        "PASS",
        "one answer spoken" + ("" if merged else " (to a fragment: the halves never merged)"),
        metrics,
        evidence if not merged else [],
    )


def check_long_streamed(runs: list[Ctx]) -> Result:
    """A long English answer is spoken in several chunks and starts before the run completes."""
    ctx = runs[0]
    turns = [t for t in ctx.turns() if t.utterance]
    if not turns or not turns[0].finals():
        return Result("ERROR", "no answer was produced")
    turn = turns[0]
    answer = max(turn.finals(), key=lambda r: len(r.chunks))
    first_audio = turn.first_final_playback()
    eos = ctx.speech_end_before(turn.utterance or 0.0)
    notes = [n for n in ctx.tts_notes if n.get("response_id") == answer.response_id]
    metrics = {
        "chunks": len(answer.chunks),
        "tts_segments": len(notes),
        "eos_to_first_audio_s": _round(first_audio - eos if first_audio and eos else None),
        "audio_before_completed_s": _round(
            answer.end_ts - first_audio if answer.end_ts and first_audio else None
        ),
        "route": answer.route,
        "emission_mode": answer.emission_mode,
    }
    evidence = [f"chunk {i} {ctx.rel(t)} {txt!r}" for i, t, txt in sorted(answer.chunks)[:8]]
    if len(answer.chunks) < 2:
        return Result("FAIL", "the answer was one chunk, not streamed", metrics, evidence)
    if first_audio is None or answer.end_ts is None or first_audio >= answer.end_ts:
        if ctx.config.get("terse"):
            return Result(
                "INCONCLUSIVE",
                "--terse answers arrive in one burst; run without it to judge streaming",
                metrics,
                evidence,
            )
        return Result("FAIL", "no audio before the run completed", metrics, evidence)
    return Result(
        "PASS",
        f"{len(answer.chunks)} chunks, audio {metrics['audio_before_completed_s']}s"
        " before the run completed",
        metrics,
    )


def check_after_tool_streamed(runs: list[Ctx]) -> Result:
    """The answer after a tool is still spoken sentence by sentence."""
    ctx = runs[0]
    turns = [t for t in ctx.turns() if t.utterance]
    tool_turns = [t for t in turns if t.actions]
    if not tool_turns:
        return Result(
            "INCONCLUSIVE",
            "the model called no tool",
            {"turns": len(turns)},
            [t.transcript for t in turns],
        )
    turn = tool_turns[0]
    result_ts = min((e.ts for e in turn.results), default=None)
    answer = max(turn.finals(), key=lambda r: len(r.chunks), default=None)
    if answer is None:
        return Result("ERROR", "no answer after the tool")
    after = [c for c in answer.chunks if result_ts is None or c[1] >= result_ts]
    first_audio = turn.first_final_playback()
    spread = max((c[1] for c in after), default=0.0) - min((c[1] for c in after), default=0.0)
    metrics = {
        "tools": [e.p.get("tool_name") for e in turn.actions],
        "chunks": len(answer.chunks),
        "chunks_after_tool_result": len(after),
        "chunk_spread_s": _round(spread),
        "first_audio_before_completed": bool(
            first_audio and answer.end_ts and first_audio < answer.end_ts
        ),
        "route": answer.route,
        "emission_mode": answer.emission_mode,
    }
    evidence = (
        [f"tool result {ctx.rel(result_ts)}"]
        + [f"chunk {i} {ctx.rel(t)} {txt!r}" for i, t, txt in sorted(answer.chunks)]
        + [f"completed {ctx.rel(answer.end_ts)} first audio {ctx.rel(first_audio)}"]
    )
    if len(after) < 2:
        return Result(
            "FAIL",
            f"{len(after)} chunk(s) after the tool: not sentence by sentence",
            metrics,
            evidence,
        )
    if not metrics["first_audio_before_completed"] or spread < 0.1:
        return Result(
            "FAIL",
            "the answer was delivered whole, not streamed",
            metrics,
            evidence,
        )
    return Result("PASS", f"{len(after)} chunks streamed after the tool result", metrics)


def check_no_repeat_tools(runs: list[Ctx]) -> Result:
    """No turn proposes the same tool with identical arguments twice."""
    seen: dict[tuple[str, str, str], list[str]] = {}
    total = 0
    for ctx in runs:
        for turn in ctx.turns():
            for ev in turn.actions:
                total += 1
                args = json.dumps(ev.p.get("arguments"), sort_keys=True, ensure_ascii=False)
                key = (turn.turn_id, str(ev.p.get("tool_name")), args)
                seen.setdefault(key, []).append(f"{ctx.name} {ctx.rel(ev.ts)}")
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    metrics = {"tool_calls": total, "turns_scanned": sum(len(c.turns()) for c in runs)}
    if dupes:
        return Result(
            "FAIL",
            f"{len(dupes)} repeated tool call(s)",
            metrics,
            [f"{k[0]} {k[1]} {k[2][:120]} x{len(v)} at {v}" for k, v in dupes.items()],
        )
    if not total:
        return Result("INCONCLUSIVE", "no tool was called in the scenarios played", metrics)
    return Result("PASS", f"{total} tool call(s), none repeated", metrics)


def check_stop_button(runs: list[Ctx]) -> Result:
    """The stop button stops audible speech within 500 ms."""
    ctx = runs[0]
    press = ctx.marks.get("stop_press")
    if press is None:
        return Result("ERROR", "the stop button was never pressed (no answer was playing)")
    interrupted = [e for e in ctx.of("surface.playback_interrupted") if e.ts >= press - 0.05]
    outcome = ctx.mark_events.get("stop_outcome")
    latency = interrupted[0].ts - press if interrupted else None
    metrics = {
        "press_to_interrupt_s": _round(latency),
        "outcome": outcome.p if outcome else "-",
        "reason": interrupted[0].p.get("reason") if interrupted else "-",
    }
    evidence = [_ev_line(ctx, e) for e in interrupted]
    if latency is None:
        return Result("FAIL", "playback was not interrupted", metrics, evidence)
    if latency > 0.5:
        return Result(
            "FAIL", f"stopped {latency * 1000:.0f} ms after the press (> 500 ms)", metrics, evidence
        )
    return Result("PASS", f"stopped {latency * 1000:.0f} ms after the press", metrics)


def check_stop_thinking(runs: list[Ctx]) -> Result:
    """Stopping a thinking turn cancels it and no answer is spoken afterwards."""
    ctx = runs[0]
    press = ctx.marks.get("stop_press")
    if press is None:
        return Result("ERROR", "the stop button was never pressed")
    turn = next((t for t in ctx.turns() if t.utterance), None)
    if turn is None:
        return Result("ERROR", "no turn")
    before = [pb for r in turn.finals() for pb in r.playbacks if pb.started < press] + [
        c for r in turn.finals() for c in r.chunks if c[1] < press
    ]
    if before:
        return Result(
            "INCONCLUSIVE",
            "the answer had already started when the button was pressed",
            {"press": ctx.rel(press)},
        )
    cancelled = [e for e in ctx.of("response.cancelled") if e.ts >= press - 0.05]
    spoken = [pb.started for r in turn.finals() for pb in r.playbacks if pb.started > press]
    chunks_after = [c for r in turn.finals() for c in r.chunks if c[1] > press]
    outcome = ctx.mark_events.get("stop_outcome")
    metrics = {
        "cancelled_reason": [e.p.get("reason") for e in cancelled],
        "answer_chunks_after": len(chunks_after),
        "playbacks_after": len(spoken),
        "outcome": outcome.p if outcome else "-",
    }
    evidence = [_ev_line(ctx, e) for e in cancelled] + [
        f"chunk {c[0]} {ctx.rel(c[1])} {c[2]!r}" for c in chunks_after
    ]
    if not any(e.p.get("reason") == "user_stop" for e in cancelled):
        return Result("FAIL", "the turn was not cancelled as user_stop", metrics, evidence)
    if spoken or chunks_after:
        return Result("FAIL", "an answer was spoken after the stop", metrics, evidence)
    return Result("PASS", "cancelled as user_stop, nothing spoken after", metrics)


def check_language(runs: list[Ctx]) -> Result:
    """Each answer is synthesized in the language of the question."""
    ctx = runs[0]
    rows: list[str] = []
    bad: list[str] = []
    boosts: dict[str, str] = {}
    conn_boost: dict[str, str] = {}
    for req in ctx.tts_requests:
        if req["event"] == "task_start":
            conn_boost[req["conn"]] = str(req.get("language_boost"))
        elif req["event"] == "task_continue":
            boosts[str(req.get("text"))] = conn_boost.get(req["conn"], "?")
    asked = [t for t in ctx.turns() if t.utterance]
    for turn in asked:
        want = text_language(turn.transcript)
        expect = {"zh": "Chinese", "en": "English"}[want]
        for resp in turn.finals():
            for note in sorted(
                (n for n in ctx.tts_notes if n.get("response_id") == resp.response_id),
                key=lambda n: n.get("sequence", 0),
            ):
                text = str(note.get("text"))
                got = boosts.get(text)
                rows.append(f"{turn.transcript!r} -> {text[:50]!r} sent as {got}")
                if got is None:
                    bad.append(f"no MiniMax task_continue was logged for {text[:50]!r}")
                elif got != expect:
                    bad.append(
                        f"{turn.transcript!r} wants {expect}, {text[:50]!r} was sent as {got}"
                    )
    metrics = {"questions": len(asked), "tts_segments": len(rows)}
    if not rows:
        return Result(
            "ERROR",
            "no TTS segment was recorded",
            metrics,
            [f"{len(ctx.tts_requests)} MiniMax requests logged"],
        )
    if bad:
        return Result("FAIL", "synthesis language differs from the question's", metrics, bad + rows)
    return Result("PASS", "every segment was synthesized in the question's language", metrics, rows)


CONFIG_KEYS = ("commentary_max_lines", "conversation_idle_exit_s")


def _stopword(text: str) -> Say:
    return Say(text, gap_before=0.0)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("slow_tool", (Controls(True), Say(Q_SLOW), WaitIdle(timeout=150))),
    Scenario(
        "talk_over",
        (
            Controls(True),
            Say(Q_LONG, long=True),
            SayWhen("surface.playback_started", 2.0, "Mm-hmm.", where=_final, real_ok=False),
            Sleep(1.5),
            Say("嗯", real_ok=False),
            Sleep(1.5),
            Say("Stop."),
            WaitIdle(),
        ),
    ),
    Scenario("split_utterance", (Controls(True), Say(list(Q_SPLIT), asr="scripted"), WaitIdle())),
    Scenario("calendar_tool", (Controls(True), Say(Q_CALENDAR, long=True), WaitIdle(timeout=150))),
    Scenario(
        "stop_button",
        (
            Controls(True),
            Say(Q_LONG, long=True),
            WaitFor("surface.playback_started", _final),
            Sleep(2.0),
            PressStop("answer"),
            WaitIdle(),
        ),
    ),
    Scenario(
        "stop_thinking",
        (
            Controls(True),
            Say(Q_LONG, long=True),
            WaitFor("turn.started"),
            Sleep(0.3),
            PressStop("turn"),
            WaitIdle(),
        ),
    ),
    Scenario(
        "language",
        (
            Controls(True),
            Say(Q_EN),
            WaitIdle(),
            Say(Q_ZH),
            WaitIdle(),
        ),
    ),
)

CASES: tuple[Case, ...] = (
    Case(
        "wait_line_after_answer",
        "slow_tool",
        check_wait_line,
        doc="no wait line starts after the answer began; at most the configured count",
    ),
    Case(
        "backchannel_no_turn",
        "talk_over",
        check_backchannel,
        doc="mm-hmm and 嗯 over her voice are no turn and do not interrupt her",
    ),
    Case(
        "stop_word_stops",
        "talk_over",
        check_stop_word,
        doc="'stop' over her voice stops her within 1.5 s of its end, no new turn",
    ),
    Case(
        "split_utterance_one_turn",
        "split_utterance",
        check_split,
        doc="'What's the weather like' + 0.9 s + 'tomorrow in Victoria?' = one answer",
    ),
    Case(
        "long_english_streamed",
        "talk_over",
        check_long_streamed,
        doc="a long English answer is chunked and audible before the run completes",
    ),
    Case(
        "english_after_tool_streamed",
        "calendar_tool",
        check_after_tool_streamed,
        doc="the answer after a tool is streamed sentence by sentence",
    ),
    Case(
        "no_repeated_tool_calls",
        "calendar_tool",
        check_no_repeat_tools,
        all_runs=True,
        doc="per turn, no tool is proposed twice with identical arguments",
    ),
    Case(
        "conversation_stays_open_during_tool",
        "slow_tool",
        check_conversation_open,
        doc="conversation mode is not closed before the answer plays",
    ),
    Case(
        "stop_button_stops_playback",
        "stop_button",
        check_stop_button,
        doc="foreground_output cancel interrupts playback within 500 ms",
    ),
    Case(
        "stop_button_stops_thinking",
        "stop_thinking",
        check_stop_thinking,
        doc="user_stop on a thinking turn leaves no spoken answer",
    ),
    Case(
        "synthesis_language",
        "language",
        check_language,
        doc="TTS language follows the question's language (English and Chinese)",
    ),
)

TERSE_SUFFIX = " Answer in two very short sentences."
_CJK = re.compile(r"[㐀-鿿]")


def voice_for(text: str) -> str:
    """The macOS ``say`` voice: Tingting when the text holds any CJK, else Samantha."""
    return "Tingting" if _CJK.search(text) else "Samantha"
