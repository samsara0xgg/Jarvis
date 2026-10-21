"""ADR 0370: the config is read, merged and validated once, at the entry point.

Every refused value is exercised through ``load_runtime_config``, the function an entry point
calls, and every boot through the entry points themselves: a daemon's ``boot_runtime`` and the
terminal command. A malformed value stops the process before it has opened a database or
connected anywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jarvis.cli import main
from jarvis.runtime.config import RuntimeBootstrapError, load_runtime_config
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from tests.support.runtime_config import boot_runtime, load_settings, runtime_config

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


@pytest.mark.parametrize(
    ("block", "ttl_ms"),
    [({}, 600_000), ({"ttl_ms": 5000}, 5000)]
    + [({"ttl_ms": bad}, 600_000) for bad in (0, -1, True, "5000", 1.5)],
)
def test_the_confirmation_ttl_is_a_positive_int_else_ten_minutes(
    tmp_path: Path, block: dict[str, object], ttl_ms: int,
) -> None:
    """ADR 0012: a card waits ``confirmation.ttl_ms``; anything unusable is the default."""
    config = load_settings(tmp_path, {"confirmation": block})
    assert config.confirmation.ttl_ms == ttl_ms


@pytest.mark.parametrize(
    ("block", "preset", "timeout_s"),
    [
        ({}, "haiku", 4.0),
        ({"words_preset": "sonnet", "words_timeout_s": 2.5}, "sonnet", 2.5),
        ({"words_preset": "", "words_timeout_s": 0}, "haiku", 4.0),
        ({"words_timeout_s": True}, "haiku", 4.0),
        ({"words_timeout_s": "slow"}, "haiku", 4.0),
        ({"words_timeout_s": 3}, "haiku", 3.0),
    ],
)
def test_the_words_against_a_card_read_on_their_preset_with_one_attempts_budget(
    tmp_path: Path, block: dict[str, object], preset: str, timeout_s: float,
) -> None:
    """ADR 0321: ``confirmation.words_preset`` and ``words_timeout_s``, with their defaults."""
    config = load_settings(tmp_path, {"confirmation": block})
    assert (config.confirmation.words_preset, config.confirmation.words_timeout_s) == (
        preset, timeout_s,
    )


@pytest.mark.parametrize(
    ("value", "lead_in_ms"),
    [(1500, 1500), (0, 0), (None, 2000), (True, 2000), ("1500", 2000), (1.5, 2000)],
)
def test_the_lead_in_is_a_whole_number_of_milliseconds_else_two_seconds(
    tmp_path: Path, value: object, lead_in_ms: int,
) -> None:
    """ADR 0320: ``voice.lead_in_ms`` is the median call time from which a tool says its line."""
    config = load_settings(tmp_path, {"voice": {"lead_in_ms": value}})
    assert config.voice.lead_in_ms == lead_in_ms


def test_the_tool_iteration_bounds_resolve_the_voice_key_to_the_typed_one() -> None:
    """ADR 0060: a spoken turn reads the voice bound, else the typed one, else decide()'s own."""

    def bounds(llm: dict[str, object]) -> tuple[int, int]:
        iterations = runtime_config({"llm": llm}).tool_iterations
        return iterations.for_turn(spoken=False), iterations.for_turn(spoken=True)

    assert bounds({"max_tool_iterations": 40, "max_tool_iterations_voice": 8}) == (40, 8)
    assert bounds({"max_tool_iterations": 40}) == (40, 40)
    assert bounds({"max_tool_iterations_voice": 8}) == (5, 8)
    assert bounds({"max_tool_iterations": True, "max_tool_iterations_voice": 0}) == (5, 5)


@pytest.mark.parametrize(
    ("tier", "expected"),
    [(" priority ", "priority"), ("", None), ("  ", None), (1, None), (None, None)],
)
def test_the_voice_service_tier_is_stripped_text_or_nothing(
    tmp_path: Path, tier: object, expected: str | None,
) -> None:
    """``realtime.response.voice_service_tier`` rides a spoken turn's requests when set."""
    config = load_settings(tmp_path, {"realtime": {"response": {"voice_service_tier": tier}}})
    assert config.realtime.voice_service_tier == expected


def test_the_observer_block_resolves_to_cadences_and_watched_paths(tmp_path: Path) -> None:
    """Repos expand ``~`` and drop blank or non-text rows; usage is off unless enabled."""
    config = load_settings(tmp_path / "a", {"observer": {
        "poll_interval_s": 30, "repos": ["~/x", "", "  ", 3, "/y"],
        "usage": {"enabled": True, "poll_interval_s": 120},
        "claude_sessions": {"enabled": True},
    }})
    observer = config.observer
    assert observer.poll_interval_s == 30.0
    assert observer.repos == (str(Path("~/x").expanduser()), "/y")
    assert observer.usage_poll_interval_s == 120.0
    assert observer.claude_sessions is True
    bare = load_settings(tmp_path / "b", {"observer": {
        "poll_interval_s": 0, "repos": "~/x", "usage": {"enabled": False},
        "claude_sessions": {"enabled": "yes"},
    }}).observer
    assert (bare.poll_interval_s, bare.repos, bare.usage_poll_interval_s) == (60.0, (), None)
    assert bare.claude_sessions is False
    fallback = load_settings(tmp_path / "c", {"observer": {"usage": {"enabled": True}}}).observer
    assert fallback.usage_poll_interval_s == 300.0


def test_the_supervisor_cadences_are_positive_numbers_else_the_shipped_ones(
    tmp_path: Path,
) -> None:
    """ADR 0009 D4: the sweep every 30 s and an action's budget of 700 s, unless told."""
    shipped = load_settings(tmp_path / "a", {"supervisor": {}}).supervisor
    assert (shipped.sweep_interval_s, shipped.default_budget_s) == (30.0, 700.0)
    told = load_settings(
        tmp_path / "b", {"supervisor": {"sweep_interval_s": 5, "default_budget_s": 60.5}},
    ).supervisor
    assert (told.sweep_interval_s, told.default_budget_s) == (5.0, 60.5)
    bad = load_settings(
        tmp_path / "c", {"supervisor": {"sweep_interval_s": -1, "default_budget_s": True}},
    ).supervisor
    assert (bad.sweep_interval_s, bad.default_budget_s) == (30.0, 700.0)
