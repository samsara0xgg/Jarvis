"""ADR 0170 step 4c: a brain's reads of TimeSink and git go to its terminal.

Acceptance checks with the real reader code against temp fixtures (a git repo, a TimeSink store in
its real schema), through a real terminal client connected to a real brain hub over a real
socket. The measure each time is the one-machine answer: the same fixtures read by the same
readers with the store and the repositories on this machine. A brain's answer must be that
answer, from the terminal; with no terminal connected it must say plainly that the device is not
there, in the tool's own result and, for the background jobs, in the coverage they already keep.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from datetime import time as dtime
from typing import TYPE_CHECKING, Any, Self

import pytest
import uvicorn

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import build_default_registry, make_screen_capture
from jarvis.runtime import _reads_connected, bootstrap_runtime_app
from jarvis.runtime.daily_report import DailyReportService, DailySchedule
from jarvis.runtime.terminal import _declared, _observe, _Watched, make_executor
from jarvis.runtime.work_state import WorkStateService
from jarvis.shared.device_link import DeviceCallError
from jarvis.state import device_reads, timesink
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_report import (
    claim_originals,
    gather_day,
    read_detail,
    resolve_zone,
    search_day,
)
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import append_record, open_memory_db
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.work_state import gather_evidence
from jarvis.surface import terminal_link
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.terminal_events import BrainEvents, EventOutbox
from jarvis.surface.terminal_link import TerminalHub, run_terminal_client
from tests.integration.test_daily_report import (
    DAY,
    NOW,
    TZ,
    ZONE,
    CannedReporter,
    _one_item,
    git_repo,
)
from tests.integration.test_flat_tool_dispatch import _Fixture, _request
from tests.integration.test_terminal_observers import (
    _AsRemote,
    _free_port,
    _make_repo,
    _wait_for,
    commit,
    rows,
)
from tests.integration.test_timesink_activity import add_capture, add_event, add_span, source

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from jarvis.state.projects import Project
    from jarvis.surface.claude_sessions import ClaudeSessions

__all__ = ["source"]

FROM, TO = "2026-09-19T07:00:00-07:00", "2026-09-20T07:00:00-07:00"
"""The local day of 2026-09-19 in Vancouver."""
QUERY = {"from": FROM, "to": TO, "sources": ["app", "screen"]}


# --- the rig: a brain's hub on a real socket, a terminal with its own files ---------------------


class _Brain:
    """A real uvicorn server carrying a hub on ``/terminal/ws``, over a real event log."""

    def __init__(
        self, root: Path, log: Path,
        deps: Callable[[TerminalHub], dict[str, Any]] | None = None,
    ) -> None:
        self.hub = TerminalHub(events=BrainEvents(_brain_log(log)))
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, root),
                **({} if deps is None else deps(self.hub)),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.port = _free_port()
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


def _brain_log(path: Path) -> sqlite3.Connection:
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


class _Terminal:
    """A terminal's link as ``run_terminal`` builds it: the real executor over its own files."""

    def __init__(  # noqa: PLR0913 — the link, the device's files and what watches them.
        self,
        url: str,
        token: str,
        *,
        store: Path | None,
        repos: tuple[str, ...],
        watched: _Watched | None = None,
        projects: tuple[Project, ...] = (),
        claude: ClaudeSessions | None = None,
    ) -> None:
        registry = build_default_registry(obsidian_vault_root=None)
        registry.register(make_screen_capture(800))
        self._args = (
            url, token, _declared(registry),
            make_executor(
                registry, timesink_store=store, repos=repos, projects=projects, claude=claude,
            ),
            watched,
        )
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, tools, execute, watched = self._args
            outbox = None if watched is None else EventOutbox()
            observing = None if watched is None or outbox is None else asyncio.create_task(
                _observe(outbox, watched),
            )
            self.task = asyncio.create_task(run_terminal_client(
                url, token, tools=tools, execute=execute, events=outbox,
            ))
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                if observing is not None:
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


class _Tools:
    """The real default registry, log and dispatcher of one machine, as a tool caller sees them."""

    def __init__(self, root: Path, **registry: Any) -> None:  # noqa: ANN401 — build_default_registry's.
        root.mkdir(exist_ok=True)
        self.memory = root / "memory.db"
        with closing(open_memory_db(self.memory)):
            pass
        self.log = bootstrap_runtime(root).event_log
        self.registry = build_default_registry(memory_db_path=self.memory, **registry)
        self.fx = _Fixture(root, tools=self.registry.get_definitions())
        self.sequence = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch and decode the persisted output; an error has ``error`` and ``code``."""
        self.sequence += 1
        result = self.fx.dispatch(_request(name, f"t{self.sequence}", arguments=args))
        assert result.slots[0].tool_output is not None
        return dict(json.loads(result.slots[0].tool_output))


def _fixtures(tmp_path: Path, store: sqlite3.Connection) -> tuple[Path, dict[str, str]]:
    """One day on the device: spans, captures, state events; its repo and the commits' SHAs."""
    add_span(store, "2026-09-19 16:00:00.000", "2026-09-19 17:00:00.000", title="Jobs at RBC")
    add_span(store, "2026-09-19 17:00:00.000", "2026-09-19 17:30:00.000", title="cc | daily",
             bundle="com.apple.Terminal", name="Terminal")
    add_capture(store, "2026-09-19 16:10:00.000", "2026-09-19 16:20:00.000",
                text="部署成功 deploy finished")
    add_capture(store, "2026-09-19 17:05:00.000", "2026-09-19 17:10:00.000",
                text="the quick brown fox", image=None)
    add_event(store, "2026-09-19 16:30:00.000", "idle")
    add_event(store, "2026-09-19 16:40:00.000", "active")
    repo = tmp_path / "repo"
    shas = git_repo(repo, [
        ("feat(state): first", "2026-09-19T09:00:00-07:00", "main"),
        ("fix(state): second", "2026-09-19T11:30:00-07:00", "main"),
    ])
    return repo, shas


@dataclasses.dataclass
class _Rig:
    """A device with its files, a brain with a hub, and the one-machine reference."""

    store: Path
    repo: Path
    shas: dict[str, str]
    brain: _Brain
    terminal: _Terminal
    device: Callable[..., dict[str, Any]]


@pytest.fixture
def rig(tmp_path: Path, source: sqlite3.Connection) -> Iterator[_Rig]:
    """The device's own files, a brain and a connected terminal."""
    repo, shas = _fixtures(tmp_path, source)
    store = tmp_path / "timesink.sqlite"
    token = pair_device(tmp_path, "macbook")
    log = bootstrap_runtime(tmp_path / "brain-root").event_log
    with _Brain(tmp_path, log) as brain, _Terminal(
        brain.url, token, store=store, repos=(str(repo),),
    ) as terminal:
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        yield _Rig(store, repo, shas, brain, terminal, brain.hub.call)


def _disconnect(rig: _Rig) -> None:
    rig.terminal.stop()
    _wait_for(lambda: not rig.brain.hub.connected(), "the terminal never left")


# --- the timeline tool ---------------------------------------------------------------------


def test_a_brains_timeline_answers_what_one_machine_answers_from_the_terminals_store(
    tmp_path: Path, rig: _Rig,
) -> None:
    """query_activity and read_activity on a brain equal the one-machine answer, to the byte."""
    one = _Tools(tmp_path / "all", timesink_db_path=rig.store, observed_repos=(str(rig.repo),))
    brain = _Tools(tmp_path / "brain", device_link=rig.device)

    want = one.call("query_activity", QUERY)
    got = brain.call("query_activity", QUERY)
    assert want["total"] == 4
    assert want["store"] is not None
    assert {a: (t["foreground_s"], t["screen_rows"]) for a, t in want["totals"].items()} == {
        "A": (3600, 2), "B": (1800, 0),  # Chrome an hour with two captures, Terminal half an hour
    }
    assert [k for _, k in want["state_events"]] == ["idle", "active"]
    assert got == want

    for source_name in ("app", "screen"):
        only = {**QUERY, "sources": [source_name], "app": "chrome"}
        assert brain.call("query_activity", only) == one.call("query_activity", only)
    summary = {**QUERY, "summary_only": True}
    assert brain.call("query_activity", summary) == one.call("query_activity", summary)

    for ref in (row[0] for row in want["rows"]):
        assert brain.call("read_activity", {"activity_id": ref}) == one.call(
            "read_activity", {"activity_id": ref},
        )
    missing = brain.call("read_activity", {"activity_id": "c999:" + "0" * 8})
    assert missing == one.call("read_activity", {"activity_id": "c999:" + "0" * 8})
    assert missing["code"] == "not_found"  # the reader's own refusal, carried as it is


def test_a_brains_git_rows_are_the_terminals_observations_and_its_coverage_names_the_repos(
    tmp_path: Path, source: sqlite3.Connection,
) -> None:
    """The observers' commits reach the brain's log and the timeline lists them as on one machine.

    The git source reads the log, so what the terminal pushed is what it returns; what a brain
    cannot know alone is whether a repository is watched, which it asks the terminal.
    """
    del source
    repo = _make_repo(tmp_path / "watched")
    token = pair_device(tmp_path, "macbook")
    log = bootstrap_runtime(tmp_path / "brain").event_log
    with _Brain(tmp_path, log) as brain, _Terminal(
        brain.url, token, store=None, repos=(str(repo),),
        watched=_Watched((str(repo),), 0.1, None, 300),
    ):
        _wait_for(lambda: bool(rows(log, "repo.state_observed")), "no first state")
        commit(repo, "second")
        _wait_for(lambda: bool(rows(log, "project.commit_seen")), "no commit")
        on_brain = _Tools(tmp_path / "brain", device_link=brain.hub.call)
        window = {"from": "2020-01-01T00:00:00Z", "to": "2099-01-01T00:00:00Z",
                  "sources": ["git"]}
        got = on_brain.call("query_activity", window)
    texts = [r[4].removeprefix(f"{repo}: ") for r in got["rows"]]
    assert "first" in texts
    assert any(t.startswith("second (committed ") for t in texts)
    assert all(r[4].startswith(f"{repo}: ") for r in got["rows"])
    assert got["coverage"]["git"]["configured_now"] is True
    assert got["coverage"]["git"]["observations_in_window"] == len(texts)
    with sqlite3.connect(log) as conn:
        assert {n for (n,) in conn.execute("SELECT DISTINCT ingestion_node FROM events "
                                           "WHERE type LIKE 'repo.%' OR type LIKE 'project.%'")
                } == {"macbook"}

    # One machine with the same repository configured and the same events in its own log.
    one = _Tools(tmp_path / "all", observed_repos=(str(repo),))
    with closing(open_event_log(one.log)) as theirs, sqlite3.connect(log) as conn:
        for uid, kind, ts, payload in conn.execute(
            "SELECT event_uid, type, ts_epoch_ms, payload_json FROM events "
            "WHERE type IN ('repo.state_observed','project.commit_seen') ORDER BY id"
        ):
            emit_event(theirs, type=kind, payload=json.loads(payload), ts_epoch_ms=ts,
                       event_uid=uid, actor="observer")
    assert one.call("query_activity", window)["rows"] == got["rows"]
    assert one.call("query_activity", window)["coverage"] == got["coverage"]


def test_with_no_terminal_the_timeline_says_the_device_is_not_there(
    tmp_path: Path, rig: _Rig,
) -> None:
    """App and screen: an error whose sentence names the device; git: the log's rows, flagged."""
    brain = _Tools(tmp_path / "brain", device_link=rig.device)
    ref = brain.call("query_activity", QUERY)["rows"][0][0]
    _disconnect(rig)

    refused = brain.call("query_activity", QUERY)
    assert refused["code"] == "device_not_connected"
    assert "unavailable" in refused["error"]
    assert "device is not connected" in refused["error"]

    gone = brain.call("read_activity", {"activity_id": ref})
    assert gone["code"] == "device_not_connected"
    assert gone["error"] == device_reads.NOT_CONNECTED

    emit_event(
        open_event_log(brain.log), type="project.commit_seen",
        payload={"repo_path": "/r", "commit_sha": "a" * 40, "subject": "recorded earlier",
                 "committed_at_ms": int(datetime(2026, 9, 19, 18, tzinfo=UTC).timestamp() * 1000),
                 "actor": "observer"},
        ts_epoch_ms=int(datetime(2026, 9, 19, 18, tzinfo=UTC).timestamp() * 1000),
    )
    listed = brain.call("query_activity", {**QUERY, "sources": ["git"]})
    assert [r[4] for r in listed["rows"]] == ["/r: recorded earlier"]
    git = listed["coverage"]["git"]
    assert git["configured_now"] is None
    assert device_reads.NOT_CONNECTED in git["reason"]
    assert "missing" in git["reason"]


def test_a_saved_item_citing_the_devices_data_is_checked_by_the_terminal_before_the_write(
    tmp_path: Path, rig: _Rig,
) -> None:
    """A TimeSink ref is verified on the terminal; a stale or unreachable one is not saved."""
    brain = _Tools(tmp_path / "brain", device_link=rig.device)
    page = brain.call("query_activity", QUERY)
    span = next(r for r in page["rows"] if r[0].startswith("s"))
    full = brain.call("read_activity", {"activity_id": span[0]})["source_refs"][0]
    stale = full[:-8] + ("0" if not full.endswith("0") else "1") * 8

    def save(ref: str, request: str) -> dict[str, Any]:
        return brain.call("save_knowledge", {
            "statement": "RBC jobs page was open", "kind": "fact", "basis": "observation",
            "source_refs": [ref], "request_id": request,
        })

    saved = save(full, "k1")
    assert saved.get("code") is None
    assert saved["version"] == 1
    assert save(stale, "k2")["code"] == "source_changed"
    _disconnect(rig)
    after = save(full, "k3")
    assert after["code"] == "device_not_connected"
    assert after["error"] == device_reads.NOT_CONNECTED
    with closing(sqlite3.connect(brain.log)) as conn:
        saved_rows = conn.execute("SELECT type FROM events WHERE type='knowledge.revised'")
        assert len(saved_rows.fetchall()) == 1


# --- the day's collection ------------------------------------------------------------------


def _day(conn: sqlite3.Connection, **kwargs: Any) -> Any:  # noqa: ANN401 — gather_day's stores.
    zone_name, zone = resolve_zone(ZONE, TZ)
    return gather_day(
        conn, day=DAY, zone_name=zone_name, zone=zone, now=NOW, **kwargs,
    )


def _record(memory: Path, identity: str, text: str) -> None:
    """One of Allen's own lines, said at 11:00 on the day."""
    append_record(memory, record_id=identity, source="allen", text=text)
    with closing(sqlite3.connect(memory)) as db:
        db.execute("UPDATE records SET ts=? WHERE id=?", ("2026-09-19T11:00:00-07:00", identity))
        db.commit()


def _log_with_commit(path: Path, rig: _Rig) -> sqlite3.Connection:
    """A log where the repo observer saw the day's second commit a minute after it was made."""
    conn = open_event_log(path)
    emit_event(
        conn, type="project.commit_seen",
        ts_epoch_ms=int(datetime.fromisoformat("2026-09-19T11:31:00-07:00").timestamp() * 1000),
        payload={
            "repo_path": str(rig.repo), "commit_sha": rig.shas["fix(state): second"],
            "subject": "fix(state): second",
            "committed_at_ms": int(
                datetime.fromisoformat("2026-09-19T11:30:00-07:00").timestamp() * 1000,
            ),
            "actor": "observer",
        },
    )
    return conn


def test_the_days_material_on_a_brain_equals_one_machines_and_names_the_gap_without_a_terminal(
    tmp_path: Path, rig: _Rig,
) -> None:
    """Windows, captures, state, commits, searches and originals; then the same with no terminal."""
    memory = tmp_path / "memory.db"
    with closing(open_memory_db(memory)):
        pass
    _record(memory, "rec-1", "明天继续写日报工具。")
    conn = _log_with_commit(tmp_path / "day-log.db", rig)

    want = _day(conn, memory_path=memory, timesink_path=rig.store, repos=(str(rig.repo),))
    got = _day(conn, memory_path=memory, timesink_path=None, repos=(), device=rig.device)
    assert dataclasses.asdict(got) == dataclasses.asdict(want)
    assert want.coverage["app"] != "unavailable"
    assert want.coverage["git"] == "available"
    assert {"a1", "s1", "g1", "g2"} <= set(want.refs)
    assert [row["subject"] for row in want.sections["git"]] == [
        "feat(state): first", "fix(state): second",
    ]

    zone = resolve_zone(ZONE, TZ)[1]
    for query in ("deploy", "quick fox", "nothing of the kind"):
        assert search_day(got, query, timesink_path=None, zone=zone, device=rig.device) == (
            search_day(want, query, timesink_path=rig.store, zone=zone)
        )
    kwargs: dict[str, Any] = {"conn": conn, "memory_path": memory}
    for key in sorted(want.refs):
        assert read_detail(key, got, **kwargs, timesink_path=None, device=rig.device) == (
            read_detail(key, want, **kwargs, timesink_path=rig.store)
        )
    keys = sorted(want.refs)
    assert claim_originals(keys, got, terms=["second"], **kwargs, timesink_path=None,
                           device=rig.device) == claim_originals(
        keys, want, terms=["second"], **kwargs, timesink_path=rig.store,
    )

    _disconnect(rig)
    gap = _day(conn, memory_path=memory, timesink_path=None, repos=(), device=rig.device)
    assert (gap.coverage["app"], gap.coverage["screen"], gap.coverage["git"]) == (
        "unavailable", "unavailable", "unavailable",
    )
    assert gap.coverage["records"] == "available"
    for name in ("app", "screen", "git"):
        assert device_reads.NOT_CONNECTED in gap.served[name]
    assert any(device_reads.NOT_CONNECTED in limit for limit in gap.limits)
    assert [row["key"] for row in gap.sections["records"]] == ["r1"]
    assert [row["subject"] for row in gap.sections["git"]] == ["fix(state): second"]  # the log's
    assert not gap.empty

    assert "not connected" in search_day(
        want, "deploy", timesink_path=None, zone=zone, device=rig.device,
    )
    assert "cannot be read right now" in read_detail(
        "s1", want, **kwargs, timesink_path=None, device=rig.device,
    )
    originals = claim_originals(
        ["s1", "g1"], want, terms=[], **kwargs, timesink_path=None, device=rig.device,
    )
    assert [o["kind"] for o in originals] == ["unreadable", "unreadable"]
    conn.close()


class _Service:
    """A daily-report service over the day's files, on one machine or as a brain."""

    def __init__(self, root: Path, *, device: Callable[..., dict[str, Any]] | None,
                 store: Path | None, repos: tuple[str, ...]) -> None:
        root.mkdir()
        self.memory = root / "memory.db"
        with closing(open_memory_db(self.memory)):
            pass
        self.reporter = CannedReporter()
        self.service = DailyReportService(
            memory_path=self.memory, timesink_path=store, repos=repos, reporter=self.reporter,
            model="canned", tz=TZ, device=device,
        )
        self.conn = open_event_log(root / "events.db")

    def run(self) -> dict[str, Any]:
        return self.service.run(self.conn, action_id="dr1", now=NOW, local_date=DAY.isoformat(),
                                timezone=ZONE)


def test_the_daily_report_on_a_brain_cites_the_terminals_data_and_records_the_gap_without_one(
    tmp_path: Path, rig: _Rig,
) -> None:
    """Generated citing what the terminal verified; with none, the coverage says what is missing."""
    on_brain = _Service(tmp_path / "svc", device=rig.device, store=None, repos=())
    _record(on_brain.memory, "rec-1", "明天继续写日报工具。")
    on_brain.reporter.report = _one_item("日报工具", "completed", ["r1", "s1", "a1"])
    done = on_brain.run()
    assert done["outcome"] == "generated", done
    assert done["coverage"]["app"] != "unavailable"
    assert done["evidence_counts"]["windows"] == 2

    _disconnect(rig)
    again = _Service(tmp_path / "svc2", device=rig.device, store=None, repos=())
    nothing = again.run()
    assert nothing["outcome"] == "no_evidence"
    assert nothing["coverage"]["app"] == nothing["coverage"]["git"] == "unavailable"
    assert device_reads.NOT_CONNECTED in nothing["served"]["app"]
    assert any(device_reads.NOT_CONNECTED in limit for limit in nothing["limits"])

    again.reporter.report = _one_item("日报工具", "completed", ["r1"])
    _record(again.memory, "rec-1", "明天继续写日报工具。")
    partial = again.run()
    assert partial["outcome"] == "generated", partial
    assert (partial["coverage"]["app"], partial["coverage"]["screen"],
            partial["coverage"]["git"]) == ("unavailable",) * 3
    assert partial["coverage"]["records"] == "available"


def test_a_day_written_while_the_terminal_was_away_is_written_again_when_it_connects(
    tmp_path: Path, source: sqlite3.Connection,
) -> None:
    """The schedule's attempt at its hour is not its last.

    A day whose coverage was marked unavailable is written again once a terminal that reads
    TimeSink and git is there, and then left alone.
    """
    repo, _ = _fixtures(tmp_path, source)
    store = tmp_path / "timesink.sqlite"
    token = pair_device(tmp_path, "macbook")
    log = bootstrap_runtime(tmp_path / "brain-root").event_log
    with _Brain(tmp_path, log) as brain:
        svc = _Service(tmp_path / "svc", device=brain.hub.call, store=None, repos=())
        schedule = DailySchedule(
            svc.service, event_log_path=tmp_path / "svc" / "events.db", at=dtime(5), zone=TZ,
            device_ready=functools.partial(_reads_connected, brain.hub),
        )
        assert schedule.catch_up() is None  # nothing was written yet

        first = schedule.write(DAY, now=NOW)  # the hour comes, no terminal, no evidence at all
        assert (first["outcome"], first["device_gap"]) == ("no_evidence", True)
        assert schedule.catch_up() is None, "no terminal to ask yet"

        _record(svc.memory, "rec-1", "明天继续写日报工具。")
        svc.reporter.report = _one_item("日报工具", "completed", ["r1"])
        partial = schedule.write(DAY, now=NOW, catch_up=True)  # still no terminal
        assert (partial["outcome"], partial["device_gap"]) == ("generated", True)
        assert partial["coverage"]["app"] == "unavailable"
        assert schedule.catch_up() is None

        with _Terminal(brain.url, token, store=store, repos=(str(repo),)):
            _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
            assert schedule.catch_up() == DAY
            svc.reporter.report = _one_item("日报工具", "completed", ["r1", "s1", "a1"])
            done = schedule.write(DAY, now=NOW, catch_up=True)
            assert (done["outcome"], done["device_gap"]) == ("generated", False)
            assert done["version"] == partial["version"] + 1
            assert done["coverage"]["app"] != "unavailable"
            assert done["evidence_counts"]["windows"] == 2
            assert schedule.catch_up() is None, "the day is whole: nothing more to catch up"

        assert schedule.due(datetime(2026, 9, 20, 12, 1, tzinfo=UTC)) is None  # no second attempt


def test_a_catch_up_gives_up_after_its_few_tries_and_a_one_machine_schedule_has_none(
    tmp_path: Path,
) -> None:
    """A terminal that stays unreadable is not asked for more model calls without end."""
    svc = _Service(tmp_path / "svc", device=lambda *_: (_ for _ in ()).throw(
        DeviceCallError("gone", code="device_timeout"),
    ), store=None, repos=())
    _record(svc.memory, "rec-1", "明天继续写日报工具。")
    svc.reporter.report = _one_item("日报工具", "completed", ["r1"])
    schedule = DailySchedule(
        svc.service, event_log_path=tmp_path / "svc" / "events.db", at=dtime(5), zone=TZ,
        device_ready=lambda: True,
    )
    assert schedule.write(DAY, now=NOW)["device_gap"] is True
    for _ in range(DailySchedule.CATCH_UPS):
        assert schedule.catch_up() == DAY
        assert schedule.write(DAY, now=NOW, catch_up=True)["device_gap"] is True
    assert schedule.catch_up() is None

    alone = DailySchedule(svc.service, event_log_path=tmp_path / "svc" / "events.db",
                          at=dtime(5), zone=TZ)
    alone.write(DAY, now=NOW)
    assert alone.catch_up() is None


# --- the work state ------------------------------------------------------------------------


def test_the_work_state_evidence_on_a_brain_equals_one_machines_and_names_the_gap(
    tmp_path: Path, rig: _Rig,
) -> None:
    """The same bounded material from the terminal's store; with none, coverage and a limit."""
    memory = tmp_path / "memory.db"
    with closing(open_memory_db(memory)):
        pass
    conn = _log_with_commit(tmp_path / "ws-log.db", rig)
    now = datetime(2026, 9, 19, 18, 0, tzinfo=UTC)
    args: dict[str, Any] = {"memory_path": memory, "note": "n", "question": "deploy fox", "tz": TZ,
                            "now": now}
    want = gather_evidence(conn, timesink_path=rig.store, repos=(str(rig.repo),), **args)
    got = gather_evidence(conn, timesink_path=None, repos=(), device=rig.device, **args)
    assert dataclasses.asdict(got) == dataclasses.asdict(want)
    assert want.coverage["app"] != "unavailable"
    assert want.coverage["git"] == "partial"
    assert want.counts["git"] == 1
    assert want.counts["recent"] + want.counts["earlier"] > 0

    _disconnect(rig)
    gap = gather_evidence(conn, timesink_path=None, repos=(), device=rig.device, **args)
    assert (gap.coverage["app"], gap.coverage["screen"], gap.coverage["git"]) == (
        "unavailable", "unavailable", "unavailable",
    )
    assert sum(device_reads.NOT_CONNECTED in limit for limit in gap.limits) == 2  # timesink, git
    assert gap.counts["git"] == 1  # the observed commit is the brain's own record
    assert gap.counts["recent"] + gap.counts["earlier"] == 0
    conn.close()


def test_the_work_states_head_check_comes_from_the_terminal_and_never_fails_without_one(
    tmp_path: Path, rig: _Rig,
) -> None:
    """The dashboard's "checked" clock: the terminal's head; absent a terminal, left as it was."""
    service = WorkStateService(
        event_log_path=tmp_path / "e.db", memory_path=None, timesink_path=None, repos=(),
        analyst=None, model="canned", tz=TZ, device=rig.device,
    )
    service._note_head()  # noqa: SLF001 — the head read of a refresh.
    assert service._checked is not None  # noqa: SLF001
    head = service._checked[1]  # noqa: SLF001
    assert (head["status"], head["span_high"], head["capture_high"]) == ("ok", 2, 2)
    before = service._checked  # noqa: SLF001
    _disconnect(rig)
    service._note_head()  # noqa: SLF001
    assert service._checked is before  # noqa: SLF001


# --- role all, and the terminal's own limits -----------------------------------------------


def test_the_menu_a_brain_offers_is_the_menu_one_machine_offers(
    tmp_path: Path, rig: _Rig,
) -> None:
    """The timeline's name, description and schema, and every activity tool's whole definition."""
    (tmp_path / "m").mkdir()
    kwargs: dict[str, Any] = {"memory_db_path": tmp_path / "m" / "memory.db"}
    one = build_default_registry(**kwargs)
    brain = build_default_registry(**kwargs, device_link=rig.device)
    by = {t.name: t for t in one.get_definitions()}
    timeline = {t.name: t for t in brain.get_definitions()}["query_activity"]
    assert (timeline.name, timeline.description, timeline.input_schema) == (
        by["query_activity"].name, by["query_activity"].description,
        by["query_activity"].input_schema,
    )
    own = {t.name: _hash_of(t) for t in one.get_definitions()}
    theirs = {t.name: _hash_of(t) for t in brain.get_definitions()}
    activity = {"query_activity", "read_activity", "save_knowledge", "save_briefing",
                "get_briefing", "search_knowledge", "search_records", "read_records", "recall"}
    assert {n: h for n, h in theirs.items() if n in activity} == {
        n: h for n, h in own.items() if n in activity
    }


def _hash_of(tool: Any) -> str:  # noqa: ANN401 — one definition.
    return hashlib.sha256(json.dumps(
        [tool.name, tool.description, tool.input_schema, tool.risk_level, tool.read_only,
         sorted(c.value for c in tool.allowed_callers)], sort_keys=True, default=str,
    ).encode()).hexdigest()


def test_one_machine_wires_no_device_into_its_readers_and_a_brain_wires_its_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`all` hands every reader no link; `brain` hands each the hub's `call`."""
    runtimes = []
    for name, role in (("all", "all"), ("brain", "brain")):
        root = tmp_path / name / "root"
        root.mkdir(parents=True)
        (root / "settings.yaml").write_text(
            f"runtime:\n  role: {role}\ndaily_report:\n  at: '03:00'\n", encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(tmp_path / name))
        runtimes.append(bootstrap_runtime_app(runtime_root=root))
    one, brain = runtimes
    try:
        assert one.terminal_hub is None
        assert brain.terminal_hub is not None
        assert one.work_state is not None
        assert brain.work_state is not None
        assert one.daily_schedule is not None
        assert brain.daily_schedule is not None
        assert one.work_state._device is None  # noqa: SLF001
        assert one.daily_schedule._service._device is None  # noqa: SLF001
        assert one.daily_schedule._device_ready is None  # noqa: SLF001
        assert brain.work_state._device == brain.terminal_hub.call  # noqa: SLF001
        assert brain.daily_schedule._service._device == brain.terminal_hub.call  # noqa: SLF001
        assert brain.daily_schedule._device_ready is not None  # noqa: SLF001
    finally:
        one.conn.close()
        brain.conn.close()


def test_the_terminal_reads_only_what_its_config_names_and_refuses_the_rest(
    tmp_path: Path, rig: _Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository it does not watch, an unknown read, a reply too large: refused with a code."""
    outsider = tmp_path / "outsider"
    git_repo(outsider, [("x", "2026-09-19T09:00:00-07:00", "main")])
    sha = "0" * 40
    for fn, args in (("show", {"repo": str(outsider), "sha": sha}),
                     ("exists", {"repo": str(outsider), "sha": sha})):
        with pytest.raises(DailyError) as refused:
            device_reads.ask(rig.device, device_reads.GIT_READ, fn, **args)
        assert refused.value.code == "not_watched"
    with pytest.raises(DailyError) as unknown:
        device_reads.ask(rig.device, device_reads.TIMESINK_READ, "drop_table")
    assert unknown.value.code == "unknown_read"
    with pytest.raises(DailyError) as bad:
        device_reads.ask(rig.device, device_reads.TIMESINK_READ, "query_spans", start="x")
    assert bad.value.code == "bad_request"
    monkeypatch.setattr("jarvis.runtime.terminal._MAX_READ_CHARS", 10)
    with pytest.raises(DailyError) as large:
        device_reads.ask(rig.device, device_reads.TIMESINK_READ, "capture_texts", ids=[1, 2])
    assert large.value.code == "result_too_large"


def test_a_terminal_with_no_timesink_or_repositories_answers_that_it_has_none(
    tmp_path: Path, source: sqlite3.Connection,
) -> None:
    """Its config is the truth: no store is the existing unavailable coverage, no repos is none."""
    del source
    token = pair_device(tmp_path, "macbook")
    log = bootstrap_runtime(tmp_path / "brain-root").event_log
    with _Brain(tmp_path, log) as brain, _Terminal(
        brain.url, token, store=None, repos=(),
    ):
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        tools = _Tools(tmp_path / "brain", device_link=brain.hub.call)
        page = tools.call("query_activity", QUERY)
        assert page["store"] is None
        assert page["coverage"]["app"]["status"] == "unavailable"
        git = tools.call("query_activity", {**QUERY, "sources": ["git"]})
        assert git["coverage"]["git"]["configured_now"] is False
        with timesink.snapshot(None, brain.hub.call) as snap:
            assert snap is None  # the terminal has no store: the existing "unreadable" path
