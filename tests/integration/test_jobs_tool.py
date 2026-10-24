"""ADR 0367: ``jobs``, the one tool for the job hunt.

It reads the applications the Jobs page shows, adds one the way the page does and changes one the
way the page's edit does, in his local time, and says when job mail was last read. Everything here
is invented: companies, people and their mail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.execution.jobs_tool import JobsSource, build_jobs_tool
from jarvis.execution.tools import Tool, ToolError, ToolRegistry
from jarvis.runtime.bootstrap.catalog import _register_jobs
from jarvis.shared import CallerPrincipal, zone_label
from jarvis.state.ledger import LedgerSources, TerminalScreen, submitted_count
from tests.support import job_mail as job_mail_support
from tests.support.job_mail import (
    NOW,
    OWNER_ZONE,
    JobHarness,
    fixed_zh_words,  # noqa: F401 - autouse, found by name
    job_harness,
)
from tests.support.ledger import terminal_link, write_mail
from tests.support.timesink_moment import CHROME, FakeTimeSink
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.tools import Receipted
    from tests.support.job_mail import JobJev

jev = job_mail_support.jev  # the fixture, found by name in this module

TODAY = NOW.astimezone(OWNER_ZONE).date()  # Wednesday 2026-09-30



def _at(day: date, hour: int = 9) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=OWNER_ZONE)


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


@dataclass
class _Rig:
    """A job mail, the tool over it, and the page's database."""

    harness: JobHarness
    tool: Tool

    @property
    def db(self) -> Path:
        return self.harness.db

    def mail(self, key: str, when: date, **fields: str | None) -> None:
        write_mail(self.db, key, _at(when), **fields)

    def ask(self, **args: Any) -> Receipted:  # noqa: ANN401
        out = self.tool.handler(args, tool_context(clock=self.harness.clock))
        return cast("Receipted", out)

    def call(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401
        return dict(self.ask(**args))

    def fail(self, **args: Any) -> ToolError:  # noqa: ANN401
        with pytest.raises(ToolError) as caught:
            self.ask(**args)
        return caught.value

    def page(self) -> list[dict[str, Any]]:
        """The applications ``GET /inherent/jobs`` serves."""
        applications: list[dict[str, Any]] = self.harness.client.get("/inherent/jobs").json()[
            "applications"
        ]
        return applications


def _rig(tmp_path: Path, jev: JobJev) -> _Rig:
    harness = job_harness(tmp_path, jev, mails=[])
    job = harness.job
    sources = LedgerSources(harness.db, tmp_path / "events.db", None, OWNER_ZONE)
    (tool,) = build_jobs_tool(JobsSource(job.jobs_view, job.add_application, job.edit_application),
                              sources)
    return _Rig(harness, tool)


def _hunt(rig: _Rig) -> None:
    """Six applications in every state a row can be in; one of them he told her of."""
    rig.mail("a1", _ago(16), kind="receipt", company="Acme Robotics", role="Data Analyst Co-op",
             sender_domain="acme-robotics.example", subject="Thanks for applying")
    rig.mail("a2", _ago(8), kind="interview", company="Acme Robotics", role="Data Analyst Co-op",
             sender_domain="acme-robotics.example", subject="Interview invitation",
             event_at=_at(date(2026, 10, 8), 10).isoformat(), event_text="Thursday 10 am")
    rig.mail("o1", _ago(20), kind="receipt", company="Orbit Labs", role="Backend Intern",
             sender_domain="orbit-labs.example", subject="Application received")
    rig.mail("o2", _ago(12), kind="rejection", company="Orbit Labs", role="Backend Intern",
             sender_domain="orbit-labs.example", subject="Update on your application")
    rig.mail("h1", _ago(2), kind="offer", company="Helix Health", role="Backend Developer Intern",
             sender_domain="helix-health.example", subject="Offer of employment")
    rig.mail("n1", _ago(5), kind="receipt", company="Workable", sender_name="Workable",
             sender_domain="workable.com", subject="Thank you for applying to Nova Foods!")
    rig.mail("d1", _ago(30), kind="receipt", company="Delta Corp", role="Analyst",
             sender_domain="delta-corp.example", subject="We received your application")
    rig.harness.job.add_application({
        "company": "Garnet Co", "role": "Data Co-op", "applied_at": _ago(4).isoformat(),
        "source": "said",
    })


def _two_maple_roles(rig: _Rig) -> None:
    """One company he applied to twice for different roles: two rows of his own."""
    for role in ("Data Analyst Co-op", "QA Co-op"):
        rig.harness.job.add_application({
            "company": "Maple Works", "role": role, "applied_at": _ago(6).isoformat(),
            "source": "said",
        })


@pytest.fixture
def rig(tmp_path: Path, jev: JobJev) -> _Rig:
    """A job mail with nothing read yet, and the tool over it."""
    return _rig(tmp_path, jev)


def _by(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["company"]: row for row in rows}


# --- the tool ---------------------------------------------------------------------------------


def test_jobs_is_one_tool_on_the_menu_absent_while_job_mail_is_off(
    rig: _Rig, tmp_path: Path,
) -> None:
    """One tool, L1, reads for list and find only, with no required argument; none without mail."""
    tool = rig.tool
    assert tool.name == "jobs"
    assert (tool.risk_level, tool.read_only) == ("L1", False)
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert tool.input_schema.get("required") is None
    assert tool.reads is not None
    reading = {a: tool.reads({"action": a}) for a in ("list", "find", "add", "update")}
    assert reading == {"list": True, "find": True, "add": False, "update": False}
    assert tool.reads({}) is True  # the default action is list
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert "he " in f"{tool.description} " or " his " in tool.description
    assert "the user" not in tool.description.lower()
    props = tool.input_schema["properties"]
    assert all(p.get("description") for p in props.values())
    assert set(props) == {
        "action", "company", "role", "application", "status", "when", "note", "hidden",
    }

    sources = LedgerSources(rig.db, tmp_path / "events.db", None, OWNER_ZONE)
    on, off = ToolRegistry(), ToolRegistry()
    _register_jobs(on, rig.harness.job, sources)
    _register_jobs(off, None, sources)
    assert [t.name for t in on.get_definitions()] == ["jobs"]
    assert [t.name for t in on.for_caller(CallerPrincipal.JARVIS_LLM)] == ["jobs"]
    assert off.get_definitions() == ()
    assert build_jobs_tool(None, sources) == ()


# --- list -------------------------------------------------------------------------------------


def test_a_list_is_the_jobs_page_in_its_order_numbered_and_summed_up(rig: _Rig) -> None:
    """Row n is the page's n-th application; the summary counts by status and says how fresh."""
    _hunt(rig)
    out = rig.call()
    rows = out["applications"]
    assert [r["company"] for r in rows] == [a["company"] for a in rig.page()]
    assert [r["n"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["id"] for r in rows] == [a["id"] for a in rig.page()]
    by = _by(rows)
    assert by["Helix Health"]["status"] == "offer"
    assert by["Orbit Labs"]["status"] == "rejected"
    assert by["Acme Robotics"]["status"] == "interviewing"
    assert by["Nova Foods"]["status"] == "applied"  # the employer, not Workable
    assert "Workable" not in by
    assert by["Delta Corp"]["status"] == "no_reply"
    assert by["Garnet Co"]["source"] == "he added"
    assert by["Helix Health"]["source"] == "mail"
    summary = out["summary"]
    assert summary.startswith("6 applications: ")
    for part in ("1 offer", "1 interviewing", "2 applied", "1 no reply", "1 rejected"):
        assert part in summary
    assert len(summary) <= 400
    assert "\n" not in summary
    assert out["summary"].count("{") == 0
    receipt = rig.ask().receipt
    assert receipt is not None
    assert (receipt.status, receipt.reads_only) == ("done", True)
    assert receipt.covered is not None
    assert (receipt.covered.shown, receipt.covered.more) == (6, 0)


def test_a_list_can_be_narrowed_by_status_and_by_the_day_he_applied(rig: _Rig) -> None:
    """A status, a stretch of days in his words; the summary says how many of how many."""
    _hunt(rig)
    only = rig.call(status="rejected")
    assert [r["company"] for r in only["applications"]] == ["Orbit Labs"]
    assert only["summary"].startswith("1 rejected application (of 6)")
    week = rig.call(when="last 10 days")
    assert {r["company"] for r in week["applications"]} == {
        "Helix Health", "Nova Foods", "Garnet Co",
    }
    assert " sent " in week["summary"]
    nothing = rig.call(status="offer", when="yesterday")
    assert nothing["applications"] == []
    assert nothing["summary"].startswith("0 offer applications sent yesterday")
    assert rig.fail(status="maybe").code == "invalid_arguments"


def test_an_edit_on_the_jobs_page_is_in_the_next_list_and_a_hidden_row_is_not(rig: _Rig) -> None:
    """The tool reads what the page shows: his edit applied, his hidden row left out."""
    _hunt(rig)
    orbit = _by(rig.page())["Orbit Labs"]["id"]
    delta = _by(rig.page())["Delta Corp"]["id"]
    rig.harness.job.edit_application(orbit, {"status": "applied", "note": "they may reopen it"})
    rig.harness.job.edit_application(delta, {"hidden": True})
    by = _by(rig.call()["applications"])
    assert by["Orbit Labs"]["status"] == "applied"
    assert "Delta Corp" not in by
    assert rig.call()["summary"].startswith("5 applications")


def test_the_count_submitted_is_the_blocks_and_says_at_least(rig: _Rig, tmp_path: Path) -> None:
    """The summary's number is the state block's own count, not the page's row count."""
    _hunt(rig)
    sources = LedgerSources(rig.db, tmp_path / "events.db", None, OWNER_ZONE)
    count = submitted_count(sources, rig.harness.clock.now())
    assert count >= len(rig.page()) - 1
    assert f"At least {count} submitted in all." in rig.call()["summary"]


def test_the_tool_counts_from_the_kept_screen_answers_and_never_asks_the_mac(
    rig: _Rig, tmp_path: Path,
) -> None:
    """Block D reads the terminal's kept answers; so does the tool, so the two numbers agree."""
    _hunt(rig)
    asked: list[str] = []
    live = LedgerSources(
        rig.db, tmp_path / "events.db", None, OWNER_ZONE,
        TerminalScreen(terminal_link(None, asked, up=[True])),
    )
    registry = ToolRegistry()
    _register_jobs(registry, rig.harness.job, live)
    (tool,) = registry.get_definitions()

    tool.handler({}, tool_context(clock=rig.harness.clock))

    assert asked == []
    submitted_count(live, rig.harness.clock.now())  # a live read of the same count does ask
    assert asked


# --- find -------------------------------------------------------------------------------------


def test_a_find_gives_one_application_whole_with_its_timeline_and_mails(rig: _Rig) -> None:
    """The timeline in order with the ahead step marked; mails newest first; the interview held."""
    _hunt(rig)
    out = rig.call(action="find", company="acme robotics")
    (row,) = out["applications"]
    assert row["company"] == "Acme Robotics"
    assert row["timeline"][0].endswith(": applied")
    assert "invited to an interview" in row["timeline"][1]
    assert row["timeline"][-1].endswith("(ahead)") or "interview" in row["timeline"][-1]
    assert [m["kind"] for m in row["mails"]] == ["interview", "receipt"]
    assert row["interview"]
    assert out["summary"].startswith("Acme Robotics (Data Analyst Co-op): interviewing")
    by_role = rig.call(action="find", company="Acme", role="analyst")
    assert len(by_role["applications"]) == 1
    nope = rig.call(action="find", company="Zenith Mills")
    assert nope["applications"] == []
    assert nope["summary"].startswith("No application on his list matches Zenith Mills.")
    assert rig.fail(action="find").code == "invalid_arguments"


def test_a_find_that_matches_two_roles_names_both_and_numbers_them(rig: _Rig) -> None:
    """Two applications at one company are two rows, each findable by its number."""
    _two_maple_roles(rig)
    out = rig.call(action="find", company="Maple")
    assert [r["n"] for r in out["applications"]] == [1, 2]
    assert out["summary"].startswith("2 applications match Maple: ")
    assert "Data Analyst Co-op" in out["summary"]
    assert "QA Co-op" in out["summary"]
    assert [r["role"] for r in rig.call(action="find", company="Maple", role="qa")[
        "applications"]] == ["QA Co-op"]


# --- add --------------------------------------------------------------------------------------


def test_add_writes_the_pages_own_row_as_he_told_it_with_the_day_in_his_words(rig: _Rig) -> None:
    """A company and a role, and 'last Friday' is a day; the row is the page's, source said."""
    before = len(rig.page())
    out = rig.call(action="add", company="Cedar Ltd", role="QA Co-op", when="last Friday")
    assert out["summary"] == "Added Cedar Ltd (QA Co-op), applied Fri Sep 25."
    assert out["added"]["company"] == "Cedar Ltd"
    row = _by(rig.page())["Cedar Ltd"]
    assert (row["role"], row["applied_at"][:10], row["source"]) == (
        "QA Co-op", "2026-09-25", "manual",
    )
    assert len(rig.page()) == before + 1
    assert out["undo"] == {"action": "update", "application": out["added"]["id"], "hidden": True}
    receipt = rig.ask(action="add", company="Dune Inc").receipt
    assert receipt is not None
    assert receipt.reads_only is False
    assert receipt.object is not None
    assert receipt.object.startswith("job:")
    today = _by(rig.page())["Dune Inc"]
    assert today["applied_at"][:10] == TODAY.isoformat()
    assert today["role"] == ""


def test_add_for_one_already_on_the_list_adds_nothing_and_names_its_row(rig: _Rig) -> None:
    """The same company with the same role, or with no role on either side, is the same one."""
    _hunt(rig)
    before = len(rig.page())
    out = rig.call(action="add", company="acme robotics", role="data analyst co-op")
    assert out["summary"].endswith("Nothing added.")
    assert "already on his list" in out["summary"]
    assert out["existing"]["company"] == "Acme Robotics"
    assert len(rig.page()) == before
    assert "Nothing added" in rig.call(action="add", company="Acme Robotics")["summary"]
    assert len(rig.page()) == before
    other = rig.call(action="add", company="Acme Robotics", role="Warehouse Co-op")
    assert other["summary"].startswith("Added Acme Robotics (Warehouse Co-op)")
    assert len(rig.page()) == before + 1
    # the row number it named is a row of the whole list, so an update by that number lands
    number = out["existing"]["n"]
    rig.call(action="add", company="Helix Health")
    held = rig.call(action="update", application=str(number), note="asked about the start date")
    assert held["summary"].startswith("Acme Robotics")


def test_add_refuses_a_day_ahead_a_stretch_and_an_empty_company(rig: _Rig) -> None:
    """An application is on one day that has happened; a company is needed."""
    assert rig.fail(action="add", company="Cedar Ltd", when="next Monday").code == (
        "invalid_arguments"
    )
    assert "stretch" in str(rig.fail(action="add", company="Cedar Ltd", when="last week"))
    assert rig.fail(action="add").code == "invalid_arguments"
    assert rig.fail(action="add", company="Cedar Ltd", status="applied").code == (
        "invalid_arguments"
    )
    assert "Cedar Ltd" not in _by(rig.page())


# --- update -----------------------------------------------------------------------------------


def test_update_changes_a_row_and_says_from_what_to_what_with_the_way_back(rig: _Rig) -> None:
    """The page's own edit: a status, a day, a note; the answer names the old value."""
    _hunt(rig)
    helix = _by(rig.call()["applications"])["Helix Health"]
    out = rig.call(action="update", application=helix["id"], status="rejected")
    assert out["summary"] == "Helix Health (Backend Developer Intern): status offer to rejected."
    assert out["changed"] == [{"field": "status", "from": "offer", "to": "rejected"}]
    assert out["undo"] == {"action": "update", "application": helix["id"], "status": "offer"}
    assert _by(rig.page())["Helix Health"]["status"] == "rejected"
    back = rig.call(**out["undo"])
    assert _by(rig.page())["Helix Health"]["status"] == "offer"
    assert back["changed"][0]["to"] == "offer"
    same = rig.call(action="update", application=helix["id"], status="offer")
    assert same["changed"] == []
    assert same["summary"].endswith("nothing changed.")
    dated = rig.call(action="update", application=helix["id"], when="last Monday", note="x")
    assert {c["field"] for c in dated["changed"]} == {"applied_at", "note"}
    assert _by(rig.page())["Helix Health"]["note"] == "x"
    assert rig.fail(action="update", application=helix["id"]).code == "invalid_arguments"


def test_update_puts_a_hidden_row_back_by_its_id(rig: _Rig) -> None:
    """Hiding is an update, and so is its undo, though the hidden row is not on the list."""
    _hunt(rig)
    delta = _by(rig.call()["applications"])["Delta Corp"]
    hid = rig.call(action="update", application=delta["id"], hidden=True)
    assert hid["summary"].endswith("taken off his list.")
    assert "Delta Corp" not in _by(rig.call()["applications"])
    shown = rig.call(**hid["undo"])
    assert shown["summary"].endswith("put back on his list.")
    assert "Delta Corp" in _by(rig.call()["applications"])


def test_update_finds_its_row_by_number_by_id_or_by_company(rig: _Rig) -> None:
    """A number is the place in the last list; an id always works; a company when it is one."""
    _hunt(rig)
    rows = rig.call()["applications"]
    nova = next(r for r in rows if r["company"] == "Nova Foods")
    by_number = rig.call(action="update", application=str(nova["n"]), note="one")
    assert by_number["summary"].startswith("Nova Foods:")
    by_id = rig.call(action="update", application=nova["id"], note="two")
    assert by_id["changed"][0]["from"] == "one"
    by_name = rig.call(action="update", application="nova foods", note="three")
    assert by_name["changed"][0]["from"] == "two"
    assert rig.fail(action="update", application="Zenith Mills", note="x").code == "not_found"
    gone = rig.fail(action="update", application="9", note="x")
    assert gone.code == "not_found"
    assert "rows 1 to" in str(gone)


def test_a_company_with_two_applications_is_asked_about_not_guessed(rig: _Rig) -> None:
    """Update by a company name that matches two rows is a question for him, with the ids."""
    _two_maple_roles(rig)
    error = rig.fail(action="update", application="Maple", status="rejected")
    assert (error.code, error.kind) == ("ambiguous", "needs_him")
    ids = {a["id"] for a in rig.page()}
    assert all(i in str(error) for i in ids)
    assert {a["status"] for a in rig.page()} == {"applied"}  # nothing was changed


def test_a_row_number_with_no_list_since_the_restart_asks_for_one_first(rig: _Rig) -> None:
    """The numbers are kept in memory; a fresh process has none, and says what to do."""
    _hunt(rig)
    error = rig.fail(action="update", application="2", status="rejected")
    assert (error.code, error.kind) == ("no_list", "retry_other_args")
    assert "jobs()" in str(error)
    assert {a["status"] for a in rig.page()} >= {"offer"}
    rig.call()
    assert rig.call(action="update", application="2", note="ok")["changed"]


def test_an_argument_that_does_not_go_with_the_action_is_named(rig: _Rig) -> None:
    """A status on a find, or a made-up argument, comes back as invalid with the way out."""
    wrong = rig.fail(action="find", company="Acme", status="offer")
    assert wrong.code == "invalid_arguments"
    assert "status does not go with action=find" in str(wrong)
    assert "no argument 'colour'" in str(rig.fail(colour="red"))
    assert "action must be one of" in str(rig.fail(action="delete"))
    assert "true or false" in str(rig.fail(action="update", application="x", hidden="yes"))


# --- time -------------------------------------------------------------------------------------


def test_the_interview_is_both_readings_where_a_pacific_label_reads_two_ways(rig: _Rig) -> None:
    """ADR 0379: 10 am PST on a November day is 10:00 or 11:00 here; in July it is one time."""
    for key, day, company in (
        ("w", date(2026, 11, 12), "Winter Works"),
        ("s", date(2027, 7, 15), "Summer Supply"),
    ):
        first, *later = zone_label.read("10 am PST", day, (10, 0), OWNER_ZONE)
        rig.mail(key, _ago(1), kind="interview", company=company, role="Intern",
                 sender_domain=f"{key}-co.example", subject="Interview",
                 event_at=first.isoformat(), event_text="Interview at 10 am PST",
                 event_later_at=later[0].isoformat() if later else None)
    by = _by(rig.call()["applications"])
    winter = by["Winter Works"]["interview"]
    assert "10:00 or 11:00" in winter
    assert "10 am PST" in winter
    assert winter.startswith("Thu Nov 12, ")
    summer = by["Summer Supply"]["interview"]
    assert "10:00" in summer
    assert " or " not in summer


def test_an_eastern_time_is_told_in_his_clock(rig: _Rig) -> None:
    """2 pm ET is what o'clock here, as one time: Eastern is one zone."""
    (when,) = zone_label.read("2 pm ET", date(2027, 7, 15), (14, 0), OWNER_ZONE)
    rig.mail("e", _ago(1), kind="interview", company="East Mills", role="Intern",
             sender_domain="east-mills.example", subject="Interview",
             event_at=when.isoformat(), event_text="Wednesday 2 pm ET")
    interview = _by(rig.call()["applications"])["East Mills"]["interview"]
    assert "11:00" in interview
    assert " or " not in interview


def test_time_on_the_site_is_unknown_not_zero_when_nothing_could_read_it(
    rig: _Rig, tmp_path: Path,
) -> None:
    """No TimeSink answered: every row says unknown and the summary says why; never 0."""
    _hunt(rig)
    out = rig.call()
    assert {r["on_site_14d"] for r in out["applications"]} == {"unknown"}
    assert "His time on job sites is unknown" in out["summary"]
    rig.harness.job.timesink_path = tmp_path / "no-such-store.sqlite"
    again = rig.call()
    assert {r["on_site_14d"] for r in again["applications"]} == {"unknown"}


def test_time_on_the_site_is_said_when_the_store_answers(rig: _Rig, tmp_path: Path) -> None:
    """A TimeSink with spans: Acme's minutes are said, a company he never visited is 0 minutes."""
    _hunt(rig)
    store = FakeTimeSink(tmp_path)
    store.span(900, 300, CHROME, "Google Chrome", domain="careers.acme-robotics.example",
               title="Apply", url="https://careers.acme-robotics.example/jobs/1")
    rig.harness.job.timesink_path = store.path
    out = rig.call()
    by = _by(out["applications"])
    assert by["Acme Robotics"]["on_site_14d"] == "10m"
    assert by["Orbit Labs"]["on_site_14d"] != "unknown"
    assert "unknown" not in out["summary"]


# --- how fresh --------------------------------------------------------------------------------


def test_the_answer_says_when_job_mail_was_last_read(rig: _Rig) -> None:
    """Never read since the start, then read now; and since when reading has been failing."""
    first = rig.call()["summary"]
    assert "Job mail has not been read since the start" in first
    rig.harness.job.poll_once()
    second = rig.call()["summary"]
    assert "Job mail was last read today" in second
    assert "10:30 am" in second
    rig.harness.clock.advance(3600)
    rig.harness.gmail.search_failures = 5
    with pytest.raises(Exception):  # noqa: B017, PT011 - the poller's own failure
        rig.harness.job.poll_once()
    third = rig.call()["summary"]
    assert "Job mail was last read today" in third
    assert "reading has failed since today" in third
    assert "11:30 am" in third


# --- size -------------------------------------------------------------------------------------


def test_a_long_list_still_fits_the_budget_and_says_how_many_were_left_out(rig: _Rig) -> None:
    """Rows are cut whole from the end; the summary names how to narrow."""
    for n in range(60):
        rig.harness.job.add_application({
            "company": f"Company Number {n:02d} {'Holdings ' * 6}", "role": "R" * 90,
            "applied_at": _ago(n % 20).isoformat(), "source": "said",
        })
    out = rig.ask()
    answer = dict(out)
    size = len(json.dumps(answer, ensure_ascii=False))
    assert size <= rig.tool.max_result_chars
    shown = len(answer["applications"])
    assert 0 < shown < 60
    assert f"{60 - shown} not shown; narrow with status or when, or find a company." in (
        answer["summary"]
    )
    assert len(answer["summary"]) <= 400
    assert out.receipt is not None
    assert out.receipt.covered is not None
    assert (out.receipt.covered.shown, out.receipt.covered.more) == (shown, 60 - shown)


def test_a_found_application_with_many_mails_shows_five_and_counts_the_rest(rig: _Rig) -> None:
    """Whole, but not endless: five mails, newest first, and how many more there are."""
    for n in range(9):
        rig.mail(f"x{n}", _ago(20 - n), kind="other", company="Maple Works", role="Intern",
                 sender_domain="maple-works.example", subject=f"Update {n} " + "s" * 200)
    (row,) = rig.call(action="find", company="Maple Works")["applications"]
    assert len(row["mails"]) == 5
    assert row["more_mails"] == 9 - 5
    assert all(len(m["subject"]) <= 80 for m in row["mails"])
