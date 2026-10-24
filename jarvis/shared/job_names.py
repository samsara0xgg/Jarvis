"""Who a job mail is about: the hiring platforms, and the employer a subject names (ADR 0367).

A hiring platform sends mail for an employer, so a sender's name there is the platform's, not the
employer's. The decision layer reading a letter, the applications the Jobs page lists and the
state block's job hunt all tell a platform from an employer here, from one list, so they cannot
drift apart.

Layer rules: stdlib only.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# Applicant-tracking systems: they send for the employer, so their domain is not the company.
# Each name is written with its case, spaces and hyphens taken out, as :func:`is_tracking_system`
# compares (a ``greenhouse-mail`` domain label, a "Smart Recruiters" sender).
TRACKING_SYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "ashby",
        "ashbyhq",
        "bamboohr",
        "clearco",
        "clearcompany",
        "greenhouse",
        "greenhousemail",
        "icims",
        "jobvite",
        "lever",
        "myworkday",
        "myworkdayjobs",
        "njoyn",
        "slfworkdaynotifications",
        "smartrecruiters",
        "taleo",
        "workable",
        "workday",
    },
)
# Job boards name themselves as the sender too, but they are not tracking systems: LinkedIn's
# own mail is read by its domain (ADR 0177), and a board's mail is never moved into an employer's
# application by its role.
JOB_BOARDS: Final[frozenset[str]] = frozenset({"indeed", "linkedin"})

_SUBJECT_COMPANY: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"thanks for applying to (.+?)(?:[.!]|$)",
        r"thank you for applying to (.+?)(?:[.!]|$)",
        r"thank you for your interest in (.+?)(?:[.!]|$)",
        r"applying at (.+?)(?:[.!]|$)",
        r"your application to (.+?)(?:[.!]|$)",
    )
)
_FILLER: Final[re.Pattern[str]] = re.compile(r"\b(the|inc|ltd|group|technologies|technology)\b")


def _plain(name: str) -> str:
    return re.sub(r"[\s-]", "", name).casefold()


def is_tracking_system(name: str) -> bool:
    """Whether a sender, company or domain label is only an applicant-tracking system's name."""
    return _plain(name) in TRACKING_SYSTEMS


def is_platform(name: str) -> bool:
    """Whether a name is a tracking system's or a job board's: a sender that is no employer."""
    plain = _plain(name)
    return plain in TRACKING_SYSTEMS or plain in JOB_BOARDS


def company_key(name: str) -> str:
    """A company's name with its filler words and punctuation taken out, for matching."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", _FILLER.sub(" ", name.lower()))
    return " ".join(cleaned.split())


def employer_of(company: str, subject: str, known: Mapping[str, str]) -> str:
    """The employer a mail is about: a platform's mail names the platform, so read the subject.

    ``known`` maps :func:`company_key` to the way the mails of the employers themselves spell
    each name, so a subject that says "Acme" meets the application "Acme Inc". A platform's mail
    whose subject names no employer has none: ``""``.
    """
    if not is_platform(company):
        return company
    for pattern in _SUBJECT_COMPANY:
        found = pattern.search(subject)
        if found:
            name = found.group(1).strip(" .!")
            return known.get(company_key(name), name)
    return ""
