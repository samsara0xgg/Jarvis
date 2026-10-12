"""ADR 0251 — a heavy ask is handed to the deep lane, which answers in the background.

Real ``drive_turn``/``decide()``, a real on-disk Event Log, the real OpenAI and Anthropic SDKs and
one localhost peer that speaks both wire shapes; only the models' replies are scripted. The voice
lane is a ``/v1/chat/completions`` model, the deep lane a ``/v1/messages`` one, so every request
the peer records says which lane sent it, with which model and effort, and with which tools.
"""

# ruff: noqa: RUF001, E501 — his words carry fullwidth punctuation; scripted replies read best on a line
from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self

import pytest
import yaml
from anthropic import AuthenticationError

from jarvis.decision.deep import DEEP_RULES, DeepConfigError, load_deep_settings
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.decision.think_mode import load_think_mode
from jarvis.decision.turn_guards import REPEATED_CALL
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.deep_tool import DESCRIPTION, DeepToolRegistry, build_deep_tool
from jarvis.execution.tools import ActionLifecycle, Tool, ToolRegistry, UnknownToolError
from jarvis.runtime import (
    JarvisRuntime,
    _wave4_response_flags,
    drive_turn,
    make_barge_in_interrupt_callable,
)
from jarvis.runtime.deep_lane import DeepLane
from jarvis.runtime.inherent_loop import (
    _drive_turn_in_worker_thread,
    _emit_turn_failed,
    _start_intent_pump,
)
from jarvis.shared import CallerPrincipal, lang
from jarvis.shared.realtime import Wave1FeatureFlags, Wave5InputFlags
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log, turn_origin
from jarvis.state.input_claim import claim_input_once
from jarvis.state.memory_db import MemorySettings
from jarvis.state.turn_overlap import any_turn_in_flight, open_deep_turn, turns_in_flight
from tests.canary._helpers import repo_root
from tests.integration.test_wave4a_response_run import _realtime_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import Event

_TASK = "把这周的求职邮件理一遍，哪些该回"
_VOICE = "你让我理的这周求职邮件，有三封要回，先回腾讯那封。"
_DOCUMENT = "这周的求职邮件\n1. 腾讯 面试邀约，周五前回\n2. 字节 补充资料\n3. 美团 笔试链接"
_ANSWER = f"<voice>{_VOICE}</voice><document>{_DOCUMENT}</document>"
_HANDED = "这个我得仔细查一下，查好了告诉你。"
_MAIL = {"mails": [{"from": "腾讯", "subject": "面试邀约"}, {"from": "字节", "subject": "补充资料"}]}
_SLOT = {"slots": ["周五上午", "周六下午"]}

# A reply is ("say", text), ("call", tool, arguments), ("status", http code) or ("hold", reply).
type Reply = tuple[Any, ...]
_HAND_OFF: tuple[Reply, ...] = (
    ("call", "think_deeper", {"task": f"{_TASK}。他要的是哪些邮件需要回，先后顺序。"}),
    ("say", _HANDED),
)
_DEEP_RUN: tuple[Reply, ...] = (
    ("call", "mail", {"q": "求职"}),
    ("call", "calendar_find", {"day": "周五"}),
    ("say", _ANSWER),
)
# Every tool but the search and the hand-off, the deferred one on it, the superseded one off.
_DEEP_MENU = ["mail", "calendar_find", "send_reply"]
_FAST_KEY = "DEEP_LANE_FAST_FIXTURE_KEY"
_DEEP_KEY = "DEEP_LANE_DEEP_FIXTURE_KEY"


def _openai(reply: Reply, n: int) -> dict[str, Any]:
    if reply[0] == "call":
        message: dict[str, Any] = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"call_{n}", "type": "function",
                            "function": {"name": reply[1], "arguments": json.dumps(reply[2])}}],
        }
        finish = "tool_calls"
    else:
        message, finish = {"role": "assistant", "content": reply[1]}, "stop"
    return {
        "id": f"cmpl_{n}", "object": "chat.completion", "created": 1, "model": "gpt-6-luna",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }


def _anthropic(reply: Reply, n: int) -> dict[str, Any]:
    if reply[0] == "call":
        block: dict[str, Any] = {"type": "tool_use", "id": f"toolu_{n}", "name": reply[1], "input": reply[2]}
        stop = "tool_use"
    else:
        block, stop = {"type": "text", "text": reply[1]}, "end_turn"
    return {
        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [block], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 6},
    }


class _Peer:
    """Localhost model peer: ``/v1/chat/completions`` is the voice lane, ``/v1/messages`` the deep."""

    def __init__(self) -> None:
        self.script: dict[str, list[Reply]] = {"fast": [], "deep": []}
        self.requests: dict[str, list[dict[str, Any]]] = {"fast": [], "deep": []}
        self.url = ""
        self._served = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.release = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._stop.set()
        self._thread.join(5)

    def then(self, lane: str, *replies: Reply) -> None:
        """Queue replies for the lane's next requests."""
        with self._lock:
            self.script[lane].extend(replies)

    def unused(self) -> dict[str, int]:
        """How many scripted replies each lane did not ask for."""
        with self._lock:
            return {lane: len(left) for lane, left in self.script.items()}

    async def _main(self) -> None:
        server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        self._ready.set()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._stop.wait)
        finally:
            server.close()
            await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            path = lines[0].split()[1]
            fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = json.loads(await reader.readexactly(int(fields["Content-Length"])))
            lane = "deep" if path.endswith("/messages") else "fast"
            with self._lock:
                self.requests[lane].append(body)
                self._served += 1
                reply: Reply = self.script[lane].pop(0) if self.script[lane] else ("status", 500)
                n = self._served
            if reply[0] == "hold":  # ("hold", reply): answer with reply once the test sets `release`
                await asyncio.get_running_loop().run_in_executor(None, self.release.wait, 20)
                reply = reply[1]
            if reply[0] == "status":
                payload = json.dumps({"type": "error", "error": {
                    "type": "authentication_error" if reply[1] == 401 else "api_error",
                    "message": "scripted failure"}}).encode()
                status = f"{reply[1]} Scripted"
            else:
                payload = json.dumps((_anthropic if lane == "deep" else _openai)(reply, n)).encode()
                status = "200 OK"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nConnection: close\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload,
            )
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def _llm(url: str, **deep: Any) -> dict[str, Any]:  # noqa: ANN401 — llm.deep keys
    fast = {"model": "gpt-6-luna", "base_url": f"{url}/v1", "api_key_env": _FAST_KEY, "max_tokens": 256}
    return {
        "provider": "openai", "default_preset": "fast", "timeout_s": 10, "max_retries": 0,
        "max_tool_iterations": 8,
        "think": {"preset": "fast-think", "on_words": "想想吧|仔细查"},
        "deep": {"preset": "deep", "max_tool_iterations": 6, "live_window_s": 120, **deep},
        "presets": {
            "fast": {**fast, "reasoning_effort": "none"},
            "fast-think": {**fast, "reasoning_effort": "medium"},
            "deep": {
                "provider": "anthropic", "model": "claude-sonnet-5-5", "base_url": url,
                "api_key_env": _DEEP_KEY, "max_tokens": 1024, "reasoning_effort": "high",
                "timeout_s": 60, "spoken_structured": False,
            },
        },
    }


def _registry(lane: DeepLane, ran: list[tuple[str, dict[str, Any]]]) -> ToolRegistry:
    def make(name: str, result: dict[str, Any], **flags: Any) -> Tool:  # noqa: ANN401 — Tool fields
        def run(args: Any, _ctx: Any) -> dict[str, Any]:  # noqa: ANN401 — the flat handler shape
            ran.append((name, dict(args)))
            return result

        return Tool(
            name=name, description=f"{name} of the fixture.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            handler=run, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            **{"risk_level": "L1", "read_only": True, **flags},
        )

    registry = ToolRegistry()
    for tool in (
        # The mail tool replaces a deferred one (ADR 0242): the deep menu must not bring it back.
        make("mail", _MAIL, supersedes={"mcp__gmail__gmail_search": "mail(query=...)"}),
        make("mcp__gmail__gmail_search", _MAIL, deferred=True),
        make("calendar_find", _SLOT, deferred=True),
        make("send_reply", {"sent": True}, risk_level="L3", read_only=False, requires_confirmation=True),
        make("tool_search", {"loaded_tools": []}), *build_deep_tool(lane),
    ):
        registry.register(tool)
    return registry


class _Lane:
    """One runtime over the peer, with the tool calls its registry ran."""

    def __init__(self, tmp_path: Path, peer: _Peer, *, cancel: bool = False, **deep: Any) -> None:  # noqa: ANN401
        llm = _llm(peer.url, **deep)
        paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(paths.event_log)
        self.memory = tmp_path / "memory.db"
        self.ran: list[tuple[str, dict[str, Any]]] = []
        config = {**_realtime_config(lifecycle=True, cancel=cancel), "llm": llm}
        bus = CommittedEventBus()
        lane = DeepLane(load_deep_settings(llm) or pytest.fail("no llm.deep"), committed_event_bus=bus)
        flags = _wave4_response_flags(config)
        self.runtime = JarvisRuntime(
            config=config, runtime_paths=paths, conn=self.conn,
            tool_registry=_registry(lane, self.ran), lifecycle=ActionLifecycle(),
            llm_client=LLMClient(llm), system_prompt="SYS",
            wave1_features=Wave1FeatureFlags(transactional_event_append=True, lifecycle_terminal_cas=True),
            response_flags=replace(flags, slow_results=True),
            llm_session_factory=LLMSessionFactory(llm), committed_event_bus=bus,
            think_mode=load_think_mode(llm), deep=lane, input_flags=Wave5InputFlags(intent_pump=True),
            response_runs=ResponseRunRegistry() if flags.independent_response_cancel else None,
            memory=MemorySettings(db_path=self.memory, audio_dir=tmp_path / "audio"),
        )
        self.turns = 0

    def say(self, words: str, *, channel: str = "inherent_wake", ago_s: float = 0) -> Event:
        """What he says, as the surface writes it, ``ago_s`` seconds in the past."""
        self.turns += 1
        turn_id = f"T{self.turns}"
        return emit_event(
            self.conn, type="surface.user_intent",
            payload={"transcript": words, "turn_id": turn_id, "channel": channel, "language": "zh-CN"},
            correlation={"turn_id": turn_id}, ts_epoch_ms=int((time.time() - ago_s) * 1000),
        )

    def drive(self, intent: Event) -> None:
        """Run the turn the way the pump does."""
        drive_turn(self.runtime, user_intent_event=intent, available_surfaces=frozenset(), streaming_enabled=True)

    def deep_intent(self, *, nth: int = -1) -> Event:
        """The task the voice lane handed over, as the pump would find it."""
        rows = self.conn.execute(
            "SELECT event_uid FROM events WHERE type = 'surface.user_intent' "
            "AND json_extract(payload_json, '$.channel') = 'deep' ORDER BY id",
        ).fetchall()
        from jarvis.state.event_log import get_event  # noqa: PLC0415 — only this helper needs it

        event = get_event(self.conn, rows[nth][0])
        assert event is not None
        return event

    def rows(self, event_type: str, turn_id: str | None = None) -> list[dict[str, Any]]:
        out = [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id", (event_type,))]
        return [p for p in out if turn_id is None or p.get("turn_id") == turn_id]

    def answer(self, turn_id: str) -> dict[str, Any]:
        """The one ``surface.response_emitted`` the turn wrote."""
        (emitted,) = self.rows("surface.response_emitted", turn_id)
        return emitted

    def records(self) -> list[tuple[str, str]]:
        with contextlib.closing(sqlite3.connect(self.memory)) as db:
            return list(db.execute("SELECT source, text FROM records ORDER BY ts, rowid"))


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FAST_KEY, "synthetic")
    monkeypatch.setenv(_DEEP_KEY, "synthetic")


def _names(request: dict[str, Any]) -> list[str]:
    """The tools a request offered, whichever wire shape carried them."""
    return [t["function"]["name"] if "function" in t else t["name"] for t in request.get("tools", [])]


def _strings(node: Any) -> Iterator[str]:  # noqa: ANN401 — any JSON value
    """Every string a request carries, unescaped."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _has(request: dict[str, Any], needle: str) -> bool:
    """Whether any string of the request holds ``needle``."""
    return any(needle in one for one in _strings(request))


def _system(request: dict[str, Any]) -> str:
    """The system prompt of an Anthropic request, whether sent as a string or as text blocks."""
    system = request["system"]
    return system if isinstance(system, str) else "".join(b["text"] for b in system)


# --- the voice lane hands off and ends its turn ------------------------------------------------


def test_the_voice_lane_hands_a_heavy_ask_over_and_ends_its_turn(tmp_path: Path) -> None:
    """One call, one true sentence; the task rides a row of its own, in his words."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        intent = lane.say(_TASK)
        lane.drive(intent)

        assert len(peer.requests["fast"]) == 2
        assert peer.requests["deep"] == []  # nothing deep has run yet: the voice turn is over
        offered = peer.requests["fast"][0]
        assert "think_deeper" in _names(offered)
        description = next(t["function"]["description"] for t in offered["tools"] if t["function"]["name"] == "think_deeper")
        assert description == DESCRIPTION
        assert len(DESCRIPTION) <= 600
        # Its answer is the sentence the tool said, and the turn is closed.
        assert lane.answer("T1")["text"] == _HANDED
        assert [r["turn_id"] for r in lane.rows("turn.ended")] == ["T1"]
        # The task is a row of the deep channel: his words and what the turn knew, the origin turn.
        task = lane.deep_intent()
        assert task.payload["channel"] == "deep"
        assert task.payload["transcript"].startswith(_TASK)
        assert task.payload["from_turn"] == "T1"
        assert open_deep_turn(lane.conn, since_ms=0) == task.payload["turn_id"]
        # He is not shown as having said it: no row of his, the voice turn's own row stays one.
        assert [source for source, _ in lane.records()] == ["allen", "jarvis"]


# --- the deep run -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "ago_s", "dismissed", "attention"),
    [
        ("inherent_wake", 0, False, "voice_notify"),  # still talking with her: spoken
        ("cli_stdin", 0, False, "badge_card"),  # typed: a card, nothing said
        ("inherent_wake", 0, True, "badge_card"),  # he ended the conversation meanwhile
        ("inherent_wake", 600, False, "badge_card"),  # he spoke ten minutes ago and went quiet
    ],
)
def test_the_deep_run_works_on_its_preset_with_every_tool_and_delivers_by_where_he_is(
    tmp_path: Path, channel: str, ago_s: float, dismissed: bool, attention: str,  # noqa: FBT001 - pytest parameter
) -> None:
    """Sonnet at high effort, deferred tools on the menu, several steps; spoken live, else a card."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        peer.then("deep", *_DEEP_RUN)
        lane.drive(lane.say(_TASK, channel=channel, ago_s=ago_s))
        if dismissed:
            emit_event(lane.conn, type="surface.conversation_words",
                       payload={"turn_id": "T1", "reason": "dismissed", "transcript": "好了"})
        task = lane.deep_intent()
        lane.drive(task)

        first, *_, last = peer.requests["deep"]
        assert len(peer.requests["deep"]) == 3
        assert first["model"] == "claude-sonnet-5-5"
        assert first["output_config"]["effort"] == "high"
        # Every tool, the deferred one included; neither the search nor the hand-off.
        assert _names(first) == _DEEP_MENU
        assert _names(last) == _DEEP_MENU
        assert DEEP_RULES in _system(first)
        assert _has(first, _TASK)  # the task is the message
        assert _has(first, "a deep task the voice lane handed over")
        assert [name for name, _ in lane.ran] == ["mail", "calendar_find"]
        # The answer says what it answers, and goes out the way he can take it.
        turn_id = task.payload["turn_id"]
        emitted = lane.answer(turn_id)
        assert emitted["voice_text"] == _VOICE
        assert emitted["document_text"] == _DOCUMENT
        assert emitted["attention_channel"] == attention
        assert [r["turn_id"] for r in lane.rows("turn.ended")] == ["T1", turn_id]
        # It is in the conversation (its document, the part that stays) and his words are not
        # restated as his own: the task has no row of his.
        assert lane.records() == [("allen", _TASK), ("jarvis", _HANDED), ("jarvis", _DOCUMENT)]
        peer.then("fast", ("say", "好的。"))
        lane.drive(lane.say("刚才那个结果再说一遍", channel=channel))
        assert _has(peer.requests["fast"][-1], _DOCUMENT)  # "the result from earlier" is there
        assert peer.unused() == {"fast": 0, "deep": 0}


def test_the_voice_lane_that_comes_later_knows_the_task_is_running(tmp_path: Path) -> None:
    """While the task runs, the next sentence's state block names it and does not repeat it as his words."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF, ("say", "现在是下午三点。"))
        lane.drive(lane.say(_TASK))
        claim_input_once(lane.conn, trigger_event=lane.deep_intent())  # the pump's claim
        lane.drive(lane.say("现在几点"))
        latest = peer.requests["fast"][-1]
        assert _has(latest, "Still being answered in another turn")
        assert _has(latest, _TASK)
        # The task is not what he "went on to say" while the earlier answer was awaited.
        assert not _has(latest, "went on to say")


# --- think words force the deep lane ----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "forced"),
    [
        ("想想吧 这周先做哪个项目", True),
        ("仔细查一下这个月为什么花了这么多钱", True),
        ("现在几点", False),
    ],
)
def test_a_think_word_hands_his_sentence_over_without_asking_the_voice_model(
    tmp_path: Path, said: str, forced: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """The word decides before any model is asked: the lane's own sentence, his words as the task."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", ("say", "三点。"))
        lane.drive(lane.say(said))
        if not forced:
            assert len(peer.requests["fast"]) == 1
            assert not lane.conn.execute("SELECT 1 FROM events WHERE json_extract(payload_json, '$.channel') = 'deep'").fetchone()
            return
        assert peer.requests["fast"] == []
        assert lane.answer("T1")["text"] == _HANDED
        assert lane.deep_intent().payload["transcript"] == said
        assert [r["turn_id"] for r in lane.rows("turn.ended")] == ["T1"]


# --- one task at a time ---------------------------------------------------------------------


def test_a_second_task_is_refused_while_one_runs_and_accepted_once_it_is_over(tmp_path: Path) -> None:
    """The refusal is a true sentence and writes nothing; the lane frees when the task ends."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        lane.drive(lane.say(_TASK))
        first = lane.deep_intent()
        # Asked again while the first one waits to be driven.
        peer.then("fast", ("call", "think_deeper", {"task": "查清楚这个月为什么花了这么多钱"}), ("say", "我还在查上一件事，等我告诉你结果了再说这个。"))
        lane.drive(lane.say("查清楚这个月为什么花了这么多钱"))
        result = next(
            json.loads(m["content"]) for m in peer.requests["fast"][-1]["messages"]
            if m["role"] == "tool" and "busy" in m["content"]
        )
        assert result["started"] is False
        assert result["reason"] == "busy"
        assert "还在查上一件事" in result["spoken"]
        assert lane.deep_intent() == first  # no second task was written
        # The first finishes; the lane is free again.
        peer.then("deep", *_DEEP_RUN)
        lane.drive(first)
        peer.then("fast", ("call", "think_deeper", {"task": "周末怎么安排比较好"}), ("say", _HANDED))
        lane.drive(lane.say("周末怎么安排比较好"))
        assert lane.deep_intent().payload["transcript"] == "周末怎么安排比较好"
        assert lane.deep_intent(nth=0) == first


# --- a failing run says so -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "attention"),
    [("inherent_wake", "voice_notify"), ("cli_stdin", "badge_card")],
)
def test_a_deep_run_that_fails_says_so_plainly(tmp_path: Path, channel: str, attention: str) -> None:
    """A refused key ends the task; a fixed sentence says it did not finish, to him where he is."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        peer.then("deep", ("call", "mail", {"q": "求职"}), ("status", 401))
        lane.drive(lane.say(_TASK, channel=channel))
        task = lane.deep_intent()
        with pytest.raises(AuthenticationError) as raised:
            lane.drive(task)
        # The pump closes the failed turn with the error it saw; the lane is free again.
        _emit_turn_failed(lane.conn, intent_event=task, exc=raised.value)
        assert open_deep_turn(lane.conn, since_ms=0) != task.payload["turn_id"]
        # The notice is a task of its own that answers in one sentence.
        notice = lane.deep_intent()
        assert notice.payload["deep_failure"] == "unauthorized"
        assert notice.payload["from_turn"] == task.payload["turn_id"]
        lane.drive(notice)
        emitted = lane.answer(notice.payload["turn_id"])
        assert emitted["text"] == f"你让我仔细查的那件事没查成。{lang.t('failure.unauthorized', lang='zh')}"
        assert emitted["attention_channel"] == attention
        # No model was asked for it, and the failed one is not retried by it.
        assert len(peer.requests["deep"]) == 2
        assert lane.rows("surface.response_emitted", task.payload["turn_id"]) == []
        assert peer.unused() == {"fast": 0, "deep": 0}


# --- the deep view of the real menu ----------------------------------------------------------------

_WRITES = (
    "keep", "save_knowledge", "save_briefing", "save_place", "forget_place", "dismiss_card",
    "mark_highlight", "set_voice", "start_night_run", "end_night_run", "open_url", "open_path",
    "write_mail_draft", "record_application", "refresh_work_state", "disable_plugin", "bus",
)


def test_the_deep_view_of_the_real_menu_offers_only_what_reads_or_asks_first(tmp_path: Path) -> None:
    """Nobody watches the turn: no write without his card, no question, no plugin resume, no page flip."""
    from scripts import tool_exam  # noqa: PLC0415 - this test and the next only

    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((repo_root() / "config/jarvis.yaml").read_text())
    view = DeepToolRegistry(tool_exam._exam_registry(config, recorded))  # noqa: SLF001
    seen = view.for_caller(CallerPrincipal.JARVIS_LLM)
    offered = {t.name for t in seen}
    assert all(t.read_only or t.requires_confirmation for t in seen)
    assert not offered & {"think_deeper", "tool_search", "ask_user", "open_plugin", "show_on_dashboard", *_WRITES}
    assert {"mail", "job_ledger", "my_schedule", "my_day", "web_search", "list_plugins", "remove_plugin", "search_knowledge"} <= offered
    for name in ("keep", "ask_user", "open_plugin"):
        with pytest.raises(UnknownToolError):
            view.dispatch(SimpleNamespace(tool_name=name), None, None, None)  # type: ignore[arg-type]


# --- a barge-in does not reach the deep task ------------------------------------------------------


def test_a_barge_in_leaves_a_running_deep_task_alone_and_still_answers(tmp_path: Path) -> None:
    """The task has no playback to interrupt: the barge-in finds no run, and the answer arrives."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer, cancel=True)
        peer.then("fast", *_HAND_OFF)
        lane.drive(lane.say(_TASK))
        task = lane.deep_intent()
        peer.then("deep", ("hold", ("say", _ANSWER)))
        worker = threading.Thread(
            target=_drive_turn_in_worker_thread, args=(lane.runtime,), kwargs={"user_intent_event": task},
        )
        worker.start()
        for _ in range(500):
            if peer.requests["deep"]:
                break
            time.sleep(0.01)
        assert peer.requests["deep"], "the deep run did not start"
        assert lane.runtime.response_runs is not None
        (run,) = lane.runtime.response_runs.open_runs()
        assert (run.interrupt_policy.confirmed_playback, run.interrupt_policy.generation_action) == ("ignore", "continue")

        assert make_barge_in_interrupt_callable(lane.runtime)("conversation_speech") == "no_open_run"

        peer.release.set()
        worker.join(10)
        assert lane.answer(task.payload["turn_id"])["text"]
        assert lane.rows("response.cancelled") == []


# --- a running task does not hold the daemon busy -------------------------------------------------


def test_a_running_deep_task_is_named_to_later_turns_but_does_not_hold_the_daemon_busy(tmp_path: Path) -> None:
    """Conversation mode and the idle check read any_turn_in_flight: the task must not count there."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        lane.drive(lane.say(_TASK))
        task = lane.deep_intent()
        peer.then("deep", ("hold", ("say", _ANSWER)))
        worker = threading.Thread(
            target=_drive_turn_in_worker_thread, args=(lane.runtime,), kwargs={"user_intent_event": task},
        )
        worker.start()
        try:
            for _ in range(500):
                if peer.requests["deep"]:
                    break
                time.sleep(0.01)
            assert peer.requests["deep"], "the deep run did not start"
            assert any_turn_in_flight(lane.conn, since_ms=0) is False
            later = lane.say("几点了")
            named = turns_in_flight(lane.conn, trigger_event_uid=later.event_uid, since_ms=0)
            assert [turn.turn_id for turn in named] == [task.payload["turn_id"]]
        finally:
            peer.release.set()
            worker.join(10)


# --- the turn's guards still apply in the deep run ---------------------------------------------


def test_the_turn_guards_apply_to_the_deep_run(tmp_path: Path) -> None:
    """A repeated call is not run twice; an answer that only promises a lookup is sent back once."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        peer.then(
            "deep",
            ("say", "我去查一下你的邮件。"),  # promises a lookup no call backs
            ("call", "mail", {"q": "求职"}),
            ("call", "mail", {"q": "求职"}),  # the same call again
            ("say", _ANSWER),
        )
        lane.drive(lane.say(_TASK))
        lane.drive(lane.deep_intent())

        assert [name for name, _ in lane.ran] == ["mail"]  # the repeat did not run
        requests = peer.requests["deep"]
        assert len(requests) == 4
        assert _has(requests[1], "has not happened")  # ADR 0240: the note sent with the bounce
        assert _has(requests[3], REPEATED_CALL)  # ADR 0240: the repeat's result is the note
        assert lane.answer(lane.deep_intent().payload["turn_id"])["voice_text"] == _VOICE


def test_the_deep_run_answers_when_its_step_cap_is_spent(tmp_path: Path) -> None:
    """The cap is the lane's own; a spent one still ends in an answer, from a request with no tools."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer, max_tool_iterations=2)
        peer.then("fast", *_HAND_OFF)
        peer.then(
            "deep",
            ("call", "mail", {"q": "求职"}), ("call", "mail", {"q": "面试"}),
            ("say", "<voice>只查到两封，先回腾讯。</voice><document>腾讯、字节</document>"),
        )
        lane.drive(lane.say(_TASK, channel="cli_stdin"))
        lane.drive(lane.deep_intent())

        requests = peer.requests["deep"]
        assert len(requests) == 3
        assert _names(requests[1]) == _DEEP_MENU
        assert "tools" not in requests[2]  # the wrap-up asks for the answer, with no tools to call
        assert lane.answer(lane.deep_intent().payload["turn_id"])["voice_text"].startswith("只查到两封")


# --- a write still asks first ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "attention"),
    [("inherent_wake", "ask_confirm"), ("cli_stdin", "badge_card")],
)
def test_a_write_the_deep_run_wants_still_waits_on_its_card(
    tmp_path: Path, channel: str, attention: str,
) -> None:
    """ADR 0062 holds: the run stops at the card, nothing is sent; the question is spoken only live."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        peer.then("deep", ("call", "send_reply", {"to": "腾讯", "body": "周五可以"}))
        lane.drive(lane.say(_TASK, channel=channel))
        task = lane.deep_intent()
        lane.drive(task)

        assert len(peer.requests["deep"]) == 1  # the loop ends at the card
        assert lane.ran == []  # nothing was sent
        (card,) = lane.rows("confirmation.requested")
        assert card["action_snapshot"]["tool_name"] == "send_reply"
        assert lane.answer(task.payload["turn_id"])["attention_channel"] == attention
        # The card is the handing-over turn's device's: a phone's task asks on that phone.
        assert turn_origin(lane.conn, task.payload["turn_id"]) == ("deep", "mac")


# --- configuration ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (None, None),
        ({"preset": "deep"}, ("deep", 60, 120)),
        ({"preset": "deep", "max_tool_iterations": 9, "live_window_s": 30}, ("deep", 9, 30)),
        ({"preset": "missing"}, DeepConfigError),
        ({"preset": "deep", "max_tool_iterations": 0}, DeepConfigError),
        ({"preset": "deep", "live_window_s": "soon"}, DeepConfigError),
        ({"preset": "deep", "max_tool_iterations": True}, DeepConfigError),
        ({}, DeepConfigError),
    ],
)
def test_llm_deep_is_read_strictly(block: Any, expected: Any) -> None:  # noqa: ANN401
    """Absent means no deep lane; a broken block stops the boot instead of half-working."""
    llm: dict[str, Any] = {"presets": {"deep": {"model": "m"}}}
    if block is not None:
        llm["deep"] = block
    if expected is DeepConfigError:
        with pytest.raises(DeepConfigError):
            load_deep_settings(llm)
        return
    settings = load_deep_settings(llm)
    assert (settings and (settings.preset, settings.max_tool_iterations, settings.live_window_s)) == (
        expected or None
    )


def test_the_shipped_config_defines_a_deep_lane_that_waits_longer_than_the_voice_lane() -> None:
    """config/jarvis.yaml: Sonnet at high effort, its own long timeout, and the words that reach it."""
    llm = yaml.safe_load((repo_root() / "config/jarvis.yaml").read_text())["llm"]
    settings = load_deep_settings(llm)
    assert settings is not None
    factory = LLMSessionFactory(llm)
    deep, voice = factory.snapshot(settings.preset), factory.snapshot(None)
    assert (deep.provider, deep.model, deep.reasoning_effort) == ("anthropic", "claude-sonnet-5-5", "high")
    assert (deep.timeout_s, voice.timeout_s) == (300.0, 30.0)  # the per-preset limit, then the global
    think = load_think_mode(llm)
    assert think is not None
    assert [bool(think.on.search(said)) for said in ("想想吧", "仔细查一下这个月的花销", "几点了", "think it over")] == [
        True, True, False, True,
    ]


# --- the whole path, through the pump ---------------------------------------------------------


def test_the_pump_runs_the_voice_turn_then_the_task_it_handed_over(tmp_path: Path) -> None:
    """Pump, claim, worker thread, hand-off and the deep turn, from one spoken sentence."""
    with _Peer() as peer:
        lane = _Lane(tmp_path, peer)
        peer.then("fast", *_HAND_OFF)
        peer.then("deep", *_DEEP_RUN)

        async def body() -> None:
            tasks = await _start_intent_pump(lane.runtime, poll_interval_s=0.01, queue_capacity=8, max_concurrent_turns=3)
            try:
                lane.say(_TASK)
                for _ in range(1000):
                    await asyncio.sleep(0.01)
                    if len(lane.rows("turn.ended")) == 2:
                        return
                pytest.fail("the task did not end")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(body())
        task = lane.deep_intent()
        assert lane.answer(task.payload["turn_id"])["attention_channel"] == "voice_notify"
        assert lane.answer("T1")["text"] == _HANDED
        assert peer.unused() == {"fast": 0, "deep": 0}
