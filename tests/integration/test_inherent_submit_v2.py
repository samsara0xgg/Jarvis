"""Acceptance for the authenticated v2 input routes (ADR-0014 D21).

The routes are exercised through ``TestClient`` against the real app factory
with the real runtime bindings — ``jarvis.runtime.inherent_loop._submit_text_v2``
and ``_submit_asr_v2`` over a real Event Log — so what these cases prove about
idempotency and status codes is what the daemon does.  Only the recognizer is a
fake; ASR quality is not what is under test here.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import socket as socket_module
import wave
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as ws_connect

from jarvis.deployment import inherent_v2_token_matches
from jarvis.runtime.inherent_loop import _submit_asr_v2, _submit_text_v2
from jarvis.shared.realtime import new_connection_id
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_protocol import RuntimeCapabilities
from jarvis.surface.inherent_server import InherentDeps, InherentV2Deps, create_app
from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event

_TOKEN = "t" * 64
_CAPABILITIES = RuntimeCapabilities(
    text_input=True,
    voice_input=True,
    image_input=False,
    response_interrupt=False,
    action_cancel=False,
    confirmation_actions=False,
    natural_barge_in=False,
    aec_profile="headphones_only",
)


def _wav(*, duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """One tiny synthetic mono PCM16 WAV upload."""
    pcm = (b"\x10\x00") * int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buf.getvalue()


def _noop_submit(text: str) -> None:
    _ = text


def _app(
    tmp_path: Path,
    *,
    voice_pipeline_callable: Callable[[bytes, str, str, str], Event] | None = None,
) -> TestClient:
    """The real app with the real runtime bindings over a real log."""
    log_path = tmp_path / "mac_events.db"
    open_event_log(log_path).close()
    deps = InherentDeps(
        submit_callable=_noop_submit,
        broadcaster=InherentBroadcaster(),
        v2=InherentV2Deps(
            token_matches=functools.partial(inherent_v2_token_matches, _TOKEN),
            mint_connection_id=new_connection_id,
            boot_id="B1",
            log_epoch="E1",
            high_water_cursor=lambda: 0,
            runtime_capabilities=lambda: _CAPABILITIES,
            submit_text=functools.partial(_submit_text_v2, log_path),
            submit_asr=(
                None
                if voice_pipeline_callable is None
                else functools.partial(_submit_asr_v2, log_path, voice_pipeline_callable)
            ),
        ),
    )
    return TestClient(create_app(deps))


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _text_body(request_id: str, text: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "client_instance_id": "I-1",
        "client_created_at_ms": 1_700_000_000_000,
        "text": text,
    }


def _log(tmp_path: Path) -> sqlite3.Connection:
    return open_event_log(tmp_path / "mac_events.db")


def _rows(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(json.loads(str(payload)), event_uid=str(uid))
        for uid, payload in conn.execute(
            "SELECT event_uid, payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


# --- Text ------------------------------------------------------------------


def test_submit_v2_returns_the_receipt_and_appends_one_canonical_row(tmp_path: Path) -> None:
    """The D21 accepted response names the exact durable row and its turn."""
    client = _app(tmp_path)
    resp = client.post(
        "/inherent/submit/v2", json=_text_body("r1", " 明天下雨吗 "), headers=_auth(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["request_id"] == "r1"
    assert body["turn_id"].startswith("T")
    assert body["session_id"] is None

    conn = _log(tmp_path)
    rows = _rows(conn, "surface.user_intent")
    assert len(rows) == 1
    assert rows[0]["event_uid"] == body["input_event_uid"]
    assert rows[0]["turn_id"] == body["turn_id"]
    assert rows[0]["source_client_request_id"] == "r1"
    assert rows[0]["source_surface"] == "inherent_v2"
    assert rows[0]["transcript"] == "明天下雨吗"
    conn.close()


def test_submit_v2_identical_retry_returns_the_identical_receipt(tmp_path: Path) -> None:
    """The lost-response case: retry the same bytes, get the same ids, one turn."""
    client = _app(tmp_path)
    body = _text_body("r1", "hello")
    first = client.post("/inherent/submit/v2", json=body, headers=_auth()).json()
    second = client.post("/inherent/submit/v2", json=body, headers=_auth())

    assert second.status_code == 200
    assert second.json() == first

    conn = _log(tmp_path)
    assert len(_rows(conn, "surface.user_intent")) == 1
    conn.close()


def test_submit_v2_different_payload_under_one_request_id_is_409(tmp_path: Path) -> None:
    """A conflicting reuse is refused and appends nothing."""
    client = _app(tmp_path)
    client.post("/inherent/submit/v2", json=_text_body("r1", "hello"), headers=_auth())
    resp = client.post("/inherent/submit/v2", json=_text_body("r1", "goodbye"), headers=_auth())

    assert resp.status_code == 409
    conn = _log(tmp_path)
    rows = _rows(conn, "surface.user_intent")
    assert len(rows) == 1
    assert rows[0]["transcript"] == "hello"
    conn.close()


def test_submit_v2_empty_text_is_400(tmp_path: Path) -> None:
    """Post-strip emptiness is refused exactly as v1 refuses it."""
    client = _app(tmp_path)
    resp = client.post("/inherent/submit/v2", json=_text_body("r1", "   "), headers=_auth())
    assert resp.status_code == 400


# --- Authentication --------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": _TOKEN}],
    ids=["missing", "wrong_token", "no_scheme"],
)
def test_v2_routes_refuse_an_unauthenticated_request(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    """Both routes answer 403, the status ``/inherent/ws/v2`` gives a bad token."""
    client = _app(tmp_path, voice_pipeline_callable=_transcribing_pipeline("你好"))
    wav = _wav()

    text = client.post("/inherent/submit/v2", json=_text_body("r1", "hello"), headers=headers)
    asr = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r2", wav),
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=headers,
    )

    assert text.status_code == 403
    assert asr.status_code == 403
    conn = _log(tmp_path)
    assert _rows(conn, "surface.user_intent") == []
    conn.close()


def test_a_bad_token_is_http_403_on_the_route_and_on_the_socket(tmp_path: Path) -> None:
    """The parity claim, on a real ASGI server rather than the test transport.

    ``TestClient`` surfaces the socket's pre-accept close as a
    ``WebSocketDisconnect``; only a real server turns it into the HTTP 403 the
    card names.  So this one case runs uvicorn on a free port and asserts the
    same status from both halves of the v2 surface.
    """

    async def _body() -> None:
        log_path = tmp_path / "mac_events.db"
        open_event_log(log_path).close()
        deps = InherentDeps(
            submit_callable=_noop_submit,
            broadcaster=InherentBroadcaster(),
            v2=InherentV2Deps(
                token_matches=functools.partial(inherent_v2_token_matches, _TOKEN),
                mint_connection_id=new_connection_id,
                boot_id="B1",
                log_epoch="E1",
                high_water_cursor=lambda: 0,
                runtime_capabilities=lambda: _CAPABILITIES,
                submit_text=functools.partial(_submit_text_v2, log_path),
            ),
        )
        with socket_module.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(deps),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="off",
            ),
        )
        serving = asyncio.create_task(server.serve())
        try:
            for _ in range(500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            bad = {"Authorization": "Bearer wrong"}
            async with httpx.AsyncClient() as http:
                posted = await http.post(
                    f"http://127.0.0.1:{port}/inherent/submit/v2",
                    json=_text_body("r1", "hello"),
                    headers=bad,
                )
            assert posted.status_code == 403

            def _upgrade() -> None:
                ws_connect(
                    f"ws://127.0.0.1:{port}/inherent/ws/v2", additional_headers=bad,
                ).close()

            with pytest.raises(InvalidStatus) as caught:
                await asyncio.to_thread(_upgrade)
            assert caught.value.response.status_code == 403
        finally:
            server.should_exit = True
            await serving

    asyncio.run(_body())


def test_v2_routes_are_absent_without_v2_deps(tmp_path: Path) -> None:
    """A v1-only deployment's route table is byte-identical to what it was."""
    log_path = tmp_path / "mac_events.db"
    open_event_log(log_path).close()
    client = TestClient(
        create_app(InherentDeps(submit_callable=_noop_submit, broadcaster=InherentBroadcaster())),
    )
    assert client.post("/inherent/submit/v2", json=_text_body("r1", "x")).status_code == 404
    assert client.post("/inherent/asr-submit/v2").status_code == 404


# --- ASR -------------------------------------------------------------------


def _asr_form(request_id: str, wav: bytes, *, sha256: str | None = None) -> dict[str, str]:
    return {
        "request_id": request_id,
        "client_instance_id": "I-1",
        "client_created_at_ms": "1700000000000",
        "audio_sha256": sha256 or hashlib.sha256(wav).hexdigest(),
        "language": "zh-CN",
    }


def _transcribing_pipeline(transcript: str) -> Callable[[bytes, str, str, str], Event]:
    """A pipeline that commits a real ``utterance.received``, as the real one does."""

    def pipeline(pcm: bytes, turn_id: str, channel: str, language: str) -> Event:
        from jarvis.state.event_log import emit_event  # noqa: PLC0415 — worker-thread import.

        _ = pcm
        conn = open_event_log(_PIPELINE_LOG[0])
        try:
            return emit_event(
                conn,
                type="utterance.received",
                payload={
                    "transcript": transcript,
                    "turn_id": turn_id,
                    "channel": channel,
                    "language": language,
                    "emotion": "HAPPY",
                },
                correlation={"turn_id": turn_id},
            )
        finally:
            conn.close()

    return pipeline


_PIPELINE_LOG: list[Path] = []


@pytest.fixture(autouse=True)
def _bind_pipeline_log(tmp_path: Path) -> None:
    """Point the fake pipeline at this test's log, as the daemon binding does."""
    _PIPELINE_LOG.clear()
    _PIPELINE_LOG.append(tmp_path / "mac_events.db")


def test_asr_submit_v2_happy_path_and_retry(tmp_path: Path) -> None:
    """One upload commits one ``utterance.received``; the retry replays its ids."""
    client = _app(tmp_path, voice_pipeline_callable=_transcribing_pipeline("你好"))
    wav = _wav()
    form = _asr_form("r1", wav)

    resp = client.post(
        "/inherent/asr-submit/v2",
        data=form,
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["text"] == "你好"
    assert body["emotion"] == "HAPPY"
    assert body["turn_id"].startswith("T")

    conn = _log(tmp_path)
    rows = _rows(conn, "utterance.received")
    assert len(rows) == 1
    assert rows[0]["turn_id"] == body["turn_id"]
    assert rows[0]["event_uid"] == body["input_event_uid"]
    conn.close()

    again = client.post(
        "/inherent/asr-submit/v2",
        data=form,
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )
    assert again.status_code == 200
    assert again.json() == body
    conn = _log(tmp_path)
    assert len(_rows(conn, "utterance.received")) == 1
    conn.close()


def test_asr_submit_v2_too_large_is_413(tmp_path: Path) -> None:
    """Parity with test_inherent_server_asr_submit.py: 6 MB exceeds the 5 MB cap."""
    client = _app(tmp_path, voice_pipeline_callable=_never_called)
    big = b"R" * (6 * 1024 * 1024)
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", big),
        files={"audio": ("u.wav", big, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 413


def test_asr_submit_v2_wrong_content_type_is_415(tmp_path: Path) -> None:
    """Parity: an unsupported content type is refused before the body is read."""
    client = _app(tmp_path, voice_pipeline_callable=_never_called)
    wav = _wav()
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", wav),
        files={"audio": ("u.mp3", wav, "audio/mpeg")},
        headers=_auth(),
    )
    assert resp.status_code == 415


def test_asr_submit_v2_empty_recognition_is_422(tmp_path: Path) -> None:
    """Parity: ``VoicePipelineEmptyError`` is 422, and the lease is released."""
    client = _app(tmp_path, voice_pipeline_callable=_raising(VoicePipelineEmptyError("empty")))
    wav = _wav()
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", wav),
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 422

    conn = _log(tmp_path)
    state = conn.execute("SELECT state FROM input_submission_receipts").fetchone()
    assert str(state[0]) == "released"
    conn.close()


def test_asr_submit_v2_busy_is_503(tmp_path: Path) -> None:
    """Parity: ``VoiceInputBusyError`` is 503."""
    client = _app(tmp_path, voice_pipeline_callable=_raising(VoiceInputBusyError("busy")))
    wav = _wav()
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", wav),
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 503


def test_asr_submit_v2_mismatched_digest_is_400(tmp_path: Path) -> None:
    """A truncated upload cannot resolve a receipt against audio nobody heard."""
    client = _app(tmp_path, voice_pipeline_callable=_never_called)
    wav = _wav()
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", wav, sha256="0" * 64),
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_asr_submit_v2_different_audio_under_one_request_id_is_409(tmp_path: Path) -> None:
    """A second, different upload under one request id is a conflict."""
    client = _app(tmp_path, voice_pipeline_callable=_transcribing_pipeline("你好"))
    first, second = _wav(), _wav(duration_s=0.75)
    client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", first),
        files={"audio": ("u.wav", first, "audio/wav")},
        headers=_auth(),
    )
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", second),
        files={"audio": ("u.wav", second, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 409
    conn = _log(tmp_path)
    assert len(_rows(conn, "utterance.received")) == 1
    conn.close()


def test_asr_submit_v2_is_501_when_the_pipeline_is_unwired(tmp_path: Path) -> None:
    """A text-only deployment answers 501, exactly as the v1 route does."""
    client = _app(tmp_path)
    wav = _wav()
    resp = client.post(
        "/inherent/asr-submit/v2",
        data=_asr_form("r1", wav),
        files={"audio": ("u.wav", wav, "audio/wav")},
        headers=_auth(),
    )
    assert resp.status_code == 501


def _never_called(pcm: bytes, turn_id: str, channel: str, language: str) -> Event:
    _ = (pcm, turn_id, channel, language)
    pytest.fail("pipeline should not be reached")


def _raising(error: Exception) -> Callable[[bytes, str, str, str], Event]:
    def pipeline(pcm: bytes, turn_id: str, channel: str, language: str) -> Event:
        _ = (pcm, turn_id, channel, language)
        raise error

    return pipeline
