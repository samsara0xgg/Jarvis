"""ADR 0231: typed turns with files over the phone socket, live steps, and the steps summary.

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
in both languages; (e) ADR 0267: the history rendered for the model puts a ``[You did: ...]`` line
after an answer whose turn ran tools that worked, in English, and none after one that ran none or
whose tools failed; an answer that copies the line has it cut from the history and the page.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from jarvis.shared import lang, steps
from jarvis.state.memory_db import append_record, conversation_rows
from tests.support.phone import (
    StepsHost,
    answer_with_record,
    frames_of,
    history_of,
    logged_tool_call,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tools.phone_fake_client import FakePhone


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


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
    with StepsHost(tmp_path) as host:
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
            answer_with_record(host, said["turn_id"], "Two lines.")
            _wait_row(phone, "surface.response_emitted")
            assert {r["payload"]["turn_id"] for r in phone.rows()} == {said["turn_id"]}

            phone.say("u-files", "what is in this", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 2, "no said for the resend")
            assert frames_of(phone, "said")[1]["turn_id"] == said["turn_id"]  # the same turn
            assert len(host.intents()) == 2

            phone.say("u-blank", "  ", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 3, "no said for blank words")
            assert host.intents()[-1]["transcript"] == lang.t("attach.no_words")
        finally:
            phone.close()


def test_one_message_is_one_turn_over_http_and_the_socket(tmp_path: Path) -> None:
    """ADR 0262: one `utterance_id` sent twice, or by say then submit, is one turn."""
    with StepsHost(tmp_path) as host:
        phone = host.phone()
        try:
            body = {"text": "the driver got off", "utterance_id": "m-bus"}
            first = host.http("POST", "/inherent/submit", json=body)
            again = host.http("POST", "/inherent/submit", json=body)
            assert (first.status_code, again.status_code) == (200, 200)
            assert first.json()["turn_id"] == again.json()["turn_id"]
            phone.say("m-bus", "the driver got off", spoken=False)  # the socket's resend
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 1, "no said for the resend")
            assert frames_of(phone, "said")[0]["turn_id"] == first.json()["turn_id"]
            assert len(host.intents()) == 1

            phone.say("m-late", "second", spoken=False)  # a say first, then its HTTP fallback
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 2, "no said for the say")
            late = frames_of(phone, "said")[1]
            sent = host.http(
                "POST", "/inherent/submit", json={"text": "second", "utterance_id": "m-late"},
            )
            assert sent.json()["turn_id"] == late["turn_id"]
            assert len(host.intents()) == 2

            plain = [host.http("POST", "/inherent/submit", json={"text": "same"}) for _ in "ab"]
            assert plain[0].json()["turn_id"] != plain[1].json()["turn_id"]  # no id: as before
            assert host.http(
                "POST", "/inherent/submit", json={"text": "x", "utterance_id": "no good!"},
            ).status_code == 422
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
    with StepsHost(tmp_path) as host:
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
            assert not frames_of(phone, "said")
        finally:
            phone.close()


def test_a_resent_say_whose_files_expired_is_answered_with_its_turn(tmp_path: Path) -> None:
    """(a) The turn exists, so the resend gets its `said`; a new say naming the file is refused."""
    with StepsHost(tmp_path) as host:
        file_id = host.upload(b"notes\n", "notes.txt")
        phone = host.phone()
        try:
            phone.say("u-exp", "read this", spoken=False, attachments=[file_id])
            turn = phone.wait_text("said")["turn_id"]
            path = host.store.path(file_id)
            assert path is not None
            path.unlink()  # the retention sweep, as far as the store can tell
            phone.say("u-exp", "read this", spoken=False, attachments=[file_id])
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 2, "the resend got no said")
            assert frames_of(phone, "said")[1]["turn_id"] == turn
            assert not frames_of(phone, "error")
            assert len(host.intents()) == 1
            phone.say("u-new", "read this", spoken=False, attachments=[file_id])
            assert phone.wait_text("error")["code"] == "attachment_refused"
            assert len(host.intents()) == 1
        finally:
            phone.close()


def test_a_host_without_a_store_refuses_files_and_still_takes_words(tmp_path: Path) -> None:
    """(a) No store: files are refused with their own code, plain words still pass."""
    with StepsHost(tmp_path, files=False) as host:
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
    with StepsHost(tmp_path) as host:
        phone, other = host.phone(), host.phone(host.other_token)
        try:
            phone.say("u-typed", "bus to uvic and cakes", spoken=False)
            turn = phone.wait_text("said")["turn_id"]
            other.say("u-other", "someone else", spoken=False)
            other_turn = other.wait_text("said")["turn_id"]
            logged_tool_call(host, other_turn, "web_search", {"query": "not for the iphone"})
            logged_tool_call(host, turn, "tool_search", {"query": "plumbing"})  # not a step
            logged_tool_call(host, turn, "transit", {"origin": "here", "destination": "Library"})
            logged_tool_call(host, turn, "web_search", {"query": "small  cakes\nin town"}, take=0.3)
            logged_tool_call(host, turn, "read_file", {"path": "/Users/someone/secret.txt"})
            logged_tool_call(
                host,
                turn,
                "mcp__gmail__gmail_send",
                {"to": "x@example.com", "body": "hi"},
                error="send_failed",
            )
            logged_tool_call(host, turn, "frobnicate", "{}", ending="action.timeout_assumed")
            answer_with_record(host, turn, "Take the 28; cakes are downtown.")
            _wait_row(phone, "surface.response_emitted")
            phone.wait_until(lambda: len(frames_of(phone, "step")) >= 10, "steps never arrived")
            frames = frames_of(phone, "step")

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
            other.wait_until(lambda: len(frames_of(other, "step")) == 2, "ipad got no steps")
            assert [(f["turn_id"], f["label"], f["state"]) for f in frames_of(other, "step")] == [
                (other_turn, "Searching the web \u00b7 not for the iphone", "running"),
                (other_turn, "Searching the web \u00b7 not for the iphone", "done"),
            ]
        finally:
            phone.close()
            other.close()


def test_a_spoken_turn_gets_steps_like_a_typed_one(tmp_path: Path) -> None:
    """(b) A turn the phone spoke is its own, so its tools show the same way."""
    with StepsHost(tmp_path) as host:
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
            logged_tool_call(host, turn, "weather", {})
            phone.wait_until(
                lambda: len(frames_of(phone, "step")) == 2, "no steps for a spoken turn"
            )
            assert [(f["label"], f["state"]) for f in frames_of(phone, "step")] == [
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
    with StepsHost(tmp_path) as host:
        phone = host.phone()
        try:
            phone.say("u-1", "bus and cakes", spoken=False)
            busy = phone.wait_text("said")["turn_id"]
            phone.say("u-2", "thanks", spoken=False)
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 2, "no second said")
            quiet = frames_of(phone, "said")[1]["turn_id"]
            logged_tool_call(host, busy, "transit", {"destination": "home"})
            logged_tool_call(
                host, busy, "web_search", {"query": "small cakes"}, ending="action.cancelled"
            )
            busy_answer = answer_with_record(host, busy, "Take the 28.")
            quiet_answer = answer_with_record(host, quiet, "You are welcome.")
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


def test_the_conversation_row_of_her_answer_carries_the_places_it_showed(tmp_path: Path) -> None:
    """ADR 0279: history redraws the map card from the row's ``places``; none, no key."""
    places = [
        {
            "name": "Ramen Shop",
            "lat": 48.43,
            "lng": -123.37,
            "address": "1 Fort St",
            "place_id": "ChIJone",
            "kind": "place",
        },
    ]
    with StepsHost(tmp_path) as host:
        phone = host.phone()
        try:
            phone.say("u-1", "ramen near me", spoken=False)
            shown = phone.wait_text("said")["turn_id"]
            phone.say("u-2", "thanks", spoken=False)
            phone.wait_until(lambda: len(frames_of(phone, "said")) == 2, "no second said")
            plain = frames_of(phone, "said")[1]["turn_id"]
            with_places = answer_with_record(host, shown, "There is one.", places)
            without = answer_with_record(host, plain, "You are welcome.")
        finally:
            phone.close()
        rows = {r["id"]: r for r in host.http("GET", "/inherent/conversation").json()["rows"]}
        assert rows[with_places]["places"] == places
        assert "places" not in rows[without]
        assert "places" not in {k for r in rows.values() if r["source"] == "allen" for k in r}


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
            "bus",
            {"to": "Library"},
            "Checking transit \u00b7 to Library",
            "查公交 \u00b7 去 Library",
        ),
        ("bus", {}, "Checking transit", "查公交"),
        ("bus", {"pin": [1]}, "Following the bus", "跟进公交"),
        ("bus", {"ride": "start", "route": "28"}, "Following the bus", "跟进公交"),
        ("bus", {"get_off": "Fir Lane"}, "Following the bus", "跟进公交"),
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
        ("keep", {"text": "a secret"}, "Saving a note", "记下来"),
        ("kept", {}, "Searching your records", "翻记录"),
        ("schedule", {}, "Checking your calendar", "查日程"),
        ("schedule", {"action": "add", "what": "buy milk"}, "Changing your calendar", "改日程"),
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


def test_the_history_follows_an_answer_whose_turn_ran_tools_with_what_it_did(
    tmp_path: Path,
) -> None:
    """(e) ADR 0267: the line is English, on its own, only after a turn with a tool that worked."""
    lang.set_language("zh")  # the reader is the model: the line does not follow the setting
    host = StepsHost(tmp_path)
    logged_tool_call(host, "T1", "keep", {"text": "buy milk"}, take=0)
    logged_tool_call(host, "T1", "where_am_i", {}, take=0)
    logged_tool_call(
        host, "T1", "keep", {"text": "again"}, take=0
    )  # a repeat of a kind is named once
    logged_tool_call(
        host, "T3", "web_search", {"query": "x"}, take=0, error="boom"
    )  # failed: not listed
    append_record(host.memory, record_id="u1", source="allen", text="save a note")
    answer_with_record(host, "T1", "Noted.")
    append_record(host.memory, record_id="u2", source="allen", text="thanks")
    answer_with_record(host, "T2", "You are welcome.")
    append_record(host.memory, record_id="u3", source="allen", text="search")
    answer_with_record(host, "T3", "The search failed.")
    contents = [turn["content"] for turn in history_of(host)]
    assert contents[1].endswith("Noted.\n[You did: saved a note, checked where you are]")
    assert contents[3].endswith("You are welcome.")
    assert contents[5].endswith("The search failed.")
    assert sum("[You did:" in c for c in contents) == 1


def test_a_line_she_copies_into_her_answer_is_cut_from_the_history_and_the_page(
    tmp_path: Path,
) -> None:
    """(e) The line is the program's; her copy of it is not words of hers, anywhere it is read."""
    host = StepsHost(tmp_path)
    append_record(host.memory, record_id="u1", source="allen", text="save a note")
    append_record(
        host.memory,
        record_id="a1",
        source="jarvis",
        text="Saved.\n[You did: saved a note]",
    )
    append_record(
        host.memory,
        record_id="a2",
        source="jarvis",
        text="Done [You did: set a reminder] ok",
    )
    assert [t["content"] for t in history_of(host)][1] == "Saved.\nDone ok"
    rows, _ = conversation_rows(host.memory, since="", after=0, before=0, limit=10)
    assert [r["text"] for r in rows if r["source"] == "jarvis"] == ["Saved.", "Done ok"]
