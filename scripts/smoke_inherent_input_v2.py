r"""Daemon-level smoke for the ADR-0014 D21 authenticated v2 input routes.

Runs the whole D21 chain against a live daemon and prints it as one line per
fact:

1. connect ``/inherent/ws/v2`` with the boot token and complete the D8
   snapshot handshake, so the socket is live before any input is sent;
2. ``POST /inherent/submit/v2`` with a real question and print the receipt;
3. re-POST the byte-identical request and print the identical receipt — the
   lost-response case, which must not produce a second turn;
4. POST the same ``request_id`` with different text and print the 409;
5. read ``view.delta`` until a ``response.opened`` carries this submission's
   ``source_client_request_id``, and print the ``surface.response_emitted``
   that closes that turn;
6. ``POST /inherent/asr-submit/v2`` with a WAV and print the
   ``utterance.received`` row carrying the receipt's ``turn_id``.

The canary is the printed chain
``request_id -> input_event_uid -> turn_id -> response_group_id``.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/smoke_inherent_input_v2.py \\
        --port 8052 --runtime-root /path/to/root \\
        --question '用一句话说明什么是事件溯源' \\
        --wav data/sensevoice-small-int8/test_wavs/zh.wav

Unlike ``smoke_inherent_sequencer.py`` this run needs a real LLM: the answer
comes from the daemon's configured presets, not from rows written by hand.
``--wav`` defaults to the synthesized tone
(tests/integration/test_voice_ptt_end_to_end.py's ``_wav`` shape); pass a real
speech sample to see ASR return a transcript rather than 422.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from websockets.sync.client import connect

from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    import sqlite3

CLIENT_INSTANCE_ID = "I" + uuid.uuid4().hex
MULTIPART_BOUNDARY = "jarvis-inherent-v2-smoke"
HTTP_OK = 200
HTTP_CONFLICT = 409


def _out(line: str) -> None:
    """Print one operator-facing line; stdout is this script's interface."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _hello() -> dict[str, Any]:
    return {
        "protocol_version": 2,
        "message_type": "client.hello",
        "message_id": "R" + uuid.uuid4().hex,
        "client_instance_id": CLIENT_INSTANCE_ID,
        "connection_id": None,
        "sent_at_ms": int(time.time() * 1000),
        "payload": {
            "supported_versions": [2],
            "client_build": "smoke_inherent_input_v2",
            "view_schema_versions": [1],
            "capabilities": ["paged_snapshot", "transport_ack"],
            "last_log_epoch": None,
            "last_applied_cursor": None,
            "has_complete_local_state": False,
        },
    }


def _recv(ws: Any, expected: str, timeout: float = 10.0) -> dict[str, Any]:  # noqa: ANN401 — sync client.
    raw = ws.recv(timeout=timeout)
    frame: dict[str, Any] = json.loads(raw)
    if frame["message_type"] != expected:
        msg = f"expected {expected}, got {frame['message_type']}: {str(raw)[:200]}"
        raise RuntimeError(msg)
    return frame


def _handshake(ws: Any) -> str:  # noqa: ANN401 — sync client connection.
    """Hello, adopt the paged snapshot, ACK it; return the connection id."""
    ws.send(json.dumps(_hello()))
    hello = _recv(ws, "server.hello")
    connection_id = str(hello["connection_id"])
    _out(f"server.hello connection_id={connection_id} log_epoch={hello['log_epoch']}")
    begin = _recv(ws, "snapshot.begin")
    for _ in range(int(begin["payload"]["counts"]["response_groups"])):
        _recv(ws, "snapshot.page")
    end = _recv(ws, "snapshot.end")["payload"]
    ws.send(
        json.dumps(
            {
                "protocol_version": 2,
                "message_type": "transport.ack",
                "message_id": "R" + uuid.uuid4().hex,
                "client_instance_id": CLIENT_INSTANCE_ID,
                "connection_id": connection_id,
                "sent_at_ms": int(time.time() * 1000),
                "payload": {
                    "through_cursor": end["through_cursor"],
                    "snapshot_id": end["snapshot_id"],
                },
            },
        ),
    )
    _out(f"transport.ack snapshot through_cursor={end['through_cursor']}")
    return connection_id


def _post(
    url: str,
    token: str,
    *,
    body: bytes,
    content_type: str,
) -> tuple[int, dict[str, Any]]:
    """POST and return ``(status, decoded body)``; a refusal is a status, not a raise."""
    request = urllib.request.Request(  # noqa: S310 — a fixed http://127.0.0.1 URL.
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as refused:
        raw = refused.read()
        try:
            return int(refused.code), json.loads(raw)
        except json.JSONDecodeError:
            return int(refused.code), {"detail": raw.decode("utf-8", "replace")}


def _text_body(request_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "client_instance_id": CLIENT_INSTANCE_ID,
            "client_created_at_ms": int(time.time() * 1000),
            "text": text,
        },
    ).encode("utf-8")


def _multipart(fields: dict[str, str], wav: bytes) -> bytes:
    body = bytearray()
    for key, value in sorted(fields.items()):
        body += f"--{MULTIPART_BOUNDARY}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    body += f"--{MULTIPART_BOUNDARY}\r\n".encode()
    body += b'Content-Disposition: form-data; name="audio"; filename="u.wav"\r\n'
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += wav
    body += f"\r\n--{MULTIPART_BOUNDARY}--\r\n".encode()
    return bytes(body)


def _synthesized_wav(duration_s: float = 0.5) -> bytes:
    """The ``_wav`` shape from tests/integration/test_voice_ptt_end_to_end.py."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x10\x00" * int(16000 * duration_s))
    return buf.getvalue()


def _await_opened(ws: Any, request_id: str, deadline_s: float) -> dict[str, Any]:  # noqa: ANN401
    """Read view.delta frames until one opens a group for this submission."""
    until = time.monotonic() + deadline_s
    while time.monotonic() < until:
        raw = ws.recv(timeout=max(1.0, until - time.monotonic()))
        frame = json.loads(raw)
        if frame["message_type"] != "view.delta":
            continue
        for change in frame["payload"]["changes"]:
            if (
                change.get("kind") == "response.opened"
                and change.get("source_client_request_id") == request_id
            ):
                _out(
                    f"view.delta event_cursor={frame['event_cursor']} response.opened "
                    f"response_group_id={change['response_group_id']} "
                    f"turn_id={change['turn_id']} "
                    f"source_client_request_id={change['source_client_request_id']}",
                )
                opened: dict[str, Any] = change
                return opened
    msg = f"no response.opened carried source_client_request_id={request_id!r}"
    raise RuntimeError(msg)


def _await_row(conn: sqlite3.Connection, event_type: str, turn_id: str, deadline_s: float) -> str:
    """Poll the daemon's log for one row of this turn; return its payload JSON."""
    until = time.monotonic() + deadline_s
    while time.monotonic() < until:
        row = conn.execute(
            "SELECT id, payload_json FROM events WHERE type = ? "
            "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id ASC LIMIT 1",
            (event_type, turn_id),
        ).fetchone()
        if row is not None:
            _out(f"{event_type} id={row[0]} payload={str(row[1])[:220]}")
            return str(row[1])
        time.sleep(0.25)
    msg = f"no {event_type} row for turn_id={turn_id!r} within {deadline_s}s"
    raise RuntimeError(msg)


def _submit_text(base: str, token: str, question: str) -> tuple[str, dict[str, Any]]:
    """Steps 2-4: the receipt, the byte-identical retry, and the 409."""
    request_id = "R" + uuid.uuid4().hex
    body = _text_body(request_id, question)
    status, receipt = _post(
        f"{base}/inherent/submit/v2", token, body=body, content_type="application/json",
    )
    if status != HTTP_OK:
        msg = f"submit/v2 answered {status}: {receipt}"
        raise RuntimeError(msg)
    _out(f"submit/v2 status={status} receipt={json.dumps(receipt, ensure_ascii=False)}")

    retry_status, retried = _post(
        f"{base}/inherent/submit/v2", token, body=body, content_type="application/json",
    )
    identical = "identical" if retried == receipt else "DIFFERENT"
    _out(
        f"submit/v2 retry status={retry_status} "
        f"receipt={json.dumps(retried, ensure_ascii=False)} -> {identical}",
    )
    if retried != receipt:
        msg = "the byte-identical retry did not replay the original receipt"
        raise RuntimeError(msg)

    conflict_status, conflict = _post(
        f"{base}/inherent/submit/v2",
        token,
        body=_text_body(request_id, question + "(different)"),
        content_type="application/json",
    )
    _out(f"submit/v2 conflicting payload status={conflict_status} body={conflict}")
    if conflict_status != HTTP_CONFLICT:
        msg = f"a different payload under one request_id answered {conflict_status}, not 409"
        raise RuntimeError(msg)
    return request_id, receipt


def _submit_audio(base: str, token: str, wav: bytes) -> dict[str, Any]:
    """Step 6's upload half."""
    request_id = "R" + uuid.uuid4().hex
    digest = hashlib.sha256(wav).hexdigest()
    status, receipt = _post(
        f"{base}/inherent/asr-submit/v2",
        token,
        body=_multipart(
            {
                "request_id": request_id,
                "client_instance_id": CLIENT_INSTANCE_ID,
                "client_created_at_ms": str(int(time.time() * 1000)),
                "audio_sha256": digest,
                "language": "zh-CN",
            },
            wav,
        ),
        content_type=f"multipart/form-data; boundary={MULTIPART_BOUNDARY}",
    )
    _out(
        f"asr-submit/v2 status={status} audio_bytes={len(wav)} "
        f"receipt={json.dumps(receipt, ensure_ascii=False)}",
    )
    if status != HTTP_OK:
        msg = f"asr-submit/v2 answered {status}: {receipt}"
        raise RuntimeError(msg)
    return receipt


def main() -> int:
    """Run the D21 chain against a live daemon and print every step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--question", default="用一句话说明什么是事件溯源")
    parser.add_argument(
        "--wav", type=Path, default=None, help="WAV to upload; synthesized if unset.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    token = (args.runtime_root / "inherent-v2.token").read_text().strip()
    conn = open_event_log(args.runtime_root / "mac_events.db")
    try:
        with connect(
            f"ws://{args.host}:{args.port}/inherent/ws/v2",
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
        ) as ws:
            _handshake(ws)
            request_id, receipt = _submit_text(base, token, args.question)
            opened = _await_opened(ws, request_id, args.timeout)
            _await_row(conn, "surface.response_emitted", str(receipt["turn_id"]), args.timeout)

        wav = args.wav.read_bytes() if args.wav else _synthesized_wav()
        asr = _submit_audio(base, token, wav)
        _await_row(conn, "utterance.received", str(asr["turn_id"]), 10.0)

        _out(
            "canary chain: request_id=" + request_id
            + " -> input_event_uid=" + str(receipt["input_event_uid"])
            + " -> turn_id=" + str(receipt["turn_id"])
            + " -> response_group_id=" + str(opened["response_group_id"]),
        )
        _out(
            "asr canary chain: request_id=" + str(asr["request_id"])
            + " -> input_event_uid=" + str(asr["input_event_uid"])
            + " -> turn_id=" + str(asr["turn_id"]),
        )
    finally:
        conn.close()
    _out("input v2 smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
