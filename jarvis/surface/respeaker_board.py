"""The reSpeaker XVF3800's own controls over USB (ADR 0147): lights, output levels, direction.

Vendor control transfers to the board's servicers, the register table Seeed's
``xvf_host.py`` uses on firmware 2.1.1. Everything here lives in the board's
RAM: a replug, a re-enumeration or a reboot puts the factory values back, so
:func:`ensure` reads the registers and rewrites them only when they differ.
Nothing here saves to the board's flash (``SAVE_CONFIGURATION``) or touches
its audio processing.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)
VENDOR, PRODUCT = 0x2886, 0x001A
# LED_EFFECT's values, in order: 0 off .. 5 one color per light.
LIGHTS = ("off", "breath", "rainbow", "solid", "direction", "ring")
# name: (resource id, command id, count, struct format)
REGISTERS: dict[str, tuple[int, int, int, str]] = {
    "VERSION": (48, 0, 3, "B"),
    "AIC3104_HP_LEVEL": (48, 11, 1, "B"),
    "AIC3104_LINEOUT_LEVEL": (48, 12, 1, "B"),
    "LED_EFFECT": (20, 12, 1, "B"),
    "LED_BRIGHTNESS": (20, 13, 1, "B"),
    "LED_SPEED": (20, 15, 1, "B"),
    "LED_COLOR": (20, 16, 1, "I"),
    "LED_DOA_COLOR": (20, 17, 2, "I"),
    "DOA_VALUE": (20, 18, 2, "H"),
    "LED_RING_COLOR": (20, 19, 12, "I"),
}
_BUSY = 64  # the servicer has not answered yet: ask again
_TIMEOUT_MS = 1000
_LOCK = threading.Lock()  # the page's save, the watch and the status poll share the board


def _find() -> Any:  # noqa: ANN401 — a pyusb Device, untyped.
    import libusb_package  # noqa: PLC0415 — loaded on demand, like the voice wheels.

    return libusb_package.find(idVendor=VENDOR, idProduct=PRODUCT)


def _read(dev: Any, name: str) -> list[int]:  # noqa: ANN401 — a pyusb Device.
    resid, cmd, count, fmt = REGISTERS[name]
    size = struct.calcsize("<" + fmt)
    for _ in range(100):
        answer = bytes(dev.ctrl_transfer(0xC0, 0, 0x80 | cmd, resid, count * size + 1, _TIMEOUT_MS))
        if answer[0] == 0:
            return list(struct.unpack("<" + fmt * count, answer[1 : 1 + count * size]))
        if answer[0] != _BUSY:
            msg = f"{name}: status {answer[0]}"
            raise OSError(msg)
    msg = f"{name}: still busy"
    raise OSError(msg)


def _write(dev: Any, name: str, values: list[int]) -> None:  # noqa: ANN401 — a pyusb Device.
    resid, cmd, count, fmt = REGISTERS[name]
    dev.ctrl_transfer(0x40, 0, cmd, resid, struct.pack("<" + fmt * count, *values), _TIMEOUT_MS)


def _rgb(color: str, scale: float) -> int:
    """``#rrggbb`` as the board's 0xRRGGBB, each channel scaled (gamma on: it reads linear)."""
    value = int(color[1:], 16)
    return sum(round(((value >> shift) & 0xFF) * scale) << shift for shift in (0, 8, 16))


def registers(look: Mapping[str, Any]) -> dict[str, list[int]]:
    """The register values for a saved look, in write order.

    ``look`` holds ``light``, ``brightness`` (0..1), ``speed``, ``color``,
    ``direction_colors``, ``ring_colors``, ``headphone`` and ``lineout``.
    The board's own brightness only drives breath and rainbow, so solid,
    direction and ring are dimmed by scaling their colors.
    """
    light, brightness = LIGHTS.index(look["light"]), float(look["brightness"])
    wanted: dict[str, list[int]] = {}
    if look["light"] in ("breath", "rainbow"):
        wanted["LED_BRIGHTNESS"] = [round(255 * brightness)]
        wanted["LED_SPEED"] = [int(look["speed"])]
    if look["light"] == "breath":
        wanted["LED_COLOR"] = [_rgb(look["color"], 1.0)]
    if look["light"] == "solid":
        wanted["LED_COLOR"] = [_rgb(look["color"], brightness)]
    if look["light"] == "direction":
        wanted["LED_DOA_COLOR"] = [_rgb(one, brightness) for one in look["direction_colors"]]
    if look["light"] == "ring":
        wanted["LED_RING_COLOR"] = [_rgb(one, brightness) for one in look["ring_colors"]]
    # After the colors: writing a color register can switch the effect (seen on 2.1.1).
    wanted["LED_EFFECT"] = [light]
    wanted["AIC3104_HP_LEVEL"] = [int(look["headphone"])]
    wanted["AIC3104_LINEOUT_LEVEL"] = [int(look["lineout"])]
    return wanted


def ensure(look: Mapping[str, Any]) -> str:
    """Put ``look`` on the board if it is not there: ``applied``, ``kept`` or ``absent``."""
    import usb.core  # noqa: PLC0415 — loaded on demand.
    import usb.util  # noqa: PLC0415

    wanted = registers(look)
    with _LOCK:
        dev = None
        try:
            dev = _find()
            if dev is None:
                return "absent"
            if all(_read(dev, name) == values for name, values in wanted.items()):
                return "kept"
            for name, values in wanted.items():
                _write(dev, name, values)
        except (usb.core.USBError, OSError) as exc:
            LOGGER.debug("reSpeaker board: %s: %s", type(exc).__name__, exc)
            return "absent"
        else:
            return "applied"
        finally:
            if dev is not None:
                usb.util.dispose_resources(dev)


def status() -> dict[str, Any]:
    """``{present, firmware, direction, speech}`` for the Settings page, in board degrees."""
    import usb.core  # noqa: PLC0415 — loaded on demand.
    import usb.util  # noqa: PLC0415

    gone: dict[str, Any] = {"present": False, "firmware": None, "direction": None, "speech": False}
    with _LOCK:
        dev = None
        try:
            dev = _find()
            if dev is None:
                return gone
            direction, speech = _read(dev, "DOA_VALUE")
            version = ".".join(str(part) for part in _read(dev, "VERSION"))
        except (usb.core.USBError, OSError) as exc:
            LOGGER.debug("reSpeaker board: %s: %s", type(exc).__name__, exc)
            return gone
        else:
            return {"present": True, "firmware": version, "direction": direction,
                    "speech": bool(speech)}
        finally:
            if dev is not None:
                usb.util.dispose_resources(dev)
