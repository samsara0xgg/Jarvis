"""Daily work report request and reply handling (ADR 0024): the skill's model call.

The skill's instructions are the system prompt; the day's evidence is keyed
material; the model may ask once for the originals behind a few keys and must
then report through ``report_daily_work``. The report's rules are enforced
here when the saved text is composed, never by prompt alone.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

from jarvis.shared.skills import load_skill
from jarvis.state.daily_report import MAX_DETAILS

if TYPE_CHECKING:
    from datetime import datetime

    from jarvis.decision.llm import ChatResult
    from jarvis.state.daily_report import DayEvidence

SKILL = load_skill("daily-work-report")
REPORT_TOOL_NAME = "report_daily_work"
DETAILS_TOOL_NAME = "request_details"
REPORT_NOW = "以上就是你要的原文。材料到此为止，现在必须调用 report_daily_work 汇报这一天。"
"""Served with the details: a model that keeps asking instead of reporting is told to report."""
REPORT_AGAIN = "上一次汇报无法解析，请按 schema 重新调用 report_daily_work，内容可以更简短。"
"""Served after an unusable reply: providers malform or truncate long arguments now and then."""
STATUSES = ("browsed", "discussed", "attempted", "completed")
CONTENT_LIMIT = 44000
"""Sized for the whole day cited: every source written once, plus capped prose and ref numbers.

ponytail: not a proof — a model that cites every key in every claim still overruns and is
truncated with a note. Cap the refs per claim if that ever stops being hypothetical.
"""
MAX_SOURCE_REFS = 20
_STATUS_LABELS = {
    "browsed": "浏览",
    "discussed": "讨论",
    "attempted": "尝试/进行中",
    "completed": "完成",
}
# The grade is the class of the cited source. It never says the source supports the claim, and
# it never clears a status: only the report model can read a quote, so 完成 stays the model's.
_GRADE_LABELS = {
    "confirmed": "引用来源：当天的 Git 提交，或 Allen 本人的记录",
    "screen": "引用来源：仅屏幕/应用记录",
    "inferred": "引用来源：无有效引用",
}
_GRADE_SHORT = {
    "confirmed": "当天提交或本人记录",
    "screen": "仅屏幕/应用记录",
    "inferred": "无有效引用",
}
_NOT_VERIFIED = (
    "状态（浏览/讨论/尝试/完成）是报告作者的判断；"
    "运行时只标注每条引用的来源种类，既不核实来源是否支持这条结论，"
    "也不核实事情是否真的做完。"
)
_MAX_ITEMS = 12
_MAX_DECISIONS = 8
_MAX_OPEN = 10
_MAX_NEXT = 8
_MAX_SUGGESTIONS = 6
_MAX_UNCERTAINTIES = 10
_TITLE = 80
_TEXT = 400
_SHORT = 240
_SUMMARY = 1200
_SERVED = 3000
"""``summary_of`` serves the whole 核心摘要 section, digest included, not just the prose."""
_SUMMARY_HEADING = "## 核心摘要"
_REF_PREFIX = "引用："


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
    "description": "汇报这一天的工作报告；字段含义见系统指令中的报告格式。",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "核心摘要，2 到 6 句。"},
            "items": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "description": "按工作事项归并的活动、产出与进展。",
                "items": _claim_schema(
                    ("title", {"type": "string"}),
                    ("status", {"type": "string", "enum": list(STATUSES)}),
                    ("activity", {"type": "string"}),
                ),
            },
            "decisions": {
                "type": "array",
                "maxItems": _MAX_DECISIONS,
                "description": "重要决定与方案变化；rationale 有依据才填，否则 null。",
                "items": _claim_schema(
                    ("text", {"type": "string"}),
                    ("rationale", {"type": ["string", "null"]}),
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
            "summary",
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
        f"索取至多 {MAX_DETAILS} 条关键条目的原文（屏幕 OCR 全文、对话原文、Git 载荷），"
        "用材料里的方括号键。只能调用一次，之后必须用 report_daily_work 汇报。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_DETAILS}
        },
        "required": ["keys"],
    },
}


class DailyReportParseError(ValueError):
    """The model's reply was not a usable report."""


def _lines(section: str, rows: list[dict[str, Any]], render: str) -> list[str]:
    if not rows:
        return []
    return [f"## {section}", *(render.format(**row) for row in rows), ""]


def render_material(evidence: DayEvidence) -> str:
    """Render the keyed day as the model's only material."""
    sections = evidence.sections
    partial = "（这一天尚未结束，材料只到当前为止）" if evidence.window["partial"] else ""
    out: list[str] = [
        f"报告日期：{evidence.day}（{evidence.zone}）；"
        f"证据窗口 {evidence.window['from']} 到 {evidence.window['to']}{partial}。",
        "来源覆盖：" + ", ".join(f"{k}={v}" for k, v in evidence.coverage.items()) + "。",
        "",
    ]
    if evidence.limits:
        out += ["## 材料范围说明", *(f"- {limit}" for limit in evidence.limits), ""]
    out += _lines(
        "各应用时长（分钟，TimeSink 估计；不是有效工作时长）",
        sections.get("apps", []),
        "- {app}: {minutes}（{spans} 段）",
    )
    out += _lines(
        "主要窗口（应用 — 标题，分钟，首次-最后）",
        sections.get("windows", []),
        "[{key}] {first}-{last} {app} — {title}（{minutes} 分钟，{spans} 段）",
    )
    out += _lines(
        "屏幕内容（同一窗口同一小时归并；×n = 该小时内容变化次数；"
        "文字为 OCR 摘要，原文用 request_details）",
        sections.get("screen", []),
        "[{key}] {hour} ×{count} {app} — {title}: {text}",
    )
    out += _lines(
        "TimeSink 状态事件（锁屏/睡眠/空闲/暂停等，解释空白时段）",
        sections.get("state_events", []),
        "- {at} {kind} {phase}",
    )
    out += _lines(
        "当天的对话记录（时间顺序；who=allen 为 Allen 的原话）",
        sections.get("records", []),
        "[{key}] {at} {who}: {text}",
    )
    out += _lines(
        "当天观察到的 Git 提交（同一提交跨 worktree 已合并；"
        "late=True 表示当天才看到的旧提交，不算当天工作）",
        sections.get("git", []),
        "[{key}] 提交于 {committed}，观察于 {observed}，{sha} {subject}（{paths}）late={late}",
    )
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
        "上下文：前一天报告的摘录（用于解释延续和变化，不是当天活动）",
        sections.get("previous", []),
        "[{key}] {date}：{text}",
    )
    return "\n".join(out)


def build_request(evidence: DayEvidence) -> tuple[str, list[dict[str, Any]]]:
    """The skill's instructions as the system prompt plus one user message of material."""
    content = (
        f"以下是 {evidence.day} 的材料。可以先用 request_details 索取关键条目原文，"
        "然后调用 report_daily_work 汇报。\n\n"
        f"{render_material(evidence)}"
    )
    return SKILL.instructions, [{"role": "user", "content": content}]


def requested_details(result: ChatResult) -> dict[str, list[str]] | None:
    """Keys per ``request_details`` call, or None when the model already reported."""
    if any(call.name == REPORT_TOOL_NAME for call in result.tool_calls):
        return None
    requests: dict[str, list[str]] = {}
    for call in result.tool_calls:
        if call.name != DETAILS_TOOL_NAME:
            continue
        try:
            raw = json.loads(call.arguments_json)
        except ValueError:
            raw = {}
        keys = raw.get("keys") if isinstance(raw, dict) else None
        wanted = [str(k) for k in keys if isinstance(k, str)] if isinstance(keys, list) else []
        requests[call.call_id] = wanted[:MAX_DETAILS]
    return requests or None


def _text(value: Any, limit: int) -> str:  # noqa: ANN401 — model output.
    return " ".join(str(value).split())[:limit]


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
        if name == "rationale":
            claim[name] = _text(value, _SHORT) if isinstance(value, str) and value.strip() else None
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


def _texts(raw: Any, limit: int) -> list[str]:  # noqa: ANN401 — model output.
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        _malformed("list is not text")
    return [_text(x, _SHORT) for x in raw if x.strip()][:limit]


def parse_report(result: ChatResult) -> dict[str, Any]:
    """Normalize the forced tool call (or a bare JSON reply) into the report shape.

    Every required field must be present with the schema's type; a partial
    reply raises, so a malformed answer never becomes a saved report.
    """
    raw: Any = None
    for call in result.tool_calls:
        if call.name == REPORT_TOOL_NAME:
            try:
                raw = json.loads(call.arguments_json)
            except ValueError as exc:
                msg = "report_daily_work arguments are not JSON"
                raise DailyReportParseError(msg) from exc
            break
    if raw is None and result.text:
        text = result.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            raw = json.loads(text)
        except ValueError as exc:
            msg = "model replied without a report_daily_work call"
            raise DailyReportParseError(msg) from exc
    if not isinstance(raw, dict):
        msg = "model replied without a report_daily_work call"
        raise DailyReportParseError(msg)
    if not isinstance(raw.get("summary"), str) or not raw["summary"].strip():
        _malformed("summary is missing")
    for key in ("items", "decisions", "open_items", "user_next_steps"):
        if not isinstance(raw.get(key), list):
            _malformed(f"{key} is not a list")
    report: dict[str, Any] = {
        "summary": _text(raw["summary"], _SUMMARY),
        "items": [_claim(x, "title", "status", "activity") for x in raw["items"]][:_MAX_ITEMS],
        "decisions": [_claim(x, "text", "rationale") for x in raw["decisions"]][:_MAX_DECISIONS],
        "open_items": [_claim(x, "text") for x in raw["open_items"]][:_MAX_OPEN],
        "user_next_steps": [_claim(x, "text") for x in raw["user_next_steps"]][:_MAX_NEXT],
        "suggestions": _texts(raw.get("suggestions"), _MAX_SUGGESTIONS),
        "uncertainties": _texts(raw.get("uncertainties"), _MAX_UNCERTAINTIES),
    }
    return report


def summary_of(content: str) -> str:
    """The 核心摘要 section of a saved report, for a tool result's preview."""
    _, found, rest = content.partition(_SUMMARY_HEADING)
    if not found:
        return content[:_SUMMARY]
    body = rest.split("\n## ", 1)[0]
    return " ".join(body.split())[:_SERVED]


class _Composer:
    """Turn a parsed report into the saved text, enforcing the evidence rules."""

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

    def source(self, numbers: list[int]) -> list[str]:
        return [self.cited[number - 1] for number in numbers]

    def grade(self, numbers: list[int]) -> str:
        refs = self.source(numbers)
        if any(r in self.evidence.commits or r in self.evidence.stated for r in refs):
            return "confirmed"
        if any(r.startswith("timesink") for r in refs):
            return "screen"
        return "inferred"

    def stated(self, numbers: list[int]) -> bool:
        return any(r in self.evidence.stated for r in self.source(numbers))


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
    """Last resort only: the capped prose and one line per cited source fit by construction."""
    if len(content) <= CONTENT_LIMIT:
        return content
    note = "\n…（报告超出保存上限，已截断。）"
    return content[: CONTENT_LIMIT - len(note)] + note


def _digest(marks: list[str], moved: int) -> list[str]:
    """Every item's status and grade, by name, inside 核心摘要.

    ``summary_of`` serves this section alone to the conversation, so the prose
    alone would be the whole report downstream. The digest travels with it: a
    summary that contradicts the body is contradicted in the same breath, and
    the reader is told which item, not merely how many.
    """
    if not marks:
        return [f"（逐项引用来源：本报告没有归并出工作事项。{_NOT_VERIFIED}）"]
    tail = f"另有 {moved} 条“下一步”没有 Allen 原话依据，已归入建议。" if moved else ""
    return [f"（逐项引用来源：{'；'.join(marks)}。{tail}{_NOT_VERIFIED}）"]


def compose_report(
    report: dict[str, Any],
    evidence: DayEvidence,
    *,
    model: str,
    generated_at: datetime,
) -> tuple[str, list[str], dict[str, str]]:
    """The saved content, its first 20 source refs and its coverage.

    Keys the model was not given are dropped and counted; every claim is graded
    by the class of source it cites, which never certifies the claim itself and
    never alters the status the model chose; a next step that does not cite
    Allen's own words becomes a suggestion; every item's status and grade are
    restated by name inside 核心摘要, the only section the conversation is
    served; each cited source is written out once, in 证据引用.
    """
    composer = _Composer(evidence)
    partial = "，这一天尚未结束" if evidence.window["partial"] else ""
    lines = ["## 工作事项"]
    marks: list[str] = []
    thin = 0
    for index, item in enumerate(report["items"], 1):
        refs = composer.refs(item["refs"])
        grade = composer.grade(refs)
        label = _STATUS_LABELS[item["status"]]
        if item["status"] == "completed" and grade != "confirmed":
            thin += 1
        marks.append(f"{index} {item['title']}／{label}／{_GRADE_SHORT[grade]}")
        lines += [
            f"### {index}. {item['title']} — {label}［{_GRADE_LABELS[grade]}］",
            item["activity"],
            _ref_line(refs),
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
    suggestions = [*report["suggestions"], *moved]
    lines += [f"- {text}" for text in suggestions] or ["- 无。"]
    lines += ["", "## 数据覆盖与不确定性"]
    covered = "，".join(f"{k}={v}" for k, v in evidence.coverage.items())
    lines.append(f"- 来源覆盖：{covered}。")
    counts = evidence.counts
    lines.append(
        f"- 材料规模：应用 {counts.get('apps', 0)} 个、窗口 {counts.get('windows', 0)} 个、"
        f"屏幕时段 {counts.get('screen', 0)} 个、对话记录 {counts.get('records', 0)} 条、"
        f"Git 提交 {counts.get('git', 0)} 个、状态事件 {counts.get('state_events', 0)} 条"
        + (f"；最后观察到 {evidence.observed_until}" if evidence.observed_until else "")
        + "。"
    )
    lines += [f"- 材料范围：{limit}" for limit in evidence.limits]
    lines.append(f"- {_NOT_VERIFIED}")
    if composer.unknown:
        lines.append(f"- 有 {composer.unknown} 处引用不是材料里的键，已丢弃。")
    if thin:
        lines.append(
            f"- 有 {thin} 项标为完成的事项只有屏幕/应用记录或没有有效引用，"
            "逐项见核心摘要的引用来源一览。"
        )
    if moved:
        lines.append(f"- 有 {len(moved)} 条“下一步”没有 Allen 原话依据，已归入建议。")
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
        # The digest leads the section: downstream readers are served this section alone.
        *_digest(marks, len(moved)),
        report["summary"],
        "",
    ]
    return (
        _fit("\n".join([*head, *lines])),
        composer.cited[:MAX_SOURCE_REFS],
        dict(evidence.coverage),
    )
