"""ADR 0216: a phone that holds her audio asks the host to judge the words said over her.

Acceptance checks against the real ``/phone/ws`` route over a real event log. The hooks of the
judge (Jev's ``ask``, ``note``, ``begin``, ``recent``) and the host's actions (stop playback,
``barge_in``, ``cancel_turn``, ``set_quiet``) are fakes that record what they were asked, so no
network and no key are involved; the verdict itself comes from the real shared judge
(:mod:`jarvis.surface.word_judge`) and its regexes.

Shown: (a) ``ready`` carries ``judges``; (b) a table of words, flags and hooks gives the verdict,
the ``turn_id`` (set only for a turn), the host's calls in their order, and the turns written;
(c) an unflagged say is unchanged plus ``verdict: "turn"``, and typed words ignore the flags;
(d) a flag that is not a bool is ``bad_say`` and writes nothing; (e) the receive loop keeps
reading while Jev is asked; (f) a resent flagged say is judged again and an unflagged resend
records the turn once; (g) a judge that raises leaves the words a turn.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime import inherent_loop
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents, phone_turn_id
from tests.integration.test_phone_here import REMOTE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TURN_WORDS = "把它推到明天"
RECENT = "今天天气很好适合出门散步"


@dataclass
class _Rig:
    """The route over a real log with fake host actions; ``calls`` is their order."""

    root: Path
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    asked: list[tuple[Any, ...]] = field(default_factory=list)
    elsewhere_asked: list[tuple[str, str, str]] = field(default_factory=list)
    log: Path = field(init=False)
    token: str = field(init=False)
    client: TestClient = field(init=False)

    def build(
        self, *, quiet: bool = True, ask: Callable[..., str | None] | None = None,
        recent: str | None = RECENT,
        elsewhere: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        self.root.mkdir(exist_ok=True)
        self.log = self.root / "events.db"
        open_event_log(self.log).close()
        conn = sqlite3.connect(self.log, check_same_thread=False)
        self.token = pair_device(self.root, "iphone")

        async def open_voice(_device: str, _send: Callable[[bytes], bool]) -> Any:  # noqa: ANN401
            async def stop_playback(reason: str) -> str:
                self.calls.append(("stop_playback", reason))
                return "stopped"

            async def nothing() -> None:
                return None

            return SimpleNamespace(
                player=SimpleNamespace(
                    connection_lost=lambda: None, is_running=False,
                    observe_clock=lambda *_a: None,
                ),
                begin=nothing, close=nothing, stop_playback=stop_playback,
            )

        def barge_in(device: str) -> str:
            self.calls.append(("barge_in", device))
            return "cancelled"

        def cancel_turn(turn_id: str, reason: str) -> str:
            self.calls.append(("cancel_turn", turn_id, reason))
            return "cancelled"

        def set_quiet(level: str) -> None:
            self.calls.append(("set_quiet", level))

        def ask_words(*args: Any) -> str | None:  # noqa: ANN401
            self.asked.append(args)
            return None if ask is None else ask(*args)

        def heard_elsewhere(device: str, turn_id: str, text: str) -> bool:
            self.elsewhere_asked.append((device, turn_id, text))
            return False if elsewhere is None else elsewhere(device, turn_id, text)

        hub = PhoneHub(
            events=BrainEvents(conn),
            rows=inherent_loop._PhoneRows(conn),  # noqa: SLF001
            open_voice=open_voice,
            barge_in=barge_in,
            cancel_turn=cancel_turn,
            ask_words=ask_words,
            note_words=lambda *args: self.calls.append(("note_words", args[1])),
            begin_line=lambda *args: self.calls.append(("begin_line", args[0])),
            recent_speech=None if recent is None else (lambda: recent),
            set_quiet=set_quiet if quiet else None,
            heard_elsewhere=None if elsewhere is None else heard_elsewhere,
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=lambda one: device_name_for_token(self.root, one),
            ),
        )
        require_local_key(
            app, lambda header: local_key_matches(local_key(self.root), header),
            extra_hosts=[REMOTE],
            device_token_matches=lambda one: device_token_matches(self.root, one),
        )
        self.client = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))

    def connect(self) -> Any:  # noqa: ANN401 - a Starlette test websocket context
        return self.client.websocket_connect(
            f"ws://{REMOTE}:8006{PHONE_PATH}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def turns(self) -> list[str]:
        with sqlite3.connect(self.log) as raw:
            return [
                row[0] for row in raw.execute(
                    "SELECT json_extract(payload_json, '$.turn_id') FROM events "
                    "WHERE type IN ('utterance.received', 'surface.user_intent') ORDER BY id",
                )
            ]


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    """A rig over an empty directory; a test builds it with the hooks it needs."""
    return _Rig(tmp_path)


def _open(ws: Any) -> dict[str, Any]:  # noqa: ANN401
    ws.send_json({"type": "hello", "voice": True})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return dict(ready)


def _say(
    ws: Any,  # noqa: ANN401
    utterance_id: str, text: str = TURN_WORDS, *, spoken: bool = True, **flags: Any,  # noqa: ANN401
) -> dict[str, Any]:
    ws.send_json(
        {"type": "say", "utterance_id": utterance_id, "text": text, "spoken": spoken, **flags},
    )
    while (reply := ws.receive_json())["type"] not in {"said", "error"}:
        pass
    return dict(reply)


def test_ready_says_the_host_judges(rig: _Rig) -> None:
    """``ready`` carries ``judges: true``, with or without Jev, recent speech or quiet."""
    rig.build()
    with rig.connect() as ws:
        assert _open(ws)["judges"] is True
    bare = _Rig(rig.root / "bare")  # the regexes alone are a judge
    bare.build(quiet=False, recent=None)
    with bare.connect() as ws:
        assert _open(ws)["judges"] is True


# (words, flags, hooks, verdict, the host's calls in order, whether a turn is written)
BARGE = [("stop_playback", "barge_in"), ("barge_in", "iphone")]
CANCEL = [("stop_playback", "user_stop")]
CANCEL_RECENT = ("cancel_recent",)  # cancel_turn for each turn the device opened lately
CASES: dict[str, tuple[str, dict[str, bool], dict[str, Any], str, list[tuple[Any, ...]], bool]] = {
    "a listening sound over her lets her go on": (
        "嗯", {"over_her": True}, {}, "backchannel", [], False,
    ),
    "a laugh over her lets her go on": ("哈哈", {"over_her": True}, {}, "backchannel", [], False),
    "a listening sound in conversation mode is dropped": (
        "嗯嗯", {"conversation": True}, {}, "backchannel", [], False,
    ),
    "a copy of her own words is her echo": (
        "今天天气很好适合出门", {"over_her": True}, {}, "echo", [], False,
    ),
    "the same words with nothing she said lately are a turn": (
        "今天天气很好适合出门", {"over_her": True}, {"recent": None}, "turn",
        BARGE, True,
    ),
    "a stop request over her stops her as barge does": (
        "别说了", {"over_her": True}, {}, "stop", BARGE, False,
    ),
    "wait in conversation mode stops her as barge does": (
        "等我一下", {"conversation": True}, {}, "wait", BARGE, False,
    ),
    "a dismissal cancels what is on its way as cancel does": (
        "退下", {"conversation": True}, {}, "dismissed", [*CANCEL, CANCEL_RECENT], False,
    ),
    "a quiet phrase is a dismissal and sets the level": (
        "勿扰", {"over_her": True}, {}, "quiet:dnd",
        [*CANCEL, CANCEL_RECENT, ("set_quiet", "dnd")], False,
    ),
    "a quiet phrase is no quiet phrase where it cannot be set": (
        "勿扰", {"over_her": True}, {"quiet": False}, "turn", BARGE, True,
    ),
    "words over her are a turn, after what barge does": (
        TURN_WORDS, {"over_her": True}, {}, "turn", BARGE, True,
    ),
    "words in conversation mode alone are a turn and stop nothing": (
        TURN_WORDS, {"conversation": True}, {}, "turn", [], True,
    ),
    "both flags: words are a turn after barge": (
        TURN_WORDS, {"over_her": True, "conversation": True}, {}, "turn",
        BARGE, True,
    ),
    "Jev calls a short line a keep-going": (
        "好的", {"over_her": True}, {"ask": lambda *_a: "keep_going"}, "backchannel", [], False,
    ),
    "Jev calls a line over her a stop": (
        "那个", {"over_her": True}, {"ask": lambda *_a: "stop"}, "stop",
        BARGE, False,
    ),
    "Jev confirms a dismissal found in a sentence": (
        "好了你可以退下了", {"conversation": True}, {"ask": lambda *_a: "dismiss"}, "dismissed",
        [*CANCEL, CANCEL_RECENT], False,
    ),
    "a dismissal inside a sentence Jev does not confirm is a turn": (
        "好了你可以退下了", {"conversation": True}, {"ask": lambda *_a: None}, "turn", [], True,
    ),
    "Jev failing leaves the line a turn": (
        TURN_WORDS, {"over_her": True}, {"ask": lambda *_a: 1 / 0}, "turn",
        BARGE, True,
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_each_verdict_is_acted_on_and_a_turn_is_written_only_for_a_turn(
    rig: _Rig, name: str,
) -> None:
    """Each verdict gives its ``said``, the host's calls in order, and a turn only for ``turn``."""
    words, flags, hooks, verdict, expected, written = CASES[name]
    rig.build(**hooks)
    with rig.connect() as ws:
        _open(ws)
        earlier = _say(ws, "earlier", spoken=False)  # a recent turn a dismissal cancels
        assert (earlier["turn_id"], earlier["verdict"]) == (
            phone_turn_id("iphone", "earlier"), "turn",
        )
        rig.calls.clear()
        reply = _say(ws, "u1", words, **flags)
    host_calls = [c for c in rig.calls if c[0] not in {"note_words", "begin_line"}]
    want = [
        ("cancel_turn", earlier["turn_id"], "user_stop") if c == CANCEL_RECENT else c
        for c in expected
    ]
    assert reply["type"] == "said", reply
    assert reply["verdict"] == verdict
    assert reply["turn_id"] == (phone_turn_id("iphone", "u1") if written else None)
    assert host_calls == want
    assert rig.turns() == (
        [earlier["turn_id"], phone_turn_id("iphone", "u1")] if written else [earlier["turn_id"]]
    )


def test_an_unflagged_say_is_unchanged_plus_a_turn_verdict_and_typed_words_ignore_flags(
    rig: _Rig,
) -> None:
    """An unflagged say is a turn as before; typed words are never judged."""
    rig.build(ask=lambda *_a: "stop")
    with rig.connect() as ws:
        _open(ws)
        spoken = _say(ws, "a1", "别说了")  # no flag: not judged, a turn
        typed = _say(ws, "a2", "别说了", spoken=False, over_her=True, conversation=True)
    assert spoken == {
        "type": "said", "utterance_id": "a1", "turn_id": phone_turn_id("iphone", "a1"),
        "verdict": "turn",
    }
    assert typed["verdict"] == "turn"
    assert typed["turn_id"] == phone_turn_id("iphone", "a2")
    assert rig.calls == []
    assert rig.asked == []
    assert rig.turns() == [spoken["turn_id"], typed["turn_id"]]


@pytest.mark.parametrize(
    "flags",
    [
        {"over_her": "yes"}, {"over_her": 1}, {"over_her": None}, {"conversation": "true"},
        {"conversation": 0}, {"conversation": None}, {"over_her": True, "conversation": []},
    ],
)
def test_a_flag_that_is_not_a_bool_is_bad_say_and_writes_nothing(
    rig: _Rig, flags: dict[str, Any],
) -> None:
    """A flag present and not a bool is refused whatever the words, and nothing is written."""
    rig.build()
    with rig.connect() as ws:
        _open(ws)
        for spoken in (True, False):  # typed words refuse a bad flag too, then ignore a good one
            reply = _say(ws, "b1", spoken=spoken, **flags)
            assert (reply["type"], reply["code"]) == ("error", "bad_say")
    assert rig.turns() == []
    assert rig.calls == []


def test_the_receive_loop_keeps_reading_while_jev_is_asked(rig: _Rig) -> None:
    """A ping is answered while Jev is still being asked, so DISCARD_ACK would flow too."""
    release = threading.Event()
    asked = threading.Event()

    def slow(*_args: Any) -> str | None:  # noqa: ANN401
        asked.set()
        assert release.wait(10)
        return "keep_going"

    rig.build(ask=slow)
    with rig.connect() as ws:
        _open(ws)
        ws.send_json({
            "type": "say", "utterance_id": "c1", "text": "好的", "spoken": True,
            "over_her": True,
        })
        assert asked.wait(10)
        ws.send_json({"type": "ping", "t_ns": 7})
        assert ws.receive_json()["type"] == "pong"  # read while the judge is still waiting
        release.set()
        reply = ws.receive_json()
    assert (reply["type"], reply["verdict"], reply["turn_id"]) == ("said", "backchannel", None)


def test_a_resent_flagged_say_is_judged_again_and_an_unflagged_resend_records_one_turn(
    rig: _Rig,
) -> None:
    """Judging is never cached; the unflagged resend is the one that writes the turn."""
    rig.build()
    with rig.connect() as ws:
        _open(ws)
        first = _say(ws, "d1", "嗯", over_her=True)
        again = _say(ws, "d1", "嗯", over_her=True)
        assert (first["verdict"], again["verdict"]) == ("backchannel", "backchannel")
        assert rig.turns() == []
        gave_up = _say(ws, "d1", "嗯")
        resent = _say(ws, "d1", "嗯")
    assert gave_up == resent
    assert gave_up["turn_id"] == phone_turn_id("iphone", "d1")
    assert rig.turns() == [gave_up["turn_id"]]


def test_a_judge_that_raises_leaves_the_words_a_turn(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken judge cannot lose Allen's words."""
    def boom(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        msg = "the judge broke"
        raise RuntimeError(msg)

    monkeypatch.setattr("jarvis.surface.word_judge.words_verdict", boom)
    rig.build()
    with rig.connect() as ws:
        _open(ws)
        reply = _say(ws, "e1", TURN_WORDS, over_her=True)
    assert (reply["verdict"], reply["turn_id"]) == ("turn", phone_turn_id("iphone", "e1"))
    assert rig.turns() == [reply["turn_id"]]
