"""Daily work report request, reply, check and composition (ADR 0028): the skill's model calls.

The skill's instructions are the system prompt; the day's evidence is keyed
material served whole; for three rounds the model may search the day and ask
for originals, then it must draft the report through ``report_daily_work``.
A ``completed`` part in that draft is a claim: the program rules on what it
can (no refs, old commits, a question for a confirmation, a commit for a
deployment), one verification call per item reads the cited originals and
rules on the rest, and the saved wording and the served summary are built
from those rulings, never from the draft alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

from jarvis.shared.lang import TEXT, language_name, t
from jarvis.shared.skills import load_skill
from jarvis.state.daily_report import MAX_DETAILS, MAX_HITS, is_question, summary_section

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from jarvis.decision.llm import ChatResult
    from jarvis.state.daily_report import DayEvidence

SKILL = load_skill("daily-work-report")
REPORT_TOOL_NAME = "report_daily_work"
DETAILS_TOOL_NAME = "request_details"
SEARCH_TOOL_NAME = "search_material"
JUDGE_TOOL_NAME = "judge_claims"
SUMMARY_TOOL_NAME = "write_summary"
QUERY_MORE = (
    "Those are the query results. You may query again (search_material and request_details"
    " can be called together), or report now with report_daily_work."
)
"""Served after a query round while rounds remain."""
REPORT_NOW = (
    "Those are the query results. The query rounds are used up; call report_daily_work now"
    " to report the day. Anything you could not check is not completed: write it as"
    " attempted and note it in uncertainties."
)
"""Served after the last query round: the model reports what it has, not what it guesses."""
REPORT_AGAIN = (
    "The last report could not be parsed. Call report_daily_work again following the"
    " schema; it may be shorter."
)
"""Served after an unusable reply: providers malform or truncate long arguments now and then."""
STATUSES = ("browsed", "discussed", "attempted", "completed")
ASSERTS = ("merged", "deployed", "other")
"""What a completed part claims happened; the merge and deployment rules key on it."""
CONTENT_LIMIT = 44000
"""Reports exceeding this budget fail without saving or dropping their citation index."""
MAX_SOURCE_REFS = 20
VERDICTS = ("supported", "partial", "unsupported")
_REST = ("attempted", "discussed", "browsed")
"""The unchecked statuses counted at the foot of the summary, highest first."""
_MAX_ITEMS = 12
_MAX_PARTS = 4
"""Parts of one item with a status each: code, tests, deployment, an application."""
_MAX_DECISIONS = 8
_MAX_OPEN = 10
_MAX_NEXT = 8
_PLAN_TODOS = 30
"""Open (and completed) To Do lines written into the report; the material carries them all."""
_MAX_SUGGESTIONS = 6
_MAX_UNCERTAINTIES = 10
_TITLE = 80
_PART = 24
"""Part names the model writes run to a clause (2026-09-19: 执行层重构与 Codex 迁移)."""
_TEXT = 400
_SHORT = 240
_SHOWS = 120
_MAIN_LINE = 200
_SERVED = 3000
"""``summary_of`` serves the whole summary section, digest included, not just the prose."""
_SHA = re.compile(r"(?<![0-9a-zA-Z])(?=[0-9]*[a-f])[0-9a-f]{7,40}(?![0-9a-zA-Z])")
"""A commit number in prose: at least one hex letter, so a phone number or an id is not one."""
_COMPLETION_WORDS = re.compile(
    r"完成|成功|已经|已部署|已合并|已上线|已重启|部署了|合并了|上线了|重启了|通过|做完|修好|搞定"
    r"|done|deployed|merged|passed|shipped|fixed",
    re.IGNORECASE,
)
"""What the model's one summary sentence may not assert: completion is stated by the table."""
_TITLE_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[一-鿿]{2,}")

JUDGE_SYSTEM = (
    "You are a checker. You get one item of a work report, the parts it claims are done, and"
    " the originals the author cites. Judge each part from the originals only: supported ="
    " the originals really show this part (the same thing, the same scope) done or its"
    " product existing; partial = the originals show only some of it done, or a smaller"
    " scope; unsupported = the originals are unrelated to this part, only a plan / question"
    " / discussion, or show an attempt rather than completion. A commit proves only that a"
    " change was committed, not tests, a deployment or a merge; when an agent (Codex, Claude)"
    ' says "done", "deployed" or "tests passed", that is its own account, not a'
    " verified result — when the originals hold only such words, say in shows that it is the"
    " agent's own account. shows is one sentence on what the originals actually show, written"
    " in {language}. The screen field: when the original is screen text, page (a third-party"
    " web or app page, such as an application confirmation page) or agent (the agent's own"
    " words in a terminal / Codex / ChatGPT); null when it is not screen text. Answer by"
    " calling judge_claims, one entry per part."
)
SUMMARY_SYSTEM = (
    "You write the daily report. Below is the day's list of items after checking; each"
    " item's status text is the check's finding and cannot change. In one sentence (at most"
    " 200 characters), in {language}, write the day's main line: what kinds of work were done"
    " and around what. Describe only the main line; never say anything was done, deployed,"
    " merged or passed — whether it was is stated by the list. Answer by calling"
    " write_summary."
)


def _claim_schema(*fields: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    properties = dict(fields)
    properties["refs"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": properties,
        "required": [*properties],
    }


REPORT_TOOL: dict[str, Any] = {
    "name": REPORT_TOOL_NAME,
    "description": (
        "Report the draft of the day's work report; the fields are described in the report"
        " format of the system instructions. The summary is written by the runtime after its"
        " checks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "description": "Activities, products and progress, merged by work item.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": f"At most {_TITLE} characters.",
                        },
                        "activity": {
                            "type": "string",
                            "description": (
                                f"At most {_TEXT} characters; longer text is cut at a"
                                " sentence end."
                            ),
                        },
                        "progress": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": _MAX_PARTS,
                            "description": (
                                "The item's progress, split by part: code, tests,"
                                " deployment, an application and so on, one entry each"
                                " with its own status and refs; a single-part item has"
                                " one entry with part null. completed is your claim; the"
                                " runtime checks it against the originals in refs."
                            ),
                            "items": _claim_schema(
                                (
                                    "part",
                                    {
                                        "type": ["string", "null"],
                                        "description": (
                                            f"The part's name, at most {_PART} characters;"
                                            " null for a single-part item."
                                        ),
                                    },
                                ),
                                ("status", {"type": "string", "enum": list(STATUSES)}),
                                (
                                    "asserts",
                                    {
                                        "type": "string",
                                        "enum": list(ASSERTS),
                                        "description": (
                                            "What a completed part claims happened: merged"
                                            " = merged into main; deployed = deployed,"
                                            " released, published or restarted into"
                                            " effect; other = anything else (code written"
                                            " or committed, an application sent). other"
                                            " when not completed."
                                        ),
                                    },
                                ),
                            ),
                        },
                    },
                    "required": ["title", "activity", "progress"],
                },
            },
            "decisions": {
                "type": "array",
                "maxItems": _MAX_DECISIONS,
                "description": (
                    "Important decisions and changes of plan; rationale only with grounds,"
                    " otherwise null."
                ),
                "items": _claim_schema(
                    ("text", {"type": "string", "description": f"At most {_TEXT} characters."}),
                    (
                        "rationale",
                        {
                            "type": ["string", "null"],
                            "description": f"At most {_SHORT} characters.",
                        },
                    ),
                ),
            },
            "open_items": {
                "type": "array",
                "maxItems": _MAX_OPEN,
                "description": "Unfinished items, blockers and questions to confirm.",
                "items": _claim_schema(("text", {"type": "string"})),
            },
            "user_next_steps": {
                "type": "array",
                "maxItems": _MAX_NEXT,
                "description": (
                    "Next steps the user stated outright; refs must include a record key"
                    " with who=allen."
                ),
                "items": _claim_schema(("text", {"type": "string"})),
            },
            "suggestions": {
                "type": "array",
                "maxItems": _MAX_SUGGESTIONS,
                "items": {"type": "string"},
                "description": "Your own suggestions, kept apart from the user's commitments.",
            },
            "uncertainties": {
                "type": "array",
                "maxItems": _MAX_UNCERTAINTIES,
                "items": {"type": "string"},
                "description": "Uncertainties, conflicting evidence, gaps in the material.",
            },
        },
        "required": [
            "items",
            "decisions",
            "open_items",
            "user_next_steps",
            "suggestions",
            "uncertainties",
        ],
    },
}
DETAILS_TOOL: dict[str, Any] = {
    "name": DETAILS_TOOL_NAME,
    "description": (
        f"Fetch the whole original of up to {MAX_DETAILS} entries (full screen OCR, the"
        " conversation's own words, a commit's content and changed files, a Codex session"
        " turn by turn), by the bracketed keys in the material or in search results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_DETAILS},
        },
        "required": ["keys"],
    },
}
SEARCH_TOOL: dict[str, Any] = {
    "name": SEARCH_TOOL_NAME,
    "description": (
        "Search all of the day's material by keywords (full screen text, window titles,"
        " conversation records, commit subjects, full Codex sessions); returns the matching"
        f" keys with one line of context, at most {MAX_HITS}. Use it to check whether a fact"
        " (a commit, a sentence, a page, an error) really appeared that day, and where. It can"
        " be called in the same round as request_details."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Keywords or a phrase"}},
        "required": ["query"],
    },
}
JUDGE_TOOL: dict[str, Any] = {
    "name": JUDGE_TOOL_NAME,
    "description": "Give the finding for each part claimed done.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "part": {
                            "type": "integer",
                            "description": "The part's number, in the order given.",
                        },
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "shows": {
                            "type": "string",
                            "description": (
                                f"What the originals actually show, at most {_SHOWS}"
                                " characters."
                            ),
                        },
                        "screen": {
                            "type": ["string", "null"],
                            "enum": ["page", "agent", None],
                            "description": (
                                "Whether screen text comes from a third-party page (page)"
                                " or is the agent's own account (agent)."
                            ),
                        },
                    },
                    "required": ["part", "verdict", "shows", "screen"],
                },
            }
        },
        "required": ["verdicts"],
    },
}
SUMMARY_TOOL: dict[str, Any] = {
    "name": SUMMARY_TOOL_NAME,
    "description": "The day's main line, in one sentence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "main_line": {
                "type": "string",
                "description": f"At most {_MAIN_LINE} characters.",
            }
        },
        "required": ["main_line"],
    },
}


class DailyReportParseError(ValueError):
    """The model's reply was not a usable report."""


def _lines(section: str, rows: list[dict[str, Any]], render: str) -> list[str]:
    if not rows:
        return []
    return [f"## {section}", *(render.format(**row) for row in rows), ""]


def _session_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    out = [
        "## Agent sessions (local Codex session files, turn by turn in full; when an agent says"
        ' "done" or "merged" that is its own account, not a verified result; Claude Code'
        " sessions are not included)"
    ]
    for row in rows:
        out.append(f"[{row['key']}] {row['first']}-{row['last']} {row['app']} @ {row['cwd']}")
        out += [f"  {turn['at']} {turn['role']}: {turn['text']}" for turn in row["turns"]]
    out.append("")
    return out


def _next_day(evidence: DayEvidence) -> str:
    return (date.fromisoformat(evidence.day) + timedelta(days=1)).isoformat()


def _event_line(row: dict[str, Any]) -> str:
    where = t("report.event_where", location=row["location"]) if row["location"] else ""
    return f"- {row['date']} {row['time']} {row['subject']}{where}"


def _todo_line(row: dict[str, Any], today: str) -> str:
    if row["done"]:
        state = t("report.todo.done")
    elif row["due"] is None:
        state = t("report.todo.no_due")
    elif row["due"] < today:
        state = t("report.todo.overdue", due=row["due"])
    else:
        state = t("report.todo.due", due=row["due"])
    important = t("report.todo_important") if row["important"] else ""
    return t("report.todo", title=row["title"], list=row["list"], state=state, important=important)


def plan_lines(evidence: DayEvidence) -> list[str]:
    """ADR 0036: the next day's calendar and open To Do, written by code from Microsoft's read."""
    nxt = _next_day(evidence)
    head = t("report.h.plan", day=nxt)
    if evidence.coverage.get("calendar") != "available":
        return [head, f"- {evidence.served.get('plan') or t('report.plan_unread')}", ""]
    sections = evidence.sections
    events = [row for row in sections["calendar"] if row["date"] == nxt]
    open_ = [row for row in sections["todos"] if not row["done"]]
    done = [row for row in sections["todos"] if row["done"]]
    lines = [head, t("report.calendar"), *(_event_line(row) for row in events)]
    if not events:
        lines.append(t("report.calendar_empty"))
    lines.append(t("report.todos_open", count=len(open_)))
    # ponytail: the saved report has a hard size limit; the rest stay in To Do and the material.
    lines += [_todo_line(row, nxt) for row in open_[:_PLAN_TODOS]]
    if not open_:
        lines.append(t("report.todos_none"))
    if len(open_) > _PLAN_TODOS:
        lines.append(t("report.todos_more", count=len(open_) - _PLAN_TODOS))
    if done:
        lines.append(t("report.todos_done", day=evidence.day))
        lines += [
            t("report.todo_done_line", title=row["title"], list=row["list"])
            for row in done[:_PLAN_TODOS]
        ]
    return [*lines, ""]


def render_material(evidence: DayEvidence) -> str:
    """Render the whole keyed day as the model's material."""
    sections = evidence.sections
    partial = (
        " (the day is not over; the material runs only up to now)"
        if evidence.window["partial"]
        else ""
    )
    out: list[str] = [
        f"Report date: {evidence.day} ({evidence.zone}); "
        f"evidence window {evidence.window['from']} to {evidence.window['to']}{partial}.",
        "## Material coverage (not captured, unreadable, or given in full)",
        *(f"- {text}" for text in evidence.served.values()),
        "",
    ]
    if evidence.limits:
        out += ["## Material scope", *(f"- {limit}" for limit in evidence.limits), ""]
    out += _lines(
        'Key pages on screen (captures matched to phrases such as "submitted / received /'
        ' sent"; the full text is under Screen content)',
        sections.get("milestones", []),
        "[{key}] {first} {app} — {title}: …{text}…",
    )
    out += _lines(
        "Time per app (minutes, TimeSink's estimate; not effective working time)",
        sections.get("apps", []),
        "- {app}: {minutes} ({spans} spans)",
    )
    out += _lines(
        "All windows (app — title, minutes, first-last; in time order)",
        sections.get("windows", []),
        "[{key}] {first}-{last} {app} — {title} ({minutes} min, {spans} spans)",
    )
    out += _lines(
        "Screen content (each capture's full OCR text, in time order; ×n = near-repeat"
        " captures of the same window in a row)",
        sections.get("screen", []),
        "[{key}] {first}-{last} ×{count} {app} — {title}: {text}",
    )
    out += _lines(
        "TimeSink state events (lock / sleep / idle / pause and the like; they explain gaps)",
        sections.get("state_events", []),
        "- {at} {kind} {phase}",
    )
    out += _lines(
        "The day's conversation records (in time order, in full; who=allen is the user's own"
        " words)",
        sections.get("records", []),
        "[{key}] {at} {who}: {text}",
    )
    out += _lines(
        "The day's Git commits (from the local repositories, one commit across worktrees"
        " merged; late=True is an old commit first seen that day, not that day's work)",
        sections.get("git", []),
        "[{key}] committed {committed}, observed {observed}, {sha} {subject} ({paths})"
        " late={late} {main}",
    )
    out += _session_lines(sections.get("agent", []))
    out += _lines(
        "Repository states observed that day",
        sections.get("repo_states", []),
        "[{key}] {at} {repo} branch {branch}, {dirty} files with uncommitted changes, HEAD {head}",
    )
    nxt = _next_day(evidence)
    if sections.get("calendar"):
        out += [
            "## Context: Microsoft Calendar (the report day and the next, local time; a plan,"
            " not evidence of the day's activity)",
            *(_event_line(row) for row in sections["calendar"]),
            "",
        ]
    if sections.get("todos"):
        out += [
            f"## Context: Microsoft To Do (open ones, and those done on the report day; overdue"
            f" counts from {nxt}; not evidence of the day's activity)",
            *(_todo_line(row, nxt) for row in sections["todos"]),
            "",
        ]
    out += _lines(
        "Context: saved knowledge (not the day's activity)",
        sections.get("knowledge", []),
        "[{key}] {kind}: {statement}",
    )
    out += _lines(
        "Context: the previous day's report summary (to explain continuity and change, not"
        " the day's activity)",
        sections.get("previous", []),
        "[{key}] {date}: {text}",
    )
    return "\n".join(out)


def build_request(evidence: DayEvidence) -> tuple[str, list[dict[str, Any]]]:
    """The skill's instructions as the system prompt plus one user message of material."""
    content = (
        f"Here is all the material for {evidence.day}. You may first check facts with"
        " search_material and fetch originals with request_details (up to three rounds),"
        " then report the draft with report_daily_work.\n\n"
        f"{render_material(evidence)}"
    )
    system = f"{SKILL.instructions}\n\nWrite every text field in {language_name()}."
    return system, [{"role": "user", "content": content}]


def requested_queries(result: ChatResult) -> list[tuple[str, str, Any]]:
    """Each search or details call as (call id, tool, argument); empty once the model reported.

    A search carries its query string, a details request its keys, bounded.
    """
    if any(call.name == REPORT_TOOL_NAME for call in result.tool_calls):
        return []
    queries: list[tuple[str, str, Any]] = []
    for call in result.tool_calls:
        if call.name not in (DETAILS_TOOL_NAME, SEARCH_TOOL_NAME):
            continue
        try:
            raw = json.loads(call.arguments_json)
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if call.name == SEARCH_TOOL_NAME:
            queries.append((call.call_id, call.name, str(raw.get("query") or "")))
            continue
        keys = raw.get("keys")
        wanted = [str(k) for k in keys if isinstance(k, str)] if isinstance(keys, list) else []
        queries.append((call.call_id, call.name, wanted[:MAX_DETAILS]))
    return queries


_BREAKS = "。；！？.;!?\n"
"""Sentence ends in either language, where an over-long text is cut."""


def _text(value: Any, limit: int) -> str:  # noqa: ANN401 — model output.
    """One line, at most ``limit`` characters, cut at the last sentence end when too long."""
    flat = " ".join(str(value).split())
    if len(flat) <= limit:
        return flat
    head = flat[: limit - 1]
    stop = max(head.rfind(mark) for mark in _BREAKS)
    if stop >= limit // 2:
        return head[: stop + 1] + "…"
    return head + "…"


def _malformed(what: str) -> NoReturn:
    message = f"report_daily_work reply is malformed: {what}"
    raise DailyReportParseError(message)


def _refs(raw: Any) -> list[str]:  # noqa: ANN401 — model output.
    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
        _malformed("refs is not a list of keys")
    return list(raw)


def _claim(raw: Any, *fields: str) -> dict[str, Any]:  # noqa: ANN401 — model output.
    if isinstance(raw, str) and "title" not in fields:
        # A one-sentence entry sometimes comes back bare; it means the same, with no refs.
        raw = {"text": raw, "refs": []}
    if not isinstance(raw, dict):
        _malformed("entry is not an object")
    claim: dict[str, Any] = {}
    for name in fields:
        value = raw.get(name)
        if name in ("rationale", "part"):
            # Both are optional: a decision without a stated reason, a part of a whole item.
            limit = _SHORT if name == "rationale" else _PART
            claim[name] = _text(value, limit) if isinstance(value, str) and value.strip() else None
            continue
        if name == "status":
            if value not in STATUSES:
                _malformed("status is not browsed/discussed/attempted/completed")
            claim[name] = value
            continue
        if name == "asserts":
            if value is not None and value not in ASSERTS:
                _malformed("asserts is not merged/deployed/other")
            claim[name] = value or "other"
            continue
        if not isinstance(value, str) or not value.strip():
            _malformed(f"{name} is missing")
        claim[name] = _text(value, _TITLE if name == "title" else _TEXT)
    claim["refs"] = _refs(raw.get("refs"))
    return claim


def _item(raw: Any) -> dict[str, Any]:  # noqa: ANN401 — model output.
    """One work item: its title and activity, and each part's status with its own refs.

    A mixed item — code committed, deployment unconfirmed — is one entry per
    part, so no single status can cover development, tests and deployment.
    """
    if not isinstance(raw, dict):
        _malformed("item is not an object")
    item: dict[str, Any] = {}
    for name in ("title", "activity"):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            _malformed(f"{name} is missing")
        item[name] = _text(value, _TITLE if name == "title" else _TEXT)
    progress = raw.get("progress")
    if not isinstance(progress, list) or not progress:
        _malformed("progress is not a non-empty list")
    item["progress"] = [
        _claim(part, "part", "status", "asserts") for part in progress[:_MAX_PARTS]
    ]
    return item


def _texts(raw: Any, limit: int) -> list[str]:  # noqa: ANN401 — model output.
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        _malformed("list is not text")
    return [_text(x, _SHORT) for x in raw if x.strip()][:limit]


def _arguments(result: ChatResult, name: str) -> Any:  # noqa: ANN401 — model output.
    """The JSON arguments of the named tool call, or the bare JSON reply, or None."""
    for call in result.tool_calls:
        if call.name == name:
            try:
                return json.loads(call.arguments_json)
            except ValueError as exc:
                msg = f"{name} arguments are not JSON"
                raise DailyReportParseError(msg) from exc
    if result.text:
        text = result.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def parse_report(result: ChatResult) -> dict[str, Any]:
    """Normalize the draft tool call (or a bare JSON reply) into the report shape.

    Every required field must be present with the schema's type; a partial
    reply raises, so a malformed answer never becomes a saved report.
    """
    raw = _arguments(result, REPORT_TOOL_NAME)
    if not isinstance(raw, dict):
        msg = "model replied without a report_daily_work call"
        raise DailyReportParseError(msg)
    for key in ("items", "decisions", "open_items", "user_next_steps"):
        if not isinstance(raw.get(key), list):
            _malformed(f"{key} is not a list")
    return {
        "items": [_item(x) for x in raw["items"]][:_MAX_ITEMS],
        "decisions": [_claim(x, "text", "rationale") for x in raw["decisions"]][:_MAX_DECISIONS],
        "open_items": [_claim(x, "text") for x in raw["open_items"]][:_MAX_OPEN],
        "user_next_steps": [_claim(x, "text") for x in raw["user_next_steps"]][:_MAX_NEXT],
        "suggestions": _texts(raw.get("suggestions"), _MAX_SUGGESTIONS),
        "uncertainties": _texts(raw.get("uncertainties"), _MAX_UNCERTAINTIES),
    }


def summary_of(content: str) -> str:
    """The summary section of a saved report (either language), for a tool result's preview."""
    body = summary_section(content)
    if body is None:
        return content[:_SERVED]
    return " ".join(body.split())[:_SERVED]


BRIEF_LEAD = 110
"""Characters of the day's main line the home's brief card shows."""
BRIEF_NOTE = 160
"""Characters of one item's activity the brief page shows when a row is opened."""
_BRIEF_SECTIONS = ("items", "open", "next", "decisions", "suggestions")
"""What a morning wants first: the day, then what is left, then what Allen said comes next."""
_REFS_TAIL = re.compile(r"\s*[（(](?:引用|Sources)[：:][^）)]*[）)]\s*$")
_GROUP = re.compile(r"[（(][^（()）]*[）)]")
_SENTENCE_END = "。.!?！？；;"
_NO_ITEM = ("report.no_items", "report.no_decisions", "report.no_open", "report.no_next")
_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("check", ("invalid", "self_report", "unchecked", "unsupported_shows", "unsupported")),
    ("part", ("partial",)),
    ("going", ("report.status.attempted",)),
    ("done", ("merged", "user", "committed", "page", "screen")),
    ("discussed", ("report.status.discussed",)),
    ("browsed", ("report.status.browsed",)),
)
"""A status's kind and the saved wordings that mean it, the first kind found winning: an
item with one part unchecked is unchecked, one with a part still going is not done."""


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a sentence end when one falls in the second half."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [cut.rfind(mark) for mark in _SENTENCE_END]
    if (end := max(ends)) >= limit // 2:
        return cut[: end + 1]
    return cut.rstrip("，,、 ") + "…"


def _status_of(head: str) -> tuple[str, str]:
    """An item heading's title and status, split at the last ``" — "`` outside any brackets."""
    depth, split = 0, -1
    for at, char in enumerate(head):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif depth == 0 and head.startswith(" — ", at):
            split = at
    if split < 0:
        return head, ""
    return head[:split], head[split + 3 :]


def _plain(status: str) -> str:
    """A status without the keys, commits and quotes in its brackets: those are the audit's."""
    while (shorter := _GROUP.sub("", status)) != status:
        status = shorter
    return status.strip()


def _tag_of(status: str) -> str:
    """The kind of a saved status (see ``_TAGS``), or an empty string when it is none of them."""
    for tag, keys in _TAGS:
        for key in keys:
            wordings = TEXT[key if key.startswith("report.") else f"report.claim.{key}"]
            if any(w.partition("{")[0] in status for w in wordings.values()):
                return tag
    return ""


def _brief_items(lines: Sequence[str]) -> list[dict[str, str]]:
    """Each work item as a title, a kind and its short status, and its activity as the note."""
    refs = tuple(text.partition("{")[0] for text in TEXT["report.refs"].values())
    entries: list[list[str]] = []
    for line in lines:
        if line.startswith("### "):
            entries.append([re.sub(r"^\d+\.\s+", "", line[4:]).strip()])
        elif entries and line.strip() and not line.startswith(refs):
            entries[-1].append(line.strip())
    out = []
    for head, *activity in entries:
        title, status = _status_of(head)
        row = {"text": title}
        if tag := _tag_of(status):
            row |= {
                "tag": tag,
                "label": t(f"brief.tag.{tag}"),
                "status": _clip(_plain(status), BRIEF_NOTE // 2),
            }
        if note := _clip(" ".join(" ".join(activity).split()), BRIEF_NOTE):
            row["note"] = note
        out.append(row)
    return out


def _brief_bullets(lines: Sequence[str]) -> list[dict[str, str]]:
    """A bulleted section without its citations and without the line that says it is empty."""
    empty = {
        text.removeprefix("- ")
        for key in (*_NO_ITEM, "report.none")
        for text in TEXT[key].values()
    }
    found = [_REFS_TAIL.sub("", line[2:]).strip() for line in lines if line.startswith("- ")]
    return [{"text": text} for text in found if text and text not in empty]


def brief_of(content: str) -> dict[str, Any]:
    """The home's morning brief from a saved report (either language).

    The saved report is an audit: statuses with commit numbers, a citation line under
    every item, the coverage of the material, the sources. This keeps what a person reads
    over breakfast, as data the page lays out: ``summary`` (the day's main line cut to a
    sentence for the card), ``lead`` (twice as much, for the page), ``items`` (how many
    work items) and ``sections``, each a title and rows of ``text`` with, for work items,
    a ``tag`` (done, check, part, going, discussed, browsed), its ``label`` and plain
    ``status``, and the activity as ``note``. A claim nobody checked keeps saying so in
    its tag.
    """
    names = {
        heading: key
        for key in ("summary", *_BRIEF_SECTIONS)
        for heading in TEXT[f"report.h.{key}"].values()
    }
    found: dict[str, list[str]] = {}
    here: list[str] | None = None
    for line in content.splitlines():
        if line.startswith("## "):
            key = names.get(line.strip())
            here = found.setdefault(key, []) if key and key not in found else None
        elif here is not None:
            here.append(line)
    lead_lines = [ln.strip() for ln in found.get("summary", []) if ln.strip()] or [
        ln.strip() for ln in content.splitlines() if ln.strip() and not ln.startswith("#")
    ]
    lead = " ".join(lead_lines[0].split()) if lead_lines else ""
    rows = {
        key: (_brief_items if key == "items" else _brief_bullets)(found.get(key, []))
        for key in _BRIEF_SECTIONS
    }
    return {
        "summary": _clip(lead, BRIEF_LEAD),
        "lead": _clip(lead, BRIEF_LEAD * 2),
        "items": len(rows["items"]),
        "sections": [
            {"key": key, "title": t(f"brief.h.{key}"), "rows": rows[key]}
            for key in _BRIEF_SECTIONS
            if rows[key]
        ],
    }


# --- claims: what a completed part asserts, what the program rules, what the check found ---


@dataclass
class Claim:
    """One part of one item; a ``completed`` status is a claim until it is checked."""

    item: int
    part: str | None
    status: str
    refs: list[str]
    asserts: str = "other"
    """merged · deployed · other — what the draft says a completed part achieved."""
    kinds: dict[str, str] = field(default_factory=dict)
    """Cited key -> commit / late_commit / user / question / jarvis / screen / agent / other."""
    ruling: str = "unclaimed"
    """unclaimed · check · repo · self_report · invalid — the program's ruling on the claim."""
    reason: str = ""
    verdict: str | None = None
    """supported · partial · unsupported from the check; None while unchecked."""
    shows: str = ""
    screen: str | None = None
    unchecked: str | None = None
    """Why a claim that needed the check never got one: the budget ran out, or the check failed."""

    @property
    def claimed(self) -> bool:
        """Whether the draft called this part completed."""
        return self.status == "completed"

    @property
    def needs_check(self) -> bool:
        """Whether the program left this claim to the verification call and none has ruled yet."""
        return self.ruling == "check" and self.verdict is None and self.unchecked is None


def _kind_of(key: str, evidence: DayEvidence) -> str:
    ref = evidence.refs.get(key)
    if ref is None:
        return "unknown"
    if key in evidence.commit_rows:
        return "commit" if ref in evidence.commits else "late_commit"
    if ref.startswith("record:"):
        if ref not in evidence.stated:
            return "jarvis"
        # A record's haystack line is "HH:MM who: text"; the request test anchors on the text.
        text = evidence.haystack.get(key, "").partition(": ")[2]
        return "question" if is_question(text) else "user"
    prefixes = {"timesink-capture:": "screen", "codex-session:": "agent"}
    return next((kind for prefix, kind in prefixes.items() if ref.startswith(prefix)), "other")


def _rule(claim: Claim, evidence: DayEvidence) -> None:
    """What the program can decide about a completed part before any model reads it."""
    kinds = set(claim.kinds.values())
    evidential = kinds & {"commit", "user", "screen", "agent"}
    if not claim.refs or kinds <= {"unknown"}:
        claim.ruling, claim.reason = "invalid", t("report.reason.no_keys")
        return
    if not evidential:
        if "late_commit" in kinds:
            claim.reason = t("report.reason.late_commit")
        elif "question" in kinds:
            claim.reason = t("report.reason.question")
        else:
            claim.reason = t("report.reason.not_proof")
        claim.ruling = "invalid"
        return
    if claim.asserts == "merged" and "commit" in kinds:
        shas = [evidence.commit_rows[k] for k, kind in claim.kinds.items() if kind == "commit"]
        off = [row["sha"] for row in shas if row["on_main"] is not True]
        if off:
            claim.ruling = "invalid"
            claim.reason = t("report.reason.not_on_main", shas=t("sep.list").join(off))
        else:
            claim.ruling = "repo"
        return
    if claim.asserts == "deployed" and "user" not in kinds:
        if evidential <= {"commit"}:
            claim.ruling, claim.reason = "invalid", t("report.reason.commit_not_deploy")
        else:
            claim.ruling = "self_report"
        return
    claim.ruling = "self_report" if evidential <= {"agent"} else "check"


def screen_claims(report: dict[str, Any], evidence: DayEvidence) -> list[Claim]:
    """Every part of every item as a claim, the completed ones ruled on by the program."""
    claims = []
    for index, item in enumerate(report["items"], 1):
        for part in item["progress"]:
            claim = Claim(
                index, part["part"], part["status"], list(part["refs"]), part["asserts"]
            )
            claim.kinds = {key: _kind_of(key, evidence) for key in claim.refs}
            if claim.claimed:
                _rule(claim, evidence)
            claims.append(claim)
    return claims


def title_terms(title: str) -> list[str]:
    """Words of an item's title that pick a session's relevant turns: 3+ letters or 2+ CJK."""
    return _TITLE_TERM.findall(title)


def build_check_request(
    item: dict[str, Any], claims: list[Claim], originals: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """The verification call for one item: the claims in order, then the cited originals."""
    lines = [
        f"Item: {item['title']}",
        f"The author's activity and progress: {item['activity']}",
        "",
        "Parts claimed done:",
    ]
    for number, claim in enumerate(claims, 1):
        cited = ", ".join(claim.refs) or "nothing"
        lines.append(f"{number}. {claim.part or 'the whole item'} (cites {cited})")
    lines += ["", "The cited originals:"]
    for original in originals:
        lines += [f"[{original['key']}] kind={original['kind']}", original["text"], ""]
    system = JUDGE_SYSTEM.format(language=language_name())
    return system, [{"role": "user", "content": "\n".join(lines)}]


def parse_verdicts(result: ChatResult, claims: list[Claim]) -> None:
    """Write the check's verdicts onto the claims; a missing or malformed one stays unchecked."""
    raw = _arguments(result, JUDGE_TOOL_NAME)
    rows = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        msg = "judge_claims reply is malformed: verdicts is not a list"
        raise DailyReportParseError(msg)
    for row in rows:
        if not isinstance(row, dict) or row.get("verdict") not in VERDICTS:
            continue
        number = row.get("part")
        if not isinstance(number, int) or not 1 <= number <= len(claims):
            continue
        claim = claims[number - 1]
        claim.verdict = str(row["verdict"])
        claim.shows = _text(row.get("shows") or "", _SHOWS)
        screen = row.get("screen")
        claim.screen = screen if screen in ("page", "agent") else None


def build_summary_request(
    evidence: DayEvidence, table: list[str]
) -> tuple[str, list[dict[str, Any]]]:
    """The summary call: the verified table, and nothing else."""
    content = f"The items of {evidence.day}, after checking:\n" + "\n".join(table)
    return SUMMARY_SYSTEM.format(language=language_name()), [{"role": "user", "content": content}]


def parse_main_line(result: ChatResult) -> str | None:
    """The model's one sentence, or None when it asserts completion or is unusable."""
    raw = _arguments(result, SUMMARY_TOOL_NAME)
    line = raw.get("main_line") if isinstance(raw, dict) else None
    if not isinstance(line, str) or not line.strip() or _COMPLETION_WORDS.search(line):
        return None
    return _text(line, _MAIN_LINE)


# --- composition: the saved text, built from the rulings ---


class _Composer:
    """Turn a checked report into the saved text, enforcing the evidence rules."""

    def __init__(self, evidence: DayEvidence) -> None:
        self.evidence = evidence
        self.cited: list[str] = []
        self.unknown = 0
        self._number: dict[str, int] = {}

    def refs(self, keys: list[str]) -> list[int]:
        """Citation numbers into ``cited``; a key the model was never given is counted instead.

        Numbers, not the references themselves: a reference is written out once,
        under the sources heading, so a claim citing it many times cannot grow the report.
        """
        found: list[int] = []
        for key in keys:
            ref = self.evidence.refs.get(key)
            if ref is None:
                self.unknown += 1
                continue
            if ref not in self._number:
                self.cited.append(ref)
                self._number[ref] = len(self.cited)
            if self._number[ref] not in found:
                found.append(self._number[ref])
        return found

    def stated(self, numbers: list[int]) -> bool:
        return any(self.cited[n - 1] in self.evidence.stated for n in numbers)


def _commits_named(claim: Claim, evidence: DayEvidence) -> str:
    rows = [evidence.commit_rows[k] for k, kind in claim.kinds.items() if kind == "commit"]
    if not rows:
        return ""
    mains = {row["main"] for row in rows}
    sep = t("sep.list")
    if len(mains) == 1:
        return t("report.commits", shas=sep.join(row["sha"] for row in rows), main=mains.pop())
    # Mixed: each commit carries its own flag, so a merged one cannot vouch for a branch one.
    commits = sep.join(t("report.commit_main", sha=row["sha"], main=row["main"]) for row in rows)
    return t("report.commits_mixed", commits=commits)


def _keys_of(claim: Claim, kind: str) -> str:
    return t("sep.list").join(k for k, v in claim.kinds.items() if v == kind)


def _settled(claim: Claim) -> str | None:
    """The text a ruling or a non-supporting verdict settles; None once the check supported it."""
    if claim.ruling == "invalid":
        return t("report.claim.invalid", reason=claim.reason)
    if claim.ruling == "self_report":
        cited = _keys_of(claim, "agent") or _keys_of(claim, "screen")
        return t("report.claim.self_report", keys=cited)
    if claim.unchecked:
        return t("report.claim.unchecked", why=claim.unchecked)
    if claim.verdict == "unsupported":
        return t("report.claim.unsupported_shows", shows=claim.shows)
    if claim.verdict == "partial":
        return t("report.claim.partial", shows=claim.shows)
    return None


def wording(claim: Claim, evidence: DayEvidence) -> tuple[str, bool]:
    """The saved status text of a part, and whether it is evidenced (a commit, the user, a page)."""
    if not claim.claimed:
        return t(f"report.status.{claim.status}"), False
    if claim.ruling == "repo":
        return t("report.claim.merged", commits=_commits_named(claim, evidence)), True
    settled = _settled(claim)
    if settled is not None:
        return settled, False
    return _supported_wording(claim, evidence)


def _supported_wording(claim: Claim, evidence: DayEvidence) -> tuple[str, bool]:
    """A supported claim is worded by what it cites: the user's record, a commit, a page, an agent.

    Each wording carries whether it is evidence (the user, a commit, a page or the screen)
    or an agent's own account.
    """
    parts: list[tuple[str, bool]] = []
    if "user" in claim.kinds.values():
        parts.append((t("report.claim.user", keys=_keys_of(claim, "user")), True))
    if named := _commits_named(claim, evidence):
        parts.append((t("report.claim.committed", commits=named), True))
    if "screen" in claim.kinds.values():
        screen_keys = _keys_of(claim, "screen")
        if claim.screen == "agent":
            parts.append((t("report.claim.self_report", keys=screen_keys), False))
        elif claim.screen == "page":
            parts.append((t("report.claim.page", keys=screen_keys), True))
        elif not parts:
            # The judge did not say whose words the screen holds; it is not called a page.
            parts.append((t("report.claim.screen", keys=screen_keys), True))
    if "agent" in claim.kinds.values() and not parts:
        parts.append((t("report.claim.self_report", keys=_keys_of(claim, "agent")), False))
    evidenced = any(proof for _, proof in parts)
    text = t("sep.clause").join(words for words, _ in parts)
    return text or t("report.claim.unsupported"), evidenced


def _ref_line(numbers: list[int]) -> str:
    """Citation numbers; the sources at the foot of the report resolve every one of them."""
    if not numbers:
        return t("report.refs_none")
    return t("report.refs", refs=", ".join(f"#{n}" for n in numbers))


def _bullets(rows: list[dict[str, Any]], composer: _Composer, empty: str) -> list[str]:
    if not rows:
        return [f"- {empty}"]
    out = []
    for row in rows:
        refs = composer.refs(row["refs"])
        line = f"- {row['text']}"
        if row.get("rationale"):
            line += t("report.rationale", rationale=row["rationale"])
        out.append(t("report.bullet", line=line, refs=_ref_line(refs)))
    return out


def _fit(content: str) -> str:
    """Never persist a report whose body or citation index would be cut off."""
    if len(content) > CONTENT_LIMIT:
        message = f"report exceeds {CONTENT_LIMIT} characters; nothing saved or truncated"
        raise DailyReportParseError(message)
    return content


def _prose_shas(report: dict[str, Any], evidence: DayEvidence) -> list[str]:
    """Commit numbers written in the prose that are not same-day commits, or not cited."""
    known = {row["sha"]: key for key, row in evidence.commit_rows.items()}
    notes = []
    for index, item in enumerate(report["items"], 1):
        cited = {k for part in item["progress"] for k in part["refs"]}
        for token in dict.fromkeys(_SHA.findall(item["activity"])):
            short = token[:7]
            if short not in known:
                notes.append(t("report.sha_unknown", index=index, sha=short))
            elif known[short] not in cited:
                notes.append(t("report.sha_uncited", index=index, sha=short, key=known[short]))
    return notes


def _table(
    report: dict[str, Any], claims: list[Claim], evidence: DayEvidence
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    """Per item its parts' wordings; the evidenced, the unverified, and counts of the rest."""
    evidenced: list[str] = []
    unverified: list[str] = []
    counts: dict[str, int] = {}
    table: list[str] = []
    for index, item in enumerate(report["items"], 1):
        mine = [c for c in claims if c.item == index]
        words = [wording(c, evidence) for c in mine]
        breakdown = t("sep.clause").join(
            (t("report.part", part=c.part) if c.part else "") + text
            for c, (text, _) in zip(mine, words, strict=True)
        )
        line = t("report.table_line", index=index, title=item["title"], status=breakdown)
        table.append(line)
        if any(proven for _, proven in words):
            evidenced.append(line)
        elif any(c.claimed for c in mine):
            unverified.append(line)
        else:
            top = max((c.status for c in mine), key=STATUSES.index)
            counts[top] = counts.get(top, 0) + 1
    return table, evidenced, unverified, counts


def summary_table(report: dict[str, Any], claims: list[Claim], evidence: DayEvidence) -> list[str]:
    """The verified table the summary call is given: one line per item, statuses final."""
    return _table(report, claims, evidence)[0]


def _summary_lines(
    report: dict[str, Any], claims: list[Claim], evidence: DayEvidence, main_line: str | None
) -> list[str]:
    """The summary: one model sentence on the day's main line, then the program's verified lines."""
    _, evidenced, unverified, counts = _table(report, claims, evidence)
    if main_line is None:
        titles = t("sep.list").join(item["title"] for item in report["items"][:3])
        main_line = (
            t("report.main_line_titles", titles=titles) if titles else t("report.main_line_none")
        )
    out = [main_line]
    sep = t("sep.items")
    if evidenced:
        out.append(t("report.evidenced", items=sep.join(evidenced)))
    if unverified:
        out.append(t("report.unverified", items=sep.join(unverified)))
    rest = [
        t(f"report.count.{status}", count=counts[status]) for status in _REST if counts.get(status)
    ]
    if rest:
        out.append(t("report.rest", items=t("sep.list").join(rest)))
    return out


def compose_report(  # noqa: PLR0913 — the draft, its rulings, the one model sentence, the clock.
    report: dict[str, Any],
    evidence: DayEvidence,
    claims: list[Claim],
    *,
    main_line: str | None,
    model: str,
    generated_at: datetime,
) -> tuple[str, list[str], dict[str, str]]:
    """The saved content, its first 20 source refs and its coverage.

    Keys the model was not given are dropped and counted; every part's status
    text is the ruling on its claim; a next step that does not cite Allen's
    own words is removed from that section and named as an uncertainty;
    the summary is built from the rulings; each cited source is written out
    once, under the sources heading. Headings and fixed lines are in the
    language setting.
    """
    composer = _Composer(evidence)
    partial = t("report.partial") if evidence.window["partial"] else ""
    lines = [t("report.h.items")]
    unproven: list[str] = []
    for index, item in enumerate(report["items"], 1):
        numbers: list[int] = []
        heads: list[str] = []
        for claim in (c for c in claims if c.item == index):
            found = composer.refs(claim.refs)
            numbers += [n for n in found if n not in numbers]
            text, proven = wording(claim, evidence)
            heads.append((t("report.part", part=claim.part) if claim.part else "") + text)
            if claim.claimed and not proven:
                where = (
                    t("report.where_part", index=index, part=claim.part)
                    if claim.part
                    else t("report.where", index=index)
                )
                unproven.append(t("report.unproven_entry", where=where, text=text))
        lines += [
            t("report.item", index=index, title=item["title"], status=t("sep.clause").join(heads)),
            item["activity"],
            _ref_line(numbers),
            "",
        ]
    if not report["items"]:
        lines += [t("report.no_items"), ""]
    lines += [
        t("report.h.decisions"),
        *_bullets(report["decisions"], composer, t("report.no_decisions")),
        "",
        t("report.h.open"),
        *_bullets(report["open_items"], composer, t("report.no_open")),
        "",
    ]
    kept: list[dict[str, Any]] = []
    moved: list[str] = []
    for step in report["user_next_steps"]:
        if composer.stated(composer.refs(step["refs"])):
            kept.append(step)
        else:
            moved.append(step["text"])
    lines += [
        t("report.h.next"),
        *_bullets(kept, composer, t("report.no_next")),
        "",
        t("report.h.suggestions"),
    ]
    lines += [f"- {text}" for text in report["suggestions"]] or [t("report.none")]
    lines += ["", t("report.h.coverage")]
    lines += [f"- {text}" for text in evidence.served.values()]
    counts = evidence.counts
    observed = (
        t("report.last_observed", at=evidence.observed_until) if evidence.observed_until else ""
    )
    lines.append(
        t(
            "report.scale",
            windows=counts.get("windows", 0),
            screen=counts.get("screen", 0),
            records=counts.get("records", 0),
            git=counts.get("git", 0),
            agent=counts.get("agent", 0),
            state=counts.get("state_events", 0),
            observed=observed,
        )
    )
    lines += [f"- {t('material.scope', limit=limit)}" for limit in evidence.limits]
    lines.append(t("report.not_verified"))
    if composer.unknown:
        lines.append(t("report.unknown_refs", count=composer.unknown))
    if unproven:
        lines.append(t("report.unproven", parts=t("sep.clause").join(unproven)))
    lines += _prose_shas(report, evidence)
    if moved:
        quoted = "".join(t("quote", text=text) for text in moved)
        lines.append(t("report.moved_next", count=len(moved), quoted=quoted.strip()))
    lines += [f"- {text}" for text in report["uncertainties"]]
    lines += [
        "",
        t("report.h.sources"),
        t(
            "report.sources",
            count=len(composer.cited),
            kept=min(len(composer.cited), MAX_SOURCE_REFS),
        ),
        *(f"#{number} {ref}" for number, ref in enumerate(composer.cited, 1)),
    ]
    head = [
        t("report.title", day=evidence.day, zone=evidence.zone),
        t(
            "report.generated",
            at=generated_at.isoformat(timespec="seconds"),
            start=evidence.window["from"],
            end=evidence.window["to"],
            partial=partial,
            model=model,
        ),
        "",
        t("report.h.summary"),
        # Downstream readers are served this section alone: the one model sentence never
        # travels without the program's lines saying what was evidenced and what was not.
        *_summary_lines(report, claims, evidence, main_line),
        "",
        *plan_lines(evidence),
    ]
    return (
        _fit("\n".join([*head, *lines])),
        composer.cited[:MAX_SOURCE_REFS],
        dict(evidence.coverage),
    )
