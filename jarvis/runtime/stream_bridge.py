"""Runtime bridge: drive an async ``LLMStreamHandle`` from a thread with no loop.

``decide()`` runs on a worker thread under ``asyncio.to_thread`` and must never
touch the daemon's event loop. This bridge owns a private loop on the calling
thread, reads the handle there, and cancels the handle on that same loop as
soon as the run's cancellation token is set; the token is the only object
that crosses threads.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jarvis.decision.llm_stream import LLMStreamEvent, LLMStreamHandle, StreamDisposition
    from jarvis.decision.response_run import ResponseCancellationToken

_CANCEL_POLL_S = 0.05


class LoopBoundTokenStream:
    """``SyncTokenStream`` over a private event loop owned by the calling thread."""

    def __init__(
        self,
        handle: LLMStreamHandle,
        *,
        cancellation_token: ResponseCancellationToken,
        cancel_poll_s: float = _CANCEL_POLL_S,
    ) -> None:
        """Bind an unread handle to the run's token; no I/O happens here."""
        self._handle = handle
        self._token = cancellation_token
        self._cancel_poll_s = cancel_poll_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @property
    def disposition(self) -> StreamDisposition | None:
        """Return the handle's settled outcome, ``None`` while it is open."""
        return self._handle.result

    def _loop_for_thread(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop

    async def _cancel_when_token_set(self) -> None:
        # ponytail: 50 ms poll; a token callback would need a new L3 seam.
        while not self._token.is_cancelled:  # noqa: ASYNC110 - a threading.Event set by another thread
            await asyncio.sleep(self._cancel_poll_s)
        await self._handle.cancel(self._token.reason or "cancelled")

    def __iter__(self) -> Iterator[LLMStreamEvent]:
        """Yield typed events; a set token ends the iteration after settling."""
        loop = self._loop_for_thread()
        events = self._handle.events()
        watcher = loop.create_task(self._cancel_when_token_set())
        try:
            while True:
                try:
                    yield loop.run_until_complete(anext(events))
                except StopAsyncIteration:
                    return
        finally:
            watcher.cancel()
            loop.run_until_complete(asyncio.gather(watcher, return_exceptions=True))
            aclose = getattr(events, "aclose", None)
            if callable(aclose):
                loop.run_until_complete(aclose())

    def cancel(self, reason: str) -> None:
        """Settle the handle as cancelled from the owning thread, between events."""
        self._loop_for_thread().run_until_complete(self._handle.cancel(reason))

    def close(self) -> None:
        """Release the handle and the private loop; idempotent."""
        if self._closed:
            return
        self._closed = True
        loop = self._loop_for_thread()
        try:
            loop.run_until_complete(self._handle.aclose())
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
