"""ADR 0161 — the moment: TimeSink's saved spans decide when alerts wait, and what a snapshot says.

A fake TimeSink store is built in a tmp dir with the real ``span`` and ``stateEvent`` tables, as
read from Allen's own store. The real ``Moment`` reads it; the job-mail harness of
``test_job_mail`` runs the poller, the routes and the ledger over it. Each check asserts what is
read, which alerts are served or spoken, what the ledger page shows, or what a snapshot holds.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.decision import moment as rules
from jarvis.decision.attention import replay, rule_judge_v1
from jarvis.runtime import RuntimeBootstrapError, _moment
from jarvis.runtime.card_feedback import CardFeedback
from jarvis.runtime.moment import Moment, MomentSettings
from jarvis.shared import lang
from jarvis.state import job_ledger, job_time, timesink_moment
from tests.canary._helpers import repo_root
from tests.integration.test_job_mail import _Harness, _harness, _Jev

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# The two tables exactly as TimeSink's own store creates them (columns the reader never uses stay).
_SPAN = (
    'CREATE TABLE "span" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "start" DATETIME NOT NULL, '
    '"end" DATETIME NOT NULL, "appBundleID" TEXT NOT NULL, "appName" TEXT NOT NULL, "title" TEXT, '
    '"url" TEXT, "domain" TEXT, "document" TEXT, "deviceID" TEXT, "originID" INTEGER, '
    '"remoteSeq" TEXT, "keySeconds" INTEGER NOT NULL DEFAULT 0)'
)
_EVENT = (
    'CREATE TABLE "stateEvent" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "at" DATETIME NOT NULL, '
    '"kind" TEXT NOT NULL)'
)
# Words that live only in window titles and URLs; none may ever reach a doc or a snapshot.
TITLE_WORD = "Quarterly-Plan-TITLE-WORD"
PATH_WORD = "private-PATH-WORD-4471"
CHROME = "com.google.Chrome"
T0 = datetime.now(UTC).replace(microsecond=0)


def _text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class FakeTimeSink:
    """A TimeSink store in a tmp dir; times are given as seconds before ``T0``."""

    def __init__(self, directory: Path) -> None:
        """Create the store with the two tables."""
        self.path = directory / "timesink.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.execute(_SPAN)
            conn.execute(_EVENT)

    def span(  # noqa: PLR0913 - the row's columns
        self,
        start: float,
        end: float,
        bundle: str,
        name: str,
        *,
        title: str | None = None,
        url: str | None = None,
        domain: str | None = None,
        at: datetime = T0,
    ) -> None:
        """Add a span row."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO span (start,end,appBundleID,appName,title,url,domain)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    _text(at - timedelta(seconds=start)),
                    _text(at - timedelta(seconds=end)),
                    bundle,
                    name,
                    title,
                    url,
                    domain,
                ),
            )

    def event(self, ago: float, kind: str, at: datetime = T0) -> None:
        """Add a state-event row."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO stateEvent (at,kind) VALUES (?,?)",
                (_text(at - timedelta(seconds=ago)), kind),
            )


def _moment_for(
    store: FakeTimeSink | Path, db: Path, fields: dict[str, bool] | None = None
) -> Moment:
    path = store.path if isinstance(store, FakeTimeSink) else store
    moment = Moment(MomentSettings(fields or dict.fromkeys(rules.FIELDS, True)), path, db)
    moment.now = lambda: T0
    return moment


def _facts(store: FakeTimeSink, tmp_path: Path) -> dict[str, Any]:
    return _moment_for(store, tmp_path / "memory.db").facts()


@pytest.fixture(autouse=True)
def _zh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-a-secret")
    before = lang.language()
    lang.set_language("zh")
    yield
    lang.set_language(before)


@pytest.fixture
def jev() -> Iterator[_Jev]:
    """Jev."""
    fake = _Jev()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def store(tmp_path: Path) -> FakeTimeSink:
    """Store."""
    return FakeTimeSink(tmp_path)


def _job_harness(
    tmp_path: Path,
    jev: _Jev,
    moment_store: FakeTimeSink | Path | None,
    **settings: Any,  # noqa: ANN401
) -> _Harness:
    """The job-mail harness with a moment (and the ledger's time column) over ``moment_store``."""
    h = _harness(tmp_path, jev, **settings)
    if moment_store is not None:
        h.job.moment = _moment_for(moment_store, h.db)
        h.job.timesink_path = h.job.moment._path  # noqa: SLF001 - the same store
    return h


# --- what is read -----------------------------------------------------------------------


def test_front_app_site_and_since(store: FakeTimeSink, tmp_path: Path) -> None:
    """The newest span is the front app; its stretch (same app and site) began at ``since``."""
    store.span(900, 400, "com.anthropic.claudefordesktop", "Claude", title="Claude")
    store.span(
        400, 200, CHROME, "Google Chrome", title="A", url="https://a.example/x", domain="a.example"
    )
    store.span(
        200, 5, CHROME, "Google Chrome", title="B", url="https://a.example/y", domain="a.example"
    )
    facts = _facts(store, tmp_path)
    assert facts["read"] == "ok"
    assert (facts["front_app"], facts["site_domain"]) == ("Google Chrome", "a.example")
    assert facts["since"] == (T0 - timedelta(seconds=400)).isoformat(timespec="seconds")
    assert (facts["presence"], facts["in_call"], facts["screen_share"]) == (
        "active",
        "no",
        "unknown",
    )
    assert facts["today"] != "unknown"


@pytest.mark.parametrize(
    ("marks", "ended", "expected"),
    [
        ([(600, "idle")], 600, "idle"),
        ([(300, "lock")], 300, "locked"),
        ([(300, "lock"), (200, "sleep")], 300, "asleep"),
        ([(600, "idle"), (100, "active")], 600, None),  # he came back: the span would be fresh
        ([(300, "lock"), (100, "unlock")], 300, None),
        ([(300, "lock"), (100, "stop")], 300, None),  # TimeSink stopped since: not trusted
    ],
)
def test_idle_locked_and_asleep_come_from_timesinks_own_marks(
    store: FakeTimeSink,
    tmp_path: Path,
    marks: list[tuple[float, str]],
    ended: float,
    expected: str | None,
) -> None:
    """A mark with no span after it is the state; a resume, a stop or a stale mark is not."""
    store.span(ended + 400, ended, CHROME, "Google Chrome", domain="a.example")
    for ago, kind in marks:
        store.event(ago, kind)
    facts = _facts(store, tmp_path)
    if expected is None:
        assert facts["presence"] == "unknown"  # the span is stale and nothing explains why
        assert rules.hold_reason(facts) is None
    else:
        assert facts["presence"] == expected
        assert rules.hold_reason(facts) == expected


def test_a_span_after_the_mark_means_he_is_active_again(
    store: FakeTimeSink, tmp_path: Path
) -> None:
    """A span after the mark means he is active again."""
    store.span(900, 600, CHROME, "Google Chrome", domain="a.example")
    store.event(600, "idle")
    store.span(100, 3, CHROME, "Google Chrome", domain="a.example")
    assert _facts(store, tmp_path)["presence"] == "active"


@pytest.mark.parametrize(
    ("bundle", "title", "domain", "url", "app"),
    [
        ("us.zoom.xos", "Zoom Meeting", None, None, "Zoom"),
        ("us.zoom.xos", "zoom floating video window", None, None, "Zoom"),
        ("us.zoom.xos", "Zoom Workplace", None, None, None),  # the lobby is not a call
        ("us.zoom.xos", None, None, None, None),
        (
            CHROME,
            "Meet - abc-defg-hij",
            "meet.google.com",
            "https://meet.google.com/abc-defg-hij",
            "Google Meet",
        ),
        (CHROME, "Google Meet", "meet.google.com", "https://meet.google.com/landing", None),
        (CHROME, "Join", "us02web.zoom.us", "https://us02web.zoom.us/wc/123/join", "Zoom"),
        (CHROME, "Launch Meeting - Zoom", "us02web.zoom.us", "https://us02web.zoom.us/j/123", None),
        (
            CHROME,
            "Meeting join | Coffee Chat",
            "teams.microsoft.com",
            "https://teams.microsoft.com/v2/",
            "Microsoft Teams",
        ),
        ("com.microsoft.teams2", "Chat | Bo | Microsoft Teams", None, None, None),
        ("com.apple.FaceTime", "Bo", None, None, "FaceTime"),
        ("com.apple.FaceTime", "FaceTime", None, None, None),
        ("com.webex.meetingmanager", "Webex", None, None, "Webex"),
        ("com.tencent.meeting", "", None, None, "Tencent Meeting"),
        ("com.tencent.meeting", "腾讯会议", None, None, None),
        ("com.hnc.Discord", "General | Server - Discord", None, None, None),  # not detectable
    ],
)
def test_call_windows_by_app_title_and_site(
    bundle: str, title: str | None, domain: str | None, url: str | None, app: str | None
) -> None:
    """Call windows by app title and site."""
    assert timesink_moment.call_app(bundle, title, domain, url) == app


def test_a_call_holds_until_its_window_has_been_gone_for_the_grace(
    store: FakeTimeSink, tmp_path: Path
) -> None:
    """In a call while its window is front or was within the grace; screen share is unknown."""
    store.span(200, 40, "us.zoom.xos", "zoom.us", title="Zoom Meeting")
    store.span(40, 3, "com.mitchellh.ghostty", "Ghostty", title="zsh")
    facts = _facts(store, tmp_path)
    assert (facts["in_call"], facts["call_app"], facts["front_app"]) == ("yes", "Zoom", "Ghostty")
    assert rules.hold_reason(facts) == "call"
    assert facts["screen_share"] == "unknown"

    later = Moment(MomentSettings(dict.fromkeys(rules.FIELDS, True)), store.path, tmp_path / "m.db")
    later.now = lambda: T0 + timedelta(seconds=timesink_moment.CALL_GRACE_S)
    # 40 s ago + the grace is past, but the span is stale then, so no call and no hold.
    assert later.hold() is None


def test_idle_in_a_call_is_still_a_call(store: FakeTimeSink, tmp_path: Path) -> None:
    """Listening in a meeting looks idle to TimeSink; the window just before it still counts."""
    store.span(
        900,
        600,
        CHROME,
        "Google Chrome",
        title="Meet - abc-defg-hij",
        url="https://meet.google.com/abc-defg-hij",
        domain="meet.google.com",
    )
    store.event(600, "idle")
    facts = _facts(store, tmp_path)
    assert (facts["presence"], facts["in_call"]) == ("idle", "yes")
    assert rules.hold_reason(facts) == "call"


def test_stale_or_unreadable_store_is_unknown_everywhere(
    store: FakeTimeSink, tmp_path: Path
) -> None:
    """Stale or unreadable store is unknown everywhere."""
    store.span(900, timesink_moment.FRESH_S + 30, CHROME, "Google Chrome", domain="a.example")
    stale = _facts(store, tmp_path)
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not a database")
    for facts in (
        stale,
        _moment_for(tmp_path / "missing.sqlite", tmp_path / "m.db").facts(),
        _moment_for(broken, tmp_path / "m.db").facts(),
        _moment_for(tmp_path, tmp_path / "m.db").facts(),  # a directory
    ):
        assert facts["read"] in ("stale", "unreadable")
        assert {
            facts[k] for k in ("front_app", "site_domain", "since", "presence", "in_call", "today")
        } == {"unknown"}
        assert rules.hold_reason(facts) is None
    assert stale["read"] == "stale"


def test_the_store_is_only_read(store: FakeTimeSink, tmp_path: Path) -> None:
    """The store is only read."""
    store.span(100, 3, CHROME, "Google Chrome", domain="a.example")
    before = store.path.read_bytes()
    _moment_for(store, tmp_path / "memory.db").snapshot()
    assert store.path.read_bytes() == before


# --- the 现况 doc -----------------------------------------------------------------------


def _secret_store(store: FakeTimeSink) -> FakeTimeSink:
    store.span(
        3000,
        5,
        CHROME,
        "Google Chrome",
        title=TITLE_WORD,
        url=f"https://careers.example/{PATH_WORD}",
        domain="careers.example",
    )
    store.span(3600, 3000, "us.zoom.xos", "zoom.us", title=f"Zoom Meeting {TITLE_WORD}")
    return store


def test_the_doc_names_apps_and_domains_never_titles_or_urls(
    store: FakeTimeSink, tmp_path: Path
) -> None:
    """The doc names apps and domains never titles or urls."""
    snap = _moment_for(_secret_store(store), tmp_path / "memory.db").snapshot()
    doc = snap["doc"]
    assert doc["text"].startswith("Now: front app Google Chrome, site careers.example, since ")
    assert "Today so far: " in doc["text"]
    assert len(doc["text"]) <= rules.DOC_MAX_CHARS
    dumped = json.dumps(snap, ensure_ascii=False)
    for secret in (TITLE_WORD, PATH_WORD, "https://", "Zoom Meeting"):
        assert secret not in dumped
    assert set(doc["fields"]) == set(rules.FIELDS)


def test_each_field_can_be_switched_off(store: FakeTimeSink, tmp_path: Path) -> None:
    """Each field can be switched off."""
    _secret_store(store)
    on = _moment_for(store, tmp_path / "m.db").snapshot()["doc"]
    assert "site careers.example" in on["text"]
    fields = dict.fromkeys(rules.FIELDS, True)
    for name, gone in (
        ("site", "site "),
        ("since", "since "),
        ("call", "call"),
        ("presence", "at the computer"),
    ):
        off = _moment_for(store, tmp_path / "m.db", {**fields, name: False}).snapshot()["doc"]
        assert name not in off["fields"]
        assert gone not in off["text"]
        assert len(off["fields"]) == len(rules.FIELDS) - 1
    none = _moment_for(store, tmp_path / "m.db", dict.fromkeys(rules.FIELDS, False)).snapshot()[
        "doc"
    ]
    assert none == {"text": "", "fields": {}}
    no_today = {**fields, "job_today": False, "apps_today": False}
    assert (
        "Today so far"
        not in _moment_for(store, tmp_path / "m.db", no_today).snapshot()["doc"]["text"]
    )


def test_unknown_facts_render_as_unknown_and_the_doc_stays_short(tmp_path: Path) -> None:
    """Unknown facts render as unknown and the doc stays short."""
    facts = _moment_for(tmp_path / "missing.sqlite", tmp_path / "m.db").facts()
    doc = rules.render_doc(facts, dict.fromkeys(rules.FIELDS, True))
    assert doc["text"] == "Now: unknown.\nToday so far: unknown."
    many = {**facts, "front_app": "x" * 600, "read": "ok"}
    assert (
        len(rules.render_doc(many, dict.fromkeys(rules.FIELDS, True), "y" * 900)["text"])
        <= rules.DOC_MAX_CHARS
    )


def test_the_moment_block_is_validated_at_boot(tmp_path: Path) -> None:
    """The moment block is validated at boot."""
    shipped = yaml.safe_load((repo_root() / "config/jarvis.yaml").read_text())
    block = shipped["moment"]
    assert block["enabled"] is True
    assert set(block["fields"]) == set(rules.FIELDS)
    path, db = tmp_path / "jarvis.yaml", tmp_path / "memory.db"
    assert isinstance(_moment(shipped, path, db), Moment)
    assert _moment({}, path, db) is None
    assert _moment({"moment": {**block, "enabled": False}}, path, db) is None
    for bad in (
        {**block, "enabled": "yes"},
        {**block, "fields": {**block["fields"], "site": "yes"}},
        {**block, "fields": {k: v for k, v in block["fields"].items() if k != "site"}},
        {**block, "fields": {**block["fields"], "titles": True}},
    ):
        with pytest.raises(RuntimeBootstrapError, match="moment"):
            _moment({"moment": bad}, path, db)


# --- delivery ----------------------------------------------------------------------------


def _in_a_call(store: FakeTimeSink) -> FakeTimeSink:
    store.span(
        300,
        3,
        CHROME,
        "Google Chrome",
        title="Meet - abc-defg-hij",
        url="https://meet.google.com/abc-defg-hij",
        domain="meet.google.com",
    )
    return store


def test_in_a_call_nothing_is_shown_spoken_or_marked_then_one_summary(
    tmp_path: Path, jev: _Jev, store: FakeTimeSink
) -> None:
    """Held alerts stay pending, never ``shown``; nothing is spoken; the end is one summary."""
    _in_a_call(store)
    h = _job_harness(tmp_path, jev, store)
    h.job.poll_once()
    assert h.notices() == []
    assert h.spoken == 0
    states = h.sql("SELECT state, shown_at FROM job_alert")
    assert states
    assert {s for s, _ in states} == {"pending"}
    assert {at for _, at in states} == {None}
    delivery = [json.loads(d) for (d,) in h.sql("SELECT delivery FROM attention_log")]
    assert any("held_call" in d for d in delivery)
    assert any("suppressed" in d for d in delivery)  # the spoken line never started

    # The call ends: its window is gone for longer than the grace and he is active elsewhere.
    h.age_alerts(timedelta(minutes=2))
    (tmp_path / "later").mkdir()
    other = FakeTimeSink(tmp_path / "later")
    other.span(100, 3, "com.mitchellh.ghostty", "Ghostty", title="zsh")
    h.job.moment = _moment_for(other, h.db)
    notices = h.notices()
    assert [n["kind"] for n in notices] == ["digest"]
    assert len(notices[0]["items"]) >= 2
    assert h.spoken == 0


def test_away_holds_until_he_returns(tmp_path: Path, jev: _Jev, store: FakeTimeSink) -> None:
    """Away holds until he returns."""
    store.span(900, 600, CHROME, "Google Chrome", domain="a.example")
    store.event(600, "lock")
    h = _job_harness(tmp_path, jev, store)
    h.job.poll_once()
    assert h.notices() == []
    assert {s for (s,) in h.sql("SELECT state FROM job_alert")} == {"pending"}
    store.event(100, "unlock")
    store.span(90, 3, CHROME, "Google Chrome", domain="a.example")
    h.job.moment = _moment_for(store, h.db)  # a fresh read: he is back
    assert h.notices()


@pytest.mark.parametrize("broken", ["missing", "stale", "off"])
def test_unknown_behaves_exactly_as_before(
    tmp_path: Path, jev: _Jev, store: FakeTimeSink, broken: str
) -> None:
    """An unreadable or stale store, or no moment at all, serves the same notices and speaks."""
    (tmp_path / "base").mkdir()
    baseline = _harness(tmp_path / "base", jev)
    baseline.job.poll_once()
    if broken == "stale":
        store.span(900, 400, CHROME, "Google Chrome", domain="a.example")
    sources: dict[str, FakeTimeSink | Path | None] = {
        "missing": tmp_path / "gone.sqlite",
        "stale": store,
        "off": None,
    }
    source = sources[broken]
    (tmp_path / "with").mkdir()
    h = _job_harness(tmp_path / "with", jev, source)
    h.job.poll_once()

    def shape(one: _Harness) -> list[tuple[str, str, str]]:
        return sorted((n["kind"], n["level"], n["title"]) for n in one.notices())

    assert shape(h) == shape(baseline)
    assert h.spoken == baseline.spoken >= 1


# --- snapshots ---------------------------------------------------------------------------


def test_every_decision_snapshot_carries_the_moment_and_no_titles(
    tmp_path: Path, jev: _Jev, store: FakeTimeSink
) -> None:
    """Every decision snapshot carries the moment and no titles."""
    _secret_store(store)
    h = _job_harness(tmp_path, jev, store)
    h.job.poll_once()
    rows = h.sql("SELECT stage, moment_json FROM job_decision")
    assert {stage for stage, _ in rows} >= {"header", "body"}
    for _stage, raw in rows:
        snap = json.loads(raw)
        assert snap["facts"]["front_app"] == "Google Chrome"
        assert snap["facts"]["site_domain"] == "careers.example"
        assert snap["doc"]["text"].startswith("Now: ")
        assert set(snap["doc"]["fields"]) == set(rules.FIELDS)
    packs = [json.loads(p) for (p,) in h.sql("SELECT pack_json FROM attention_log")]
    assert packs
    assert all(p["situation"]["moment"]["facts"]["read"] == "ok" for p in packs)
    everything = json.dumps([rows, packs], ensure_ascii=False)
    for secret in (TITLE_WORD, PATH_WORD):
        assert secret not in everything
    # Replaying a logged pack still works with the moment in its situation.
    assert replay(rule_judge_v1, [{"pack_json": json.dumps(p)} for p in packs])


def test_a_card_snapshot_carries_the_moment_and_no_titles(
    tmp_path: Path, store: FakeTimeSink
) -> None:
    """A card snapshot (ADR 0160) holds the same moment: app names and domains only."""
    db = tmp_path / "memory.db"
    cards = CardFeedback(db, lambda: "off", _moment_for(_secret_store(store), db))
    cards.act("pop-1", {"action": "seen", "kind": "pop", "level": "card", "facts": {"count": 1}})
    (row,) = job_ledger.list_attention(db, "card:pop")
    situation = json.loads(row["pack_json"])["situation"]
    assert situation["quiet"] == "off"
    snap = situation["moment"]
    assert snap["facts"]["front_app"] == "Google Chrome"
    assert snap["facts"]["site_domain"] == "careers.example"
    assert snap["doc"]["text"].startswith("Now: ")
    for secret in (TITLE_WORD, PATH_WORD, "https://", "Zoom Meeting"):
        assert secret not in row["pack_json"]


def test_an_old_decision_table_gains_the_column(tmp_path: Path) -> None:
    """An old decision table gains the column."""
    db = tmp_path / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE job_decision (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,"
            " at TEXT, stage TEXT, sender_name TEXT, sender_domain TEXT, subject TEXT,"
            " received_at TEXT, probabilities TEXT, judge TEXT, body_excerpt TEXT, verdict TEXT)"
        )
    job_ledger.record_decision(db, "m", "header", "job", T0, moment={"facts": {"read": "ok"}})
    with sqlite3.connect(db) as conn:
        assert json.loads(conn.execute("SELECT moment_json FROM job_decision").fetchone()[0]) == {
            "facts": {"read": "ok"}
        }


# --- the ledger's time column ------------------------------------------------------------


def _day(moment: datetime) -> str:
    return moment.astimezone().date().isoformat()


def test_the_ledger_shows_time_per_company_and_the_other_job_sites(
    tmp_path: Path, jev: _Jev, store: FakeTimeSink
) -> None:
    """The ledger shows time per company and the other job sites."""
    now = datetime.now(UTC).replace(microsecond=0)
    h = _job_harness(tmp_path, jev, store)
    h.job.poll_once()
    # Ten minutes on LinkedIn jobs with Northwind in the title; 90 s of a company's own career site
    # (Helix, by its mail domain); 30 s on a Workday host that names CGI; 60 s on an unnamed ATS.
    store.span(
        900,
        300,
        CHROME,
        "Google Chrome",
        at=now,
        domain="www.linkedin.com",
        title="Software Developer Co-op - Northwind | LinkedIn",
        url="https://www.linkedin.com/jobs/view/1",
    )
    store.span(
        300,
        210,
        CHROME,
        "Google Chrome",
        at=now,
        domain="careers.helix.example",
        title="Open roles",
        url="https://careers.helix.example/roles",
    )
    store.span(
        200,
        170,
        CHROME,
        "Google Chrome",
        at=now,
        domain="cgi.wd3.myworkdayjobs.com",
        title="Apply",
        url="https://cgi.wd3.myworkdayjobs.com/en-US/x",
    )
    store.span(
        160,
        100,
        CHROME,
        "Google Chrome",
        at=now,
        domain="acmecorp.greenhouse.io",
        title="Apply now",
        url="https://boards.greenhouse.io/other/jobs/1",
    )
    store.span(
        90,
        30,
        CHROME,
        "Google Chrome",
        at=now,
        domain="www.linkedin.com",
        title="Feed | LinkedIn",
        url="https://www.linkedin.com/feed/",
    )  # not jobs
    store.span(
        30,
        0,
        CHROME,
        "Google Chrome",
        at=now,
        domain="www.linkedin.com",
        title="ai Jobs | LinkedIn",
        url="https://www.linkedin.com/jobs/search/",
    )  # no company
    reply = h.client.get("/inherent/jobs").json()
    by_company = {g["company"]: g for g in reply["ledger"]}
    spent = {name: g["time_spent"] for name, g in by_company.items()}

    def seconds(name: str) -> int:
        """Seconds."""
        return sum(one["seconds"] for one in spent[name])

    assert (seconds("Northwind"), by_company["Northwind"]["time_total_s"]) == (600, 600)
    assert seconds("Helix") == 90
    assert seconds("CGI") == 30
    assert spent["Orbital"] == []
    assert by_company["Orbital"]["time_total_s"] == 0
    # LinkedIn's own digests make a "LinkedIn" ledger group; it never collects job-page time.
    assert by_company["LinkedIn"]["time_total_s"] == 0
    assert reply["job_site_other_s"] == 90  # the unnamed ATS page and the unnamed LinkedIn search
    assert all(
        one["day"] == _day(now) or one["day"] == _day(now - timedelta(seconds=900))
        for one in spent["Northwind"]
    )
    # Titles and URLs are used to attribute, never returned.
    assert "Northwind |" not in json.dumps(reply["ledger"][0]["time_spent"])


def test_time_is_split_at_local_midnight_and_limited_to_fourteen_days(store: FakeTimeSink) -> None:
    """Time is split at local midnight and limited to fourteen days."""
    noon = datetime.combine(datetime.now(UTC).astimezone().date(), time(12)).astimezone(UTC)
    midnight = datetime.combine(datetime.now(UTC).astimezone().date(), time.min).astimezone(UTC)
    books = [job_time.Company("Northwind", frozenset())]
    # 20 minutes before and 10 after this local midnight, and a span 20 days ago.
    store.span(
        1800,
        0,
        CHROME,
        "Google Chrome",
        at=midnight + timedelta(seconds=600),
        domain="x.myworkdayjobs.com",
        title="Northwind",
    )
    old = noon - timedelta(days=20)
    store.span(
        600, 0, CHROME, "Google Chrome", at=old, domain="x.myworkdayjobs.com", title="Northwind"
    )
    from jarvis.state import timesink  # noqa: PLC0415

    with timesink.snapshot(store.path) as snap:
        found = job_time.job_time(snap, books, noon)
    assert found is not None
    days = found["by_company"]["northwind"]
    assert days == {_day(midnight - timedelta(seconds=1)): 1200.0, _day(midnight): 600.0}
    with timesink.snapshot(tmp_unreadable(store)) as snap:
        assert job_time.job_time(snap, books, noon) is None


def tmp_unreadable(store: FakeTimeSink) -> Path:
    """Tmp unreadable."""
    return store.path.parent / "absent.sqlite"


def test_a_missing_store_gives_an_empty_time_column(tmp_path: Path, jev: _Jev) -> None:
    """A missing store gives an empty time column."""
    h = _job_harness(tmp_path, jev, tmp_path / "absent.sqlite")
    h.job.poll_once()
    reply = h.client.get("/inherent/jobs").json()
    assert reply["job_site_other_s"] == 0
    assert all(g["time_spent"] == [] and g["time_total_s"] == 0 for g in reply["ledger"])
