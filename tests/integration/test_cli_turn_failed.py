"""A forwarded turn the daemon ends with `failed` makes the CLI exit non-zero with a reason.

The daemon's `turn.failed` reaches the v1 socket as `{"op": "failed", "payload": {turn_id,
reason, message}}` and no `open` ever follows. The CLI used to ignore it and either wait for
its timeout or print an empty answer with exit 0.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from jarvis import cli
from jarvis.cli import _collect_response, _forward

if TYPE_CHECKING:
    from collections.abc import Mapping

FAILED = {
    "op": "failed",
    "payload": {"turn_id": "T1", "reason": "missing_key", "message": "no model key"},
}
OPEN = {"op": "open", "payload": {"turn_id": "T1", "q": "hi"}}
APPEND = {"op": "append", "payload": {"turn_id": "T1", "token": "hello"}}
DONE = {"op": "done", "payload": {"turn_id": "T1"}}


def _run(envelopes: list[Mapping[str, object]], monkeypatch: pytest.MonkeyPatch) -> int:
    async def attempt(utterance: str, **_kw: object) -> str:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        for envelope in envelopes:
            queue.put_nowait(dict(envelope))
        return await _collect_response(queue, utterance=utterance, turn_id="T1", timeout_s=2)

    monkeypatch.setattr(cli, "_forward_attempt", attempt)
    return asyncio.run(_forward("hi", timeout_s=2, key="k"))


@pytest.mark.parametrize(
    "envelopes",
    [[FAILED], [OPEN, APPEND, FAILED]],
    ids=["before-any-answer", "mid-answer"],
)
def test_a_failed_turn_exits_5_with_the_reason_and_no_stdout(
    envelopes: list[Mapping[str, object]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 5, `the turn failed: <message> (<reason>)` on stderr, nothing on stdout."""
    assert _run(envelopes, monkeypatch) == 5
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "jarvis: the turn failed: no model key (missing_key)\n"


def test_another_turns_failure_and_a_good_answer_are_untouched(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `failed` for some other turn is not ours; our answer still prints with exit 0."""
    other = {"op": "failed", "payload": {"turn_id": "T2", "reason": "x", "message": "y"}}
    assert _run([other, OPEN, APPEND, DONE], monkeypatch) == 0
    assert capsys.readouterr() == ("hello\n", "")
