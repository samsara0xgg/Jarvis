"""ADR 0370: the config is read, merged and validated once, at the entry point.

Every refused value is exercised through ``load_runtime_config``, the function an entry point
calls, and every boot through the entry points themselves: a daemon's ``boot_runtime`` and the
terminal command. A malformed value stops the process before it has opened a database or
connected anywhere.
"""

from __future__ import annotations

import logging
from datetime import time
from pathlib import Path

import pytest

from jarvis.cli import main
from jarvis.runtime.config import (
    DiagnosticsSettings,
    RuntimeBootstrapError,
    WorkersSettings,
    load_runtime_config,
)
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


@pytest.mark.parametrize("key", ["socket_wait_s", "push_wait_s"])
@pytest.mark.parametrize("value", [0, -1, 31, True, "4", None])
def test_a_locate_wait_outside_zero_to_thirty_seconds_stops_the_load(
    tmp_path: Path, key: str, value: object,
) -> None:
    """ADR 0281: Here waits for the phone's fix at most thirty seconds, never none."""
    with pytest.raises(RuntimeBootstrapError, match=rf"locate\.{key} is seconds, above 0 and up"):
        load_settings(tmp_path, {"locate": {key: value}})


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"phone": 5}, r"locate\.phone is the name of a paired device"),
        ({"max_age_s": 0}, r"locate\.max_age_s is a whole number of seconds above 0"),
        ({"max_age_s": True}, r"locate\.max_age_s is a whole number of seconds above 0"),
        ({"max_age_s": 1.5}, r"locate\.max_age_s is a whole number of seconds above 0"),
    ],
)
def test_a_locate_phone_or_age_that_is_not_usable_stops_the_load(
    tmp_path: Path, block: dict[str, object], message: str,
) -> None:
    """The phone is a name or empty; a report's age limit is whole seconds above zero."""
    with pytest.raises(RuntimeBootstrapError, match=message):
        load_settings(tmp_path, {"locate": block})


def test_the_locate_block_resolves_to_its_waits_phone_and_age(tmp_path: Path) -> None:
    """Seconds may be whole or fractional; the phone's name is stripped."""
    locate = load_settings(
        tmp_path, {"locate": {"socket_wait_s": 2, "push_wait_s": 6.5, "phone": " iPad ",
                              "max_age_s": 600}},
    ).locate
    assert (locate.socket_wait_s, locate.push_wait_s, locate.phone, locate.max_age_s) == (
        2.0, 6.5, "iPad", 600,
    )
    shipped = runtime_config({}).locate
    assert (shipped.socket_wait_s, shipped.push_wait_s, shipped.phone, shipped.max_age_s) == (
        4.0, 8.0, "", 900,
    )


def test_work_state_auto_must_be_true_or_false_and_names_the_file(tmp_path: Path) -> None:
    """ADR 0180: a background analysis does not run on a guess."""
    with pytest.raises(RuntimeBootstrapError, match=r"jarvis\.yaml work_state\.auto must be true"):
        load_settings(tmp_path, {"work_state": {"auto": "yes"}})
    assert load_settings(tmp_path / "on", {"work_state": {"auto": True}}).work_state.auto is True
    assert runtime_config({}).work_state.auto is False


@pytest.mark.parametrize(
    ("block", "preset"),
    [({}, "gpt6-luna"), ({"preset": "  "}, "gpt6-luna"), ({"preset": 3}, "gpt6-luna"),
     ({"preset": "fast"}, "fast")],
)
def test_the_work_state_preset_is_a_nonblank_name_else_the_fallback(
    block: dict[str, object], preset: str,
) -> None:
    """The daily report runs on that preset too, unless it names its own."""
    config = runtime_config({"work_state": block})
    assert config.work_state.preset == preset
    assert config.daily_report.preset == preset
    named = runtime_config({"work_state": block, "daily_report": {"preset": "deep"}})
    assert named.daily_report.preset == "deep"


def test_the_daily_report_time_is_hh_mm_else_off_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR 0101: an unreadable ``daily_report.at`` writes no report and says so."""
    assert runtime_config({}).daily_report.at is None
    assert runtime_config({"daily_report": {"at": "06:30"}}).daily_report.at == time(6, 30)
    with caplog.at_level(logging.WARNING, logger="jarvis.runtime"):
        assert runtime_config({"daily_report": {"at": "soon"}}).daily_report.at is None
    assert len(_warnings(caplog, "is not HH:MM")) == 1
    codex = runtime_config({"daily_report": {"codex_sessions": "yes"}}).daily_report
    assert codex.codex_sessions is False
    assert runtime_config({"daily_report": {"codex_sessions": True}}).daily_report.codex_sessions


def test_the_dashboard_mail_page_without_the_view_is_off_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR 0176: the mail page's open letter is the view's open item."""
    with caplog.at_level(logging.WARNING, logger="jarvis.runtime"):
        config = runtime_config({"dashboard": {"mail": {"enabled": True}}})
    assert (config.dashboard.view, config.dashboard.mail) == (False, True)
    assert len(_warnings(caplog, "needs dashboard.view.enabled")) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jarvis.runtime"):
        runtime_config({"dashboard": {"view": {"enabled": True}, "mail": {"enabled": True}}})
    assert not _warnings(caplog, "needs dashboard.view.enabled")


def test_ambient_sounds_are_on_unless_false_and_anything_else_stops_the_load(
    tmp_path: Path,
) -> None:
    """ADR 0151: ``realtime.ambient_sounds`` is a bool; a brain forces it off."""
    assert runtime_config({}).realtime.ambient_sounds is True
    assert runtime_config({"realtime": {"ambient_sounds": False}}).realtime.ambient_sounds is False
    with pytest.raises(TypeError, match=r"realtime\.ambient_sounds must be true or false"):
        load_settings(tmp_path, {"realtime": {"ambient_sounds": "off"}})
    brain = runtime_config({"runtime": {"role": "brain"}}).realtime
    assert brain.ambient_sounds is False


def test_the_diagnostics_switches_are_on_only_when_true() -> None:
    """ADR 0120: ``yes`` and ``1`` are not true."""
    assert runtime_config({}).diagnostics == DiagnosticsSettings(
        log_llm_io=False, record_tts_audio=False,
    )
    on = runtime_config({"diagnostics": {"log_llm_io": True, "record_tts_audio": "yes"}})
    assert on.diagnostics == DiagnosticsSettings(log_llm_io=True, record_tts_audio=False)


@pytest.mark.parametrize(
    ("value", "minutes"),
    [(None, 60), (45, 45), (30.5, 30), (1, 1), (1440, 1440), (0, 60), (1441, 60), (True, 60),
     ("45", 60)],
)
def test_a_commitment_rings_ahead_by_a_minute_count_within_a_day_else_an_hour(
    value: object, minutes: int,
) -> None:
    """``commitments.remind_before_min``, 1 to 1440 minutes."""
    config = runtime_config({"commitments": {"remind_before_min": value}})
    assert config.remind_before_min == minutes


def test_the_maps_cap_is_a_nonnegative_number_else_the_default() -> None:
    """ADR 0283: 0 switches the cap off; a negative or textual cap is the default."""
    assert runtime_config({}).maps_monthly_usd == 10.0
    assert runtime_config({"maps": {"monthly_usd": 0}}).maps_monthly_usd == 0.0
    assert runtime_config({"maps": {"monthly_usd": -1}}).maps_monthly_usd == 10.0
    assert runtime_config({"maps": {"monthly_usd": True}}).maps_monthly_usd == 10.0


def test_the_plan_day_ends_at_a_clock_time_else_the_default() -> None:
    """ADR 0302: ``day_plan.day_ends`` is HH:MM."""
    assert runtime_config({"day_plan": {"day_ends": "21:15"}}).plan_day_ends == time(21, 15)
    assert runtime_config({"day_plan": {"day_ends": "late"}}).plan_day_ends == time(23, 30)


def test_the_tools_web_bounds_are_positive_numbers_else_the_shipped_defaults() -> None:
    """ADR-0011 D7: UX knobs, so a malformed value is the default and never stops the boot."""
    shipped = runtime_config({}).tools.web
    told = runtime_config({"tools": {"web": {
        "search_max_results": 3, "fetch_max_bytes": 5000, "fetch_max_text_bytes": 900,
        "timeout_s": 2.5,
    }}}).tools.web
    assert (told.search_max_results, told.fetch_max_bytes, told.fetch_max_text_bytes,
            told.timeout_s) == (3, 5000, 900, 2.5)
    for bad in (0, -1, True, "9", None):
        broken = runtime_config({"tools": {"web": {
            "search_max_results": bad, "fetch_max_bytes": bad, "fetch_max_text_bytes": bad,
            "timeout_s": bad,
        }}}).tools.web
        assert broken == shipped
    assert runtime_config({"tools": {"web": 5}}).tools.web == shipped


def test_the_search_provider_and_its_key_variable_are_stripped_names_never_a_key() -> None:
    """The key is read from the environment at boot, by the name this holds; it is not stored."""
    web = runtime_config({"tools": {"web": {
        "search_provider": " tavily ", "search_api_key_env": " MY_KEY ",
    }}}).tools.web
    assert (web.search_provider, web.search_api_key_env) == ("tavily", "MY_KEY")
    blank = runtime_config({"tools": {"web": {"search_provider": 3, "search_api_key_env": " "}}})
    assert (blank.tools.web.search_provider, blank.tools.web.search_api_key_env) == ("", "")


def test_the_screen_and_vault_settings_read_a_name_a_width_and_a_folder() -> None:
    """`screen_look`'s vision preset and width; `search_notes`' vault, ``~`` expanded."""
    shipped = runtime_config({}).tools
    assert (shipped.vision_preset, shipped.obsidian_vault_root) == ("vision", None)
    told = runtime_config({"tools": {
        "screen": {"vision_preset": "eyes", "max_width_px": 800},
        "obsidian": {"vault_root": "~/Notes"},
    }}).tools
    assert (told.vision_preset, told.screen_max_width_px) == ("eyes", 800)
    assert told.obsidian_vault_root == Path("~/Notes").expanduser()
    for bad in ("", "  ", -3, True, 0):
        broken = runtime_config({"tools": {
            "screen": {"vision_preset": bad, "max_width_px": bad},
            "obsidian": {"vault_root": bad},
        }}).tools
        assert (broken.vision_preset, broken.screen_max_width_px, broken.obsidian_vault_root) == (
            shipped.vision_preset, shipped.screen_max_width_px, None,
        )


def test_workers_are_on_only_when_true_and_a_brain_forces_them_off(tmp_path: Path) -> None:
    """ADR 0019: roots are ``~`` expanded and resolved; a brain has no folders of its own."""
    assert runtime_config({}).tools.workers == WorkersSettings(enabled=False, roots=())
    on = runtime_config({"tools": {"workers": {"enabled": True, "roots": [str(tmp_path), "~"]}}})
    assert on.tools.workers.enabled is True
    assert on.tools.workers.roots == (tmp_path.resolve(), Path("~").expanduser().resolve())
    assert runtime_config({"tools": {"workers": {"enabled": "yes"}}}).tools.workers.enabled is False
    brain = runtime_config({
        "runtime": {"role": "brain"}, "tools": {"workers": {"enabled": True, "roots": ["~"]}},
    })
    assert brain.tools.workers == WorkersSettings(enabled=False, roots=())


def test_a_brain_has_no_vault_and_no_bookmarks_of_its_own() -> None:
    """ADR 0225: the vault and the open_path bookmarks are the terminal's alone."""
    brain = runtime_config({"runtime": {"role": "brain"}, "tools": {
        "obsidian": {"vault_root": "~/Notes"}, "open_path": {"aliases": {"a": "/x"}},
    }}).tools
    assert (brain.obsidian_vault_root, dict(brain.open_path)) == (None, {})


def test_a_settings_file_that_is_not_yaml_is_a_refusal_naming_the_file_and_line(
    tmp_path: Path,
) -> None:
    """Not a traceback, and none of the file's own text in the message."""
    root = _root(tmp_path, "locate: [\n  secret-looking-text\n")
    named = r"settings\.yaml is not valid YAML.*\(line \d+\)"
    with pytest.raises(RuntimeBootstrapError, match=named) as caught:
        load_runtime_config(None, root)
    assert "secret-looking-text" not in str(caught.value)
