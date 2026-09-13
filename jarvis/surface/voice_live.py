"""GPT-Live full-duplex voice session: mic tee → OpenAI Live WebSocket → local player.

ADR-0016: the model listens and speaks on its own; Jarvis owns the
microphone, the speaker and the session lifecycle.  Ordinary conversation
never enters L3.  A ``session.delegation.created`` is the one bridge to the
backend (D2): the request is built from the user transcript around the
delegation's timeline offset, submitted through the callables the
composition root injects, and the answer comes back as a ``commentary`` or
``thinking`` append (D4, D5).  The conversation is persisted to memory.db
through the injected ``record`` callable (D3).  This module imports nothing
from ``state``, ``decision`` or ``runtime`` (D9).  The session is billed per
second, so every exit path funnels through :meth:`LiveVoice.stop` and the
daemon never opens one on its own.

Wire facts come from the ``openai`` 3.13 ``types/live`` package: one
``session.start`` → ``session.started``; audio both ways as base64 PCM16 at
the startup-selected rate; transcripts as ordered deltas without turn
boundaries; ``session.close`` → terminal ``session.closed`` with cumulative
``usage.seconds``.  Appends carry an ``event_id`` and are acknowledged by
``client_event_id``; the ACK proves
receipt, never that the model consumed or spoke the content.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import dataclasses
import itertools
import json
import logging
import os
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from jarvis.surface import voice_audio, voice_tts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.surface.inherent_output import InherentBroadcaster

LOGGER = logging.getLogger(__name__)

LIVE_WS_URL = "wss://api.openai.com/v1/live/sessions"

# The official template's fixed labels (docs/gpt-live/live-prompting.md); the
# backend owns tool rules and permissions, so only the read-only capabilities
# of ADR-0016 D6 are listed here.
DEFAULT_INSTRUCTIONS = """\
You are Jarvis, Allen 的私人语音助手。
语言：默认自然、简短的中文口语；Allen 说英文时切换到英文。
节奏：像面对面聊天，一次只说一两句，不长篇大论，不重复解释。
Backchannel policy: Use moderate backchannels. 简短的"嗯""好"即可。
Interruption policy: Stop speaking when the user interrupts. Listen to what they say.
被要求"别说了"时立刻停下，等 Allen 再开口再回应。
Delegation policy:
Backend tools:
- 后台只能查，不能做：搜网页、读网页、查笔记、查过去的对话记录、看当前时间。
Delegate to the backend when:
- Allen 要查资料、查最新或动态信息、回忆以前说过的事、要一个需要核实的事实。
Do not delegate to the backend when:
- 闲聊、寒暄、你自己就能答的常识；Allen 要执行操作时直接说明这一版后台只能查不能做。
Do not guess the result while waiting.
等后台结果时可以继续聊别的，但不要编造查询结果，也不要说已经查到了。
"""  # noqa: RUF001 — intentional Chinese punctuation.

# Appends are written in the language the model speaks (docs/gpt-live/live-prompting.md).
# Commentary is a fact for the model to say in its own words, not an instruction to it;
# thinking is a fact it may use later without speaking it now.
NO_REQUEST_THINKING = (
    "后台没有捕获到要查的内容，请让用户再说一遍要查什么。"  # noqa: RUF001 — intentional Chinese punctuation.
)
FAILED_COMMENTARY = "刚才那个查询失败了，后台没有拿到结果。"  # noqa: RUF001 — intentional Chinese punctuation.
NO_BACKEND_COMMENTARY = "这个会话没有接后台，查不了。"  # noqa: RUF001 — intentional Chinese punctuation.
TIMEOUT_COMMENTARY = "刚才那个查询还没拿到结果，拿到后再说。"  # noqa: RUF001 — intentional Chinese punctuation.
LONG_RESULT_COMMENTARY = (
    "查到了，但结果太长不适合口述，完整结果在界面上。"  # noqa: RUF001 — intentional Chinese punctuation.
)

LiveState = Literal["idle", "connecting", "active", "closing"]
DeliveryKind = Literal["commentary", "thinking"]

_HEARING_HOLD_S = 1.5
_MONITOR_PERIOD_S = 0.1
_READER_POLL_S = 0.05
# A delegation can arrive before the sentence that caused it is transcribed:
# wait for fragments to stop for ``transcript_settle_ms``, at most this long.
_SETTLE_MAX_S = 2.0
# A same-speaker pause this long closes a memory.db row (D3).
_ROW_GAP_S = 1.5
# No user speech older than this belongs to a delegation, and a first query
# the model has not spoken before opens its window this far back (D2).
_REQUEST_REACH_MS = 10_000
# Speech-sized result budget (D4): about this many Chinese characters, cut at a
# sentence end, so a 500-token append never fails on length.
_COMMENTARY_BUDGET_CHARS = 300
_SENTENCE_ENDS = "。！？!?；;\n"  # noqa: RUF001 — both scripts' sentence punctuation.
_BRIEF_MAX_CHARS = 1500
# The bus wake covers ResponseRun terminals; ``turn.failed`` and a missing bus
# are covered by this slow safety poll of the Event Log.
_RESULT_POLL_S = 5.0
# ``response.completed`` commits before the renderer writes the
# ``surface.response_emitted`` row (runtime/__init__.py, terminalizer.complete
# precedes render_response); the bus wakes on both, and a wake re-reads briefly
# instead of waiting a poll.
_WAKE_RETRY_S = 0.2
_WAKE_RETRIES = 15


@dataclasses.dataclass(frozen=True)
class GptLiveConfig:
    """``realtime.gpt_live`` block; defaults are the shipped values."""

    enabled: bool = False
    model: str = "gpt-live-1"
    voice: str = "marin"
    sample_rate_hz: int = 24000
    instructions: str = DEFAULT_INSTRUCTIONS
    api_key_env: str = "OPENAI_API_KEY"
    idle_close_s: float = 30.0
    max_session_s: float = 1800.0
    connect_timeout_s: float = 10.0
    close_timeout_s: float = 10.0
    input_backlog_s: float = 0.5
    input_chunk_ms: int = 100
    player_ring_seconds: float = 30.0
    # ADR-0016 D2/D5: how long user fragments must stop before a delegation's
    # request is frozen, and how long a submitted turn may take before the
    # session is told there is no result yet.
    transcript_settle_ms: int = 600
    delegation_timeout_s: float = 90.0


_FIELD_CHECKS: dict[str, tuple[Callable[[object], bool], str]] = {
    "bool": (lambda v: isinstance(v, bool), "a bool"),
    "int": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "a positive int"),
    "float": (
        lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0,
        "a positive number",
    ),
    "str": (lambda v: isinstance(v, str) and bool(v), "a non-empty string"),
}


def gpt_live_config_from_mapping(mapping: Mapping[str, object]) -> GptLiveConfig:
    """Validate the ``realtime.gpt_live`` mapping; malformed explicit keys raise."""
    kwargs: dict[str, object] = {}
    for field in dataclasses.fields(GptLiveConfig):
        if field.name not in mapping:
            continue
        value = mapping[field.name]
        check, expected = _FIELD_CHECKS[str(field.type)]
        if not check(value):
            msg = f"realtime.gpt_live.{field.name} must be {expected}"
            raise TypeError(msg)
        kwargs[field.name] = float(value) if field.type == "float" else value  # type: ignore[arg-type]
    config = GptLiveConfig(**kwargs)  # type: ignore[arg-type]
    if config.sample_rate_hz not in (16000, 24000):
        msg = "realtime.gpt_live.sample_rate_hz must be 16000 or 24000"
        raise ValueError(msg)
    return config


class _Resampler:
    """Stateful mono float32 resampler; identity when the rates match."""

    def __init__(self, *, input_rate_hz: int, output_rate_hz: int) -> None:
        self._stream: Any | None = None
        if input_rate_hz != output_rate_hz:
            import soxr  # noqa: PLC0415 - optional wheel, imported where used

            self._stream = soxr.ResampleStream(
                input_rate_hz, output_rate_hz, 1, dtype="float32", quality="HQ",
            )

    def feed(self, samples: np.ndarray) -> np.ndarray:
        if self._stream is None:
            return samples
        return self._stream.resample_chunk(samples)


@dataclasses.dataclass(frozen=True)
class DelegationResult:
    """What the composition root's ``lookup_result`` found for one ``turn_id``.

    ``status`` is ``answered`` when a ``surface.response_emitted`` row exists,
    ``failed`` when only a failure terminal does.  ``voice_text`` is the L3
    spoken cut when the renderer produced one.
    """

    status: Literal["answered", "failed"]
    text: str = ""
    voice_text: str | None = None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class _Fragment:
    """One transcript delta on the session timeline."""

    start_ms: int
    end_ms: int
    text: str


@dataclasses.dataclass
class _RowBuffer:
    """Unflushed same-speaker fragments; closed into one memory.db row on a pause."""

    fragments: list[_Fragment] = dataclasses.field(default_factory=list)
    last_append: float = 0.0

    def text(self) -> str:
        return "".join(f.text for f in self.fragments).strip()


@dataclasses.dataclass
class _Pending:
    """One client delegation, frozen at submission so a redelivery replays it (D2).

    ``foreground`` is true until a newer delegation supersedes this one while
    it is still running (D5); a superseded turn completes into memory.db, but
    its result is withheld from Live.
    """

    delegation_id: str
    session_id: str
    epoch: int
    offset_ms: int
    window_start_ms: int
    # Set once the window is frozen: the same id the pause flusher would give a
    # row starting at the first window fragment, so an already-flushed row is
    # not written twice (append_record ignores a repeated id).
    record_id: str = ""
    created: float = dataclasses.field(default_factory=time.monotonic)
    request_text: str | None = None
    # The request of the delegation this one superseded (D5): a lone correction
    # such as "改成后天的" is only resolvable next to what it corrects.
    prior_request: str | None = None
    window_end_ms: int = 0
    turn_id: str | None = None
    state: str = "settling"
    foreground: bool = True
    timed_out: bool = False
    wake: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


@dataclasses.dataclass
class _Append:
    """A sent append awaiting its ACK, matched by ``client_event_id`` (D4)."""

    delegation_id: str | None
    kind: str
    content: str
    retried: bool = False


@dataclasses.dataclass
class _LiveRun:
    """Everything one session owns; dropped as a unit by :meth:`LiveVoice._teardown`."""

    epoch: int
    player: voice_tts.AudioStreamPlayer
    subscription: voice_audio.AudioSubscription
    ws: Any  # websockets.asyncio.client.ClientConnection
    started_monotonic: float = dataclasses.field(default_factory=time.monotonic)
    session_id: str | None = None
    tasks: list[asyncio.Task[None]] = dataclasses.field(default_factory=list)
    # ADR-0016 D2-D5: delegation bookkeeping. ``user_fragments`` keeps every
    # user delta for request windows and ``assistant_end_ms`` where each
    # assistant delta ended, for window starts; the row buffers hold what
    # memory.db has not seen yet; ``pending`` is keyed by delegation id;
    # ``appends`` by the ``event_id`` of a sent append.
    user_fragments: list[_Fragment] = dataclasses.field(default_factory=list)
    assistant_end_ms: list[int] = dataclasses.field(default_factory=list)
    last_user_fragment_at: float = 0.0
    user_row: _RowBuffer = dataclasses.field(default_factory=_RowBuffer)
    assistant_row: _RowBuffer = dataclasses.field(default_factory=_RowBuffer)
    pending: dict[str, _Pending] = dataclasses.field(default_factory=dict)
    last_delegation_offset_ms: int = 0
    appends: dict[str, _Append] = dataclasses.field(default_factory=dict)
    delegation_tasks: list[asyncio.Task[None]] = dataclasses.field(default_factory=list)
    # Mic → model. The reader thread fills ``out``; the send task drains it.
    out: collections.deque[bytes] = dataclasses.field(default_factory=collections.deque)
    out_bytes: int = 0
    out_ready: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    reader_stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    mic_muted: bool = False
    dropped_backlog_events: int = 0
    # Model → speaker. The recv task fills ``play``; the writer thread drains it.
    play: queue.Queue[bytes | None] = dataclasses.field(default_factory=queue.Queue)
    writer: threading.Thread | None = None
    writer_busy: bool = False
    cancel_write: threading.Event = dataclasses.field(default_factory=threading.Event)
    # Latest end_ms of USER speech. Assistant transcript timestamps run ahead of
    # local playback by whatever is buffered, so they must not move the boundary.
    max_user_end_ms: int = 0
    # Presence + accounting.
    last_activity: float = dataclasses.field(default_factory=time.monotonic)
    hearing_until: float = 0.0
    speaking: bool = False
    hearing: bool = False
    subtitle_seq: int = 0
    event_seq: int = 0
    usage_s: float | None = None
    usage_final: bool = False
    close_reason: str | None = None
    server_reason: str | None = None
    closed: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    last_error: str | None = None
    notice: str | None = None


class LiveVoice:
    """Lifecycle owner for one GPT-Live session at a time.

    ``idle → connecting → active → closing → idle``; every transition runs
    under one asyncio lock, and callbacks from a finished run are ignored by
    epoch.  Control entry points are coroutines on the daemon loop; the mic
    reader and the player writer are plain threads because both sides block.
    """

    def __init__(  # noqa: PLR0913 - explicit composition-root wiring
        self,
        *,
        config: GptLiveConfig,
        broadcaster: InherentBroadcaster,
        ingress: Callable[[], voice_audio.AudioIngress | None],
        mic_muted: Callable[[], bool],
        speech_muted: Callable[[], bool],
        output_device: object | None = None,
        on_owns_speech: Callable[[bool], None] | None = None,
        delegate: Callable[[str, str, str, str], str] | None = None,
        record: Callable[[str, str, str], None] | None = None,
        brief: Callable[[], str] | None = None,
        lookup_result: Callable[[str], DelegationResult | None] | None = None,
    ) -> None:
        """Bind the daemon-owned pieces; nothing connects until :meth:`start`.

        The four ADR-0016 D9 callables are blocking (they touch SQLite) and
        are always called through ``asyncio.to_thread``: ``delegate(text,
        delegation_id, session_id, record_id) -> turn_id`` submits one
        request through the idempotent inbox; ``record(source, text,
        record_id)`` appends one memory.db row; ``brief()`` renders the
        startup context; ``lookup_result(turn_id)`` reads the Event Log.
        Without ``delegate`` a delegation is answered with a fact that the
        backend is not connected.
        """
        self._config = config
        self._broadcaster = broadcaster
        self._ingress = ingress
        self._mic_muted = mic_muted
        self._speech_muted = speech_muted
        self._output_device = output_device
        self._on_owns_speech = on_owns_speech
        self._delegate = delegate
        self._record = record
        self._brief = brief
        self._lookup_result = lookup_result
        self._lock = asyncio.Lock()
        self._state: LiveState = "idle"
        self._epoch = 0
        self._run: _LiveRun | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_status: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def owns_speech(self) -> bool:
        """True while the local chain must stay silent and take no new wake."""
        return self._state in ("connecting", "active", "closing")

    @property
    def stop_budget_s(self) -> float:
        """Upper bound on :meth:`stop`.

        The ``session.closed`` wait plus the fixed socket-close (2 s) and
        thread-join (3 s) bounds in :meth:`_teardown`, plus 1 s of slack.
        """
        return self._config.close_timeout_s + 6.0

    def status(self) -> dict[str, object]:
        """Snapshot for ``POST /inherent/controls`` and the ``live`` wire op."""
        run = self._run
        status: dict[str, object] = {
            "state": self._state,
            "ingress_available": self._ingress() is not None,
            "session_id": None,
            "elapsed_s": 0.0,
            "usage_s": None,
            "usage_final": False,
            "reason": None,
            "server_reason": None,
            "speaking": False,
            "hearing": False,
            "mic_muted": self._mic_muted(),
            "speech_muted": self._speech_muted(),
            "dropped_input_events": 0,
            "error": None,
            "notice": None,
            "pending_delegations": 0,
            "foreground_delegation": None,
        }
        if run is None:
            # Keep the last session's accounting visible after it closed.
            for key in ("usage_s", "usage_final", "reason", "server_reason", "error", "session_id"):
                status[key] = self._last_status.get(key)
            status["usage_final"] = bool(status["usage_final"])
            return status
        status.update(
            session_id=run.session_id,
            elapsed_s=round(time.monotonic() - run.started_monotonic, 1),
            usage_s=run.usage_s,
            usage_final=run.usage_final,
            reason=run.close_reason,
            server_reason=run.server_reason,
            speaking=run.speaking,
            hearing=run.hearing,
            dropped_input_events=run.dropped_backlog_events,
            error=run.last_error,
            notice=run.notice,
            pending_delegations=sum(
                1 for p in run.pending.values() if p.state in ("settling", "submitted")
            ),
            foreground_delegation=next(
                (p.delegation_id for p in run.pending.values() if p.foreground), None,
            ),
        )
        return status

    def deliver(self, turn_id: str) -> None:
        """Wake the delegation that owns ``turn_id``; safe from any thread (D9).

        The composition root calls this from its committed-event-bus
        subscriber when a response terminal commits.  Nothing is read here:
        the delegation task re-queries the Event Log on the loop thread.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._wake_turn, turn_id)

    def _wake_turn(self, turn_id: str) -> None:
        run = self._run
        if run is None:
            return
        for pending in run.pending.values():
            if pending.turn_id == turn_id:
                pending.wake.set()

    # ------------------------------------------------------------------
    # Control entry points; the lock serializes them
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, object]:
        """Open a session, or answer why not; never opens a second one."""
        async with self._lock:
            if self._state != "idle":
                return self._refused(f"busy:{self._state}")
            ingress = self._ingress()
            if ingress is None:
                return self._refused("no_single_ingress")
            api_key = os.environ.get(self._config.api_key_env)
            if not api_key:
                return self._refused(f"missing_{self._config.api_key_env}")
            self._epoch += 1
            self._loop = asyncio.get_running_loop()
            self._state = "connecting"
            self._notify_owns_speech()
            await self._broadcast("connecting")
            try:
                run = await asyncio.wait_for(
                    self._connect(ingress, api_key), timeout=self._config.connect_timeout_s * 2,
                )
            except Exception as exc:  # noqa: BLE001 - every failure lands in idle with a reason
                LOGGER.warning("gpt_live start failed: %s", exc)
                self._last_status = {"reason": "start_failed", "error": str(exc)[:200]}
                self._state = "idle"
                self._notify_owns_speech()
                await self._broadcast("closed")
                return self.status()
            self._run = run
            self._state = "active"
            self._apply_mutes(run)
            run.tasks = [
                asyncio.create_task(self._recv_loop(run), name="gpt_live.recv"),
                asyncio.create_task(self._send_loop(run), name="gpt_live.send"),
                asyncio.create_task(self._monitor_loop(run), name="gpt_live.monitor"),
            ]
            LOGGER.info("gpt_live session %s started (epoch %d)", run.session_id, run.epoch)
            await self._broadcast("started")
            return self.status()

    async def stop(self, *, reason: str = "user") -> dict[str, object]:
        """Hang up: ``session.close`` → wait ``session.closed`` (bounded) → release everything."""
        async with self._lock:
            run = self._run
            if run is None or self._state != "active":
                return self.status()
            self._state = "closing"
            await self._broadcast("closing")
            await self._teardown(run, reason)
            return self.status()

    def set_mic_muted(self, muted: bool) -> None:  # noqa: FBT001 - Callable[[bool], None] shape
        """Local sender gate first, server mute second; nothing recorded while muted is sent."""
        run = self._run
        if run is None or self._state != "active":
            return
        run.mic_muted = muted
        run.out.clear()
        run.out_bytes = 0
        event = "session.input_audio.mute" if muted else "session.input_audio.unmute"
        asyncio.get_running_loop().create_task(self._send_json(run, {"type": event}))

    def set_speech_muted(self, muted: bool) -> None:  # noqa: FBT001 - Callable[[bool], None] shape
        """Speaker mute is the Live player's gain, like the local chain's."""
        run = self._run
        if run is not None:
            run.player.set_gain(0.0 if muted else 1.0)

    # ------------------------------------------------------------------
    # Session bring-up / teardown
    # ------------------------------------------------------------------

    async def _connect(self, ingress: voice_audio.AudioIngress, api_key: str) -> _LiveRun:
        from websockets.asyncio.client import connect  # noqa: PLC0415 - optional wheel

        cfg = self._config
        player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=cfg.sample_rate_hz,
            ring_seconds=cfg.player_ring_seconds,
            device=self._output_device,
            lazy_open=True,
        )
        subscription: voice_audio.AudioSubscription | None = None
        ws: Any = None
        # D7: the brief is one developer message of startup history, rendered by
        # the composition root from memory.db; SQLite work stays off the loop.
        brief = ""
        if self._brief is not None:
            try:
                brief = (await asyncio.to_thread(self._brief))[:_BRIEF_MAX_CHARS]
            except Exception:
                LOGGER.exception("gpt_live brief failed; starting without history")
        history: list[dict[str, object]] = []
        if brief:
            history.append({
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": brief}],
            })
        try:
            player.start()
            ws = await asyncio.wait_for(
                connect(
                    LIVE_WS_URL,
                    additional_headers={"Authorization": f"Bearer {api_key}"},
                    max_size=None,
                ),
                timeout=cfg.connect_timeout_s,
            )
            await ws.send(json.dumps({
                "type": "session.start",
                "session": {
                    "model": cfg.model,
                    "instructions": cfg.instructions,
                    "input": history,
                    "audio": {
                        "format": {"type": "audio/pcm", "rate": cfg.sample_rate_hz},
                        "output": {"voice": cfg.voice},
                    },
                    "delegation": {"type": "client"},
                    "store": False,
                },
            }))
            LOGGER.info("gpt_live session.start sent with %d history chars", len(brief))
            session_id = await asyncio.wait_for(
                _await_started(ws), timeout=cfg.connect_timeout_s,
            )
            # Subscribed only now: the ring keeps its oldest frames, so a lane
            # opened before the handshake hands the reader a second of stale
            # microphone and the first append backlog drops it (measured
            # 2026-09-12: "dropping 24000 stale bytes" 5 ms after session.started).
            # DIAGNOSTIC: when this subscriber's ring overflows the newest frame is
            # dropped (voice_audio.py) and the capture owner's active-discontinuity
            # count is untouched; the reader polls every 50 ms, so overflow means a stall.
            subscription = ingress.subscribe(
                name="gpt_live", purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
            )
        except BaseException:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            if subscription is not None:
                subscription.close()
            with contextlib.suppress(Exception):
                player.close()
            raise
        run = _LiveRun(epoch=self._epoch, player=player, subscription=subscription, ws=ws)
        run.session_id = session_id
        loop = asyncio.get_running_loop()
        run.reader = threading.Thread(
            target=self._reader_main, args=(run, loop), name="gpt_live.reader", daemon=True,
        )
        run.writer = threading.Thread(
            target=self._writer_main, args=(run,), name="gpt_live.writer", daemon=True,
        )
        run.reader.start()
        run.writer.start()
        return run

    async def _teardown(self, run: _LiveRun, reason: str) -> None:
        """Bounded release in dependency order; caller holds the lock and set ``closing``."""
        cfg = self._config
        if run.close_reason is None:
            run.close_reason = reason
        run.reader_stop.set()
        run.mic_muted = True
        await self._close_delegations(run)
        if not run.closed.is_set():
            await self._send_json(run, {"type": "session.close"})
            try:
                await asyncio.wait_for(run.closed.wait(), timeout=cfg.close_timeout_s)
            except TimeoutError:
                LOGGER.warning(
                    "gpt_live session %s: no session.closed within %.1fs; forcing socket close",
                    run.session_id, cfg.close_timeout_s,
                )
        current = asyncio.current_task()
        for task in run.tasks:
            if task is not current:
                task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(run.ws.close(), timeout=2.0)
        for task in run.tasks:
            if task is not current:
                with contextlib.suppress(BaseException):
                    await task
        run.subscription.close()
        if run.reader is not None:
            run.reader.join(timeout=1.0)
        run.cancel_write.set()
        _drain_queue(run.play)
        run.play.put(None)
        if run.writer is not None:
            run.writer.join(timeout=2.0)
        with contextlib.suppress(Exception):
            run.player.flush()
        with contextlib.suppress(Exception):
            run.player.close()
        LOGGER.info(
            "gpt_live session %s closed: reason=%s server_reason=%s usage_s=%s final=%s "
            "dropped_input=%d",
            run.session_id, run.close_reason, run.server_reason, run.usage_s,
            run.usage_final, run.dropped_backlog_events,
        )
        self._last_status = {
            "session_id": run.session_id,
            "usage_s": run.usage_s,
            "usage_final": run.usage_final,
            "reason": run.close_reason,
            "server_reason": run.server_reason,
            "error": run.last_error,
        }
        self._run = None
        self._state = "idle"
        self._notify_owns_speech()
        await self._broadcast("closed")

    async def _close_delegations(self, run: _LiveRun) -> None:
        """Cancel delegation tasks and flush the row buffers before the socket closes.

        The turns keep running in the backend; their results land in
        memory.db and the UI without this session (D4).
        """
        for task in run.delegation_tasks:
            task.cancel()
        for task in run.delegation_tasks:
            with contextlib.suppress(BaseException):
                await task
        open_delegations = [
            p.delegation_id for p in run.pending.values() if p.state in ("settling", "submitted")
        ]
        if open_delegations:
            LOGGER.info("gpt_live closing with open delegations %s", open_delegations)
        await self._flush_row(run, "user")
        await self._flush_row(run, "assistant")

    async def _auto_stop(self, epoch: int, reason: str) -> None:
        async with self._lock:
            run = self._run
            if run is None or run.epoch != epoch or self._state != "active":
                return
            self._state = "closing"
            await self._teardown(run, reason)

    def _schedule_auto_stop(self, run: _LiveRun, reason: str) -> None:
        asyncio.get_running_loop().create_task(self._auto_stop(run.epoch, reason))

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _recv_loop(self, run: _LiveRun) -> None:  # noqa: C901, PLR0912 - one branch per wire event
        from websockets.exceptions import ConnectionClosed  # noqa: PLC0415

        try:
            async for raw in run.ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.output_audio.delta":
                    run.last_activity = time.monotonic()
                    run.play.put(_pcm16_to_float32(base64.b64decode(event["delta"])))
                elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                    await self._on_transcript(run, event)
                elif kind == "session.usage.updated":
                    run.usage_s = float(event["usage"]["seconds"])
                elif kind == "session.delegation.created":
                    await self._on_delegation(run, event)
                elif kind == "error":
                    err = event.get("error") or {}
                    run.last_error = f"{err.get('code')}: {err.get('message')}"
                    LOGGER.warning(
                        "gpt_live error: %s (param=%s client_event_id=%s)",
                        run.last_error, err.get("param"), err.get("client_event_id"),
                    )
                    await self._on_append_error(run, err)
                    await self._broadcast("error")
                elif kind == "session.closed":
                    run.usage_s = float(event["usage"]["seconds"])
                    run.usage_final = True
                    # The server's own reason is kept even when Jarvis initiated the close.
                    run.server_reason = str(event.get("reason"))
                    if run.close_reason is None:
                        run.close_reason = run.server_reason
                    run.closed.set()
                    if self._state == "active":
                        self._schedule_auto_stop(run, run.close_reason)
                    return
                elif kind == "info" or str(kind).endswith(("muted", "appended", "updated")):
                    self._on_ack(run, str(kind), event)
                else:
                    LOGGER.debug("gpt_live event %s", kind)
        except ConnectionClosed as exc:
            LOGGER.warning("gpt_live websocket closed: %s", exc)
            run.closed.set()
            if self._state == "active":
                self._schedule_auto_stop(run, "connection_lost")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("gpt_live recv loop failed")
            run.closed.set()
            if self._state == "active":
                self._schedule_auto_stop(run, "recv_failed")

    async def _on_transcript(self, run: _LiveRun, event: dict[str, Any]) -> None:
        now = time.monotonic()
        run.last_activity = now
        start_ms = int(event.get("start_ms", 0))
        end_ms = int(event.get("end_ms", start_ms))
        role = "user" if event["type"] == "session.input_transcript.delta" else "assistant"
        fragment = _Fragment(start_ms=start_ms, end_ms=end_ms, text=str(event.get("delta", "")))
        if role == "user":
            # Every user delta with its span: three sentence tails went missing
            # right after a row flush (2026-09-12), and only this shows which.
            LOGGER.info("gpt_live user fragment %d-%d %r", start_ms, end_ms, fragment.text)
            run.max_user_end_ms = max(run.max_user_end_ms, end_ms)
            run.hearing_until = now + _HEARING_HOLD_S
            run.user_fragments.append(fragment)
            run.last_user_fragment_at = now
            run.user_row.fragments.append(fragment)
            run.user_row.last_append = now
        else:
            run.assistant_end_ms.append(end_ms)
            run.assistant_row.fragments.append(fragment)
            run.assistant_row.last_append = now
        run.subtitle_seq += 1
        await self._broadcaster.broadcast_op(
            "subtitle",
            session_id=run.session_id,
            seq=run.subtitle_seq,
            role=role,
            delta=str(event.get("delta", "")),
            start_ms=start_ms,
            end_ms=end_ms,
        )

    # ------------------------------------------------------------------
    # Delegation bridge (ADR-0016 D2-D5)
    # ------------------------------------------------------------------

    async def _on_delegation(self, run: _LiveRun, event: dict[str, Any]) -> None:
        """Register the delegation and hand it to a task; the recv loop never waits."""
        delegation_id = str((event.get("delegation") or {}).get("id"))
        offset_ms = int(event.get("offset_ms", run.max_user_end_ms))
        existing = run.pending.get(delegation_id)
        if existing is not None:
            LOGGER.info(
                "gpt_live delegation %s redelivered; pending state=%s turn_id=%s",
                delegation_id, existing.state, existing.turn_id,
            )
            return
        if self._delegate is None:
            LOGGER.info("gpt_live delegation %s refused: no backend injected", delegation_id)
            await self._send_append(
                run, "commentary", NO_BACKEND_COMMENTARY, delegation_id=delegation_id,
            )
            return
        # D5: one foreground query. An older delegation still completes into
        # memory.db and the UI, but its result is withheld from Live: a quiet
        # append can still shape later speech (live-tested 2026-09-12, the
        # superseded "明天" forecast was spoken as "后天"). Only the request
        # it displaces travels as 此前请求: its answer is not in memory.db yet.
        # A finished exchange already closes context_note, and a prefix on
        # an unrelated follow-up misreads it as a correction (live run
        # 2026-09-12 21:32: "明天卡尔加里" answered as 后天). One still
        # settling has no request text yet and lends none.
        prior_request: str | None = None
        for older in run.pending.values():
            if older.foreground and older.state in ("settling", "submitted"):
                older.foreground = False
                prior_request = older.request_text or prior_request
                LOGGER.info(
                    "gpt_live delegation %s superseded by %s; its result stays off Live",
                    older.delegation_id, delegation_id,
                )
        pending = _Pending(
            delegation_id=delegation_id,
            session_id=str(run.session_id),
            epoch=run.epoch,
            offset_ms=offset_ms,
            window_start_ms=_window_start_ms(run, offset_ms),
            prior_request=prior_request,
        )
        run.last_delegation_offset_ms = offset_ms
        run.pending[delegation_id] = pending
        run.notice = None
        LOGGER.info(
            "gpt_live delegation %s registered offset_ms=%d window_start_ms=%d",
            delegation_id, offset_ms, pending.window_start_ms,
        )
        task = asyncio.create_task(
            self._delegation_task(run, pending), name=f"gpt_live.delegation.{delegation_id}",
        )
        run.delegation_tasks.append(task)
        await self._broadcast("state")

    async def _delegation_task(self, run: _LiveRun, pending: _Pending) -> None:
        try:
            await self._run_delegation(run, pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("gpt_live delegation %s task failed", pending.delegation_id)
            pending.state = "failed"
            await self._send_append(
                run, "commentary", FAILED_COMMENTARY, delegation_id=pending.delegation_id,
            )
        finally:
            run.delegation_tasks = [t for t in run.delegation_tasks if not t.done()]

    async def _run_delegation(self, run: _LiveRun, pending: _Pending) -> None:
        cfg = self._config
        fragments, unflushed = await self._settle_request(run, pending)
        text = "".join(f.text for f in fragments).strip()
        if not text:
            pending.state = "dropped"
            LOGGER.info("gpt_live delegation %s dropped: no user transcript in window",
                        pending.delegation_id)
            await self._send_append(
                run, "thinking", NO_REQUEST_THINKING, delegation_id=pending.delegation_id,
            )
            return
        pending.request_text = text
        # D3: only the fragments the pause flusher has not written yet become the
        # request row (live run 2026-09-12: sharing the flushed row's id lost
        # the rest of the sentence to INSERT OR IGNORE). An already-flushed
        # window still names its row so drive_turn neither writes nor repeats it.
        pending.record_id = _row_record_id(run, "allen", (unflushed or fragments)[0])
        # A correction that superseded a running lookup is sent next to the
        # request it corrects; the memory row stays Allen's own words.
        request = (
            f"此前请求：{pending.prior_request}\n用户修正：{text}"  # noqa: RUF001 — Chinese punctuation.
            if pending.prior_request
            else text
        )
        LOGGER.info(
            "gpt_live delegation %s request=%r window=(%d,%d] record_id=%s",
            pending.delegation_id, request, pending.window_start_ms, pending.window_end_ms,
            pending.record_id,
        )
        row_text = "".join(f.text for f in unflushed).strip()
        if self._record is not None and row_text:
            await asyncio.to_thread(self._record, "allen", row_text, pending.record_id)
        assert self._delegate is not None  # noqa: S101 - checked in _on_delegation
        turn_id = await asyncio.to_thread(
            self._delegate, request, pending.delegation_id, pending.session_id, pending.record_id,
        )
        pending.turn_id = turn_id
        pending.state = "submitted"
        LOGGER.info("gpt_live delegation %s submitted turn_id=%s", pending.delegation_id, turn_id)
        await self._send_append(
            run, "thinking",
            f"正在查：{text[:40]}。还没有结果，不要猜。",  # noqa: RUF001 — Chinese punctuation.
            delegation_id=pending.delegation_id,
        )
        # D4/D5: one lookup now, then bus wakes plus a slow safety poll until the
        # deadline; after the deadline a late result is only ever thinking.
        deadline = pending.created + cfg.delegation_timeout_s
        result = await self._await_result(pending, turn_id, deadline)
        if result is None:
            pending.timed_out = True
            LOGGER.info("gpt_live delegation %s timed out after %.0fs (turn_id=%s)",
                        pending.delegation_id, cfg.delegation_timeout_s, turn_id)
            await self._deliver(run, pending, "commentary", TIMEOUT_COMMENTARY)
            result = await self._await_result(pending, turn_id, None)
            assert result is not None  # noqa: S101 - a None deadline only returns with a result
        pending.state = "answered" if result.status == "answered" else "failed"
        if result.status == "failed":
            LOGGER.info("gpt_live delegation %s failed: %s", pending.delegation_id, result.reason)
            await self._deliver(run, pending, "commentary", FAILED_COMMENTARY)
            return
        content = _speech_cut(result.voice_text or result.text) or LONG_RESULT_COMMENTARY
        if pending.timed_out:
            LOGGER.info("gpt_live delegation %s late result -> thinking", pending.delegation_id)
            await self._deliver(run, pending, "thinking", content)
        else:
            await self._deliver(run, pending, "commentary", content)

    async def _settle_request(
        self, run: _LiveRun, pending: _Pending,
    ) -> tuple[list[_Fragment], list[_Fragment]]:
        """Wait for the user's fragments around the offset to stop, bounded (D2).

        Returns the window fragments and the subset the pause flusher had not
        written yet; the latter is what becomes the request's memory row.
        """
        settle_s = self._config.transcript_settle_ms / 1000.0
        window_end = pending.offset_ms + self._config.transcript_settle_ms
        deadline = time.monotonic() + _SETTLE_MAX_S

        def in_window() -> list[_Fragment]:
            return [
                f for f in run.user_fragments
                if pending.window_start_ms < f.start_ms <= window_end
            ]

        while time.monotonic() < deadline:
            idle = time.monotonic() - run.last_user_fragment_at
            if in_window() and idle >= settle_s:
                break
            await asyncio.sleep(_MONITOR_PERIOD_S)
        fragments = in_window()
        pending.window_end_ms = window_end
        # Those fragments become the request row; drop them from the user row
        # buffer so the pause flusher does not write them a second time.
        taken = {id(f) for f in fragments}
        unflushed = [f for f in run.user_row.fragments if id(f) in taken]
        run.user_row.fragments = [f for f in run.user_row.fragments if id(f) not in taken]
        return fragments, unflushed

    async def _lookup(self, turn_id: str) -> DelegationResult | None:
        if self._lookup_result is None:
            return None
        return await asyncio.to_thread(self._lookup_result, turn_id)

    async def _await_result(
        self, pending: _Pending, turn_id: str, deadline: float | None,
    ) -> DelegationResult | None:
        """Wait for the turn's outcome: bus wakes with short re-reads, plus the safety poll.

        Returns ``None`` only when ``deadline`` passes; a ``None`` deadline
        waits until the session task is cancelled.
        """
        result = await self._lookup(turn_id)
        while result is None and (deadline is None or time.monotonic() < deadline):
            wait = _RESULT_POLL_S
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            woke = True
            try:
                await asyncio.wait_for(pending.wake.wait(), timeout=wait)
            except TimeoutError:
                woke = False
            pending.wake.clear()
            result = await self._lookup(turn_id)
            retries = _WAKE_RETRIES if woke else 0
            while result is None and retries > 0:
                await asyncio.sleep(_WAKE_RETRY_S)
                result = await self._lookup(turn_id)
                retries -= 1
        return result

    async def _deliver(
        self, run: _LiveRun, pending: _Pending, kind: DeliveryKind, content: str,
    ) -> None:
        """Send a result only into the session it belongs to (D4, D5)."""
        current = self._run
        if current is not run or run.epoch != pending.epoch or run.session_id != pending.session_id:
            LOGGER.info(
                "gpt_live delegation %s result skipped: session mismatch (kept in memory)",
                pending.delegation_id,
            )
            return
        if not pending.foreground:
            # Superseded: memory.db and the UI keep the result, Live never hears
            # of it. Even a quiet append can shape later speech.
            LOGGER.info(
                "gpt_live delegation %s result withheld from Live (superseded, %d chars)",
                pending.delegation_id, len(content),
            )
            return
        LOGGER.info(
            "gpt_live delegation %s delivered as %s (%d chars)",
            pending.delegation_id, kind, len(content),
        )
        await self._send_append(run, kind, content, delegation_id=pending.delegation_id)
        await self._broadcast("state")

    async def _send_append(
        self, run: _LiveRun, kind: str, content: str, *, delegation_id: str | None,
    ) -> None:
        event_id = await self._send_json(run, {
            "type": f"session.{kind}.append",
            "content": content,
            "delegation_id": delegation_id,
        })
        if event_id is not None:
            run.appends[event_id] = _Append(delegation_id=delegation_id, kind=kind, content=content)

    def _on_ack(self, run: _LiveRun, kind: str, event: dict[str, Any]) -> None:
        """ACKs prove receipt only, never that speech stopped or content was used."""
        client_event_id = str(event.get("client_event_id", ""))
        sent = run.appends.pop(client_event_id, None)
        if sent is not None:
            LOGGER.info(
                "gpt_live ack %s matched delegation %s kind=%s (%d chars)",
                kind, sent.delegation_id, sent.kind, len(sent.content),
            )
            return
        LOGGER.info("gpt_live ack %s for %s %s", kind, client_event_id, event.get("message", ""))

    async def _on_append_error(self, run: _LiveRun, err: dict[str, Any]) -> None:
        """Match an error to a sent append; an over-length append is halved and resent once."""
        sent = run.appends.pop(str(err.get("client_event_id", "")), None)
        if sent is None:
            return
        message = f"{err.get('code', '')} {err.get('message', '')}".lower()
        if "token" in message and not sent.retried and len(sent.content) > 1:
            shorter = _speech_cut(sent.content, budget=len(sent.content) // 2)
            LOGGER.info(
                "gpt_live append for delegation %s over length; resending %d -> %d chars",
                sent.delegation_id, len(sent.content), len(shorter),
            )
            event_id = await self._send_json(run, {
                "type": f"session.{sent.kind}.append",
                "content": shorter,
                "delegation_id": sent.delegation_id,
            })
            if event_id is not None:
                run.appends[event_id] = dataclasses.replace(sent, content=shorter, retried=True)
            return
        LOGGER.warning(
            "gpt_live append for delegation %s rejected: %s", sent.delegation_id, message.strip(),
        )

    async def _flush_row(self, run: _LiveRun, role: str) -> None:
        """Close one speaker's buffered fragments into a memory.db row (D3)."""
        row = run.user_row if role == "user" else run.assistant_row
        if not row.fragments:
            return
        fragments, row.fragments = row.fragments, []
        text = "".join(f.text for f in fragments).strip()
        if not text or self._record is None:
            return
        source = "allen" if role == "user" else "jarvis_live"
        record_id = _row_record_id(run, source, fragments[0])
        try:
            await asyncio.to_thread(self._record, source, text, record_id)
        except Exception:
            LOGGER.exception("gpt_live memory row %s failed", record_id)

    async def _send_loop(self, run: _LiveRun) -> None:
        while True:
            await run.out_ready.wait()
            run.out_ready.clear()
            while run.out:
                chunk = run.out.popleft()
                run.out_bytes -= len(chunk)
                await run.ws.send(json.dumps({
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }))

    def _enqueue_out(self, run: _LiveRun, chunk: bytes) -> None:
        """Loop-thread side of the mic tee: bounded backlog, drop-oldest by clearing."""
        if run.reader_stop.is_set():
            return
        limit = int(self._config.input_backlog_s * self._config.sample_rate_hz * 2)
        if run.out_bytes + len(chunk) > limit:
            run.dropped_backlog_events += 1
            LOGGER.warning(
                "gpt_live input backlog over %.2fs; dropping %d stale bytes",
                self._config.input_backlog_s, run.out_bytes,
            )
            run.out.clear()
            run.out_bytes = 0
        run.out.append(chunk)
        run.out_bytes += len(chunk)
        run.out_ready.set()

    async def _monitor_loop(self, run: _LiveRun) -> None:
        cfg = self._config
        while True:
            await asyncio.sleep(_MONITOR_PERIOD_S)
            now = time.monotonic()
            speaking = run.writer_busy or run.player.bytes_pending() > 0
            hearing = now < run.hearing_until
            if speaking != run.speaking or hearing != run.hearing:
                run.speaking, run.hearing = speaking, hearing
                if speaking:
                    run.last_activity = now
                await self._broadcast("state")
            # D3: a same-speaker pause closes the row. The assistant row waits for
            # playback to drain so the row holds what was actually heard.
            if run.user_row.fragments and now - run.user_row.last_append >= _ROW_GAP_S:
                await self._flush_row(run, "user")
            if (
                run.assistant_row.fragments
                and not speaking
                and now - run.assistant_row.last_append >= _ROW_GAP_S
            ):
                await self._flush_row(run, "assistant")
            if any(p.state == "submitted" and not p.timed_out for p in run.pending.values()):
                run.last_activity = now  # a running lookup keeps the session open, until timeout
            if now - run.last_activity > cfg.idle_close_s:
                self._schedule_auto_stop(run, "idle")
                return
            if now - run.started_monotonic > cfg.max_session_s:
                self._schedule_auto_stop(run, "max_session")
                return

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _reader_main(self, run: _LiveRun, loop: asyncio.AbstractEventLoop) -> None:
        """Mic tee: canonical 16 kHz frames → session rate → ~100 ms chunks on the loop."""
        cfg = self._config
        resampler: _Resampler | None = None
        chunk_bytes = cfg.sample_rate_hz * cfg.input_chunk_ms // 1000 * 2
        pending = bytearray()
        while not run.reader_stop.is_set():
            frame = run.subscription.read(timeout_s=_READER_POLL_S)
            if frame is None:
                continue
            if resampler is None:
                resampler = _Resampler(
                    input_rate_hz=frame.sample_rate_hz, output_rate_hz=cfg.sample_rate_hz,
                )
            if frame.discontinuity_before:
                LOGGER.debug("gpt_live mic discontinuity before frame %d", frame.sequence)
            # Muted: keep the frame clock running with silence. The session timeline
            # advances on input frames and append ACKs stall when frames stop
            # (docs/gpt-live/voice-websockets.md); real microphone content never leaves.
            samples = (
                np.zeros(frame.frame_count, dtype=np.float32)
                if run.mic_muted
                else np.frombuffer(frame.pcm16_mono, dtype="<i2").astype(np.float32) / 32768.0
            )
            out = resampler.feed(samples)
            pending += np.clip(out * 32768.0, -32768, 32767).astype("<i2").tobytes()
            if len(pending) >= chunk_bytes:
                chunk = bytes(pending[:chunk_bytes])
                del pending[:chunk_bytes]
                loop.call_soon_threadsafe(self._enqueue_out, run, chunk)

    def _writer_main(self, run: _LiveRun) -> None:
        """Speaker side: blocking ``player.write`` off the loop; teardown cancels in flight."""
        while True:
            try:
                item = run.play.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            if run.cancel_write.is_set():
                continue
            run.writer_busy = True
            try:
                run.player.write(item, cancel_event=run.cancel_write, timeout_s=30.0)
            except Exception:
                LOGGER.exception("gpt_live player write failed")
            finally:
                run.writer_busy = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_mutes(self, run: _LiveRun) -> None:
        """A session inherits the switches as they are now, not only future flips."""
        run.player.set_gain(0.0 if self._speech_muted() else 1.0)
        if self._mic_muted():
            run.mic_muted = True
            asyncio.get_running_loop().create_task(
                self._send_json(run, {"type": "session.input_audio.mute"}),
            )

    async def _send_json(self, run: _LiveRun, payload: dict[str, object]) -> str | None:
        """Send one command with a fresh ``event_id``; return it so the ACK can be matched."""
        run.event_seq += 1
        event_id = f"jarvis-{run.event_seq}"
        payload = {**payload, "event_id": event_id}
        try:
            await run.ws.send(json.dumps(payload))
            LOGGER.info("gpt_live sent %s as %s", payload["type"], event_id)
        except Exception as exc:  # noqa: BLE001 - the recv loop owns connection failure
            LOGGER.warning("gpt_live send %s failed: %s", payload.get("type"), exc)
            return None
        return event_id

    def _refused(self, reason: str) -> dict[str, object]:
        LOGGER.warning("gpt_live start refused: %s", reason)
        status = self.status()
        status["reason"] = reason
        return status

    def _notify_owns_speech(self) -> None:
        if self._on_owns_speech is not None:
            with contextlib.suppress(Exception):
                self._on_owns_speech(self.owns_speech)

    async def _broadcast(self, phase: str) -> None:
        with contextlib.suppress(Exception):
            await self._broadcaster.broadcast_op("live", phase=phase, **self.status())


async def _await_started(ws: Any) -> str:  # noqa: ANN401 - websockets connection
    """Read until ``session.started``; an ``error`` before it is a failed start."""
    async for raw in ws:
        event = json.loads(raw)
        kind = event.get("type")
        if kind == "session.started":
            session = event["session"]
            # The resolved configuration is the evidence that format and voice took.
            keys = ("id", "model", "audio", "delegation", "store")
            LOGGER.info(
                "gpt_live session.started %s", json.dumps({k: session.get(k) for k in keys}),
            )
            return str(session["id"])
        if kind == "error":
            err = event.get("error") or {}
            msg = f"session.start rejected: {err.get('code')}: {err.get('message')}"
            raise RuntimeError(msg)
    msg = "websocket closed before session.started"
    raise RuntimeError(msg)


def _pcm16_to_float32(pcm: bytes) -> bytes:
    aligned = len(pcm) - (len(pcm) % 2)
    samples = np.frombuffer(pcm[:aligned], dtype="<i2").astype(np.float32) / 32768.0
    return samples.tobytes()


def _drain_queue(items: queue.Queue[bytes | None]) -> None:
    with contextlib.suppress(queue.Empty):
        while True:
            items.get_nowait()


def _window_start_ms(run: _LiveRun, offset_ms: int) -> int:
    """Where this delegation's request window opens on the session timeline (D2).

    The request is what Allen said since the model last finished speaking: the
    window opens at the last assistant ``end_ms`` before his latest burst of
    speech (fragments closer together than a row gap), never before the previous
    delegation's offset, and ten seconds back while the model has not spoken.
    The anchor is the burst, not the offset, so a barge-in stays whole:
    assistant timestamps run ahead of local playback, and a model that says
    "我来查" before delegating puts its own end_ms after the request.
    """
    fragments = run.user_fragments
    burst_start = offset_ms
    if fragments and offset_ms - fragments[-1].end_ms <= _REQUEST_REACH_MS:
        burst_start = fragments[-1].start_ms
        gap_ms = int(_ROW_GAP_S * 1000)
        for later, earlier in itertools.pairwise(reversed(fragments)):
            if later.start_ms - earlier.end_ms > gap_ms:
                break
            burst_start = earlier.start_ms
    anchor = max(
        (end for end in run.assistant_end_ms if end < burst_start),
        default=offset_ms - _REQUEST_REACH_MS,
    )
    return max(run.last_delegation_offset_ms, anchor)


def _row_record_id(run: _LiveRun, source: str, first: _Fragment) -> str:
    """memory.db id of the row that starts at ``first``; shared by flusher and request (D3)."""
    return f"live:{run.session_id}:{source}:{first.start_ms}"


def _speech_cut(text: str, *, budget: int = _COMMENTARY_BUDGET_CHARS) -> str:
    """Speech-sized cut of a backend answer (D4): whole sentences within ``budget``.

    Returns the text unchanged when it fits.  Otherwise the longest prefix that
    ends at a sentence boundary; when no boundary falls inside the budget the
    answer is not speakable in short form and the caller says so instead.
    """
    text = text.strip()
    if len(text) <= budget:
        return text
    head = text[:budget]
    cut = max(head.rfind(mark) for mark in _SENTENCE_ENDS)
    if cut < 0:
        return LONG_RESULT_COMMENTARY
    return head[: cut + 1].strip()


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "DelegationResult",
    "GptLiveConfig",
    "LiveVoice",
    "gpt_live_config_from_mapping",
]
