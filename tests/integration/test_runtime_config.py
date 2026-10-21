"""ADR 0370: the config is read, merged and validated once, at the entry point.

Every refused value is exercised through ``load_runtime_config``, the function an entry point
calls, and every boot through the entry points themselves: a daemon's ``boot_runtime`` and the
terminal command. A malformed value stops the process before it has opened a database or
connected anywhere.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from jarvis.cli import main
from jarvis.runtime.config import RuntimeBootstrapError, load_runtime_config
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from pathlib import Path

_DOWNGRADE = "realtime.response downgraded"
_DOWNGRADED_CONFIG = (
    "realtime:\n  enabled: false\n  response:\n    routine_streaming:\n      enabled: true\n"
)


def _root(tmp_path: Path, settings: str = "") -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    return root


def _warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def _downgrade_traces() -> list[object]:
    return [p for p in realtime_trace_snapshot() if p.name == "response_activation_downgraded"]


@pytest.mark.parametrize("role", ["all", "brain"])
def test_the_shipped_config_loads_in_each_role(tmp_path: Path, role: str) -> None:
    """The shipped file, with only a role laid over it, parses into the typed tree."""
    root = _root(tmp_path, f"runtime:\n  role: {role}\n")
    config = load_runtime_config(None, root)
    assert config.role == role
    assert config.repo_root.joinpath("config", "jarvis.yaml") == config.config_path
    assert config.pricing_path == config.repo_root / "data" / "pricing.json"
    assert config.realtime.enabled is True
    assert config.realtime.response.reason == "validated"
    assert config.realtime.cancel_timeout_ms == 500
    assert (config.realtime.input_queue_capacity, config.realtime.input_max_concurrent_turns) == (
        8, 2,
    )
    # a brain forces what drives a device off, whatever the user's file says (ADR 0170)
    assert (config.raw["tools"]["open_path"] is None) == (role == "brain")
    assert (config.raw["realtime"]["ambient_sounds"] is False) == (role == "brain")


def test_a_missing_config_file_is_refused_before_anything_opens(tmp_path: Path) -> None:
    """An explicit path that is not a file stops the load, naming it."""
    with pytest.raises(RuntimeBootstrapError, match=r"config file .* does not exist"):
        load_runtime_config(tmp_path / "nowhere.yaml", _root(tmp_path))


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("runtime:\n  role: nope\n", r"runtime\.role must be all or brain"),
        ("runtime:\n  listen_addresses: 8.8.8.8\n", r"runtime\.listen_addresses must be a list"),
        ("runtime:\n  listen_addresses: ['nonsense']\n", "not an IP address"),
        ("runtime:\n  listen_addresses: ['8.8.8.8']\n", "not a private address"),
        ("runtime:\n  listen_addresses: ['0.0.0.0']\n", "not a private address"),
        ("runtime:\n  listen_hosts: ['*.local']\n", "plain host names, no wildcards"),
    ],
)
def test_a_refused_runtime_value_stops_the_load(
    tmp_path: Path, settings: str, message: str,
) -> None:
    """Role and listen addresses: the same refusals, with the same text, at the load."""
    with pytest.raises(RuntimeBootstrapError, match=message):
        load_runtime_config(None, _root(tmp_path, settings))


def test_a_zone_that_is_not_iana_is_a_value_error_naming_the_setting(tmp_path: Path) -> None:
    """``work_state.timezone`` is the one setting that raises ``ValueError`` (ADR 0322)."""
    root = _root(tmp_path, "work_state:\n  timezone: Mars/Olympus\n")
    with pytest.raises(ValueError, match=r"work_state\.timezone must be an IANA zone name"):
        load_runtime_config(None, root)


@pytest.mark.parametrize("key", ["queue_capacity", "max_concurrent_turns"])
@pytest.mark.parametrize("bad", ["0", "-3", "true", "'many'", "2.5"])
def test_a_bad_intake_bound_falls_back_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, key: str, bad: str,
) -> None:
    """Anything but a positive int reads as the shipped bound, and the file is told once."""
    caplog.set_level(logging.WARNING, logger="jarvis.runtime")
    config = load_runtime_config(None, _root(tmp_path, f"realtime:\n  input:\n    {key}: {bad}\n"))
    assert (config.realtime.input_queue_capacity, config.realtime.input_max_concurrent_turns) == (
        8, 2,
    )
    assert len(_warnings(caplog, f"realtime.input.{key}")) == 1


def test_a_bad_cancel_timeout_falls_back_to_500_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The cancel connection's busy timeout is a positive int, else 500 ms."""
    caplog.set_level(logging.WARNING, logger="jarvis.runtime")
    root = _root(tmp_path, "realtime:\n  response:\n    cancel_timeout_ms: 0\n")
    assert load_runtime_config(None, root).realtime.cancel_timeout_ms == 500
    assert len(_warnings(caplog, "realtime.response.cancel_timeout_ms=0")) == 1


def test_the_downgrade_is_reported_once_per_daemon_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A streaming switch under a parent that is off: one warning, one trace, all off."""
    caplog.set_level(logging.WARNING, logger="jarvis.runtime")
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_realtime_trace()
    runtime = boot_runtime(_root(tmp_path, _DOWNGRADED_CONFIG))
    try:
        assert runtime.response_flags.all_disabled
        assert runtime.config.realtime.response.reason == "realtime_parent_disabled"
        assert runtime.committed_event_bus is None
    finally:
        runtime.conn.close()
    assert len(_warnings(caplog, _DOWNGRADE)) == 1
    assert len(_downgrade_traces()) == 1


def test_the_downgrade_is_reported_once_per_terminal_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The terminal reads the same resolved flags; it resolves nothing a second time."""
    caplog.set_level(logging.WARNING, logger="jarvis.runtime")
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _root(tmp_path, _DOWNGRADED_CONFIG)
    token = tmp_path / "token"
    token.write_text("t\n", encoding="utf-8")
    token.chmod(0o600)

    async def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("jarvis.runtime.terminal.run_terminal_client", interrupted)
    monkeypatch.setattr("jarvis.runtime.terminal.build_tts_pipeline", lambda *_a, **_k: None)
    reset_realtime_trace()
    argv = [
        "terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file", str(token),
        "--runtime-root", str(root), "--voice",
    ]
    assert main(argv) == 0
    assert len(_warnings(caplog, _DOWNGRADE)) == 1
    assert len(_downgrade_traces()) == 1


def test_a_malformed_value_stops_the_terminal_before_it_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Even a section the terminal does not read: one place refuses, at the entry."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _root(tmp_path, "runtime:\n  role: nope\n")
    token = tmp_path / "token"
    token.write_text("t\n", encoding="utf-8")
    token.chmod(0o600)
    connected: list[object] = []

    async def client(*args: object, **_kwargs: object) -> None:
        connected.append(args)

    monkeypatch.setattr("jarvis.runtime.terminal.run_terminal_client", client)
    code = main([
        "terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file", str(token),
        "--runtime-root", str(root),
    ])
    assert code == 1
    assert "runtime.role must be all or brain" in capsys.readouterr().err
    assert connected == []
