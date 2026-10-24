"""ADR 0401: a turn from his watch says so, and an iPad is named his iPad.

The phone asks for its watch with ``via: "watch"`` on the real ask route, which writes the turn's
real opening row; the turn's state line is built from that row. An iPad says what it is in its
push registration, and the real whereabouts names it from the real registrations file.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import _MISHEARD, _interaction_line
from jarvis.runtime.turn.context import turn_device, whereabouts_lines
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import (
    emit_event,
    iter_events_of_types,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.state.push_tokens import RegistrationError, register_body, registrations
from tests.support.phone import here_daemon
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from pathlib import Path

PHONE_TOKEN = "a1" * 32
PAD_TOKEN = "b2" * 32


def _opening(log: Path, turn_id: str) -> Any:  # noqa: ANN401 - the log's own Event
    with closing(open_runtime_event_log(log)) as conn:
        return next(
            e for e in iter_events_of_types(conn, ("surface.user_intent",))
            if e.payload["turn_id"] == turn_id
        )


def _line(trigger: Any, device: str | None) -> str | None:  # noqa: ANN401 - the log's Event
    packet = SimpleNamespace(trigger_event=trigger)
    ctx = SimpleNamespace(routine_stream=None, record_sent_message=None, turn_device=device)
    return _interaction_line(packet, ctx)  # type: ignore[arg-type]


def test_words_said_to_his_watch_are_a_spoken_turn_shown_on_the_watch(tmp_path: Path) -> None:
    """She read 「这条是你在手机上打的字」 for his first words to the watch (2026-10-23 19:49)."""
    log, _key, token, _loopback, remote = here_daemon(tmp_path)
    phone = {"Authorization": f"Bearer {token}"}
    said = "能听见我说话吗"
    watch = remote.post("/inherent/ask", json={"text": said, "via": "watch"}, headers=phone)
    typed = remote.post("/inherent/ask", json={"text": said}, headers=phone)
    assert (watch.status_code, typed.status_code) == (200, 200)

    from_watch = _opening(log, watch.json()["turn_id"])
    # The turn stays the phone's: its device, and with it "here" and its cards (ADR 0337).
    with closing(open_runtime_event_log(log)) as conn:
        assert turn_origin(conn, watch.json()["turn_id"]) == ("cli_stdin", "iphone")
    assert from_watch.payload["via"] == "watch"
    assert _line(from_watch, "his phone (iphone)") == (
        "This turn: spoken to his watch, relayed by his phone (iphone); your answer is shown on"
        f" the watch, not read aloud.\n{_MISHEARD}"
    )

    from_phone = _opening(log, typed.json()["turn_id"])
    assert "via" not in from_phone.payload
    assert _line(from_phone, "his phone (iphone)") == (
        "This turn: typed on his phone (iphone); your answer is shown there, not read aloud."
    )


def test_an_ask_for_anyone_but_the_watch_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """A parameter is honoured or refused, never dropped."""
    log, _key, token, _loopback, remote = here_daemon(tmp_path)
    phone = {"Authorization": f"Bearer {token}"}
    for via in ("ipad", "", 1, ["watch"]):
        response = remote.post(
            "/inherent/ask", json={"text": "hello", "via": via}, headers=phone,
        )
        assert response.status_code == 400, via
    with closing(open_runtime_event_log(log)) as conn:
        assert list(iter_events_of_types(conn, ("surface.user_intent",))) == []


def _runtime(root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root.mkdir()
    (root / "settings.yaml").write_text("runtime:\n  role: brain\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(root.parent))
    return boot_runtime(root)


def _typed(runtime: Any, turn_id: str, device: str) -> None:  # noqa: ANN401
    emit_event(
        runtime.conn, type="surface.user_intent", ingestion_node=device,
        payload={"transcript": "hi", "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )


def test_an_ipad_that_says_so_is_his_ipad_and_stays_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It registers for pushes as the phone does; only what it says it is tells them apart."""
    brain = _runtime(tmp_path / "b", monkeypatch)
    root = brain.runtime_paths.root
    try:
        for name in ("iphone", "ipad"):
            pair_device(root, name)
        register_body(root, "iphone", {
            "device_token": PHONE_TOKEN, "environment": "sandbox", "idiom": "phone",
        })
        register_body(root, "ipad", {
            "device_token": PAD_TOKEN, "environment": "sandbox", "idiom": "pad",
        })
        _typed(brain, "T1", "iphone")
        _typed(brain, "T2", "ipad")
        (devices,) = whereabouts_lines(brain, None)
        assert devices.endswith(
            ". His devices: ipad, his iPad, not connected now;"
            " iphone, his phone, not connected now.",
        )
        assert turn_device(brain, "T1") == "his phone (iphone)"
        assert turn_device(brain, "T2") == "his iPad (ipad)"

        # A registration that leaves it out (a Live Activity token, an older build) keeps it.
        register_body(root, "ipad", {"device_token": PAD_TOKEN, "environment": "production"})
        assert registrations(root)["ipad"].idiom == "pad"
        assert turn_device(brain, "T2") == "his iPad (ipad)"
    finally:
        brain.conn.close()


def test_a_phone_that_never_said_is_still_his_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registrations made before ADR 0401 carry no idiom."""
    brain = _runtime(tmp_path / "b", monkeypatch)
    root = brain.runtime_paths.root
    try:
        pair_device(root, "iphone")
        register_body(root, "iphone", {"device_token": PHONE_TOKEN, "environment": "sandbox"})
        _typed(brain, "T1", "iphone")
        assert registrations(root)["iphone"].idiom is None
        assert turn_device(brain, "T1") == "his phone (iphone)"
    finally:
        brain.conn.close()


@pytest.mark.parametrize("idiom", ["tablet", "Pad", "", None, 1])
def test_an_idiom_is_phone_or_pad(tmp_path: Path, idiom: object) -> None:
    """Anything else is refused, and the message repeats no value; null is no idiom."""
    body = {"device_token": PAD_TOKEN, "environment": "sandbox", "idiom": idiom}
    if idiom is None:
        register_body(tmp_path, "ipad", body)
        assert registrations(tmp_path)["ipad"].idiom is None
        return
    with pytest.raises(RegistrationError) as caught:
        register_body(tmp_path, "ipad", body)
    assert str(caught.value) == 'idiom is "phone" or "pad"'
