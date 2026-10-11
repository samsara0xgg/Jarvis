"""ADR 0227: every setting has an owner, a terminal reports the brain's, the owner decides.

Real table, real files in a temporary runtime root, the hub's own ``attach`` for the hello, and
the notice routes' own callables for the card and its answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.runtime import _BRAIN_OVERRIDES
from jarvis.runtime.inherent_loop import _settings_notice_deps
from jarvis.runtime.settings import PATHS
from jarvis.runtime.settings_owner import (
    PRESENT,
    SettingsDrift,
    check_own,
    leaf_paths,
    owner_of,
    release,
    terminal_report,
)
from jarvis.state.device_tokens import pair_device, unpair_device
from jarvis.surface.inherent_server import _notices_for
from jarvis.surface.terminal_link import (
    SETTINGS_NOTICE,
    TerminalHub,
    _parse_hello,
    run_terminal_client,
)

if TYPE_CHECKING:
    from pathlib import Path

CONFIG = {"observer": {"usage": {"enabled": False}}, "memory": {"retain_audio": True}}


def test_every_setting_has_an_owner() -> None:
    """A new key in config/jarvis.yaml or on the page fails here until someone decides its owner."""
    from pathlib import Path  # noqa: PLC0415

    shipped = yaml.safe_load(
        (Path(__file__).parents[2] / "config" / "jarvis.yaml").read_text(encoding="utf-8"),
    )
    missing = sorted(path for path in leaf_paths(shipped) if owner_of(path) is None)
    missing += sorted(".".join(path) for path in PATHS.values() if owner_of(".".join(path)) is None)
    assert missing == []


def test_what_a_brain_forces_off_is_device_owned() -> None:
    """`_BRAIN_OVERRIDES` and the table agree: a brain overrides only what a terminal owns."""
    forced = leaf_paths({
        group: {key: (0 if value is None else value) for key, value in block.items()}
        for group, block in _BRAIN_OVERRIDES.items()
    })
    # a block replaced by None is named by its own path
    forced |= {f"{group}.{key}" for group, block in _BRAIN_OVERRIDES.items()
               for key, value in block.items() if value is None}
    assert forced
    assert {path: owner_of(path) for path in forced if owner_of(path) != "device"} == {}


def test_a_terminal_reports_only_brain_side_plain_values(tmp_path: Path) -> None:
    """Device keys stay home; a secret-looking key is reported as present, never its value."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({
        "observer": {"usage": {"enabled": True}, "repos": ["~/x"]},
        "llm": {"presets": {"luna": {"api_key_env": "SOME_ENV_NAME"}}},
        "realtime": {"wake_threshold": 0.9},
        "runtime": {"role": "all"},
        "onboarded": "2026-10-10",
    }), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({"keep_audio": False}), encoding="utf-8")
    assert terminal_report(settings, tmp_path) == {
        "llm.presets.luna.api_key_env": PRESENT,
        "memory.retain_audio": False,
        "observer.usage.enabled": True,
        "onboarded": "2026-10-10",
    }
    assert "SOME_ENV_NAME" not in json.dumps(terminal_report(settings, tmp_path))


def test_each_side_warns_once_of_the_other_sides_keys_and_never_prints_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning per side naming the keys; the values (a path here) never reach the log."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({
        "observer": {"repos": ["~/secret-path"], "usage": {"enabled": True}},
        "onboarded": "x",
    }), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert check_own("brain", settings, tmp_path) == ["observer.repos"]
        assert check_own("terminal", settings, tmp_path) == ["onboarded"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]
    assert "secret-path" not in caplog.text


def test_a_hello_carries_the_settings_and_a_bad_one_carries_none() -> None:
    """The brain keeps a mapping of key paths and drops anything else."""
    hello = {"type": "hello", "tools": ["read_file"], "settings": {"observer.usage.enabled": True}}
    assert _parse_hello(json.dumps(hello)).settings == {"observer.usage.enabled": True}  # type: ignore[union-attr]
    assert _parse_hello(json.dumps({**hello, "settings": [1]})).settings is None  # type: ignore[union-attr]
    assert _parse_hello(json.dumps({"type": "hello", "tools": []})).settings is None  # type: ignore[union-attr]


async def _send(_frame: str) -> None:
    return None


def _hello(hub: TerminalHub, name: str, settings: dict[str, Any]) -> None:
    async def connect() -> None:
        hub.attach(name, frozenset(), _send, settings=settings)

    asyncio.run(connect())


def test_a_difference_becomes_one_card_for_that_device_and_nothing_changes_by_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning and one card per change, for one device; no file moves until a choice."""
    pair_device(tmp_path, "macbook")
    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text("language: en\n", encoding="utf-8")
    drift = SettingsDrift(tmp_path, yaml_path, CONFIG)
    hub = TerminalHub()
    hub.on_settings = drift.on_report
    report = {"observer.usage.enabled": True, "memory.retain_audio": True, "llm.x.api_key": PRESENT}

    with caplog.at_level(logging.WARNING):
        _hello(hub, "macbook", report)
    (card,) = drift.notices()
    assert card["id"] == f"{SETTINGS_NOTICE}macbook"
    assert card["device"] == "macbook"
    assert card["keys"] == ["llm.x.api_key", "observer.usage.enabled"]  # retain_audio is equal
    assert [c["id"] for c in card["choices"]] == ["device", "brain"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "True" not in caplog.text
    assert yaml_path.read_text(encoding="utf-8") == "language: en\n"

    # only the device it belongs to is shown it (ADR 0218); every other reads none
    body = {"notices": [card, {"id": "j", "device": None}]}
    assert [n["id"] for n in _notices_for(body, "macbook")["notices"]] == [card["id"], "j"]
    assert [n["id"] for n in _notices_for(body, "iphone")["notices"]] == ["j"]

    # a reconnect with the same files does not raise it again, once it is dismissed
    assert drift.act(card["id"], "dismissed", None) is False
    _hello(hub, "macbook", report)
    assert drift.notices() == []
    # but changed values do
    _hello(hub, "macbook", {**report, "observer.usage.enabled": False, "onboarded": "x"})
    assert [c["keys"] for c in drift.notices()] == [["llm.x.api_key", "onboarded"]]
    # files that no longer differ clear it, and unpairing forgets the device
    _hello(hub, "macbook", {})
    assert drift.notices() == []
    unpair_device(tmp_path, "macbook")
    assert "macbook" not in json.loads(
        (tmp_path / "terminal-settings.json").read_text(encoding="utf-8"),
    )["devices"]


def test_choosing_the_terminals_values_writes_the_brains_files_after_a_backup(
    tmp_path: Path,
) -> None:
    """Plain values land in the brain's yaml or page store; secrets are never copied."""
    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text("language: en\nobserver:\n  poll_interval_s: 30\n", encoding="utf-8")
    drift = SettingsDrift(tmp_path, yaml_path, CONFIG)
    drift.on_report("macbook", {
        "observer.usage.enabled": True, "keep_audio": 1, "llm.x.api_key": PRESENT,
        "memory.audio_retention_days": 30,
    })
    card_id = drift.notices()[0]["id"]
    assert drift.act(card_id, "seen", None) is False
    assert drift.act(card_id, "feedback", "device") is True  # a restart brings it in
    saved = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert saved == {
        "language": "en",
        "observer": {"poll_interval_s": 30, "usage": {"enabled": True}},
    }
    assert [p.read_text(encoding="utf-8") for p in tmp_path.glob("settings.yaml.bak-*")] == [
        "language: en\nobserver:\n  poll_interval_s: 30\n",
    ]
    # a page key goes to the page store, where the page would have put it; a secret is never copied
    page = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert page == {"audio_days": 30}
    assert drift.notices() == []
    with pytest.raises(LookupError):
        drift.act("settings-nobody", "feedback", "device")


def test_choosing_the_brains_values_changes_no_file_and_the_terminal_releases_its_copy(
    tmp_path: Path,
) -> None:
    """The brain keeps its files; the terminal drops only its brain-owned keys, backed up."""
    brain_yaml = tmp_path / "brain" / "settings.yaml"
    brain_yaml.parent.mkdir()
    brain_yaml.write_text("language: en\n", encoding="utf-8")
    drift = SettingsDrift(brain_yaml.parent, brain_yaml, CONFIG)
    drift.on_report("macbook", {"observer.usage.enabled": True})
    assert drift.act(drift.notices()[0]["id"], "feedback", "brain") is False
    assert brain_yaml.read_text(encoding="utf-8") == "language: en\n"
    assert drift.notices() == []

    mac = tmp_path / "mac"
    mac.mkdir()
    (mac / "settings.yaml").write_text(yaml.safe_dump({
        "observer": {"usage": {"enabled": True}, "repos": ["~/x"]},
        "llm": {"presets": {"luna": {"model": "m"}}, "provider": "openai"},
        "onboarded": "2026-10-10",
    }), encoding="utf-8")
    (mac / "settings.json").write_text(
        json.dumps({"tts_voice": "Chinese (Mandarin)_Sweet_Lady", "wake_threshold": 0.9}),
        encoding="utf-8",
    )
    assert release(mac / "settings.yaml", mac) == [
        "llm.presets.luna.model", "llm.provider", "onboarded", "realtime.tts_voice",
    ]
    # a key owned by both stays, as does a device key; only the brain's go
    assert yaml.safe_load((mac / "settings.yaml").read_text(encoding="utf-8")) == {
        "observer": {"usage": {"enabled": True}, "repos": ["~/x"]},
    }
    assert json.loads((mac / "settings.json").read_text(encoding="utf-8")) == {
        "wake_threshold": 0.9,
    }
    assert len(list(mac.glob("settings.*.bak-*"))) == 2


def test_the_card_rides_the_notice_routes_and_restarts_only_when_idle(tmp_path: Path) -> None:
    """The card is served beside the others, and its answer reaches the drift, not job mail."""
    drift = SettingsDrift(tmp_path, tmp_path / "settings.yaml", CONFIG)
    drift.on_report("macbook", {"observer.usage.enabled": True})
    seen: list[str] = []

    async def mail_read() -> dict[str, Any]:
        return {"notices": [{"id": "reminder-1"}], "hold": None}

    async def mail_act(notice_id: str, action: str, reaction: str | None) -> None:
        seen.append(f"{notice_id}:{action}:{reaction}")

    deps = _settings_notice_deps(
        {"notices_read": mail_read, "notice_act": mail_act}, drift, lambda: True,
    )

    async def run() -> list[str]:
        ids = [n["id"] for n in (await deps["notices_read"]())["notices"]]
        await deps["notice_act"]("reminder-1", "seen", None)
        await deps["notice_act"](f"{SETTINGS_NOTICE}macbook", "feedback", "brain")
        return ids

    assert asyncio.run(run()) == [f"{SETTINGS_NOTICE}macbook", "reminder-1"]
    assert seen == ["reminder-1:seen:None"]
    assert drift.notices() == []


def test_a_real_terminals_hello_reaches_the_brains_drift_over_the_socket(tmp_path: Path) -> None:
    """The settings a terminal client is given ride its hello to the brain's `on_settings`."""
    from tests.integration.test_terminal_link import _Brain, _wait_for  # noqa: PLC0415

    token = pair_device(tmp_path, "macbook")
    drift = SettingsDrift(tmp_path, tmp_path / "settings.yaml", CONFIG)
    with _Brain(tmp_path) as brain:
        brain.hub.on_settings = drift.on_report
        loop = asyncio.new_event_loop()
        task: list[asyncio.Task[None]] = []

        async def client() -> None:
            task.append(asyncio.create_task(run_terminal_client(
                brain.url, token, tools=frozenset({"read_file"}), execute=lambda *_: {},
                settings={"observer.usage.enabled": True},
            )))
            with contextlib.suppress(asyncio.CancelledError):
                await task[0]

        thread = threading.Thread(target=lambda: loop.run_until_complete(client()), daemon=True)
        thread.start()
        try:
            _wait_for(lambda: bool(drift.notices()), "the brain never heard the settings")
        finally:
            loop.call_soon_threadsafe(lambda: task and task[0].cancel())
            thread.join(timeout=10)
    assert drift.notices()[0]["keys"] == ["observer.usage.enabled"]
