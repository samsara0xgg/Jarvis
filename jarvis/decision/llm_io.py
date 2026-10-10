"""One resident event loop for OpenAI and Anthropic network I/O, and the clients that live on it.

A stream read on a loop of its own had to open its own client, and so a new
connection and TLS handshake, for every request: 0.44-0.75 s to reach the
model where a reused connection took 0.33-0.38 s (2026-09-30, Mac). Here the
HTTP request runs on one resident loop whose client, one per provider
identity, keeps its connections between requests; the chunks cross back to
the reading loop, so the stream handle, its accounting and its trace context
stay on the thread that reads it. :func:`warm_openai` opens a connection
while Allen is still talking.

Layer rules: stdlib + ``openai`` + ``anthropic`` + ``httpx``; no ``jarvis.*`` import.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

LOGGER = logging.getLogger(__name__)

# How long an idle connection stays open. The SDK default is 5 s, shorter than
# one spoken question, so the connection warmed at its start was gone by its end.
_KEEPALIVE_S: Final[float] = 30.0
# One warm-up per window: every backchannel starts speech too.
_WARM_EVERY_S: Final[float] = 2.0

type _Key = tuple[tuple[str, Any], ...]


class _IoLoop:
    """The loop thread, started on first use, and its per-identity clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: dict[_Key, Any] = {}  # touched on the loop thread only
        self._warmed: dict[_Key, float] = {}

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="jarvis-llm-io", daemon=True).start()
                self._loop = loop
            return self._loop

    def client(
        self, options: Mapping[str, Any], provider: str = "openai",
    ) -> Any:  # noqa: ANN401 — lazy SDK type
        """This identity's client; call on the loop thread only."""
        key = (("provider", provider), *sorted(options.items()))
        client = self._clients.get(key)
        if client is None:
            import httpx  # noqa: PLC0415 — lazy provider construction

            limits = httpx.Limits(
                max_connections=100, max_keepalive_connections=20, keepalive_expiry=_KEEPALIVE_S,
            )
            if provider == "anthropic":
                import anthropic  # noqa: PLC0415

                client = anthropic.AsyncAnthropic(
                    **options, http_client=anthropic.DefaultAsyncHttpxClient(limits=limits),
                )
            else:
                import openai  # noqa: PLC0415

                client = openai.AsyncOpenAI(
                    **options, http_client=openai.DefaultAsyncHttpxClient(limits=limits),
                )
            self._clients[key] = client
        return client

    def warm(self, options: Mapping[str, Any], model: str, provider: str = "openai") -> None:
        key = (("provider", provider), *sorted(options.items()))
        now = time.monotonic()
        with self._lock:
            if now - self._warmed.get(key, -_WARM_EVERY_S) < _WARM_EVERY_S:
                return
            self._warmed[key] = now

        async def _open() -> None:
            await self.client(options, provider).models.retrieve(model)

        def _done(future: Any) -> None:  # noqa: ANN401 — concurrent.futures.Future
            if not future.cancelled() and future.exception() is not None:
                LOGGER.debug("LLM connection warm-up failed", exc_info=future.exception())

        asyncio.run_coroutine_threadsafe(_open(), self.loop()).add_done_callback(_done)


_IO: Final[_IoLoop] = _IoLoop()


async def _pump(
    options: Mapping[str, Any], body: Mapping[str, Any], *, responses: bool,
    put: Callable[[tuple[str, Any]], None], provider: str = "openai",
) -> None:
    """On the resident loop: run one request and hand every chunk to ``put``."""
    try:
        client = _IO.client(options, provider)
        if provider == "anthropic":
            response = await client.messages.create(**body)
        else:
            response = await (
                client.responses.create(**body)
                if responses
                else client.chat.completions.create(**body)
            )
        async with response:
            async for chunk in response:
                put(("chunk", chunk.model_dump(exclude_none=True)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised on the reading side, where it belongs.
        put(("error", exc))
    else:
        put(("end", None))


async def stream_openai(
    options: Mapping[str, Any], body: Mapping[str, Any], *, responses: bool,
    provider: str = "openai",
) -> AsyncIterator[Mapping[str, Any]]:
    """One stream's chunks (``provider``'s), requested on the resident loop, read on this one.

    Closing or cancelling this iterator cancels the request on the resident
    loop, which drops its connection instead of returning it half-read.
    """
    here = asyncio.get_running_loop()
    arrived: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def put(item: tuple[str, Any]) -> None:
        with contextlib.suppress(RuntimeError):  # the reading loop already closed
            here.call_soon_threadsafe(arrived.put_nowait, item)

    request = asyncio.run_coroutine_threadsafe(
        _pump(options, body, responses=responses, put=put, provider=provider), _IO.loop(),
    )
    try:
        while True:
            kind, value = await arrived.get()
            if kind == "end":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        request.cancel()


def warm_openai(options: Mapping[str, Any], model: str, provider: str = "openai") -> None:
    """Open a connection for ``options`` on the resident loop; never blocks, never raises."""
    _IO.warm(options, model, provider)


__all__ = ["stream_openai", "warm_openai"]
