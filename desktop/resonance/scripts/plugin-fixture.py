"""Isolated production plugin service for the Electron acceptance script."""  # noqa: INP001

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

import uvicorn

from jarvis.execution.tools import ToolContext
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_flat_tool_dispatch import _Fixture
from tests.integration.test_mcp_tools import HERE, _free_port, _wait_listening
from tests.integration.test_plugin_connections import _package


def main() -> None:
    """Run local OAuth and desktop endpoints using only disposable test data."""
    root = Path(sys.argv[1])
    os.environ["JARVIS_PLUGIN_CATALOG"] = str(root / "empty")
    os.environ.pop("GITHUB_TOKEN", None)
    oauth_port, desktop_port, callback_port = _free_port(), _free_port(), _free_port()
    oauth = subprocess.Popen(  # noqa: S603 — our own local fixture
        [sys.executable, str(HERE / "mcp_oauth_server.py"), str(oauth_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_listening(oauth_port)
    endpoint = f"http://127.0.0.1:{oauth_port}/mcp"
    _package(root, "linear", {"linear": {"url": endpoint, "auth": "oauth"}})
    _package(root, "github", {"github": {"url": endpoint, "bearer_token_env_var": "GITHUB_TOKEN"}})
    _package(root, "gateway", {}, app_only=True)
    for name, description in (
        ("linear", "Manage issues, projects and team workflows"),
        ("github", "Access repositories, issues and pull requests"),
    ):
        (root / "plugins" / name / ".codex-plugin/plugin.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "interface": {
                        "displayName": "Linear" if name == "linear" else "GitHub",
                        "shortDescription": description,
                    },
                }
            )
        )
    fx = _Fixture(root / "runtime", tools=())
    opened: list[str] = []
    service = PluginConnections(
        repo_root=root,
        runtime_root=fx.paths.root,
        event_log=fx.paths.event_log,
        registry=fx.registry,
        config={"tools": {"mcp": {"timeout_s": 4, "oauth_callback_port": callback_port}}},
        open_url=opened.append,
    )
    service.initialize()
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            plugin_read=service.read,
            plugin_action=service.action,
            plugin_authorize=service.settings.matches,
        )
    )

    @app.post("/test/request")
    def request(body: dict[str, Any]) -> dict[str, Any]:
        conn = open_event_log(fx.paths.event_log)
        try:
            event = emit_event(
                conn,
                type="surface.user_intent",
                payload={"transcript": "Find my Linear tasks", "turn_id": "UI-T1"},
                correlation={"turn_id": "UI-T1"},
            )
            emit_event(
                conn,
                type="action.running",
                payload={"action_id": event.event_uid},
                correlation={"turn_id": "UI-T1"},
            )
            service.request_from_tool(
                {
                    "plugin_id": body.get("plugin_id", "linear"),
                    "purpose": "Find my assigned tasks",
                    "continue_task": True,
                },
                ToolContext(conn, fx.paths, event.event_uid),
            )
        finally:
            conn.close()
        return service.read()

    @app.post("/test/authorize")
    def authorize() -> dict[str, bool]:
        with urllib.request.urlopen(opened[-1], timeout=8) as response:  # noqa: S310 — local fixture OAuth
            return {"ok": response.status == HTTPStatus.OK}

    print(json.dumps({"port": desktop_port, "root": str(fx.paths.root)}), flush=True)  # noqa: T201 — parent process handshake
    try:
        uvicorn.run(app, host="127.0.0.1", port=desktop_port, log_level="error")
    finally:
        service.stop()
        fx.close()
        oauth.terminate()
        oauth.wait(timeout=10)


if __name__ == "__main__":
    main()
