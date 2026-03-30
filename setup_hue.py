"""Interactive CLI utility for pairing with a Philips Hue Bridge and updating config.yaml."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import yaml

from devices.hue.hue_bridge import HueBridge, HueBridgeAuthenticationError, HueBridgeConnectionError
from devices.hue.hue_discovery import HueDiscovery

LOGGER = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config() -> dict[str, Any]:
    """Load the project config file."""

    with CONFIG_PATH.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def save_config(config: dict[str, Any]) -> None:
    """Persist the project config file."""

    with CONFIG_PATH.open("w", encoding="utf-8") as file_obj:
        yaml.safe_dump(config, file_obj, allow_unicode=True, sort_keys=False)


def choose_bridge_ip(discovery: HueDiscovery, manual_ip: str | None = None) -> str:
    """Choose a bridge IP from discovery or a manual fallback."""

    if manual_ip:
        result = discovery.validate_bridge(manual_ip)
        if not result.online:
            raise RuntimeError(f"Hue Bridge at {manual_ip} is unreachable.")
        return result.ip

    bridges = discovery.discover_bridges()
    if bridges:
        online_bridges = [bridge for bridge in bridges if bridge.online]
        if online_bridges:
            if len(online_bridges) == 1:
                return online_bridges[0].ip
            print("发现多个 Hue Bridge：")
            for index, bridge in enumerate(online_bridges, start=1):
                print(f"{index}. {bridge.ip}")
            selection = input("请选择 Bridge 编号: ").strip()
            return online_bridges[int(selection) - 1].ip

    manual_value = input("未自动发现 Bridge，请手动输入 Bridge IP: ").strip()
    if not manual_value:
        raise RuntimeError("Bridge IP is required.")
    result = discovery.validate_bridge(manual_value)
    if not result.online:
        raise RuntimeError(f"Hue Bridge at {manual_value} is unreachable.")
    return result.ip


def main() -> int:
    """Run the interactive Hue pairing flow."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Pair smart-home-voice-lock with a Philips Hue Bridge.")
    parser.add_argument("--ip", help="Optional manually specified Hue Bridge IP.")
    parser.add_argument(
        "--devicetype",
        default="smart-home-voice-lock#setup",
        help="Hue API devicetype value used during pairing.",
    )
    args = parser.parse_args()

    config = load_config()
    discovery = HueDiscovery(config)
    bridge_ip = choose_bridge_ip(discovery, manual_ip=args.ip)
    print(f"已找到 Hue Bridge: {bridge_ip}")
    input("请按下 Hue Bridge 的配对按钮，然后按回车继续...")

    bridge_config = config.setdefault("hue", {}).setdefault("bridge", {})
    bridge_config["ip"] = bridge_ip
    bridge = HueBridge(config)

    try:
        username = bridge.create_username(args.devicetype)
    except HueBridgeAuthenticationError as exc:
        print(f"配对失败：{exc}")
        return 1
    except HueBridgeConnectionError as exc:
        print(f"Hue Bridge 不可达：{exc}")
        return 1

    bridge_config["username"] = username
    save_config(config)
    print(f"已获取 Hue username: {username}")

    bridge = HueBridge(config)
    bridge.connect()
    lights = bridge.get_all_lights()
    groups = bridge.get_all_groups()
    scenes = bridge.get_all_scenes()
    print(f"扫描完成：{len(lights)} 个灯，{len(groups)} 个组，{len(scenes)} 个场景。")

    bridge.flash_all_lights()
    save_config(config)
    print("Hue 配对成功，所有灯已闪烁一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
