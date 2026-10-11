"""ADR 0228: typed turns with files over the phone socket, live steps, and the steps summary.

Acceptance checks against a real uvicorn server with the real ``/phone/ws`` route, the real
``_PhoneRows`` cursors over a real event log, the real attachment store and the real
``_add_steps`` behind ``GET /inherent/conversation``. No model and no key: the tool calls are the
rows the dispatcher writes (``action.proposed``, ``action.running``, the terminal row), appended by
the test with the same shapes and the same correlation keys. The phone is the fake client.

Shown: (a) a typed ``say`` that names a stored file opens the same turn ``POST /inherent/submit``
opens with it, is acknowledged at once, is answered in rows, and the store's refusals, blank words
and a resend behave as the HTTP route's do; (b) a turn's tool calls reach its own device as
``step`` frames, running then done or failed, labelled from the table with a safe detail, in log
order with the rows, for typed and spoken turns alike and never for another device's turn; (c) the
conversation row of her answer carries the steps, read from the log; (d) the label table, as data,
in both languages.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest
import uvicorn

from jarvis.runtime import inherent_loop
from jarvis.shared import lang, steps
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import append_record, conversation_rows
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents
from tests.integration.test_terminal_voice import _AsRemote, _free_port, _wait_for
from tools.phone_fake_client import FakePhone

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


class _Host:
    """The phone route, the attachment routes and the conversation route on one log."""

    def __init__(self, root: Path, *, files: bool = True) -> None:
        self.root = root
        self.files = files
        self.log = root / "events.db"
        self.memory = root / "memory.db"
        open_event_log(self.log).close()
        self.store = Attachments(root / "artifacts" / ATTACHMENTS_DIRNAME)
        self.token = pair_device(root, "iphone")
        self.other_token = pair_device(root, "ipad")
        self.port = _free_port()
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def _conversation(self, after: int, limit: int, before: int) -> dict[str, Any]:
        """What the daemon's ``_read_conversation`` does, with the real step lookup."""
        rows, more = conversation_rows(
            self.memory,
            since="",
            after=after,
            before=before,
            limit=limit,
        )
        inherent_loop._add_steps(self.log, rows)  # noqa: SLF001
        return {"since": "", "rows": rows, "has_more": more}

    async def _main(self) -> None:
        conn = sqlite3.connect(self.log, check_same_thread=False)
        hub = PhoneHub(
            events=BrainEvents(conn),
            rows=inherent_loop._PhoneRows(conn),  # noqa: SLF001
            check_attachments=self.store.check_for_turn if self.files else None,
        )
        submit_files = functools.partial(inherent_loop._submit_with_attachments, self.log)  # noqa: SLF001
        submit_as_device = functools.partial(inherent_loop._submit_as_device, self.log)  # noqa: SLF001
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=functools.partial(device_name_for_token, self.root),
                attachments=self.store,
                submit_attachments=submit_files,
                submit_as_device=submit_as_device,
                conversation_read=self._conversation,
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.root)),
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                _AsRemote(app),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            ),
        )
        await self.server.serve()

    def __enter__(self) -> Self:
        self.thread.start()
        _wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def phone(self, token: str | None = None) -> FakePhone:
        phone = FakePhone(
            f"ws://127.0.0.1:{self.port}{PHONE_PATH}",
            token or self.token,
            voice=False,
        ).start()
        phone.hello()
        return phone

    def http(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        return httpx.request(
            method,
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
            **kwargs,
        )

    def upload(self, content: bytes, name: str) -> str:
        sent = self.http("POST", "/inherent/attachments", files={"file": (name, content)})
        assert sent.status_code == 200, sent.text
        return str(sent.json()["id"])

    def intents(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.log) as raw:
            return [
                {**json.loads(row[0]), "_node": row[1]}
                for row in raw.execute(
                    "SELECT payload_json, ingestion_node FROM events "
                    "WHERE type = 'surface.user_intent' ORDER BY id",
                )
            ]


def _answer(host: _Host, turn_id: str, text: str) -> str:
    """Her answer's rows, and its memory record under the emitted row's uid; that uid."""
    conn = open_event_log(host.log)
    try:
        base = {"turn_id": turn_id, "response_id": f"R-{turn_id}", "phase": "final"}
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                **base,
                "query": "q",
                "kind": "text",
                "response_group_id": "G",
                "channel": "speech",
            },
        )
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={
                **base,
                "text": text,
                "response_group_id": "G",
                "sequence": 0,
                "channel": "speech",
                "segment_hash": "h",
            },
        )
        emitted = emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                **base,
                "text": text,
                "response_group_id": "G",
                "channel": "speech",
            },
        )
    finally:
        conn.close()
    append_record(host.memory, record_id=emitted.event_uid, source="jarvis", text=text)
    return emitted.event_uid


def _tool(  # noqa: PLR0913 - one keyword per thing a logged call can vary in
    host: _Host,
    turn_id: str,
    tool: str,
    arguments: object,
    *,
    take: float = 0.15,
    ending: str = "action.result_observed",
    error: str | None = None,
) -> None:
    """One tool call as the dispatcher logs it: proposed, running, then its terminal row."""
    conn = open_event_log(host.log)
    action = f"A-{tool}-{time.monotonic_ns()}"
    try:
        emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": action,
                "tool_name": tool,
                "caller_principal": "jarvis_llm",
                "risk_level": "L1",
                "arguments": arguments,
                "turn_id": turn_id,
            },
            correlation={"action_id": action, "turn_id": turn_id},
        )
        running = emit_event(
            conn,
            type="action.running",
            payload={"action_id": action},
            correlation={"action_id": action, "turn_id": turn_id},
        )
        time.sleep(take)
        ended = {"action_id": action}
        if ending == "action.result_observed":
            ended |= {"semantics": "error" if error else "observation", "tool_output": "{}"}
        if error:
            ended["error"] = error
        emit_event(
            conn,
            type=ending,
            payload=ended,
            source_event_id=running.event_uid,
            correlation={"action_id": action},
        )
    finally:
        conn.close()


def _frames(phone: FakePhone, kind: str) -> list[dict[str, Any]]:
    with phone.lock:
        return [f for f in phone.texts if f.get("type") == kind]


def _wait_row(phone: FakePhone, event_type: str) -> None:
    phone.wait_until(
        lambda: event_type in {r["event_type"] for r in phone.rows()},
        f"no {event_type} row",
    )


# --- (a) a typed say with files ----------------------------------------------------------------


def test_a_typed_say_with_a_stored_file_is_the_turn_submit_makes_and_is_answered_in_rows(
    tmp_path: Path,
) -> None:
    """(a) The same turn as /inherent/submit, acknowledged at once, answered in rows."""
    with _Host(tmp_path) as host:
        file_id = host.upload(b"line one\nline two\n", "notes.txt")
        phone = host.phone()
        try:
            phone.say("u-files", "what is in this", spoken=False, attachments=[file_id, file_id])
            said = phone.wait_text("said")  # at once, before any row
            assert said["verdict"] == "turn"
            assert not phone.rows()
            submitted = host.http(
                "POST",
                "/inherent/submit",
                json={"text": "what is in this", "attachments": [file_id]},
            ).json()
            by_say, by_http = host.intents()
            assert by_say["turn_id"] == said["turn_id"]
            assert by_http["turn_id"] == submitted["turn_id"]
            assert by_say["attachments"] == [file_id]  # asked twice, kept once
            assert set(by_say) - {"turn_id"} == set(by_http) - {"turn_id"}
            assert (
                by_say["transcript"],
                by_say["channel"],
                by_say["language"],
                by_say["_node"],
            ) == (
                by_http["transcript"],
                by_http["channel"],
                "en",
                by_http["_node"],
            )
            assert by_say["_node"] == "iphone"
            _answer(host, said["turn_id"], "Two lines.")
            _wait_row(phone, "surface.response_emitted")
            assert {r["payload"]["turn_id"] for r in phone.rows()} == {said["turn_id"]}

            phone.say("u-files", "what is in this", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(_frames(phone, "said")) == 2, "no said for the resend")
            assert _frames(phone, "said")[1]["turn_id"] == said["turn_id"]  # the same turn
            assert len(host.intents()) == 2

            phone.say("u-blank", "  ", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(_frames(phone, "said")) == 3, "no said for blank words")
            assert host.intents()[-1]["transcript"] == lang.t("attach.no_words")
        finally:
            phone.close()


@pytest.mark.parametrize(
    ("spoken", "attachments", "code"),
    [
        (False, ["not an id!"], "bad_say"),
        (False, "x" * 32, "bad_say"),
        (True, ["a" * 32], "bad_say"),
        (False, ["0" * 32], "attachment_refused"),
        (False, [c * 32 for c in "bcdef"], "attachment_refused"),
    ],
)
def test_a_say_the_store_or_the_frame_check_refuses_opens_no_turn(
    tmp_path: Path,
    spoken: bool,  # noqa: FBT001 - a parameter of the table
    attachments: object,
    code: str,
) -> None:
    """(a) A bad id, files on spoken words or a refusing store: an error, no turn, no ack."""
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.send(
                {
                    "type": "say",
                    "utterance_id": "u-bad",
                    "text": "look",
                    "spoken": spoken,
                    "attachments": attachments,
                }
            )
            assert phone.wait_text("error")["code"] == code
            time.sleep(0.2)
            assert not host.intents()
            assert not _frames(phone, "said")
        finally:
            phone.close()


def test_a_resent_say_whose_files_expired_is_answered_with_its_turn(tmp_path: Path) -> None:
    """(a) The turn exists, so the resend gets its `said`; a new say naming the file is refused."""
    with _Host(tmp_path) as host:
        file_id = host.upload(b"notes\n", "notes.txt")
        phone = host.phone()
        try:
            phone.say("u-exp", "read this", spoken=False, attachments=[file_id])
            turn = phone.wait_text("said")["turn_id"]
            path = host.store.path(file_id)
            assert path is not None
            path.unlink()  # the retention sweep, as far as the store can tell
            phone.say("u-exp", "read this", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(_frames(phone, "said")) == 2, "the resend got no said")
            assert _frames(phone, "said")[1]["turn_id"] == turn
            assert not _frames(phone, "error")
            assert len(host.intents()) == 1
            phone.say("u-new", "read this", spoken=False, attachments=[file_id])
            assert phone.wait_text("error")["code"] == "attachment_refused"
            assert len(host.intents()) == 1
        finally:
            phone.close()


def test_a_host_without_a_store_refuses_files_and_still_takes_words(tmp_path: Path) -> None:
    """(a) No store: files are refused with their own code, plain words still pass."""
    with _Host(tmp_path, files=False) as host:
        phone = host.phone()
        try:
            phone.say("u-1", "look", spoken=False, attachments=["a" * 32])
            assert phone.wait_text("error")["code"] == "no_attachments"
            phone.say("u-2", "just words", spoken=False)
            assert phone.wait_text("said")["verdict"] == "turn"
        finally:
            phone.close()


# --- (b) live steps ----------------------------------------------------------------------------


def test_a_turns_tool_calls_reach_its_device_as_steps_in_log_order_with_the_rows(
    tmp_path: Path,
) -> None:
    """(b) Running then done or failed, labelled from the table, only for the device's own turns."""
    with _Host(tmp_path) as host:
        phone, other = host.phone(), host.phone(host.other_token)
        try:
            phone.say("u-typed", "bus to uvic and cakes", spoken=False)
            turn = phone.wait_text("said")["turn_id"]
            other.say("u-other", "someone else", spoken=False)
            other_turn = other.wait_text("said")["turn_id"]
            _tool(host, other_turn, "web_search", {"query": "not for the iphone"})
            _tool(host, turn, "tool_search", {"query": "plumbing"})  # not a step
            _tool(host, turn, "transit", {"origin": "here", "destination": "Library"})
            _tool(host, turn, "web_search", {"query": "small  cakes\nin town"}, take=0.3)
            _tool(host, turn, "read_file", {"path": "/Users/someone/secret.txt"})
            _tool(
                host,
                turn,
                "mcp__gmail__gmail_send",
                {"to": "x@example.com", "body": "hi"},
                error="send_failed",
            )
            _tool(host, turn, "frobnicate", "{}", ending="action.timeout_assumed")
            _answer(host, turn, "Take the 28; cakes are downtown.")
            _wait_row(phone, "surface.response_emitted")
            phone.wait_until(lambda: len(_frames(phone, "step")) >= 10, "steps never arrived")
            frames = _frames(phone, "step")

            shown = [(f["step"], f["tool"], f["label"], f["state"]) for f in frames]
            assert shown == [
                (1, "transit", "Checking transit \u00b7 to Library", "running"),
                (1, "transit", "Checking transit \u00b7 to Library", "done"),
                (2, "web_search", "Searching the web \u00b7 small cakes in town", "running"),
                (2, "web_search", "Searching the web \u00b7 small cakes in town", "done"),
                (3, "read_file", "Working with a file", "running"),
                (3, "read_file", "Working with a file", "done"),
                (4, "mcp__gmail__gmail_send", "Writing mail", "running"),
                (4, "mcp__gmail__gmail_send", "Writing mail", "failed"),
                (5, "frobnicate", "Working on it...", "running"),
                (5, "frobnicate", "Working on it...", "failed"),
            ]
            assert {f["turn_id"] for f in frames} == {turn}
            running, done = frames[2], frames[3]
            assert running["ended_ms"] is None
            assert done["started_ms"] == running["started_ms"]
            assert done["ended_ms"] - done["started_ms"] >= 250
            # log order: every step came before the answer's first row
            kinds = [
                f["type"] if f["type"] == "step" else f["event_type"]
                for f in list(phone.texts)
                if f["type"] in {"step", "row"}
            ]
            assert kinds.index("surface.response_open") == 10
            # the other device got its own turn's steps, and only those
            other.wait_until(lambda: len(_frames(other, "step")) == 2, "ipad got no steps")
            assert [(f["turn_id"], f["label"], f["state"]) for f in _frames(other, "step")] == [
                (other_turn, "Searching the web \u00b7 not for the iphone", "running"),
                (other_turn, "Searching the web \u00b7 not for the iphone", "done"),
            ]
        finally:
            phone.close()
            other.close()


def test_a_spoken_turn_gets_steps_like_a_typed_one(tmp_path: Path) -> None:
    """(b) A turn the phone spoke is its own, so its tools show the same way."""
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.say("u-voice", "is it raining", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            with sqlite3.connect(host.log) as raw:
                (channel,) = raw.execute(
                    "SELECT json_extract(payload_json, '$.channel') FROM events "
                    "WHERE type = 'utterance.received'",
                ).fetchone()
            assert channel == "phone_voice"
            _tool(host, turn, "weather", {})
            phone.wait_until(lambda: len(_frames(phone, "step")) == 2, "no steps for a spoken turn")
            assert [(f["label"], f["state"]) for f in _frames(phone, "step")] == [
                ("Checking the weather", "running"),
                ("Checking the weather", "done"),
            ]
        finally:
            phone.close()


# --- (c) the summary under her answer ---------------------------------------------------------


def test_the_conversation_row_of_her_answer_carries_the_steps_read_from_the_log(
    tmp_path: Path,
) -> None:
    """(c) The summary comes from the log: per step label and ms, a total and a phrase."""
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.say("u-1", "bus and cakes", spoken=False)
            busy = phone.wait_text("said")["turn_id"]
            phone.say("u-2", "thanks", spoken=False)
            phone.wait_until(lambda: len(_frames(phone, "said")) == 2, "no second said")
            quiet = _frames(phone, "said")[1]["turn_id"]
            _tool(host, busy, "transit", {"destination": "home"})
            _tool(host, busy, "web_search", {"query": "small cakes"}, ending="action.cancelled")
            busy_answer = _answer(host, busy, "Take the 28.")
            quiet_answer = _answer(host, quiet, "You are welcome.")
        finally:
            phone.close()
        # a new reader of the same files: nothing is held in memory between the turn and now
        page = host.http("GET", "/inherent/conversation").json()
        rows = {row["id"]: row for row in page["rows"]}
        busy_row, quiet_row = rows[busy_answer], rows[quiet_answer]
        assert [(s["label"], s.get("failed", False)) for s in busy_row["steps"]] == [
            ("Checking transit \u00b7 to home", False),
            ("Searching the web \u00b7 small cakes", True),
        ]
        assert all(s["ms"] >= 100 for s in busy_row["steps"])
        assert busy_row["steps_summary"] == "checked transit, searched the web"
        assert busy_row["steps_ms"] >= 300
        assert not {"steps", "steps_ms", "steps_summary"} & set(quiet_row)
        paged = host.http("GET", "/inherent/conversation", params={"limit": 1}).json()["rows"]
        assert [r["id"] for r in paged] == [quiet_answer]  # the newest page, one row
        older = host.http(
            "GET",
            "/inherent/conversation",
            params={"before": quiet_row["seq"], "limit": 1},
        ).json()["rows"]
        assert [r["id"] for r in older] == [busy_answer]
        assert older[0]["steps"] == busy_row["steps"]


# --- (d) the label table -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "en", "zh"),
    [
        (
            "web_search",
            {"query": "small cakes"},
            "Searching the web \u00b7 small cakes",
            "上网搜 \u00b7 small cakes",
        ),
        (
            "web_search",
            {"query": "x" * 60},
            "Searching the web \u00b7 " + "x" * 23 + "\u2026",
            "上网搜 \u00b7 " + "x" * 23 + "\u2026",
        ),
        (
            "web_fetch",
            {"url": "https://www.example.com/a?token=secret"},
            "Reading a page \u00b7 example.com",
            "读网页 \u00b7 example.com",
        ),
        (
            "transit",
            {"destination": "Library"},
            "Checking transit \u00b7 to Library",
            "查公交 \u00b7 去 Library",
        ),
        (
            "transit",
            {"destination": "Home"},
            "Checking transit \u00b7 to home",
            "查公交 \u00b7 去 家",
        ),
        ("transit", {}, "Checking transit", "查公交"),
        (
            "mcp__microsoft__get-calendar-view",
            {
                "startDateTime": "2026-10-11T00:00:00-07:00",
                "endDateTime": "2026-10-13T00:00:00-07:00",
            },
            "Checking your calendar \u00b7 10/11-10/13",
            "查日程 \u00b7 10/11-10/13",
        ),
        (
            "mcp__microsoft__get-calendar-view",
            {"startDateTime": "2026-10-11T00:00:00", "endDateTime": "2026-10-11T23:59:00"},
            "Checking your calendar \u00b7 10/11",
            "查日程 \u00b7 10/11",
        ),
        ("mcp__microsoft__list-todo-tasks", {}, "Checking your to-dos", "查待办"),
        (
            "mcp__microsoft__create-calendar-event",
            {"body": {"subject": "private"}},
            "Changing your calendar",
            "改日程",
        ),
        ("mcp__gmail__gmail_search", {"query": "from:boss"}, "Checking your mail", "查邮件"),
        ("mcp__gmail__gmail_send", {"to": "a@b.c"}, "Writing mail", "写邮件"),
        ("mcp__notion__notion-search", {"query": "plans"}, "Searching Notion", "搜 Notion"),
        ("mcp__hue__set_light", {"on": True}, "Controlling the lights", "控制灯"),
        ("remember", {"text": "a secret"}, "Saving a note", "记下来"),
        ("read_file", {"path": "/home/x/y"}, "Working with a file", "处理文件"),
        ("set_reminder", {"text": "call mum"}, "Working on reminders", "处理提醒"),
        ("where_am_i", {}, "Checking where you are", "看位置"),
        ("mcp__other__thing", {}, "Working on it...", "正在处理…"),
        ("never_heard_of_it", None, "Working on it...", "正在处理…"),
        (
            "web_search",
            '{"query": "from json text"}',
            "Searching the web \u00b7 from json text",
            "上网搜 \u00b7 from json text",
        ),
    ],
)
def test_the_label_table(tool: str, arguments: object, en: str, zh: str) -> None:
    """(d) A tool and its arguments to a label, in both languages; details are cut and safe."""
    assert steps.label(tool, arguments) == en
    lang.set_language("zh")
    assert steps.label(tool, arguments) == zh


def test_every_kind_has_both_wordings() -> None:
    """(d) Every kind the table can name has a label and a past-tense phrase in zh and en."""
    kinds = {steps.kind(tool) for tool in steps._KIND} | {  # noqa: SLF001
        steps.kind(f"mcp__{server}__{name}")
        for server in ("microsoft", "gmail", "notion", "hue")
        for name in ("get-todo", "get_x", "make_x")
    }
    for kind in kinds - {"generic"}:
        for language in ("en", "zh"):
            assert lang.t(f"step.{kind}", lang=language)
            assert lang.t(f"step.{kind}.done", lang=language)
