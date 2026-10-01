"""ADR 0112: the heard text of the playing answer reaches the companion as the ``playback`` op."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from jarvis.surface.heard_feed import MIN_INTERVAL_S, HeardFeed
from jarvis.surface.inherent_output import InherentBroadcaster


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Sink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def broadcast_op_sync(self, op: str, **payload: object) -> None:
        self.sent.append((op, payload))


def _offer(feed: HeardFeed, heard: str, *, response_id: str = "R1", final: bool = False) -> None:
    feed.offer(
        turn_id="T1",
        response_id=response_id,
        playback_generation_id=1,
        heard=heard,
        final=final,
    )


def test_heard_feed_sends_changes_throttled_and_never_repeats() -> None:
    """A change goes out at most ~16 times a second; a repeat (a held answer) sends nothing."""
    clock, sink = _Clock(), _Sink()
    feed = HeardFeed(sink, clock=clock)

    _offer(feed, "")  # nothing heard yet: nothing to say
    _offer(feed, "hello")
    _offer(feed, "hello there")  # inside the throttle window: dropped, offered again next poll
    clock.now += MIN_INTERVAL_S + 0.001
    _offer(feed, "hello there")
    clock.now += 1.0
    _offer(feed, "hello there")  # unchanged (a held answer): silence
    clock.now += 1.0
    _offer(feed, "hello there is")

    assert sink.sent == [
        ("playback", {"turn_id": "T1", "response_id": "R1", "heard": "hello"}),
        ("playback", {"turn_id": "T1", "response_id": "R1", "heard": "hello there"}),
        ("playback", {"turn_id": "T1", "response_id": "R1", "heard": "hello there is"}),
    ]


def test_heard_feed_final_skips_the_throttle_and_a_new_response_restarts() -> None:
    """The terminal snapshot lands at once; the same text of another response is not a repeat."""
    clock, sink = _Clock(), _Sink()
    feed = HeardFeed(sink, clock=clock)

    _offer(feed, "a")
    _offer(feed, "ab", final=True)  # the terminal snapshot is exact, at once
    _offer(feed, "a", response_id="R2", final=True)  # same text, another response: sent

    assert [p["heard"] for _op, p in sink.sent] == ["a", "ab", "a"]
    assert [p["response_id"] for _op, p in sink.sent] == ["R1", "R1", "R2"]


def test_heard_feed_without_a_sender_or_with_a_failing_one_does_nothing() -> None:
    """An older broadcaster, or a send that raises, never reaches the playback owner."""
    _offer(HeardFeed(None), "a")  # older broadcasters have no op bridge

    class _Broken:
        def broadcast_op_sync(self, op: str, **payload: object) -> None:
            raise RuntimeError(op, payload)

    _offer(HeardFeed(_Broken()), "a")


def test_playback_op_reaches_a_client_as_one_envelope() -> None:
    """The real broadcaster wraps the feed's message as one ``playback`` envelope."""

    class _Client:
        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        async def send_json(self, frame: dict[str, Any]) -> None:
            self.frames.append(frame)

    async def _body() -> list[dict[str, Any]]:
        broadcaster = InherentBroadcaster()
        broadcaster.attach_loop(asyncio.get_running_loop())
        client = _Client()
        await broadcaster.register(cast("Any", client))
        feed = HeardFeed(broadcaster)
        _offer(feed, "hello")
        await asyncio.sleep(0.05)
        return client.frames

    assert asyncio.run(_body()) == [
        {"op": "playback", "payload": {"turn_id": "T1", "response_id": "R1", "heard": "hello"}},
    ]
