"""ADR 0170 step 4b: the observers a terminal runs push their events into the brain's log.

Acceptance checks, each against the real code and real files: an `event` frame is appended on
the brain with the device's name as `ingestion_node`, once however often it is sent; a type no
observer produces, an oversize frame, a malformed one and an unpaired token are all refused and
leave no row; the repo observer (a real temp git repo) and the TimeSink observer (a temp
database in TimeSink's schema) emit through the outbox exactly the events they append to a log
on one machine, and the allowlist is what they produce; one machine's wiring is untouched; and
a real terminal process side (client, outbox and observers) against a real server delivers the
events, picks up from the brain's baseline after a restart, and buffers across a dropped link.
"""

from __future__ import annotations

import asyncio
import functools
import json
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.runtime import bootstrap_runtime_app
from jarvis.runtime.terminal import _observe, _Watched
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import terminal_events, terminal_link
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.repo_observer import RepoObserver
from jarvis.surface.terminal_events import (
    MAX_EVENT_CHARS,
    OBSERVER_EVENT_TYPES,
    BrainEvents,
    EventOutbox,
)
from jarvis.surface.terminal_link import TerminalHub, run_terminal_client
from jarvis.surface.timesink_observer import TimesinkObserver

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

REMOTE = "100.87.250.92"
TERMINAL_WS = "ws://127.0.0.1:8006/terminal/ws"
UID = "a" * 32
OK_PAYLOAD = {
    "repo_path": "/r", "branch": "main", "head_sha": "abc", "dirty_file_count": 0,
    "last_commit_subject": "s", "observed_at_ms": 1, "actor": "observer",
}


# --- fixtures: a git repo, a TimeSink database, a brain's log --------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 — a fixed git argv on a temp repo.
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],  # noqa: S607
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return done.stdout.strip()


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    commit(path, "first")
    return path


def commit(repo: Path, subject: str) -> None:
    """Add a file and commit it with ``subject``."""
    (repo / f"{subject}.txt").write_text(subject, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)


def _make_timesink(path: Path) -> sqlite3.Connection:
    """A store in TimeSink's own schema and GRDB timestamp encoding."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE span (id INTEGER PRIMARY KEY AUTOINCREMENT, start DATETIME NOT NULL, "
        "end DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "title TEXT, url TEXT, domain TEXT, document TEXT)"
    )
    conn.execute(
        "CREATE TABLE stateEvent (id INTEGER PRIMARY KEY AUTOINCREMENT, at DATETIME NOT NULL, "
        "kind TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE capture (id INTEGER PRIMARY KEY AUTOINCREMENT, at DATETIME NOT NULL, "
        "lastSeenAt DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "windowID INTEGER NOT NULL, title TEXT, spanID INTEGER, text TEXT NOT NULL, "
        "imagePath TEXT)"
    )
    conn.commit()
    return conn


def add_span(conn: sqlite3.Connection, start: str, end: str) -> None:
    """Write a span as TimeSink's tracker does."""
    conn.execute(
        "INSERT INTO span(start,end,appBundleID,appName,title) VALUES(?,?,?,?,?)",
        (start, end, "com.apple.Terminal", "Terminal", "zsh"),
    )
    conn.commit()


def _brain_log(path: Path) -> sqlite3.Connection:
    """The brain's event log, usable from the server's own thread."""
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


def rows(path: Path, *types: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(type, ingestion_node, payload)`` of the log's rows, oldest first."""
    marks = ",".join("?" for _ in types)
    with sqlite3.connect(path) as conn:
        found = conn.execute(
            f"SELECT type, ingestion_node, payload_json FROM events WHERE type IN ({marks}) "  # noqa: S608
            "ORDER BY id",
            types,
        ).fetchall()
    return [(kind, node, json.loads(payload)) for kind, node, payload in found]


def frame(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """A good event frame, with ``changes`` laid over it."""
    base: dict[str, Any] = {
        "type": "event", "event_uid": UID, "event_type": "repo.state_observed",
        "ts_epoch_ms": int(time.time() * 1000), "payload": OK_PAYLOAD,
    }
    return {**base, **changes}


# --- the brain's end ---------------------------------------------------------------------


def _brain_client(tmp_path: Path) -> tuple[TestClient, TerminalHub, Path]:
    log = tmp_path / "events.db"
    hub = TerminalHub(events=BrainEvents(_brain_log(log)))
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=hub,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000)), hub, log


def _send(ws: Any, body: dict[str, Any] | str) -> dict[str, Any]:  # noqa: ANN401
    ws.send_text(body if isinstance(body, str) else json.dumps(body))
    return dict(ws.receive_json())


def test_an_event_frame_is_appended_once_under_the_terminals_name(tmp_path: Path) -> None:
    """The paired name is the row's `ingestion_node`; a resend is acknowledged, not repeated."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    ts = int(time.time() * 1000) - 5_000
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        ready = ws.receive_json()
        assert (ready["type"], ready["device"], ready["baseline"]) == ("ready", "macbook", [])

        assert _send(ws, frame(ts_epoch_ms=ts)) == {"type": "ack", "event_uid": UID, "ok": True}
        assert _send(ws, frame(ts_epoch_ms=ts)) == {"type": "ack", "event_uid": UID, "ok": True}
    assert rows(log, "repo.state_observed") == [("repo.state_observed", "macbook", OK_PAYLOAD)]
    with sqlite3.connect(log) as conn:
        assert conn.execute("SELECT event_uid, ts_epoch_ms, actor FROM events").fetchall() == [
            (UID, ts, "observer"),
        ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (frame(event_type="utterance.received", payload={"transcript": "x", "turn_id": "t"}),
         "event_type_not_allowed"),
        (frame(event_type="confirmation.accepted",
               payload={"confirmation_id": "c", "utterance_raw": "yes", "grammar_rule_id": "g"}),
         "event_type_not_allowed"),
        (frame(event_type="usage.state_observed"), "event_type_not_allowed"),
        (frame(event_type="not.a.type"), "event_type_not_allowed"),
        (frame(event_type=None), "event_type_not_allowed"),
        (frame(payload={**OK_PAYLOAD, "pad": "x" * MAX_EVENT_CHARS}), "event_too_large"),
        (frame(payload={"repo_path": "/r"}), "bad_event"),
        (frame(payload=["not", "an", "object"]), "bad_event"),
        (frame(ts_epoch_ms="now"), "bad_event"),
        (frame(ts_epoch_ms=True), "bad_event"),
    ],
)
def test_an_event_no_observer_produces_or_that_is_malformed_is_refused(
    tmp_path: Path, body: dict[str, Any], code: str,
) -> None:
    """The allowlist, the size cap and the shape checks each refuse with a code and no row."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        ws.receive_json()
        assert _send(ws, body) == {"type": "ack", "event_uid": UID, "ok": False, "code": code}
        # Still a live link: a good event after the refusals is taken.
        assert _send(ws, frame())["ok"] is True
    with sqlite3.connect(log) as conn:
        assert conn.execute("SELECT type FROM events").fetchall() == [("repo.state_observed",)]


def test_an_event_without_a_usable_uid_is_refused_and_an_unpaired_terminal_cannot_send(
    tmp_path: Path,
) -> None:
    """No uid, or a uid that is not 32 hex digits: refused. An unpaired token never gets in."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        ws.receive_json()
        for uid in (None, "short", "G" * 32, 7):
            ack = _send(ws, frame(event_uid=uid))
            assert (ack["ok"], ack["code"], ack["event_uid"]) == (False, "bad_event", None)
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": "Bearer not-paired"},
    ):
        pass
    assert refused.value.code == 1008
    assert rows(log, "repo.state_observed") == []


def test_a_clock_far_from_the_brains_does_not_place_the_event(tmp_path: Path) -> None:
    """A timestamp more than a week old or minutes ahead is replaced by the arrival time."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    before = int(time.time() * 1000)
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        ws.receive_json()
        _send(ws, frame(event_uid="1" * 32, ts_epoch_ms=1000))
        _send(ws, frame(event_uid="2" * 32, ts_epoch_ms=before + 3_600_000))
    with sqlite3.connect(log) as conn:
        stamps = [ts for (ts,) in conn.execute("SELECT ts_epoch_ms FROM events ORDER BY id")]
    assert len(stamps) == 2
    assert all(before <= ts <= int(time.time() * 1000) for ts in stamps)


def test_ready_carries_the_latest_repo_state_per_repo_and_the_latest_timesink_head(
    tmp_path: Path,
) -> None:
    """What an observer folds its baseline from: the newest row of each, no other type."""
    token = pair_device(tmp_path, "macbook")
    client, hub, log = _brain_client(tmp_path)
    conn = sqlite3.connect(log)
    for path, head in (("/a", "1"), ("/b", "9"), ("/a", "2")):
        emit_event(conn, type="repo.state_observed", payload={**OK_PAYLOAD, "repo_path": path,
                                                              "head_sha": head})
    timesink = {"status": "ok", "identity": "i", "span_high": 1, "span_latest_end": None,
                "capture_high": 0, "capture_latest_seen": None, "state_high": 0,
                "state_latest": None, "observed_at_ms": 1, "actor": "observer"}
    emit_event(conn, type="timesink.state_observed", payload=timesink)
    emit_event(conn, type="timesink.state_observed", payload={**timesink, "span_high": 2})
    emit_event(conn, type="mac.sleeping", payload={"ts_epoch_ms": 1})
    assert hub.events is not None
    baseline = hub.events.baseline()
    assert sorted((r["event_type"], r["payload"].get("repo_path"), r["payload"].get("head_sha"),
                   r["payload"].get("span_high")) for r in baseline) == [
        ("repo.state_observed", "/a", "2", None),
        ("repo.state_observed", "/b", "9", None),
        ("timesink.state_observed", None, None, 2),
    ]
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        assert ws.receive_json()["baseline"] == baseline


def test_a_brain_without_event_wiring_and_a_one_machine_runtime_take_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub with no log refuses events and `ready` has no baseline; `all` builds no hub.

    One machine keeps `observer.*` as the user wrote it; a brain still forces it off.
    """
    token = pair_device(tmp_path, "macbook")
    bare = TerminalHub()
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=bare,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000))
    with client.websocket_connect(
        TERMINAL_WS, headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": []}))
        assert ws.receive_json() == {"type": "ready", "device": "macbook"}
        assert _send(ws, frame())["code"] == "events_not_accepted"

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _make_repo(tmp_path / "repo")
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        f"observer:\n  repos: [{repo}]\n  timesink:\n    enabled: true\n"
        f"    db_path: {tmp_path}/t.db\n",
        encoding="utf-8",
    )
    one = bootstrap_runtime_app(runtime_root=root)
    brain_root = tmp_path / "broot"
    brain_root.mkdir()
    (brain_root / "settings.yaml").write_text(
        f"runtime:\n  role: brain\nobserver:\n  repos: [{repo}]\n", encoding="utf-8",
    )
    brain = bootstrap_runtime_app(runtime_root=brain_root)
    try:
        assert one.terminal_hub is None
        assert one.config["observer"]["repos"] == [str(repo)]
        assert one.config["observer"]["timesink"]["enabled"] is True
        assert brain.terminal_hub is not None
        assert isinstance(brain.terminal_hub.events, BrainEvents)
        assert brain.config["observer"]["repos"] == []  # the brain still observes nothing itself
        assert brain.config["observer"]["timesink"]["enabled"] is False
    finally:
        one.conn.close()
        brain.conn.close()


# --- the observers emit through the outbox --------------------------------------------------


def _unstamped(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != "observed_at_ms"}


def _sent(outbox: EventOutbox) -> list[dict[str, Any]]:
    return [json.loads(text) for text in outbox._pending.values()]  # noqa: SLF001


def test_the_repo_observer_emits_through_the_outbox_what_it_appends_on_one_machine(
    tmp_path: Path,
) -> None:
    """Same observer, same repo, two sinks: the log and the outbox hold the same events."""

    async def scenario() -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
        repo = _make_repo(tmp_path / "repo")
        log = open_event_log(tmp_path / "local.db")
        local = RepoObserver(log, [repo])
        outbox = EventOutbox()
        scratch = open_event_log(tmp_path / "scratch.db")
        remote = RepoObserver(scratch, [repo], emit_event=outbox.emit_event)
        local.poll_once()
        remote.poll_once()  # first sight of the repo: its state, no commits
        commit(repo, "second")
        commit(repo, "third")
        (repo / "dirty.txt").write_text("x", encoding="utf-8")
        local.poll_once()
        remote.poll_once()
        on_one_machine = [
            (kind, json.loads(payload)) for kind, payload in log.execute(
                "SELECT type, payload_json FROM events ORDER BY id",
            )
        ]
        assert scratch.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)  # nothing local
        return on_one_machine, _sent(outbox)

    on_one_machine, sent = asyncio.run(scenario())
    assert [(f["event_type"], _unstamped(f["payload"])) for f in sent] == [
        (kind, _unstamped(payload)) for kind, payload in on_one_machine
    ]
    assert [f["event_type"] for f in sent] == [
        "repo.state_observed", "project.commit_seen", "project.commit_seen", "repo.state_observed",
    ]
    assert all(f["type"] == "event" and len(f["event_uid"]) == 32 for f in sent)


def test_the_timesink_observer_emits_through_the_outbox_what_it_appends_on_one_machine(
    tmp_path: Path,
) -> None:
    """A head change is one event; an unchanged head, none; the payload is the log's."""

    async def scenario() -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
        store = _make_timesink(tmp_path / "timesink.sqlite")
        log = open_event_log(tmp_path / "local.db")
        local = TimesinkObserver(log, tmp_path / "timesink.sqlite")
        outbox = EventOutbox()
        remote = TimesinkObserver(
            open_event_log(tmp_path / "scratch.db"), tmp_path / "timesink.sqlite",
            emit_event=outbox.emit_event,
        )
        for observer in (local, remote):
            observer.emit(observer.collect())
        add_span(store, "2026-10-06 09:00:00.000", "2026-10-06 09:05:00.000")
        for observer in (local, remote, local, remote):
            observer.emit(observer.collect())
        store.close()
        on_one_machine = [
            (kind, json.loads(payload)) for kind, payload in log.execute(
                "SELECT type, payload_json FROM events ORDER BY id",
            )
        ]
        return on_one_machine, _sent(outbox)

    on_one_machine, sent = asyncio.run(scenario())
    assert [f["payload"]["status"] for f in sent] == ["ok", "ok"]
    assert [f["payload"]["span_high"] for f in sent] == [0, 1]
    assert [(f["event_type"], _unstamped(f["payload"])) for f in sent] == [
        (kind, _unstamped(payload)) for kind, payload in on_one_machine
    ]


def test_the_allowlist_is_what_the_moved_observers_produce(tmp_path: Path) -> None:
    """Every type the observers emit here is allowed, and nothing else is."""

    async def scenario() -> set[str]:
        repo = _make_repo(tmp_path / "repo")
        store = _make_timesink(tmp_path / "ts.sqlite")
        add_span(store, "2026-10-06 09:00:00.000", "2026-10-06 09:05:00.000")
        outbox = EventOutbox()
        repo_observer = RepoObserver(
            open_event_log(tmp_path / "a.db"), [repo], emit_event=outbox.emit_event,
        )
        repo_observer.poll_once()
        commit(repo, "second")
        repo_observer.poll_once()
        timesink = TimesinkObserver(
            open_event_log(tmp_path / "b.db"), tmp_path / "ts.sqlite", emit_event=outbox.emit_event,
        )
        timesink.emit(timesink.collect())
        store.close()
        return {f["event_type"] for f in _sent(outbox)}

    produced = asyncio.run(scenario())
    assert produced == set(OBSERVER_EVENT_TYPES)
    assert {"repo.state_observed", "project.commit_seen", "timesink.state_observed"} == set(
        OBSERVER_EVENT_TYPES,
    )


def test_the_outbox_refuses_a_type_no_observer_produces_and_keeps_a_bounded_buffer() -> None:
    """A terminal cannot queue arbitrary events; a full buffer drops the oldest.

    An ack, accepted or refused, clears an event.
    """

    async def scenario() -> None:
        outbox = EventOutbox(max_pending=3)
        conn = open_event_log(Path(":memory:"))
        with pytest.raises(ValueError, match="not an event a terminal may send"):
            outbox.emit_event(conn, type="utterance.received",
                              payload={"transcript": "x", "turn_id": "t"})
        with pytest.raises(ValueError, match="over the"):
            outbox.emit_event(conn, type="repo.state_observed",
                              payload={**OK_PAYLOAD, "pad": "x" * MAX_EVENT_CHARS})
        for n in range(5):
            outbox.emit_event(conn, type="repo.state_observed",
                              payload={**OK_PAYLOAD, "head_sha": str(n)})
        assert [f["payload"]["head_sha"] for f in _sent(outbox)] == ["2", "3", "4"]
        assert outbox.dropped == 2
        first, second, third = (f["event_uid"] for f in _sent(outbox))
        outbox.ack({"type": "ack", "event_uid": first, "ok": True})
        outbox.ack({"type": "ack", "event_uid": second, "ok": False, "code": "bad_event"})
        outbox.ack({"type": "ack", "event_uid": "unknown", "ok": True})
        outbox.ack({"type": "ack", "ok": True})
        assert [f["event_uid"] for f in _sent(outbox)] == [third]

    asyncio.run(scenario())


# --- a real terminal client, outbox and observers against a real server ----------------------


class _AsRemote:
    """ASGI shim: the server sees every peer as a device on the tailnet, not on loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": (REMOTE, 50000)}
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Brain:
    """A real uvicorn server with a real event log behind the terminal route."""

    def __init__(self, root: Path, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.log = root / "events.db"
        hub = TerminalHub(events=BrainEvents(_brain_log(self.log)))
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=hub,
                device_name=functools.partial(device_name_for_token, root),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(_AsRemote(app), host="127.0.0.1", port=self.port,
                           log_level="warning", lifespan="off"),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        _wait_for(lambda: self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 15
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


class _Terminal:
    """The terminal's link plus its observers (as `run_terminal` runs them), stoppable."""

    def __init__(self, url: str, token: str, watched: _Watched) -> None:
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self._args = (url, token, watched)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, watched = self._args
            outbox = EventOutbox()
            observing = asyncio.create_task(_observe(outbox, watched))
            self.task = asyncio.create_task(run_terminal_client(
                url, token, tools=frozenset(), execute=lambda *_: {}, events=outbox,
            ))
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                observing.cancel()
                await asyncio.wait({observing})

        self.loop.run_until_complete(main())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=10)

    def __exit__(self, *_exc: object) -> None:
        self.stop()


@pytest.fixture(autouse=True)
def _fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_link, "_RECONNECT_FIRST_S", 0.1)
    monkeypatch.setattr(terminal_link, "_RECONNECT_LAST_S", 0.3)


def test_a_real_terminal_reports_its_repo_and_timesink_to_the_brains_log(tmp_path: Path) -> None:
    """Repo state, a new commit and the TimeSink head land in the brain's log, tagged macbook."""
    token = pair_device(tmp_path, "macbook")
    repo = _make_repo(tmp_path / "repo")
    store = _make_timesink(tmp_path / "timesink.sqlite")
    add_span(store, "2026-10-06 09:00:00.000", "2026-10-06 09:05:00.000")
    watched = _Watched((str(repo),), 0.2, tmp_path / "timesink.sqlite", 0.2)
    with _Brain(tmp_path) as brain, _Terminal(brain.url, token, watched):
        _wait_for(lambda: bool(rows(brain.log, "repo.state_observed")
                               and rows(brain.log, "timesink.state_observed")),
                  "the first observations never arrived")
        commit(repo, "second")
        _wait_for(lambda: bool(rows(brain.log, "project.commit_seen")), "no commit arrived")
    found = rows(brain.log, "repo.state_observed", "project.commit_seen", "timesink.state_observed")
    assert {(kind, node) for kind, node, _ in found} == {
        ("repo.state_observed", "macbook"),
        ("project.commit_seen", "macbook"),
        ("timesink.state_observed", "macbook"),
    }
    commits = [p for kind, _, p in found if kind == "project.commit_seen"]
    assert [(c["repo_path"], c["subject"]) for c in commits] == [(str(repo), "second")]
    store.close()


def test_a_restarted_terminal_continues_from_the_brains_baseline(tmp_path: Path) -> None:
    """No repeat of what the brain already has.

    A commit made while the terminal was down is reported when it is back.
    """
    token = pair_device(tmp_path, "macbook")
    repo = _make_repo(tmp_path / "repo")
    watched = _Watched((str(repo),), 0.2, None, 300)
    with _Brain(tmp_path) as brain:
        with _Terminal(brain.url, token, watched):
            _wait_for(lambda: bool(rows(brain.log, "repo.state_observed")), "first state")
        commit(repo, "while down")
        with _Terminal(brain.url, token, watched):
            _wait_for(lambda: bool(rows(brain.log, "project.commit_seen")), "the missed commit")
            time.sleep(0.8)  # several polls: an unchanged repo must stay quiet
    states = rows(brain.log, "repo.state_observed")
    commits = rows(brain.log, "project.commit_seen")
    assert [s["last_commit_subject"] for _, _, s in states] == ["first", "while down"]
    assert [c["subject"] for _, _, c in commits] == ["while down"]


def test_events_made_while_the_brain_is_unreachable_are_delivered_once_it_is_back(
    tmp_path: Path,
) -> None:
    """The terminal keeps what it saw in memory and sends it on reconnect, each event once."""
    token = pair_device(tmp_path, "macbook")
    repo = _make_repo(tmp_path / "repo")
    watched = _Watched((str(repo),), 0.1, None, 300)
    port = _free_port()
    with _Terminal(f"http://127.0.0.1:{port}", token, watched):
        with _Brain(tmp_path, port) as first:
            _wait_for(lambda: bool(rows(first.log, "repo.state_observed")), "first state")
        # The brain is gone; the terminal keeps polling and queues what it sees.
        commit(repo, "one")
        commit(repo, "two")
        time.sleep(1.0)
        assert [r[2]["subject"] for r in rows(first.log, "project.commit_seen")] == []
        with _Brain(tmp_path, port) as second:
            _wait_for(lambda: len(rows(second.log, "project.commit_seen")) == 2,
                      "buffered commits were never delivered")
            time.sleep(0.5)
    commits = rows(second.log, "project.commit_seen")
    assert [c["subject"] for _, _, c in commits] == ["one", "two"]
    assert {node for _, node, _ in commits} == {"macbook"}
    assert terminal_events.MAX_PENDING >= 2
