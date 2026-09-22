"""Daily work report request, reply, check and composition (ADR 0027): the skill's model calls.

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
from typing import TYPE_CHECKING, Any, NoReturn

from jarvis.shared.skills import load_skill
from jarvis.state.daily_report import MAX_DETAILS, MAX_HITS, is_question

if TYPE_CHECKING:
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
    "以上是查询结果。还可以再查询（search_material、request_details 可同时调用），"
    "或者直接调用 report_daily_work 汇报。"
)
"""Served after a query round while rounds remain."""
REPORT_NOW = (
    "以上是查询结果。查询轮数已用完，现在必须调用 report_daily_work 汇报这一天。"
    "没来得及核对的事项不要写成 completed，写成 attempted 并记入 uncertainties。"
)
"""Served after the last query round: the model reports what it has, not what it guesses."""
REPORT_AGAIN = "上一次汇报无法解析，请按 schema 重新调用 report_daily_work，内容可以更简短。"
"""Served after an unusable reply: providers malform or truncate long arguments now and then."""
STATUSES = ("browsed", "discussed", "attempted", "completed")
CONTENT_LIMIT = 44000
"""Reports exceeding this budget fail without saving or dropping their citation index."""
MAX_SOURCE_REFS = 20
VERDICTS = ("supported", "partial", "unsupported")
_STATUS_LABELS = {"browsed": "浏览", "discussed": "讨论", "attempted": "尝试/进行中"}
_REST = (("attempted", "进行中"), ("discussed", "讨论"), ("browsed", "浏览"))
_NOT_VERIFIED = (
    "每个标为完成的部分都对照它引用的原文核查过，核查结论写在状态里；"
    "浏览/讨论/进行中是报告作者的判断，运行时不核实。"
)
_MAX_ITEMS = 12
_MAX_PARTS = 4
"""Parts of one item with a status each: code, tests, deployment, an application."""
_MAX_DECISIONS = 8
_MAX_OPEN = 10
_MAX_NEXT = 8
_MAX_SUGGESTIONS = 6
_MAX_UNCERTAINTIES = 10
_TITLE = 80
_PART = 12
_TEXT = 400
_SHORT = 240
_SHOWS = 120
_MAIN_LINE = 200
_SERVED = 3000
"""``summary_of`` serves the whole 核心摘要 section, digest included, not just the prose."""
_SUMMARY_HEADING = "## 核心摘要"
_REF_PREFIX = "引用："
_DEPLOY = re.compile(r"部署|上线|常驻|重启|发布|上架|deploy|restart|launch|online", re.I)
_MERGE = re.compile(r"合并|合入|merge|进 ?main", re.I)
_SHA = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{7,40}(?![0-9a-zA-Z])")
_COMPLETION_WORDS = re.compile(
    r"完成|已部署|已合并|已上线|已重启|通过|做完|搞定|done|deployed|merged|passed|shipped", re.I
)
"""What the model's one summary sentence may not assert: completion is stated by the table."""

JUDGE_SYSTEM = (
    "你是核查员。给你一条工作报告里的事项、它声称完成的部分，以及作者引用的原文。"
    "只根据原文判断每个部分：supported = 原文确实显示这个部分（同一件事、同一范围）已经完成或产物已存在；"
    "partial = 原文只显示其中一部分完成，或范围更小；unsupported = 原文与这个部分无关、只是计划/提问/讨论、"
    "或只显示尝试而非完成。提交只证明改动已提交，不证明测试、部署、合并；代理（Codex、Claude）说的"
    "「已完成」「已部署」「测试通过」是自述，不是核实结果——原文若只有这类话，shows 里写明是自述。"
    "shows 用一句话写原文实际显示了什么。screen 字段：原文是屏幕文字时，写 page（第三方网页或应用页面，"
    "如申请确认页）或 agent（终端/Codex/ChatGPT 里代理自己说的话）；不是屏幕文字时为 null。"
    "必须调用 judge_claims 回答，每个部分一条。"
)
SUMMARY_SYSTEM = (
    "你是日报撰写员。下面是这一天已经核查过的事项清单，每项的状态文字是核查结论，不可更改。"
    "用一句话（不超过 200 字）写这一天的主线：做了哪几类事、围绕什么。只描述主线，"
    "不要说任何事情已完成、已部署、已合并或通过——完成与否由清单陈述。必须调用 write_summary 回答。"
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
    "description": "汇报这一天的工作报告草稿；字段含义见系统指令中的报告格式。摘要由运行时核查后另行生成。",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "description": "按工作事项归并的活动、产出与进展。",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": f"不超过 {_TITLE} 字。"},
                        "activity": {
                            "type": "string",
                            "description": f"不超过 {_TEXT} 字，超出会在句末截断。",
                        },
                        "progress": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": _MAX_PARTS,
                            "description": (
                                "这一项的进展，按部分分开：代码、测试、部署、申请等各一条，"
                                "各带自己的 status 和 refs；单一事项只填一条，part 为 null。"
                                "completed 是你的声称，运行时会对照 refs 的原文核查。"
                            ),
                            "items": _claim_schema(
                                (
                                    "part",
                                    {
                                        "type": ["string", "null"],
                                        "description": (
                                            f"部分名，不超过 {_PART} 字；单一事项为 null。"
                                        ),
                                    },
                                ),
                                ("status", {"type": "string", "enum": list(STATUSES)}),
                            ),
                        },
                    },
                    "required": ["title", "activity", "progress"],
                },
            },
            "decisions": {
                "type": "array",
                "maxItems": _MAX_DECISIONS,
                "description": "重要决定与方案变化；rationale 有依据才填，否则 null。",
                "items": _claim_schema(
                    ("text", {"type": "string", "description": f"不超过 {_TEXT} 字。"}),
                    (
                        "rationale",
                        {"type": ["string", "null"], "description": f"不超过 {_SHORT} 字。"},
                    ),
                ),
            },
            "open_items": {
                "type": "array",
                "maxItems": _MAX_OPEN,
                "description": "未完成事项、阻碍与待确认问题。",
                "items": _claim_schema(("text", {"type": "string"})),
            },
            "user_next_steps": {
                "type": "array",
                "maxItems": _MAX_NEXT,
                "description": "Allen 本人明确表达的下一步；refs 必须含 who=allen 的记录键。",
                "items": _claim_schema(("text", {"type": "string"})),
            },
            "suggestions": {
                "type": "array",
                "maxItems": _MAX_SUGGESTIONS,
                "items": {"type": "string"},
                "description": "你提出的建议，与 Allen 的承诺分开。",
            },
            "uncertainties": {
                "type": "array",
                "maxItems": _MAX_UNCERTAINTIES,
                "items": {"type": "string"},
                "description": "不确定之处、证据冲突、材料缺口。",
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
        f"索取至多 {MAX_DETAILS} 条条目的完整原文（屏幕 OCR 全文、对话原文、提交内容与改动文件、"
        "Codex 会话逐轮全文），用材料或检索结果里的方括号键。"
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
        "在这一天的全部材料里按关键字检索（屏幕全文、窗口标题、对话记录、提交标题、Codex 会话全文），"
        f"返回命中的键和一行上下文，最多 {MAX_HITS} 条。用来核对某个事实（某次提交、某句话、"
        "某个页面、某个错误）当天是否真的出现过、出现在哪。可以和 request_details 同一轮调用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "关键字或短语"}},
        "required": ["query"],
    },
}
JUDGE_TOOL: dict[str, Any] = {
    "name": JUDGE_TOOL_NAME,
    "description": "对每个声称完成的部分给出核查结论。",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "part": {"type": "integer", "description": "部分编号，按给出的顺序。"},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "shows": {
                            "type": "string",
                            "description": f"原文实际显示了什么，不超过 {_SHOWS} 字。",
                        },
                        "screen": {
                            "type": ["string", "null"],
                            "enum": ["page", "agent", None],
                            "description": "屏幕文字来自第三方页面（page）还是代理自述（agent）。",
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
    "description": "这一天的主线，一句话。",
    "input_schema": {
        "type": "object",
        "properties": {
            "main_line": {"type": "string", "description": f"不超过 {_MAIN_LINE} 字。"}
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
        "## 代理会话（Codex 本机会话文件，逐轮全文；代理说的「已完成」「已合并」是它的自述，"
        "不是核实结果；Claude Code 会话未收录）"
    ]
    for row in rows:
        out.append(f"[{row['key']}] {row['first']}-{row['last']} {row['app']} @ {row['cwd']}")
        out += [f"  {turn['at']} {turn['role']}: {turn['text']}" for turn in row["turns"]]
    out.append("")
    return out


def render_material(evidence: DayEvidence) -> str:
    """Render the whole keyed day as the model's material."""
    sections = evidence.sections
    partial = "（这一天尚未结束，材料只到当前为止）" if evidence.window["partial"] else ""
    out: list[str] = [
        f"报告日期：{evidence.day}（{evidence.zone}）；"
        f"证据窗口 {evidence.window['from']} 到 {evidence.window['to']}{partial}。",
        "## 材料覆盖（没有采集到、不可读、还是全部给出）",
        *(f"- {text}" for text in evidence.served.values()),
        "",
    ]
    if evidence.limits:
        out += ["## 材料范围说明", *(f"- {limit}" for limit in evidence.limits), ""]
    out += _lines(
        "屏幕上的关键页面（自动匹配到「已提交/已收到/已发送」类短语的截屏，全文见屏幕内容）",
        sections.get("milestones", []),
        "[{key}] {first} {app} — {title}: …{text}…",
    )
    out += _lines(
        "各应用时长（分钟，TimeSink 估计；不是有效工作时长）",
        sections.get("apps", []),
        "- {app}: {minutes}（{spans} 段）",
    )
    out += _lines(
        "全部窗口（应用 — 标题，分钟，首次-最后；时间顺序）",
        sections.get("windows", []),
        "[{key}] {first}-{last} {app} — {title}（{minutes} 分钟，{spans} 段）",
    )
    out += _lines(
        "屏幕内容（每条截屏的 OCR 全文，时间顺序；×n = 同一窗口连续近似重复的截屏数）",
        sections.get("screen", []),
        "[{key}] {first}-{last} ×{count} {app} — {title}: {text}",
    )
    out += _lines(
        "TimeSink 状态事件（锁屏/睡眠/空闲/暂停等，解释空白时段）",
        sections.get("state_events", []),
        "- {at} {kind} {phase}",
    )
    out += _lines(
        "当天的对话记录（时间顺序，全文；who=allen 为 Allen 的原话）",
        sections.get("records", []),
        "[{key}] {at} {who}: {text}",
    )
    out += _lines(
        "当天的 Git 提交（本地仓库记录，同一提交跨 worktree 已合并；"
        "late=True 表示当天才看到的旧提交，不算当天工作）",
        sections.get("git", []),
        "[{key}] 提交于 {committed}，观察于 {observed}，{sha} {subject}（{paths}）"
        "late={late} {main}",
    )
    out += _session_lines(sections.get("agent", []))
    out += _lines(
        "当天观察到的仓库状态",
        sections.get("repo_states", []),
        "[{key}] {at} {repo} 分支 {branch}，未提交改动 {dirty} 个文件，HEAD {head}",
    )
    out += _lines(
        "上下文：未完成的本地待办（不是当天活动）",
        sections.get("todos", []),
        "[{key}] {title}（due {due_at}, {priority}, project {project}）",
    )
    out += _lines(
        "上下文：已保存的知识（不是当天活动）",
        sections.get("knowledge", []),
        "[{key}] {kind}: {statement}",
    )
    out += _lines(
        "上下文：前一天报告的核心摘要（用于解释延续和变化，不是当天活动）",
        sections.get("previous", []),
        "[{key}] {date}：{text}",
    )
    return "\n".join(out)


def build_request(evidence: DayEvidence) -> tuple[str, list[dict[str, Any]]]:
    """The skill's instructions as the system prompt plus one user message of material."""
    content = (
        f"以下是 {evidence.day} 的全部材料。可以先用 search_material 核对事实、用 request_details "
        "索取原文（最多三轮），然后调用 report_daily_work 汇报草稿。\n\n"
        f"{render_material(evidence)}"
    )
    return SKILL.instructions, [{"role": "user", "content": content}]


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
    item["progress"] = [_claim(part, "part", "status") for part in progress[:_MAX_PARTS]]
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
    """The 核心摘要 section of a saved report, for a tool result's preview."""
    _, found, rest = content.partition(_SUMMARY_HEADING)
    if not found:
        return content[:_SERVED]
    body = rest.split("\n## ", 1)[0]
    return " ".join(body.split())[:_SERVED]


# --- claims: what a completed part asserts, what the program rules, what the check found ---


@dataclass
class Claim:
    """One part of one item; a ``completed`` status is a claim until it is checked."""

    item: int
    part: str | None
    status: str
    refs: list[str]
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
    """Why a claim that needed the check never got one: 核查预算耗尽 or 核查失败."""

    @property
    def claimed(self) -> bool:
        return self.status == "completed"

    @property
    def needs_check(self) -> bool:
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
        return "question" if is_question(evidence.haystack.get(key, "")) else "user"
    if ref.startswith("timesink-capture:"):
        return "screen"
    if ref.startswith("codex-session:"):
        return "agent"
    return "other"


def _rule(claim: Claim, evidence: DayEvidence) -> None:
    """What the program can decide about a completed part before any model reads it."""
    kinds = set(claim.kinds.values())
    evidential = kinds & {"commit", "user", "screen", "agent"}
    if not claim.refs or kinds <= {"unknown"}:
        claim.ruling, claim.reason = "invalid", "没有引用材料里的键"
        return
    if not evidential:
        if "late_commit" in kinds:
            claim.reason = "引用的是当天才看到的旧提交，不是当天的工作"
        elif "question" in kinds:
            claim.reason = "引用的是 Allen 的提问或请求，不是确认"
        else:
            claim.reason = "引用的记录不能证明完成（Jarvis 自己的话或窗口时段）"
        claim.ruling = "invalid"
        return
    name = claim.part or ""
    if _MERGE.search(name) and "commit" in kinds:
        shas = [evidence.commit_rows[k] for k, kind in claim.kinds.items() if kind == "commit"]
        off = [row["sha"] for row in shas if row["main"] != "已在 main"]
        if off:
            claim.ruling, claim.reason = "invalid", f"提交 {'、'.join(off)} 未进 main"
        else:
            claim.ruling = "repo"
        return
    if _DEPLOY.search(name) and "user" not in kinds:
        if evidential <= {"commit"}:
            claim.ruling, claim.reason = "invalid", "提交不能证明部署、上线或重启发生了"
        else:
            claim.ruling = "self_report"
        return
    claim.ruling = "self_report" if evidential <= {"agent"} else "check"


def screen_claims(report: dict[str, Any], evidence: DayEvidence) -> list[Claim]:
    """Every part of every item as a claim, the completed ones ruled on by the program."""
    claims = []
    for index, item in enumerate(report["items"], 1):
        for part in item["progress"]:
            claim = Claim(index, part["part"], part["status"], list(part["refs"]))
            claim.kinds = {key: _kind_of(key, evidence) for key in claim.refs}
            if claim.claimed:
                _rule(claim, evidence)
            claims.append(claim)
    return claims


def title_terms(title: str) -> list[str]:
    """Words of an item's title that pick a session's relevant turns: 3+ letters or 2+ CJK."""
    return [
        w for w in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[一-鿿]{2,}", title)
    ]


def build_check_request(
    item: dict[str, Any], claims: list[Claim], originals: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """The verification call for one item: the claims in order, then the cited originals."""
    lines = [f"事项：{item['title']}", f"作者写的活动与进展：{item['activity']}", "", "声称完成的部分："]
    for number, claim in enumerate(claims, 1):
        lines.append(f"{number}. {claim.part or '整体'}（引用 {'、'.join(claim.refs) or '无'}）")
    lines += ["", "引用的原文："]
    for original in originals:
        lines += [f"[{original['key']}] 类型={original['kind']}", original["text"], ""]
    return JUDGE_SYSTEM, [{"role": "user", "content": "\n".join(lines)}]


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
    content = f"{evidence.day} 的事项清单（核查后）：\n" + "\n".join(table)
    return SUMMARY_SYSTEM, [{"role": "user", "content": content}]


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
        in 证据引用, so a claim citing it many times cannot grow the report.
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
    flag = mains.pop() if len(mains) == 1 else "部分已在 main"
    return f"提交 {'、'.join(row['sha'] for row in rows)}，{flag}"


def _keys_of(claim: Claim, kind: str) -> str:
    return "、".join(k for k, v in claim.kinds.items() if v == kind)


def wording(claim: Claim, evidence: DayEvidence) -> tuple[str, bool]:
    """The saved status text of a part, and whether it is evidenced (a commit, Allen, a page)."""
    if not claim.claimed:
        return _STATUS_LABELS[claim.status], False
    if claim.ruling == "invalid":
        return f"声称完成，引用无效：{claim.reason}", False
    if claim.ruling == "repo":
        return f"已合并到 main（{_commits_named(claim, evidence)}）", True
    if claim.ruling == "self_report":
        return f"据代理自述已完成，尚未核实（{_keys_of(claim, 'agent') or _keys_of(claim, 'screen')}）", False
    if claim.unchecked:
        return f"声称完成，未核实（{claim.unchecked}）", False
    if claim.verdict == "unsupported":
        return f"声称完成，引用不支持（原文显示：{claim.shows}）", False
    if claim.verdict == "partial":
        return f"部分完成：{claim.shows}", False
    parts: list[str] = []
    if "user" in claim.kinds.values():
        parts.append(f"用户确认完成（{_keys_of(claim, 'user')}）")
    if named := _commits_named(claim, evidence):
        parts.append(f"已提交（{named}）")
    if "screen" in claim.kinds.values():
        if claim.screen == "agent":
            parts.append(f"据代理自述已完成，尚未核实（{_keys_of(claim, 'screen')}）")
        else:
            parts.append(f"页面显示已完成（{_keys_of(claim, 'screen')}）")
    if "agent" in claim.kinds.values() and not parts:
        parts.append(f"据代理自述已完成，尚未核实（{_keys_of(claim, 'agent')}）")
    evidenced = any(p.startswith(("用户确认", "已提交", "页面显示")) for p in parts)
    return "；".join(parts) or "声称完成，引用不支持", evidenced


def _ref_line(numbers: list[int]) -> str:
    """Citation numbers; 证据引用 at the foot of the report resolves every one of them."""
    return f"{_REF_PREFIX}{', '.join(f'#{n}' for n in numbers)}" if numbers else f"{_REF_PREFIX}无"


def _bullets(rows: list[dict[str, Any]], composer: _Composer, empty: str) -> list[str]:
    if not rows:
        return [f"- {empty}"]
    out = []
    for row in rows:
        refs = composer.refs(row["refs"])
        line = f"- {row['text']}"
        if row.get("rationale"):
            line += f"　理由：{row['rationale']}"
        out.append(f"{line}（{_ref_line(refs)}）")
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
                notes.append(f"第 {index} 项正文提到的提交号 {short} 不在当天的提交里")
            elif known[short] not in cited:
                notes.append(f"第 {index} 项正文提到提交 {short}（{known[short]}）但没有引用它")
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
        breakdown = "；".join(
            (f"{c.part}：" if c.part else "") + text for c, (text, _) in zip(mine, words, strict=True)
        )
        line = f"{index} {item['title']}（{breakdown}）"
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
    """核心摘要: one model sentence on the day's main line, then the program's verified lines."""
    table, evidenced, unverified, counts = _table(report, claims, evidence)
    if main_line is None:
        titles = "、".join(item["title"] for item in report["items"][:3])
        main_line = f"这一天的主要事项：{titles}。" if titles else "这一天没有归并出工作事项。"
    out = [main_line]
    if evidenced:
        out.append("有实证：" + "、".join(evidenced))
    if unverified:
        out.append("声称完成但未核实或不成立：" + "、".join(unverified))
    rest = [f"{heading} {counts[status]} 项" for status, heading in _REST if counts.get(status)]
    if rest:
        out.append(f"另有{'、'.join(rest)}，见工作事项。")
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
    核心摘要 is built from the rulings; each cited source is written out
    once, in 证据引用.
    """
    composer = _Composer(evidence)
    partial = "，这一天尚未结束" if evidence.window["partial"] else ""
    lines = ["## 工作事项"]
    unproven: list[str] = []
    for index, item in enumerate(report["items"], 1):
        numbers: list[int] = []
        heads: list[str] = []
        for claim in (c for c in claims if c.item == index):
            found = composer.refs(claim.refs)
            numbers += [n for n in found if n not in numbers]
            text, proven = wording(claim, evidence)
            heads.append((f"{claim.part}：" if claim.part else "") + text)
            if claim.claimed and not proven:
                unproven.append(f"第 {index} 项" + (f"（{claim.part}）" if claim.part else "") + f"：{text}")
        lines += [
            f"### {index}. {item['title']} — {'；'.join(heads)}",
            item["activity"],
            _ref_line(numbers),
            "",
        ]
    if not report["items"]:
        lines += ["- 材料中未能归并出明确的工作事项。", ""]
    lines += [
        "## 重要决定与方案变化",
        *_bullets(report["decisions"], composer, "材料中未见明确的决定或方案变化。"),
        "",
        "## 未完成、阻碍与待确认",
        *_bullets(report["open_items"], composer, "材料中未见明确的未完成事项或阻碍。"),
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
        "## 用户明确表达的下一步",
        *_bullets(kept, composer, "材料中没有 Allen 本人明确表达的下一步。"),
        "",
        "## 建议（模型提出，非用户承诺）",
    ]
    lines += [f"- {text}" for text in report["suggestions"]] or ["- 无。"]
    lines += ["", "## 数据覆盖与不确定性"]
    lines += [f"- {text}" for text in evidence.served.values()]
    counts = evidence.counts
    lines.append(
        f"- 材料规模：窗口 {counts.get('windows', 0)} 个、截屏 {counts.get('screen', 0)} 条、"
        f"对话记录 {counts.get('records', 0)} 条、Git 提交 {counts.get('git', 0)} 个、"
        f"Codex 会话 {counts.get('agent', 0)} 个、状态事件 {counts.get('state_events', 0)} 条"
        + (f"；最后观察到 {evidence.observed_until}" if evidence.observed_until else "")
        + "。没有记录不代表没有活动。"
    )
    lines += [f"- 材料范围：{limit}" for limit in evidence.limits]
    lines.append(f"- {_NOT_VERIFIED}")
    if composer.unknown:
        lines.append(f"- 有 {composer.unknown} 处引用不是材料里的键，已丢弃。")
    if unproven:
        lines.append("- 声称完成但没有实证的部分：" + "；".join(unproven) + "。")
    lines += [f"- {note}。" for note in _prose_shas(report, evidence)]
    if moved:
        quoted = "".join(f"「{text}」" for text in moved)
        lines.append(
            f"- 模型把 {len(moved)} 条内容当作 Allen 明确表达的下一步，但引用的不是他的原话，"
            f"已从该节移除：{quoted}"
        )
    lines += [f"- {text}" for text in report["uncertainties"]]
    lines += [
        "",
        "## 证据引用",
        f"- 共 {len(composer.cited)} 个来源，正文按编号引用；其中前 "
        f"{min(len(composer.cited), MAX_SOURCE_REFS)} 个另存为 source_refs（字段上限）；"
        "用 read_activity / read_records 回查原文。",
        *(f"#{number} {ref}" for number, ref in enumerate(composer.cited, 1)),
    ]
    head = [
        f"# 工作日报 {evidence.day}（{evidence.zone}）",
        f"生成于 {generated_at.isoformat(timespec='seconds')}；证据窗口 "
        f"{evidence.window['from']} 到 {evidence.window['to']}{partial}；模型 {model}。",
        "",
        _SUMMARY_HEADING,
        # Downstream readers are served this section alone: the one model sentence never
        # travels without the program's lines saying what was evidenced and what was not.
        *_summary_lines(report, claims, evidence, main_line),
        "",
    ]
    return (
        _fit("\n".join([*head, *lines])),
        composer.cited[:MAX_SOURCE_REFS],
        dict(evidence.coverage),
    )
