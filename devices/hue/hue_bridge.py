"""Bridge connection wrapper combining phue/phue2 with direct Hue REST access."""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any

import requests

from devices.hue.hue_discovery import HueDiscovery

LOGGER = logging.getLogger(__name__)


class HueBridgeConnectionError(RuntimeError):
    """Raised when the Hue Bridge cannot be reached."""


class HueBridgeAuthenticationError(RuntimeError):
    """Raised when the Hue Bridge rejects the configured username."""


class HueBridge:
    """Manage an authenticated connection to a Philips Hue Bridge.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        """Read bridge and rate-limit settings from config."""

        hue_config = config.get("hue", {})
        bridge_config = hue_config.get("bridge", {})
        request_limit_config = hue_config.get("request_limits", {})

        self.ip = str(bridge_config.get("ip", "")).strip() or None
        self.username = str(bridge_config.get("username", "")).strip() or None
        self.verify_ssl = bool(bridge_config.get("verify_ssl", False))
        self.timeout_seconds = float(bridge_config.get("timeout_seconds", 5.0))
        self.auto_discover = bool(bridge_config.get("auto_discover", True))
        self.allow_http_fallback = bool(bridge_config.get("allow_http_fallback", True))
        self.discovery = HueDiscovery(config)

        self._client: Any | None = None
        self._base_url: str | None = None
        self._session = requests.Session()
        self._rate_limits = {
            "lights": max(float(request_limit_config.get("lights_per_second", 10)), 0.1),
            "groups": max(float(request_limit_config.get("groups_per_second", 1)), 0.1),
            "default": max(float(request_limit_config.get("default_per_second", 10)), 0.1),
        }
        self._last_request_time = {
            "lights": 0.0,
            "groups": 0.0,
            "default": 0.0,
        }
        self._rate_lock = threading.Lock()
        self.logger = LOGGER

    def connect(self) -> None:
        """Connect to the bridge and validate the configured username.

        Raises:
            HueBridgeConnectionError: If the bridge is unreachable.
            HueBridgeAuthenticationError: If the username is missing or invalid.
        """

        if not self.ip and self.auto_discover:
            self.ip = self.discovery.choose_bridge().ip

        if not self.ip:
            raise HueBridgeConnectionError(
                "Hue Bridge IP is missing. Run setup_hue.py or configure hue.bridge.ip."
            )
        if not self.username:
            raise HueBridgeAuthenticationError(
                "Hue Bridge username is missing. Run setup_hue.py to pair first."
            )

        bridge_class = self._load_bridge_class()
        try:
            self._client = self._build_library_bridge(bridge_class)
            self._base_url = self._choose_base_url()
            self.request("GET", f"/api/{self.username}/config")
        except HueBridgeAuthenticationError:
            raise
        except Exception as exc:
            raise HueBridgeConnectionError(
                f"Failed to connect to Hue Bridge at {self.ip}: {exc}"
            ) from exc

        self.logger.info("Connected to Hue Bridge at %s", self.ip)

    def get_all_lights(self) -> dict[str, dict[str, Any]]:
        """Return all light resources from the bridge."""

        payload = self.request("GET", f"/api/{self.username}/lights")
        return payload if isinstance(payload, dict) else {}

    def get_all_groups(self) -> dict[str, dict[str, Any]]:
        """Return all group resources from the bridge."""

        payload = self.request("GET", f"/api/{self.username}/groups")
        return payload if isinstance(payload, dict) else {}

    def get_all_scenes(self) -> dict[str, dict[str, Any]]:
        """Return all scene resources from the bridge."""

        payload = self.request("GET", f"/api/{self.username}/scenes")
        return payload if isinstance(payload, dict) else {}

    def is_connected(self) -> bool:
        """Check whether the bridge is currently reachable and authenticated."""

        if not self._base_url or not self.username:
            return False

        try:
            self.request("GET", f"/api/{self.username}/config")
            return True
        except (HueBridgeConnectionError, HueBridgeAuthenticationError):
            return False

    def reconnect(self) -> None:
        """Reconnect to the bridge, rediscovering its IP when configured."""

        if self.auto_discover:
            try:
                discovery_result = self.discovery.choose_bridge()
                if discovery_result.ip != self.ip:
                    self.logger.warning(
                        "Hue Bridge IP changed from %s to %s.",
                        self.ip,
                        discovery_result.ip,
                    )
                self.ip = discovery_result.ip
            except RuntimeError as exc:
                raise HueBridgeConnectionError(str(exc)) from exc

        self._client = None
        self._base_url = None
        self.connect()

    def create_username(self, devicetype: str = "smart-home-voice-lock") -> str:
        """Create a new username by calling the bridge registration endpoint.

        Args:
            devicetype: Identifier sent to the bridge during pairing.

        Returns:
            The newly issued Hue username.

        Raises:
            HueBridgeConnectionError: If the bridge is unreachable.
            HueBridgeAuthenticationError: If the link button was not pressed.
        """

        if not self.ip:
            if self.auto_discover:
                self.ip = self.discovery.choose_bridge().ip
            else:
                raise HueBridgeConnectionError("Hue Bridge IP is missing.")

        base_url = self._choose_base_url()
        try:
            response = self._session.request(
                "POST",
                f"{base_url}/api",
                json={"devicetype": devicetype},
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise HueBridgeConnectionError(f"Failed to pair with Hue Bridge: {exc}") from exc

        username = self._extract_success_value(payload, "username")
        if not username:
            error_message = self._extract_error_message(payload)
            raise HueBridgeAuthenticationError(
                error_message or "Hue Bridge pairing failed. Press the bridge button and try again."
            )

        self.username = str(username)
        self.logger.info("Created Hue username for bridge %s", self.ip)
        return self.username

    def flash_all_lights(self) -> None:
        """Blink all discovered lights once to indicate setup success."""

        for light_id in self.get_all_lights():
            try:
                self.request(
                    "PUT",
                    f"/api/{self.username}/lights/{light_id}/state",
                    {"alert": "select"},
                )
            except RuntimeError as exc:
                self.logger.warning("Failed to flash Hue light %s: %s", light_id, exc)

    def request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Send a rate-limited REST request to the bridge."""

        if not self._base_url:
            self._base_url = self._choose_base_url()

        self._wait_for_rate_limit(path)
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                json=data,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            self.logger.warning("Hue Bridge request failed: %s %s (%s)", method, path, exc)
            raise HueBridgeConnectionError(
                f"Hue Bridge 不可达，请检查网络、IP 和 Bridge 电源状态。({exc})"
            ) from exc

        self._raise_for_hue_error(payload)
        return payload

    def _build_library_bridge(self, bridge_class: type) -> Any:
        """Instantiate the underlying phue/phue2 bridge wrapper."""

        last_error: Exception | None = None
        candidate_kwargs = (
            {"ip": self.ip, "username": self.username, "save_config": False},
            {"ip": self.ip, "username": self.username},
        )
        for kwargs in candidate_kwargs:
            try:
                return bridge_class(**kwargs)
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                break

        if last_error is None:
            raise HueBridgeConnectionError("Unable to instantiate Hue Bridge client.")
        raise last_error

    def _choose_base_url(self) -> str:
        """Pick an HTTPS base URL, falling back to HTTP when allowed."""

        if not self.ip:
            raise HueBridgeConnectionError("Hue Bridge IP is missing.")

        candidate_urls = [f"https://{self.ip}"]
        if self.allow_http_fallback:
            candidate_urls.append(f"http://{self.ip}")

        last_error: Exception | None = None
        for base_url in candidate_urls:
            try:
                response = self._session.request(
                    "GET",
                    f"{base_url}/api/config",
                    timeout=self.timeout_seconds,
                    verify=self.verify_ssl,
                )
                response.raise_for_status()
                return base_url
            except requests.RequestException as exc:
                last_error = exc
                continue

        raise HueBridgeConnectionError(
            f"Unable to reach Hue Bridge at {self.ip}: {last_error}"
        )

    def _wait_for_rate_limit(self, path: str) -> None:
        """Throttle requests according to Hue best-practice request rates."""

        bucket = "default"
        if "/lights" in path:
            bucket = "lights"
        elif "/groups" in path:
            bucket = "groups"

        min_interval = 1.0 / self._rate_limits[bucket]
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_time[bucket]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_time[bucket] = time.monotonic()

    def _raise_for_hue_error(self, payload: Any) -> None:
        """Interpret Hue API error payloads and raise typed exceptions."""

        if isinstance(payload, list):
            for item in payload:
                error = item.get("error") if isinstance(item, dict) else None
                if error is None:
                    continue
                description = str(error.get("description", "Unknown Hue API error"))
                error_type = int(error.get("type", -1))
                if error_type in {1, 7, 101}:
                    raise HueBridgeAuthenticationError(description)
                raise HueBridgeConnectionError(description)

    def _extract_success_value(self, payload: Any, key: str) -> str | None:
        """Extract a field from a Hue success payload list."""

        if not isinstance(payload, list):
            return None
        for item in payload:
            success = item.get("success") if isinstance(item, dict) else None
            if not isinstance(success, dict):
                continue
            for success_key, value in success.items():
                if success_key.endswith(f"/{key}"):
                    return str(value)
        return None

    def _extract_error_message(self, payload: Any) -> str | None:
        """Extract the first Hue API error description from a payload."""

        if not isinstance(payload, list):
            return None
        for item in payload:
            error = item.get("error") if isinstance(item, dict) else None
            if error:
                return str(error.get("description", "Unknown Hue API error"))
        return None

    def _load_bridge_class(self) -> type:
        """Load `Bridge` from phue2 first, then fall back to phue."""

        module_names = ("phue2", "phue")
        import_errors: list[str] = []
        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                import_errors.append(f"{module_name}: {exc}")
                continue

            bridge_class = getattr(module, "Bridge", None)
            if bridge_class is not None:
                return bridge_class

        raise HueBridgeConnectionError(
            "Neither phue2 nor phue is installed. Install phue2 on Python 3.10+ or use phue as fallback. "
            + "; ".join(import_errors)
        )
