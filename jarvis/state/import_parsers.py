"""ADR 0259: the parsers that turn a file he handed over into dated entries.

One function per source, each ``(file name, bytes, fallback instant) -> entries``; ``PARSERS`` is
the registry, so a new source is one function and one line. A parser reads facts only (an amount, a
time, a counterparty), never judges them. Amounts are integer fen (1/100 CNY), never floats.

The three Chinese statements print local time with no zone and are read as Asia/Shanghai; the
Claude export carries its own offset. Every entry holds the UTC instant and the zone it was
written in.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from pypdf.errors import PyPdfError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
CHINA: Final = "Asia/Shanghai"
_CHINA: Final = ZoneInfo(CHINA)

SUCCESS: Final = frozenset({
    "支付成功", "交易成功", "已转账", "已收钱", "朋友已收钱", "对方已收钱", "已收款",
    "提现已到账", "充值完成", "充值成功", "还款成功", "已存入零钱", "等待确认收货",
})
"""Statuses of money that moved (an order awaiting delivery is already paid). A refund, a closed
or a failed order is shown, not summed."""
_DIRECTIONS: Final = {"支出": "out", "收入": "in"}


@dataclass(frozen=True)
class Parsed:
    """One entry as a parser reads it, before the file's digest is attached."""

    source: str
    key: str
    pos: str
    at_ms: int
    zone: str
    kind: str
    text: str
    fields: dict[str, Any]


def _ms(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


# --- Claude.ai data export -----------------------------------------------------------------


def _iso_ms(text: object, fallback_ms: int) -> int:
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return fallback_ms
    return _ms(moment if moment.tzinfo else moment.replace(tzinfo=UTC))


def _objects(data: bytes) -> list[dict[str, Any]]:
    loaded = json.loads(data)
    many = loaded if isinstance(loaded, list) else [loaded]
    return [one for one in many if isinstance(one, dict)]


_PLACEHOLDER: Final = re.compile(
    r"(?:`{3}[ \t]*\n)?This block is not supported on your current device yet\.(?:[ \t]*\n?`{3})?",
)
"""What the export writes where a tool call or its result was: no words of his or of Claude's."""


def strip_placeholders(text: str) -> str:
    """``text`` without the export's stand-ins for tool blocks, and the blank lines they leave."""
    return re.sub(r"\n{3,}", "\n\n", _PLACEHOLDER.sub("", text)).strip()


def _message_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return strip_placeholders(text)
    blocks = message.get("content")
    parts = (
        [b.get("text", "") for b in blocks if isinstance(b, dict)]
        if isinstance(blocks, list) else []
    )
    return strip_placeholders("\n".join(p for p in parts if isinstance(p, str) and p.strip()))


def _chat_messages(
    chat: dict[str, Any], messages: object, where: str, fallback_ms: int,
) -> Iterator[Parsed]:
    chat_id = str(chat.get("uuid") or "")
    name = str(chat.get("name") or chat.get("title") or "")
    started = _iso_ms(chat.get("created_at"), fallback_ms)
    for i, message in enumerate(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or not (text := _message_text(message)):
            continue
        at = _iso_ms(message.get("created_at"), started)
        sender = str(message.get("sender") or message.get("role") or "")
        key = str(message.get("uuid") or _digest(chat_id, str(at), sender, text))
        fields: dict[str, Any] = {
            "conversation": chat_id, "conversation_name": name, "sender": sender,
        }
        if chat.get("project"):
            fields["project"] = chat["project"]
        yield Parsed("claude", key, f"{where} message {i + 1}", at, "UTC", "message", text, fields)


def _conversations(_name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    for n, chat in enumerate(_objects(data), 1):
        yield from _chat_messages(chat, chat.get("chat_messages"), f"conversation {n}", fallback_ms)


def _design_chats(_name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    for n, chat in enumerate(_objects(data), 1):
        yield from _chat_messages(chat, chat.get("messages"), f"design chat {n}", fallback_ms)


def _memories(_name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    for one in _objects(data):
        files = [f for f in one.get("memory_files") or [] if isinstance(f, dict)]
        newest = max(
            (_iso_ms(f.get("updated_at"), fallback_ms) for f in files), default=fallback_ms,
        )
        if isinstance(summary := one.get("conversations_memory"), str) and summary.strip():
            yield Parsed(
                "claude", f"memory:{_digest(summary)}", "conversations_memory", newest, "UTC",
                "memory", summary.strip(), {"part": "conversations_memory"},
            )
        for i, mem in enumerate(files, 1):
            path, content = str(mem.get("path") or ""), str(mem.get("content") or "").strip()
            if not content:
                continue
            at = _iso_ms(mem.get("updated_at"), fallback_ms)
            yield Parsed(
                "claude", f"memory:{path}:{at}:{_digest(content)}", f"memory_files {i}", at, "UTC",
                "memory", f"{path}\n{content}", {"part": "memory_file", "path": path},
            )


def _reflections(_name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    for one in _objects(data):
        for i, reflection in enumerate(one.get("reflections") or [], 1):
            if not isinstance(reflection, dict):
                continue
            period = str(reflection.get("period") or "")
            content = reflection.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if not text.strip():
                continue
            at = _iso_ms(period[:10], fallback_ms)
            yield Parsed(
                "claude", f"reflection:{period}:{_digest(text)}", f"reflections {i}", at, "UTC",
                "reflection", text, {"period": period},
            )


_CLAUDE_FOLDERS: Final[dict[str, Callable[[str, bytes, int], Iterator[Parsed]]]] = {
    "memories": _memories, "reflections": _reflections, "design_chats": _design_chats,
}


def claude(folder: str, name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    """Entries of one file of a Claude export, by the folder it sits in; other files give none."""
    if not name.endswith(".json"):
        return iter(())
    if (parse := _CLAUDE_FOLDERS.get(folder)) is not None:
        return parse(name, data, fallback_ms)
    return _conversations(name, data, fallback_ms) if name.startswith("conversations") else iter(())


def claude_keeps(name: str) -> bool:
    """Whether a file of a Claude export is kept at all: its login history is not."""
    return not name.startswith(("light_metadata", "."))


# --- the payment statements ----------------------------------------------------------------


def _fen(text: str) -> int | None:
    try:
        return int((Decimal(re.sub(r"[¥￥,\s]", "", text)) * 100).to_integral_value())
    except InvalidOperation:
        return None


def money(fen: int) -> str:
    """``6800`` as ``68.00``."""
    return f"{fen // 100}.{fen % 100:02d}"


def _payment(  # noqa: PLR0913 - one statement row, as its columns name it
    source: str, row: dict[str, str], pos: str, order: str, counterparty: str, item: str,
    direction: str, amount: str, status: str, method: str,
) -> Parsed | None:
    try:
        local = datetime.strptime(row["交易时间"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CHINA)
    except (KeyError, ValueError):
        return None
    fen = _fen(amount)
    if fen is None:
        return None
    way = _DIRECTIONS.get(direction, "neutral")
    counts = status in SUCCESS and way != "neutral"
    verb = {"out": "paid", "in": "received", "neutral": "moved"}[way]
    text = f"{verb} CNY {money(fen)} {'to' if way == 'out' else 'from'} {counterparty}: {item}"
    if status not in SUCCESS:
        text += f" [{status}]"
    fields = {
        "amount_fen": fen, "currency": "CNY", "direction": way, "counterparty": counterparty,
        "item": item, "status": status, "method": method, "order": order, "counts": counts,
    }
    key = order or _digest(source, *(f"{k}={v}" for k, v in sorted(row.items())))
    return Parsed(source, key, pos, _ms(local), CHINA, "payment", text, fields)


def _cells(sheet: bytes, strings: list[str]) -> Iterator[tuple[int, dict[str, str]]]:
    """``(row number, {column letters: text})`` of an xlsx sheet, in file order."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in ET.fromstring(sheet).iter(f"{ns}row"):  # noqa: S314 - his own export
        cells: dict[str, str] = {}
        for cell in row.iter(f"{ns}c"):
            kind, value = cell.get("t"), cell.find(f"{ns}v")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{ns}t"))
            elif value is None or value.text is None:
                continue
            else:
                text = strings[int(value.text)] if kind == "s" else value.text
            cells[re.sub(r"\d", "", cell.get("r", ""))] = text.strip()
        yield int(row.get("r", "0")), cells


_EXCEL_EPOCH: Final = datetime(1899, 12, 30)  # noqa: DTZ001 - a day count, read as China time


def wechat_pay(_name: str, data: bytes, _fallback_ms: int) -> Iterator[Parsed]:
    """WeChat Pay's statement .xlsx: a preamble, then a header row found by its column names."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        names = book.namelist()
        strings = (
            ["".join(t.text or "" for t in si.iter(f"{ns}t"))
             for si in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{ns}si")]  # noqa: S314
            if "xl/sharedStrings.xml" in names else []
        )
        sheet = book.read(min(n for n in names if n.startswith("xl/worksheets/sheet")))
    header: dict[str, str] | None = None
    for number, cells in _cells(sheet, strings):
        if header is None:
            if "交易时间" in cells.values() and any(v.startswith("金额") for v in cells.values()):
                header = dict(cells)
            continue
        row = {header[col]: text for col, text in cells.items() if col in header}
        if re.fullmatch(r"\d+(\.\d+)?", serial := row.get("交易时间", "")):
            # The real export stores the time as an Excel day number, China time.
            at = _EXCEL_EPOCH + timedelta(seconds=round(float(serial) * 86400))
            row["交易时间"] = at.strftime("%Y-%m-%d %H:%M:%S")
        amount = next((v for k, v in row.items() if k.startswith("金额")), "")
        parsed = _payment(
            "wechat_pay", row, f"row {number}", row.get("交易单号", ""), row.get("交易对方", ""),
            row.get("商品", ""), row.get("收/支", ""), amount, row.get("当前状态", ""),
            row.get("支付方式", ""),
        )
        if parsed is not None:
            yield parsed


def alipay(_name: str, data: bytes, _fallback_ms: int) -> Iterator[Parsed]:
    """Alipay's statement .csv (GBK): a preamble of lines, then the header line and the rows."""
    lines = data.decode("gb18030", errors="replace").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "交易时间" in line and "交易订单号" in line), None,
    )
    if start is None:
        return
    reader = csv.reader(lines[start:])
    header = [name.strip() for name in next(reader)]
    for offset, cells in enumerate(reader, 1):
        row = {name: cell.strip() for name, cell in zip(header, cells, strict=False) if name}
        parsed = _payment(
            "alipay", row, f"line {start + offset + 1}", row.get("交易订单号", ""),
            row.get("交易对方", ""), row.get("商品说明", ""), row.get("收/支", ""),
            row.get("金额", ""), row.get("交易状态", ""), row.get("收/付款方式", ""),
        )
        if parsed is not None:
            yield parsed


# --- the Bank of China debit statement (交易流水明细清单 PDF) ---------------------------------

_BOC_ROW: Final = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(\S+)\s+(-?[\d,]+\.\d{2})\s+"
    r"(-?[\d,]+\.\d{2})\s+(\S+)\s+(\S+)(?:\s+(.*))?$",
)
_BOC_PAGE_WORDS: Final = (
    "交易区间", "客户姓名", "借记卡号", "账号", "页数", "行数", "打印时间", "交易流水明细",
    "记账日期", "END",
)
_BOC_MAX_ACCOUNT: Final = 8
_BOC_CURRENCY: Final = {"人民币": "CNY"}


def _boc_cells(tokens: list[list[Any]]) -> tuple[str, str]:
    """``(text, short account)`` of a row's tail: cells split where the gap is 2+ spaces.

    A ``-----`` placeholder is empty. Digit runs of nine or more are dropped everywhere, and the
    last cell, when it is one word after another cell, is the counterparty's account: kept only
    if it is short (ADR 0259: never a long card or account number).
    """
    cells: list[str] = []
    for start, _end, text, gap in tokens:
        del start
        if gap >= 2 or not cells:  # noqa: PLR2004 - two spaces split cells
            cells.append(text)
        else:
            cells[-1] += f" {text}"
    cells = [
        c for c in (re.sub(r"\d{9,}", "", re.sub(r"^-{5,}|-{5,}$", "", c)).strip() for c in cells)
        if c
    ]
    account = ""
    if len(cells) >= 2 and " " not in cells[-1]:  # noqa: PLR2004
        last = cells.pop()
        account = last if len(last) <= _BOC_MAX_ACCOUNT else ""
    return " ".join(cells), account


def _boc_row(line: str, number: int) -> tuple[re.Match[str], list[list[Any]]] | None:
    found = _BOC_ROW.match(line)
    if found is None:
        return None
    tokens: list[list[Any]] = []
    end = None
    for word in re.finditer(r"\S+", line[found.start(8) :] if found.group(8) else ""):
        dashes = re.match(r"-{5,}(?=.)", word.group())  # a placeholder glued to the next cell
        start = found.start(8) + word.start() + (dashes.end() if dashes else 0)
        stop = found.start(8) + word.end()
        tokens.append([start, stop, word.group(), 2 if end is None else start - end])
        end = stop
    del number
    return found, tokens


def _boc_continue(tokens: list[list[Any]], line: str) -> None:
    """Join the fragments of a wrapped cell onto the end of the cell they sit under.

    A wrapped chunk starts at its cell's left edge, so a fragment belongs to the cell that starts
    at its column: the words from the one under it up to the one under the next fragment (or the
    end of that run of words). A fragment under no word is joined onto the last word.
    """
    found = [(w.start(), w.group()) for w in re.finditer(r"\S+", line)]
    under: list[int | None] = []
    for column, _text in found:
        near = [(abs(t[0] - column), i) for i, t in enumerate(tokens) if abs(t[0] - column) <= 2]  # noqa: PLR2004
        under.append(min(near)[1] if near else None)
    for n, (_column, text) in enumerate(found):
        first = under[n]
        if first is None:
            if tokens:
                tokens[-1][2] += text
            continue
        later = next((u for u in under[n + 1 :] if u is not None), None)
        last = later - 1 if later is not None else first
        while later is None and last + 1 < len(tokens) and tokens[last + 1][3] < 2:  # noqa: PLR2004
            last += 1
        tokens[max(first, last)][2] += text


def boc_text(text: str) -> Iterator[Parsed]:
    """The entries of a statement's layout text.

    A row starts at its date and time; page headers (with his name, card and account) are skipped
    and never stored.
    """
    rows: list[tuple[re.Match[str], list[list[Any]], int]] = []
    open_row = False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            open_row = False
        elif (row := _boc_row(line, number)) is not None:
            rows.append((*row, number))
            open_row = True
        elif any(word in line for word in _BOC_PAGE_WORDS):
            open_row = False
        elif open_row:
            _boc_continue(rows[-1][1], line)
    for found, tokens, number in rows:
        day, clock, currency, signed, balance, kind, channel, _ = found.groups()
        local = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CHINA)
        fen = _fen(signed)
        left = _fen(balance)
        if fen is None or left is None:
            continue
        way = "out" if fen < 0 else "in" if fen > 0 else "neutral"
        who, account = _boc_cells(tokens)
        verb = {"out": "paid", "in": "received", "neutral": "moved"}[way]
        words = f" {'to' if way == 'out' else 'from'} {who}" if who else ""
        fields = {
            "amount_fen": abs(fen), "balance_fen": left,
            "currency": _BOC_CURRENCY.get(currency, currency),
            "direction": way, "counterparty": who, "item": kind, "channel": channel,
            "status": "", "method": "", "order": "", "counts": True,
        }
        if account:
            fields["account"] = account
        yield Parsed(
            "boc", f"{day} {clock} {signed.replace(',', '')} {balance.replace(',', '')}",
            f"line {number}", _ms(local), CHINA, "payment",
            f"{verb} {fields['currency']} {money(abs(fen))}{words}: {kind}", fields,
        )


def boc(_name: str, data: bytes, _fallback_ms: int) -> Iterator[Parsed]:
    """The Bank of China debit statement PDF. An encrypted copy is kept raw and gives no entries."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return
        text = "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)
    except PyPdfError as exc:
        msg = "not a readable PDF"
        raise ValueError(msg) from exc
    yield from boc_text(text)


# --- the Claude daily letter ---------------------------------------------------------------

DAILY_SUBJECT: Final = "Jarvis Claude 日报"
"""A daily letter's subject starts with this, then the day (ADR 0295)."""
_DAILY_KEY: Final = r"[A-Za-z][\w -]{0,40}?"
_DAILY_LINE: Final = re.compile(rf"^(?:[-*]\s*)?({_DAILY_KEY})\s*[:\uff1a]\s*(.*)$")
_DAILY_SECTION: Final = re.compile(r"^##\s*(.*)$")
_DAILY_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAILY_TIME: Final = re.compile(
    r"(\d{1,2}):(\d{2})(?:\s*(?:[-\u2013\u2014~]|to)\s*(\d{1,2}):(\d{2}))?",
)


def is_daily_subject(subject: str) -> bool:
    """Whether a mail's subject is a daily letter's: every other reader of mail leaves it alone."""
    return subject.strip().casefold().startswith(DAILY_SUBJECT.casefold())


def _daily_fields(lines: list[str]) -> dict[str, str]:
    """``key: value`` lines (a leading dash allowed); a line that is none continues the last."""
    found: dict[str, str] = {}
    last = ""
    for line in lines:
        hit = _DAILY_LINE.match(line.strip())
        if hit:
            last = re.sub(r"[ -]+", "_", hit.group(1).strip().lower())
            found[last] = f"{found[last]}\n{hit.group(2)}" if last in found else hit.group(2)
        elif line.strip() and last:
            found[last] += f"\n{line.strip()}"
    return found


def _squash(text: str) -> str:
    """A short hash of ``text`` that ignores spacing: other line breaks are the same revision."""
    return _digest(" ".join(text.split()))[:10]


def _daily_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return ZoneInfo("UTC")


def _daily_clock(day: date, hour: str, minute: str, zone: ZoneInfo) -> int | None:
    try:
        return _ms(datetime.combine(day, dtime(int(hour), int(minute)), zone))
    except ValueError:
        return None


def claude_daily(
    name: str, data: bytes, fallback_ms: int, source: str = "claude_daily",
) -> Iterator[Parsed]:
    """A day's letter of his Claude chats (ADR 0295): one entry for the day, one per chat section.

    The same shape is the day's report of his Claude Code work (ADR 0303, ``source``
    ``claude_code_daily``): its sections are projects.

    Read leniently: a letter with no header, no sections or neither is still one whole-day entry
    (``parsed`` false). The day comes from the ``date:`` line, else from ``name`` (the subject),
    else from ``fallback_ms``; the zone from ``timezone:``, else UTC. A chat's keys carry the day,
    its id (else a digest of its title) and a hash of its text, so a revised chat is a new entry.
    """
    # blank lines go: a letter re-sent as HTML has them between every line, and is the same one
    lines = data.decode("utf-8", "replace").lstrip("\ufeff").splitlines()
    text = "\n".join(line.rstrip() for line in lines if line.strip())
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if (hit := _DAILY_SECTION.match(line)) is not None:
            sections.append((hit.group(1).strip(), []))
        else:
            (sections[-1][1] if sections else head).append(line)
    header = _daily_fields(head)
    zone_name = header.get("timezone", "").strip() or "UTC"
    zone = _daily_zone(zone_name)
    day = datetime.fromtimestamp(fallback_ms / 1000, zone).date()
    for where in (header.get("date", ""), name):
        if (found := _DAILY_DAY.search(where)) is not None:
            with contextlib.suppress(ValueError):  # 2026-13-45 is no day: the next source is tried
                day = date.fromisoformat(found.group(0))
                break
    noon = _ms(datetime.combine(day, dtime(12), zone))
    chats: list[Parsed] = []
    for n, (title, lines) in enumerate(sections, 1):
        body = "\n".join(lines).strip()
        fields = _daily_fields(lines)
        ident = fields.get("id", "").strip() or f"title-{_digest(title)[:12]}"
        start, end = noon, noon
        if (when := _DAILY_TIME.search(fields.get("time", ""))) is not None:
            begin = _daily_clock(day, when.group(1), when.group(2), zone)
            finish = (
                _daily_clock(day, when.group(3), when.group(4), zone) if when.group(3) else None
            )
            if begin is not None:
                start = end = begin
                if finish is not None:
                    end = finish if finish >= begin else finish + 86_400_000
        whole = f"{title}\n{body}".strip()
        chats.append(Parsed(
            source, f"chat:{day}:{ident}:{_squash(whole)}", f"section {n}", start,
            zone_name, "daily_chat", whole,
            {**fields, "title": title, "date": str(day), "timezone": zone_name, "end_ms": end},
        ))
    span = (min(c.at_ms for c in chats), max(c.fields["end_ms"] for c in chats)) if chats else (
        noon, noon,
    )
    yield Parsed(
        source, f"day:{day}:{_squash(text)}", "day", noon, zone_name, "daily", text,
        {
            **header, "date": str(day), "timezone": zone_name, "parsed": bool(sections),
            "sections": len(chats), "titles": [c.fields["title"] for c in chats],
            "start_ms": span[0], "end_ms": span[1],
        },
    )
    yield from chats


# --- the Claude Code dialogue archive ------------------------------------------------------

DIALOGUE_SOURCE: Final = "claude_code_dialogue"
_SESSION_HEAD: Final = re.compile(
    r"^# Session (\S+)\n\n- account: (.*)\n- title: (.*)\n- project folder: (.*)\n"
    r"- first-last message: (\d{2}):(\d{2})-(\d{2}):(\d{2})\n",
    re.MULTILINE,
)


def _dialogue_instant(window_start: int, zone: ZoneInfo, hour: str, minute: str) -> int:
    """The first instant at or after ``window_start`` whose local clock reads ``hour:minute``."""
    first = datetime.fromtimestamp(window_start / 1000, zone)
    clock = dtime(int(hour), int(minute))
    found = datetime.combine(first.date(), clock, zone)
    if _ms(found) < window_start:
        found = datetime.combine(first.date() + timedelta(days=1), clock, zone)
    return _ms(found)


def claude_dialogue(name: str, data: bytes, fallback_ms: int) -> Iterator[Parsed]:
    """A day's cut Claude Code dialogue (ADR 0303): one entry per session section.

    The file is a preface (``date``, ``timezone``, ``window_start_ms``) and the sessions' files
    one after another, each starting ``# Session <id>`` and its header lines. A section's start
    and end are its header's first and last message times read inside the day's window; with no
    preface they are the day's noon. The key is the day, the session and a hash of the section,
    so a rewritten day adds only the sessions that changed.
    """
    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    cut = _SESSION_HEAD.search(text)
    header = _daily_fields((text[: cut.start()] if cut else text).splitlines())
    zone_name = header.get("timezone", "").strip() or "UTC"
    zone = _daily_zone(zone_name)
    day = datetime.fromtimestamp(fallback_ms / 1000, zone).date()
    for where in (header.get("date", ""), name):
        if (found := _DAILY_DAY.search(where)) is not None:
            with contextlib.suppress(ValueError):
                day = date.fromisoformat(found.group(0))
                break
    noon = _ms(datetime.combine(day, dtime(12), zone))
    try:
        window_start = int(header["window_start_ms"])
    except (KeyError, ValueError):
        window_start = None
    heads = list(_SESSION_HEAD.finditer(text))
    for n, head in enumerate(heads, 1):
        body = text[head.start() : heads[n].start() if n < len(heads) else len(text)].rstrip()
        sid, account, title, folder, *clock = head.groups()
        start = end = noon
        if window_start is not None:
            start = _dialogue_instant(window_start, zone, clock[0], clock[1])
            end = _dialogue_instant(start, zone, clock[2], clock[3])
        yield Parsed(
            DIALOGUE_SOURCE, f"{day}:{sid}:{_squash(body)}", f"session {n}", start, zone_name,
            "session_dialogue", body,
            {
                "date": str(day), "session": sid, "account": account, "title": title,
                "folder": folder, "time": f"{clock[0]}:{clock[1]}-{clock[2]}:{clock[3]}",
                "start_ms": start, "end_ms": end,
            },
        )


PARSERS: Final[dict[str, Callable[[str, bytes, int], Iterator[Parsed]]]] = {
    "wechat_pay": wechat_pay,
    "alipay": alipay,
    "boc": boc,
    "claude_daily": claude_daily,
}
"""Sources that are one file each. ``claude`` is a directory of files, read by :func:`claude`."""
