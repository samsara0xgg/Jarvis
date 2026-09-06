"""Acceptance for the Inherent v2 per-client flow control (ADR-0014 D11 rules 1-5, 9-12).

Every case runs a real Event Log, one sequencer and one hub on a real
``asyncio`` loop.  The slow client is a socket whose ``send_text`` blocks on
an event; the stalled client is one that never ACKs while the hub's injected
clock moves.  No case sleeps for real longer than the settle helper.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from jarvis.runtime.inherent_hub import (
    FlowControlLimits,
    InherentClient,
    inherent_flow_control_limits,
)
from jarvis.runtime.inherent_view_sequencer import (
    CATCH_UP_BYTE_BUDGET,
    CATCH_UP_FRAME_BUDGET,
)
from jarvis.surface.inherent_protocol import ServerEnvelope
from jarvis.surface.inherent_server import V2Session
from tests.integration.test_inherent_sequencer import (
    _ack,
    _chunk_payload,
    _open_payload,
    _Rig,
    _settle,
    _Socket,
)

_D11_DEFAULTS = FlowControlLimits()


class _Clock:
    """The hub's injected wall clock; only :meth:`advance` moves it."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SlowSocket(_Socket):
    """Records every frame it is handed, then blocks in ``send_text`` while held."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.release.set()

    def hold(self) -> None:
        self.release.clear()

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        await self.release.wait()


async def _attach(rig: _Rig, socket: _Socket, connection_id: str) -> InherentClient:
    session = V2Session(
        connection_id=connection_id, send_text=socket.send_text, close=socket.close,
    )
    client = await rig.hub.attach(session)
    await _settle()
    return client


async def _adopt(rig: _Rig, socket: _Socket, connection_id: str) -> InherentClient:
    client = await _attach(rig, socket, connection_id)
    end = socket.of_type("snapshot.end")[0]["payload"]
    await client.on_frame(_ack(connection_id, end["through_cursor"], end["snapshot_id"]))
    await _settle()
    return client


def _ephemeral_frames(socket: _Socket) -> list[dict[str, Any]]:
    return socket.of_type("ephemeral")


async def _big_chunks(rig: _Rig, count: int, size: int, batch: int) -> None:
    """One open response and ``count`` chunks of ``size`` bytes, ``batch`` rows per drain."""
    rig.emit("surface.response_open", _open_payload("RESPbig", "RGRPbig", "Tbig"))
    for sequence in range(count):
        rig.emit(
            "surface.response_chunk",
            _chunk_payload("RESPbig", "RGRPbig", "Tbig", sequence, "x" * size),
        )
        if sequence % batch == batch - 1:
            await _settle(0.01)


# --- rule 3: backpressure closes one client, the other keeps flowing --------------


def test_a_durable_enqueue_past_the_frame_limit_closes_only_the_blocked_client(
    tmp_path: Path,
) -> None:
    """D11 rules 3 and 4 on ``durable_frames``: resync notice, 1008, and A keeps advancing."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            live_socket = _Socket()
            await _adopt(rig, live_socket, "CA")
            slow_socket = _SlowSocket()
            slow = await _adopt(rig, slow_socket, "CB")
            slow_socket.hold()
            handed_before = len(slow_socket.frames)
            turns = 0
            # Bounded so a lost limit fails the case instead of hanging it.
            while not slow.closed and turns * 3 < 2 * _D11_DEFAULTS.durable_frames:
                rig.turn(turns)
                turns += 1
                await _settle(0.01)
            assert slow.closed
            # One frame sat in the blocked send; the lane behind it held the limit.
            assert len(slow_socket.frames) == handed_before + 1
            assert slow.unacked_frames == 1
            assert turns * 3 > _D11_DEFAULTS.durable_frames
            closed_at = live_socket.cursors()[-1]
            slow_socket.release.set()
            await _settle()
            notice = slow_socket.of_type("server.resync_required")
            # Exactly the reason, no `kind`: the client decodes this frame by
            # `message_type` alone.
            assert [frame["payload"] for frame in notice] == [{"reason": "client_backpressure"}]
            assert slow_socket.frames[-1] is notice[0]
            assert slow_socket.close_state() == (1008, "client_backpressure")
            # Rule 4: the other client never noticed.
            rig.turn(turns)
            await _settle()
            cursors = live_socket.cursors()
            assert cursors == sorted(set(cursors))
            assert cursors[-1] > closed_at
            assert live_socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_a_durable_enqueue_past_the_byte_limit_closes_with_the_frame_count_under_its_limit(
    tmp_path: Path,
) -> None:
    """D11 rule 3 on ``durable_bytes``: large chunks trip the byte bound first."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            live_socket = _Socket()
            await _adopt(rig, live_socket, "CA")
            slow_socket = _SlowSocket()
            slow = await _adopt(rig, slow_socket, "CB")
            slow_socket.hold()
            chunk = 60_000
            # Streamed one row per drain, as the render path commits chunks.
            rig.emit("surface.response_open", _open_payload("RESPbig", "RGRPbig", "Tbig"))
            chunks = 0
            while not slow.closed and chunks < _D11_DEFAULTS.durable_frames:
                rig.emit(
                    "surface.response_chunk",
                    _chunk_payload("RESPbig", "RGRPbig", "Tbig", chunks, "x" * chunk),
                )
                chunks += 1
                await _settle(0.01)
            # The byte bound tripped: far fewer frames than durable_frames.
            assert chunks + 1 < _D11_DEFAULTS.durable_frames
            slow_socket.release.set()
            await _settle()
            assert slow_socket.close_state() == (1008, "client_backpressure")
            notice = slow_socket.of_type("server.resync_required")
            assert [f["payload"]["reason"] for f in notice] == ["client_backpressure"]
            closed_at = live_socket.cursors()[-1]
            rig.emit(
                "surface.response_chunk",
                _chunk_payload("RESPbig", "RGRPbig", "Tbig", chunks, "tail"),
            )
            await _settle()
            cursors = live_socket.cursors()
            assert cursors == sorted(set(cursors))
            assert cursors[-1] > closed_at
            assert live_socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- D8 handoff: a plan larger than the lane ---------------------------------------


async def _open_response(rig: _Rig, index: int, size: int) -> None:
    """One still-open response of ``size`` inline bytes; its group gets a page of its own."""
    response, group, turn = f"RESPbig{index}", f"RGRPbig{index}", f"Tbig{index}"
    rig.emit("surface.response_open", _open_payload(response, group, turn))
    rig.emit("surface.response_chunk", _chunk_payload(response, group, turn, 0, "x" * size))
    await _settle(0.01)


async def _over_total_view(rig: _Rig) -> None:
    """Three open responses: over ``durable_bytes`` in total, every page frame under it."""
    for index in range(3):
        await _open_response(rig, index, 400_000)
    await _settle()


def test_a_snapshot_over_the_lane_budget_in_total_is_delivered_and_the_client_adopts(
    tmp_path: Path,
) -> None:
    """D8: the handoff waits for the sender instead of closing on its own burst."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            await _over_total_view(rig)
            socket = _Socket()
            client = await _attach(rig, socket, "CB")
            notice = [f["payload"]["reason"] for f in socket.of_type("server.resync_required")]
            ends = socket.of_type("snapshot.end")
            assert (notice, socket.close_state(), len(ends)) == ([], None, 1)
            begin = socket.of_type("snapshot.begin")[0]["payload"]
            pages = socket.of_type("snapshot.page")
            assert sum(begin["counts"].values()) == len(pages)
            sizes = [len(frame.encode("utf-8")) for frame in socket.raw]
            # The whole delivery outgrew the lane; no single frame did.
            assert sum(sizes) > _D11_DEFAULTS.durable_bytes
            assert max(sizes) < _D11_DEFAULTS.durable_bytes
            end = ends[0]["payload"]
            await client.on_frame(_ack("CB", end["through_cursor"], end["snapshot_id"]))
            await _settle()
            assert socket.of_type("server.resync_required") == []
            assert socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())


def test_a_handoff_to_a_client_that_never_drains_still_closes_at_the_adoption_deadline(
    tmp_path: Path,
) -> None:
    """The capacity wait gets no budget of its own: rule 9's deadline still ends it."""

    async def _body() -> None:
        rig = _Rig(tmp_path, adoption_deadline_s=0.2)
        await rig.start()
        try:
            socket = _SlowSocket()
            await _over_total_view(rig)
            socket.hold()
            await _attach(rig, socket, "CB")
            # 50ms into a 200ms deadline: still waiting, not closed.
            assert socket.close_state() is None
            await _settle(0.6)
            assert socket.close_state() == (1008, "resync_required")
            assert socket.of_type("snapshot.end") == []
        finally:
            socket.release.set()
            await rig.stop()

    asyncio.run(_body())


def test_a_single_frame_larger_than_the_whole_lane_closes_at_once_with_its_own_reason(
    tmp_path: Path,
) -> None:
    """No depth can hold it, so it fails now rather than spending the 5s deadline."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            await _open_response(rig, 0, _D11_DEFAULTS.durable_bytes + 200_000)
            await _settle()
            socket = _Socket()
            # One 50ms settle inside _attach, two orders below the untouched 5s deadline.
            await _attach(rig, socket, "CB")
            notice = [f["payload"] for f in socket.of_type("server.resync_required")]
            assert notice == [{"reason": "frame_over_budget"}]
            assert socket.close_state() == (1008, "frame_over_budget")
            assert socket.of_type("snapshot.end") == []
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- rules 8 and 9: the ACK window ---------------------------------------------------


def test_no_ack_progress_for_ack_stall_s_with_a_non_empty_window_closes_on_injected_time(
    tmp_path: Path,
) -> None:
    """D11 rule 9: five injected seconds, a handful of real milliseconds."""
    started = time.monotonic()

    async def _body() -> None:
        clock = _Clock()
        rig = _Rig(tmp_path, clock=clock, ack_stall_poll_s=0.01)
        await rig.start()
        try:
            live_socket = _Socket()
            live = await _adopt(rig, live_socket, "CA")
            socket = _Socket()
            client = await _adopt(rig, socket, "CB")
            rig.turn(1)
            await _settle()
            await live.on_frame(_ack("CA", live_socket.cursors()[-1]))
            assert live.unacked_frames == 0
            assert client.unacked_frames == 3
            assert client.unacked_bytes == sum(len(r.encode()) for r in socket.raw[-3:])
            clock.advance(_D11_DEFAULTS.ack_stall_s - 0.5)
            await _settle()
            assert socket.close_state() is None
            clock.advance(0.5)
            await _settle()
            assert socket.close_state() == (1008, "ack_stalled")
            assert client.closed
            rig.turn(2)
            await _settle()
            await live.on_frame(_ack("CA", live_socket.cursors()[-1]))
            assert live_socket.close_state() is None
            assert len(live_socket.cursors()) == 6
        finally:
            await rig.stop()

    asyncio.run(_body())
    assert time.monotonic() - started < 2.0


def test_a_client_that_acks_every_25_messages_or_100_ms_is_never_closed(tmp_path: Path) -> None:
    """D11 rules 8 and 9 together: the client cadence keeps the window from stalling."""

    async def _body() -> None:
        clock = _Clock()
        rig = _Rig(tmp_path, clock=clock, ack_stall_poll_s=0.01)
        await rig.start()
        try:
            socket = _Socket()
            client = await _adopt(rig, socket, "CA")
            batch_ms = _D11_DEFAULTS.ack_batch_ms / 1000
            elapsed = 0.0
            turn = 0
            # By count: 25 frames, then an ACK, over and over with no clock movement.
            for _ in range(4):
                for _ in range(_D11_DEFAULTS.ack_batch_messages // 3 + 1):
                    rig.turn(turn)
                    turn += 1
                await _settle()
                assert client.unacked_frames >= _D11_DEFAULTS.ack_batch_messages
                await client.on_frame(_ack("CA", socket.cursors()[-1]))
                assert client.unacked_frames == 0
            # By time: one frame every 100 ms of injected time, ACKed each time.
            while elapsed < 3 * _D11_DEFAULTS.ack_stall_s:
                rig.turn(turn)
                turn += 1
                clock.advance(batch_ms)
                elapsed += batch_ms
                await _settle(0.005)
                await client.on_frame(_ack("CA", socket.cursors()[-1]))
            await _settle()
            await client.on_frame(_ack("CA", socket.cursors()[-1]))
            assert socket.close_state() is None
            assert not client.closed
            assert client.unacked_frames == 0
            assert client.last_acked_cursor == socket.cursors()[-1] == client.last_sent_cursor
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- rule 10: control fairness -----------------------------------------------------------


def test_at_most_eight_consecutive_control_frames_leave_before_one_ready_durable_frame(
    tmp_path: Path,
) -> None:
    """D11 rule 10: with both lanes backed up, control never starves durable truth."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            socket = _SlowSocket()
            client = await _adopt(rig, socket, "CA")
            socket.hold()
            rig.turn(0)
            await _settle()
            held = len(socket.frames)
            # 20 evictions put 20 ephemeral.clear frames on the control lane.
            for index in range(_D11_DEFAULTS.ephemeral_keys + 20):
                client.enqueue_ephemeral(f"k{index}", "connection.notice", {"code": "n"})
            for turn in range(1, 8):
                rig.turn(turn)
            await _settle()
            assert not client.closed
            socket.release.set()
            await _settle()
            after = socket.frames[held:]
            kinds = [
                "control" if f["payload"].get("kind") == "ephemeral.clear"
                else f["delivery_class"]
                for f in after
            ]
            assert kinds.count("control") == 20
            assert kinds.count("durable") == 2 + 7 * 3
            durable_left = kinds.count("durable")
            streak = 0
            for kind in kinds:
                if kind == "control":
                    streak += 1
                    assert not (durable_left and streak > _D11_DEFAULTS.control_fairness)
                else:
                    streak = 0
                    durable_left -= kind == "durable"
            assert kinds[: _D11_DEFAULTS.control_fairness + 1] == ["control"] * 8 + ["durable"]
            # Ephemeral values leave only after every durable frame.
            assert kinds.index("ephemeral") > max(i for i, k in enumerate(kinds) if k == "durable")
            assert kinds.count("ephemeral") == _D11_DEFAULTS.ephemeral_keys
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- rules 2 and 12: the ephemeral coalescing map ----------------------------------------


def test_32_keys_coalesce_in_place_and_the_33rd_clears_the_evicted_key_first(
    tmp_path: Path,
) -> None:
    """D11 rules 2 and 12: latest value per key, ordered clear before a new key's frame."""

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            socket = _SlowSocket()
            client = await _adopt(rig, socket, "CA")
            socket.hold()
            rig.turn(0)
            await _settle()
            keys = [f"utt{i}" for i in range(_D11_DEFAULTS.ephemeral_keys)]
            for revision in (1, 2):
                for key in keys:
                    client.enqueue_ephemeral(
                        key, "input.partial",
                        {"utterance_id": key, "revision": revision, "text": f"r{revision}"},
                    )
            socket.release.set()
            await _settle()
            frames = _ephemeral_frames(socket)
            assert [f["payload"]["utterance_id"] for f in frames] == keys
            assert {f["payload"]["revision"] for f in frames} == {2}
            sequences = [f["ephemeral_sequence"] for f in frames]
            assert sequences == list(range(1, len(keys) + 1))
            for raw in socket.raw:
                ServerEnvelope.model_validate_json(raw)

            client.enqueue_ephemeral(
                "utt-new", "input.partial", {"utterance_id": "utt-new", "revision": 1, "text": "n"},
            )
            await _settle()
            tail = _ephemeral_frames(socket)[len(keys):]
            assert [f["payload"]["kind"] for f in tail] == ["ephemeral.clear", "input.partial"]
            assert tail[0]["payload"] == {"kind": "ephemeral.clear", "key": keys[0]}
            assert tail[1]["payload"]["utterance_id"] == "utt-new"
            assert [f["ephemeral_sequence"] for f in tail] == [len(keys) + 1, len(keys) + 2]
            assert socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- D8 step 6: the catch-up budgets -----------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "chunk"),
    [
        pytest.param(CATCH_UP_FRAME_BUDGET, 8, id="frames"),
        pytest.param(CATCH_UP_BYTE_BUDGET // 60_000 + 1, 60_000, id="bytes"),
    ],
)
def test_a_catch_up_over_either_budget_closes_the_client_with_nothing_partial_sent(
    tmp_path: Path, rows: int, chunk: int,
) -> None:
    """D8 step 6: frame and byte budgets close the same way; no partial catch-up leaves."""
    assert rows * chunk > CATCH_UP_BYTE_BUDGET or rows + 1 > CATCH_UP_FRAME_BUDGET

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            live_socket = _Socket()
            await _adopt(rig, live_socket, "CA")
            socket = _Socket()
            client = await _attach(rig, socket, "CB")
            end = socket.of_type("snapshot.end")[0]["payload"]
            await _big_chunks(rig, count=rows, size=chunk, batch=8)
            await _settle()
            await client.on_frame(_ack("CB", end["through_cursor"], end["snapshot_id"]))
            await _settle()
            assert socket.close_state() == (1008, "catch_up_budget")
            assert socket.cursors() == []
            assert client.closed
            assert len(live_socket.cursors()) == rows + 1
            assert live_socket.close_state() is None
        finally:
            await rig.stop()

    asyncio.run(_body())


# --- the exception guard -------------------------------------------------------------------


def test_an_exception_in_one_snapshot_path_closes_that_client_and_serves_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """One client's fault closes it with 1008; the sequencer keeps serving everyone else."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "presenter exploded"
        raise RuntimeError(msg)

    async def _body() -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            live_socket = _Socket()
            await _adopt(rig, live_socket, "CA")
            monkeypatch.setattr("jarvis.runtime.inherent_hub.build_snapshot_plan", _boom)
            broken_socket = _Socket()
            broken = await _attach(rig, broken_socket, "CB")
            monkeypatch.undo()
            assert broken_socket.close_state() == (1008, "internal_error")
            assert broken.closed
            assert broken_socket.frames == []
            rig.turn(1)
            await _settle()
            assert len(live_socket.cursors()) == 3
            third_socket = _Socket()
            await _adopt(rig, third_socket, "CC")
            assert third_socket.close_state() is None
            rig.turn(2)
            await _settle()
            assert len(third_socket.cursors()) == 3
            assert len(live_socket.cursors()) == 6
        finally:
            await rig.stop()

    with caplog.at_level("ERROR", logger="jarvis.runtime.inherent_hub"):
        asyncio.run(_body())
    assert [r.message for r in caplog.records if "snapshot path" in r.message] == [
        "inherent v2 snapshot path for CB raised; closing",
    ]


# --- configuration ------------------------------------------------------------------------


def test_the_committed_flow_control_block_carries_exactly_the_d11_defaults() -> None:
    """config/jarvis.yaml pins every D11 limit; an absent block or key keeps the default."""
    config = yaml.safe_load(Path("config/jarvis.yaml").read_text(encoding="utf-8"))
    block = config["realtime"]["inherent"]["v2_sequencer"]["flow_control"]
    assert block == {
        "control_frames": 32,
        "durable_frames": 256,
        "durable_bytes": 1048576,
        "ephemeral_keys": 32,
        "ack_batch_messages": 25,
        "ack_batch_ms": 100,
        "ack_stall_s": 5,
        "control_fairness": 8,
    }
    assert inherent_flow_control_limits(config) == FlowControlLimits()
    assert inherent_flow_control_limits({}) == FlowControlLimits()
    partial = {"realtime": {"inherent": {"v2_sequencer": {"flow_control": {"durable_frames": 4}}}}}
    assert inherent_flow_control_limits(partial) == FlowControlLimits(durable_frames=4)
