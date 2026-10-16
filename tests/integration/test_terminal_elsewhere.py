"""ADR 0219: the brain knows whether another device heard a terminal's words, and acts for it.

The brain's end of ``/terminal/ws`` as ``test_terminal_listen`` has it, over a real websocket
with the hooks bound by hand. The question is asked by the brain itself as an utterance arrives
(ADR 0277); ``test_utterance_judged_on_receipt`` has that.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from jarvis.surface.terminal_listen import ASKS
from tests.integration.test_terminal_listen import _ask, _connect, _listening_client

if TYPE_CHECKING:
    from pathlib import Path


def test_the_brain_answers_elsewhere_for_the_terminal_that_asked(tmp_path: Path) -> None:
    """The hook is told which terminal sent the ask; the frame carries no device."""
    seen: list[tuple[str, str, str]] = []

    def heard_elsewhere(device: str, turn_id: str, text: str) -> bool:
        seen.append((device, turn_id, text))
        return device == "ipad"

    client, _hub, _log = _listening_client(tmp_path, heard_elsewhere=heard_elsewhere)
    with ExitStack() as stack:
        mac = _connect(client, tmp_path, "macbook", stack)
        ipad = _connect(client, tmp_path, "ipad", stack)
        _ask(mac, "elsewhere", {"turn_id": "T1", "text": "把灯打开"}, "a" * 32)
        _ask(ipad, "elsewhere", {"turn_id": "T2", "text": "把灯打开"}, "b" * 32)
        first, second = mac.receive_json(), ipad.receive_json()
    assert (first["ok"], first["value"]) == (True, {"elsewhere": False})
    assert (second["ok"], second["value"]) == (True, {"elsewhere": True})
    assert seen == [("macbook", "T1", "把灯打开"), ("ipad", "T2", "把灯打开")]
    assert "elsewhere" in ASKS


def test_a_brain_with_no_hook_or_a_malformed_ask_says_false_or_refuses(tmp_path: Path) -> None:
    """No hook is the neutral answer; text that is not text is refused and the link lives on."""
    client, _hub, _log = _listening_client(tmp_path)
    with ExitStack() as stack:
        ws = _connect(client, tmp_path, "macbook", stack)
        _ask(ws, "elsewhere", {"turn_id": "T1", "text": "把灯打开"}, "a" * 32)
        neutral = ws.receive_json()
        _ask(ws, "elsewhere", {"turn_id": "T1", "text": 7}, "b" * 32)
        refused = ws.receive_json()
        _ask(ws, "elsewhere", {"text": "x"}, "c" * 32)
        no_turn = ws.receive_json()
        _ask(ws, "working", {}, "d" * 32)
        alive = ws.receive_json()
    assert (neutral["ok"], neutral["value"]) == (True, {"elsewhere": False})
    assert (refused["ok"], refused["code"]) == (False, "bad_args")
    assert (no_turn["ok"], no_turn["code"]) == (False, "bad_args")
    assert alive["ok"] is True


def test_interrupt_supersede_and_cancel_runs_are_told_which_terminal_asked(
    tmp_path: Path,
) -> None:
    """The three voice-driven cancels reach their hook with the sender's name; frames unchanged."""
    seen: list[tuple[Any, ...]] = []

    def interrupt(device: str, source: str) -> str:
        seen.append(("interrupt", device, source))
        return "cancelled"

    client, _hub, _log = _listening_client(
        tmp_path,
        interrupt=interrupt,
        supersede=lambda device, turn_id: seen.append(("supersede", device, turn_id)),
        cancel_runs=lambda device: seen.append(("cancel_runs", device)),
        hold_runs=lambda held: seen.append(("hold", held)),
    )
    with ExitStack() as stack:
        ws = _connect(client, tmp_path, "ipad", stack)
        _ask(ws, "interrupt", {"source": "conversation_speech"}, "a" * 32)
        interrupted = ws.receive_json()
        _ask(ws, "supersede", {"turn_id": "T7"}, "b" * 32)
        superseded = ws.receive_json()
        _ask(ws, "cancel_runs", {}, "c" * 32)
        cancelled = ws.receive_json()
        _ask(ws, "hold", {"held": True}, None)  # a tell: global, no device
        _ask(ws, "working", {}, "d" * 32)
        ws.receive_json()
    assert interrupted["value"] == {"outcome": "cancelled"}
    assert superseded["value"] == {}
    assert cancelled["value"] == {}
    assert seen == [
        ("interrupt", "ipad", "conversation_speech"), ("supersede", "ipad", "T7"),
        ("cancel_runs", "ipad"), ("hold", True),
    ]
