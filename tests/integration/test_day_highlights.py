"""ADR 0235 — the day's line and highlights, written with the day's prose, drafted through the day.

Real memory.db, Event Log, TimeSink-shaped sqlite and tool registry built in ``tmp_path`` (the
ledger tests' world); the prose preset is the only fake, a client that answers from a script and
records what it was asked. The checks:

- the night's answer (prose, line, highlights) stored and served by ``GET /inherent/day``, and what
  the writer is given: the day's items, the language of the line;
- the gates, as a table of answers to what is stored: an unknown id, an over-long label, two
  sentences, six highlights... drop the line and highlights whole and leave the prose standing;
- a draft is not prose: the ledger, the nights to write and the standing text's cache key ignore
  it, and the night's row replaces it;
- the draft cadence on a fake clock: changed and 30 minutes, a failure leaving the previous draft;
- ``mark_highlight`` through the real registry: the shortest covering item, unmark, his marks
  surviving the cap of five, the latest mark winning, and recall returning the merged result;
- a ``day_prose`` made before the columns of this ADR is migrated.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, time
from pathlib import Path
from time import localtime, strftime
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import yaml

from jarvis.decision.day_prose import DayAnswer, parse_day_answer
from jarvis.execution.highlight_tool import DESCRIPTION, build_highlight_tool
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.runtime.ledger import (
    DayDrafts,
    LedgerContext,
    LedgerSettings,
    ProseSettings,
    run_day_prose,
)
from jarvis.shared import CallerPrincipal
from jarvis.state import daily_records
from jarvis.state.day_highlights import MAX_HIGHLIGHTS, merge_highlights
from jarvis.state.ledger import LedgerSources, notes_stamp, standing_text
from jarvis.state.memory_db import (
    HighlightMark,
    MemorySettings,
    append_day_draft,
    append_day_prose,
    latest_day_lines,
    open_memory_db,
    prose_days,
)
from tests.support.day_highlights import (
    DAY_KEY,
    TODAY,
    TODAY_KEY,
    Dispatcher,
    ids_of,
    moment_ms,
    picture_note,
    served_day,
    visit_row,
    work_id,
)
from tests.support.ledger import (
    MIDNIGHT,
    ZONE,
    ledger_at,
    ledger_rows,
    ledger_world,
    prompts,
    prose_steps,
    prose_writer,
    run_ledger,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tests.support.model import ScriptedModel

WRITTEN = datetime(2100, 1, 1, tzinfo=ZONE)  # a notes cut-off after the real clock of any write
MARK_TOOL_TEXT = (
    "Mark or unmark something on a day's timeline as one of that day's highlights, when he says "
    "it mattered or that it didn't. Name it by the day and a time it covers. His marks override "
    "your own picks."
)


def _night(prose: str, line: str | None, picks: Sequence[tuple[str, str]] = ()) -> str:
    """What the prose preset answers at night (ADR 0235)."""
    answer: dict[str, Any] = {"prose": prose}
    if line is not None:
        answer["line"] = line
        answer["highlights"] = [{"id": i, "label": label} for i, label in picks]
    return json.dumps(answer)


def _draft(line: str | None, picks: Sequence[tuple[str, str]] = ()) -> str:
    """What the prose preset answers for a draft."""
    return json.dumps({
        "line": line, "highlights": [{"id": i, "label": label} for i, label in picks],
    })


# --- the night -----------------------------------------------------------------------------------


def test_the_night_stores_prose_line_and_highlights_and_the_day_serves_them(
    tmp_path: Path,
) -> None:
    """One answer, one row; the writer is given the day's items and the language of the line."""
    src = ledger_world(tmp_path)
    visit_row(src, "v-cafe", ledger_at(7, 12), ledger_at(7, 12, 40), "Corner Cafe")
    work = work_id(7, 13)
    writer = prose_writer([_night(
        "He had coffee and worked on the ledger.",
        "Coffee first, then a good stretch on the ledger.",
        [("v-cafe", "Coffee at the cafe"), (work, "Ledger work")],
    )])

    outcomes = run_ledger(src, writer, 2, [])

    assert outcomes == {"2026-10-07": "landed (39 chars)"}
    (asked,) = prompts(writer)
    assert "v-cafe | stay | 12:00-12:40 | place=Corner Cafe" in asked
    assert f"{work} | work | 13:00-13:45 | active_s=2700; apps=Chrome" in asked
    assert asked.endswith("LINE LANGUAGE: English")  # his last words, 'hi'
    day = served_day(src)
    assert day["line"]["text"] == "Coffee first, then a good stretch on the ledger."
    assert day["line"]["draft"] is False
    assert datetime.fromisoformat(day["line"]["written_at"]).tzinfo is not None
    assert day["highlights"] == [
        {"id": "v-cafe", "label": "Coffee at the cafe", "by": "her"},
        {"id": work, "label": "Ledger work", "by": "her"},
    ]
    assert "The day: He had coffee and worked on the ledger." in standing_text(
        src, MIDNIGHT, notes_as_of=WRITTEN,
    )
    assert served_day(src, TODAY)["line"] is None  # another day has none


@pytest.mark.parametrize(("pinned", "named"), [("zh", "Simplified Chinese"), ("en", "English")])
def test_a_pinned_reply_language_is_the_language_of_the_line(
    tmp_path: Path, pinned: str, named: str,
) -> None:
    """The ``reply_language`` setting wins over his last words, as it does for her replies."""
    src = ledger_world(tmp_path)
    writer = prose_writer([_night("A quiet day.", "Quiet.")])
    settings = ProseSettings("p", "write", 2, 200, None, pinned)

    run_day_prose(
        src, settings, writer.as_llm_client(),
        event_log_path=src.event_log, pricing_table={}, today=TODAY, sleep=lambda _s: None,
    )
    assert prompts(writer)[0].endswith(f"LINE LANGUAGE: {named}")


class Gate(NamedTuple):
    """An answer of the writer and what the gates make of it."""

    name: str
    answer: dict[str, Any]
    dropped: str | None  # why the line and highlights are dropped, None when they stand


def _pick(item: str, label: str) -> dict[str, str]:
    return {"id": item, "label": label}


GATES: list[Gate] = [
    Gate("a line and two highlights", {
        "line": "Out for coffee.", "highlights": [_pick("a", "Coffee"), _pick("b", "Work")],
    }, None),
    Gate("no highlights is fine", {"line": "A quiet day."}, None),
    Gate("an empty list is fine", {"line": "A quiet day.", "highlights": []}, None),
    Gate("a label of 24 characters", {"line": "x.", "highlights": [_pick("a", "l" * 24)]}, None),
    Gate("a line of 140 characters", {"line": "l" * 140}, None),
    Gate("an id that is not an item of that day", {
        "line": "Out.", "highlights": [_pick("a", "Coffee"), _pick("zzz", "Gone")],
    }, "highlight 'zzz' is not an item of that day"),
    Gate("a label of 25 characters", {
        "line": "Out.", "highlights": [_pick("a", "l" * 25)],
    }, "a label is empty or over 24"),
    Gate("an empty label", {"line": "Out.", "highlights": [_pick("a", "  ")]},
         "a label is empty or over 24"),
    Gate("six highlights", {
        "line": "Out.", "highlights": [_pick("a", "1")] * 6,
    }, "more than 5 highlights"),
    Gate("the same item twice", {
        "line": "Out.", "highlights": [_pick("a", "One"), _pick("a", "Two")],
    }, "highlight 'a' is named twice"),
    Gate("a highlight that is not an object", {
        "line": "Out.", "highlights": ["a"],
    }, "a highlight lacks an id or a label"),
    Gate("highlights that are not a list", {"line": "Out.", "highlights": "a"},
         "highlights are not a list"),
    Gate("a line of 141 characters", {"line": "l" * 141}, "the line is over the 140 cap"),
    Gate("two sentences", {"line": "He went out. He came back."}, "the line is not one sentence"),
    Gate("two sentences in Chinese", {"line": "他出门了。他回家了"},
         "the line is not one sentence"),
    Gate("one sentence with a number", {"line": "He ran 3.5 km before lunch."}, None),
    Gate("a line over two lines", {"line": "Out\nand back."}, "the line is not one sentence"),
    Gate("a bullet", {"line": "- out"}, "the line holds markdown"),
    Gate("no line", {"highlights": [_pick("a", "Coffee")]}, "no line"),
    Gate("an empty line", {"line": " ", "highlights": []}, "no line"),
]


@pytest.mark.parametrize("gate", GATES, ids=lambda gate: gate.name)
def test_the_line_and_highlights_stand_or_fall_together_and_the_prose_stands_alone(
    gate: Gate,
) -> None:
    """A failing line or highlight drops both; the night's prose is kept, a draft is rejected."""
    starts = {"a": 100, "b": 200}
    night = parse_day_answer(
        json.dumps({"prose": "He went out.", **gate.answer}), "stop", 200, starts, with_prose=True,
    )
    assert isinstance(night, DayAnswer)
    assert night.prose == "He went out."
    draft = parse_day_answer(json.dumps(gate.answer), "stop", 200, starts, with_prose=False)
    if gate.dropped is None:
        assert night.dropped is None
        assert night.line == str(gate.answer["line"]).strip()
        assert draft == DayAnswer(None, night.line, night.highlights, None)
        assert [pick["id"] for pick in night.highlights] == [
            one["id"] for one in gate.answer.get("highlights", [])
        ]
        assert all(pick["start_ms"] == starts[pick["id"]] for pick in night.highlights)
    else:
        assert (night.line, night.highlights, night.dropped) == (None, (), gate.dropped)
        assert draft == gate.dropped


@pytest.mark.parametrize(
    ("text", "finish", "why"),
    [
        ("He went out.", "stop", "not valid JSON"),
        ('["a"]', "stop", "not a JSON object"),
        ('{"line": "Out."}', "stop", 'no "prose" text'),
        ('{"prose": "", "line": "Out."}', "stop", "empty"),
        ('{"prose": "## Out", "line": "Out."}', "stop", "contains markdown headings or bullets"),
        ('{"prose": "' + "p" * 201 + '", "line": "Out."}', "stop", "201 chars over the 200 cap"),
        ('{"prose": "Out.", "line": "Out."}', "length", "cut off by the output limit"),
        (None, "stop", "not valid JSON"),
    ],
)
def test_an_answer_whose_prose_fails_or_that_is_not_one_json_object_is_rejected_whole(
    text: str | None, finish: str, why: str,
) -> None:
    """A good line does not rescue bad prose; the prose gates are the ones the ledger had."""
    assert parse_day_answer(text, finish, 200, {"a": 1}, with_prose=True) == why


def test_a_fenced_answer_is_still_the_one_json_object() -> None:
    """Models wrap JSON in a code fence; the answer is read as the object inside."""
    fenced = "```json\n" + _night("He went out.", "Out.", [("a", "Coffee")]) + "\n```"
    answer = parse_day_answer(fenced, "stop", 200, {"a": 5}, with_prose=True)
    assert isinstance(answer, DayAnswer)
    assert (answer.prose, answer.line, answer.highlights) == (
        "He went out.", "Out.", ({"id": "a", "label": "Coffee", "start_ms": 5},),
    )


@pytest.mark.parametrize(
    ("picks", "why"),
    [
        ([("nope", "Lunch")], "line dropped: highlight 'nope' is not an item of that day"),
        ([("v-cafe", "A label that is far too long to show")], "line dropped: a label is empty"),
    ],
)
def test_a_bad_highlight_drops_the_line_whole_and_the_prose_is_stored_without_it(
    tmp_path: Path, picks: list[tuple[str, str]], why: str,
) -> None:
    """The row lands with its prose; the day serves no line and no highlights."""
    src = ledger_world(tmp_path)
    visit_row(src, "v-cafe", ledger_at(7, 12), ledger_at(7, 12, 40), "Corner Cafe")
    writer = prose_writer([_night("He had coffee.", "Coffee, then work.", picks)])

    outcomes = run_ledger(src, writer, 2, [])

    assert outcomes["2026-10-07"].startswith("landed (14 chars; ")
    assert why in outcomes["2026-10-07"]
    assert ledger_rows(src)["2026-10-07"] == ["He had coffee."]
    with closing(sqlite3.connect(src.memory_db)) as conn:
        assert conn.execute(
            "SELECT line, highlights FROM day_prose WHERE day = '2026-10-07'",
        ).fetchall() == [(None, None)]
    day = served_day(src)
    assert (day["line"], day["highlights"]) == (None, [])


def test_a_rejected_night_answer_is_asked_once_more_then_stores_nothing(tmp_path: Path) -> None:
    """Bad prose is today's failure: one more ask, then no row, and the next run tries again."""
    src = ledger_world(tmp_path)
    writer = prose_writer(["not json at all", _night("## heading", "Out.")])

    outcomes = run_ledger(src, writer, 2, [])

    assert outcomes == {"2026-10-07": "rejected (contains markdown headings or bullets)"}
    assert len(prompts(writer)) == 2
    assert "2026-10-07" not in prose_days(src.memory_db)


# --- a draft is not prose -------------------------------------------------------------------------


def test_a_draft_is_not_prose_for_the_ledger_or_for_the_nights_to_write(tmp_path: Path) -> None:
    """Drafts do not show in "The day", do not move its cache key and do not stop the night."""
    src = ledger_world(tmp_path)
    before_stamp = notes_stamp(src)
    before_text = standing_text(src, MIDNIGHT, notes_as_of=WRITTEN)
    for day in ("2026-10-05", DAY_KEY):
        append_day_draft(
            src.memory_db, day=day, line="A draft line.", highlights=[],
            model="m", input_chars=1, output_chars=1,
        )

    assert prose_days(src.memory_db) == {"2026-10-06"}
    assert notes_stamp(src) == before_stamp
    assert standing_text(src, MIDNIGHT, notes_as_of=WRITTEN) == before_text
    assert "A draft line." not in before_text

    writer = prose_writer([
        _night("Gamma wrote back.", "Mail day."), _night("A short Wednesday.", "Wed."),
    ])
    outcomes = run_ledger(src, writer, 3, [])

    assert [outcomes[day].split(" ")[0] for day in sorted(outcomes)] == ["landed", "landed"]
    assert prose_days(src.memory_db) == {"2026-10-05", "2026-10-06", "2026-10-07"}
    assert "The day: A short Wednesday." in standing_text(src, MIDNIGHT, notes_as_of=WRITTEN)


def test_the_nights_row_replaces_the_draft_and_a_night_with_no_line_leaves_the_draft(
    tmp_path: Path,
) -> None:
    """The current line is the newest row that has one, whoever wrote it."""
    src = ledger_world(tmp_path)
    visit_row(src, "v-cafe", ledger_at(7, 12), ledger_at(7, 12, 40), "Corner Cafe")
    drafted = [{"id": "v-cafe", "label": "Draft coffee", "start_ms": moment_ms(ledger_at(7, 12))}]
    append_day_draft(
        src.memory_db, day=DAY_KEY, line="Draft line.", highlights=drafted,
        model="m", input_chars=1, output_chars=1,
    )
    day = served_day(src)
    assert day["line"]["draft"] is True
    assert day["line"]["text"] == "Draft line."
    assert [h["label"] for h in day["highlights"]] == ["Draft coffee"]

    unusable = _night("A short Wednesday.", "Out.", [("nope", "Gone")])
    assert "line dropped" in run_ledger(src, prose_writer([unusable]), 2, [])[DAY_KEY]
    assert served_day(src)["line"]["text"] == "Draft line."  # the draft stands

    append_day_prose(
        src.memory_db,
        day=DAY_KEY,
        text="Night prose.",
        model="m",
        input_chars=1,
        output_chars=1,
        line="Night line.",
        highlights=[
            {"id": "v-cafe", "label": "Night coffee", "start_ms": moment_ms(ledger_at(7, 12))}
        ],
    )
    day = served_day(src)
    assert (day["line"]["text"], day["line"]["draft"]) == ("Night line.", False)
    assert [h["label"] for h in day["highlights"]] == ["Night coffee"]
    with closing(open_memory_db(src.memory_db)) as conn:
        assert latest_day_lines(conn, DAY_KEY, DAY_KEY)[DAY_KEY].line == "Night line."


def test_a_day_prose_table_from_before_this_adr_is_migrated_and_its_rows_stay_prose(
    tmp_path: Path,
) -> None:
    """``open_memory_db`` adds the columns, once, to a table without them."""
    path = tmp_path / "memory.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE day_prose (id TEXT PRIMARY KEY, day TEXT NOT NULL, ts TEXT NOT NULL, "
            "text TEXT NOT NULL, model TEXT, input_chars INTEGER NOT NULL, "
            "output_chars INTEGER NOT NULL)",
        )
        conn.execute(
            "INSERT INTO day_prose VALUES ('p1', '2026-10-06', '2026-10-07T05:00:00-07:00', "
            "'Before.', 'm', 1, 1)",
        )
    for _ in range(2):  # the second open finds the columns and adds nothing
        open_memory_db(path).close()
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(day_prose)")]
        assert columns[-3:] == ["line", "highlights", "draft"]
        assert conn.execute("SELECT text, line, draft FROM day_prose").fetchall() == [
            ("Before.", None, 0),
        ]
    assert prose_days(path) == {"2026-10-06"}


# --- the drafts ----------------------------------------------------------------------------------


def _schedule(
    tmp_path: Path,
    src: LedgerSources,
    writer: ScriptedModel,
    draft_prompt: str | None = "draft",
) -> DaySummarySchedule:
    """The nightly schedule over the ledger's world, its prose client the fake ``writer``."""
    prose = ProseSettings("p", "night", 7, 800, draft_prompt)
    schedule = DaySummarySchedule(
        memory=MemorySettings(db_path=src.memory_db, audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=src.event_log,
        pricing_table={},
        ledger=LedgerContext(src, LedgerSettings(prose=prose)),
    )
    schedule._prose_client = writer  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return schedule


def _line(src: LedgerSources, day: str = TODAY_KEY) -> tuple[str, bool] | None:
    with closing(open_memory_db(src.memory_db)) as conn:
        row = latest_day_lines(conn, day, day).get(day)
    return None if row is None else (row.line, row.draft)


def test_a_draft_runs_when_the_day_changed_and_half_an_hour_passed_and_a_failure_leaves_the_last(
    tmp_path: Path,
) -> None:
    """The cadence on a fake clock: the items (ids, states, ends) and 30 minutes, both."""
    src = ledger_world(tmp_path)  # today 10-08: work 10:00-11:00, one talk at 14:00
    work = work_id(8, 10)
    writer = prose_writer([])
    schedule = _schedule(tmp_path, src, writer)

    def tick(hour: int, minute: int, *script: object) -> str:
        writer.script(*prose_steps(script))
        return schedule.draft(ledger_at(8, hour, minute))

    # 10:05 - the first draft of the day, from two items
    first = tick(10, 5, _draft("Working since ten.", [(work, "Ledger work")]))
    assert first == "landed (1 highlights)"
    (asked,) = prompts(writer)
    assert asked.startswith("DAY: 2026-10-08 (still under way; it is 10:05 now)")
    assert f"{work} | work | 10:00-11:00 (now) | active_s=3600; projects=jarvis" in asked
    assert "(next)" in asked.split("DAY LINE ITEMS")[1]  # the talk at 14:00 has not happened
    assert _line(src) == ("Working since ten.", True)
    assert prose_days(src.memory_db) == {"2026-10-06"}  # a draft is not prose

    # 10:20 - half an hour has not passed; 10:36 - it has, but nothing changed
    assert tick(10, 20) == "waiting"
    visit_row(src, "v-cafe", ledger_at(8, 10, 10), None, "Corner Cafe")
    assert tick(10, 34) == "waiting"  # changed, but 29 minutes
    assert len(prompts(writer)) == 1
    assert tick(10, 36, _draft("Working, and at the cafe.", [("v-cafe", "Cafe")])) == (
        "landed (1 highlights)"
    )
    assert _line(src) == ("Working, and at the cafe.", True)

    # 11:30 - the block has ended (its state changed): a draft; 12:10 - nothing moved: none
    assert tick(11, 30, _draft("Work done, still at the cafe.")) == "landed (0 highlights)"
    assert tick(12, 10) == "unchanged"
    assert tick(13, 59) == "unchanged"
    assert len(prompts(writer)) == 3

    # 14:30 - the talk began (state next -> now -> done); the model fails: the last draft stands
    assert tick(14, 30, RuntimeError("boom")) == "failed"
    assert _line(src) == ("Work done, still at the cafe.", True)
    assert tick(14, 45) == "waiting"  # a failed try counts: not again until 15:00
    bad = _draft("Talked it over.", [("nope", "Gone")])  # asked once more, then given up on
    assert tick(15, 5, bad, _draft(None)) == "rejected (no line)"
    assert _line(src) == ("Work done, still at the cafe.", True)
    assert tick(15, 40, _draft("Then a talk with her.", [(work, "Ledger work")])) == (
        "landed (1 highlights)"
    )
    assert _line(src) == ("Then a talk with her.", True)

    # tomorrow starts clean: the reminder due at 09:00 on 10-09 is its first item
    writer.script(_draft("A reminder at nine."))
    assert schedule.draft(ledger_at(9, 9, 30)) == "landed (0 highlights)"
    assert _line(src, "2026-10-09") == ("A reminder at nine.", True)


def test_a_draft_needs_a_prompt_a_ledger_and_items(tmp_path: Path) -> None:
    """No ``draft_prompt`` or no ledger is off; a day with nothing on its line is not drafted."""
    src = ledger_world(tmp_path)
    writer = prose_writer([])
    assert _schedule(tmp_path, src, writer, draft_prompt=None).draft(ledger_at(8, 10, 5)) == "off"
    schedule = _schedule(tmp_path, src, writer)
    assert schedule.draft(ledger_at(4, 10, 5)) == "no items"  # nothing was recorded on 10-04
    bare = DaySummarySchedule(
        memory=MemorySettings(db_path=src.memory_db, audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="s"),
        llm_config={},
        event_log_path=src.event_log,
        pricing_table={},
    )
    assert bare.draft(ledger_at(8, 10, 5)) == "off"
    assert prompts(writer) == []


def test_a_restart_keeps_the_half_hour_since_the_last_draft(tmp_path: Path) -> None:
    """The last draft's time is read back from memory.db, so a restart does not draft at once."""
    src = ledger_world(tmp_path)
    with closing(open_memory_db(src.memory_db)) as conn, conn:
        conn.execute(
            "INSERT INTO day_prose (id, day, ts, text, model, input_chars, output_chars, line, "
            "highlights, draft) VALUES ('d1', ?, ?, '', 'm', 1, 1, 'Before the restart.', '[]', 1)",
            (TODAY_KEY, ledger_at(8, 10).isoformat()),
        )
    work = work_id(8, 10)
    writer = prose_writer([_draft("After the restart.", [(work, "Ledger work")])])
    drafts = DayDrafts(
        src,
        ProseSettings("p", "night", 7, 800, "draft"),
        writer.as_llm_client(),
        event_log_path=src.event_log,
        pricing_table={},
    )

    assert drafts.tick(ledger_at(8, 10, 10)) == "waiting"
    assert drafts.tick(ledger_at(8, 10, 31)) == "landed (1 highlights)"
    assert _line(src) == ("After the restart.", True)


# --- his marks ------------------------------------------------------------------------------------


@pytest.fixture
def marked_day(tmp_path: Path) -> tuple[LedgerSources, Dispatcher]:
    """10-07: a stay 12:00-13:30 with a picture inside it at 12:30, and the 13:00-13:45 work."""
    src = ledger_world(tmp_path)
    visit_row(src, "v-cafe", ledger_at(7, 12), ledger_at(7, 13, 30), "Corner Cafe")
    picture_note(src, "a" * 32, ledger_at(7, 12, 30), "A bowl of pho.")
    return src, Dispatcher(tmp_path, src)


def test_the_tool_has_the_approved_description_and_is_the_models_alone(
    marked_day: tuple[LedgerSources, Dispatcher],
) -> None:
    """The words the owner approved, exactly; a model-only write; absent without a runtime."""
    _src, call = marked_day
    assert DESCRIPTION == MARK_TOOL_TEXT
    (tool,) = (t for t in call.registry.get_definitions() if t.name == "mark_highlight")
    assert tool.description == MARK_TOOL_TEXT
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert tool.read_only is False
    assert tool.input_schema["required"] == ["day", "time"]
    assert set(tool.input_schema["properties"]) == {"day", "time", "on", "label"}
    assert build_highlight_tool(None) == ()
    assert "mark_highlight" not in {t.name for t in build_default_registry().get_definitions()}


def test_the_time_he_names_resolves_to_the_shortest_item_that_covers_it(
    marked_day: tuple[LedgerSources, Dispatcher],
) -> None:
    """A picture inside a stay is the picture; a minute inside only the stay is the stay."""
    src, call = marked_day
    first = call({"day": "2026-10-07", "time": "12:30"})
    assert first == {
        "status": "marked",
        "day": DAY_KEY,
        "kind": "picture",
        "when": "12:30",
        "label": "picture 12:30",
    }
    second = call({"day": "yesterday", "time": "12:45", "label": "Long lunch"})
    assert second == {
        "status": "marked",
        "day": DAY_KEY,
        "kind": "stay",
        "when": "12:00-13:30",
        "label": "Long lunch",
    }
    third = call({"day": "2026-10-07", "time": "13:10", "label": "Ledger"})  # the work block
    assert (third["kind"], third["when"]) == ("work", "13:00-13:45")
    day = served_day(src)
    assert [(h["id"], h["label"], h["by"]) for h in day["highlights"]] == [
        ("v-cafe", "Long lunch", "him"),
        (f"picture-{'a' * 32}", "picture 12:30", "him"),
        (work_id(7, 13), "Ledger", "him"),
    ]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"day": "2026-10-07", "time": "03:00"}, "no_item"),
        ({"day": "2026-10-07", "time": "13:46"}, "no_item"),
        ({"day": "today", "time": "12:00"}, "no_item"),
        ({"day": "tomorrow", "time": "12:00"}, "invalid_argument"),
        ({"day": "2026-10-07", "time": "25:00"}, "invalid_argument"),
        ({"day": "2026-10-07", "time": "noon"}, "invalid_argument"),
        ({"day": "2026-10-07", "time": "12:30", "label": "l" * 25}, "invalid_argument"),
        ({"day": "2026-10-07", "time": "12:30", "on": "yes"}, "invalid_argument"),
        ({"day": "2026-10-07"}, "invalid_argument"),
    ],
)
def test_a_time_nothing_covers_or_a_day_or_time_that_is_not_one_is_a_clear_error(
    marked_day: tuple[LedgerSources, Dispatcher],
    args: dict[str, Any],
    code: str,
) -> None:
    """No mark is kept, and the model is told why."""
    src, call = marked_day
    out = call(args)
    assert out.get("code") == code
    assert out["error"]
    with closing(open_memory_db(src.memory_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM day_highlight_marks").fetchone() == (0,)


def test_unmarking_removes_her_pick_and_the_latest_mark_of_an_item_wins(
    marked_day: tuple[LedgerSources, Dispatcher],
) -> None:
    """His no overrides her pick; his yes after his no brings it back, with his label."""
    src, call = marked_day
    work = work_id(7, 13)
    append_day_prose(
        src.memory_db,
        day=DAY_KEY,
        text="Night prose.",
        model="m",
        input_chars=1,
        output_chars=1,
        line="Night line.",
        highlights=[
            {"id": "v-cafe", "label": "Coffee", "start_ms": moment_ms(ledger_at(7, 12))},
            {"id": work, "label": "Ledger work", "start_ms": moment_ms(ledger_at(7, 13))},
        ],
    )
    assert ids_of(served_day(src)) == ["v-cafe", work]

    gone = call({"day": DAY_KEY, "time": "13:10", "on": False})
    assert gone == {"status": "unmarked", "day": DAY_KEY, "kind": "work", "when": "13:00-13:45"}
    assert ids_of(served_day(src)) == ["v-cafe"]
    assert served_day(src)["line"]["text"] == "Night line."  # the line is hers and stays

    back = call({"day": DAY_KEY, "time": "13:10", "label": "Ledger, again"})
    assert back["label"] == "Ledger, again"
    assert [(h["id"], h["label"], h["by"]) for h in served_day(src)["highlights"]] == [
        ("v-cafe", "Coffee", "her"),
        (work, "Ledger, again", "him"),
    ]
    kept = call({"day": DAY_KEY, "time": "13:10"})  # marked again with no label: hers is kept
    assert kept["label"] == "Ledger work"
    assert call({"day": DAY_KEY, "time": "12:45", "on": False})["status"] == "unmarked"
    assert ids_of(served_day(src)) == [work]


def test_his_marks_are_never_cut_by_the_cap_and_her_picks_fill_the_slots_left(
    tmp_path: Path,
) -> None:
    """Five of hers, then three of his: all three of his and the first two of hers."""
    src = ledger_world(tmp_path)
    for n in range(1, 9):
        visit_row(src, f"v{n}", ledger_at(7, 6 + n), ledger_at(7, 6 + n, 30), f"Place {n}")
    picks = [
        {"id": f"v{n}", "label": f"Hers {n}", "start_ms": moment_ms(ledger_at(7, 6 + n))}
        for n in range(1, 6)
    ]
    append_day_prose(
        src.memory_db,
        day=DAY_KEY,
        text="Night prose.",
        model="m",
        input_chars=1,
        output_chars=1,
        line="Night line.",
        highlights=picks,
    )
    call = Dispatcher(tmp_path, src)
    assert ids_of(served_day(src)) == ["v1", "v2", "v3", "v4", "v5"]

    for n in (8, 7, 6):
        call({"day": DAY_KEY, "time": f"{6 + n:02d}:10"})

    served = served_day(src)["highlights"]
    assert [(h["id"], h["by"]) for h in served] == [
        ("v1", "her"),
        ("v2", "her"),
        ("v6", "him"),
        ("v7", "him"),
        ("v8", "him"),
    ]
    assert len(served) == MAX_HIGHLIGHTS
    for n in (5, 4):  # and four of his leave one of hers
        call({"day": DAY_KEY, "time": f"{6 + n:02d}:10"})
    assert [h["id"] for h in served_day(src)["highlights"]] == ["v4", "v5", "v6", "v7", "v8"]
    call({"day": DAY_KEY, "time": "09:10"})  # a sixth of his is still his
    assert len(served_day(src)["highlights"]) == 6


class Merge(NamedTuple):
    """Her picks, his marks and the ids still on the line; what the day shows."""

    name: str
    picks: list[tuple[str, str, int]]
    marks: list[tuple[str, bool, str | None, str, int]]
    present: set[str] | None
    shown: list[tuple[str, str, str]]  # id, label, by


MERGES: list[Merge] = [
    Merge(
        "hers alone",
        [("a", "A", 3), ("b", "B", 1)],
        [],
        None,
        [("b", "B", "her"), ("a", "A", "her")],
    ),
    Merge(
        "his label over hers",
        [("a", "A", 1)],
        [("a", True, "Mine", "stay", 1)],
        None,
        [("a", "Mine", "him")],
    ),
    Merge(
        "her label when he gives none",
        [("a", "A", 1)],
        [("a", True, None, "stay", 1)],
        None,
        [("a", "A", "him")],
    ),
    Merge(
        "a plain label when neither gave one",
        [],
        [("a", True, None, "stay", 0)],
        None,
        [("a", "stay 17:00", "him")],
    ),
    Merge(
        "unmarked is gone, unmarked and never picked is nothing",
        [("a", "A", 1), ("b", "B", 2)],
        [("a", False, None, "stay", 1), ("z", False, None, "x", 9)],
        None,
        [("b", "B", "her")],
    ),
    Merge(
        "an id off the line is skipped, by whomever",
        [("a", "A", 1), ("b", "B", 2)],
        [("c", True, "C", "stay", 3)],
        {"a"},
        [("a", "A", "her")],
    ),
    Merge(
        "her stale pick leaves its slot to the next",
        [("s", "S", 1), ("a", "A", 2), ("b", "B", 3), ("c", "C", 4), ("d", "D", 5)],
        [("x", True, "X", "k", 0)],
        {"x", "a", "b", "c", "d"},
        [
            ("x", "X", "him"),
            ("a", "A", "her"),
            ("b", "B", "her"),
            ("c", "C", "her"),
            ("d", "D", "her"),
        ],
    ),
    Merge(
        "by start time, then id",
        [("b", "B", 1), ("a", "A", 1)],
        [("c", True, "C", "k", 0)],
        None,
        [("c", "C", "him"), ("a", "A", "her"), ("b", "B", "her")],
    ),
]


@pytest.mark.parametrize("case", MERGES, ids=lambda case: case.name)
def test_the_merge_of_her_picks_and_his_marks(case: Merge) -> None:
    """The one merge rule, as a table; start 0 is the epoch, 17:00 in UTC-7."""
    zone = ZONE
    picks = [{"id": i, "label": label, "start_ms": start} for i, label, start in case.picks]
    marks = [HighlightMark(*one) for one in case.marks]
    merged = merge_highlights(picks, marks, zone, case.present)
    assert [(h.id, h.label, h.by) for h in merged] == case.shown
    assert len(merged) <= max(MAX_HIGHLIGHTS, sum(1 for m in marks if m.on))


def test_recall_returns_the_line_and_the_merged_highlights_with_the_days_summary(
    marked_day: tuple[LedgerSources, Dispatcher],
) -> None:
    """Label and time with the summary; a day with neither keeps the old shape."""
    src, call = marked_day
    append_day_prose(
        src.memory_db,
        day=DAY_KEY,
        text="Night prose.",
        model="m",
        input_chars=1,
        output_chars=1,
        line="Coffee, then work.",
        highlights=[
            {"id": "v-cafe", "label": "Coffee", "start_ms": moment_ms(ledger_at(7, 12))},
            {"id": work_id(7, 13), "label": "Ledger work", "start_ms": moment_ms(ledger_at(7, 13))},
        ],
    )
    call({"day": DAY_KEY, "time": "12:30", "label": "Pho"})  # his, on the picture
    call({"day": DAY_KEY, "time": "13:10", "on": False})  # off with her work pick

    recalled = daily_records.recall(src.memory_db, {"from": "2026-10-07", "to": "2026-10-09"})

    (told,) = recalled["summaries"]
    clock = strftime(
        "%H:%M", localtime(moment_ms(ledger_at(7, 12, 30)) / 1000)
    )  # recall: the machine's
    coffee = strftime("%H:%M", localtime(moment_ms(ledger_at(7, 12)) / 1000))
    assert told["day"] == DAY_KEY
    assert told["line"] == "Coffee, then work."
    assert told["highlights"] == [
        {"label": "Coffee", "time": coffee},
        {"label": "Pho", "time": clock},
    ]
    assert told["summary"].startswith("## 2026-10-07")
    # a day with a summary and nothing from the day line is exactly what it was
    with closing(open_memory_db(src.memory_db)) as conn, conn:
        conn.execute(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, input_chars, "
            "output_chars) VALUES ('s9', '2026-10-06', '2026-10-07T05:00:00-07:00', 'plain', "
            "'m', 1, 1, 1)",
        )
    plain = daily_records.recall(src.memory_db, {"from": "2026-10-06", "to": "2026-10-07"})
    assert plain["summaries"] == [{"day": "2026-10-06", "summary": "plain"}]


# --- the configuration -------------------------------------------------------------------------


def test_the_shipped_ledger_config_asks_for_json_with_a_line_and_for_drafts() -> None:
    """The prompts the daemon ships with: both ask for the JSON the gates read."""
    shipped = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"
    raw = yaml.safe_load(shipped.read_text(encoding="utf-8"))["ledger"]
    settings = LedgerSettings.from_config(raw, "zh")
    assert settings is not None
    assert settings.prose is not None
    prose = settings.prose
    assert prose.reply_language == "zh"
    assert prose.draft_prompt is not None
    for prompt, keys in (
        (prose.prompt, ('"prose"', '"line"', '"highlights"')),
        (prose.draft_prompt, ('"line"', '"highlights"')),
    ):
        assert "JSON" in prompt
        assert all(key in prompt for key in keys)
        assert "LINE LANGUAGE" in prompt
    assert '"prose"' not in prose.draft_prompt
    bare = LedgerSettings.from_config({"enabled": True, "prose": {"prompt": "p"}})
    assert bare is not None
    assert bare.prose is not None
    assert bare.prose.draft_prompt is None  # no draft prompt, no drafts
