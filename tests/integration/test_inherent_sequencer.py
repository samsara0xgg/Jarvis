"""Acceptance for the Inherent v2 sequencer, snapshot and ACK path (ADR-0014 D8/D9/D11/D16).

The fold is exercised directly with plain rows; the sequencer and hub run on
a real ``asyncio`` loop over a real Event Log with fake sockets, so every
cursor asserted here is a genuine ``events.id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime.inherent_hub import SNAPSHOT_ADOPTION_DEADLINE_S, InherentClient, InherentHub
from jarvis.runtime.inherent_view_sequencer import InherentViewSequencer
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log, read_log_epoch
from jarvis.state.inherent_view import (
    INLINE_DOCUMENT_BUDGET_BYTES,
    RECENT_TERMINAL_GROUP_LIMIT,
    InherentView,
)
from jarvis.surface.inherent_presenter import (
    SNAPSHOT_PAGE_MAX_BYTES,
    SnapshotPlan,
    build_snapshot_plan,
    delta_payload,
)
from jarvis.surface.inherent_protocol import (
    ClientEnvelope,
    ResponseDelivery,
    ResponseGroupSnapshotItem,
    ResponseOpened,
    ResponseSegment,
    ServerEnvelope,
    SnapshotBeginPayload,
    SnapshotEndPayload,
    SnapshotPagePayload,
    ViewDeltaPayload,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

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


# --- presenter -----------------------------------------------------------------


def _protocol_encoder(connection_id: str) -> Callable[[str, str, Mapping[str, Any]], str]:
    """Bind a protocol-class ServerEnvelope encoder the way the hub does."""

    def encode(message_type: str, message_id: str, payload: Mapping[str, Any]) -> str:
        return ServerEnvelope(
            protocol_version=2,
            message_type=message_type,
            message_id=message_id,
            delivery_class="protocol",
            connection_id=connection_id,
            log_epoch="Lfixture0001",
            boot_id="Bfixture0001",
            sent_at_ms=1_788_200_000_000,
            payload=dict(payload),
        ).model_dump_json()

    return encode


def _decode_frames(
    plan: SnapshotPlan,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    begin = json.loads(plan.begin_frame)
    pages = [json.loads(frame) for frame in plan.page_frames]
    end = json.loads(plan.end_frame)
    return begin, pages, end


def test_delta_payload_carries_the_source_uid_and_typed_ordered_changes() -> None:
    """The presenter's delta decodes with the D10 DTOs, kinds in fold order."""
    rows = _Rows()
    opened = rows.fold("surface.response_open", _open_payload("RESP1", "RGRP1", "T1", "why?"))
    segment = rows.fold("surface.response_chunk", _chunk_payload("RESP1", "RGRP1", "T1", 0, "a"))
    closed = rows.fold("surface.response_emitted", _emitted_payload("RESP1", "RGRP1", "T1", "a"))

    decoded = [ViewDeltaPayload.model_validate(delta_payload(t)) for t in (opened, segment, closed)]

    assert [d.source_event_uid for d in decoded] == ["uid1", "uid2", "uid3"]
    assert [[c.kind for c in d.changes] for d in decoded] == [
        ["response.opened", "response.delivery"],
        ["response.segment"],
        ["response.delivery"],
    ]
    first = decoded[0].changes[0]
    assert isinstance(first, ResponseOpened)
    assert (first.question, first.lifecycle, first.revision) == ("why?", "generating", 1)
    assert first.turn_id == "T1"
    delivery = decoded[0].changes[1]
    assert isinstance(delivery, ResponseDelivery)
    assert delivery.panel_stream == "open"
    seg = decoded[1].changes[0]
    assert isinstance(seg, ResponseSegment)
    assert (seg.sequence, seg.text, seg.segment_hash) == (0, "a", hashlib.sha256(b"a").hexdigest())
    last = decoded[2].changes[0]
    assert isinstance(last, ResponseDelivery)
    assert last.panel_stream == "closed"


def test_a_snapshot_larger_than_one_page_splits_into_pages_within_64_kib() -> None:
    """D8: contiguous page_index, counts equal to pages sent, every page frame <= 64 KiB."""
    rows = _Rows()
    for index in range(12):
        rows.turn(index, close=False, text="x" * 12_000)
    checkpoint = rows.view.checkpoint(through_cursor=rows.cursor)

    plan = build_snapshot_plan(
        checkpoint, snapshot_id="Ssnap1", view_schema_version=1, encode=_protocol_encoder("C1"),
    )
    begin, pages, end = _decode_frames(plan)

    assert len(pages) > 1
    assert all(len(frame.encode()) <= SNAPSHOT_PAGE_MAX_BYTES for frame in plan.page_frames)
    assert max(len(frame.encode()) for frame in plan.page_frames) > SNAPSHOT_PAGE_MAX_BYTES // 2
    assert [page["payload"]["page_index"] for page in pages] == list(range(len(pages)))
    assert {page["payload"]["section"] for page in pages} == {"response_groups"}
    assert begin["payload"]["section_order"] == ["response_groups"]
    assert begin["payload"]["counts"] == {"response_groups": len(pages)}
    assert begin["payload"]["through_cursor"] == end["payload"]["through_cursor"] == rows.cursor
    assert begin["delivery_class"] == "protocol"
    assert begin["event_cursor"] is None
    SnapshotBeginPayload.model_validate(begin["payload"])
    SnapshotEndPayload.model_validate(end["payload"])
    items = [
        item
        for page in pages
        for item in SnapshotPagePayload.model_validate(page["payload"]).items
    ]
    assert [ResponseGroupSnapshotItem.model_validate(item).response_group_id for item in items] == [
        f"RGRP{i}" for i in range(12)
    ]


def test_content_hash_covers_the_sent_page_bytes_and_a_mutated_page_fails() -> None:
    """D8: SHA-256 over the exact page frames in section-then-page order; one byte breaks it."""
    rows = _Rows()
    for index in range(6):
        rows.turn(index, text="汉" * 9_000)
    checkpoint = rows.view.checkpoint(through_cursor=rows.cursor)

    plan = build_snapshot_plan(
        checkpoint, snapshot_id="Ssnap2", view_schema_version=1, encode=_protocol_encoder("C1"),
    )

    digest = hashlib.sha256()
    for frame in plan.page_frames:
        digest.update(frame.encode("utf-8"))
    end_hash = json.loads(plan.end_frame)["payload"]["content_hash"]
    assert plan.content_hash == digest.hexdigest() == end_hash
    assert len(plan.page_frames) >= 2
    mutated = [*plan.page_frames]
    mutated[1] = mutated[1].replace("汉", "汗", 1)
    digest = hashlib.sha256()
    for frame in mutated:
        digest.update(frame.encode("utf-8"))
    assert digest.hexdigest() != plan.content_hash


def test_snapshot_item_carries_the_preview_and_reference_for_an_over_budget_body() -> None:
    """D16 on the wire: the item holds the prefix segments plus response_id + event_uid."""
    rows = _Rows()
    rows.turn(0, text="汉" * 6000)
    checkpoint = rows.view.checkpoint(through_cursor=rows.cursor)

    plan = build_snapshot_plan(
        checkpoint, snapshot_id="Ssnap3", view_schema_version=1, encode=_protocol_encoder("C1"),
    )
    _, pages, _ = _decode_frames(plan)
    item = ResponseGroupSnapshotItem.model_validate(pages[0]["payload"]["items"][0])

    (response,) = item.responses
    assert response.document_reference is not None
    assert response.document_reference.response_id == "RESP0"
    assert response.document_reference.event_uid == "uid3"
    assert response.document_reference.utf8_bytes == 18000
    assert len(plan.page_frames[0].encode()) < 18000
    assert response.segments[0].truncated is True


# --- sequencer + hub -----------------------------------------------------------

_BOOT_ID = "Btest00000000000000000000000001"


class _Socket:
    """A fake v2 socket: records every frame the hub sends and any close."""

    def __init__(self) -> None:
        self.raw: list[str] = []
        self.frames: list[dict[str, Any]] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, text: str) -> None:
        self.raw.append(text)
        self.frames.append(json.loads(text))

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def close_state(self) -> tuple[int, str] | None:
        """Read the close through a call so mypy does not pin it after an assert."""
        return self.closed

    def of_type(self, message_type: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["message_type"] == message_type]

    def cursors(self) -> list[int]:
        return [frame["event_cursor"] for frame in self.of_type("view.delta")]


class _Rig:
    """A real Event Log, one sequencer and one hub on the running loop."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        recovery_interval_s: float = 0.01,
        adoption_deadline_s: float = 5.0,
        bus: CommittedEventBus | None = None,
    ) -> None:
        self.conn = open_event_log(tmp_path / "mac_events.db")
        self.bus = bus
        self.epoch = read_log_epoch(self.conn)
        self.sequencer = InherentViewSequencer(
            self.conn,
            log_epoch=self.epoch,
            boot_id=_BOOT_ID,
            recovery_interval_s=recovery_interval_s,
        )
        self.hub = InherentHub(
            self.sequencer,
            log_epoch=self.epoch,
            boot_id=_BOOT_ID,
            adoption_deadline_s=adoption_deadline_s,
        )
        self.tasks: tuple[asyncio.Task[None], ...] = ()

    async def start(self) -> None:
        self.tasks = await self.sequencer.start(bus=self.bus)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.conn.close()

    def emit(self, event_type: str, payload: dict[str, Any]) -> int:
        event = emit_event(
            self.conn, type=event_type, payload=payload, committed_event_bus=self.bus,
        )
        row = self.conn.execute("SELECT id FROM events WHERE event_uid = ?", (event.event_uid,))
        return int(row.fetchone()[0])

    def high_water(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def turn(self, index: int, *, close: bool = True, text: str = "hello") -> list[int]:
        response, group, turn = f"RESP{index}", f"RGRP{index}", f"T{index}"
        ids = [
            self.emit("surface.response_open", _open_payload(response, group, turn)),
            self.emit("surface.response_chunk", _chunk_payload(response, group, turn, 0, text)),
        ]
        if close:
            ids.append(
                self.emit(
                    "surface.response_emitted", _emitted_payload(response, group, turn, text),
                ),
            )
        return ids

    def noise(self, turn: str = "Tnoise") -> int:
        payload = {"turn_id": turn, "transcript": "x", "channel": "voice"}
        return self.emit("utterance.received", payload)

    async def connect(self, connection_id: str = "C1") -> tuple[_Socket, InherentClient]:
        socket = _Socket()
        client = await self.hub.attach(connection_id, socket.send_text, socket.close)
        await _settle()
        return socket, client

    async def adopt(self, connection_id: str = "C1") -> tuple[_Socket, InherentClient, int]:
        """Connect, ACK the snapshot, and return the socket, client and H."""
        socket, client = await self.connect(connection_id)
        end = socket.of_type("snapshot.end")[0]["payload"]
        await client.on_frame(_ack(connection_id, end["through_cursor"], end["snapshot_id"]))
        await _settle()
        return socket, client, int(end["through_cursor"])


def _frontier(client: InherentClient) -> int | None:
    """Read the frontier through a call so mypy does not pin it after an assert."""
    return client.live_frontier


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


def _ack(connection_id: str, through_cursor: int, snapshot_id: str | None = None) -> ClientEnvelope:
    payload: dict[str, Any] = {"through_cursor": through_cursor}
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    return ClientEnvelope(
        protocol_version=2,
        message_type="transport.ack",
        message_id=f"Rack{through_cursor}",
        client_instance_id="Ifixture0001",
        connection_id=connection_id,
        sent_at_ms=1_788_200_000_000,
        payload=payload,
    )


def test_live_deltas_follow_events_id_order_across_interleaved_producers(tmp_path: Path) -> None:
    """D6/D9: one view.delta per relevant row, event_cursor = events.id, ascending."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            socket, _client, high = await rig.adopt()
            a, b = ("RESPa", "RGRPa", "Ta"), ("RESPb", "RGRPb", "Tb")
            ids = [
                rig.emit("surface.response_open", _open_payload(*a)),
                rig.emit("surface.response_open", _open_payload(*b)),
                rig.emit("surface.response_chunk", _chunk_payload(*b, 0, "b0")),
                rig.noise(),
                rig.emit("surface.response_chunk", _chunk_payload(*a, 0, "a0")),
                rig.emit("surface.response_emitted", _emitted_payload(*a, "a0")),
                rig.emit("surface.response_emitted", _emitted_payload(*b, "b0")),
            ]
            await _settle()
            deltas = socket.of_type("view.delta")
            assert socket.cursors() == [i for i in ids if i != ids[3]]
            assert all(cursor > high for cursor in socket.cursors())
            assert [d["delivery_class"] for d in deltas] == ["durable"] * 6
            assert all(d["ephemeral_sequence"] is None for d in deltas)
            assert [d["payload"]["changes"][0]["kind"] for d in deltas] == [
                "response.opened", "response.opened", "response.segment",
                "response.segment", "response.delivery", "response.delivery",
            ]
            assert [d["message_id"] for d in deltas] == [
                d["payload"]["source_event_uid"] for d in deltas
            ]
            assert rig.sequencer.scan_cursor == rig.high_water()
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_a_bus_notification_alone_drains_with_the_recovery_timer_at_sixty_seconds(
    tmp_path: Path,
) -> None:
    """D9: the committed bus wakes the drain; without a wake nothing polls."""

    async def _body() -> None:
        bus = CommittedEventBus()
        rig = _Rig(tmp_path, recovery_interval_s=60.0, bus=bus)
        await rig.start()
        try:
            socket, _client, _high = await rig.adopt()
            rig.bus = None
            unseen = rig.emit("surface.response_open", _open_payload("RESPx", "RGRPx", "Tx"))
            await _settle(0.3)
            assert socket.cursors() == []
            assert rig.sequencer.scan_cursor < unseen
            rig.bus = bus
            chunk = _chunk_payload("RESPx", "RGRPx", "Tx", 0, "x")
            seen = rig.emit("surface.response_chunk", chunk)
            await _settle(0.3)
            assert socket.cursors() == [unseen, seen]
            assert rig.sequencer.scan_cursor == seen
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_a_coalesced_wake_still_advances_the_watermark_across_every_row(tmp_path: Path) -> None:
    """D9 step 3-4: one wake after seven commits projects all and sets scan_cursor = MAX(id)."""

    async def _body() -> None:
        rig = _Rig(tmp_path, recovery_interval_s=60.0)
        await rig.start()
        try:
            socket, _client, _high = await rig.adopt()
            rig.noise("T1")
            rig.noise("T2")
            first = rig.emit("surface.response_open", _open_payload("RESPc", "RGRPc", "Tc"))
            rig.noise("T3")
            chunk = _chunk_payload("RESPc", "RGRPc", "Tc", 0, "c")
            second = rig.emit("surface.response_chunk", chunk)
            last = rig.noise("T4")
            assert rig.sequencer.scan_cursor < first
            rig.sequencer.wake()
            await _settle()
            assert socket.cursors() == [first, second]
            assert rig.sequencer.scan_cursor == last == rig.high_water()
            assert rig.sequencer.wake_pending is False
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_no_cursor_beyond_h_before_ack_then_catch_up_replays_exactly_h_to_b(tmp_path: Path) -> None:
    """D8 steps 4-7: nothing > H before ACK; catch-up is (H, B] ascending; later rows follow."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            rig.turn(0)
            rig.turn(1, close=False)
            socket, client = await rig.connect()
            begin = socket.of_type("snapshot.begin")[0]["payload"]
            high = begin["through_cursor"]
            assert high == rig.high_water()
            assert [frame["message_type"] for frame in socket.frames] == [
                "snapshot.begin", "snapshot.page", "snapshot.end",
            ]
            items = socket.of_type("snapshot.page")[0]["payload"]["items"]
            assert [item["response_group_id"] for item in items] == ["RGRP0", "RGRP1"]
            assert [item["responses"][0]["panel_stream"] for item in items] == ["closed", "open"]

            during = rig.turn(2)
            during.append(rig.noise())
            during += rig.turn(3, close=False)
            await _settle()
            assert socket.cursors() == []
            assert _frontier(client) is None

            end = socket.of_type("snapshot.end")[0]["payload"]
            await client.on_frame(_ack("C1", high, end["snapshot_id"]))
            await _settle()
            expected = [i for i in during if i != during[3]]
            assert socket.cursors() == expected
            assert client.live_frontier == max(during)
            assert client.last_acked_cursor == high

            after = rig.turn(4)
            await _settle()
            assert socket.cursors() == expected + after
            assert all(a < b for a, b in zip(socket.cursors(), socket.cursors()[1:], strict=False))
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_ack_validation_follows_d11_rule_7(tmp_path: Path) -> None:
    """D11 rule 7: ack past last sent, regressing duplicate, and wrong snapshot id or H."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            socket, client, high = await rig.adopt("C1")
            ids = rig.turn(0)
            await _settle()
            assert client.last_sent_cursor == ids[-1]
            await client.on_frame(_ack("C1", ids[-1]))
            assert client.last_acked_cursor == ids[-1]
            await client.on_frame(_ack("C1", ids[0]))
            assert client.last_acked_cursor == ids[-1]
            assert socket.close_state() is None
            await client.on_frame(_ack("C1", ids[-1] + 1))
            assert socket.close_state() == (1002, "protocol_error")
            assert client.closed

            other_socket, other = await rig.connect("C2")
            end = other_socket.of_type("snapshot.end")[0]["payload"]
            await other.on_frame(_ack("C2", end["through_cursor"], "Swrong"))
            assert other_socket.close_state() == (1002, "protocol_error")

            third_socket, third = await rig.connect("C3")
            end = third_socket.of_type("snapshot.end")[0]["payload"]
            await third.on_frame(_ack("C3", end["through_cursor"] - 1, end["snapshot_id"]))
            assert third_socket.close_state() == (1002, "protocol_error")
            assert high < ids[0]
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_no_ack_within_the_adoption_deadline_closes_with_resync_required(tmp_path: Path) -> None:
    """D11 rule 9: the deadline is five seconds in production; a shortened one closes the client."""
    assert SNAPSHOT_ADOPTION_DEADLINE_S == 5.0

    async def _body() -> None:
        rig = _Rig(tmp_path, adoption_deadline_s=0.2)
        await rig.start()
        try:
            socket, client = await rig.connect()
            assert socket.close_state() is None
            await _settle(0.4)
            assert socket.close_state() == (1008, "resync_required")
            assert client.closed
            live_socket, _live, _high = await rig.adopt("C2")
            assert live_socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())
