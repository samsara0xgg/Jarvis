"""``session.recent_records`` drives compaction: what the window cuts is folded, off the turn.

With the window on, every record it hides is folded into the summary by the
existing summariser (previous summary plus those records, anchor = the last
hidden record), so the prompt is the summary and the latest N to 2N-1 records
with nothing missing between them. Real memory.db, real Event Log, real
``CompactionSweep`` and ``run_compaction``; only the summariser's provider
call is faked, by a client that returns a well-formed summary and records
what it was asked.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from jarvis.decision.compaction import REQUIRED_HEADINGS
from jarvis.decision.llm import ChatResult
from jarvis.runtime import session_compaction
from jarvis.runtime.session_compaction import CompactionSweep, run_compaction
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    SessionSettings,
    local_now,
    open_memory_db,
    render_context,
    verbatim_stats,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

N = 20
HEADINGS = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


class _Summariser:
    """Stands in for the compact preset's client: records each request it is sent."""

    provider = "openai"
    model = "fake-summariser"

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.fail_next = False
        self.reply: str | None = None

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        """One summary: the headings and the number of records this call folded."""
        if self.fail_next:
            self.fail_next = False
            msg = "provider unreachable"
            raise TimeoutError(msg)
        text = str(messages[0]["content"])
        self.requests.append(text)
        said = _numbers(text.split("[Records")[1])
        text = self.reply or (
            f"## Conversation summary\n{HEADINGS}\nfolds records {said[0]} to {said[-1]}"
        )
        return ChatResult(
            text=text, tool_calls=(), finish_reason="stop", input_tokens=1, output_tokens=1,
            raw={}, model_used=self.model,
        )


def _fill(path: Path, first: int, last: int) -> None:
    """Records ``first`` to ``last`` inclusive, one minute apart, ending at the newest."""
    base = local_now() - timedelta(minutes=500)
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (
                    f"r{index:04d}",
                    (base + timedelta(minutes=index)).isoformat(timespec="seconds"),
                    "allen" if index % 2 == 0 else "jarvis",
                    f"第{index}句",
                )
                for index in range(first, last + 1)
            ],
        )


def _summaries(path: Path) -> list[tuple[str, str]]:
    with closing(open_memory_db(path)) as conn:
        return [
            (str(upto), str(text))
            for upto, text in conn.execute(
                "SELECT upto_record_id, summary FROM summaries ORDER BY rowid",
            )
        ]


def _numbers(text: str) -> list[int]:
    return [int(number) for number in re.findall(r"第(\d+)句", text)]


def _said(history: tuple[dict[str, str], ...]) -> list[int]:
    """The records in the history; the summary and the note name none."""
    return _numbers(str(history))


def _sweep(tmp_path: Path, summariser: _Summariser) -> CompactionSweep:
    log = tmp_path / "events.db"
    open_event_log(log).close()
    sweep = CompactionSweep(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=SessionSettings(compact_prompt="summarise", recent_records=N),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        context_length=lambda: 1_050_000,
        live_open=lambda: False,
    )
    sweep._client = summariser  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return sweep


def _tick(sweep: CompactionSweep) -> None:
    """One supervisor tick, and wait for the job it starts, if any."""

    async def _go() -> None:
        sweep.tick()
        if sweep._task is not None:  # noqa: SLF001
            await sweep._task  # noqa: SLF001

    asyncio.run(_go())


def test_the_window_moving_folds_what_it_hid_once_and_the_prompt_has_no_gap(
    tmp_path: Path,
) -> None:
    """150 records at N=20 hide 120: one fold, then summary + records 120-149, no gap."""
    path = tmp_path / "memory.db"
    _fill(path, 0, 149)
    summariser = _Summariser()
    sweep = _sweep(tmp_path, summariser)
    assert verbatim_stats(path, recent=N).hidden == 120
    before = render_context(path, exclude_id="", recent=N).history
    assert before[0]["content"].startswith("[Earlier conversation · 120 records")

    _tick(sweep)
    _tick(sweep)  # nothing hidden any more: no second call

    assert len(summariser.requests) == 1
    assert "(none, this is the first summary)" in summariser.requests[0]
    assert _numbers(summariser.requests[0].split("[Records")[1]) == list(range(120))
    [(upto, summary)] = _summaries(path)
    assert upto == "r0119"
    history = render_context(path, exclude_id="", recent=N).history
    assert history[0]["content"].startswith("[Conversation summary · up to ")
    assert "search_records" in history[0]["content"]
    assert "read_records" in history[0]["content"]
    assert summary in history[0]["content"]
    assert _said(history) == list(range(120, 150))
    assert "Earlier conversation" not in str(history)
    assert verbatim_stats(path, recent=N).hidden == 0

    # Ten more records: the window moves again, and the next fold extends the summary.
    _fill(path, 150, 159)
    _tick(sweep)
    assert len(summariser.requests) == 2
    assert summary in summariser.requests[1]
    assert _numbers(summariser.requests[1].split("[Records")[1]) == list(range(120, 140))
    assert [upto for upto, _ in _summaries(path)] == ["r0119", "r0139"]
    assert _said(render_context(path, exclude_id="", recent=N).history) == list(range(140, 160))


def test_a_failed_fold_keeps_the_old_summary_and_is_retried_on_a_later_turn(
    tmp_path: Path,
) -> None:
    """A raised call and a malformed summary each leave the store alone; a new record retries."""
    path = tmp_path / "memory.db"
    _fill(path, 0, 149)
    summariser = _Summariser()
    sweep = _sweep(tmp_path, summariser)

    summariser.fail_next = True
    _tick(sweep)
    assert _summaries(path) == []
    _tick(sweep)  # same newest record: not retried on every tick
    assert summariser.requests == []

    _fill(path, 150, 150)  # a later turn
    summariser.reply = "## Conversation summary\nno headings at all"
    _tick(sweep)
    assert len(summariser.requests) == 1
    assert _summaries(path) == []
    # The prompt still shows everything the window leaves, with the note.
    assert render_context(path, exclude_id="", recent=N).history[0]["content"].startswith(
        "[Earlier conversation · 120 records",
    )

    _fill(path, 151, 151)
    summariser.reply = None
    _tick(sweep)
    assert [upto for upto, _ in _summaries(path)] == ["r0119"]


def test_with_the_window_off_the_idle_size_and_age_gates_still_apply(tmp_path: Path) -> None:
    """N=0: 150 recent records are far under 40% of a 1.05M window, so nothing folds."""
    path = tmp_path / "memory.db"
    _fill(path, 0, 149)
    summariser = _Summariser()
    sweep = _sweep(tmp_path, summariser)
    sweep._settings = replace(sweep._settings, recent_records=0)  # noqa: SLF001
    _tick(sweep)
    assert summariser.requests == []
    assert _summaries(path) == []


def test_the_first_fold_of_a_long_history_is_a_chain_of_bounded_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked at 1000 characters: each call extends the summary the one before wrote."""
    path = tmp_path / "memory.db"
    _fill(path, 0, 149)
    monkeypatch.setattr(session_compaction, "_CHUNK_CHARS", 1000)
    summariser = _Summariser()
    log = tmp_path / "events.db"
    open_event_log(log).close()
    outcome = run_compaction(
        MemorySettings(db_path=path, audio_dir=tmp_path / "audio"),
        SessionSettings(compact_prompt="summarise", recent_records=N),
        summariser,  # type: ignore[arg-type]
        event_log_path=log,
        pricing_table={},
    )
    assert outcome.startswith("summary landed: 120 records")
    assert len(summariser.requests) > 3
    assert "(none, this is the first summary)" in summariser.requests[0]
    assert all("folds records " in request for request in summariser.requests[1:])
    anchors = [upto for upto, _ in _summaries(path)]
    assert anchors[-1] == "r0119"
    assert len(anchors) == len(summariser.requests)
    assert _said(render_context(path, exclude_id="", recent=N).history) == list(range(120, 150))
