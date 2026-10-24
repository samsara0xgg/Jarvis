"""ADR 0390: the topics she keeps over the conversation, for the chat page's topic list.

- ``GET /inherent/topics`` — ``{"topics": [...]}``, open ones first, each side newest talk
  first. A topic is ``{id, name, state, line, recap, count, last_ms, positions}``: ``state``
  ``open`` or ``done``, ``line`` one sentence on where it stands, ``recap`` what she wrote of it
  (rewritten each night from his words), ``count`` how many places it was talked about and
  ``last_ms`` when last. ``positions`` are those places, one per day, oldest first:
  ``{record_id, seq, ts_ms, by, lines}``, the first line of that day's talk (open the chat there
  with ``GET /inherent/conversation?around=<record_id>``), ``by`` ``him`` when he said any of it
  inside the topic and ``night`` when only the nightly pass filed it, and ``lines`` the record
  ids of that day's lines under the topic, oldest first, to light up.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI

    from jarvis.state.topics import Listed
    from jarvis.surface.inherent_server.deps import InherentDeps


def topics_payload(found: Sequence[Listed]) -> dict[str, Any]:
    """The body of ``GET /inherent/topics`` for the topics as the store lists them."""
    return {
        "topics": [
            {
                "id": one.topic.id,
                "name": one.topic.name,
                "state": one.topic.state,
                "line": one.topic.line,
                "recap": one.topic.recap,
                "count": len(one.places),
                "last_ms": one.last_ms,
                "positions": [
                    {
                        "record_id": place.lines[0].record_id,
                        "seq": place.lines[0].seq,
                        "ts_ms": place.lines[0].ts_ms,
                        "by": place.by,
                        "lines": [line.record_id for line in place.lines],
                    }
                    for place in one.places
                ],
            }
            for one in found
        ],
    }


def register_topics_route(app: FastAPI, deps: InherentDeps) -> None:
    """``GET /inherent/topics``, when ``deps.topics_read`` is wired."""
    if deps.topics_read is None:
        return
    topics_read = deps.topics_read

    @app.get("/inherent/topics")
    async def topics() -> dict[str, Any]:
        """ADR 0390: the topics she keeps, read off the loop thread."""
        return topics_payload(await asyncio.to_thread(topics_read))
