"""A boot that fails part-way closes what it opened and raises the failure as it was (ADR 0372).

The refusals are the real ones a boot makes after the Event Log is open and the plugin
connections are running: a Tier 0 table that does not parse, and an ``llm`` section that is gone.
The test asks what a caller of ``bootstrap_runtime_app`` can observe afterwards: the Event Log
connection is closed, no thread the boot started is left, and the geocoding of kept places, which
a boot starts only once nothing else can fail, never ran.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution.geocode import Unplaced
from jarvis.runtime.bootstrap import core as core_module
from jarvis.runtime.bootstrap import places as places_module
from jarvis.runtime.bootstrap.app import bootstrap_runtime_app
from jarvis.runtime.config import RuntimeBootstrapError, RuntimeConfig, load_runtime_config
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.state.event_log import open_event_log
from tests.support.mcp import ECHO
from tests.support.runtime_config import SHIPPED

if TYPE_CHECKING:
    from pathlib import Path

REPO = SHIPPED.parents[1]
ADDRESS = "1 Test Street"


class _Watch:
    """What a boot opened, seen through the seams it opens it by."""

    def __init__(self) -> None:
        self.connections: list[sqlite3.Connection] = []
        self.plugin_threads: list[threading.Thread] = []
        self.geocoded: list[str] = []


@pytest.fixture
def watch(monkeypatch: pytest.MonkeyPatch) -> _Watch:
    """Record the Event Log connection, the threads the plugin start adds and each geocode."""
    seen = _Watch()

    def open_and_record(path: Path) -> sqlite3.Connection:
        conn = open_event_log(path)
        seen.connections.append(conn)
        return conn

    initialize = PluginConnections.initialize

    def initialize_and_record(self: PluginConnections) -> None:
        before = set(threading.enumerate())
        initialize(self)
        seen.plugin_threads.extend(set(threading.enumerate()) - before)

    def geocode(address: str, _key: str) -> tuple[float, float]:
        seen.geocoded.append(address)
        raise Unplaced(address)

    monkeypatch.setattr(core_module, "open_event_log", open_and_record)
    monkeypatch.setattr(PluginConnections, "initialize", initialize_and_record)
    monkeypatch.setattr(places_module, "geocode", geocode)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "not-a-real-key")
    return seen


def _boot_inputs(tmp_path: Path, settings: dict[str, Any]) -> tuple[RuntimeConfig, RuntimePaths]:
    """The shipped config in a copy of its directory, over a root that keeps echo and one address.

    The echo server is a stdio MCP server the plugin start connects to, so a thread and a child
    process are running by the time a later stage can fail; the address is a place with no centre,
    which a successful boot would geocode.
    """
    shutil.copytree(SHIPPED.parent, tmp_path / "repo" / "config")
    (tmp_path / "repo" / "data").mkdir()
    shutil.copy(REPO / "data" / "pricing.json", tmp_path / "repo" / "data" / "pricing.json")
    paths = bootstrap_runtime(tmp_path / "root")
    settings = {
        "tools": {"mcp": {"servers": ECHO, "timeout_s": 20}},
        "transit": {"home": ADDRESS},
        **settings,
    }
    (paths.root / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    config = load_runtime_config(
        tmp_path / "repo" / "config" / "jarvis.yaml", paths.root,
        prompt_path=REPO / "prompts" / "jarvis_v1.md",
    )
    return config, paths


def _geocoding_threads() -> list[threading.Thread]:
    return [one for one in threading.enumerate() if one.name == "jarvis-place-locate"]


def _wait_for_geocoding() -> None:
    for one in _geocoding_threads():
        one.join(timeout=10)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "empty-catalog"))


def test_a_boot_that_succeeds_keeps_what_it_opened_and_geocodes_the_kept_place(
    tmp_path: Path, watch: _Watch,
) -> None:
    """The control: the stack releases a good boot, and the places are located after it."""
    config, paths = _boot_inputs(tmp_path, {})

    runtime = bootstrap_runtime_app(config, paths)
    _wait_for_geocoding()
    try:
        runtime.conn.execute("SELECT 1").fetchone()  # still open: the runtime owns it
        assert runtime.plugin_connections is not None
        assert watch.plugin_threads, "the echo server's connection should be running"
        assert all(one.is_alive() for one in watch.plugin_threads)
        assert watch.geocoded == [ADDRESS]
    finally:
        runtime.conn.close()
        assert runtime.plugin_connections is not None
        runtime.plugin_connections.stop()


def test_a_tier0_table_that_does_not_parse_leaves_nothing_open(
    tmp_path: Path, watch: _Watch,
) -> None:
    """The audit's case (F8): a typo in ``tier0_patterns.yaml`` after the plugins started."""
    config, paths = _boot_inputs(tmp_path, {})
    (config.config_path.parent / "tier0_patterns.yaml").write_text("rows: [unclosed\n")
    before = set(threading.enumerate())

    with pytest.raises(RuntimeBootstrapError, match=r"tier0_patterns\.yaml invalid"):
        bootstrap_runtime_app(config, paths)

    _assert_nothing_left(watch, before)


def test_a_missing_llm_section_leaves_nothing_open(tmp_path: Path, watch: _Watch) -> None:
    """The same cleanup from a later stage, which lives in another module."""
    config, paths = _boot_inputs(tmp_path, {"llm": None})
    before = set(threading.enumerate())

    with pytest.raises(RuntimeBootstrapError, match="has no 'llm' section"):
        bootstrap_runtime_app(config, paths)

    _assert_nothing_left(watch, before)


def _assert_nothing_left(watch: _Watch, before: set[threading.Thread]) -> None:
    (conn,) = watch.connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    assert watch.plugin_threads, "the echo server's connection should have been running"
    assert not [one for one in watch.plugin_threads if one.is_alive()]
    assert not [one for one in set(threading.enumerate()) - before if one.is_alive()]
    assert not _geocoding_threads()
    assert watch.geocoded == []
