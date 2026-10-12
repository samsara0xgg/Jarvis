"""ADR 0226: one state line says how a turn came in, from which device, and how the answer goes.

A spoken turn adds that a word may be misheard; a typed one is exactly what he wrote.
"""

from types import SimpleNamespace

from jarvis.decision import _MISHEARD, _interaction_line
from jarvis.decision.pre_route import TYPED_CHANNEL


def _line(channel: str, device: str | None, *, sent: object | None = object()) -> str | None:
    route = SimpleNamespace(context=SimpleNamespace(route="spoken"), structured=True)
    packet = SimpleNamespace(trigger_event=SimpleNamespace(payload={"channel": channel}))
    ctx = SimpleNamespace(routine_stream=route, record_sent_message=sent, turn_device=device)
    return _interaction_line(packet, ctx)  # type: ignore[arg-type]


def test_a_typed_turn_says_typed_and_shown_on_its_device() -> None:
    """Typed on the phone or a computer: shown there, never read aloud (ADR 0181)."""
    assert _line(TYPED_CHANNEL, "his phone (iphone)") == (
        "This turn: typed on his phone (iphone); your answer is shown there, not read aloud."
    )
    assert _line(TYPED_CHANNEL, "macbook, a computer") == (
        "This turn: typed on macbook, a computer; your answer is shown there, not read aloud."
    )
    assert "misheard" not in (_line(TYPED_CHANNEL, "his phone (iphone)", sent=None) or "")


def test_a_spoken_turn_says_spoken_and_read_aloud_on_its_device() -> None:
    """Spoken to the phone, a computer or the Mac she runs on: read aloud there."""
    assert _line("phone_voice", "his phone (iphone)") == (
        "This turn: spoken to his phone (iphone); your answer is read aloud there.\n" + _MISHEARD
    )
    assert _line("inherent_ptt", "macbook, a computer") == (
        "This turn: spoken to macbook, a computer; your answer is read aloud there.\n" + _MISHEARD
    )
    assert _line("speech", "this Mac") == (
        "This turn: spoken to this Mac; your answer is read aloud there.\n" + _MISHEARD
    )


def test_with_no_device_known_no_device_is_claimed() -> None:
    """A brain's own default row names no device; the line still says spoken or typed."""
    assert _line("speech", None) == "This turn: spoken; your answer is read aloud.\n" + _MISHEARD
    assert _line(TYPED_CHANNEL, None) == (
        "This turn: typed; your answer is shown as text, not read aloud."
    )


def test_the_reply_shape_rides_the_line_when_the_prompt_does_not_carry_it() -> None:
    """Without replay-as-sent the spoken reply note follows the line, typed or spoken."""
    for channel in (TYPED_CHANNEL, "phone_voice"):
        line = _line(channel, "his phone (iphone)", sent=None)
        assert line is not None
        first, note = line.split("\n", 1)
        assert first.startswith("This turn: ")
        assert note


def test_other_channels_keep_their_lines() -> None:
    """A card tap or a resumed plugin is not a spoken or typed turn."""
    assert _line("card", "his phone (iphone)") == "Channel: text"
    assert _line("gpt_live", None) == (
        "This turn: spoken, relayed by Live; your answer is read aloud.\n" + _MISHEARD
    )
