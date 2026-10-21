"""A TimeSink store in a tmp dir, and the job-mail harness with a moment read over it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.decision import moment as rules
from jarvis.runtime.moment import Moment, MomentSettings
from tests.support.job_mail import NOW, JobHarness, JobJev, job_harness

if TYPE_CHECKING:
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


CHROME = "com.google.Chrome"


T0 = NOW
"""The instant spans are placed against: the one the job mail harness believes it is."""


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


def moment_for(
    store: FakeTimeSink | Path, db: Path, fields: dict[str, bool] | None = None
) -> Moment:
    path = store.path if isinstance(store, FakeTimeSink) else store
    moment = Moment(MomentSettings(fields or dict.fromkeys(rules.FIELDS, True)), path, db)
    moment.now = lambda: T0
    return moment


def moment_harness(
    tmp_path: Path,
    jev: JobJev,
    moment_store: FakeTimeSink | Path | None,
    **settings: Any,  # noqa: ANN401
) -> JobHarness:
    """The job-mail harness with a moment (and the ledger's time column) over ``moment_store``."""
    h = job_harness(tmp_path, jev, **settings)
    if moment_store is not None:
        h.job.moment = moment_for(moment_store, h.db)
        h.job.timesink_path = h.job.moment._path  # noqa: SLF001 - the same store
    return h
