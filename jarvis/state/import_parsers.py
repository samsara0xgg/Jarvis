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

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
CHINA: Final = "Asia/Shanghai"
_CHINA: Final = ZoneInfo(CHINA)

SUCCESS: Final = frozenset({
    "支付成功", "交易成功", "已转账", "已收钱", "朋友已收钱", "对方已收钱", "已收款",
    "提现已到账", "充值完成", "充值成功", "还款成功", "已存入零钱",
})
"""Statuses of money that moved. A refund, a closed or a failed order is shown, not summed."""
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


def _message_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    blocks = message.get("content")
    parts = (
        [b.get("text", "") for b in blocks if isinstance(b, dict)]
        if isinstance(blocks, list) else []
    )
    return "\n".join(p for p in parts if isinstance(p, str) and p.strip()).strip()


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


PARSERS: Final[dict[str, Callable[[str, bytes, int], Iterator[Parsed]]]] = {
    "wechat_pay": wechat_pay,
    "alipay": alipay,
}
"""Sources that are one file each. ``claude`` is a directory of files, read by :func:`claude`."""
