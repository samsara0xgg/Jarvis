"""ADR 0400: a voice terminal's words are heard by Qwen in the cloud, through the brain.

The terminal keeps the microphone, wake, VAD and endpointing. From speech onset it streams the
utterance's audio to the brain, which streams it on to Qwen's streaming recognizer and holds the
key; the words come back while the owner speaks, and the final ones when the terminal says the
utterance ended. Both ends live here: :class:`BrainHearing` with :class:`QwenEars` on the brain,
:class:`RemoteHearing` and :class:`CloudFinalRecognizer` on the terminal. The frames ride the
``/terminal/ws`` link of :mod:`jarvis.surface.terminal_link`, beside ``tts`` and ``ask``::

    terminal -> brain   {"type": "hear", "sid": "<32 hex>", "op": "open", "terms": ["Jarvis", ...]}
    terminal -> brain   {"type": "hear", "sid": "...", "op": "audio", "pcm": "<base64>"}
    terminal -> brain   {"type": "hear", "sid": "...", "op": "finish"}
    terminal -> brain   {"type": "hear", "sid": "...", "op": "abort"}
    brain -> terminal   {"type": "hear", "sid": "...", "op": "text", "text": "..."}
    brain -> terminal   {"type": "hear", "sid": "...", "op": "done", "text": "..."}
    brain -> terminal   {"type": "hear", "sid": "...", "op": "failed", "error": {...}}

``pcm`` is 16 kHz mono int16. ``text`` is every word heard so far, sent each time it changes;
``done`` carries the final words after a ``finish``. The words are written as said: fillers and
repeats stay, and no model polishes them before the turn's model reads them.

Qwen writes its final words only after ``finish``, and its words at the end of the audio matched
them in 12 of 30 clips (2026-10-23), so the commit waits for them; finishing at each pause
instead lost words and was slower at p90 (ADR 0400).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import json
import logging
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.surface.terminal_voice import HEX_ID, error_body

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from jarvis.surface.terminal_speaker import VoiceLink
    from jarvis.surface.voice_asr import SenseVoiceRecognizer, TranscriptionResult

LOGGER = logging.getLogger("jarvis.surface.terminal_hearing")

KEY_ENV: Final = "BAILIAN_API_KEY"
URL: Final = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
"""Model Studio, international (Singapore): the key must come from the same region."""
MODEL: Final = "qwen-audio-3.1-asr-flash-streaming"
"""The streaming model, not the voice-input one: that one drops fillers and once dropped a name
the owner said (「那个 Claude Code」, 2026-10-23 replay)."""
LANGUAGES: Final = ("zh", "en")
TERM_WEIGHT: Final = 4
MAX_TERMS: Final = 500
MAX_SESSIONS: Final = 4
"""Streams one terminal may hold: the utterance in progress and the ones still being finished."""
FRAME_BYTES: Final = 3200
"""100 ms of audio: what the terminal sends in one ``audio`` frame."""
FINISH_TIMEOUT_S: Final = 4.0
"""How long the commit waits for Qwen's last words before SenseVoice's stand: the median is about
0.7 s after the end of the audio (Yana's 100 streamed clips, 2026-10-19)."""
_IDLE_KEPT: Final = 2
_MAX_PCM_CHARS: Final = 64 * 1024
_SHORT_TERM: Final = re.compile(r"[A-Za-z]{1,2}")


class HearingError(RuntimeError):
    """Qwen refused or failed a stream, or the brain could not run it."""


class _Socket(Protocol):
    """The upstream connection: a websockets client connection, or a test's stand-in."""

    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


type Connect = Callable[[], Awaitable[_Socket]]


def qwen_terms(terms: Sequence[str]) -> list[str]:
    """The owner's word list as Qwen's hot words: deduplicated, at most ``MAX_TERMS``.

    A one- or two-letter Latin term is left out: Yana learned 「pi」 and 「xu」 from his edits,
    and 「pi」 replaced 「Jarvis」 in a 2026-10-23 replay.
    """
    kept = dict.fromkeys(t.strip() for t in terms)
    return [t for t in kept if t and not _SHORT_TERM.fullmatch(t)][:MAX_TERMS]


class QwenEars:
    """Qwen's streaming recognizer on the brain, one task per utterance.

    A connection is kept open between utterances: a new one took 0.66 s to connect against
    0.2 s to start a task on an open one, and an open one still took a task after 60 s idle
    (2026-10-23). A kept connection that fails before its task starts is replaced once.
    """

    def __init__(self, key: str, *, connect: Connect | None = None) -> None:
        """Hear with ``key``; ``connect`` opens one upstream connection (tests pass their own)."""
        self._key = key
        self._connect = connect or self._open
        self._idle: list[_Socket] = []

    async def _open(self) -> _Socket:
        from websockets.asyncio.client import connect  # noqa: PLC0415 — only a brain needs it.

        socket: _Socket = await connect(
            URL, additional_headers={"Authorization": f"Bearer {self._key}"}, max_size=None,
        )
        return socket

    async def warm(self) -> None:
        """Have one connection open before the first utterance needs it."""
        if not self._idle:
            self._idle.append(await self._connect())

    async def hear(
        self,
        audio: asyncio.Queue[bytes | None],
        terms: Sequence[str],
        on_text: Callable[[str], Awaitable[None]],
    ) -> str:
        """The words of the audio ``audio`` yields until ``None``; ``on_text`` gets them so far.

        Raises:
            HearingError: Qwen refused the task or failed it.
        """
        kept = self._idle.pop() if self._idle else None
        if kept is not None:
            try:
                started = await self._start(kept, terms)
            except Exception as exc:  # noqa: BLE001 — a stale connection, whatever it raised.
                LOGGER.info("a kept Qwen connection failed to start a task (%s); opening one",
                            type(exc).__name__)
                _close_later(kept)
                kept = None
            else:
                return await self._finish_task(kept, started, audio, on_text)
        socket = await self._connect()
        return await self._finish_task(socket, await self._start(socket, terms), audio, on_text)

    async def _start(self, socket: _Socket, terms: Sequence[str]) -> str:
        task_id = uuid.uuid4().hex
        parameters: dict[str, Any] = {
            "format": "pcm", "sample_rate": 16_000, "language_hints": list(LANGUAGES),
        }
        if terms:
            parameters["vocabulary"] = dict.fromkeys(terms, TERM_WEIGHT)
        await socket.send(json.dumps({
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": MODEL, "parameters": parameters, "input": {},
            },
        }))
        while True:
            event, _ = _event(await socket.recv())
            if event == "task-started":
                return task_id

    async def _finish_task(
        self, socket: _Socket, task_id: str, audio: asyncio.Queue[bytes | None],
        on_text: Callable[[str], Awaitable[None]],
    ) -> str:
        sender = asyncio.create_task(_send_audio(socket, task_id, audio))
        sentences: dict[int, str] = {}
        heard = ""
        try:
            while True:
                event, sentence = _event(await socket.recv())
                if event == "task-finished":
                    break
                if sentence is None:
                    continue
                sentences[sentence[0]] = sentence[1]
                words = "".join(sentences[k] for k in sorted(sentences))
                if words != heard:
                    heard = words
                    await on_text(heard)
        except BaseException:
            sender.cancel()
            _close_later(socket)
            raise
        await sender
        if len(self._idle) < _IDLE_KEPT:
            self._idle.append(socket)
        else:
            _close_later(socket)
        return heard


def _close_later(socket: _Socket) -> None:
    """Close ``socket`` without waiting: Qwen took seconds to answer a close (2026-10-23)."""
    asyncio.create_task(socket.close()).add_done_callback(_log_failure)


async def _send_audio(socket: _Socket, task_id: str, audio: asyncio.Queue[bytes | None]) -> None:
    while (chunk := await audio.get()) is not None:
        await socket.send(chunk)
    await socket.send(json.dumps({
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }))


def _event(raw: str | bytes) -> tuple[str, tuple[int, str] | None]:
    """One upstream message: its event, and ``(sentence id, text)`` when it carries words.

    Raises:
        HearingError: the task failed.
    """
    message = json.loads(raw)
    header = message.get("header") or {}
    event = str(header.get("event") or "")
    if event == "task-failed":
        msg = f"{header.get('error_code')}: {header.get('error_message')}"
        raise HearingError(msg)
    if event != "result-generated":
        return event, None
    sentence = ((message.get("payload") or {}).get("output") or {}).get("sentence") or {}
    number = sentence.get("sentence_id")
    if sentence.get("heartbeat") or type(number) is not int or number <= 0:
        return event, None
    return event, (number, str(sentence.get("text") or ""))


@dataclass(eq=False)
class _Stream:
    audio: asyncio.Queue[bytes | None]
    task: asyncio.Task[None] | None = None


class BrainHearing:
    """The brain's half: the streams of every voice terminal, heard by one :class:`QwenEars`.

    Every method runs on the loop that serves the sockets. Frames are handled in the order the
    link reads them, so a stream's audio reaches Qwen in the order it was captured.
    """

    def __init__(self, ears: QwenEars) -> None:
        """Hear every terminal's streams with ``ears``."""
        self._ears = ears
        self._streams: dict[object, dict[str, _Stream]] = {}

    def warm(self) -> None:
        """A voice terminal connected: open the upstream connection its first utterance takes."""
        task = asyncio.create_task(self._ears.warm())
        task.add_done_callback(_log_failure)

    def on_frame(
        self, link: object, send: Callable[[str], Awaitable[None]], frame: Mapping[str, Any],
    ) -> None:
        """One ``hear`` frame from the terminal ``link``; nothing here waits."""
        sid, op = frame.get("sid"), frame.get("op")
        if not isinstance(sid, str) or HEX_ID.fullmatch(sid) is None:
            return
        held = self._streams.setdefault(link, {})
        stream = held.get(sid)
        if op == "open" and stream is None:
            raw = frame.get("terms")
            terms = qwen_terms([t for t in raw if isinstance(t, str)]) if isinstance(
                raw, list,
            ) else []
            if len(held) >= MAX_SESSIONS:
                _post(send, sid, {"op": "failed", "error": {
                    "name": "Refused", "message": f"at most {MAX_SESSIONS} streams per terminal",
                }})
                return
            stream = held[sid] = _Stream(asyncio.Queue())
            stream.task = asyncio.create_task(self._run(link, send, sid, stream, terms))
        elif stream is None:
            return
        elif op == "audio":
            pcm = frame.get("pcm")
            if isinstance(pcm, str) and len(pcm) <= _MAX_PCM_CHARS:
                with contextlib.suppress(ValueError):
                    stream.audio.put_nowait(base64.b64decode(pcm, validate=True))
        elif op == "finish":
            stream.audio.put_nowait(None)
        elif op == "abort" and stream.task is not None:
            stream.task.cancel()

    async def detach(self, link: object) -> None:
        """The terminal ``link`` left: stop every stream it held."""
        tasks = [s.task for s in self._streams.pop(link, {}).values() if s.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    async def _run(
        self, link: object, send: Callable[[str], Awaitable[None]], sid: str, stream: _Stream,
        terms: list[str],
    ) -> None:
        async def tell(text: str) -> None:
            await _send(send, sid, {"op": "text", "text": text})

        try:
            words = await self._ears.hear(stream.audio, terms, tell)
            await _send(send, sid, {"op": "done", "text": words})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the terminal falls back; it needs only why.
            LOGGER.warning("hearing stream failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await _send(send, sid, {"op": "failed", "error": error_body(exc)})
        finally:
            self._streams.get(link, {}).pop(sid, None)


async def _send(send: Callable[[str], Awaitable[None]], sid: str, body: dict[str, Any]) -> None:
    await send(json.dumps({"type": "hear", "sid": sid, **body}, ensure_ascii=False))


def _post(send: Callable[[str], Awaitable[None]], sid: str, body: dict[str, Any]) -> None:
    asyncio.create_task(_send(send, sid, body)).add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        LOGGER.warning("hearing: %s", type(task.exception()).__name__)


# -- the terminal's half ----------------------------------------------------------------------


class RemoteHearing:
    """A terminal's streams to the brain, opened and fed from any thread.

    ``link`` carries the frames and routes the brain's ``hear`` frames to :meth:`receive`.
    """

    def __init__(self, link: VoiceLink) -> None:
        """Hear through ``link``; registers itself for its ``hear`` frames."""
        self._link = link
        self._lock = threading.Lock()
        self._done: dict[str, concurrent.futures.Future[str]] = {}
        link.hearing = self

    @property
    def available(self) -> bool:
        """The brain is connected and hears."""
        return self._link.brain_hears

    def open(self, terms: Sequence[str]) -> str:
        """Start a stream; returns its id. The brain's answer arrives at :meth:`finish`."""
        sid = uuid.uuid4().hex
        with self._lock:
            self._done[sid] = concurrent.futures.Future()
        self._link.post({"type": "hear", "sid": sid, "op": "open", "terms": list(terms)})
        return sid

    def send(self, sid: str, pcm: bytes) -> None:
        """Stream ``pcm`` (16 kHz mono int16) into ``sid``; dropped while the link is down."""
        self._link.post({
            "type": "hear", "sid": sid, "op": "audio",
            "pcm": base64.b64encode(pcm).decode("ascii"),
        })

    def finish(self, sid: str, timeout_s: float) -> str:
        """End ``sid``'s audio and wait for its words.

        Raises:
            HearingError: the brain or Qwen failed the stream, or the link dropped.
            TimeoutError: no words within ``timeout_s``.
        """
        with self._lock:
            done = self._done.get(sid)
        if done is None:
            msg = "no such stream"
            raise HearingError(msg)
        self._link.post({"type": "hear", "sid": sid, "op": "finish"})
        try:
            return done.result(timeout=timeout_s)
        finally:
            self.abort(sid, tell=False)

    def abort(self, sid: str, *, tell: bool = True) -> None:
        """Forget ``sid``; with ``tell`` the brain stops it too."""
        with self._lock:
            self._done.pop(sid, None)
        if tell:
            self._link.post({"type": "hear", "sid": sid, "op": "abort"})

    def receive(self, frame: Mapping[str, Any]) -> None:
        """One ``hear`` frame from the brain."""
        with self._lock:
            done = self._done.get(str(frame.get("sid")))
        if done is None or done.done():
            return
        if frame.get("op") == "done":
            done.set_result(str(frame.get("text") or ""))
        elif frame.get("op") == "failed":
            error = frame.get("error")
            why = error if isinstance(error, dict) else {}
            done.set_exception(HearingError(f"{why.get('name')}: {why.get('message')}"))

    def down(self) -> None:
        """The link dropped: every stream in flight fails."""
        with self._lock:
            pending, self._done = list(self._done.values()), {}
        for done in pending:
            if not done.done():
                done.set_exception(HearingError("the brain link dropped"))


class CloudFinalRecognizer:
    """A voice turn's words from Qwen through the brain; SenseVoice keeps the rest.

    ``realtime.final_asr: cloud`` (ADR 0400). The capture session streams each utterance from
    speech onset (:meth:`stream`) and the commit collects Qwen's words (:meth:`recognize_prepared`).
    SenseVoice still hears every committed utterance, for its language, emotion and audio event,
    the live captions, and the words themselves whenever Qwen's are missing: the brain does not
    hear, the stream failed or timed out, or Qwen wrote nothing. ``asr_engine`` says which.
    """

    def __init__(
        self, *, sensevoice: SenseVoiceRecognizer, hearing: RemoteHearing,
        terms: Callable[[], Sequence[str]],
    ) -> None:
        """Stream through ``hearing`` with ``terms()`` as hot words, read at each utterance."""
        self._sensevoice = sensevoice
        self._hearing = hearing
        self._terms = terms
        self._lock = threading.Lock()
        self._streams: dict[str, tuple[str, bytearray]] = {}

    def stream(self, utterance_id: str, pcm: bytes) -> None:
        """More of ``utterance_id``'s audio, in capture order; called on the capture thread."""
        with self._lock:
            held = self._streams.get(utterance_id)
            if held is None:
                if not self._hearing.available:
                    return
                held = self._streams[utterance_id] = (self._open(), bytearray())
                # An utterance that never committed leaves its stream behind: the oldest go.
                while len(self._streams) > MAX_SESSIONS - 1:
                    stale = next(iter(self._streams))
                    self._hearing.abort(self._streams.pop(stale)[0])
            sid, pending = held
            pending += pcm
            if len(pending) < FRAME_BYTES:
                return
            chunk = bytes(pending)
            pending.clear()
        self._hearing.send(sid, chunk)

    def recognize_prepared(
        self, utterance_id: str, audio_pcm: bytes, speech_s: float,  # noqa: ARG002 — the port's.
    ) -> TranscriptionResult:
        """The committed utterance's words: Qwen's from its stream, else SenseVoice's."""
        with self._lock:
            held = self._streams.pop(utterance_id, None)
        if held is None:
            return self._settle(None, audio_pcm, "qwen_unavailable")
        sid, pending = held
        if pending:
            self._hearing.send(sid, bytes(pending))
        return self._settle(sid, audio_pcm, None)

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """A clip heard whole (PTT, dictation): streamed at once, then collected."""
        if not self._hearing.available:
            return self._settle(None, audio_pcm, "qwen_unavailable")
        sid = self._open()
        for start in range(0, len(audio_pcm), FRAME_BYTES):
            self._hearing.send(sid, audio_pcm[start:start + FRAME_BYTES])
        return self._settle(sid, audio_pcm, None)

    def partial_text(self, audio_pcm: bytes) -> str:
        """SenseVoice's snapshot decode: the captions and the semantic endpoint."""
        return self._sensevoice.partial_text(audio_pcm)

    def prewarm(self) -> None:
        """Load SenseVoice before the microphone opens."""
        self._sensevoice.prewarm()

    def _open(self) -> str:
        return self._hearing.open(qwen_terms(self._terms()))

    def _settle(self, sid: str | None, audio_pcm: bytes, why: str | None) -> TranscriptionResult:
        heard = self._sensevoice.recognize(audio_pcm)
        if sid is None:
            return replace(heard, asr_engine=why, sensevoice_confidence=heard.confidence)
        try:
            words = self._hearing.finish(sid, FINISH_TIMEOUT_S).strip()
        except TimeoutError:
            LOGGER.warning("Qwen's words did not come within %.1f s; SenseVoice's stand",
                           FINISH_TIMEOUT_S)
            return replace(heard, asr_engine="qwen_timeout", sensevoice_confidence=heard.confidence)
        except HearingError as exc:
            LOGGER.warning("Qwen did not hear the utterance (%s); SenseVoice's words stand", exc)
            return replace(heard, asr_engine="qwen_error", sensevoice_confidence=heard.confidence)
        if not words:
            return replace(heard, asr_engine="qwen_empty", sensevoice_confidence=heard.confidence)
        if not unicodedata.category(words[-1]).startswith("P"):
            words += "." if words[-1].isascii() else "。"  # closes it, as SenseVoice's always are
        LOGGER.info("Qwen: chars=%d", len(words))  # never the words (ADR 0067)
        return replace(
            heard, text=words, asr_engine="qwen", sensevoice_confidence=heard.confidence,
        )
