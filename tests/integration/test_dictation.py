"""``POST /inherent/dictation`` — the companion's typing tool on the wire (ADR 0058).

Drives the real runtime session (``jarvis.runtime.dictation.Dictation``) through
the real route, with a scripted mic lane, scripted ears and the real polish client
over a scripted provider socket, and asserts on what the desktop reads: level lines
while recording, ``thinking`` once stopped, then the polished words with the raw
ones; a second session refused while one runs; the wake gate's flag up only while
recording; the raw words when the polish fails; no route without a voice stack.
The event log gets the polish's spend and never the words.
"""

from __future__ import annotations

import json
import threading
import time
from array import array
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import yaml
from fastapi.testclient import TestClient

from jarvis.runtime.dictation import POLISH_PROMPT, Dictation, polish_client
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.event_log import open_event_log
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
    """A capture lane that hears a steady tone until closed."""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.frames = 0

    def read(self, *, timeout_s: float = 0.0) -> _Frame | None:
        if self.closed.wait(timeout_s / 10):
            return None
        self.frames += 1
        return _Frame(array("h", [3000, -3000] * 256).tobytes())

    def close(self) -> None:
        self.closed.set()


class _Ingress:
    def __init__(self) -> None:
        self.lanes: list[_Lane] = []

    def subscribe(self, *, name: str, purpose: object, capacity: int) -> _Lane:
        assert name == "dictation"
        assert capacity > 0
        _ = purpose
        self.lanes.append(_Lane())
        return self.lanes[-1]


class _Provider:
    """The provider socket: records each request, answers or fails."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

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
    tmp_path: Path, ears: Callable[[bytes], str], provider: _Provider
) -> tuple[Dictation, _Ingress]:
    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    client = polish_client(config["llm"], config["dictation"]["polish_preset"])
    client._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=provider))  # noqa: SLF001 — provider fixture seam
    open_event_log(tmp_path / "events.db").close()
    ingress = _Ingress()
    return Dictation(
        ingress=ingress,  # type: ignore[arg-type]
        transcribe=ears,
        client=client,
        vocab_path=tmp_path / "vocab.yaml",
        event_log_path=tmp_path / "events.db",
        pricing_table=load_pricing_table(repo_root() / "data" / "pricing.json"),
    ), ingress


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
    assert ingress.lanes[0].closed.is_set()
    assert client.post("/inherent/dictation/stop").json() == {"ok": False}
    # Every frame the lane gave reached the ears.
    assert len(heard[0]) == ingress.lanes[0].frames * 1024
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


def test_dictation_without_speech_and_with_a_failing_polish(tmp_path: Path) -> None:
    """No words is ``{text: ""}`` without a model call; a failing model gives the raw words."""
    provider = _Provider("unused")
    (tmp_path / "a").mkdir()
    silent, _ = _dictation(tmp_path / "a", lambda _pcm: "", provider)
    assert _dictate(_app(silent), {})[-1] == {"text": "", "raw": ""}
    assert provider.requests == []

    (tmp_path / "b").mkdir()
    failing = _Provider(TimeoutError("slow"))
    broken, _ = _dictation(tmp_path / "b", lambda _pcm: "原话", failing)
    last = _dictate(_app(broken), {})[-1]
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
