"""The phone's conversation socket (ADR 0209): ``/phone/ws`` against ``contracts/phone_ws``.

The real route over a real event log, with the host's actions (stop playback, barge in, quiet,
the other devices' log) as recording fakes. The files hold what the phone sends, as ``valid`` and
``invalid`` lists the iOS tests encode against, and every frame the brain sends back, as examples
produced here by driving the socket: ``ready``, ``said`` for every verdict (``elsewhere`` and
``not_owner`` included, the two a client once met unhandled), ``step`` and ``error`` for every code.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from jarvis.state.attachments import AttachmentRefused
from jarvis.state.event_log import emit_event, open_runtime_event_log
from jarvis.state.voiceprint import DIMENSIONS, VoiceprintBook
from jarvis.surface import voice_asr
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ
from tests.support.contracts import WORLD_NOW_MS, Contracts, brain_at
from tests.support.phone import TURN_WORDS, JudgeRig, open_ws

if TYPE_CHECKING:
    from pathlib import Path

SOCKET = "/phone/ws"
MIC = "reSpeaker XVF3800 4-Mic Array"


def _rig(tmp_path: Path, name: str, **hooks: Any) -> JudgeRig:  # noqa: ANN401 - the rig's hooks
    """A route over a log of its own, so each example starts from an empty conversation."""
    rig = JudgeRig(tmp_path / name)
    rig.build(**hooks)
    return rig


def _answer(ws: Any) -> dict[str, Any]:  # noqa: ANN401 - a Starlette test websocket
    """The next ``said`` or ``error`` frame."""
    while (frame := ws.receive_json())["type"] not in {"said", "error"}:
        pass
    return dict(frame)


def _send(ws: Any, value: Any) -> None:  # noqa: ANN401
    """A case on the socket: text is sent as it is, anything else as JSON."""
    if isinstance(value, str):
        ws.send_text(value)
    else:
        ws.send_json(value)


# --- what the phone sends ---------------------------------------------------------------------


def test_the_brain_answers_a_hello_with_ready_and_closes_on_any_other_first_frame(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """Every valid hello opens the conversation; an invalid one is closed with code 1008."""
    inputs = contracts.inputs("phone_ws/hello", frame="hello", socket=SOCKET)
    opened = []
    for index, case in enumerate(inputs.valid):
        # a rig per socket: the test client ends a socket's loop at once, so a device that
        # reconnects on the same rig finds the last one still registered with nothing to close
        with _rig(tmp_path, f"hello-{index}").connect() as ws:
            _send(ws, case.value)
            opened.append((case.name, ws.receive_json()["type"]))
    assert opened == [(case.name, "ready") for case in inputs.valid]

    for index, case in enumerate(inputs.invalid):
        assert case.refusal == "close 1008"
        with _rig(tmp_path, f"refused-{index}").connect() as ws:
            _send(ws, case.value)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
        assert closed.value.code == 1008, case.name


def test_the_brain_takes_every_valid_say_and_answers_error_bad_say_to_every_invalid_one(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """A valid say is answered ``said``; an invalid one ``error bad_say`` and writes no turn."""
    inputs = contracts.inputs("phone_ws/say", frame="say", socket=SOCKET)
    rig = _rig(tmp_path, "say")
    with brain_at(inputs.now_ms), rig.connect() as ws:
        open_ws(ws)
        taken: dict[str, str] = {}
        for case in inputs.valid:
            ws.send_json(case.value)
            taken[case.name] = _answer(ws)["type"]
        turns_after_valid = len(rig.turns())
        refused: dict[str, tuple[str, str | None]] = {}
        for case in inputs.invalid:
            ws.send_json(case.value)
            reply = _answer(ws)
            refused[case.name] = (reply["type"], reply.get("code"))
    assert taken == {case.name: "said" for case in inputs.valid}
    assert refused == {case.name: ("error", case.refusal) for case in inputs.invalid}
    assert len(rig.turns()) == turns_after_valid, "a refused say wrote a turn"


# --- what the brain sends ---------------------------------------------------------------------


def test_the_ready_frame_says_whether_she_will_speak_and_at_what_rate(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """``voice`` follows the hello; ``sample_rate`` is her voice's rate in Hz; ``judges`` is on."""
    ready: dict[str, Any] = {}
    for name, voice in (("voice", True), ("no_voice", False)):
        with _rig(tmp_path, f"ready-{name}").connect() as ws:
            ws.send_json({"type": "hello", "voice": voice})
            ready[name] = ws.receive_json()
    contracts.output(
        "phone_ws/ready", examples=ready, frame="ready", socket=SOCKET,
        exact=("type", "voice", "sample_rate", "judges"),
        description=(
            "The brain's answer to a hello. `device` is the name the token was paired under; "
            "`voice` is false when the phone said it wants none or the brain cannot speak; "
            "`sample_rate` is the rate of her voice in Hz (the binary frames carry 16-bit PCM "
            "at it); `judges` says the brain judges words said over her (ADR 0216)."
        ),
    )
    assert ready["voice"]["sample_rate"] == PHONE_SAMPLE_RATE_HZ
    assert ready["no_voice"]["voice"] is False


def _his_voice_is_not_this(tmp_path: Path) -> VoiceprintBook:
    """A profile on axis 0 of the phone's microphone, so a voice on axis 1 is not his."""
    book = VoiceprintBook(tmp_path / "memory.db")
    centroid = np.zeros(DIMENSIONS, dtype=np.float32)
    centroid[0] = 1.0
    book.seed("iphone", MIC, 300, centroid.tolist())
    return book


def _stranger() -> dict[str, Any]:
    far = np.zeros(DIMENSIONS, dtype=np.float32)
    far[1] = 1.0
    return {
        "embedding": [round(float(one), 5) for one in far], "seconds": 2.5, "mic": MIC,
        "mic_age_s": 300.0,
    }


# (words, flags) of each verdict; what the judge makes of them is its own tests' business
VERDICTS: dict[str, tuple[str, dict[str, Any]]] = {
    "turn": (TURN_WORDS, {}),
    "backchannel": ("嗯", {"over_her": True}),
    "unclear": ("五", {"over_her": True}),
    "echo": ("今天天气很好适合出门", {"over_her": True}),
    "stop": ("别说了", {"over_her": True}),
    "wait": ("等我一下", {"conversation": True}),
    "dismissed": ("退下", {"conversation": True}),
    "quiet:dnd": ("勿扰", {"over_her": True}),
    "quiet:quiet": ("安静一点", {"over_her": True}),
    "quiet:no-pop": ("不弹模式", {"over_her": True}),
    "quiet:off": ("恢复正常", {"over_her": True}),
    "elsewhere": (TURN_WORDS, {}),
    "not_owner": (TURN_WORDS, {"voiceprint": _stranger()}),
}


def test_the_said_frame_of_every_verdict_the_brain_gives(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """One example per verdict: only ``turn`` has a turn and a record; the others name none."""
    assert {v.removeprefix("quiet:") for v in VERDICTS if v.startswith("quiet:")} == set(
        voice_asr.QUIET_REASONS,
    ), "a quiet level has no example"
    said: dict[str, Any] = {}
    with brain_at():
        for verdict, (words, flags) in VERDICTS.items():
            hooks: dict[str, Any] = {}
            if verdict == "elsewhere":
                hooks["elsewhere"] = lambda *_a: True
            if verdict == "not_owner":
                hooks["voiceprints"] = _his_voice_is_not_this(tmp_path / "book")
            rig = _rig(tmp_path, verdict.replace(":", "-"), **hooks)
            with rig.connect() as ws:
                open_ws(ws)
                ws.send_json({
                    "type": "say", "utterance_id": "u1", "text": words, "spoken": True, **flags,
                })
                said[verdict] = _answer(ws)
    assert {verdict: frame["verdict"] for verdict, frame in said.items()} == {
        verdict: verdict for verdict in VERDICTS
    }
    contracts.output(
        "phone_ws/said", examples=said, frame="said", socket=SOCKET, exact=("type", "verdict"),
        now_ms=WORLD_NOW_MS,
        description=(
            "The answer to a say, one per verdict. `turn`: the words are a turn, `turn_id` names "
            "it and `record_id` is the id its message will have. Every other verdict records "
            "nothing and has `turn_id` null: the phone resumes her audio on `backchannel`, "
            "`unclear` and `echo`; the brain has stopped her on `stop` and `wait`, and ended "
            "the conversation on `dismissed` and `quiet:<level>` (levels quiet, no-pop, dnd, "
            "off); on `elsewhere` and `not_owner` another device or another voice has it and "
            "the phone does not resend. A client handles a verdict it does not know like "
            "`backchannel`."
        ),
    )


def _action(conn: sqlite3.Connection, turn_id: str, action_id: str, at: int, *, ends: str) -> None:
    """One tool call of a turn as the dispatcher logs it: proposed, running, then its end."""
    correlation = {"action_id": action_id, "turn_id": turn_id}
    emit_event(
        conn, type="action.proposed", ts_epoch_ms=at, correlation=correlation,
        payload={
            "action_id": action_id, "tool_name": "web_search", "caller_principal": "jarvis_llm",
            "risk_level": "L1", "arguments": {"query": "victoria weather"}, "turn_id": turn_id,
        },
    )
    running = emit_event(
        conn, type="action.running", ts_epoch_ms=at + 100, correlation=correlation,
        payload={"action_id": action_id},
    )
    failed = ends == "failed"
    emit_event(
        conn, type="action.result_observed", ts_epoch_ms=at + 2_600,
        source_event_id=running.event_uid, correlation={"action_id": action_id},
        payload={
            "action_id": action_id, "semantics": "error" if failed else "observation",
            "tool_output": "{}", **({"error": "timed out"} if failed else {}),
        },
    )


def test_the_step_frames_of_a_tool_that_runs_finishes_and_fails(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """``running`` with no end, then the same ``step`` number as ``done``; or as ``failed``."""
    rig = _rig(tmp_path, "step")
    with brain_at() as clock, rig.connect() as ws:
        open_ws(ws)
        ws.send_json({"type": "say", "utterance_id": "t1", "text": "天气", "spoken": False})
        turn_id = _answer(ws)["turn_id"]
        with closing(open_runtime_event_log(rig.log)) as conn:
            _action(conn, turn_id, "A1", clock.time_ms(), ends="done")
            _action(conn, turn_id, "A2", clock.time_ms() + 5_000, ends="failed")
        steps: list[dict[str, Any]] = []
        while len(steps) < 4:
            if (frame := ws.receive_json())["type"] == "step":
                steps.append(frame)
    assert [(s["step"], s["state"]) for s in steps] == [
        (1, "running"), (1, "done"), (2, "running"), (2, "failed"),
    ]
    contracts.output(
        "phone_ws/step", examples={"running": steps[0], "done": steps[1], "failed": steps[3]},
        frame="step", socket=SOCKET, exact=("type", "state"), now_ms=WORLD_NOW_MS,
        description=(
            "One tool call of a turn this device opened (ADR 0231). It is sent `running` when "
            "the tool starts and again with the same `step` number as `done` or `failed`, with "
            "`ended_ms` set; `step` counts from 1 per turn. `label` is the line to show, in the "
            "brain's language; `started_ms` and `ended_ms` are the brain's clock in epoch ms."
        ),
    )


def _refusing(message: str) -> Any:  # noqa: ANN401 - a hook that raises
    def refuse(*_args: object) -> None:
        raise AttachmentRefused(413, message)

    return refuse


def _failing(*_args: object) -> Any:  # noqa: ANN401
    msg = "the host could not do it"
    raise RuntimeError(msg)


def test_the_error_frame_of_every_code_the_brain_gives(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """One example per ``code``, each from the thing that really raises it."""
    typed = {"type": "say", "utterance_id": "t1", "text": "hi", "spoken": False}
    files = ["0123456789abcdef0123456789abcdef"]
    errors: dict[str, dict[str, Any]] = {}

    def error_of(code: str, frames: list[Any], **options: Any) -> None:  # noqa: ANN401
        rig = _rig(tmp_path, code, **options)
        if code == "retry":
            with sqlite3.connect(rig.log) as conn:
                conn.execute(
                    "CREATE TRIGGER fail_one BEFORE INSERT ON events "
                    "WHEN NEW.type = 'surface.user_intent' "
                    "BEGIN SELECT RAISE(ABORT, 'disk is full'); END",
                )
        with rig.connect() as ws:
            ws.send_json({"type": "hello", "voice": code != "no_voice"})
            ws.receive_json()
            for frame in frames:
                if isinstance(frame, bytes):
                    ws.send_bytes(frame)
                else:
                    ws.send_json(frame)
            reply = ws.receive_json()
            while reply["type"] != "error":
                reply = ws.receive_json()
        errors[code] = reply

    with brain_at():
        error_of("bad_say", [{"type": "say"}])
        error_of("bad_ping", [{"type": "ping", "t_ns": "now"}])
        error_of("bad_located", [{"type": "located"}])
        error_of("no_voice", [b"\x00"])
        error_of("no_attachments", [{**typed, "attachments": files}])
        error_of(
            "attachment_refused", [{**typed, "attachments": files}],
            hub={"check_attachments": _refusing("that file is too big")},
        )
        error_of("retry", [typed])
        error_of(
            "quiet_failed",
            [{
                "type": "say", "utterance_id": "u1", "text": "勿扰", "spoken": True,
                "over_her": True,
            }],
            hub={"set_quiet": _failing},
        )
        error_of(
            "interrupt_failed",
            [{"type": "say", "utterance_id": "u1", "text": TURN_WORDS, "spoken": True,
              "over_her": True}],
            hub={"barge_in": _failing},
        )
    assert {code: frame["code"] for code, frame in errors.items()} == {
        code: code for code in errors
    }
    contracts.output(
        "phone_ws/error", examples=errors, frame="error", socket=SOCKET, exact=("type", "code"),
        description=(
            "A refusal or a failure, one example per code. `bad_say`, `bad_ping` and "
            "`bad_located` mean the frame was malformed and is not resent; `no_voice`, "
            "`no_attachments` and `attachment_refused` mean the host cannot take what was "
            "sent (the last carries the store's own sentence in `message`); `retry` means the "
            "host could not write the say and the phone sends it again; `quiet_failed` and "
            "`interrupt_failed` come beside a `said` that still follows. `message` is for "
            "the log, not for the screen."
        ),
    )

