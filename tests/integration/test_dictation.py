"""``POST /inherent/dictation`` — the companion's typing tool on the wire (ADR 0058).

Drives the real runtime session (``jarvis.runtime.dictation.Dictation``) through
the real route, with a scripted mic lane, scripted ears and the real polish client
over a scripted provider socket, and asserts on what the desktop reads: level lines
while recording, ``thinking`` once stopped, then the polished words with the raw
ones; a second session refused while one runs; the wake gate's flag up only while
recording; the raw words when the polish fails; no route without a voice stack.
The ears hear half a second from before the request, unbroken into the session,
and the polish connection is opened as the session starts. A stretch that ends in
a half-second pause is heard while he goes on talking, and the stop hears only
the rest (ADR 0076). The event log gets the polish's spend and never the words.
"""

from __future__ import annotations

import json
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import numpy as np
import yaml
from fastapi.testclient import TestClient

from jarvis.runtime.dictation import POLISH_PROMPT, PRE_ROLL_FRAMES, Dictation, polish_client
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RAW = "嗯 把 typlus 的悬浮窗 改成 星核的样子"
POLISHED = "把 Typlus 的悬浮窗改成星核的样子。"


@dataclass(frozen=True)
class _Frame:
    pcm16_mono: bytes


class _Lane:
    """A capture lane that hears a steady tone until closed, one 32 ms frame at a time.

    Each frame's first sample is its number, so the test can tell which ones the ears got.
    """

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.frames = 0

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def read(self, *, timeout_s: float = 0.0) -> _Frame | None:
        _ = timeout_s
        if self._closed.wait(0.032):
            return None
        self.frames += 1
        return _Frame(array("h", [self.frames, *[3000, -3000] * 255, 3000]).tobytes())

    def close(self) -> None:
        self._closed.set()


QUIET = bytes(1024)


def _tone(number: int) -> bytes:
    """A loud 32 ms frame whose first sample is its number."""
    return array("h", [number, *[3000, -3000] * 255, 3000]).tobytes()


class _Talk(_Lane):
    """A mic that is quiet (a frame a millisecond) until given frames, then plays them at once."""

    def __init__(self) -> None:
        super().__init__()
        self.script: deque[bytes] = deque()

    def read(self, *, timeout_s: float = 0.0) -> _Frame | None:
        _ = timeout_s
        if self._closed.wait(0 if self.script else 0.001):
            return None
        return _Frame(self.script.popleft() if self.script else QUIET)


class _EnergySession:
    """Silero's ONNX shape; any sound in the frame counts as speech."""

    def run(self, _names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        speech = np.asarray([[probability]], dtype=np.float32)
        return [speech, inputs["h"].copy(), inputs["c"].copy()]


class _Ingress:
    def __init__(self, lane: type[_Lane] = _Lane) -> None:
        self.lanes: list[_Lane] = []
        self._lane = lane

    def subscribe(self, *, name: str, purpose: object, capacity: int) -> _Lane:
        assert name == "dictation"
        assert capacity > 0
        _ = purpose
        self.lanes.append(self._lane())
        return self.lanes[-1]


class _Provider:
    """The provider socket: records each request, answers or fails."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self.warmed: list[str] = []

    def warm(self, model: str) -> object:
        self.warmed.append(model)
        return SimpleNamespace(id=model)

    def create(self, **kwargs: Any) -> object:  # noqa: ANN401 — the SDK's keywords
        self.requests.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(
            id="resp-dictation",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.answer, tool_calls=None),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=900,
                completion_tokens=30,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def _dictation(
    tmp_path: Path, ears: Callable[[bytes], str], provider: _Provider, lane: type[_Lane] = _Lane,
) -> tuple[Dictation, _Ingress]:
    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    client = polish_client(config["llm"], config["dictation"]["polish_preset"])
    client._openai_client = SimpleNamespace(  # noqa: SLF001 — provider fixture seam
        chat=SimpleNamespace(completions=provider), models=SimpleNamespace(retrieve=provider.warm),
    )
    open_event_log(tmp_path / "events.db").close()
    ingress = _Ingress(lane)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        dictation = Dictation(
            ingress=ingress,  # type: ignore[arg-type]
            vad=voice_audio.SileroVad(mode="record"),
            transcribe=ears,
            client=client,
            vocab_path=tmp_path / "vocab.yaml",
            event_log_path=tmp_path / "events.db",
            pricing_table=load_pricing_table(repo_root() / "data" / "pricing.json"),
        )
    return dictation, ingress


def _app(dictation: Dictation | None) -> TestClient:
    return TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                dictation=dictation,
            )
        )
    )


def _dictate(
    client: TestClient, context: dict[str, str], *, meanwhile: Callable[[], None] | None = None
) -> list[dict[str, Any]]:
    """One session as the desktop runs it: it records until the second tap (``/stop``, 0.3 s in).

    The test client hands back the whole stream at once, so the tap comes from another thread.
    """

    def tap() -> None:
        time.sleep(0.15)
        if meanwhile is not None:
            meanwhile()
        time.sleep(0.15)
        assert client.post("/inherent/dictation/stop").json() == {"ok": True}

    thread = threading.Thread(target=tap)
    thread.start()
    response = client.post("/inherent/dictation", json=context)
    thread.join()
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    return [json.loads(line) for line in response.text.splitlines() if line]


def _events(tmp_path: Path) -> list[tuple[str, str]]:
    conn = open_event_log(tmp_path / "events.db")
    try:
        return [(row[0], row[1]) for row in conn.execute("SELECT type, payload_json FROM events")]
    finally:
        conn.close()


def test_dictation_streams_levels_then_the_polished_words(tmp_path: Path) -> None:
    """Levels while recording, ``thinking`` after stop, then ``{text, raw}``; one at a time."""
    heard: list[bytes] = []

    def ears(pcm: bytes) -> str:
        heard.append(pcm)
        return RAW

    provider = _Provider(POLISHED)
    dictation, ingress = _dictation(tmp_path, ears, provider)
    # Typlus's vocab.yaml: user terms, then auto ones, a repeat dropped.
    (tmp_path / "vocab.yaml").write_text(
        "user:\n- Typlus\n- 星核\nauto:\n- 星核\n- worktree\nrejected:\n- chless\n",
        encoding="utf-8",
    )
    client = _app(dictation)
    context = {"app": "Ghostty", "window": "claude", "selected": ""}
    seen: dict[str, object] = {}

    def meanwhile() -> None:
        # The wake gate reads this flag: up while he dictates. A second session waits its turn.
        seen["active"] = dictation.active
        seen["second"] = client.post("/inherent/dictation", json=context).status_code

    (lane,) = ingress.lanes
    while lane.frames <= PRE_ROLL_FRAMES:  # the mic has been on for a while before the tap
        time.sleep(0.01)
    before = lane.frames
    lines = _dictate(client, context, meanwhile=meanwhile)
    assert seen == {"active": True, "second": 409}
    levels = [line["level"] for line in lines[:-2]]
    assert len(levels) > 2
    assert all(0 <= level <= 1 for level in levels)
    assert max(levels) > 0.5  # the tone is loud
    assert lines[-2]["state"] == "thinking"
    assert lines[-2]["seconds"] > 0
    assert lines[-1] == {"text": POLISHED, "raw": RAW}
    assert not dictation.active
    assert client.post("/inherent/dictation/stop").json() == {"ok": False}
    # The ears got the last half second before the request, then every frame after it, no gap.
    numbers = [array("h", heard[0][i : i + 1024])[0] for i in range(0, len(heard[0]), 1024)]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
    assert before - PRE_ROLL_FRAMES <= numbers[0] < before
    # One lane for the daemon's life: still listening for the next pre-roll.
    assert len(ingress.lanes) == 1
    assert not lane.closed
    lane.close()
    # The polish connection was opened once, as the session started.
    assert provider.warmed == ["gpt-5.4-mini"]
    # One request to gpt-5.4-mini with Typlus's prompt, the raw words and where they land.
    (request,) = provider.requests
    assert request["model"] == "gpt-5.4-mini"
    sent = json.dumps(request["messages"], ensure_ascii=False)
    assert json.dumps(POLISH_PROMPT, ensure_ascii=False)[1:-1] in sent
    assert "by this user):\\nTyplus, 星核, worktree\\n" in sent
    assert "chless" not in sent
    assert f"Raw transcript:\\n{RAW}" in sent
    assert "- app: Ghostty\\n- window: claude\\n- selected text: (none)\\n" in sent
    # The spend is on the ledger; the words are nowhere in it.
    events = _events(tmp_path)
    (cost,) = [json.loads(payload) for kind, payload in events if kind == "cost.recorded"]
    assert cost["model"] == "gpt-5.4-mini"
    assert cost["cost_usd"] > 0
    assert not any("typlus" in payload.lower() or "悬浮窗" in payload for _, payload in events)


def _wait(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_a_stretch_ending_in_a_pause_is_heard_while_he_goes_on(tmp_path: Path) -> None:
    """5.4 s of talk, a 0.6 s pause, more talk: the first stretch is heard before the stop."""
    heard: list[bytes] = []
    words = iter(["第一段。", "second part"])

    def ears(pcm: bytes) -> str:
        heard.append(pcm)
        return next(words) if pcm.strip(b"\0") else ""  # like the pipeline: silence is no words

    provider = _Provider(POLISHED)
    dictation, ingress = _dictation(tmp_path, ears, provider, _Talk)
    (lane,) = ingress.lanes
    assert isinstance(lane, _Talk)
    seen: dict[str, int] = {}

    def talk() -> None:
        lane.script.extend([*(_tone(n) for n in range(1, 171)), *[QUIET] * 20])
        _wait(lambda: len(heard) == 1)
        seen["heard_while_recording"] = len(heard)
        lane.script.extend(_tone(n) for n in range(171, 211))
        _wait(lambda: not lane.script)

    lines = _dictate(_app(dictation), {}, meanwhile=talk)
    lane.close()
    assert seen == {"heard_while_recording": 1}
    assert lines[-1] == {"text": POLISHED, "raw": "第一段。second part"}
    # The first stretch runs past 5 s and ends in exactly the half-second pause that cut it.
    first = heard[0]
    assert len(first) >= 5 * 32_000
    assert first.endswith(QUIET * 16)
    assert not first.endswith(QUIET * 17)
    # Every spoken frame was heard once, in order, across the cuts.
    frames = [pcm[i : i + 1024] for pcm in heard for i in range(0, len(pcm), 1024)]
    assert [array("h", f)[0] for f in frames if f != QUIET] == list(range(1, 211))
    # The joined words went to the polish; the session was already warm, so no second warm-up.
    sent = json.dumps(provider.requests, ensure_ascii=False)
    assert "Raw transcript:\\n第一段。second part" in sent
    assert provider.warmed == ["gpt-5.4-mini"]


def test_the_ears_hear_a_stretch_with_whisper_and_skip_a_quiet_one() -> None:
    """ADR 0077: Whisper hears speech; quiet stretches reach no model."""
    calls: list[str] = []

    class _Ear:
        def __init__(self, name: str) -> None:
            self.name = name

        def recognize(self, _pcm: bytes) -> voice_asr.TranscriptionResult:
            calls.append(self.name)
            return voice_asr.TranscriptionResult(
                text=f"{self.name}的话", confidence=0.9, language_detected="zh", emotion=None,
            )

        def partial_text(self, pcm: bytes) -> str:
            return self.recognize(pcm).text

    pipe = voice_pipeline.VoicePipeline(
        conn_factory=lambda: None,  # type: ignore[arg-type,return-value]
        recognizer=_Ear("sensevoice"),
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=None,
    )
    whisper = _Ear("whisper")
    # One loud 0.2 s among 3 s of near-silence is still speech; 3 s of near-silence is not.
    quiet = array("h", [100, -100] * 24_000).tobytes()
    spoken = quiet[: 2 * 16_000] + _tone(1) * 7 + quiet[2 * 16_000 :]
    assert pipe.transcribe(quiet, recognizer=whisper) == ""
    assert calls == []
    assert pipe.transcribe(spoken, recognizer=whisper) == "whisper的话"
    assert pipe.transcribe(spoken) == "sensevoice的话"
    assert calls == ["whisper", "sensevoice"]


def test_dictation_without_speech_and_with_a_failing_polish(tmp_path: Path) -> None:
    """No words is ``{text: ""}`` without a model call; a failing model gives the raw words."""
    provider = _Provider("unused")
    (tmp_path / "a").mkdir()
    silent, silent_mic = _dictation(tmp_path / "a", lambda _pcm: "", provider)
    assert _dictate(_app(silent), {})[-1] == {"text": "", "raw": ""}
    assert provider.requests == []
    silent_mic.lanes[0].close()

    (tmp_path / "b").mkdir()
    failing = _Provider(TimeoutError("slow"))
    broken, broken_mic = _dictation(tmp_path / "b", lambda _pcm: "原话", failing)
    last = _dictate(_app(broken), {})[-1]
    broken_mic.lanes[0].close()
    # No vocab.yaml, no vocabulary block.
    assert "User vocabulary" not in json.dumps(failing.requests)
    assert set(last) == {"error", "raw"}
    assert last["raw"] == "原话"
    assert "slow" in last["error"]
    assert not broken.active


def test_no_voice_stack_no_dictation_route() -> None:
    """Without the daemon's mic the routes are not there, which the desktop shows as voice off."""
    client = _app(None)
    assert client.post("/inherent/dictation", json={}).status_code == 404
    assert client.post("/inherent/dictation/stop").status_code == 404
