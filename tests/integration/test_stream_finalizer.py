"""ADR-0008 D3: the spoken prefix is durable, immutable, and finalized byte-for-byte."""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING

from jarvis.state.event_log import open_event_log
from jarvis.state.stream_emission import committed_text_prefix
from tests.integration.test_stream_emission_gate import _context, _Run

if TYPE_CHECKING:
    from pathlib import Path

_SEGMENTS = ("冰从周围吸收热量。", "这些热量来自空气。")
_TAIL = "所以冰会变成水。"


def _commit_prefix(run: _Run, segments: tuple[str, ...]) -> str:
    """Gate and expose ``segments`` in order; return the text now spoken."""
    prefix = ""
    for sequence, text in enumerate(segments):
        outcome = run.gate(text, sequence=sequence)
        assert outcome.permit is not None
        run.emit(outcome.permit, text=text)
        prefix += text
    return prefix


def test_restart_reconstructs_prefix_from_event_log_equal_to_memory(tmp_path: Path) -> None:
    """After a crash only exposed chunks are prefix; a permit alone never is."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix = _commit_prefix(run, _SEGMENTS)
        dangling = run.gate(_TAIL, sequence=len(_SEGMENTS))
        assert dangling.permit is not None
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as restarted:
        durable = committed_text_prefix(restarted, run.run.response_id)
    assert durable.text == prefix
    assert durable.next_segment_sequence == len(_SEGMENTS)
    assert durable.policy_hash == run.policy.policy_hash
    assert durable.prefix_hash == hashlib.sha256(prefix.encode()).hexdigest()
