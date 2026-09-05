r"""Daemon-level smoke for the Inherent v2 sequencer (ADR-0014 D8/D9/D11).

Connects to a running daemon's ``/inherent/ws/v2`` with the boot token
under its runtime root, completes the hello, receives and verifies the
paged snapshot, ACKs it, then appends one response turn to that daemon's
Event Log and prints the ``view.delta`` frames that arrive.

With ``--two-clients`` it instead runs the D11 flow-control scenario:
client A completes the handoff and keeps reading and ACKing, client B
completes its handoff and then stops reading its socket and never ACKs.
A burst of large chunk rows follows, and the run prints B's close code and
reason (``client_backpressure`` or ``ack_stalled``) beside A's still
advancing ``view.delta`` cursors.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/smoke_inherent_sequencer.py \\
        --port 8032 --runtime-root /path/to/root [--two-clients]

No LLM is involved: the turn is written directly as
``surface.response_{open,chunk,emitted}`` rows, exactly what the render path
commits, so the sequencer's recovery timer picks it up the same way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from jarvis.state.event_log import emit_event, open_event_log

BURST_CHUNKS = 24
BURST_CHUNK_BYTES = 150_000
CLOSE_DEADLINE_S = 20.0
RESYNC_CLOSE_CODE = 1008


def _out(line: str) -> None:
    """Print one operator-facing line; stdout is this script's interface."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _hello() -> dict[str, Any]:
    return {
        "protocol_version": 2,
        "message_type": "client.hello",
        "message_id": "R" + uuid.uuid4().hex,
        "client_instance_id": "I" + uuid.uuid4().hex,
        "connection_id": None,
        "sent_at_ms": int(time.time() * 1000),
        "payload": {
            "supported_versions": [2],
            "client_build": "smoke_inherent_sequencer",
            "view_schema_versions": [1],
            "capabilities": ["paged_snapshot", "transport_ack"],
            "last_log_epoch": None,
            "last_applied_cursor": None,
            "has_complete_local_state": False,
        },
    }


def _recv(ws: Any, expected: str) -> tuple[str, dict[str, Any]]:  # noqa: ANN401 — sync client connection.
    raw = ws.recv(timeout=10)
    if not isinstance(raw, str):
        msg = f"expected a text frame, got {type(raw).__name__}"
        raise TypeError(msg)
    frame: dict[str, Any] = json.loads(raw)
    if frame["message_type"] != expected:
        msg = f"expected {expected}, got {frame['message_type']}: {raw[:200]}"
        raise RuntimeError(msg)
    return raw, frame


def _emit_turn(event_log: Path, turn_id: str) -> list[str]:
    conn = open_event_log(event_log)
    response, group = "RESP" + uuid.uuid4().hex, "RGRP" + uuid.uuid4().hex
    base = {
        "turn_id": turn_id,
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }
    text = "sequencer smoke"
    uids = []
    try:
        for event_type, extra in (
            ("surface.response_open", {"query": "smoke?", "kind": "text"}),
            (
                "surface.response_chunk",
                {
                    "text": text,
                    "sequence": 0,
                    "segment_hash": hashlib.sha256(text.encode()).hexdigest(),
                },
            ),
            ("surface.response_emitted", {"text": text, "delivered_via": ["inherent"]}),
        ):
            uids.append(emit_event(conn, type=event_type, payload={**base, **extra}).event_uid)
    finally:
        conn.close()
    return uids


def _ack_frame(
    connection_id: str, through_cursor: int, snapshot_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"through_cursor": through_cursor}
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    return {
        "protocol_version": 2,
        "message_type": "transport.ack",
        "message_id": "R" + uuid.uuid4().hex,
        "client_instance_id": "Ismoke",
        "connection_id": connection_id,
        "sent_at_ms": int(time.time() * 1000),
        "payload": payload,
    }


def _handshake(ws: Any, label: str) -> tuple[str, dict[str, Any]]:  # noqa: ANN401 — sync connection.
    """Hello, verify the paged snapshot's hash, ACK it; return the id and end payload."""
    ws.send(json.dumps(_hello()))
    _, hello = _recv(ws, "server.hello")
    connection_id = hello["connection_id"]
    _out(f"[{label}] server.hello connection_id={connection_id} log_epoch={hello['log_epoch']}")
    _, begin = _recv(ws, "snapshot.begin")
    digest = hashlib.sha256()
    for _ in range(begin["payload"]["counts"]["response_groups"]):
        raw, _page = _recv(ws, "snapshot.page")
        digest.update(raw.encode("utf-8"))
    _, end = _recv(ws, "snapshot.end")
    payload = end["payload"]
    verified = "ok" if digest.hexdigest() == payload["content_hash"] else "MISMATCH"
    _out(
        f"[{label}] snapshot.end through_cursor={payload['through_cursor']} "
        f"content_hash={payload['content_hash'][:16]}... verified={verified}",
    )
    ws.send(
        json.dumps(_ack_frame(connection_id, payload["through_cursor"], payload["snapshot_id"])),
    )
    _out(f"[{label}] transport.ack snapshot through_cursor={payload['through_cursor']}")
    return connection_id, payload


class _Burst:
    """Emits one large chunk row per call, the way the render path streams them.

    Pacing matters: a whole multi-megabyte burst appended in one go reaches
    every lane inside a single synchronous drain, so even a reading client
    would trip rule 3.  One row at a time is what lets a healthy reader keep
    up while a client that stopped reading fills its own lane.
    """

    def __init__(self, event_log: Path, turn_id: str) -> None:
        self._conn = open_event_log(event_log)
        self._base = {
            "turn_id": turn_id,
            "response_id": "RESP" + uuid.uuid4().hex,
            "response_group_id": "RGRP" + uuid.uuid4().hex,
            "phase": "final",
            "channel": "document",
        }
        self._text = "b" * BURST_CHUNK_BYTES
        self._hash = hashlib.sha256(self._text.encode()).hexdigest()
        emit_event(
            self._conn,
            type="surface.response_open",
            payload={**self._base, "query": "burst?", "kind": "text"},
        )

    def chunk(self, sequence: int) -> None:
        emit_event(
            self._conn,
            type="surface.response_chunk",
            payload={
                **self._base,
                "text": self._text,
                "sequence": sequence,
                "segment_hash": self._hash,
            },
        )

    def close(self, sequences: int) -> None:
        """Terminate the response, so the fold collapses its body to the D8 preview.

        Without this a later snapshot would carry every megabyte this burst
        appended and no client could adopt it.
        """
        emit_event(
            self._conn,
            type="surface.response_emitted",
            payload={
                **self._base,
                "text": self._text * sequences,
                "delivered_via": ["inherent"],
            },
        )
        self._conn.close()


def _drain_until_closed(ws: Any, label: str) -> tuple[int, str]:  # noqa: ANN401 — sync connection.
    """Read whatever the server queued for a stopped reader until it closes."""
    frames = 0
    while True:
        try:
            ws.recv(timeout=CLOSE_DEADLINE_S)
        except ConnectionClosed as closed:
            code = closed.rcvd.code if closed.rcvd is not None else -1
            reason = closed.rcvd.reason if closed.rcvd is not None else "<no close frame>"
            _out(f"[{label}] closed code={code} reason={reason} after {frames} buffered frames")
            return code, reason
        frames += 1


def _run_two_clients(url: str, token: str, root: Path) -> int:
    """D11 rules 3, 4 and 9: B stops reading and never ACKs; A keeps flowing."""
    headers = {"Authorization": f"Bearer {token}"}
    with (
        connect(url, additional_headers=headers) as a,
        connect(url, additional_headers=headers) as b,
    ):
        a_id, _a_end = _handshake(a, "A")
        _handshake(b, "B")
        _out("[B] stops reading its socket and sends no further ack")
        burst = _Burst(root / "mac_events.db", "Tburst" + uuid.uuid4().hex[:8])
        a_cursor = 0
        try:
            for sequence in range(BURST_CHUNKS):
                burst.chunk(sequence)
                # A keeps reading and ACKing, so only B's lane grows.
                _, delta = _recv(a, "view.delta")
                a_cursor = delta["event_cursor"]
                a.send(json.dumps(_ack_frame(a_id, a_cursor)))
                if sequence % 5 == 0:
                    _out(f"[A] view.delta event_cursor={a_cursor} acked")
        finally:
            burst.close(BURST_CHUNKS)
        _out(
            f"emitted {BURST_CHUNKS} burst rows of {BURST_CHUNK_BYTES} bytes each; "
            f"[A] last view.delta event_cursor before B closes = {a_cursor}",
        )
        code, reason = _drain_until_closed(b, "B")
        uids = _emit_turn(root / "mac_events.db", "Tafter" + uuid.uuid4().hex[:8])
        _out(f"emitted one more turn after B is gone event_uids={uids[:1]}...")
        for _ in range(3):
            _, delta = _recv(a, "view.delta")
            a_cursor = delta["event_cursor"]
            a.send(json.dumps(_ack_frame(a_id, a_cursor)))
            _out(f"[A] view.delta after B closed event_cursor={a_cursor}")
    if code != RESYNC_CLOSE_CODE or reason not in {"client_backpressure", "ack_stalled"}:
        _out(f"unexpected close for B: {code} {reason}")
        return 1
    _out(f"two-client smoke ok (B closed {code} {reason}, A still advancing at {a_cursor})")
    return 0


def main(argv: list[str]) -> int:
    """Run the smoke; return 0 when every frame arrived as the card requires."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--two-clients",
        action="store_true",
        help="Run the D11 flow-control scenario instead of the single-client run.",
    )
    args = parser.parse_args(argv)
    root: Path = args.runtime_root.expanduser().resolve()
    token = (root / "inherent-v2.token").read_text(encoding="utf-8").strip()
    url = f"ws://127.0.0.1:{args.port}/inherent/ws/v2"
    if args.two_clients:
        return _run_two_clients(url, token, root)

    with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        ws.send(json.dumps(_hello()))
        _, hello = _recv(ws, "server.hello")
        connection_id = hello["connection_id"]
        _out(
            f"server.hello connection_id={connection_id} log_epoch={hello['log_epoch']} "
            f"boot_id={hello['boot_id']} high_water={hello['payload']['server_high_water_cursor']}",
        )
        _, begin = _recv(ws, "snapshot.begin")
        payload = begin["payload"]
        _out(
            f"snapshot.begin snapshot_id={payload['snapshot_id']} "
            f"through_cursor={payload['through_cursor']} section_order={payload['section_order']} "
            f"counts={payload['counts']} view_schema_version={payload['view_schema_version']}",
        )
        digest = hashlib.sha256()
        for _ in range(payload["counts"]["response_groups"]):
            raw, page = _recv(ws, "snapshot.page")
            digest.update(raw.encode("utf-8"))
            items = page["payload"]["items"]
            groups = [item["response_group_id"][:12] for item in items]
            _out(
                f"snapshot.page page_index={page['payload']['page_index']} "
                f"bytes={len(raw.encode('utf-8'))} items={len(items)} groups={groups}",
            )
        _, end = _recv(ws, "snapshot.end")
        computed = digest.hexdigest()
        verified = "ok" if computed == end["payload"]["content_hash"] else "MISMATCH " + computed
        _out(
            f"snapshot.end through_cursor={end['payload']['through_cursor']} "
            f"content_hash={end['payload']['content_hash']} verified={verified}",
        )
        if computed != end["payload"]["content_hash"]:
            return 1
        ack = {
            "protocol_version": 2,
            "message_type": "transport.ack",
            "message_id": "R" + uuid.uuid4().hex,
            "client_instance_id": "Ismoke",
            "connection_id": connection_id,
            "sent_at_ms": int(time.time() * 1000),
            "payload": {
                "snapshot_id": end["payload"]["snapshot_id"],
                "through_cursor": end["payload"]["through_cursor"],
            },
        }
        ws.send(json.dumps(ack))
        _out(f"transport.ack sent payload={ack['payload']}")
        uids = _emit_turn(root / "mac_events.db", "Tsmoke" + uuid.uuid4().hex[:8])
        _out(f"emitted surface.response_* rows event_uids={uids}")
        for index in range(3):
            _, delta = _recv(ws, "view.delta")
            kinds = [change["kind"] for change in delta["payload"]["changes"]]
            _out(
                f"view.delta{' (first live)' if index == 0 else ''} "
                f"event_cursor={delta['event_cursor']} message_id={delta['message_id']} "
                f"delivery_class={delta['delivery_class']} changes={kinds}",
            )
            if delta["message_id"] != uids[index]:
                _out(f"unexpected source uid: {delta['message_id']} != {uids[index]}")
                return 1
    _out("smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
