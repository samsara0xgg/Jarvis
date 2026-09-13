"""GPT-Live full-duplex voice session: mic tee → OpenAI Live WebSocket → local player.

Phase A of ``docs/gpt-live-integration-planning.md``: the model listens and
speaks on its own; Jarvis owns the microphone, the speaker and the session
lifecycle.  Nothing here touches L3: no delegation, no ResponseRun, no TTS
watcher.  The session is billed per second, so every exit path funnels
through :meth:`LiveVoice.stop` and the daemon never opens one on its own.

Wire facts come from the ``openai`` 3.13 ``types/live`` package: one
``session.start`` → ``session.started``; audio both ways as base64 PCM16 at
the startup-selected rate; transcripts as ordered deltas without turn
boundaries; ``session.close`` → terminal ``session.closed`` with cumulative
``usage.seconds``.  There is no server-side "stop speaking" event, so hush is
a local playback gate plus a best-effort instruction.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import dataclasses
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

DEFAULT_INSTRUCTIONS = (
    "You are Jarvis, Allen's private voice assistant. Speak natural, brief, spoken "
    "Mandarin Chinese; switch to English when Allen does. This build has no background "
    "lookup, tools or long-term memory: if asked to look something up, take an action or "
    "recall earlier events, say plainly that the backend is not connected yet, do not "
    "pretend to check, and do not make Allen wait. When interrupted or told to stop, stop "
    "immediately and stay silent until Allen speaks again. Do not repeat yourself; keep "
    "answers short."
)

# Appends are written in the language the model speaks (docs/gpt-live/live-prompting.md).
# Commentary is a fact for the model to say in its own words, not an instruction to it.
DELEGATION_UNSUPPORTED_COMMENTARY = (
    "这一版还没有接后台：查不了资料，做不了操作，也记不住以前的事。"  # noqa: RUF001 — intentional Chinese punctuation.
)

HUSH_INSTRUCTION = (
    "用户要求你停止说话。立刻停下，保持沉默，直到用户再次开口。"  # noqa: RUF001 — intentional Chinese punctuation.
)

LiveState = Literal["idle", "connecting", "active", "closing"]

_HEARING_HOLD_S = 1.5
_MONITOR_PERIOD_S = 0.1
_READER_POLL_S = 0.05


@dataclasses.dataclass(frozen=True)
class GptLiveConfig:
    """``realtime.gpt_live`` block; defaults are the shipped values."""

    enabled: bool = False
    model: str = "gpt-live-1"
    voice: str = "marin"
    sample_rate_hz: int = 24000
    instructions: str = DEFAULT_INSTRUCTIONS
    api_key_env: str = "OPENAI_API_KEY"
    idle_close_s: float = 180.0
    max_session_s: float = 1800.0
    connect_timeout_s: float = 10.0
    close_timeout_s: float = 10.0
    input_backlog_s: float = 0.5
    input_chunk_ms: int = 100
    player_ring_seconds: float = 30.0


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
    output_gate: bool = True
    hush_at_ms: int = 0
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
    ) -> None:
        """Bind the daemon-owned pieces; nothing connects until :meth:`start`."""
        self._config = config
        self._broadcaster = broadcaster
        self._ingress = ingress
        self._mic_muted = mic_muted
        self._speech_muted = speech_muted
        self._output_device = output_device
        self._on_owns_speech = on_owns_speech
        self._lock = asyncio.Lock()
        self._state: LiveState = "idle"
        self._epoch = 0
        self._run: _LiveRun | None = None
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
            "hushed": False,
            "speaking": False,
            "hearing": False,
            "mic_muted": self._mic_muted(),
            "speech_muted": self._speech_muted(),
            "dropped_input_events": 0,
            "error": None,
            "notice": None,
        }
        if run is None:
            # Keep the last session's accounting visible after it closed.
            for key in ("usage_s", "usage_final", "reason", "error", "session_id"):
                status[key] = self._last_status.get(key)
            status["usage_final"] = bool(status["usage_final"])
            return status
        status.update(
            session_id=run.session_id,
            elapsed_s=round(time.monotonic() - run.started_monotonic, 1),
            usage_s=run.usage_s,
            usage_final=run.usage_final,
            reason=run.close_reason,
            hushed=not run.output_gate,
            speaking=run.speaking,
            hearing=run.hearing,
            dropped_input_events=run.dropped_backlog_events,
            error=run.last_error,
            notice=run.notice,
        )
        return status

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

    async def hush(self) -> dict[str, object]:
        """Stop the speaker now; playback stays gated until the user speaks again."""
        async with self._lock:
            run = self._run
            if run is None or self._state != "active":
                return self.status()
            run.output_gate = False
            run.hush_at_ms = run.max_user_end_ms
            run.cancel_write.set()
            _drain_queue(run.play)
            run.player.flush()
            await self._send_json(run, {
                "type": "session.instructions.append",
                "content": HUSH_INSTRUCTION,
                "delegation_id": None,
            })
            # buffered_ms: received audio not yet played, i.e. how far the assistant
            # transcript was ahead of the speaker at this moment.
            LOGGER.info(
                "gpt_live hushed at %d ms (last user speech), buffered_ms=%d",
                run.hush_at_ms,
                run.player.bytes_pending() // 4 * 1000 // self._config.sample_rate_hz,
            )
            await self._broadcast("state")
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
        try:
            player.start()
            subscription = ingress.subscribe(
                name="gpt_live", purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
            )
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
                    "audio": {
                        "format": {"type": "audio/pcm", "rate": cfg.sample_rate_hz},
                        "output": {"voice": cfg.voice},
                    },
                    "store": False,
                },
            }))
            session_id = await asyncio.wait_for(
                _await_started(ws), timeout=cfg.connect_timeout_s,
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
            "gpt_live session %s closed: reason=%s usage_s=%s final=%s dropped_input=%d",
            run.session_id, run.close_reason, run.usage_s, run.usage_final,
            run.dropped_backlog_events,
        )
        self._last_status = {
            "session_id": run.session_id,
            "usage_s": run.usage_s,
            "usage_final": run.usage_final,
            "reason": run.close_reason,
            "error": run.last_error,
        }
        self._run = None
        self._state = "idle"
        self._notify_owns_speech()
        await self._broadcast("closed")

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
                    if run.output_gate:
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
                    LOGGER.warning("gpt_live error: %s", run.last_error)
                    await self._broadcast("error")
                elif kind == "session.closed":
                    run.usage_s = float(event["usage"]["seconds"])
                    run.usage_final = True
                    if run.close_reason is None:
                        run.close_reason = str(event.get("reason"))
                    run.closed.set()
                    if self._state == "active":
                        self._schedule_auto_stop(run, run.close_reason)
                    return
                elif kind == "info" or str(kind).endswith(("muted", "appended", "updated")):
                    # ACKs prove receipt only, never that speech stopped or content was used.
                    LOGGER.info(
                        "gpt_live ack %s for %s %s", kind, event.get("client_event_id"),
                        event.get("message", ""),
                    )
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
        if role == "user":
            run.max_user_end_ms = max(run.max_user_end_ms, end_ms)
            run.hearing_until = now + _HEARING_HOLD_S
            if not run.output_gate and start_ms >= run.hush_at_ms:
                # The user spoke after the hush: that is the only thing that reopens playback.
                # "After" is judged on user timestamps only; gating on the assistant's
                # transcript swallowed a reply when the user spoke right after hushing.
                run.output_gate = True
                run.cancel_write.clear()
                LOGGER.info("gpt_live playback reopened by user speech at %d ms", start_ms)
                await self._broadcast("state")
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

    async def _on_delegation(self, run: _LiveRun, event: dict[str, Any]) -> None:
        delegation_id = str((event.get("delegation") or {}).get("id"))
        run.notice = "delegation_unsupported"
        LOGGER.info("gpt_live delegation %s refused: phase A has no backend", delegation_id)
        await self._send_json(run, {
            "type": "session.commentary.append",
            "content": DELEGATION_UNSUPPORTED_COMMENTARY,
            "delegation_id": delegation_id,
        })
        await self._broadcast("state")

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
        """Speaker side: blocking ``player.write`` off the loop; hush cancels in flight."""
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

    async def _send_json(self, run: _LiveRun, payload: dict[str, object]) -> None:
        """Send one command with a fresh ``event_id`` so its ACK can be matched in the log."""
        run.event_seq += 1
        payload = {**payload, "event_id": f"jarvis-{run.event_seq}"}
        try:
            await run.ws.send(json.dumps(payload))
            LOGGER.info("gpt_live sent %s as %s", payload["type"], payload["event_id"])
        except Exception as exc:  # noqa: BLE001 - the recv loop owns connection failure
            LOGGER.warning("gpt_live send %s failed: %s", payload.get("type"), exc)

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
            return str(event["session"]["id"])
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


__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "GptLiveConfig",
    "LiveVoice",
    "gpt_live_config_from_mapping",
]
