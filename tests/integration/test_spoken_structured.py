"""A structured spoken turn speaks its ``spoken`` string and shows ``written`` apart (ADR 0114).

Same shape as ``test_spoken_streaming``: real ``drive_turn``/``decide()``, a real
Event Log, the real OpenAI SDK against a localhost /v1/responses peer. Only the
model's output is scripted: here a JSON text ``{"spoken": ..., "written": ...}``
that the peer cuts into six-character deltas, so escapes split across them.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import SPOKEN_REPLY_FORMAT
from jarvis.decision.stream_envelope import compose_envelope
from jarvis.decision.stream_json import SpokenJsonExtractor
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.voice_media import _ResponseBuffer, _ResponseChunk
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - autouse fixture, shared with the other spoken-route tests
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_wire_routine_streaming import (
    _drive,
    _payloads,
    _rows,
    _wait_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event

_SPOKEN = "今天有三件事。时间我写在屏幕上了。"
_WRITTEN = '- 10:00 产品会\n- 14:30 评审 "Q3"\nhttps://zoom.us/j/123'


def _reply(spoken: str, written: str, *, ascii_only: bool = True) -> str:
    """The model's text under the schema: the literal ``{"spoken":...,"written":...}``."""
    return json.dumps({"spoken": spoken, "written": written}, ensure_ascii=ascii_only)


def _structured_runtime(tmp_path: Path, url: str, *, structured: bool = True) -> JarvisRuntime:
    runtime = _spoken_runtime(tmp_path, url)
    flags = replace(runtime.response_flags, spoken_structured=structured)
    return replace(runtime, response_flags=flags)


def _chunks(runtime: JarvisRuntime) -> list[str]:
    return [payload["text"] for payload in _payloads(runtime.conn, "surface.response_chunk")]


# ---- the extractor ----------------------------------------------------------

_EXTRACTED = [
    # (spoken, written): escapes of every kind, a surrogate pair, a slash.
    ('He said "hi" \\ there\n你好 😀 é/', '- a\n- "b"\t😀\n\\n'),
    ("今天有三件事。", ""),
    ("", "only on screen"),
]


@pytest.mark.parametrize("ascii_only", [True, False])
@pytest.mark.parametrize(("spoken", "written"), _EXTRACTED)
def test_the_extractor_gives_the_same_text_wherever_the_deltas_split(
    spoken: str, written: str, *, ascii_only: bool,
) -> None:
    """Split at every position (every pair of them, and one character at a time)."""
    text = _reply(spoken, written, ascii_only=ascii_only).replace("/", "\\/")  # \/ is JSON too
    cuts = range(len(text) + 1)
    splits = [[text[:i], text[i:j], text[j:]] for i in cuts for j in range(i, len(text) + 1)]
    for deltas in [*splits, list(text)]:
        extractor = SpokenJsonExtractor()
        said = "".join(extractor.feed(delta) for delta in deltas)
        reply = extractor.finish()
        assert (said, reply.spoken, reply.written) == (spoken, spoken, written), deltas
        assert reply.complete
        assert not extractor.failed


def test_a_lone_surrogate_becomes_a_replacement_character() -> None:
    """A high half with no low half after it, and a low half with no high half before it."""
    extractor = SpokenJsonExtractor()
    said = extractor.feed('{"spoken":"a\\ud83dB\\ude00c\\ud83d')
    said += extractor.feed('\\ud83d\\ude00","written":""}')
    assert said == "a�B�c�😀"


def test_a_cut_stream_keeps_what_was_already_unescaped() -> None:
    """Mid-escape and mid-written: the spoken prefix counts, an unfinished written does not."""
    cut = SpokenJsonExtractor()
    assert cut.feed('{"spoken":"一\\u4e8c\\u4e0') == "一二"
    assert (cut.finish().spoken, cut.finish().complete) == ("一二", False)
    written = SpokenJsonExtractor()
    assert written.feed('{"spoken":"好。","written":"- 10:00 产') == "好。"
    assert (written.finish().written, written.failed) == ("", False)


@pytest.mark.parametrize(("spoken", "said", "written"), [
    ("Here:\n```python\nprint(1)\n```\nDone.", "Here:\n\nDone.",
     "- note\n\n```python\nprint(1)\n```"),
    ("Use `ls` and ``x``.", "Use `ls` and ``x``.", "- note"),
    ("Open:\n```\nrm -rf", "Open:\n", "- note\n\n```\nrm -rf\n```"),
])
def test_a_code_block_in_spoken_goes_to_the_screen(spoken: str, said: str, written: str) -> None:
    """A fenced block is never said wherever the deltas split; it lands after written, once."""
    text = _reply(spoken, "- note", ascii_only=True)
    for deltas in [[text], list(text)]:
        extractor = SpokenJsonExtractor()
        assert "".join(extractor.feed(delta) for delta in deltas) == said
        assert extractor.finish().written == written
    extractor = SpokenJsonExtractor()
    extractor.feed(_reply("A:\n```\nprint(1)\n```", "print(1)", ascii_only=True))
    assert extractor.finish().written == "print(1)"


@pytest.mark.parametrize("text", ["plain words", "<voice>hi</voice>", '{"spoken":5}', '{"a" 1}'])
def test_the_extractor_fails_on_what_the_schema_cannot_produce(text: str) -> None:
    """Text that is not the schema's JSON hands out no spoken text and says so."""
    extractor = SpokenJsonExtractor()
    assert extractor.feed(text) == ""
    assert extractor.failed
    assert not extractor.has_spoken


# ---- the turn ----------------------------------------------------------------


def test_a_reply_that_is_only_code_says_it_is_on_screen(tmp_path: Path) -> None:
    """Code put in spoken is not read aloud: the screen gets it, the voice one line about it."""
    code = '```python\nprint("hi")\n```'
    with _Peer([[("final_answer", _reply(code, ""))]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        _drive(runtime, _spoken(runtime.conn, "turn-c", "用Python写个Hello World"))
    assert _chunks(runtime) == ["代码在屏幕上。"]
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    assert (emitted["voice_text"], emitted["document_text"]) == ("代码在屏幕上。", code)


def test_a_structured_answer_speaks_its_spoken_part_and_keeps_written_as_the_document(
    tmp_path: Path,
) -> None:
    """Chunks are the spoken sentences only; the plan is the envelope; the request, the schema."""
    with _Peer([[("final_answer", _reply(_SPOKEN, _WRITTEN))]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-s", "今天有什么安排"))
    conn = runtime.conn
    assert _chunks(runtime) == ["今天有三件事。", "时间我写在屏幕上了。"]
    assert result.response_plan.text == compose_envelope(_SPOKEN, _WRITTEN)
    (emitted,) = _payloads(conn, "surface.response_emitted")
    assert (emitted["voice_text"], emitted["document_text"]) == (_SPOKEN, _WRITTEN)
    assert emitted["written_apart"] is True
    assert {gate["outcome"] for gate in _payloads(conn, "gate.evaluated")} == {"permit"}
    ((path, body),) = peer.requests
    assert path == "/v1/responses"
    assert body["text"] == {"format": SPOKEN_REPLY_FORMAT}
    assert body["text"]["format"]["strict"] is True
    note = json.dumps(body)
    assert 'a JSON object with \\"spoken\\" and \\"written\\"' in note
    assert "<voice></voice>" not in note


def test_a_typed_turn_takes_the_spoken_route_in_one_request_and_is_never_played(
    tmp_path: Path,
) -> None:
    """ADR 0181: the talk field's turn gets the spoken request shape and card, no rewrite."""
    from jarvis.runtime.inherent_loop import _TTS_SILENT_CHANNELS  # noqa: PLC0415
    from jarvis.state.event_log import emit_event  # noqa: PLC0415

    with _Peer([[("final_answer", _reply(_SPOKEN, _WRITTEN))]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        typed = emit_event(
            runtime.conn,
            type="surface.user_intent",
            payload={"transcript": "今天有什么安排", "turn_id": "turn-t", "channel": "cli_stdin"},
            correlation={"turn_id": "turn-t"},
        )
        result = _drive(runtime, typed)
    assert len(peer.requests) == 1, "the answer request only: no spoken-form rewrite"
    ((path, body),) = peer.requests
    assert path == "/v1/responses"
    assert body["text"] == {"format": SPOKEN_REPLY_FORMAT}
    assert 'a JSON object with \\"spoken\\" and \\"written\\"' in json.dumps(body)
    assert result.response_plan.text == compose_envelope(_SPOKEN, _WRITTEN)
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    assert (emitted["voice_text"], emitted["document_text"]) == (_SPOKEN, _WRITTEN)
    # Nothing of it is played: the speaker's silent set holds its intent channel.
    assert "cli_stdin" in _TTS_SILENT_CHANNELS


def test_the_surface_never_speaks_the_written_part(tmp_path: Path) -> None:
    """Chunks plus the emitted voice text are all L5 can speak; written is in neither."""
    with _Peer([[("final_answer", _reply(_SPOKEN, _WRITTEN))]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        _drive(runtime, _spoken(runtime.conn, "turn-quiet", "今天有什么安排"))
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    buffer = _ResponseBuffer(
        row_id=1, source_event_id="e", response_id="r", response_group_id="g", turn_id="t",
        phase="final", channel="both", gate_mode="sentence", stream=True,
    )
    for sequence, text in enumerate(_chunks(runtime)):
        buffer.chunks[sequence] = _ResponseChunk(sequence, text, "hash")
    buffer.append_voice_suffix(emitted["voice_text"], source_event_uid="e")
    spoken = buffer.speech_text()
    assert "三件事" in spoken
    assert not any(part in spoken for part in ("产品会", "评审", "zoom", "10:00"))


def test_the_done_envelope_carries_written_only_when_it_is_apart() -> None:
    """The one wire addition: ``written`` on ``done``, set only by ``written_apart``."""

    class _Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, message: dict[str, Any]) -> None:
            self.sent.append(message)

    def _done(payload: dict[str, object]) -> Event:
        return SimpleNamespace(payload={"turn_id": "t1", "text": "x", **payload})  # type: ignore[return-value]

    async def _run() -> list[dict[str, Any]]:
        broadcaster, client = InherentBroadcaster(), _Client()
        await broadcaster.register(client)  # type: ignore[arg-type]
        await broadcaster.broadcast_done(_done({"document_text": "d", "written_apart": True}))
        await broadcaster.broadcast_done(_done({"document_text": "d"}))
        await broadcaster.broadcast_done(_done({"document_text": "", "written_apart": True}))
        return client.sent

    apart, whole, empty = asyncio.run(_run())
    assert apart == {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "t1", "written": "d"}}
    assert whole == empty == {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "t1"}}


def test_the_done_envelope_carries_the_whole_spoken_answer() -> None:
    """``spoken`` is ``voice_text`` (never the written part), so the screen holds all she says."""

    async def _run() -> dict[str, Any]:
        sent: list[dict[str, Any]] = []

        class _Client:
            async def send_json(self, message: dict[str, Any]) -> None:
                sent.append(message)

        broadcaster = InherentBroadcaster()
        await broadcaster.register(_Client())  # type: ignore[arg-type]
        payload = {
            "turn_id": "t1",
            "voice_text": "Yes. And the rest.",
            "document_text": "d",
            "written_apart": True,
        }
        await broadcaster.broadcast_done(SimpleNamespace(payload=payload))  # type: ignore[arg-type]
        return sent[0]

    done = asyncio.run(_run())
    assert done["payload"]["spoken"] == "Yes. And the rest."
    assert done["payload"]["written"] == "d"


def test_the_first_spoken_sentence_commits_before_written_has_arrived(tmp_path: Path) -> None:
    """Spoken streams first: its first sentence is a durable chunk while the reply is still open."""
    spoken = "我能听见你。今天也辛苦了。"
    reply = _reply(spoken, _WRITTEN, ascii_only=False)
    with _Peer([[("final_answer", reply)]], hold=True) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive, runtime, _spoken(runtime.conn, "turn-first", "你好"))
            _wait_for(lambda: _rows(runtime.conn, "surface.response_chunk"))
            assert peer.completed == 0
            assert not _rows(runtime.conn, "response.completed")
            assert _chunks(runtime) == ["我能听见你。"]
            peer.release.set()
            result = future.result(timeout=20)
    assert _chunks(runtime) == ["我能听见你。", "今天也辛苦了。"]
    assert result.response_plan.text == compose_envelope(spoken, _WRITTEN)


def test_an_empty_written_part_is_a_plain_spoken_answer(tmp_path: Path) -> None:
    """Nothing more for the screen: no envelope, no ``written_apart``, nothing on ``done``."""
    with _Peer([[("final_answer", _reply("我能听见你。", ""))]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-chat", "你能听见我吗"))
    assert result.response_plan.text == "我能听见你。"
    assert _chunks(runtime) == ["我能听见你。"]
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    assert "written_apart" not in emitted
    assert emitted["voice_text"] == "我能听见你。"


def test_a_stream_cut_in_written_keeps_the_spoken_part(tmp_path: Path) -> None:
    """Spoken was said as written; an unfinished written part is not a document."""
    cut = _reply(_SPOKEN, _WRITTEN)[: -len(_WRITTEN) // 2]
    assert '"written"' in cut
    with _Peer([[("final_answer", cut)]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-cut-w", "今天有什么安排"))
    assert result.response_plan.text == _SPOKEN
    assert "".join(_chunks(runtime)) == _SPOKEN
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    assert "written_apart" not in emitted


def test_a_stream_cut_in_spoken_keeps_only_the_unescaped_prefix(tmp_path: Path) -> None:
    """Cut inside a unicode escape: the text before it is kept, no escape text."""
    full = _reply("你好。我看一下", "")
    cut = full[: full.index("\\u770b") + len("\\u770b") + len("\\u4e0")]  # 我看, then 「\u4e0」
    with _Peer([[("final_answer", cut)]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-cut-s", "你好"))
    assert result.response_plan.text == "你好。我看"
    assert "".join(_chunks(runtime)) == "你好。我看"
    assert "\\" not in result.response_plan.text


def test_a_tool_call_comes_first_and_the_schema_answer_after_its_result(tmp_path: Path) -> None:
    """Both requests carry the schema; no line before the call; then the schema answer."""
    answer = _reply("你还没有记过备忘录。", "")
    outputs = [[("call", "list_memos")], [("final_answer", answer)]]
    with _Peer(outputs) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-call", "我记过什么"))
    (proposed,) = _payloads(runtime.conn, "action.proposed")
    assert proposed["tool_name"] == "list_memos"
    assert not proposed.get("lead_in")
    assert len(peer.requests) == 2
    assert [body["text"] for _path, body in peer.requests] == [{"format": SPOKEN_REPLY_FORMAT}] * 2
    assert result.response_plan.text == "你还没有记过备忘录。"
    assert _chunks(runtime) == ["你还没有记过备忘录。"]


def test_text_that_is_not_the_schema_is_read_as_it_would_be_without_it(tmp_path: Path) -> None:
    """Plain words and the old envelope both go through the splitter, as today."""
    tagged = "<voice>我看一下。</voice><document>- 10:00 产品会</document>"
    with _Peer([[("final_answer", "我能听见你。")]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        plain = _drive(runtime, _spoken(runtime.conn, "turn-plain", "你好"))
    assert plain.response_plan.text == "我能听见你。"
    with _Peer([[("final_answer", tagged)]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        enveloped = _drive(runtime, _spoken(runtime.conn, "turn-tags", "今天有什么安排"))
    assert enveloped.response_plan.text == compose_envelope("我看一下。", "- 10:00 产品会")
    emitted = _payloads(runtime.conn, "surface.response_emitted")[-1]  # the first turn's is above
    assert "written_apart" not in emitted  # the old envelope's document is the whole answer


@pytest.mark.parametrize("structured", [False, True])
def test_the_request_carries_the_schema_only_when_the_switch_is_on(
    tmp_path: Path, *, structured: bool,
) -> None:
    """Off: today's body and today's note, byte for byte what spoken streaming sent."""
    answer = "<voice>我看一下。</voice><document>- 10:00 产品会</document>"
    with _Peer([[("final_answer", answer)]]) as peer:
        runtime = _structured_runtime(tmp_path, peer.url, structured=structured)
        result = _drive(runtime, _spoken(runtime.conn, "turn-sw", "今天有什么安排"))
    ((_path, body),) = peer.requests
    note = json.dumps(body["input"], ensure_ascii=False) + body["instructions"]
    if structured:
        assert body["text"] == {"format": SPOKEN_REPLY_FORMAT}
        assert "<voice></voice>" not in note
    else:
        assert "text" not in body
        assert "<voice></voice>" in note
        assert result.response_plan.text == compose_envelope("我看一下。", "- 10:00 产品会")


# ---- the tool budget of a spoken turn (ADR 0178) -----------------------------


def test_a_spoken_turn_stops_at_the_voice_cap_and_wraps_up_in_the_spoken_shape(
    tmp_path: Path,
) -> None:
    """The cap is the voice one, not 40; the no-tool wrap-up keeps the schema and its language."""
    cap = 2
    said, details = "查到两封。还有些没查完。", "- 邮件一\n- 邮件二"
    wrap_up = _reply(said, details)
    outputs = [[("call", "list_memos")]] * cap + [[("final_answer", wrap_up)]]
    with _Peer(outputs) as peer:
        runtime = _structured_runtime(tmp_path, peer.url)
        llm = {"max_tool_iterations": 40, "max_tool_iterations_voice": cap}
        runtime = replace(runtime, config={**runtime.config, "llm": llm})
        result = _drive(runtime, _spoken(runtime.conn, "turn-cap", "帮我找找邮件里跟 co-op 有关的"))
    assert len(peer.requests) == cap + 1
    assert all(body["tools"] for _, body in peer.requests[:cap])
    _, last = peer.requests[-1]
    assert not last.get("tools")
    assert last["text"] == {"format": SPOKEN_REPLY_FORMAT}
    assert "no more tools can be called" in last["input"][-1]["content"]
    assert '"spoken"' in last["input"][-1]["content"]
    language = {
        "role": "user",
        "content": "[Not new words from the user: still answering "
        '"帮我找找邮件里跟 co-op 有关的"]\n[Reply language for this turn: Chinese]',
    }
    assert last["input"][-2] == language
    assert result.response_plan.text == compose_envelope(said, details)
