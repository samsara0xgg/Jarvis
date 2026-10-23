"""The media owner's spare TTS session: connected while Allen talks, taken by the next answer."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared import lang
    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.contract import StreamingTTSProvider
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_tts import TTSSession

LOGGER = logging.getLogger(__name__)

# A session connected while Allen is still talking spares his answer the
# transport and task handshake (TLS alone took 0.11-0.15 s to api-uw on
# 2026-09-29). MiniMax closes a started task after 120 s without an event.
SPARE_SESSION_MAX_AGE_S = 60.0


class SpareSessions:
    """The only writer of the spare session; the actor's thread only (ADR 0378).

    It creates its task on the actor's loop and every call runs there. The owner decides when
    a spare may be made, through ``admitting`` and ``language``, both read where the old gate
    read them; the spare says how it is connected, aged, taken and closed.
    """

    def __init__(
        self,
        *,
        provider: StreamingTTSProvider,
        config: StreamingMediaConfig,
        clock: ShutdownClock,
        admitting: Callable[[], bool],
        language: Callable[[], lang.Language],
    ) -> None:
        """Make spares of ``provider``, closed within ``clock``'s deadline.

        ``admitting`` answers whether the owner is awake and accepting; ``language`` the
        language the next spare reads: that of the owner's last answer, or the system's.
        """
        self._provider = provider
        self._config = config
        self._clock = clock
        self._admitting = admitting
        self._language = language
        # Connected for the next answer while Allen talks; actor-owned.
        self._spare: TTSSession | None = None
        self._spare_language: lang.Language = "en"
        self._spare_ready_at = 0.0
        self._spare_task: asyncio.Task[None] | None = None

    async def prewarm(self) -> None:
        """Allen started talking: have the first session of his answer connected now."""
        if self._spare_task is not None or not self._admitting():
            return
        if self._spare is not None:
            if self._spare_fresh():
                return
            await self._drop_spare()
        # His words are not recognized yet, so the spare reads the language of his
        # last answer (the system language before any); an answer in the other one
        # does not take it.
        language = self._language()
        session = self._provider.create_tts_session(
            endpoint_index=0,
            language=language,
            idle_close_s=self._config.session_idle_close_s,
            command_queue_capacity=self._config.session_command_capacity,
            audio_queue_capacity=self._config.session_audio_capacity,
        )
        self._spare_task = asyncio.create_task(
            self._connect_spare(session, language), name="tts-spare-connect",
        )

    async def _connect_spare(self, session: TTSSession, language: lang.Language) -> None:
        try:
            await session.connect()
        except Exception:  # noqa: BLE001 - a spare that cannot connect is only not used
            LOGGER.debug("spare TTS session did not connect", exc_info=True)
            await self._clock.wait_task_bounded(asyncio.create_task(session.close()), timeout_s=0.5)
            return
        finally:
            self._spare_task = None
        if not self._admitting():
            await self._clock.wait_task_bounded(asyncio.create_task(session.close()), timeout_s=0.5)
            return
        self._spare, self._spare_ready_at = session, asyncio.get_running_loop().time()
        self._spare_language = language
        record_realtime_trace("tts_session_prewarmed", measurement_semantics="spare_connected")

    def _spare_fresh(self) -> bool:
        return asyncio.get_running_loop().time() - self._spare_ready_at <= SPARE_SESSION_MAX_AGE_S

    async def take(self, language: lang.Language) -> TTSSession | None:
        """The connected spare for an answer's first endpoint, if fresh and in its language.

        One still connecting is left to become the next answer's spare.
        """
        if self._spare is not None and self._spare_fresh() and self._spare_language == language:
            spare, self._spare = self._spare, None
            return spare
        await self._drop_spare()
        return None

    async def _drop_spare(self) -> None:
        spare, self._spare = self._spare, None
        if spare is not None:
            await self._clock.wait_task_bounded(asyncio.create_task(spare.close()), timeout_s=0.5)

    async def close(self) -> None:
        """Shutdown and sleep: no spare survives, including one still connecting."""
        if self._spare_task is not None:
            await self._clock.wait_task_bounded(self._spare_task, timeout_s=0.5)
        await self._drop_spare()
