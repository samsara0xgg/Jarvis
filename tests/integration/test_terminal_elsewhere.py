"""ADR 0219: the brain acts for the terminal that asked, and no other.

The brain's end of ``/terminal/ws`` as ``test_terminal_listen`` has it, over a real websocket
with the hooks bound by hand. Whether another device heard the same words is asked by the brain
itself as an utterance arrives (ADR 0277); ``test_utterance_judged_on_receipt`` has that.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from tests.support.terminal_listen import ask_op, connect_listening, listening_client

if TYPE_CHECKING:
    from pathlib import Path


def test_interrupt_and_cancel_runs_are_told_which_terminal_asked(
    tmp_path: Path,
) -> None:
    """The voice-driven cancels reach their hook with the sender's name; frames unchanged."""
    seen: list[tuple[Any, ...]] = []

    def interrupt(device: str, source: str) -> str:
        seen.append(("interrupt", device, source))
        return "cancelled"

    client, _hub, _log = listening_client(
        tmp_path,
        interrupt=interrupt,
        cancel_runs=lambda device: seen.append(("cancel_runs", device)),
        hold_runs=lambda held: seen.append(("hold", held)),
    )
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "ipad", stack)
        ask_op(ws, "interrupt", {"source": "conversation_speech"}, "a" * 32)
        interrupted = ws.receive_json()
        ask_op(ws, "cancel_runs", {}, "c" * 32)
        cancelled = ws.receive_json()
        ask_op(ws, "hold", {"held": True}, None)  # a tell: global, no device
        ask_op(ws, "working", {}, "d" * 32)
        ws.receive_json()
    assert interrupted["value"] == {"outcome": "cancelled"}
    assert cancelled["value"] == {}
    assert seen == [
        ("interrupt", "ipad", "conversation_speech"), ("cancel_runs", "ipad"), ("hold", True),
    ]
