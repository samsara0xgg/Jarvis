"""The web as a test scripts it: every httpx request answered by a function, every one kept.

Code that calls ``httpx.post`` or a ``Client`` reaches the network through one transport class.
:func:`fake_web` replaces what that transport does with a function of the request, so a test
asserts on what a provider was sent and decides what it answers; no socket is opened and nothing
waits. A request body is read before the function sees it, so ``request.content`` is the bytes
the caller sent.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

type Answer = Callable[[httpx.Request], httpx.Response]


@contextlib.contextmanager
def fake_web(answer: Answer) -> Iterator[list[httpx.Request]]:
    """Answer every httpx request with ``answer(request)``; yields the requests seen, in order."""
    seen: list[httpx.Request] = []

    def handle(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        reply = answer(request)
        reply.request = request
        return reply

    with patch.object(httpx.HTTPTransport, "handle_request", handle):
        yield seen


def json_reply(payload: Mapping[str, Any], status: int = 200) -> httpx.Response:
    """A JSON answer."""
    return httpx.Response(status, json=dict(payload))


def page_reply(
    text: str, status: int = 200, *, content_type: str = "text/html; charset=utf-8",
    location: str | None = None,
) -> httpx.Response:
    """A page: ``text`` under ``content_type``; ``location`` makes it a redirect."""
    headers = {"content-type": content_type, **({"location": location} if location else {})}
    return httpx.Response(status, content=text.encode(), headers=headers)


def body_of(request: httpx.Request) -> dict[str, Any]:
    """The JSON object a request carried."""
    decoded = json.loads(request.content)
    assert isinstance(decoded, dict)
    return decoded
