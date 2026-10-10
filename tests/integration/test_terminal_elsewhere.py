"""ADR 0219: a terminal asks the brain whether another device heard its words, and acts for itself.

Both ends of ``/terminal/ws`` as ``test_terminal_listen`` has them: the brain's, over a real
websocket with the hooks bound by hand, and the terminal's, against a brain played by hand.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from jarvis.surface import terminal_listen
from jarvis.surface.terminal_listen import ASKS, LinkedTurn
from tests.integration.test_terminal_listen import (
    _ask,
    _Bench,
    _connect,
    _listening_client,
    _ok,
)

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


def test_a_terminal_reads_the_brains_answer_and_every_failure_as_false(tmp_path: Path) -> None:
    """A brain that knows the op, one that does not, one that errors, and one that says nothing."""
    answers: dict[str, Any] = {
        "yes": lambda _frame: _ok({"elsewhere": True}),
        "no": lambda _frame: _ok({"elsewhere": False}),
        "old brain": lambda _frame: {"ok": False, "code": "unknown_op", "message": "unknown ask"},
        "erroring": lambda _frame: {"ok": False, "code": "brain_error", "message": "OSError"},
        "textless": lambda _frame: _ok({"other": 1}),
        "silent": None,
    }
    got: dict[str, bool] = {}
    for brain_is, answer in answers.items():
        bench = _Bench(tmp_path / brain_is.replace(" ", "_"), answer)
        try:
            started = time.monotonic()
            got[brain_is] = LinkedTurn(bench.link).heard_elsewhere("T1", "把灯打开")
            # A silent brain holds the line only briefly.
            assert time.monotonic() - started < 1.0, brain_is
            if brain_is == "yes":
                [frame] = bench.frames
                assert (frame["op"], frame["args"]) == (
                    "elsewhere", {"turn_id": "T1", "text": "把灯打开"},
                )
        finally:
            bench.close()
    assert got == {
        "yes": True, "no": False, "old brain": False, "erroring": False, "textless": False,
        "silent": False,
    }


def test_a_gone_brain_is_false_and_the_wait_is_short(tmp_path: Path) -> None:
    """The line is not held for a brain that is not there, or one that cannot answer in time."""
    assert terminal_listen.ELSEWHERE_TIMEOUT_S <= 0.3
    bench = _Bench(tmp_path)
    try:
        bench.link.down()
        assert LinkedTurn(bench.link).heard_elsewhere("T1", "把灯打开") is False
    finally:
        bench.close()

