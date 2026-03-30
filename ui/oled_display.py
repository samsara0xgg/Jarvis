"""OLED display controller with state machine.

Listens to the EventBus for Jarvis state changes and renders the
appropriate animation frames on a 128x64 OLED (or a luma emulator
for desktop development).

Graceful degradation: if luma is not installed the display becomes a
silent no-op so the rest of Jarvis still works.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from core.event_bus import EventBus

LOGGER = logging.getLogger(__name__)

# Valid display states
_VALID_STATES = {"idle", "listening", "thinking", "speaking", "face"}


class OledDisplay:
    """OLED display controller with state machine.

    Listens to event_bus for state changes and renders appropriate frames.
    Runs frame refresh in a background thread.
    """

    def __init__(self, config: dict, event_bus: EventBus) -> None:
        oled_cfg = config.get("oled", {})
        self._width: int = int(oled_cfg.get("width", 128))
        self._height: int = int(oled_cfg.get("height", 64))
        self._fps: int = int(oled_cfg.get("fps", 15))
        self._driver: str = str(oled_cfg.get("driver", "ssd1306"))
        self._emulator: bool = bool(oled_cfg.get("emulator", False))
        self._i2c_port: int = int(oled_cfg.get("i2c_port", 1))
        self._i2c_address: int = int(oled_cfg.get("i2c_address", 0x3C))

        self._event_bus = event_bus
        self._device: Any = None
        self._available = False

        # State machine
        self._lock = threading.Lock()
        self._state: str = "idle"
        self._frame_num: int = 0
        self._speaking_text: str = ""
        self._scroll_offset: int = 0
        self._idle_context: dict[str, Any] = {}

        # Thread control
        self._running = False
        self._thread: threading.Thread | None = None

        # Try to create the luma device
        self._device = self._create_device()
        if self._device is not None:
            self._available = True
            LOGGER.info(
                "OLED display ready (%s, %dx%d, %d fps, emulator=%s)",
                self._driver, self._width, self._height, self._fps, self._emulator,
            )
        else:
            LOGGER.warning(
                "OLED display unavailable — luma not installed. "
                "Display will be a no-op.",
            )

        # Subscribe to state changes
        self._event_bus.on("jarvis.state_changed", self._on_state_changed)

    # ------------------------------------------------------------------
    # Device creation
    # ------------------------------------------------------------------

    def _create_device(self) -> Any:
        """Attempt to create a luma OLED device or emulator.

        Returns the device object, or None if luma is not installed.
        """
        if self._emulator:
            return self._create_emulator_device()
        return self._create_hardware_device()

    def _create_emulator_device(self) -> Any:
        """Create a luma emulator device (for Mac / desktop development)."""
        try:
            from luma.emulator.device import pygame as pygame_device
            from luma.core.render import canvas as _  # noqa: F401 — verify luma.core

            device = pygame_device(
                width=self._width,
                height=self._height,
                transform="scale2x",
                mode="1",
            )
            return device
        except ImportError:
            LOGGER.debug("luma.emulator not installed.")
            return None
        except Exception as exc:
            LOGGER.debug("Failed to create emulator device: %s", exc)
            return None

    def _create_hardware_device(self) -> Any:
        """Create a real luma.oled device over I2C."""
        try:
            from luma.core.interface.serial import i2c
            if self._driver == "sh1106":
                from luma.oled.device import sh1106 as DeviceClass
            else:
                from luma.oled.device import ssd1306 as DeviceClass

            serial = i2c(port=self._i2c_port, address=self._i2c_address)
            device = DeviceClass(serial, width=self._width, height=self._height)
            return device
        except ImportError:
            LOGGER.debug("luma.oled not installed.")
            return None
        except Exception as exc:
            LOGGER.debug("Failed to create hardware OLED device: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the display refresh loop."""
        if not self._available:
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._render_loop, daemon=True, name="oled-render",
        )
        self._thread.start()
        LOGGER.info("OLED render thread started.")

    def stop(self) -> None:
        """Stop the display and clean up."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        self._event_bus.off("jarvis.state_changed", self._on_state_changed)

        if self._device is not None:
            try:
                self._device.hide()
            except Exception:
                pass
        LOGGER.info("OLED display stopped.")

    def set_state(self, state: str) -> None:
        """Change display state: idle, listening, thinking, speaking, face."""
        if state not in _VALID_STATES:
            LOGGER.warning("Ignoring unknown OLED state: %s", state)
            return
        with self._lock:
            if self._state != state:
                self._state = state
                self._frame_num = 0
                self._scroll_offset = 0
                LOGGER.debug("OLED state -> %s", state)

    def set_speaking_text(self, text: str) -> None:
        """Set text to display in speaking mode."""
        with self._lock:
            self._speaking_text = text
            self._scroll_offset = 0

    def set_idle_context(self, context: dict[str, Any]) -> None:
        """Update idle screen context (weather, etc)."""
        with self._lock:
            self._idle_context.update(context)

    # ------------------------------------------------------------------
    # Event bus callback
    # ------------------------------------------------------------------

    def _on_state_changed(self, data: dict | None) -> None:
        """Event bus callback for jarvis.state_changed."""
        if not isinstance(data, dict):
            return
        state = data.get("state")
        if state is not None:
            self.set_state(str(state))

    # ------------------------------------------------------------------
    # Render loop
    # ------------------------------------------------------------------

    def _render_loop(self) -> None:
        """Background thread: render frames at configured FPS."""
        from PIL import Image, ImageDraw

        from ui.oled_frames import (
            render_face_frame,
            render_idle_frame,
            render_listening_frame,
            render_speaking_frame,
            render_thinking_frame,
        )

        interval = 1.0 / max(self._fps, 1)

        while self._running:
            start = time.monotonic()

            # Snapshot current state under lock
            with self._lock:
                state = self._state
                frame_num = self._frame_num
                speaking_text = self._speaking_text
                scroll_offset = self._scroll_offset
                idle_context = dict(self._idle_context)
                self._frame_num += 1

            # Create a fresh 1-bit image
            image = Image.new("1", (self._width, self._height), "black")
            draw = ImageDraw.Draw(image)

            # Dispatch to the appropriate renderer
            if state == "idle":
                render_idle_frame(draw, self._width, self._height, idle_context)
            elif state == "listening":
                render_listening_frame(draw, self._width, self._height, frame_num)
            elif state == "thinking":
                render_thinking_frame(draw, self._width, self._height, frame_num)
            elif state == "speaking":
                render_speaking_frame(
                    draw, self._width, self._height, speaking_text, scroll_offset,
                )
                # Auto-scroll for long text
                with self._lock:
                    self._scroll_offset += 1
            elif state == "face":
                render_face_frame(draw, self._width, self._height, frame_num)

            # Push to device
            try:
                self._device.display(image)
            except Exception as exc:
                LOGGER.debug("OLED display error: %s", exc)

            # Maintain target FPS
            elapsed = time.monotonic() - start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
