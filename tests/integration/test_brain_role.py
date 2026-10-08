"""ADR 0170: `runtime.role: brain` runs the daemon headless; `all` stays the default.

Two acceptance checks. The first assembles the runtime in both roles and reads what it
offers. The second boots a real ``python -m jarvis serve`` in brain role on a throwaway
runtime root with no API key in its environment, asks it for liveness, hands it a typed
turn, and reads what it logged and wrote: no voice model download, no power observer, and
the turn reaching the model request, which is refused for the missing key before any
network call.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import RuntimeBootstrapError, bootstrap_runtime_app
from jarvis.shared import CallerPrincipal
from jarvis.state.plugin_settings import local_key

if TYPE_CHECKING:
    from pathlib import Path

DEVICE_TOOLS = {
    "open_path", "open_url", "read_clipboard", "screen_look", "read_file", "write_file",
    "search_notes",
}
NIGHT_TOOLS = {"start_night_run", "end_night_run"}


def _runtime(tmp_path: Path, settings: str, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    return bootstrap_runtime_app(runtime_root=root)


def _menu(runtime: Any) -> set[str]:  # noqa: ANN401
    return {t.name for t in runtime.tool_registry.for_caller(CallerPrincipal.JARVIS_LLM)}


def test_the_default_role_is_all_and_the_brain_runs_no_device_tool_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`all` offers open, clipboard and screen as before; a brain has them as proxies only."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    default = _runtime(tmp_path / "a", "assistant_name: Jarvis\n", monkeypatch)
    brain = _runtime(tmp_path / "b", "runtime:\n  role: brain\n", monkeypatch)
    try:
        assert default.role == "all"
        assert {"open_path", "read_file", "read_clipboard", "open_url", "screen_look"} <= {
            t.name for t in default.tool_registry.get_definitions()
        }
        assert _menu(default) >= NIGHT_TOOLS
        assert default.night is not None
        assert default.terminal_hub is None

        assert brain.role == "brain"
        held = {t.name for t in brain.tool_registry.get_definitions()}
        assert held >= NIGHT_TOOLS  # proxies: the run is the terminal's (ADR 0192)
        assert brain.terminal_hub is not None
        assert DEVICE_TOOLS - {"search_notes"} <= held  # no vault named: no search_notes
        assert {"create_memo", "remember", "web_fetch", "ask_user"} <= _menu(brain)
        assert brain.night is None
        assert {row.tool_name for row in brain.tier0_table} <= held
        observer = brain.config["observer"]
        assert observer["timesink"]["enabled"] is False
        assert observer["repos"] == []
        assert brain.config["realtime"]["gpt_live"]["enabled"] is False
    finally:
        default.conn.close()
        brain.conn.close()


def test_an_unknown_role_stops_the_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in `runtime.role` is a boot error, not a silent `all`."""
    with pytest.raises(RuntimeBootstrapError, match=r"runtime\.role must be all or brain"):
        _runtime(tmp_path, "runtime:\n  role: terminal\n", monkeypatch)


def _get(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 — loopback.
    with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 — loopback.
        return bytes(response.read())


def _events(db: Path) -> list[str]:
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            return [row[0] for row in conn.execute("SELECT type FROM events ORDER BY id")]
    except sqlite3.Error:
        return []


def test_a_brain_daemon_serves_a_typed_turn_and_starts_nothing_device_bound(
    tmp_path: Path,
) -> None:
    """Boot, /api/health, a typed turn up to the model request, and a clean log."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text("runtime:\n  role: brain\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.endswith(("_API_KEY", "_ADMIN_KEY")) and name != "JARVIS_RUNTIME_ROOT"
    }
    env.update(HOME=str(tmp_path), JARVIS_LOG_LEVEL="INFO")
    log = tmp_path / "daemon.log"
    with log.open("w") as sink:
        daemon = subprocess.Popen(  # noqa: S603 — sys.executable and a fixed argv.
            [sys.executable, "-m", "jarvis", "serve", "--runtime-root", str(root),
             "--port", str(port)],
            stdout=sink, stderr=subprocess.STDOUT, env=env,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 25
        while True:
            try:
                assert json.loads(_get(f"{base}/api/health")) == {"status": "ok"}
                break
            except (urllib.error.URLError, ConnectionError):
                assert daemon.poll() is None, log.read_text(encoding="utf-8")[-2000:]
                assert time.monotonic() < deadline, "daemon never answered /api/health"
                time.sleep(0.2)

        submit = urllib.request.Request(  # noqa: S310 — loopback.
            f"{base}/inherent/submit",
            data=json.dumps({"text": "hello from a terminal"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {local_key(root)}",
            },
            method="POST",
        )
        with urllib.request.urlopen(submit, timeout=5) as response:  # noqa: S310 — loopback.
            turn_id = json.loads(response.read())["turn_id"]
        assert turn_id

        while "turn.failed" not in _events(root / "mac_events.db"):
            assert time.monotonic() < deadline, _events(root / "mac_events.db")
            time.sleep(0.2)
    finally:
        daemon.terminate()
        daemon.wait(timeout=20)

    types = _events(root / "mac_events.db")
    assert types.index("surface.user_intent") < types.index("response.request_admitted")
    text = log.read_text(encoding="utf-8")
    assert '"reason": "role_brain"' in text
    assert "MissingAPIKeyError" in text  # refused for the unset key, before any network call
    for absent in ("Traceback", "power observer", "voice models missing", "models: downloading"):
        assert absent not in text
    assert not (root / "models").exists()
