"""Base abstractions shared by all smart-home device implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class SmartDevice(ABC):
    """Abstract base class for executable smart-home devices.

    Args:
        device_id: Unique identifier used by the device manager.
        name: Human-readable device name.
        device_type: Canonical device category such as `light` or `door_lock`.
        required_role: Minimum role required to control this device.
        is_available: Whether the device is currently reachable and usable.
    """

    def __init__(
        self,
        device_id: str,
        name: str,
        device_type: str,
        required_role: str = "guest",
        is_available: bool = True,
    ) -> None:
        """Store common device metadata."""

        self.device_id = device_id
        self.name = name
        self.device_type = device_type
        self.required_role = required_role
        self.is_available = is_available
        self.logger = LOGGER

    @abstractmethod
    def execute(self, action: str, value: Any | None = None) -> str:
        """Execute a device action and return a human-readable result."""

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Return the current device state and metadata as a dictionary."""

    def _ensure_available(self) -> None:
        """Raise an error when the device is unavailable."""

        if not self.is_available:
            raise RuntimeError(f"Device is unavailable: {self.device_id}")
