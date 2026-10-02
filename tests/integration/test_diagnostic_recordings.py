"""ADR 0120: the two test-time recordings, with a fake MiniMax socket and a localhost SSE peer."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.shared import llm_io_log
from jarvis.surface import voice_tts
from jarvis.surface.voice_artifact_store import TtsRecorder
from tests.integration.test_typed_llm_stream import _SSE, _client, _frames
from tests.integration.test_wave2_streaming_media import _FakeWebSocket

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def llm_log(tmp_path: Path) -> Iterator[Path]:
    """Turn the request log on for one test, to a file under tmp_path."""
    path = tmp_path / "logs" / "llm-io.jsonl"
    llm_io_log.configure(path)
    yield path
    llm_io_log.configure(None)


def test_tts_audio_is_kept_per_segment_with_what_was_sent(tmp_path: Path) -> None:
    """One segment's provider PCM and a note of text, voice and model are kept."""
    recorder = TtsRecorder(tmp_path)

    async def _speak() -> None:
        ws = _FakeWebSocket()

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            del additional_headers
            return ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = voice_tts.MiniMaxTTSSession(
                api_key="key", endpoint="https://example.test", voice="Warm_Bestie",
                model="speech-x", volume=1, sample_rate_hz=8_000, connect_timeout_s=0.2,
                first_chunk_timeout_s=0.2, between_chunk_timeout_s=0.2, idle_close_s=1.0,
                command_queue_capacity=1, audio_queue_capacity=4, recorder=recorder,
            )
            await session.open("R", 1)
            await session.send(voice_tts.TTSResponseSegment("R", 1, 0, "It's 6:47 p.m."))
            ws.audio_gate.set()
            events = session.audio_events().__aiter__()
            assert isinstance(await events.__anext__(), voice_tts.TTSAudioChunk)
            assert isinstance(await events.__anext__(), voice_tts.TTSSegmentFinished)
            await session.finish()

    asyncio.run(_speak())
    recorder._pool.shutdown(wait=True)  # noqa: SLF001 - wait for the write thread
    note = json.loads((tmp_path / "tts-R-0.json").read_text())
    assert (note["text"], note["voice"], note["model"], note["complete"]) == (
        "It's 6:47 p.m.", "Warm_Bestie", "speech-x", True,
    )
    assert (tmp_path / note["audio"]).stat().st_size > 0
    assert note["audio"].startswith("tts-R-0.")


def test_recording_failure_is_one_warning_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A folder that cannot be written costs a warning in the write thread, nothing more."""
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    recorder = TtsRecorder(blocked)
    for sequence in range(2):
        recorder.keep("R", sequence, b"\0\0" * 8, sample_rate_hz=8_000, text="t", voice="v",
                      model="m")
    recorder._pool.shutdown(wait=True)  # noqa: SLF001
    assert caplog.text.count("tts recording not kept") == 1


def test_a_streamed_request_logs_its_body_and_what_came_back(
    llm_log: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request as sent (tools by name, key scrubbed), text, tool calls, finish reason, usage."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def _run() -> None:
        peer = _SSE(_frames("openai"))
        async with peer.running() as url:
            with llm_io_log.labels(kind="decision", turn_id="T1", response_id="R1"):
                handle = _client("openai", url).stream_events(
                    messages=[{"role": "user", "content": "key sk-abcdefghijklmnopqrstuvwx"}],
                    system="be brief",
                    tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
                    on_settled=lambda _disposition: None,
                )
            async for _event in handle.events():
                pass

    asyncio.run(_run())
    llm_io_log.flush()
    raw = llm_log.read_text()
    assert "sk-abcdefghijklmnopqrstuvwx" not in raw
    line = json.loads(raw)
    assert (line["kind"], line["turn_id"], line["response_id"]) == ("decision", "T1", "R1")
    assert line["request"]["tools"] == ["lookup"]
    assert line["request"]["messages"][0] == {"role": "system", "content": "be brief"}
    output = line["output"]
    assert output["text"] == "Hello. "
    assert [call["name"] for call in output["tool_calls"]] == ["lookup", "clock"]
    assert (output["finish_reason"], output["outcome"]) == ("tool_calls", "completed")
    assert output["usage"]["input"] == 12


def test_a_full_request_logs_the_same_shape(
    llm_log: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``chat()`` is the other choke point: one line per call, errors included."""
    client = LLMClient({"provider": "openai", "model": "m"})
    answer = ChatResult(text="ok", tool_calls=(), finish_reason="stop", input_tokens=1,
                        output_tokens=2, raw={})
    monkeypatch.setattr(client, "_chat_openai", lambda **_kw: answer)
    with llm_io_log.labels(kind="decision"):
        client.chat(messages=[{"role": "user", "content": "hi"}], system="s", tools=None)

    def _boom(**_kw: object) -> ChatResult:
        message = "slow"
        raise TimeoutError(message)

    monkeypatch.setattr(client, "_chat_openai", _boom)
    with pytest.raises(TimeoutError):
        client.chat(messages=[], system="s")
    llm_io_log.flush()
    first, second = (json.loads(row) for row in llm_log.read_text().splitlines())
    assert (first["call"], first["output"]["text"], first["output"]["finish_reason"]) == (
        "chat", "ok", "stop",
    )
    assert second["output"]["error"] == "TimeoutError"
