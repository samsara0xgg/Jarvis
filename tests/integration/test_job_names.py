"""One list of hiring platforms, read by the decision layer and the state layer alike (ADR 0367).

A platform's name is the sender of a confirmation, not the employer: the letter reader, the
applications the Jobs page lists and the state block's job hunt all have to agree on which names
those are, so a name one layer learns is a name every layer knows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision import job_mail as triage
from jarvis.shared.job_names import employer_of, is_platform, is_tracking_system
from jarvis.state.job_ledger import list_applications
from tests.support.ledger import NOW, ledger_at, write_mail

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Workable", "tracking system"),
        ("workday", "tracking system"),
        ("Smart Recruiters", "tracking system"),
        ("greenhouse-mail", "tracking system"),
        ("LinkedIn", "job board"),
        ("Indeed", "job board"),
        ("Acme Robotics", "employer"),
        ("", "employer"),
    ],
)
def test_a_platform_is_told_from_an_employer_by_one_list(name: str, kind: str) -> None:
    """Case, spaces and hyphens do not matter; a job board is a platform, not a tracking system."""
    told = (
        "tracking system" if is_tracking_system(name)
        else "job board" if is_platform(name)
        else "employer"
    )
    assert told == kind


def test_the_letter_reader_takes_the_employer_from_every_tracking_systems_subject() -> None:
    """Names the state layer always knew (Workable, Taleo, Jobvite, Workday) read like the rest."""
    subject = "Thank you for applying to Acme Robotics"
    for domain in (
        "app.workable.com",
        "acme.taleo.net",
        "hire.jobvite.com",
        "workday.com",
        "us.greenhouse-mail.io",
    ):
        assert triage.company_of("Pat Lee", domain, subject) == "Acme Robotics", domain


def test_the_page_joins_any_tracking_systems_mail_to_the_employer_with_the_same_role(
    tmp_path: Path,
) -> None:
    """A Workable mail names Workable; the Jobs page puts it with the Acme application it is for."""
    memory = tmp_path / "memory.db"
    write_mail(
        memory, "m1", ledger_at(5, 9), kind="receipt", company="Acme Robotics", role="Data Co-op",
    )
    write_mail(
        memory, "m2", ledger_at(6, 9), kind="interview", company="Workable", role="Data Co-op",
    )
    (acme,) = list_applications(memory, NOW)
    assert (acme["company"], acme["status"], acme["count"]) == ("Acme Robotics", "interviewing", 2)


def test_a_platforms_subject_names_the_employer_in_the_spelling_its_own_mail_uses() -> None:
    """A subject's "Acme" meets "Acme Inc" as the employer's mail spells it; no name, none."""
    known = {"acme": "Acme Inc"}
    assert employer_of("Workable", "Thanks for applying to Acme!", known) == "Acme Inc"
    assert employer_of("Workable", "Thanks for applying to Orbit Co.", known) == "Orbit Co"
    assert employer_of("Workable", "Your weekly digest", known) == ""
    assert employer_of("Acme Inc", "Your weekly digest", known) == "Acme Inc"


def test_the_page_puts_a_platforms_confirmation_under_the_employer_its_subject_names(
    tmp_path: Path,
) -> None:
    """Workable's "Thanks for applying to Acme" is Acme's application, as Acme's mail spells it."""
    memory = tmp_path / "memory.db"
    write_mail(memory, "m1", ledger_at(5, 9), kind="rejection", company="Acme Inc", role="Analyst")
    write_mail(
        memory, "m2", ledger_at(6, 9), kind="receipt", company="Workable",
        subject="Thanks for applying to Acme!",
    )
    write_mail(
        memory, "m3", ledger_at(7, 9), kind="receipt", company="Workable",
        subject="Thanks for applying to Orbit Co.",
    )
    write_mail(memory, "m4", ledger_at(7, 10), kind="job_other", company="Workable", subject="Hi")
    by_company = {a["company"]: a for a in list_applications(memory, NOW)}
    assert sorted(by_company) == ["Acme Inc", "Orbit Co", "Workable"]
    assert by_company["Acme Inc"]["count"] == 2
    assert [m["message_id"] for m in by_company["Orbit Co"]["mails"]] == ["m3"]
    assert [m["message_id"] for m in by_company["Workable"]["mails"]] == ["m4"]
