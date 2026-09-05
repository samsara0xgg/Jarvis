"""Acceptance for the Inherent v2 sequencer, snapshot and ACK path (ADR-0014 D8/D9/D11/D16).

The fold is exercised directly with plain rows; the sequencer and hub run on
a real ``asyncio`` loop over a real Event Log with fake sockets, so every
cursor asserted here is a genuine ``events.id``.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from jarvis.state.inherent_view import (
    INLINE_DOCUMENT_BUDGET_BYTES,
    RECENT_TERMINAL_GROUP_LIMIT,
    InherentView,
)

# --- fold helpers -----------------------------------------------------------


def _open_payload(response: str, group: str, turn: str, query: str = "q") -> dict[str, Any]:
    return {
        "turn_id": turn,
        "query": query,
        "kind": "text",
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }


def _chunk_payload(
    response: str, group: str, turn: str, sequence: int, text: str,
) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "text": text,
        "response_id": response,
        "response_group_id": group,
        "sequence": sequence,
        "phase": "final",
        "channel": "document",
        "segment_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def _emitted_payload(response: str, group: str, turn: str, text: str) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "text": text,
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }


class _Rows:
    """Feed rows to a fold with monotonically increasing cursors."""

    def __init__(self) -> None:
        self.view = InherentView()
        self.cursor = 0

    def fold(self, event_type: str, payload: dict[str, Any], *, uid: str | None = None) -> Any:  # noqa: ANN401
        self.cursor += 1
        return self.view.fold(
            cursor=self.cursor,
            event_uid=uid or f"uid{self.cursor}",
            event_type=event_type,
            ts_epoch_ms=1_788_200_000_000 + self.cursor,
            payload=payload,
        )

    def turn(self, index: int, *, close: bool = True, text: str = "hello") -> None:
        response, group, turn = f"RESP{index}", f"RGRP{index}", f"T{index}"
        self.fold("surface.response_open", _open_payload(response, group, turn))
        self.fold("surface.response_chunk", _chunk_payload(response, group, turn, 0, text))
        if close:
            self.fold("surface.response_emitted", _emitted_payload(response, group, turn, text))


# --- fold ---------------------------------------------------------------------


def test_fold_maps_each_relevant_row_to_one_transition_at_its_cursor() -> None:
    """Open, chunk and emitted each yield one transition carrying events.id."""
    rows = _Rows()
    opened = rows.fold("surface.response_open", _open_payload("RESP1", "RGRP1", "T1", "why?"))
    skipped = rows.fold("utterance.received", {"turn_id": "T2", "transcript": "x"})
    segment = rows.fold("surface.response_chunk", _chunk_payload("RESP1", "RGRP1", "T1", 0, "a"))
    closed = rows.fold("surface.response_emitted", _emitted_payload("RESP1", "RGRP1", "T1", "a"))

    assert skipped is None
    assert rows.view.through_cursor == 4
    assert (opened.cursor, opened.event_uid) == (1, "uid1")
    assert [change.kind for change in opened.changes] == ["response.opened", "response.delivery"]
    assert opened.changes[0].response.question == "why?"
    assert [change.kind for change in segment.changes] == ["response.segment"]
    assert segment.changes[0].segment.sequence == 0
    assert [change.kind for change in closed.changes] == ["response.delivery"]
    assert closed.changes[0].response.panel_stream == "closed"
    assert closed.changes[0].response.revision == 4


def test_fold_rejects_a_regressing_cursor_and_ignores_rows_without_a_response_id() -> None:
    """D6: cursors never regress; a legacy row without ids is not Inherent-relevant."""
    rows = _Rows()
    legacy = rows.fold("surface.response_open", {"turn_id": "T1", "query": "q", "kind": "text"})

    assert legacy is None
    with pytest.raises(ValueError, match="does not advance"):
        rows.view.fold(
            cursor=1, event_uid="dup", event_type="surface.response_open", ts_epoch_ms=0,
            payload=_open_payload("RESP1", "RGRP1", "T1"),
        )


def test_checkpoint_keeps_every_open_group_and_exactly_twenty_terminal_groups() -> None:
    """D16: 40 terminal groups collapse to the 20 most recent; open groups all stay."""
    rows = _Rows()
    for index in range(40):
        rows.turn(index)
    rows.turn(40, close=False)
    rows.turn(41, close=False)

    checkpoint = rows.view.checkpoint(through_cursor=rows.cursor)

    terminal = [group for group in checkpoint.groups if group.terminal]
    open_groups = [group for group in checkpoint.groups if not group.terminal]
    assert len(terminal) == RECENT_TERMINAL_GROUP_LIMIT == 20
    assert [group.response_group_id for group in terminal] == [f"RGRP{i}" for i in range(20, 40)]
    assert [group.response_group_id for group in open_groups] == ["RGRP40", "RGRP41"]
    assert checkpoint.through_cursor == rows.cursor
    cursors = [group.opened_cursor for group in checkpoint.groups]
    assert cursors == sorted(cursors)


def test_a_closed_body_over_the_inline_budget_becomes_a_preview_plus_reference() -> None:
    """D16: above 16384 UTF-8 bytes the snapshot carries a bounded prefix and the durable ref."""
    rows = _Rows()
    big = "汉" * 6000  # 18000 bytes of UTF-8 in one segment
    rows.fold("surface.response_open", _open_payload("RESP1", "RGRP1", "T1"))
    rows.fold("surface.response_chunk", _chunk_payload("RESP1", "RGRP1", "T1", 0, "lead. "))
    rows.fold("surface.response_chunk", _chunk_payload("RESP1", "RGRP1", "T1", 1, big))
    live = rows.view.checkpoint(through_cursor=rows.cursor).groups[0].responses[0]
    emitted = _emitted_payload("RESP1", "RGRP1", "T1", big)
    rows.fold("surface.response_emitted", emitted, uid="uid-emitted")

    closed = rows.view.checkpoint(through_cursor=rows.cursor).groups[0].responses[0]

    assert live.document_reference is None
    assert [segment.text for segment in live.segments] == ["lead. ", big]
    assert [segment.text for segment in closed.segments] == ["lead. "]
    assert closed.document_reference is not None
    assert closed.document_reference.response_id == "RESP1"
    assert closed.document_reference.event_uid == "uid-emitted"
    assert closed.document_reference.utf8_bytes == 6 + 18000 > INLINE_DOCUMENT_BUDGET_BYTES


def test_a_single_oversized_first_segment_is_cut_on_a_character_boundary() -> None:
    """The preview is never empty and never splits a multi-byte character."""
    rows = _Rows()
    big = "汉" * 6000
    rows.fold("surface.response_open", _open_payload("RESP1", "RGRP1", "T1"))
    rows.fold("surface.response_chunk", _chunk_payload("RESP1", "RGRP1", "T1", 0, big))
    rows.fold("surface.response_emitted", _emitted_payload("RESP1", "RGRP1", "T1", big))

    closed = rows.view.checkpoint(through_cursor=rows.cursor).groups[0].responses[0]

    (preview,) = closed.segments
    assert preview.truncated is True
    assert len(preview.text.encode()) == INLINE_DOCUMENT_BUDGET_BYTES // 3 * 3 == 16383
    assert preview.segment_hash == hashlib.sha256(big.encode()).hexdigest()
