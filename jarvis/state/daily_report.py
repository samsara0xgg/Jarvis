"""Bounded, keyed evidence for one local calendar day (ADR 0024) and the report's save path.

The daily work report reads the same stores as the daily-loop tools, but as a
runtime job: whole-day queries, explicit caps with the omitted range written
into the material, keys that map back to durable source references, and one
save through the briefing revision store.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.state import daily_store, timesink
from jarvis.state.daily_contract import DailyError, fingerprint
from jarvis.state.daily_records import read_connection
from jarvis.state.daily_store import current_items, high_water

if TYPE_CHECKING:
    from collections.abc import Sequence

ALLEN_SOURCE = "allen"
MAX_DETAILS = 10
DETAIL_TEXT = 2500
MAX_HITS = 20
_SNIPPET_BEFORE = 60
_SNIPPET_AFTER = 100
_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")
_MAX_APPS = 15
_MAX_WINDOWS = 40
_MAX_SCREEN_LINES = 120
_SCREEN_TEXT = 200
_SCREEN_BUDGET = 30000
_MAX_RECORDS = 80
_RECORD_TEXT = 240
_MAX_COMMITS = 40
_MAX_REPO_STATES = 10
_MAX_STATE_EVENTS = 40
_MAX_TODOS = 20
_MAX_KNOWLEDGE = 15
_MAX_AGENT_SESSIONS = 20
_AGENT_ASK = 120
_AGENT_ANSWER = 200
_PREVIOUS_TEXT = 1200
_LOCALTIME = Path("/etc/localtime")
_GIT_TIMEOUT_S = 5.0
_FIELD_SEP = "\x1f"
_MAIN = "main"
_GIT_WALK = "--max-count=5000"
"""The newest commits walked per repository; the day is filtered here, not by ``--since``.

``--since`` stops walking at the first commit older than the date, so one
rebased or amended commit with an old date hides everything beneath it.
ponytail: 5000 newest commits per walk; raise it if a repository ever holds
more than that in one day.
"""


def resolve_zone(name: str | None, configured: tzinfo | None) -> tuple[str, tzinfo]:
    """The report's zone: the argument, else the configured zone, else the system's IANA zone."""
    if name is not None:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"timezone must be an IANA zone name: {name!r}"
            raise DailyError(msg) from exc
    if isinstance(configured, ZoneInfo) and configured.key:
        return configured.key, configured
    try:
        parts = _LOCALTIME.resolve().parts
        key = "/".join(parts[parts.index("zoneinfo") + 1 :])
        return key, ZoneInfo(key)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return "UTC", UTC


def resolve_day(local_date: str | None, zone: tzinfo, now: datetime) -> date:
    """Yesterday by the local calendar, or the given day, which cannot lie in the future."""
    today = now.astimezone(zone).date()
    if local_date is None:
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(local_date)
        if day.isoformat() != local_date:
            raise ValueError  # noqa: TRY301 — noncanonical dates share the message below.
    except ValueError as exc:
        msg = "local_date must be YYYY-MM-DD"
        raise DailyError(msg) from exc
    if day > today:
        msg = f"local_date {local_date} is after today ({today.isoformat()}) in that zone"
        raise DailyError(msg)
    return day


def day_window(day: date, zone: tzinfo, now: datetime) -> tuple[datetime, datetime, bool]:
    """The local calendar day [00:00, 24:00), cut at ``now`` while the day is still running."""
    start = datetime.combine(day, time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    # Every rendered bound is local: a day cut at "now" must not print in UTC.
    return start, min(end, now.astimezone(zone)), end > now


def existing_report(conn: sqlite3.Connection, day: str, zone: str) -> dict[str, Any] | None:
    """The saved briefing's first chunk and metadata, or None."""
    try:
        return daily_store.get_briefing(conn, {"local_date": day, "timezone": zone})
    except DailyError as exc:
        if exc.code == "not_found":
            return None
        raise


@dataclass(frozen=True)
class DayEvidence:
    """Everything the report model may read; keys map back to durable source refs."""

    day: str
    zone: str
    window: dict[str, Any]
    coverage: dict[str, str]
    counts: dict[str, int]
    limits: list[str]
    observed_until: str | None
    sections: dict[str, list[dict[str, Any]]]
    refs: dict[str, str] = field(default_factory=dict)
    """Every key the model may cite, rendered or not; search reaches the rest."""
    stated: frozenset[str] = frozenset()
    commits: dict[str, str] = field(default_factory=dict)
    """Same-day commit refs and their short SHAs: what a proof label may name."""
    haystack: dict[str, str] = field(default_factory=dict)
    """Key -> searchable text for windows, records, commits and sessions; captures are searched
    in the store, whose text was never copied here."""

    @property
    def empty(self) -> bool:
        """True when the day holds no activity at all (context alone is not a day)."""
        return not any(
            self.sections.get(key) for key in ("windows", "screen", "records", "git", "agent")
        )


def _squeeze(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _ocr(item: dict[str, Any], limit: int) -> str:
    return _squeeze(item["summary"].partition(" — ")[2], limit)


def _row_id(item: dict[str, Any]) -> str:
    return str(item["source_refs"][0].split(":")[2])


@dataclass
class _Gather:
    """Mutable scratch shared by the section builders of one day."""

    zone: tzinfo
    start: datetime
    end: datetime
    day: date
    refs: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    stated: set[str] = field(default_factory=set)
    commits: dict[str, str] = field(default_factory=dict)
    haystack: dict[str, str] = field(default_factory=dict)
    latest: datetime | None = None

    def clock(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(self.zone).strftime("%H:%M")

    def day_clock(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(self.zone).strftime("%m-%d %H:%M")

    def saw(self, value: str) -> None:
        moment = min(datetime.fromisoformat(value), self.end)
        if self.latest is None or moment > self.latest:
            self.latest = moment


def _span_sections(g: _Gather, items: list[dict[str, Any]]) -> None:
    minutes_by_app: dict[str, float] = defaultdict(float)
    spans_by_app: dict[str, int] = defaultdict(int)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        g.saw(item["ended_at"])
        minutes = item["duration_seconds_in_window"] / 60
        minutes_by_app[item["app_name"]] += minutes
        spans_by_app[item["app_name"]] += 1
        group = groups.setdefault(
            (item["app_name"], item["summary"]),
            {
                "app": item["app_name"],
                "title": item["summary"],
                "minutes": 0.0,
                "spans": 0,
                "first": item["occurred_at"],
                "last": item["ended_at"],
                "_ref": item["source_refs"][0],
                "_longest": -1.0,
            },
        )
        group["minutes"] += minutes
        group["spans"] += 1
        group["first"] = min(group["first"], item["occurred_at"])
        group["last"] = max(group["last"], item["ended_at"])
        if minutes > group["_longest"]:
            group["_longest"] = minutes
            group["_ref"] = item["source_refs"][0]
    ranked_apps = sorted(minutes_by_app, key=lambda app: -minutes_by_app[app])
    rows: list[dict[str, Any]] = [
        {"app": app, "minutes": round(minutes_by_app[app]), "spans": spans_by_app[app]}
        for app in ranked_apps[:_MAX_APPS]
    ]
    tail = ranked_apps[_MAX_APPS:]
    if tail:
        rows.append(
            {
                "app": f"其他 {len(tail)} 个应用",
                "minutes": round(sum(minutes_by_app[app] for app in tail)),
                "spans": sum(spans_by_app[app] for app in tail),
            }
        )
    g.sections["apps"] = rows
    ranked = sorted(groups.values(), key=lambda x: -x["minutes"])
    if len(ranked) > _MAX_WINDOWS:
        rest = ranked[_MAX_WINDOWS:]
        g.limits.append(
            f"窗口共 {len(ranked)} 个，只逐条列出时长最长的 {_MAX_WINDOWS} 个；"
            f"其余 {len(rest)} 个（合计 {round(sum(x['minutes'] for x in rest))} 分钟）"
            "只计入应用时长"
        )
    windows = []
    for index, group in enumerate(ranked):
        key = f"a{group['_ref'].split(':')[2]}"
        g.refs[key] = group["_ref"]
        row = {
            "key": key,
            "app": group["app"],
            "title": group["title"],
            "minutes": round(group["minutes"]),
            "spans": group["spans"],
            "first": g.clock(group["first"]),
            "last": g.clock(group["last"]),
        }
        g.haystack[key] = (
            f"{row['first']}-{row['last']} {row['app']} — {row['title']}（{row['minutes']} 分钟）"
        )
        if index < _MAX_WINDOWS:
            windows.append(row)
    g.sections["windows"] = windows


def _screen_section(g: _Gather, items: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        g.saw(item["ended_at"])
        g.refs[f"s{_row_id(item)}"] = item["source_refs"][0]
        groups[(item["app_name"], item["window_title"])].append(item)
    lines: list[dict[str, Any]] = []
    budget = _SCREEN_BUDGET
    omitted_lines = omitted_captures = 0
    for (app, title), members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        buckets: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for item in members:
            began = datetime.fromisoformat(item["occurred_at"]).astimezone(g.zone)
            buckets[began.replace(minute=0, second=0, microsecond=0)].append(item)
        for hour in sorted(buckets):
            bucket = buckets[hour]
            if len(lines) >= _MAX_SCREEN_LINES or budget <= 0:
                omitted_lines += 1
                omitted_captures += len(bucket)
                continue
            longest = max(bucket, key=lambda x: int(x["text_chars"]))
            text = _ocr(longest, min(_SCREEN_TEXT, budget))
            budget -= len(text)
            key = f"s{_row_id(longest)}"
            lines.append(
                {
                    "key": key,
                    "app": app,
                    "title": title or "",
                    "hour": f"{hour:%H:%M}-{hour + timedelta(hours=1):%H:%M}",
                    "count": len(bucket),
                    "text": text,
                }
            )
    if omitted_lines:
        g.limits.append(
            f"屏幕内容共 {len(items)} 条、{len(groups)} 个窗口，只列出 {len(lines)} 个时段"
            f"（每行为同一窗口同一小时内 OCR 最长的一条）；未列出 {omitted_lines} 个时段共 "
            f"{omitted_captures} 条，优先保留内容变化最多的窗口"
        )
    g.sections["screen"] = lines


def _state_section(g: _Gather, state: dict[str, Any]) -> None:
    rows = [
        {"at": g.day_clock(e["at"]), "kind": e["kind"], "phase": "起始状态"}
        for e in state["at_start"]
    ]
    events = state["events"]
    if len(events) > _MAX_STATE_EVENTS:
        g.limits.append(f"状态事件共 {len(events)} 条，只列出前 {_MAX_STATE_EVENTS} 条")
    rows += [
        {"at": g.clock(e["at"]), "kind": e["kind"], "phase": ""}
        for e in events[:_MAX_STATE_EVENTS]
    ]
    g.sections["state_events"] = rows


def _timesink_sections(g: _Gather, snap: timesink.Snapshot | None) -> None:
    spans = timesink.query_spans(snap, g.start, g.end)
    captures = timesink.query_captures(snap, g.start, g.end)
    state = timesink.query_state(snap, g.start, g.end)
    g.coverage["app"] = str(spans["coverage"]["status"])
    g.coverage["screen"] = "partial" if captures["items"] else "unavailable"
    if snap is None:
        g.limits.append("TimeSink 不可读：没有应用、窗口和屏幕数据")
    elif not captures["items"]:
        g.limits.append("这一天没有屏幕内容记录（TimeSink 截屏未运行或不可用），只有应用/窗口时段")
    _span_sections(g, spans["items"])
    _screen_section(g, captures["items"])
    _state_section(g, state)


def _record_section(g: _Gather, memory_path: Path | None) -> None:
    try:
        with closing(read_connection(memory_path)) as memory:
            rows = memory.execute(
                "SELECT id,ts,source,text FROM records WHERE datetime(ts) >= datetime(?) "
                "AND datetime(ts) < datetime(?) ORDER BY rowid",
                (
                    g.start.astimezone(UTC).isoformat(timespec="seconds"),
                    g.end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchall()
    except (DailyError, sqlite3.Error):
        g.coverage["records"] = "unavailable"
        g.limits.append("对话记录库不可读：没有对话材料")
        g.sections["records"] = []
        return
    g.coverage["records"] = "available"
    if len(rows) > _MAX_RECORDS:
        g.limits.append(f"当天的对话记录共 {len(rows)} 条，只列出前 {_MAX_RECORDS} 条")
    records = []
    for index, (identity, ts, source, text) in enumerate(rows):
        key = f"r{index + 1}"
        ref = f"record:{identity}"
        g.refs[key] = ref
        if source == ALLEN_SOURCE:
            g.stated.add(ref)
        row = {
            "key": key,
            "at": g.clock(str(ts)),
            "who": str(source),
            "text": _squeeze(str(text), _RECORD_TEXT),
        }
        g.haystack[key] = f"{row['at']} {row['who']}: {_squeeze(str(text), DETAIL_TEXT)}"
        if index < _MAX_RECORDS:
            records.append(row)
    g.sections["records"] = records


def _fold_git_rows(
    g: _Gather, rows: list[Any], repos: Sequence[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """One entry per commit and per repo; what the day never saw is written into the limits."""
    commits: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    unwatched: set[str] = set()
    dropped = 0
    for uid, kind, observed, raw in rows:
        payload = json.loads(raw)
        repo = str(payload.get("repo_path") or "")
        if repos and repo not in repos:
            # A repo observed that day but no longer configured is omitted, not invisible.
            unwatched.add(repo)
            continue
        if kind == "project.commit_seen":
            # The observer burst-caps a poll; what it never emitted is missing from this day.
            dropped += int(payload.get("skipped_count") or 0)
            sha = str(payload.get("commit_sha") or uid)
            entry = commits.setdefault(
                sha,
                {
                    "uid": uid,
                    "sha": sha,
                    "subject": _squeeze(str(payload.get("subject") or ""), 160),
                    "committed_ms": int(payload.get("committed_at_ms") or observed),
                    "observed_ms": int(observed),
                    "paths": [],
                },
            )
            if repo not in entry["paths"]:
                entry["paths"].append(repo)
        else:
            states[repo] = {
                "uid": uid,
                "repo": repo,
                "branch": str(payload.get("branch") or "?"),
                "dirty": int(payload.get("dirty_file_count") or 0),
                "head": str(payload.get("head_sha") or "")[:7],
                "observed_ms": int(observed),
            }
    if unwatched:
        g.limits.append(
            f"当天还观察到 {len(unwatched)} 个仓库的活动，但它们已不在被观察列表里，未列入："
            + "、".join(sorted(unwatched))
        )
    if dropped:
        g.limits.append(
            f"Git 观察器追上积压时跳过了 {dropped} 个更早的提交，没有写进事件日志；"
            "当天的提交清单以本地仓库记录为准"
        )
    return commits, states


def _git(repo: str, args: Sequence[str]) -> str | None:
    """One read-only git command in a watched repository; None when it cannot run."""
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv; `repo` is a configured path.
            ["git", "-C", repo, *args],  # noqa: S607 — git is resolved from PATH on purpose.
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _local_commits(g: _Gather, repos: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Every commit on a local branch of a watched repository, committed inside the day.

    The observer only writes what it saw between polls and never backfills a
    repository it started watching that day, so the local repository is the
    authority on what was committed; the observer contributes when it saw it.
    """
    start, end = int(g.start.timestamp()), int(g.end.timestamp())
    found: dict[str, dict[str, Any]] = {}
    unreadable = []
    for repo in repos:
        log = _git(
            repo, ("log", "--branches", _GIT_WALK, f"--format=%H{_FIELD_SEP}%ct{_FIELD_SEP}%s")
        )
        if log is None:
            unreadable.append(repo)
            continue
        merged = _git(repo, ("log", _MAIN, _GIT_WALK, "--format=%H"))
        on_main = set(merged.split()) if merged is not None else None
        for line in log.splitlines():
            sha, _, rest = line.partition(_FIELD_SEP)
            seconds, _, subject = rest.partition(_FIELD_SEP)
            if not sha or not seconds.isdigit() or not start <= int(seconds) < end:
                continue
            entry = found.setdefault(
                sha,
                {
                    "sha": sha,
                    "subject": _squeeze(subject, 160),
                    "committed_ms": int(seconds) * 1000,
                    "paths": [],
                    "on_main": None,
                },
            )
            if repo not in entry["paths"]:
                entry["paths"].append(repo)
            if on_main is not None:
                entry["on_main"] = sha in on_main
    if unreadable:
        g.limits.append(
            f"无法读取 {len(unreadable)} 个仓库的本地 git 记录（路径不存在或不是仓库）："
            + "、".join(unreadable)
        )
    g.coverage["git"] = "partial" if unreadable else "available"
    return found


def _git_sections(g: _Gather, conn: sqlite3.Connection, repos: Sequence[str]) -> None:
    rows = conn.execute(
        "SELECT event_uid,type,ts_epoch_ms,payload_json FROM events "
        "WHERE type IN (?,?) AND ts_epoch_ms>=? AND ts_epoch_ms<? ORDER BY ts_epoch_ms,id",
        (
            *_ACTIVITY_TYPES,
            int(g.start.timestamp() * 1000),
            int(g.end.timestamp() * 1000),
        ),
    ).fetchall()
    observed, states = _fold_git_rows(g, rows, repos)
    commits = _local_commits(g, repos)
    for sha, seen in observed.items():
        # The local log is the authority on what and when; the observer adds when it was seen.
        entry = commits.setdefault(sha, {**seen, "on_main": None})
        entry["uid"] = seen["uid"]
        entry["observed_ms"] = seen["observed_ms"]
        entry["paths"] = list(dict.fromkeys([*entry["paths"], *seen["paths"]]))
    ordered = sorted(commits.values(), key=lambda x: (x["committed_ms"], x["sha"]))
    if len(ordered) > _MAX_COMMITS:
        g.limits.append(f"当天的提交共 {len(ordered)} 个（去重后），只列出前 {_MAX_COMMITS} 个")
    git = []
    for index, entry in enumerate(ordered[:_MAX_COMMITS]):
        key = f"g{index + 1}"
        ref = (
            f"event:{entry['uid']}"
            if "uid" in entry
            else f"git:{entry['paths'][0]}:{entry['sha']}"
        )
        g.refs[key] = ref
        committed = datetime.fromtimestamp(entry["committed_ms"] / 1000, UTC).astimezone(g.zone)
        late = committed.date() != g.day
        if not late:
            # An older commit only seen today is evidence of its own day, not of this one.
            g.commits[ref] = entry["sha"][:7]
        seen_at = (
            datetime.fromtimestamp(entry["observed_ms"] / 1000, UTC).astimezone(g.zone)
            if "observed_ms" in entry
            else None
        )
        g.haystack[key] = f"{entry['sha'][:7]} {entry['subject']}"
        git.append(
            {
                "key": key,
                "sha": entry["sha"][:7],
                "subject": entry["subject"],
                "committed": committed.strftime("%m-%d %H:%M"),
                "observed": seen_at.strftime("%H:%M") if seen_at else "观察器未记录",
                "paths": ", ".join(entry["paths"]),
                "late": late,
                "main": {True: "已在 main", False: "未进 main", None: "main 未知"}[
                    entry["on_main"]
                ],
            }
        )
    g.sections["git"] = git
    repo_states = []
    ranked_states = sorted(states.values(), key=lambda x: x["repo"])[:_MAX_REPO_STATES]
    for index, entry in enumerate(ranked_states):
        key = f"p{index + 1}"
        g.refs[key] = f"event:{entry['uid']}"
        repo_states.append(
            {
                "key": key,
                "repo": entry["repo"],
                "branch": entry["branch"],
                "dirty": entry["dirty"],
                "head": entry["head"],
                "at": datetime.fromtimestamp(entry["observed_ms"] / 1000, UTC)
                .astimezone(g.zone)
                .strftime("%H:%M"),
            }
        )
    g.sections["repo_states"] = repo_states
    if not repos:
        g.coverage["git"] = "unavailable"
        g.limits.append("没有配置被观察的 Git 仓库：Git 活动只来自历史记录，可能为空")


def _folded_section(
    g: _Gather, conn: sqlite3.Connection, kind: str, prefix: str, limit: int
) -> None:
    status = "open" if kind == "todo" else "active"
    items = [x for x in current_items(conn, kind, high_water(conn)) if x["status"] == status]
    if len(items) > limit:
        noun = "未完成待办" if kind == "todo" else "知识条目"
        g.limits.append(f"{noun}共 {len(items)} 条，只列出前 {limit} 条")
    rows = []
    for index, item in enumerate(items[:limit]):
        key = f"{prefix}{index + 1}"
        g.refs[key] = item["source_ref"]
        row: dict[str, Any] = {"key": key}
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


def _previous_section(g: _Gather, conn: sqlite3.Connection, zone_name: str) -> None:
    prior = (g.day - timedelta(days=1)).isoformat()
    item = existing_report(conn, prior, zone_name)
    if item is None:
        g.sections["previous"] = []
        return
    g.refs["b1"] = item["source_ref"]
    g.sections["previous"] = [
        {"key": "b1", "date": prior, "text": _squeeze(str(item["content"]), _PREVIOUS_TEXT)}
    ]


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(str(x.get("text") or "") for x in content if isinstance(x, dict)).strip()


def codex_session(
    path: Path, start: datetime | None = None, end: datetime | None = None
) -> dict[str, Any]:
    """One Codex session file: who ran it where, and its user/assistant turns inside [start, end).

    Codex writes ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``, one JSON
    object per line; only ``session_meta`` and the user/assistant messages are
    read. Whatever the agent says about its own work is its self-report.
    """
    meta: dict[str, Any] = {}
    asks: list[str] = []
    answers: list[str] = []
    first = last = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                meta = payload
                continue
            if row.get("type") != "response_item" or payload.get("type") != "message":
                continue
            try:
                moment = datetime.fromisoformat(str(row.get("timestamp")))
            except ValueError:
                continue
            if (start and moment < start) or (end and moment >= end):
                continue
            text = _message_text(payload)
            if payload.get("role") == "user":
                asks.append(text)
            elif payload.get("role") == "assistant":
                answers.append(text)
            else:
                continue
            first = moment if first is None else first
            last = moment
    return {
        "id": str(meta.get("id") or path.stem),
        "cwd": str(meta.get("cwd") or ""),
        "originator": str(meta.get("originator") or "codex"),
        "asks": asks,
        "answers": answers,
        "first": first,
        "last": last,
    }


def _first_prompt(asks: list[str]) -> str:
    """The first user text that is not an injected preamble (instructions, pasted files)."""
    return next((a for a in asks if a and not a.startswith(("<", "#"))), asks[0] if asks else "")


def _session_files(root: Path, day: date, start: datetime) -> list[Path]:
    """Session files that may hold a turn of the day: begun by then, still written since.

    Codex files a session under the local date it began and appends to it as
    long as it is used, so a session begun last week and continued today
    lives in last week's directory; its mtime is what says it was touched.
    """
    since = start.timestamp()
    found = []
    for path in root.glob("*/*/*/*.jsonl"):
        try:
            begun = date(*(int(part) for part in path.parts[-4:-1]))
            touched = path.stat().st_mtime
        except (ValueError, OSError):
            continue
        if begun <= day and touched >= since:
            found.append(path)
    return sorted(found)


def _agent_section(g: _Gather, sessions_root: Path | None) -> None:
    if sessions_root is None or not sessions_root.is_dir():
        g.coverage["agent"] = "unavailable"
        g.sections["agent"] = []
        g.limits.append("没有代理会话记录：Codex 本机会话目录不可读")
        return
    g.coverage["agent"] = "partial"
    sessions = []
    for path in _session_files(sessions_root, g.day, g.start):
        try:
            session = codex_session(path, g.start, g.end)
        except OSError:
            continue
        if session["answers"]:
            session["path"] = path
            sessions.append(session)
    sessions.sort(key=lambda x: (-len(x["answers"]), x["first"]))
    if len(sessions) > _MAX_AGENT_SESSIONS:
        g.limits.append(
            f"当天有对话的 Codex 会话共 {len(sessions)} 个，"
            f"只列出回复最多的 {_MAX_AGENT_SESSIONS} 个"
        )
    home = str(Path.home())
    rows = []
    for index, session in enumerate(sessions[:_MAX_AGENT_SESSIONS]):
        key = f"c{index + 1}"
        g.refs[key] = f"codex-session:{session['path']}"
        g.haystack[key] = _squeeze(" ".join([*session["asks"], *session["answers"]]), DETAIL_TEXT)
        g.saw(session["last"].isoformat())
        rows.append(
            {
                "key": key,
                "first": session["first"].astimezone(g.zone).strftime("%H:%M"),
                "last": session["last"].astimezone(g.zone).strftime("%H:%M"),
                "app": session["originator"],
                "cwd": session["cwd"].replace(home, "~", 1),
                "turns": f"{len(session['asks'])} 问 {len(session['answers'])} 答",
                "ask": _squeeze(_first_prompt(session["asks"]), _AGENT_ASK),
                "answer": _squeeze(session["answers"][-1], _AGENT_ANSWER),
            }
        )
    g.sections["agent"] = rows


def gather_day(  # noqa: PLR0913 — the configured stores plus the day, its zone and the clock.
    conn: sqlite3.Connection,
    *,
    memory_path: Path | None,
    timesink_path: Path | None,
    repos: Sequence[str],
    day: date,
    zone_name: str,
    zone: tzinfo,
    now: datetime,
    codex_sessions_path: Path | None = None,
) -> DayEvidence:
    """Read every configured source once for the whole local day, bounded and keyed."""
    start, end, partial = day_window(day, zone, now)
    g = _Gather(zone=zone, start=start, end=end, day=day)
    with timesink.snapshot(timesink_path) as snap:
        _timesink_sections(g, snap)
    _record_section(g, memory_path)
    _git_sections(g, conn, repos)
    _agent_section(g, codex_sessions_path)
    _folded_section(g, conn, "todo", "t", _MAX_TODOS)
    _folded_section(g, conn, "knowledge", "k", _MAX_KNOWLEDGE)
    g.coverage["todos"] = g.coverage["knowledge"] = "available"
    _previous_section(g, conn, zone_name)
    return DayEvidence(
        day=day.isoformat(),
        zone=zone_name,
        window={
            "from": start.isoformat(timespec="seconds"),
            "to": end.isoformat(timespec="seconds"),
            "partial": partial,
        },
        coverage=g.coverage,
        counts={key: len(rows) for key, rows in g.sections.items()},
        limits=g.limits,
        observed_until=(
            g.latest.astimezone(zone).isoformat(timespec="seconds") if g.latest else None
        ),
        sections=g.sections,
        refs=g.refs,
        stated=frozenset(g.stated),
        commits=dict(g.commits),
        haystack=g.haystack,
    )


def _snippet(text: str, terms: Sequence[str]) -> str:
    """The first term's match with a little of what surrounds it, on one line."""
    flat = " ".join(text.split())
    needle = terms[0]
    at = flat.casefold().find(needle)
    if at < 0:
        return flat[: _SNIPPET_BEFORE + _SNIPPET_AFTER]
    stop = at + len(needle) + _SNIPPET_AFTER
    head = "…" if at > _SNIPPET_BEFORE else ""
    tail = "…" if stop < len(flat) else ""
    return f"{head}{flat[max(0, at - _SNIPPET_BEFORE) : stop]}{tail}"


def _capture_hits(
    evidence: DayEvidence, terms: Sequence[str], *, timesink_path: Path | None, zone: tzinfo
) -> list[tuple[str, str]]:
    """Captures of the day whose full OCR text contains every term, oldest first."""
    ids = [int(k[1:]) for k in evidence.refs if k[:1] == "s" and k[1:].isdigit()]
    if not ids:
        return []
    hits = []
    with timesink.snapshot(timesink_path) as snap:
        if snap is None:
            return []
        marks = ",".join("?" * len(ids))
        wanted = " AND ".join("instr(lower(text), ?) > 0" for _ in terms)
        rows = snap.conn.execute(
            f"SELECT id,at,appName,title,text FROM capture WHERE id IN ({marks}) "  # noqa: S608 — placeholders only.
            f"AND {wanted} ORDER BY at,id",
            (*ids, *terms),
        ).fetchall()
    for identity, at, app, title, text in rows:
        when = datetime.fromisoformat(timesink.moment(at)).astimezone(zone).strftime("%H:%M")
        line = f"{when} {app} — {title or ''}: {_snippet(str(text), terms)}"
        hits.append((f"s{identity}", line))
    return hits


def search_day(
    evidence: DayEvidence, query: str, *, timesink_path: Path | None, zone: tzinfo
) -> str:
    """Keys whose text contains every word of the query, listed or not, with context.

    Words match anywhere and in any order, case-insensitively: a model that
    asks for "RBC 申请 submitted" is looking for a page holding all three, not
    for that exact phrase.
    """
    terms = [word.casefold() for word in query.split()]
    if not terms:
        return "请给出要检索的关键字。"
    hits = _capture_hits(evidence, terms, timesink_path=timesink_path, zone=zone)
    hits += [
        (key, _snippet(text, terms))
        for key, text in evidence.haystack.items()
        if all(term in text.casefold() for term in terms)
    ]
    if not hits:
        return f"「{query}」在这一天的材料里没有出现。"
    shown = hits[:MAX_HITS]
    rest = len(hits) - len(shown)
    more = f"\n…另有 {rest} 处命中未列出，请换更具体的关键字。" if rest else ""
    listed = "\n".join(f"[{k}] {line}" for k, line in shown)
    return f"「{query}」命中 {len(hits)} 处：\n{listed}{more}"


def _commit_detail(key: str, evidence: DayEvidence) -> str | None:
    """The commit itself, from the repository it was found in, for a commit key."""
    commit = next((row for row in evidence.sections.get("git", []) if row["key"] == key), None)
    if commit is None:
        return None
    shown = _git(
        commit["paths"].split(", ")[0],
        ("show", "--stat", "--format=%H%n%an %ci%n%n%B", commit["sha"]),
    )
    return None if shown is None else f"[{key}]\n{shown[:DETAIL_TEXT]}"


def _session_detail(key: str, path: Path, start: datetime, end: datetime) -> str:
    """A Codex session's prompts and last reply inside the window, marked as its own account."""
    session = codex_session(path, start, end)
    asks = "\n".join(f"- {_squeeze(a, 160)}" for a in session["asks"] if a)[:DETAIL_TEXT]
    last = session["answers"][-1] if session["answers"] else ""
    return (
        f"[{key}] Codex 会话 {session['id']}（{session['originator']}，{session['cwd']}）"
        f"——代理的自述，不是核实结果\n提问：\n{asks}\n最后回复：\n{_squeeze(last, DETAIL_TEXT)}"
    )


def _stored_detail(
    key: str, ref: str, conn: sqlite3.Connection, memory_path: Path | None
) -> str | None:
    """A conversation record or an event-log row behind a key; None for other kinds."""
    identity = ref.partition(":")[2]
    if ref.startswith("record:"):
        with closing(read_connection(memory_path)) as memory:
            row = memory.execute(
                "SELECT ts,source,text FROM records WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            return f"[{key}] 记录已不存在"
        return f"[{key}] {row[0]} {row[1]}: {_squeeze(str(row[2]), DETAIL_TEXT)}"
    if ref.startswith("event:"):
        row = conn.execute(
            "SELECT type,payload_json FROM events WHERE event_uid=?", (identity,)
        ).fetchone()
        if row is None:
            return f"[{key}] 事件已不存在"
        return f"[{key}] {row[0]}: {str(row[1])[:DETAIL_TEXT]}"
    return None


def read_detail(  # noqa: PLR0911 — one return per reference kind.
    key: str,
    evidence: DayEvidence,
    *,
    conn: sqlite3.Connection,
    memory_path: Path | None,
    timesink_path: Path | None,
) -> str:
    """The original behind one material key, bounded; an unreadable source says so."""
    ref = evidence.refs.get(key)
    if ref is None:
        return f"[{key}] 不是材料里的键"
    shown = _commit_detail(key, evidence)
    if shown is not None:
        return shown
    try:
        if ref.startswith("timesink-capture:"):
            row = timesink.read_capture(timesink_path, ref)
            when = f"{timesink.moment(row['at'])}..{row['endedAt']}"
            head = f"{row['appName']} — {row['title'] or ''} {when}（UTC）"
            return f"[{key}] {head}\n{_squeeze(str(row['text'] or ''), DETAIL_TEXT)}"
        if ref.startswith("timesink:"):
            original = timesink.read_span(timesink_path, ref)
            # The stored row keeps GRDB's bare UTC text; unlabelled it reads as a local clock.
            body = json.dumps(original, ensure_ascii=False)[:DETAIL_TEXT]
            return f"[{key}]（原始行，时间为 UTC）{body}"
        if ref.startswith("codex-session:"):
            start, end = (datetime.fromisoformat(evidence.window[k]) for k in ("from", "to"))
            return _session_detail(key, Path(ref.partition(":")[2]), start, end)
        shown = _stored_detail(key, ref, conn, memory_path)
    except (DailyError, sqlite3.Error, OSError) as exc:
        return f"[{key}] 该条目当前不可读：{exc}"
    return shown if shown is not None else f"[{key}] 没有可读的原文"


def save_report(  # noqa: PLR0913 — the two source stores, the identity, the payload and the writer.
    conn: sqlite3.Connection,
    *,
    memory_path: Path | None,
    timesink_path: Path | None,
    day: str,
    zone: str,
    content: str,
    source_refs: Sequence[str],
    coverage: dict[str, str],
    expected_version: int,
    action_id: str,
) -> dict[str, Any]:
    """Append the next briefing version through the shared revision store.

    The request id is derived from the content, so an exact retry returns the
    original receipt while a stale ``expected_version`` is a version conflict,
    never an overwrite.
    """
    args: dict[str, Any] = {
        "local_date": day,
        "timezone": zone,
        "content": content,
        "source_refs": list(source_refs),
        "coverage": dict(coverage),
        "request_id": (
            f"daily-work-report:{day}:{zone}:{expected_version + 1}:"
            f"{fingerprint([content, list(source_refs)])[:16]}"
        ),
    }
    if expected_version:
        args["expected_version"] = expected_version
    return daily_store.write_revision(
        conn, memory_path, "save_briefing", args, action_id, timesink_path=timesink_path
    )
