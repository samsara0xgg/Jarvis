"""L3 commitment mail (ADR 0274): does a letter hold something he has to go to, and what.

Two parts, both pure. :func:`may_hold_time` is the cheap local filter: a letter with no date, no
clock time, no weekday and no "tomorrow" in it cannot hold an appointment, so the model is never
asked. :func:`build_prompt` and :func:`parse_actions` are the one model question and the reading
of its answer: the answer is untrusted, every field is checked, and an action that does not
parse is dropped, never repaired. The caller owns the Gmail reads, the model call and the log.

Layer rules: stdlib + ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import tzinfo

BODY_CHARS: Final[int] = 3000
ACTIONS: Final[tuple[str, ...]] = ("new", "update", "cancel")
_TITLE_CHARS: Final[int] = 120
_PLACE_CHARS: Final[int] = 120
_LINK_CHARS: Final[int] = 300
_EVIDENCE_CHARS: Final[int] = 200
_SKIPS: Final[int] = 60
_YEAR: Final[timedelta] = timedelta(days=366)
_NEAR_DAYS: Final[int] = 14

_MONTHS: Final[str] = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAYS: Final[str] = (
    r"(?:monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri"
    r"|saturday|sat|sunday|sun)"
)
# Generous on purpose: a letter this lets through costs one model call, a letter it holds back is
# missed for good. Every pattern is a sign that a date or an hour is in the text.
_SIGNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b",  # Oct 21, October 21st
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b",  # 21 Oct, 21st of October
        r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b",  # 2026-10-21
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",  # 10/21, 21/10/2026
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?",  # 10月21日
        r"\d{1,2}\s*[日号]",  # 21日
        r"\b\d{1,2}:\d{2}\b",  # 18:30, 6:30 PM
        r"\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])",  # 6pm, 6 p.m.
        r"[零一二三四五六七八九十两\d]{1,3}\s*[点點时時]",  # 三点, 下午3点
        r"[上下]午|早上|晚上|中午|凌晨|傍晚",
        rf"\b{_DAYS}\b",
        r"[周週][一二三四五六日天末]|星期[一二三四五六日天]|礼拜[一二三四五六日天]|周末",
        r"\btomorrow\b|\btonight\b|\bnoon\b|\bmidnight\b",
        r"明天|后天|今晚|明早|明晚",
    )
)


def may_hold_time(subject: str, body: str) -> bool:
    """Whether the letter shows any sign of a date or an hour; False means no model call."""
    text = f"{subject}\n{body}"
    return any(sign.search(text) for sign in _SIGNS)


@dataclass(frozen=True)
class Mail:
    """One letter as the model sees it."""

    sender_name: str
    sender_domain: str
    subject: str
    received_at: str
    body: str


@dataclass(frozen=True)
class Known:
    """A live commitment the model may update or cancel."""

    commitment_id: str
    title: str
    start_at: str
    weekly_until: str
    sender_domain: str = ""


@dataclass(frozen=True)
class Action:
    """One checked action: ``fields`` holds only the record fields the model gave, validated."""

    action: str
    commitment_id: str
    fields: dict[str, Any]


def related(known: Sequence[Known], domain: str, at: datetime) -> list[Known]:
    """The commitments from the same sender domain, plus any starting within 14 days of ``at``."""
    near = timedelta(days=_NEAR_DAYS)
    out = []
    for one in known:
        try:
            start = datetime.fromisoformat(one.start_at)
        except ValueError:
            continue
        if one.sender_domain == domain or abs(start - at) <= near:
            out.append(one)
    return out


_SYSTEM: Final[str] = """\
You read one email sent to Allen, a university student in Victoria, BC, and say whether it makes \
him go somewhere or be somewhere at a time. The email may be in Chinese, English or both. Answer \
with a JSON array only, no other text. An email that makes him go nowhere is [].

Count only what he is registered for, invited to personally, has booked or is obliged to attend: \
a class or a group he joined, an appointment, an exam, a government or school deadline with a \
time, an interview that is not from a job application. Marketing, newsletters, webinars he did \
not sign up for, and other people's events are [].

Each action is an object:
{"action": "new" | "update" | "cancel",
 "commitment_id": <only for update and cancel: one id from the known list>,
 "title": short, in the words of the email,
 "start_at": "YYYY-MM-DDTHH:MM:SS+HH:MM" local time of the first time, with the offset,
 "end_at": same form or omit,
 "weekly_until": "YYYY-MM-DD" the last day of a weekly series, or omit,
 "skip_dates": ["YYYY-MM-DD", ...] days of a series that do not happen, or omit,
 "place": where, or omit, "link": the join link, or omit,
 "private": true or false,
 "evidence": the one line of the email that gives the time, at most 200 characters}

Rules:
- A weekly series is ONE record with weekly_until, never one record per week.
- A reminder email for something in the known list is "update" (when the time, place or link \
changed) or nothing at all, never "new".
- A cancelled or called-off event that is in the known list is "cancel".
- "private" is true for counselling, therapy, anger or mental-health groups, legal and medical \
matters; false for the rest.
- Work out dates from the received time and today's date. If the email gives no year, take the \
next such date. If you cannot tell the time, answer [].
"""


def build_prompt(mail: Mail, known: Sequence[Known], *, zone: str, today: date) -> tuple[str, str]:
    """``(system, user)``: the rules, then the letter with his zone, today and the known list."""
    listed = [
        {"commitment_id": k.commitment_id, "title": k.title, "start_at": k.start_at,
         "weekly_until": k.weekly_until}
        for k in known
    ]
    user = "\n".join((
        f"His time zone: {zone}. Today: {today.isoformat()}.",
        f"Known commitments: {json.dumps(listed, ensure_ascii=False)}",
        f"From: {mail.sender_name} <{mail.sender_domain}>",
        f"Subject: {mail.subject}",
        f"Received: {mail.received_at}",
        "Body:",
        mail.body[:BODY_CHARS],
    ))
    return _SYSTEM, user


def _array(text: str) -> list[Any]:
    """The first JSON array in the model's text (a code fence around it is fine); else none."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        found = json.loads(text[start : end + 1])
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def _time(value: object, zone: tzinfo) -> datetime | None:
    """An ISO time; one with no offset is read in his zone."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return at.replace(tzinfo=zone) if at.tzinfo is None else at


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _text(value: object, limit: int) -> str | None:
    """A line of at most ``limit`` characters; None when it is not text."""
    return None if not isinstance(value, str) else " ".join(value.split())[:limit]


def _fields(raw: dict[str, Any], zone: tzinfo, today: date) -> dict[str, Any] | None:  # noqa: C901, PLR0911, PLR0912 - one check per field
    """The record fields the object gives, each checked; None when any one given is bad."""
    out: dict[str, Any] = {}
    if "title" in raw:
        title = _text(raw["title"], _TITLE_CHARS)
        if not title:
            return None
        out["title"] = title
    start = None
    if "start_at" in raw:
        start = _time(raw["start_at"], zone)
        if start is None or abs(start.date() - today) > _YEAR:
            return None
        out["start_at"] = start.isoformat(timespec="seconds")
    if raw.get("end_at"):
        end = _time(raw["end_at"], zone)
        if end is None or (start is not None and end <= start):
            return None
        out["end_at"] = end.isoformat(timespec="seconds")
    if raw.get("weekly_until"):
        until = _day(raw["weekly_until"])
        if until is None or abs(until - today) > _YEAR or (start and until < start.date()):
            return None
        out["weekly_until"] = until.isoformat()
    if raw.get("skip_dates"):
        skips = raw["skip_dates"]
        days = [_day(d) for d in skips] if isinstance(skips, list) and len(skips) <= _SKIPS else []
        if not days or None in days:
            return None
        out["skip_dates"] = sorted({d.isoformat() for d in days if d})
    for name, limit in (("place", _PLACE_CHARS), ("link", _LINK_CHARS)):
        if raw.get(name):
            line = _text(raw[name], limit)
            if line is None or (name == "link" and not line.startswith(("https://", "http://"))):
                return None
            out[name] = line
    if "private" in raw:
        if not isinstance(raw["private"], bool):
            return None
        out["private"] = raw["private"]
    if "evidence" in raw:
        evidence = _text(raw["evidence"], _EVIDENCE_CHARS)
        if evidence is None:
            return None
        out["evidence"] = evidence
    return out


def parse_actions(
    text: str, known_ids: set[str], *, zone: tzinfo, today: date,
) -> list[Action]:
    """The valid actions in the model's answer, in order; the rest are dropped.

    A ``new`` needs a title and a start; an ``update`` or ``cancel`` needs the id of a known
    commitment. Fields the model gave that are bad drop the whole action.
    """
    actions: list[Action] = []
    for raw in _array(text):
        if not isinstance(raw, dict) or raw.get("action") not in ACTIONS:
            continue
        kind = str(raw["action"])
        cid = raw.get("commitment_id")
        if kind == "new":
            fields = _fields(raw, zone, today)
            if fields is None or "title" not in fields or "start_at" not in fields:
                continue
            actions.append(Action("new", "", fields))
        elif isinstance(cid, str) and cid in known_ids:
            fields = {} if kind == "cancel" else _fields(raw, zone, today)
            if fields is None or (kind == "update" and not fields):
                continue
            actions.append(Action(kind, cid, fields))
    return actions
