"""Bridge discovery helpers for locating Philips Hue bridges on the local network."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


@dataclass
class BridgeDiscoveryResult:
    """Result of discovering and validating a Hue Bridge.

    Attributes:
        ip: Bridge IP address on the local network.
        bridge_id: Optional bridge identifier returned by discovery.
        online: Whether the bridge responded to a config probe.
        config: Bridge config payload when available.
    """

    ip: str
    bridge_id: str | None
    online: bool
    config: dict[str, Any] | None


class HueDiscovery:
    """Discover and validate Philips Hue bridges.

    Args:
        config: Parsed application configuration containing the `hue.bridge`
            section.
    """

    def __init__(self, config: dict) -> None:
        """Initialize discovery settings from config."""

        hue_config = config.get("hue", {})
        bridge_config = hue_config.get("bridge", {})
        self.discovery_url = str(
            bridge_config.get("discovery_url", "https://discovery.meethue.com/")
        )
        self.timeout_seconds = float(bridge_config.get("timeout_seconds", 5.0))
        self.verify_ssl = bool(bridge_config.get("verify_ssl", False))
        self.allow_http_fallback = bool(bridge_config.get("allow_http_fallback", True))
        self.logger = LOGGER

    def discover_bridges(self) -> list[BridgeDiscoveryResult]:
        """Discover Hue bridges using the Hue cloud discovery endpoint.

        Returns:
            A list of discovered and validated bridge records.
        """

        try:
            response = requests.get(
                self.discovery_url,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            self.logger.warning("Failed to query Hue discovery endpoint: %s", exc)
            return []

        results: list[BridgeDiscoveryResult] = []
        for item in payload:
            ip = str(item.get("internalipaddress", "")).strip()
            if not ip:
                continue
            bridge_id = item.get("id")
            results.append(self.validate_bridge(ip, bridge_id=bridge_id))
        return results

    def validate_bridge(
        self,
        ip: str,
        bridge_id: str | None = None,
    ) -> BridgeDiscoveryResult:
        """Validate whether a discovered or manually supplied bridge is online.

        Args:
            ip: Candidate bridge IP address.
            bridge_id: Optional bridge ID from discovery.

        Returns:
            A bridge discovery result with validation status.
        """

        normalized_ip = ip.strip()
        if not normalized_ip:
            raise ValueError("Bridge IP must not be empty.")

        urls = [f"https://{normalized_ip}/api/config"]
        if self.allow_http_fallback:
            urls.append(f"http://{normalized_ip}/api/config")

        for url in urls:
            try:
                response = requests.get(
                    url,
                    timeout=self.timeout_seconds,
                    verify=self.verify_ssl,
                )
                response.raise_for_status()
                config = response.json()
                self.logger.info("Validated Hue Bridge at %s", normalized_ip)
                return BridgeDiscoveryResult(
                    ip=normalized_ip,
                    bridge_id=bridge_id,
                    online=True,
                    config=config,
                )
            except requests.RequestException as exc:
                self.logger.debug("Bridge validation failed for %s via %s: %s", normalized_ip, url, exc)

        self.logger.warning("Hue Bridge at %s is unreachable.", normalized_ip)
        return BridgeDiscoveryResult(
            ip=normalized_ip,
            bridge_id=bridge_id,
            online=False,
            config=None,
        )

    def choose_bridge(self, manual_ip: str | None = None) -> BridgeDiscoveryResult:
        """Return a validated bridge using discovery or a manual IP fallback.

        Args:
            manual_ip: Optional user-supplied IP address.

        Returns:
            The first validated bridge result.

        Raises:
            RuntimeError: If no reachable bridge can be found.
        """

        if manual_ip:
            result = self.validate_bridge(manual_ip)
            if result.online:
                return result
            raise RuntimeError(f"Hue Bridge at {manual_ip} is unreachable.")

        for result in self.discover_bridges():
            if result.online:
                return result

        raise RuntimeError("No reachable Hue Bridge found via discovery.")
