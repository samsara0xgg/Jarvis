"""ADR-0003 Step 2 smoke — end-to-end /submit -> WS open+append*+done round-trip.

Spawns ``python -m jarvis serve`` on an ephemeral port, opens a
WebSocket, POSTs ``/inherent/submit``, and asserts the three-envelope
Step-2 wire schema (``open`` + N ``append`` + ``done``) round-trips
back through the daemon's watchers (which invoke the REAL LLM). See
ADR-0003 § Step 2 D11 (``docs/adr/0003-inherent-text.md``) for the
canonical envelope mapping.

Marked ``live_llm`` because :func:`jarvis.runtime.drive_turn` issues a
real LLM call inside the daemon's user_intent_watcher thread; the test
is skipped by default in CI without credentials.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.live_llm


def _pick_free_port() -> int:
    """Pick an OS-assigned ephemeral port and immediately release it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, *, deadline_s: float = 10.0) -> None:
    """Poll until the port accepts a TCP connection or deadline expires."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                continue
            return
    msg = f"daemon never bound port {port} within {deadline_s}s"
    raise TimeoutError(msg)


def test_daemon_submit_to_ws_round_trip(tmp_path: Path) -> None:
    """Spawn the daemon, POST a submit, receive open + append* + done on WS."""
    port = _pick_free_port()

    proc = subprocess.Popen(  # noqa: S603 — sys.executable is trusted; argv is fully controlled.
        [
            sys.executable,
            "-m",
            "jarvis",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--runtime-root",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "JARVIS_RUNTIME_ROOT": str(tmp_path)},
    )

    try:
        _wait_for_port(port, deadline_s=10.0)

        # websockets is installed transitively via uvicorn[standard] (Step 5).
        import urllib.request  # noqa: PLC0415 — lazy stdlib import keeps top-level cheap.

        from websockets.sync.client import connect  # noqa: PLC0415 — see above.

        # 1. Open WS first so it's registered before /submit fires.
        with connect(f"ws://127.0.0.1:{port}/inherent/ws") as ws:
            # 2. POST /submit
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/inherent/submit",
                data=json.dumps({"text": "hi"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — localhost loopback URL controlled by the test.
                assert resp.status == 200
                body = json.loads(resp.read().decode())
                # Subset assert: ADR-0009 D2 made the response additive
                # (``turn_id`` rides alongside). Pin the Day-1 field
                # exactly; tolerate additive keys.
                assert body["status"] == "accepted", body

            # 3. Receive open + N appends + done. Daemon must complete a
            #    full turn (real LLM call) before the first envelope
            #    arrives; subsequent envelopes are emitted back-to-back.
            open_msg = json.loads(ws.recv(timeout=120))
            assert open_msg["op"] == "open", open_msg
            # Subset assert (see /submit above): the open payload also
            # carries the additive ``turn_id``.
            expected_open = {"content": "", "streaming": True, "kind": "text", "q": "hi"}
            assert open_msg["payload"].items() >= expected_open.items(), open_msg

            appends: list[str] = []
            done_msg: dict[str, object] | None = None
            # 10s is generous for a small handful of envelopes over
            # loopback WS; 64 is a hard cap so a daemon bug can't hang
            # the test.
            for _ in range(64):
                msg = json.loads(ws.recv(timeout=10))
                if msg["op"] == "append":
                    token = msg["payload"]["token"]
                    assert isinstance(token, str), msg
                    assert token, msg
                    appends.append(token)
                elif msg["op"] == "done":
                    done_msg = msg
                    break
                else:
                    pytest.fail(f"unexpected WS envelope: {msg!r}")
            else:
                pytest.fail(f"daemon emitted >64 envelopes without done; got appends={appends!r}")

        assert done_msg is not None
        assert done_msg["op"] == "done", done_msg
        # Subset assert (see /submit above): the done payload also
        # carries the additive ``turn_id``.
        done_payload = done_msg["payload"]
        assert isinstance(done_payload, dict), done_msg
        assert done_payload["fadeMs"] == 5000, done_msg
        assert appends, "daemon emitted no append envelopes"
        assert "".join(appends), "daemon emitted empty appends"

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
