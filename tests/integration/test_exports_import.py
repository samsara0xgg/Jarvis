"""ADR 0259: exports he hands over are kept raw and read into one UTC timeline.

Synthetic exports only: a fake Claude export directory under ``tests/fixtures/imports`` and a
WeChat Pay .xlsx and an Alipay .csv built here. Each check asserts what the import counted, what
sits on disk and in memory.db, and what the tool and the day line return; no model, no network.
"""

# ruff: noqa: E501, RUF001 - statement rows and expected texts read best one per line; full-width colons
from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from pypdf import PdfWriter

from jarvis.deployment import data
from jarvis.execution.archive_tool import build_archive_tool
from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.runtime import JarvisRuntime, inherent_loop
from jarvis.state import imports, meaning
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import open_event_log
from jarvis.state.import_parsers import boc_text
from jarvis.state.memory_db import open_memory_db

CLAUDE = Path(__file__).resolve().parents[1] / "fixtures" / "imports" / "claude_export"
VANCOUVER = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)

WECHAT_HEADER = [
    "交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态",
    "交易单号", "商户单号", "备注",
]
WECHAT_ROWS = [
    ["2026-10-12 07:41:37", "商户消费", "Lotus Noodle House", "Lunch set", "支出", "¥68.00", "零钱",
     "支付成功", "W100001", "M1", "/"],
    ["2026-10-12 09:00:00", "商户消费", "Maple Coffee", "Latte", "支出", "¥31.50", "零钱",
     "已全额退款", "W100002", "M2", "/"],
    # as the real export writes it: the time an Excel day number, the amount a plain number
    ["46308.416666666664", "转账", "Friend Lin", "Transfer", "收入", "200", "零钱",
     "已收钱", "W100003", "M3", "/"],
]
ALIPAY_HEAD = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,"
)


def _xlsx(rows: list[list[str]]) -> bytes:
    """A WeChat-shaped workbook: three preamble rows, the header row, then the rows."""
    preamble = [["微信支付账单明细"], ["微信昵称：[tester]"], ["起始时间：[2026-10-01 00:00:00]"]]
    sheet = [*preamble, WECHAT_HEADER, *rows]
    strings: list[str] = []
    xml_rows = []
    for number, row in enumerate(sheet, 1):
        cells = []
        for col, text in enumerate(row):
            strings.append(text)
            cells.append(f'<c r="{chr(65 + col)}{number}" t="s"><v>{len(strings) - 1}</v></c>')
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as book:
        book.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(xml_rows)}</sheetData></worksheet>")
        book.writestr("xl/sharedStrings.xml", f"<sst {ns}>{''.join(f'<si><t>{s}</t></si>' for s in strings)}</sst>")
    return out.getvalue()


def _alipay(amount: str = "25.00") -> bytes:
    """An Alipay-shaped GBK file: preamble lines, the header line, rows with padded cells."""
    lines = [
        "------------------------------------------------------------------------------------",
        "导出信息：", "姓名：测试", "支付宝支付科技有限公司", "",
        "------------------------交易记录明细列表------------------------",
        ALIPAY_HEAD,
        f"2026-10-12 12:30:05\t,餐饮美食\t,Sunrise Bakery\t,x***@example.com\t,Bread\t,支出,{amount},余额,交易成功,A200001\t,B1\t,\t,",
        "2026-10-12 13:00:00\t,其他\t,Gift Shop\t,y***@example.com\t,Mug\t,支出,40.00,余额,交易关闭,A200002\t,B2\t,\t,",
        "2026-10-13 08:00:00\t,转账\t,Friend Lin\t,z***@example.com\t,Split\t,不计收支,10.00,余额,交易成功,A200003\t,B3\t,\t,",
        "------------------------------------------------------------------------------------",
    ]
    return "\n".join(lines).encode("gb18030")


def _import(root: Path, source: str, *paths: Path) -> imports.Counts:
    with closing(open_memory_db(root / "memory.db")) as conn:
        return imports.import_files(conn, root / imports.IMPORTS_DIRNAME, source, paths)


def _held(root: Path) -> dict[str, imports.Entry]:
    with closing(open_memory_db(root / "memory.db")) as conn:
        return {row.id: row for row in imports.entries(conn)}


def _statements(tmp_path: Path) -> tuple[Path, Path]:
    wechat, alipay = tmp_path / "wechat.xlsx", tmp_path / "alipay.csv"
    wechat.write_bytes(_xlsx(WECHAT_ROWS))
    alipay.write_bytes(_alipay())
    return wechat, alipay


def test_raw_is_kept_once_by_digest_and_a_second_import_adds_nothing(tmp_path: Path) -> None:
    """Seven files in: a byte-copy is kept once and the login history not at all."""
    first = _import(tmp_path, "claude", CLAUDE)
    kept = sorted(p.name[17:] for p in (tmp_path / "imports" / "claude").iterdir())
    again = _import(tmp_path, "claude", CLAUDE)

    assert kept == [
        "conversations 2.json", "design-1.json", "memories-1.json", "projects.json", "reflections-1.json",
    ]
    assert (first.files_stored, first.files_skipped) == (5, 1)
    # 6 messages (the empty-text one reads from content), 1 design message, 2 memories, 1 reflection
    assert (first.added, first.held, first.conflicting) == (10, 0, 0)
    assert (again.files_stored, again.files_skipped) == (0, 6)
    assert (again.added, again.held) == (0, 10)
    assert not list((tmp_path / "imports").rglob("light_metadata*"))
    kinds = sorted(e.kind for e in _held(tmp_path).values())
    assert kinds == ["memory"] * 2 + ["message"] * 7 + ["reflection"]


def test_a_file_unzipped_without_its_time_is_dated_when_it_was_handed_over(tmp_path: Path) -> None:
    """A reflection with no time of its own and a 1980 file time takes the import time instead."""
    copy = tmp_path / "reflections" / "reflections-1.json"
    copy.parent.mkdir()
    copy.write_bytes((CLAUDE / "reflections" / "reflections-1.json").read_bytes())
    os.utime(copy, (315_532_800, 315_532_800))  # 1980-01-01, a zip's default

    _import(tmp_path, "claude", copy)

    (row,) = _held(tmp_path).values()
    assert datetime.fromtimestamp(row.at_ms / 1000, UTC).year >= 2026


def test_the_exports_stand_ins_for_tool_blocks_are_dropped_even_from_rows_held_before(
    tmp_path: Path,
) -> None:
    """A message that was only a stand-in is gone; one with words keeps the words alone."""
    stand_in = "```\nThis block is not supported on your current device yet.\n```"
    chat = {"uuid": "c1", "name": "Bus pass", "created_at": "2026-10-01T10:00:00Z", "chat_messages": [
        {"uuid": "m1", "sender": "assistant", "created_at": "2026-10-01T10:00:01Z", "text": stand_in},
        {"uuid": "m2", "sender": "assistant", "created_at": "2026-10-01T10:00:02Z",
         "text": f"Looked it up.\n\n{stand_in}\n\nThe pass is 50 dollars."},
    ]}
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([chat]))
    _import(tmp_path, "claude", export)
    assert [e.text for e in _held(tmp_path).values()] == ["Looked it up.\n\nThe pass is 50 dollars."]
    # a row read by the parser before the fix still holds the stand-in: reading drops it
    with closing(open_memory_db(tmp_path / "memory.db")) as conn, conn:
        conn.execute("UPDATE import_entries SET text = ? WHERE source = 'claude'", (f"Looked it up.\n{stand_in}",))
    (held,) = _held(tmp_path).values()
    assert held.text == "Looked it up."
    seen = _SeenEmbedder()
    meaning.index_pending(tmp_path / "memory.db", seen)
    assert seen.texts == ["Looked it up."]  # the search index is built from the same clean text


def test_china_times_land_on_their_utc_instant_and_amounts_are_exact(tmp_path: Path) -> None:
    """Naive Asia/Shanghai times become UTC instants; amounts are fen; refunds do not count."""
    wechat, alipay = _statements(tmp_path)
    _import(tmp_path, "wechat_pay", wechat)
    _import(tmp_path, "alipay", alipay)

    held = {e.fields["order"]: e for e in _held(tmp_path).values()}
    lunch = held["W100001"]
    assert datetime.fromtimestamp(lunch.at_ms / 1000, UTC) == datetime(2026, 10, 11, 23, 41, 37, tzinfo=UTC)
    assert (lunch.zone, lunch.source, lunch.kind) == ("Asia/Shanghai", "wechat_pay", "payment")
    assert (lunch.fields["amount_fen"], lunch.fields["direction"], lunch.fields["counts"]) == (6800, "out", True)
    assert held["W100002"].fields["counts"] is False  # refunded
    assert held["A200002"].fields["counts"] is False  # closed
    assert held["A200003"].fields["direction"] == "neutral"
    lin = held["W100003"]  # its Excel day number is 2026-10-13 10:00 in China
    assert datetime.fromtimestamp(lin.at_ms / 1000, UTC) == datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
    assert lin.fields["amount_fen"] == 20000
    assert datetime.fromtimestamp(held["A200001"].at_ms / 1000, UTC) == datetime(2026, 10, 12, 4, 30, 5, tzinfo=UTC)
    assert held["A200001"].fields["counterparty"] == "Sunrise Bakery"  # trailing tab trimmed


def test_a_changed_held_row_is_logged_and_kept_as_it_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Same order number, other amount: counted as conflicting, logged by field name only."""
    _, alipay = _statements(tmp_path)
    _import(tmp_path, "alipay", alipay)
    changed = tmp_path / "alipay-later.csv"
    changed.write_bytes(_alipay(amount="26.00"))

    with caplog.at_level(logging.WARNING, logger="jarvis.state.imports"):
        counts = _import(tmp_path, "alipay", changed)

    assert (counts.files_stored, counts.added, counts.held, counts.conflicting) == (1, 0, 3, 1)
    assert "differs in text, fields" in caplog.text
    assert "26.00" not in caplog.text
    bakery = next(e for e in _held(tmp_path).values() if e.fields["order"] == "A200001")
    assert bakery.fields["amount_fen"] == 2500


def _tool(root: Path) -> Tool:
    (tool,) = build_archive_tool(root / "memory.db", VANCOUVER, lambda: NOW)
    return tool


def _call(tool: Tool, **args: object) -> dict[str, Any]:
    return cast("dict[str, Any]", tool.handler(args, cast("ToolContext", None)))


def test_the_archive_tool_searches_pages_totals_and_opens(tmp_path: Path) -> None:
    """Words, source, span and page; totals over every match; open; the refusals."""
    wechat, alipay = _statements(tmp_path)
    _import(tmp_path, "wechat_pay", wechat)
    _import(tmp_path, "alipay", alipay)
    _import(tmp_path, "claude", CLAUDE)
    tool = _tool(tmp_path)

    assert tool.read_only
    words = _call(tool, query="noodle")
    assert [e["text"] for e in words["entries"]] == [
        "paid CNY 68.00 to Lotus Noodle House: Lunch set",
    ]
    assert words["totals"] == {
        "payments": 1, "spent": "68.00", "received": "0.00", "currency": "CNY", "note": "all counted",
    }
    # source + date: Vancouver Oct 11 holds the noodle lunch (23:41Z) and the bakery (04:30Z Oct 12 = Oct 11 21:30)
    day = _call(tool, source="wechat_pay", date="2026-10-11")
    assert day["totals"]["spent"] == "68.00"
    span = _call(tool, source="alipay", date="2026-10-11", to="2026-10-13")
    assert span["totals"] == {
        "payments": 3, "spent": "25.00", "received": "0.00", "currency": "CNY",
        "note": "2 refunded, closed or internal; not in the totals",
    }
    # page 1 shows 8 of 13 payments + messages; totals cover only payments, all of them
    everything = _call(tool, date="2026-10-01", to="2026-10-14")
    assert len(everything["entries"]) == 8
    assert everything["more"] is True
    assert everything["totals"]["payments"] == 6
    second = _call(tool, date="2026-10-01", to="2026-10-14", page=2)
    assert len(second["entries"]) == 8
    assert second["more"] is False

    hit = _call(tool, query="soil")["entries"][0]
    assert hit["source"] == "claude"
    around = _call(tool, open=hit["id"])
    assert [t["who"] for t in around["turns"]] == ["human", "assistant", "human", "assistant"]
    assert [t.get("here") for t in around["turns"]] == [None, None, True, None]
    paid = _call(tool, open=_call(tool, query="noodle")["entries"][0]["id"])
    assert paid["entry"]["fields"]["order"] == "W100001"

    for bad in ({}, {"open": "x", "query": "y"}, {"to": "2026-10-02"}, {"source": "paypal"}):
        with pytest.raises(ToolError) as caught:
            _call(tool, **bad)
        assert caught.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as missing:
        _call(tool, open="claude-000000000000")
    assert missing.value.code == "not_found"


class _LunchEmbedder:
    """One concept, lunch in either language, plus a constant so no vector is zero."""

    model_id = "fake-lunch"
    available = True

    def embed(self, texts: Any, *, query: bool) -> np.ndarray:  # noqa: ANN401, ARG002
        """1 along lunch for a text that names it."""
        rows = [[float(any(w in t.casefold() for w in ("lunch", "午饭"))), 0.01] for t in texts]
        vectors = np.array(rows, dtype=np.float32)
        return np.asarray(vectors / np.linalg.norm(vectors, axis=1, keepdims=True), dtype=np.float32)


class _SeenEmbedder(_LunchEmbedder):
    """Keeps the passages it was asked to embed."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: Any, *, query: bool) -> np.ndarray:  # noqa: ANN401
        """Records passages, then embeds as the lunch model does."""
        if not query:
            self.texts.extend(texts)
        return super().embed(texts, query=query)


def test_the_archive_finds_an_entry_by_meaning_and_says_when_totals_rest_on_it(tmp_path: Path) -> None:
    """A Chinese word finds the English lunch payment; an exact word hit keeps its plain totals."""
    wechat, alipay = _statements(tmp_path)
    _import(tmp_path, "wechat_pay", wechat)
    _import(tmp_path, "alipay", alipay)
    embedder = _LunchEmbedder()
    while meaning.index_pending(tmp_path / "memory.db", embedder):
        pass
    (tool,) = build_archive_tool(tmp_path / "memory.db", VANCOUVER, lambda: NOW, embedder=embedder)

    near = _call(tool, query="午饭")
    assert [e["text"] for e in near["entries"]] == ["paid CNY 68.00 to Lotus Noodle House: Lunch set"]
    assert near["totals"]["spent"] == "68.00"
    assert "closest by meaning" in near["totals"]["note"]
    exact = _call(tool, query="noodle")
    assert exact["totals"]["note"] == "all counted"
    assert _call(tool, query="午饭", source="alipay")["entries"] == []


def test_the_day_line_folds_payments_and_chats_in_his_zone(tmp_path: Path) -> None:
    """The same entries land on different days in Vancouver and Shanghai."""
    wechat, _ = _statements(tmp_path)
    _import(tmp_path, "wechat_pay", wechat)
    _import(tmp_path, "claude", CLAUDE)
    conn = open_event_log(tmp_path / "events.db")
    sources = DaySources(VANCOUVER, memory_db=tmp_path / "memory.db")

    def items(zone: ZoneInfo, day: str) -> list[dict[str, Any]]:
        first, last = day_window(date.fromisoformat(day), zone)
        line = fold_day(conn, first, last, int(NOW.timestamp() * 1000), DaySources(zone, memory_db=sources.memory_db))
        return [i for i in line.items if i["kind"] in ("payment", "claude_chat")]

    vancouver = items(VANCOUVER, "2026-10-11")
    # the noodle lunch is 23:41Z: Oct 11 in Vancouver; the chat's two messages straddle UTC midnight
    assert [(i["kind"], i.get("text")) for i in vancouver] == [
        ("claude_chat", "Bus pass question"), ("payment", "Lunch set"),
    ]
    chat, pay = vancouver
    assert (pay["amount"], pay["direction"], pay["counterparty"]) == ("68.00", "out", "Lotus Noodle House")
    assert (chat["messages"], int(chat["end_ms"]) - int(chat["start_ms"])) == (2, 40 * 60 * 1000)
    assert [i["kind"] for i in items(ZoneInfo("Asia/Shanghai"), "2026-10-12")] == ["claude_chat", "payment"]
    # Friend Lin's transfer is 02:00Z on Oct 13: still Oct 12 in Vancouver, Oct 13 in Shanghai
    assert [i.get("text") for i in items(VANCOUVER, "2026-10-12")] == ["Transfer"]
    assert items(VANCOUVER, "2026-10-13") == []
    conn.close()


def test_the_imports_directory_is_outside_sweep_clear_and_export(tmp_path: Path) -> None:
    """Not a media directory, so the hourly sweep, clear-recordings and export never see it."""
    wechat, _ = _statements(tmp_path)
    _import(tmp_path, "wechat_pay", wechat)
    runtime = cast("JarvisRuntime", SimpleNamespace(
        config={}, runtime_paths=SimpleNamespace(root=tmp_path, artifacts_root=tmp_path / "artifacts"),
        attachments=None, memory=None,
    ))

    media = inherent_loop._media_dirs(runtime)  # noqa: SLF001
    data.clear_files(list(media))

    kept = tmp_path / imports.IMPORTS_DIRNAME
    assert not any(d == kept or kept in d.parents or d in kept.parents for d in media)
    assert len(list((kept / "wechat_pay").iterdir())) == 1
    assert json.loads(json.dumps(sorted(imports.SOURCES))) == ["alipay", "boc", "claude", "wechat_pay"]


FAKE_KEYS = [
    "sk-test-" + "x" * 30,
    "sk-ant-api03-" + "y" * 30,
    "AKIA" + "Z" * 16,
    "ghp_" + "a1" * 15,
    "github_pat_" + "b2" * 15,
    "xoxb-" + "1234567890-abcdef",
    "Bearer " + "q" * 24,
]


def test_a_pasted_secret_never_reaches_an_entry_or_the_tool_but_stays_in_the_raw_file(
    tmp_path: Path,
) -> None:
    """Masked in text and fields before storing, and again on the tool's way out."""
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\nmorefake\n-----END RSA PRIVATE KEY-----"
    named = "api key: " + "A1b2" * 10
    text = " | ".join([*FAKE_KEYS, pem, named, "plain " + "word" * 12])
    export = tmp_path / "export"
    export.mkdir()
    chat = {
        "uuid": "c1", "name": "keys", "created_at": "2026-10-10T18:00:00Z",
        "chat_messages": [{
            "uuid": "m1", "text": text, "sender": "human", "created_at": "2026-10-10T18:00:00Z",
        }],
    }
    raw = json.dumps([chat]).encode()
    (export / "conversations.json").write_bytes(raw)
    _import(tmp_path, "claude", export)

    (held,) = _held(tmp_path).values()
    for secret in [*FAKE_KEYS, "MIIfake", "A1b2" * 10]:
        assert secret not in held.text
    assert held.text.count("[secret removed]") == len(FAKE_KEYS) + 2
    assert "plain " + "word" * 12 in held.text  # a long plain word is not a token
    (kept,) = (tmp_path / "imports" / "claude").iterdir()
    assert kept.read_bytes() == raw
    # second guard: an entry written before the mask existed is still masked on the way out
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        conn.execute("UPDATE import_entries SET text = ?", ("leaked " + FAKE_KEYS[0],))
        conn.commit()
    tool = _tool(tmp_path)
    shown = _call(tool, source="claude")
    assert FAKE_KEYS[0] not in json.dumps(shown)
    assert "leaked [secret removed]" in json.dumps(shown)
    assert FAKE_KEYS[0] not in json.dumps(_call(tool, open=shown["entries"][0]["id"]))


def test_the_tool_shows_a_china_time_in_his_zone_not_as_written(tmp_path: Path) -> None:
    """An Alipay row at 07:41:37 Asia/Shanghai reads 4:41 pm on Oct 11 in Vancouver."""
    csv = tmp_path / "alipay.csv"
    csv.write_bytes(_alipay().replace(
        "2026-10-12 12:30:05".encode("gb18030"), "2026-10-12 07:41:37".encode("gb18030"),
    ))
    _import(tmp_path, "alipay", csv)

    shown = _call(_tool(tmp_path), query="bakery")["entries"][0]
    assert shown["when"] == "Sun Oct 11, 4:41 pm"
    assert "description" not in shown
    assert "time (in the same time zone as the rest of your context)" in _tool(tmp_path).description


# --- the Bank of China debit statement ----------------------------------------------------------

_BOC_ROW = (
    "      2026-03-04        10:11:12        人民币                -105.68         3,525.63  "
    "网上快捷支付          银企对接       -------------------财付通-Some Shop Na 财付通-Some Shop Nam     Z123N"
)
_WRAP = " " * _BOC_ROW.index("财付通-Some Shop Na ") + "me" + " " * (
    _BOC_ROW.index("财付通-Some Shop Nam ") - _BOC_ROW.index("财付通-Some Shop Na ") - 2
) + "e"
BOC_LAYOUT = f"""\
                        中国银行借记卡交易流水明细清单
 交易区间: 2026-03-01 至 2026-03-31         客户姓名: 测试用户
 借记卡号: 6200000000000000000   账号: 123456789012345678       页数: 1/1   行数: 4   打印时间: 2026-04-01
 记账日期   记账时间   币别   金额   余额   交易名称   渠道   网点名称   附言   对方账户名   对方卡号/账号
{_BOC_ROW}
{_WRAP}
      2026-03-05        09:00:00        人民币               2,000.00         5,525.63  工资                  ----------     ----------          Acme Payroll    6222000011112222333
      2026-03-31        00:00:01        人民币                   0.45         5,526.08  结息                  ----------     ----------          ----------      ----------
                                                          --------------------END--------------------
"""


def test_the_bank_statement_layout_is_read_row_by_row_without_his_card_or_account() -> None:
    """Signed amounts, China time, wrapped cells joined, placeholders empty, no long digits kept."""
    rows = list(boc_text(BOC_LAYOUT))

    assert [(r.fields["direction"], r.fields["amount_fen"], r.fields["balance_fen"]) for r in rows] == [
        ("out", 10568, 352563), ("in", 200000, 552563), ("in", 45, 552608),
    ]
    shop, pay, interest = rows
    assert datetime.fromtimestamp(shop.at_ms / 1000, UTC) == datetime(2026, 3, 4, 2, 11, 12, tzinfo=UTC)
    assert (shop.zone, shop.source, shop.kind, shop.fields["counts"]) == ("Asia/Shanghai", "boc", "payment", True)
    assert shop.key == "2026-03-04 10:11:12 -105.68 3525.63"
    assert shop.fields["counterparty"] == "财付通-Some Shop Name 财付通-Some Shop Name"
    assert shop.fields["account"] == "Z123N"
    assert shop.text == "paid CNY 105.68 to 财付通-Some Shop Name 财付通-Some Shop Name: 网上快捷支付"
    assert pay.fields["counterparty"] == "Acme Payroll"
    assert "account" not in pay.fields  # a 19-digit number is not kept
    assert (interest.fields["item"], interest.fields["counterparty"]) == ("结息", "")
    stored = json.dumps([(r.text, r.fields) for r in rows], ensure_ascii=False)
    for private in ("6200000000000000000", "123456789012345678", "测试用户", "6222000011112222333"):
        assert private not in stored


def _pdf(lines: list[tuple[int, str]], *, password: str | None = None) -> bytes:
    """A one-page PDF with each ``(x, text)`` line set in Courier at its column; ASCII only."""
    stream = "BT /F1 8 Tf\n" + "".join(
        f"1 0 0 1 {10 + col * 4.8:.1f} {780 - 12 * i} Tm ({text}) Tj\n"
        for i, (col, text) in enumerate(lines)
    ) + "ET"
    parts = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for n, body in enumerate(parts, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(parts) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(parts) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    if password is None:
        return out.encode()
    writer = PdfWriter(clone_from=io.BytesIO(out.encode()))
    writer.encrypt(password)
    sealed = io.BytesIO()
    writer.write(sealed)
    return sealed.getvalue()


def test_the_bank_pdf_is_read_through_its_text_and_an_encrypted_copy_only_kept(
    tmp_path: Path,
) -> None:
    """A generated PDF goes through pypdf's layout text; the sealed copy is stored, not read."""
    page = [
        (0, "Card: 6200000000000000000  Page 1/1"),
        (0, "2026-03-04   10:11:12   CNY   -105.68   3,525.63   Online   Bridge   Mart Shop Na"),
        (74, "me"),
    ]
    plain, sealed = tmp_path / "plain.pdf", tmp_path / "sealed.pdf"
    plain.write_bytes(_pdf(page))
    sealed.write_bytes(_pdf(page, password="fake-pass"))  # noqa: S106

    first = _import(tmp_path, "boc", sealed)
    assert (first.files_stored, first.added, first.unreadable) == (1, 0, 0)
    second = _import(tmp_path, "boc", plain)
    assert (second.files_stored, second.added) == (1, 1)
    (row,) = _held(tmp_path).values()
    assert datetime.fromtimestamp(row.at_ms / 1000, UTC) == datetime(2026, 3, 4, 2, 11, 12, tzinfo=UTC)
    assert (row.fields["direction"], row.fields["amount_fen"], row.fields["currency"]) == ("out", 10568, "CNY")
    assert "6200000000000000000" not in json.dumps(row.fields) + row.text
    assert len(list((tmp_path / "imports" / "boc").iterdir())) == 2
    with pytest.raises(ValueError, match="no parser"):
        _import(tmp_path, "paypal", plain)
