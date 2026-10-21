"""A real log, one sequencer and one hub, with a fake v2 socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from jarvis.runtime.inherent_hub import ACK_STALL_POLL_S, InherentClient, InherentHub
from jarvis.runtime.inherent_view_sequencer import InherentViewSequencer
from jarvis.state.event_log import emit_event, open_event_log, read_log_epoch
from jarvis.surface.inherent_protocol import ClientEnvelope
from jarvis.surface.inherent_server import V2Session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.state.committed_event_bus import CommittedEventBus


def open_payload(response: str, group: str, turn: str, query: str = "q") -> dict[str, Any]:
    return {
        "turn_id": turn,
        "query": query,
        "kind": "text",
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }


def chunk_payload(
    response: str,
    group: str,
    turn: str,
    sequence: int,
    text: str,
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


def emitted_payload(response: str, group: str, turn: str, text: str) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "text": text,
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }


BOOT_ID = "Btest00000000000000000000000001"


class Socket:
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


class SequencerRig:
    """A real Event Log, one sequencer and one hub on the running loop."""

    def __init__(  # noqa: PLR0913 — one keyword per injectable the hub and sequencer take.
        self,
        tmp_path: Path,
        *,
        recovery_interval_s: float = 0.01,
        adoption_deadline_s: float = 5.0,
        bus: CommittedEventBus | None = None,
        clock: Callable[[], float] = time.time,
        ack_stall_poll_s: float = ACK_STALL_POLL_S,
    ) -> None:
        self.conn = open_event_log(tmp_path / "mac_events.db")
        self.bus = bus
        self.epoch = read_log_epoch(self.conn)
        self.sequencer = InherentViewSequencer(
            self.conn,
            log_epoch=self.epoch,
            boot_id=BOOT_ID,
            recovery_interval_s=recovery_interval_s,
        )
        self.hub = InherentHub(
            self.sequencer,
            log_epoch=self.epoch,
            boot_id=BOOT_ID,
            adoption_deadline_s=adoption_deadline_s,
            clock=clock,
            ack_stall_poll_s=ack_stall_poll_s,
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
            self.conn,
            type=event_type,
            payload=payload,
            committed_event_bus=self.bus,
        )
        row = self.conn.execute("SELECT id FROM events WHERE event_uid = ?", (event.event_uid,))
        return int(row.fetchone()[0])

    def high_water(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def turn(self, index: int, *, close: bool = True, text: str = "hello") -> list[int]:
        response, group, turn = f"RESP{index}", f"RGRP{index}", f"T{index}"
        ids = [
            self.emit("surface.response_open", open_payload(response, group, turn)),
            self.emit("surface.response_chunk", chunk_payload(response, group, turn, 0, text)),
        ]
        if close:
            ids.append(
                self.emit(
                    "surface.response_emitted",
                    emitted_payload(response, group, turn, text),
                ),
            )
        return ids

    def noise(self, turn: str = "Tnoise") -> int:
        payload = {"turn_id": turn, "transcript": "x", "channel": "voice"}
        return self.emit("utterance.received", payload)

    async def connect(self, connection_id: str = "C1") -> tuple[Socket, InherentClient]:
        socket = Socket()
        session = V2Session(
            connection_id=connection_id,
            send_text=socket.send_text,
            close=socket.close,
        )
        client = await self.hub.attach(session)
        await settle()
        return socket, client

    async def adopt(self, connection_id: str = "C1") -> tuple[Socket, InherentClient, int]:
        """Connect, ACK the snapshot, and return the socket, client and H."""
        socket, client = await self.connect(connection_id)
        end = socket.of_type("snapshot.end")[0]["payload"]
        await client.on_frame(
            ack_envelope(connection_id, end["through_cursor"], end["snapshot_id"])
        )
        await settle()
        return socket, client, int(end["through_cursor"])


async def settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


def ack_envelope(
    connection_id: str, through_cursor: int, snapshot_id: str | None = None
) -> ClientEnvelope:
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
