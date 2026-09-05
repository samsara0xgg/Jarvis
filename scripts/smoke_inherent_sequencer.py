r"""Daemon-level smoke for the Inherent v2 sequencer (ADR-0014 D8/D9/D11).

Connects to a running daemon's ``/inherent/ws/v2`` with the boot token
under its runtime root, completes the hello, receives and verifies the
paged snapshot, ACKs it, then appends one response turn to that daemon's
Event Log and prints the ``view.delta`` frames that arrive.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/smoke_inherent_sequencer.py \\
        --port 8032 --runtime-root /path/to/root

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

from websockets.sync.client import connect

from jarvis.state.event_log import emit_event, open_event_log


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


def main(argv: list[str]) -> int:
    """Run the smoke; return 0 when every frame arrived as the card requires."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args(argv)
    root: Path = args.runtime_root.expanduser().resolve()
    token = (root / "inherent-v2.token").read_text(encoding="utf-8").strip()
    url = f"ws://127.0.0.1:{args.port}/inherent/ws/v2"

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
