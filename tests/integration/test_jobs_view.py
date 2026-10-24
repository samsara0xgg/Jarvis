"""ADR 0367: what the ``jobs`` tool reads from job mail, before the tool is put over it.

``JobMail.jobs_view`` is the Jobs page's applications with the time on each company's site, the
applications he hid, and how fresh the mail is: when this process started, when the mailbox was
last listed and, if reading has failed since, when it first did. ``submitted_count`` is the number
the state block states in its submitted line.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.tools import ToolError
from jarvis.state import device_reads
from jarvis.state.job_ledger import hidden_applications
from jarvis.state.ledger import LedgerSources, job_hunt_text, submitted_count
from tests.support import job_mail as job_mail_support
from tests.support.job_mail import (
    NOW,
    OWNER_ZONE,
    fixed_zh_words,  # noqa: F401 - autouse, found by name
    job_harness,
    only_rows,
)
from tests.support.ledger import write_mail
from tests.support.timesink_moment import CHROME, FakeTimeSink

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tests.support.job_mail import JobJev

jev = job_mail_support.jev  # the fixture, found by name in this module


def test_the_view_says_when_the_mailbox_was_last_read_and_since_when_reading_fails(
    tmp_path: Path, jev: JobJev,
) -> None:
    """Started, then read, then failing: each time is the process's own, in UTC text."""
    h = job_harness(tmp_path, jev, only_rows("receipt"))
    started = h.job.jobs_view()["mail"]
    assert started == {"started": NOW.isoformat(), "read": None, "failing_since": None}

    h.clock.advance(60)
    h.job.poll_once()
    read = h.job.jobs_view()["mail"]
    assert read["read"] == (NOW + timedelta(seconds=60)).isoformat()
    assert read["failing_since"] is None

    h.clock.advance(300)
    h.gmail.search_failures = 2
    for _ in range(2):
        with pytest.raises(ToolError):
            h.job.poll_once()
        h.clock.advance(300)
    failing = h.job.jobs_view()["mail"]
    assert failing["read"] == read["read"]  # the last time it did read is kept
    assert failing["failing_since"] == (NOW + timedelta(seconds=360)).isoformat()

    h.job.poll_once()
    assert h.job.jobs_view()["mail"]["failing_since"] is None


def test_the_view_is_the_pages_applications_with_the_time_on_each_site_or_none(
    tmp_path: Path, jev: JobJev,
) -> None:
    """``site_s`` is seconds on the company's site, and None (never 0) when no store answered."""
    h = job_harness(tmp_path, jev, [])
    write_mail(h.db, "a", NOW, kind="receipt", company="Acme Robotics", role="Analyst",
               sender_domain="acme-robotics.example", subject="Thanks for applying")
    served = h.client.get("/inherent/jobs").json()
    view = h.job.jobs_view()
    assert [a["id"] for a in view["applications"]] == [a["id"] for a in served["applications"]]
    assert [a["site_s"] for a in view["applications"]] == [None]

    store = FakeTimeSink(tmp_path)
    store.span(900, 300, CHROME, "Google Chrome", domain="careers.acme-robotics.example",
               title="Apply", url="https://careers.acme-robotics.example/jobs/1")
    h.job.timesink_path = store.path
    assert [a["site_s"] for a in h.job.jobs_view()["applications"]] == [600]
    h.job.timesink_path = tmp_path / "missing.sqlite"
    assert [a["site_s"] for a in h.job.jobs_view()["applications"]] == [None]


def test_a_terminal_that_is_not_there_is_the_views_note(tmp_path: Path, jev: JobJev) -> None:
    """On a brain with no terminal the time is unknown and the view carries why."""
    h = job_harness(tmp_path, jev, [])
    write_mail(h.db, "a", NOW, kind="receipt", company="Acme Robotics",
               sender_domain="acme-robotics.example", subject="Thanks for applying")

    def gone(_op: str, _args: Mapping[str, Any], _timeout: str | None) -> dict[str, Any]:
        raise device_reads.DeviceUnavailable(device_reads.NOT_CONNECTED)

    h.job.timesink_path, h.job.device = None, gone
    view = h.job.jobs_view()
    assert view["time_note"] == device_reads.NOT_CONNECTED
    assert [a["site_s"] for a in view["applications"]] == [None]


def test_the_hidden_applications_are_listed_with_their_id_company_and_role(
    tmp_path: Path, jev: JobJev,
) -> None:
    """A row he hid leaves the page's list and is in ``hidden``, so an update can restore it."""
    h = job_harness(tmp_path, jev, [])
    write_mail(h.db, "a", NOW, kind="receipt", company="Acme Robotics", role="Analyst",
               sender_domain="acme-robotics.example", subject="Thanks for applying")
    own = h.job.add_application({"company": "Delta Corp", "role": "QA", "source": "said"})
    assert h.job.jobs_view()["hidden"] == []
    h.job.edit_application(own, {"hidden": True})
    view = h.job.jobs_view()
    assert [a["company"] for a in view["applications"]] == ["Acme Robotics"]
    assert view["hidden"] == [{"id": own, "company": "Delta Corp", "role": "QA"}]
    assert hidden_applications(h.db, NOW) == view["hidden"]
    h.job.edit_application(own, {"hidden": False})
    assert h.job.jobs_view()["hidden"] == []


def test_the_submitted_count_is_the_state_blocks_own_number(tmp_path: Path, jev: JobJev) -> None:
    """The tool states the very "at least N" block D states, from mail and from his own word."""
    h = job_harness(tmp_path, jev, [])
    for key, company in (("a", "Acme Robotics"), ("b", "Orbit Labs")):
        write_mail(h.db, key, NOW - timedelta(days=3), kind="receipt", company=company,
                   sender_domain=f"{key}.example", subject="Thanks for applying")
    h.job.add_application({"company": "Delta Corp", "source": "said"})
    seen = (NOW - timedelta(days=3)).astimezone(OWNER_ZONE).date().isoformat()
    h.job.add_application(  # the same application as the mail, told the day it was sent
        {"company": "acme robotics", "applied_at": seen, "source": "said"},
    )
    sources = LedgerSources(h.db, tmp_path / "events.db", None, OWNER_ZONE)
    now = h.clock.now()
    stated = re.search(r"Applications submitted: at least (\d+)", job_hunt_text(sources, now))
    assert stated is not None
    assert submitted_count(sources, now) == int(stated[1]) == 3
