"""ADR 0356: a brain booted for real wires the weather lookup, the home and where he roughly is.

The settings name a home in Victoria and a WeatherKit key (a throwaway file); the network is the
fake of ``weatherkit_world``. With no report "Richmond" is the one near home; once a phone has
reported from Virginia it is that one, through the booted brain's own registry and Here.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import yaml

from jarvis.execution.tools import ActionLifecycle
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import emit_event
from tests.integration.weatherkit_world import Net, config, make_key
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from jarvis.runtime import JarvisRuntime

HOUR_MS = 3_600_000
HOME = {"latitude": 48.43, "longitude": -123.37, "country_code": "CA"}


def _weather(runtime: JarvisRuntime, action_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch ``weather`` through the runtime's own registry, on a worker thread."""
    request = ActionRequest(
        action_id=action_id, tool_name="weather", target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1", arguments=arguments,
        authorization_lease=None, run_id=None, turn_id=None,
    )

    def run() -> dict[str, Any]:
        with closing(sqlite3.connect(runtime.runtime_paths.event_log)) as conn:
            lifecycle = ActionLifecycle()
            lifecycle.register(action_id)
            lifecycle.transition(action_id, "authorized")
            done = runtime.tool_registry.dispatch(request, conn, runtime.runtime_paths, lifecycle)
        return dict(done.payload)

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(run).result()


def test_a_booted_brain_picks_the_richmond_near_where_he_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No report: home. A phone's report from two hours ago: where that is."""
    key, _ = make_key(tmp_path)
    Net(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    root.mkdir()
    settings = {"runtime": {"role": "brain"}, "home": {"weather": HOME}, **config(key)}
    (root / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    runtime = boot_runtime(root)
    try:
        first = _weather(runtime, "A-1", {"place": "Richmond"})
        assert first["place"] == "Richmond, British Columbia, Canada"
        assert first["same_name_elsewhere"][0]["place"] == "Richmond, Virginia, United States"
        emit_event(
            runtime.conn, type="phone.location_observed", ingestion_node="iphone",
            ts_epoch_ms=int(runtime.clock.time() * 1000) - 2 * HOUR_MS,
            payload={"lat": 37.6, "lng": -77.5, "accuracy_m": 40.0},
        )
        second = _weather(runtime, "A-2", {"place": "Richmond"})
        assert second["place"] == "Richmond, Virginia, United States"
        assert second["same_name_elsewhere"][0]["place"] == "Richmond, British Columbia, Canada"
    finally:
        runtime.conn.close()
