"""One persisted "current work state" record and the bounded evidence it is analysed from."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from jarvis.state import timesink
from jarvis.state.daily_contract import DailyError, fingerprint
from jarvis.state.daily_records import read_connection
from jarvis.state.daily_store import current_items, high_water
from jarvis.state.event_log import append_event_in_transaction

if TYPE_CHECKING:
    from pathlib import Path

EVENT_TYPE = "work_state.revised"
STATE_ID = "work_state"
BASES = ("stated", "observed", "inferred")
LINK_KINDS = ("todo", "discussion")
NOTE_REF = "note"
NOTE_KEY = "u1"
ALLEN_SOURCE = "allen"
RECENT_HOURS = 2
RECORD_HOURS = 48
RELATED_DAYS = 14
_MAX_RECENT = 30
_MAX_GROUPS = 15
_MAX_RECORDS = 40
_MAX_RELATED_RECORDS = 15
_MAX_RELATED_SCREEN = 10
_MAX_TODOS = 20
_MAX_KNOWLEDGE = 15
_MAX_GIT = 20
_MAX_STATE_EVENTS = 20
_MAX_TERMS = 8
_RECENT_TEXT = 200
_RECENT_TEXT_BUDGET = 4000
_GROUP_TEXT = 100
_RECORD_TEXT = 240
_RECORD_TEXT_BUDGET = 4000
_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")
# Question words that carry no topic; what is left is looked up in older records and captures.
_STOP_WORDS = (
    "在做什么",
    "做了什么",
    "怎么样",
    "有没有",
    "之前",
    "说的",
    "说过",
    "事情",
    "进展",
    "如何",
    "怎样",
    "现在",
    "今天",
    "主要",
    "刚才",
    "在忙",
    "什么",
    "我们",
    "我的",
    "那个",
    "这个",
    "一下",
    "了吗",
    "吗",
    "呢",
    "的",
    "了",
    "在",
    "我",
    "你",
    "请",
    "帮",
)
_TERM = re.compile(r"[A-Za-z0-9_./-]{2,}|[一-鿿]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]{4,}")


def current_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Latest saved record, or None before the first analysis."""
    row = conn.execute(
        "SELECT event_uid,payload_json FROM events WHERE type=? ORDER BY id DESC LIMIT 1",
        (EVENT_TYPE,),
    ).fetchone()
    if row is None:
        return None
    item = dict(json.loads(row[1])["item"])
    item["source_ref"] = f"event:{row[0]}"
    return item


def save_state(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    expected_version: int,
    trigger: str,
    action_id: str | None,
) -> dict[str, Any]:
    """Append the next version atomically; a stale writer never overwrites a newer record."""
    if conn.in_transaction:
        msg = "Write requires an idle connection"
        raise DailyError(msg, "transaction_busy")
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = current_state(conn)
        _check_version(previous, expected_version)
        now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        saved = {
            **item,
            "id": STATE_ID,
            "version": (previous["version"] if previous else 0) + 1,
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
        }
        payload: dict[str, Any] = {"item": saved, "trigger": trigger}
        if action_id is not None:
            payload["action_id"] = action_id
        event = append_event_in_transaction(
            conn,
            type=EVENT_TYPE,
            payload=payload,
            correlation={"action_id": action_id} if action_id else None,
            actor="jarvis_llm",
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    saved["source_ref"] = f"event:{event.event_uid}"
    return saved


def _check_version(previous: dict[str, Any] | None, expected_version: int) -> None:
    version = previous["version"] if previous else 0
    if version != expected_version:
        msg = f"Version conflict; current_version={version}. Re-read before writing."
        raise DailyError(msg, "version_conflict")


def question_terms(question: str | None) -> list[str]:
    """Topic words of a question: Latin tokens and CJK runs (plus bigrams), stop words removed."""
    text = question or ""
    for stop in _STOP_WORDS:
        text = text.replace(stop, " ")
    terms: list[str] = []
    for token in _TERM.findall(text):
        terms.append(token)
        if _CJK_RUN.fullmatch(token):
            terms.extend(token[i : i + 2] for i in range(len(token) - 1))
    unique: list[str] = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return unique[:_MAX_TERMS]


def _matches(text: str, terms: list[str]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


@dataclass(frozen=True)
class Evidence:
    """Bounded, keyed material for one analysis; keys map back to durable source refs."""

    fingerprint: str
    window: dict[str, str]
    coverage: dict[str, str]
    counts: dict[str, int]
    limits: list[str]
    observed_until: str | None
    sections: dict[str, list[dict[str, Any]]]
    refs: dict[str, str] = field(default_factory=dict)
    note: str | None = None
    question: str | None = None
    terms: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True when nothing observed or stated exists to analyse (todos alone are not activity)."""
        keys = (
            "recent",
            "earlier",
            "windows",
            "records",
            "related_records",
            "related_screen",
            "git",
        )
        return self.note is None and not any(self.sections.get(key) for key in keys)


def _local(value: str, tz: tzinfo | None) -> str:
    return datetime.fromisoformat(value).astimezone(tz).strftime("%H:%M")


def _local_day(value: str, tz: tzinfo | None) -> str:
    return datetime.fromisoformat(value).astimezone(tz).strftime("%m-%d %H:%M")


def _squeeze(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _ocr(item: dict[str, Any], limit: int) -> str:
    return _squeeze(item["summary"].partition(" — ")[2], limit)


def _row_id(item: dict[str, Any]) -> str:
    return str(item["source_refs"][0].split(":")[2])


@dataclass
class _Gather:
    """Mutable scratch shared by the section builders of one gather."""

    tz: tzinfo | None
    refs: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    latest: datetime | None = None

    def saw(self, value: str) -> None:
        moment = datetime.fromisoformat(value)
        if self.latest is None or moment > self.latest:
            self.latest = moment


def _span_sections(g: _Gather, spans: dict[str, Any]) -> None:
    minutes_by_app: dict[str, float] = defaultdict(float)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in spans["items"]:
        g.saw(item["ended_at"])
        minutes = item["duration_seconds_in_window"] / 60
        minutes_by_app[item["app_name"]] += minutes
        group = groups.setdefault(
            (item["app_name"], item["summary"]),
            {
                "app": item["app_name"],
                "title": item["summary"],
                "minutes": 0.0,
                "first": item["occurred_at"],
                "last": item["ended_at"],
                "_ref": item["source_refs"][0],
                "_longest": 0.0,
            },
        )
        group["minutes"] += minutes
        group["first"] = min(group["first"], item["occurred_at"])
        group["last"] = max(group["last"], item["ended_at"])
        if minutes > group["_longest"]:
            group["_longest"] = minutes
            group["_ref"] = item["source_refs"][0]
    g.sections["apps"] = [
        {"app": app, "minutes": round(minutes)}
        for app, minutes in sorted(minutes_by_app.items(), key=lambda kv: -kv[1])[:12]
    ]
    ranked = sorted(groups.values(), key=lambda x: -x["minutes"])
    if len(ranked) > _MAX_GROUPS:
        g.limits.append(f"今天的窗口共 {len(ranked)} 个，只列出时长最长的 {_MAX_GROUPS} 个")
    windows = []
    for group in ranked[:_MAX_GROUPS]:
        key = f"a{group['_ref'].split(':')[2]}"
        g.refs[key] = group["_ref"]
        windows.append(
            {
                "key": key,
                "app": group["app"],
                "title": group["title"],
                "minutes": round(group["minutes"]),
                "first": _local(group["first"], g.tz),
                "last": _local(group["last"], g.tz),
            }
        )
    g.sections["windows"] = windows


def _capture_sections(g: _Gather, captures: dict[str, Any], recent_start: datetime) -> None:
    recent: list[dict[str, Any]] = []
    earlier: dict[tuple[str, str | None], dict[str, Any]] = {}
    for item in captures["items"]:
        g.saw(item["ended_at"])
        began = datetime.fromisoformat(item["occurred_at"])
        if began >= recent_start or datetime.fromisoformat(item["ended_at"]) >= recent_start:
            recent.append(
                {
                    "key": f"s{_row_id(item)}",
                    "observed_at": datetime.fromisoformat(item["ended_at"])
                    .astimezone(g.tz)
                    .isoformat(timespec="seconds"),
                    "from": _local(item["occurred_at"], g.tz),
                    "to": _local(item["ended_at"], g.tz),
                    "app": item["app_name"],
                    "title": item["window_title"],
                    "text": _ocr(item, _RECENT_TEXT),
                    "chars": item["text_chars"],
                    "_ref": item["source_refs"][0],
                }
            )
            continue
        group = earlier.setdefault(
            (item["app_name"], item["window_title"]),
            {
                "key": f"s{_row_id(item)}",
                "app": item["app_name"],
                "title": item["window_title"],
                "count": 0,
                "first": item["occurred_at"],
                "last": item["ended_at"],
                "text": _ocr(item, _GROUP_TEXT),
                "_ref": item["source_refs"][0],
                "_chars": item["text_chars"],
            },
        )
        group["count"] += 1
        group["first"] = min(group["first"], item["occurred_at"])
        group["last"] = max(group["last"], item["ended_at"])
        if item["text_chars"] > group["_chars"]:
            group.update(
                key=f"s{_row_id(item)}",
                text=_ocr(item, _GROUP_TEXT),
                _ref=item["source_refs"][0],
                _chars=item["text_chars"],
            )
    if len(recent) > _MAX_RECENT:
        g.limits.append(f"最近窗口内有 {len(recent)} 条屏幕内容，只保留最新的 {_MAX_RECENT} 条")
    recent = recent[-_MAX_RECENT:]
    budget = _RECENT_TEXT_BUDGET
    for row in reversed(recent):
        row["text"] = row["text"][: max(0, budget)]
        budget -= len(row["text"])
    if budget <= 0:
        g.limits.append("最近屏幕内容的文字超出预算，较早的条目只剩标题")
    for row in recent:
        g.refs[row["key"]] = row.pop("_ref")
    g.sections["recent"] = recent
    ranked = sorted(earlier.values(), key=lambda x: -x["count"])
    if len(ranked) > _MAX_GROUPS:
        g.limits.append(
            f"今天较早的屏幕内容涉及 {len(ranked)} 个窗口，只列出最常见的 {_MAX_GROUPS} 个"
        )
    grouped = []
    for group in ranked[:_MAX_GROUPS]:
        g.refs[group["key"]] = group["_ref"]
        grouped.append(
            {
                "key": group["key"],
                "app": group["app"],
                "title": group["title"],
                "count": group["count"],
                "first": _local(group["first"], g.tz),
                "last": _local(group["last"], g.tz),
                "text": group["text"],
            }
        )
    g.sections["earlier"] = grouped


def _timesink_sections(  # noqa: PLR0913 — one snapshot, the three windows and the question terms.
    g: _Gather,
    snap: timesink.Snapshot | None,
    start: datetime,
    recent_start: datetime,
    now: datetime,
    terms: list[str],
) -> list[Any]:
    spans = timesink.query_spans(snap, start, now)
    captures = timesink.query_captures(snap, start, now)
    state = timesink.query_state(snap, start, now)
    g.coverage["app"] = str(spans["coverage"]["status"])
    g.coverage["screen"] = str(captures["coverage"]["status"])
    if snap is None:
        g.limits.append("TimeSink 不可读：没有应用、窗口和屏幕数据")
    _span_sections(g, spans)
    _capture_sections(g, captures, recent_start)
    all_events = [*state["at_start"], *state["events"]]
    events = all_events[-_MAX_STATE_EVENTS:]
    g.coverage["state"] = str(state["coverage"]["status"])
    if len(all_events) > _MAX_STATE_EVENTS:
        g.limits.append(f"状态事件共 {len(all_events)} 条，只列出最后 {_MAX_STATE_EVENTS} 条")
    for name, found in (("应用", spans), ("屏幕", captures), ("状态事件", state)):
        if found["coverage"]["status"] == "unavailable" and snap is not None:
            g.limits.append(f"TimeSink {name}来源不可用")
    g.sections["state_events"] = [{"at": _local(e["at"], g.tz), "kind": e["kind"]} for e in events]
    related = timesink.search_captures(
        snap, now - timedelta(days=RELATED_DAYS), start, terms, limit=_MAX_RELATED_SCREEN
    )
    if len(related) == _MAX_RELATED_SCREEN:
        g.limits.append(f"与问题相关的更早屏幕内容只取了最新的 {_MAX_RELATED_SCREEN} 条")
    rows = []
    for item in reversed(related):
        key = f"s{_row_id(item)}"
        g.refs[key] = item["source_refs"][0]
        rows.append(
            {
                "key": key,
                "at": _local_day(item["occurred_at"], g.tz),
                "app": item["app_name"],
                "title": item["window_title"],
                "text": _ocr(item, _GROUP_TEXT),
            }
        )
    g.sections["related_screen"] = rows
    return [
        snap.identity if snap else None,
        [(_row_id(row), row["summary"]) for row in spans["items"]],
        [(_row_id(row), row["text_chars"], row["summary"]) for row in captures["items"]],
        [(e["at"], e["kind"]) for e in events],
        [_row_id(row) for row in related],
    ]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _record_rows(
    g: _Gather, rows: list[tuple[Any, ...]], *, start_index: int, budget: int
) -> tuple[list[dict[str, Any]], int]:
    records = []
    for offset, (identity, ts, source, text) in enumerate(rows):
        excerpt = _squeeze(str(text), min(_RECORD_TEXT, max(0, budget)))
        budget -= len(excerpt)
        key = f"r{start_index + offset}"
        g.refs[key] = f"record:{identity}"
        records.append(
            {"key": key, "at": _local_day(str(ts), g.tz), "who": str(source), "text": excerpt}
        )
    records.reverse()
    return records, budget


def _record_sections(
    g: _Gather, memory_path: Path | None, since: datetime, terms: list[str]
) -> list[str]:
    try:
        with closing(read_connection(memory_path)) as memory:
            rows = memory.execute(
                "SELECT id,ts,source,text FROM records WHERE datetime(ts) >= datetime(?) "
                "ORDER BY rowid DESC LIMIT ?",
                (since.isoformat(timespec="seconds"), _MAX_RECORDS),
            ).fetchall()
            related: list[tuple[Any, ...]] = []
            if terms:
                clauses = " OR ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
                related = memory.execute(
                    f"SELECT id,ts,source,text FROM records WHERE datetime(ts) < datetime(?) "  # noqa: S608 — placeholders only.
                    f"AND ({clauses}) ORDER BY rowid DESC LIMIT ?",
                    (since.isoformat(timespec="seconds"), *map(_like, terms), _MAX_RELATED_RECORDS),
                ).fetchall()
    except (DailyError, sqlite3.Error):
        g.coverage["records"] = "unavailable"
        g.limits.append("对话记录库不可读：没有对话材料")
        g.sections["records"] = g.sections["related_records"] = []
        return []
    g.coverage["records"] = "available"
    if len(rows) == _MAX_RECORDS:
        g.limits.append(f"近 {RECORD_HOURS} 小时的对话记录超过 {_MAX_RECORDS} 条，只保留最新的")
    if len(related) == _MAX_RELATED_RECORDS:
        g.limits.append(f"与问题相关的更早对话记录只取了最新的 {_MAX_RELATED_RECORDS} 条")
    records, budget = _record_rows(g, rows, start_index=1, budget=_RECORD_TEXT_BUDGET)
    if budget <= 0:
        g.limits.append("对话记录的文字超出预算，较早的条目被截断")
    g.sections["records"] = records
    g.sections["related_records"], _ = _record_rows(
        g, related, start_index=len(rows) + 1, budget=_RECORD_TEXT_BUDGET // 2
    )
    return [str(row[0]) for row in [*rows, *related]]


def _folded_section(  # noqa: PLR0913 — one fold, its kind, key prefix, cap and the question terms.
    g: _Gather, conn: sqlite3.Connection, kind: str, prefix: str, limit: int, terms: list[str]
) -> list[Any]:
    status = "open" if kind == "todo" else "active"
    items = [x for x in current_items(conn, kind, high_water(conn)) if x["status"] == status]
    label = "title" if kind == "todo" else "statement"
    items.sort(key=lambda x: not _matches(str(x[label]), terms))
    if len(items) > limit:
        noun = "未完成待办" if kind == "todo" else "知识条目"
        g.limits.append(f"{noun}共 {len(items)} 条，只列出 {limit} 条（与问题相关的优先）")
    rows = []
    for index, item in enumerate(items[:limit]):
        key = f"{prefix}{index + 1}"
        g.refs[key] = item["source_ref"]
        row: dict[str, Any] = {"key": key, "id": item["id"]}
        if kind == "todo":
            row.update(
                title=item["title"],
                due_at=item.get("due_at"),
                priority=item.get("priority"),
                project=item.get("project"),
            )
        else:
            row.update(statement=_squeeze(item["statement"], 160), kind=item["kind"])
        rows.append(row)
    g.sections["todos" if kind == "todo" else "knowledge"] = rows
    return [(item["id"], item["version"]) for item in items]


def _git_section(
    g: _Gather, conn: sqlite3.Connection, since: datetime, repos: tuple[str, ...]
) -> list[str]:
    rows = conn.execute(
        "SELECT event_uid,type,ts_epoch_ms,payload_json FROM events "
        "WHERE type IN (?,?) AND ts_epoch_ms>=? ORDER BY ts_epoch_ms DESC,id DESC LIMIT ?",
        (*_ACTIVITY_TYPES, int(since.timestamp() * 1000), _MAX_GIT),
    ).fetchall()
    items = []
    for index, (uid, kind, observed, raw) in enumerate(rows):
        payload = json.loads(raw)
        if repos and payload.get("repo_path") not in repos:
            continue
        key = f"g{index + 1}"
        g.refs[key] = f"event:{uid}"
        text = _squeeze(
            str(payload.get("subject") or payload.get("last_commit_subject") or ""), 160
        )
        if kind == "project.commit_seen":
            # `at` is the observation time; a commit seen late still carries its own time.
            committed = datetime.fromtimestamp(payload["committed_at_ms"] / 1000, UTC)
            text = f"提交于 {committed.astimezone(g.tz):%m-%d %H:%M}：{text}"
        items.append(
            {
                "key": key,
                "at": (
                    datetime.fromtimestamp(observed / 1000, UTC).astimezone(g.tz).strftime("%H:%M")
                ),
                "repo": payload.get("repo_path"),
                "kind": "commit" if kind == "project.commit_seen" else "repo",
                "text": text,
            }
        )
    items.reverse()
    g.sections["git"] = items
    g.coverage["git"] = "partial" if repos else "unavailable"
    if not repos:
        g.limits.append("没有配置被观察的 Git 仓库：没有 Git 活动数据")
    return [str(row[0]) for row in rows]


def gather_evidence(  # noqa: PLR0913 — the configured stores plus the request's question and note.
    conn: sqlite3.Connection,
    *,
    memory_path: Path | None,
    timesink_path: Path | None,
    repos: tuple[str, ...],
    note: str | None = None,
    question: str | None = None,
    tz: tzinfo | None = None,
    now: datetime | None = None,
) -> Evidence:
    """Read every configured source once, bounded, keyed, and directed by the question's terms.

    Fingerprint the bounded material and its revisions, including changing durations and
    state events. Only explicit refreshes analyse; the request clock alone never invalidates
    a result, but a different local day or evidence moving out of the recent window does.
    """
    moment = (now or datetime.now(UTC)).astimezone(tz)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    recent_start = moment - timedelta(hours=RECENT_HOURS)
    start = min(day_start, recent_start)
    terms = question_terms(question)
    g = _Gather(tz=tz)
    if note:
        g.refs[NOTE_KEY] = NOTE_REF
    with timesink.snapshot(timesink_path) as snap:
        timesink_digest = _timesink_sections(g, snap, start, recent_start, moment, terms)
    record_ids = _record_sections(g, memory_path, moment - timedelta(hours=RECORD_HOURS), terms)
    todo_digest = _folded_section(g, conn, "todo", "t", _MAX_TODOS, terms)
    knowledge_digest = _folded_section(g, conn, "knowledge", "k", _MAX_KNOWLEDGE, terms)
    g.coverage["todos"] = g.coverage["knowledge"] = "available"
    git_digest = _git_section(g, conn, start, repos)
    digest = fingerprint(
        [
            timesink_digest,
            record_ids,
            todo_digest,
            knowledge_digest,
            git_digest,
            g.sections,
            g.refs,
            g.coverage,
            g.limits,
            day_start.date().isoformat(),
            note,
            question,
        ]
    )
    return Evidence(
        fingerprint=digest,
        window={
            "from": start.isoformat(timespec="seconds"),
            "recent_from": recent_start.isoformat(timespec="seconds"),
            "to": moment.isoformat(timespec="seconds"),
            "related_from": (moment - timedelta(days=RELATED_DAYS)).isoformat(timespec="seconds"),
        },
        coverage=g.coverage,
        counts={key: len(rows) for key, rows in g.sections.items()},
        limits=g.limits,
        observed_until=(
            g.latest.astimezone(tz).isoformat(timespec="seconds") if g.latest else None
        ),
        sections=g.sections,
        refs=g.refs,
        note=note,
        question=question,
        terms=terms,
    )


def _is_observed(refs: list[str]) -> bool:
    return any(r.startswith("timesink") for r in refs)


def _evidence_block(evidence: Evidence) -> dict[str, Any]:
    return {
        "fingerprint": evidence.fingerprint,
        "window": evidence.window,
        "coverage": evidence.coverage,
        "counts": evidence.counts,
        "limits": evidence.limits,
        "terms": evidence.terms,
    }


def compose_state(  # noqa: C901 — the record's validation rules, listed once.
    analysis: dict[str, Any],
    evidence: Evidence,
    *,
    question: str | None,
    model: str,
    analyzed_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn a parsed analysis into the record shape, enforcing the evidence rules.

    Keys the model was not given are dropped; ``stated`` needs Allen's own record or the note
    behind it and ``observed`` needs TimeSink data, else the claim becomes ``inferred``; ``now``
    only stands on observations inside the recent window; inferred claims force an uncertainty.
    """
    todos = {row["key"]: row for row in evidence.sections.get("todos", [])}
    stated = {NOTE_REF} | {
        evidence.refs[row["key"]]
        for section in ("records", "related_records")
        for row in evidence.sections.get(section, [])
        if row["who"] == ALLEN_SOURCE
    }
    downgraded = 0

    def _refs(keys: list[str]) -> list[str]:
        return sorted({evidence.refs[k] for k in keys if k in evidence.refs})

    def _is_stated(refs: list[str]) -> bool:
        return any(r in stated for r in refs)

    def _claim(raw: dict[str, Any]) -> dict[str, Any]:
        nonlocal downgraded
        refs = _refs(raw.get("refs", []))
        basis = raw["basis"]
        if (basis == "stated" and not _is_stated(refs)) or (
            basis == "observed" and not _is_observed(refs)
        ):
            basis = "inferred"
            downgraded += 1
        claim = {"text": raw["text"], "basis": basis, "refs": refs}
        if "progress" in raw:
            claim["progress"] = raw["progress"]
        return claim

    now_claim = None
    if analysis.get("now"):
        raw_now = analysis["now"]
        cited_recent = [
            row["observed_at"]
            for row in evidence.sections.get("recent", [])
            if row["key"] in raw_now.get("refs", [])
        ]
        if cited_recent:
            now_claim = {**_claim(raw_now), "as_of": max(cited_recent)}
    activities = [_claim(raw) for raw in analysis.get("activities", [])]
    links = []
    for raw in analysis.get("links", []):
        key = raw["key"]
        if raw["kind"] == "todo" and key in todos:
            ref, title = todos[key]["id"], todos[key]["title"]
        elif raw["kind"] == "discussion" and evidence.refs.get(key, "").startswith("record:"):
            ref, title = evidence.refs[key], raw.get("title", "")
        else:
            continue
        basis = raw["basis"]
        if basis == "stated" and not _is_stated([ref]):
            basis, downgraded = "inferred", downgraded + 1
        links.append(
            {"kind": raw["kind"], "ref": ref, "title": title, "note": raw["note"], "basis": basis}
        )
    uncertainties = list(analysis.get("uncertainties", []))
    claims = [c for c in [now_claim, *activities, *links] if c is not None]
    if downgraded:
        uncertainties.append(f"有 {downgraded} 条结论缺少明确依据，已按推断处理")
    if any(c["basis"] == "inferred" for c in claims) and not uncertainties:
        uncertainties.append("部分结论是推断，未经确认")
    uncertainties.extend(f"材料范围：{limit}" for limit in evidence.limits)
    return {
        "analyzed_at": (analyzed_at or datetime.now(UTC))
        .astimezone()
        .isoformat(timespec="seconds"),
        "observed_until": evidence.observed_until,
        "evidence": _evidence_block(evidence),
        "now": now_claim,
        "activities": activities,
        "links": links,
        "uncertainties": uncertainties,
        "note": evidence.note,
        "question": question,
        "model": model,
    }
