"""Acceptance for the D21 request-to-group correlation through the v2 view.

The chain the panel depends on is
``request_id -> input_event_uid/turn_id -> response_group_id``.  The first two
links are the inbox's; this file proves the last one: a ``view.delta`` whose
``response.opened`` carries the ``source_client_request_id`` of the submission
that started the turn, and a snapshot that says the same thing after a re-fold.

The rig is a real Event Log, one sequencer and one hub with a fake socket —
the same shape ``test_inherent_sequencer.py`` uses, kept local so that file
stays untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from jarvis.runtime.inherent_hub import InherentHub
from jarvis.runtime.inherent_view_sequencer import InherentViewSequencer
from jarvis.state.event_log import emit_event, open_event_log, read_log_epoch
from jarvis.state.inherent_view import (
    PENDING_REQUEST_LIMIT,
    InherentView,
    InherentViewCheckpoint,
)
from jarvis.state.input_submission_inbox import SubmissionKey, submit_text_once
from jarvis.surface.inherent_presenter import response_group_item
from jarvis.surface.inherent_protocol import (
    ClientEnvelope,
    ResponseGroupSnapshotItem,
    ResponseOpened,
    ViewDeltaPayload,
)
from jarvis.surface.inherent_server import V2Session

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_BOOT_ID = "Bcorrelation000000000000000001"


class _Socket:
    """Records every frame the hub sends."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def close(self, code: int, reason: str) -> None:
        _ = (code, reason)

    def of_type(self, message_type: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["message_type"] == message_type]


def _emit_turn(conn: sqlite3.Connection, turn_id: str, *, group: str, response: str) -> None:
    """The three rows one answered turn produces, as the render path emits them."""
    common = {
        "response_id": response,
        "response_group_id": group,
        "turn_id": turn_id,
        "phase": "final",
        "channel": "document",
    }
    emit_event(
        conn,
        type="surface.response_open",
        payload={**common, "query": "hi", "kind": "text"},
    )
    emit_event(
        conn,
        type="surface.response_chunk",
        payload={
            **common,
            "sequence": 0,
            "text": "hello",
            "segment_hash": hashlib.sha256(b"hello").hexdigest(),
        },
    )
    emit_event(conn, type="surface.response_emitted", payload={**common, "text": "hello"})


def test_the_delta_and_the_snapshot_carry_the_submitting_request_id(tmp_path: Path) -> None:
    """One v2 submission, one answered turn: the opened change names the request."""

    async def _body() -> None:
        conn = open_event_log(tmp_path / "mac_events.db")
        receipt = submit_text_once(
            conn,
            key=SubmissionKey("inherent_v2", "I-1", "req-42"),
            transcript="明天下雨吗",
        )
        _emit_turn(conn, receipt.turn_id, group="RGRP1", response="RESP1")

        epoch = read_log_epoch(conn)
        sequencer = InherentViewSequencer(
            conn, log_epoch=epoch, boot_id=_BOOT_ID, recovery_interval_s=0.01,
        )
        hub = InherentHub(sequencer, log_epoch=epoch, boot_id=_BOOT_ID)
        tasks = await sequencer.start(bus=None)
        socket = _Socket()
        try:
            client = await hub.attach(
                V2Session(
                    connection_id="C1", send_text=socket.send_text, close=socket.close,
                ),
            )
            await asyncio.sleep(0.05)
            pages = socket.of_type("snapshot.page")
            assert pages, "the client never received a snapshot page"
            item = ResponseGroupSnapshotItem.model_validate(pages[0]["payload"]["items"][0])
            assert item.source_client_request_id == "req-42"
            assert item.turn_id == receipt.turn_id

            end = socket.of_type("snapshot.end")[0]["payload"]
            await client.on_frame(
                ClientEnvelope(
                    protocol_version=2,
                    message_type="transport.ack",
                    message_id="Rack1",
                    client_instance_id="Ifixture0001",
                    connection_id="C1",
                    sent_at_ms=1_788_200_000_000,
                    payload={
                        "through_cursor": end["through_cursor"],
                        "snapshot_id": end["snapshot_id"],
                    },
                ),
            )
            await asyncio.sleep(0.05)

            # A live turn, submitted after adoption, arrives as a delta.
            second = submit_text_once(
                conn,
                key=SubmissionKey("inherent_v2", "I-1", "req-43"),
                transcript="后天呢",
            )
            _emit_turn(conn, second.turn_id, group="RGRP2", response="RESP2")
            await asyncio.sleep(0.15)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        opened = [
            change
            for frame in socket.of_type("view.delta")
            for change in ViewDeltaPayload.model_validate(frame["payload"]).changes
            if isinstance(change, ResponseOpened)
        ]
        assert len(opened) == 1
        assert opened[0].response_group_id == "RGRP2"
        assert opened[0].turn_id == second.turn_id
        assert opened[0].source_client_request_id == "req-43"
        conn.close()

    asyncio.run(_body())


def test_a_turn_with_no_inbox_submission_carries_null(tmp_path: Path) -> None:
    """A voice or v1 turn opens a group with no request id, not a stale one."""
    conn = open_event_log(tmp_path / "mac_events.db")
    view = InherentView()
    rows = [
        ("surface.response_open", {"response_id": "R1", "response_group_id": "G1",
                                   "turn_id": "T1", "query": "hi"}),
    ]
    for cursor, (event_type, payload) in enumerate(rows, start=1):
        view.fold(
            cursor=cursor,
            event_uid=f"E{cursor}",
            event_type=event_type,
            ts_epoch_ms=1,
            payload=payload,
        )
    checkpoint = view.checkpoint(through_cursor=len(rows))
    assert checkpoint.groups[0].source_client_request_id is None
    assert response_group_item(checkpoint.groups[0])["source_client_request_id"] is None
    conn.close()


def test_the_input_row_stays_irrelevant_and_produces_no_transition() -> None:
    """Recording the mapping must not put an empty delta on the wire."""
    view = InherentView()
    transition = view.fold(
        cursor=1,
        event_uid="E1",
        event_type="surface.user_intent",
        ts_epoch_ms=1,
        payload={"turn_id": "T1", "transcript": "hi", "source_client_request_id": "req-1"},
    )
    assert transition is None
    assert view.checkpoint(through_cursor=1).pending_requests == (("T1", "req-1"),)


def test_a_refold_from_a_checkpoint_stamps_the_same_request_id() -> None:
    """Catch-up must not lose a correlation whose group had not opened yet."""
    view = InherentView()
    view.fold(
        cursor=1,
        event_uid="E1",
        event_type="surface.user_intent",
        ts_epoch_ms=1,
        payload={"turn_id": "T1", "transcript": "hi", "source_client_request_id": "req-1"},
    )
    checkpoint = view.checkpoint(through_cursor=1)

    replay = InherentView.from_checkpoint(checkpoint)
    transition = replay.fold(
        cursor=2,
        event_uid="E2",
        event_type="surface.response_open",
        ts_epoch_ms=2,
        payload={"response_id": "R1", "response_group_id": "G1", "turn_id": "T1"},
    )
    assert transition is not None
    opened = transition.changes[0].response
    assert opened is not None
    assert opened.source_client_request_id == "req-1"
    # The entry is dropped once its group opens.
    assert replay.checkpoint(through_cursor=2).pending_requests == ()


def test_siblings_of_one_group_share_the_request_id() -> None:
    """A speech and a document response of one turn agree, as they do on turn_id."""
    view = InherentView()
    view.fold(
        cursor=1,
        event_uid="E1",
        event_type="surface.user_intent",
        ts_epoch_ms=1,
        payload={"turn_id": "T1", "transcript": "hi", "source_client_request_id": "req-1"},
    )
    for cursor, response_id in ((2, "R1"), (3, "R2")):
        view.fold(
            cursor=cursor,
            event_uid=f"E{cursor}",
            event_type="surface.response_open",
            ts_epoch_ms=cursor,
            payload={"response_id": response_id, "response_group_id": "G1", "turn_id": "T1"},
        )
    group = view.checkpoint(through_cursor=3).groups[0]
    assert [response.source_client_request_id for response in group.responses] == [
        "req-1",
        "req-1",
    ]


def test_the_pending_map_is_bounded() -> None:
    """An input row whose turn never opens a response cannot grow the fold."""
    view = InherentView()
    total = PENDING_REQUEST_LIMIT + 10
    for index in range(total):
        view.fold(
            cursor=index + 1,
            event_uid=f"E{index}",
            event_type="surface.user_intent",
            ts_epoch_ms=1,
            payload={
                "turn_id": f"T{index}",
                "transcript": "hi",
                "source_client_request_id": f"req-{index}",
            },
        )
    pending = view.checkpoint(through_cursor=total).pending_requests
    assert len(pending) == PENDING_REQUEST_LIMIT
    # The oldest entries were dropped, the newest kept.
    assert pending[-1] == (f"T{total - 1}", f"req-{total - 1}")


def test_an_empty_checkpoint_still_decodes_without_the_field() -> None:
    """The added field is optional on the wire: an old snapshot item still decodes."""
    item = ResponseGroupSnapshotItem.model_validate(
        {
            "response_group_id": "G1",
            "turn_id": "T1",
            "question": None,
            "created_at_ms": 1,
            "responses": [],
        },
    )
    assert item.source_client_request_id is None
    assert InherentViewCheckpoint(through_cursor=0, groups=()).pending_requests == ()
