"""A typed turn answered aloud tells the model it was typed (its words are exact, not heard)."""

from types import SimpleNamespace

from jarvis.decision import _interaction_line
from jarvis.decision.pre_route import TYPED_CHANNEL


def _line(channel: str) -> str | None:
    route = SimpleNamespace(context=SimpleNamespace(route="spoken"), structured=True)
    packet = SimpleNamespace(trigger_event=SimpleNamespace(payload={"channel": channel}))
    ctx = SimpleNamespace(routine_stream=route, record_sent_message=object())
    return _interaction_line(packet, ctx)  # type: ignore[arg-type]


def test_a_typed_turn_on_the_spoken_route_says_typed() -> None:
    """The typed turn keeps the voice reply but says its words were typed."""
    assert _line(TYPED_CHANNEL) == "Channel: voice (typed, so these are his exact words)"
    assert _line("speech") == "Channel: voice"
