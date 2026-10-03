"""ADR 0147 — the reSpeaker board's look: what the page may save, when and how it reaches the board.

The board is a fake that keeps the registers a vendor control transfer would
set, so each check asserts what lands on the board, in which order, and when
nothing is written at all.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any

import pytest
import usb.util
from fastapi.testclient import TestClient

from jarvis.runtime.settings import Settings, apply_settings
from jarvis.surface import respeaker_board
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

LOOK: dict[str, Any] = {
    "light": "direction", "brightness": 0.4, "speed": 8, "color": "#002040",
    "direction_colors": ["#002040", "#00c066"], "ring_colors": ["#002040"] * 12,
    "headphone": 8, "lineout": 8,
}


class FakeBoard:
    """Registers by name; ``writes`` in the order they arrived."""

    def __init__(self) -> None:
        """Zeroed registers, firmware 2.1.1, someone talking at 251 degrees."""
        self.regs: dict[str, list[int]] = {
            name: [0] * count for name, (_r, _c, count, _f) in respeaker_board.REGISTERS.items()
        }
        self.regs["VERSION"] = [2, 1, 1]
        self.regs["DOA_VALUE"] = [251, 1]
        self.writes: list[str] = []

    def _name(self, resid: int, cmd: int) -> str:
        return next(name for name, (r, c, _n, _f) in respeaker_board.REGISTERS.items()
                    if (r, c) == (resid, cmd & 0x7F))

    def ctrl_transfer(self, req_type: int, _req: int, value: int, index: int,
                      data: Any, _timeout: int) -> bytes:  # noqa: ANN401 — length or payload.
        """A read answers status 0 and the register; a write keeps the payload."""
        name = self._name(index, value)
        _r, _c, count, fmt = respeaker_board.REGISTERS[name]
        if req_type == 0xC0:
            return bytes([0]) + struct.pack("<" + fmt * count, *self.regs[name])
        self.regs[name] = list(struct.unpack("<" + fmt * count, bytes(data)))
        self.writes.append(name)
        return b""


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> FakeBoard:
    """The fake plugged in where pyusb would find the board."""
    fake = FakeBoard()
    monkeypatch.setattr(respeaker_board, "_find", lambda: fake)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda _dev: None)
    return fake


def test_direction_dims_its_colors_and_sets_the_effect_last(board: FakeBoard) -> None:
    """Direction has no brightness of its own: 40% scales both colors, then LED_EFFECT is 4."""
    assert respeaker_board.ensure(LOOK) == "applied"
    assert board.regs["LED_DOA_COLOR"] == [0x000D1A, 0x004D29]
    assert board.regs["LED_EFFECT"] == [4]
    assert board.writes == [
        "LED_DOA_COLOR", "LED_EFFECT", "AIC3104_HP_LEVEL", "AIC3104_LINEOUT_LEVEL",
    ]
    board.writes.clear()
    assert respeaker_board.ensure(LOOK) == "kept"
    assert board.writes == []


def test_breath_uses_the_boards_brightness_and_its_full_color(board: FakeBoard) -> None:
    """Breath and rainbow have the board's own brightness and speed; the color stays whole."""
    respeaker_board.ensure({**LOOK, "light": "breath", "brightness": 0.5, "speed": 3})
    assert board.regs["LED_BRIGHTNESS"] == [128]
    assert board.regs["LED_SPEED"] == [3]
    assert board.regs["LED_COLOR"] == [0x002040]
    assert board.regs["LED_EFFECT"] == [1]


def test_a_reset_board_gets_the_look_back(board: FakeBoard) -> None:
    """A replug or the boot animation leaves other values: the next check writes them again."""
    respeaker_board.ensure(LOOK)
    board.regs["LED_EFFECT"] = [2]  # the board's own rainbow at boot
    assert respeaker_board.ensure(LOOK) == "applied"
    assert board.regs["LED_EFFECT"] == [4]


def test_no_board_means_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unplugged: nothing written, and the page hears it is not there."""
    monkeypatch.setattr(respeaker_board, "_find", lambda: None)
    assert respeaker_board.ensure(LOOK) == "absent"
    assert respeaker_board.status() == {
        "present": False, "firmware": None, "direction": None, "speech": False,
    }


@pytest.mark.usefixtures("board")
def test_status_names_the_firmware_and_the_direction() -> None:
    """GET /inherent/board: what the page's status rows show."""
    assert respeaker_board.status() == {
        "present": True, "firmware": "2.1.1", "direction": 251, "speech": True,
    }
    app = create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
        board_status=lambda: _done(respeaker_board.status()),
    ))
    assert TestClient(app).get("/inherent/board").json()["firmware"] == "2.1.1"


async def _done(value: dict[str, Any]) -> dict[str, Any]:
    return value


def test_the_board_is_left_alone_until_a_look_is_saved(tmp_path: Path) -> None:
    """Nothing saved: no look to keep. A save reaches the board at once, with no restart banner."""
    settings = Settings(tmp_path, apply_settings({}, tmp_path), lambda _kind: [])
    put: list[dict[str, Any]] = []
    settings.on_board = put.append
    assert settings.board() is None
    answer = settings.update(
        {"board_brightness": 0.4, "board_direction_colors": ["#002040", "#00C066"]},
    )
    assert put == [{**LOOK, "speed": 8, "brightness": 0.4}]
    assert answer["values"]["board_direction_colors"] == ["#002040", "#00c066"]
    assert answer["restart_pending"] is False
    # The next boot keeps it.
    assert Settings(tmp_path, apply_settings({}, tmp_path), lambda _kind: []).board() == put[0]


@pytest.mark.parametrize(("key", "value"), [
    ("board_light", "disco"), ("board_brightness", 1.5), ("board_speed", 0),
    ("board_headphone", 10), ("board_headphone", 4.0), ("board_color", "red"),
    ("board_direction_colors", ["#002040"]), ("board_ring_colors", ["#002040"] * 11),
])
def test_a_look_the_board_cannot_take_is_refused(tmp_path: Path, key: str, value: object) -> None:
    """A value outside what the registers hold is a 400, never a write."""
    settings = Settings(tmp_path, apply_settings({}, tmp_path), lambda _kind: [])
    with pytest.raises(ValueError, match=key):
        settings.update({key: value})
