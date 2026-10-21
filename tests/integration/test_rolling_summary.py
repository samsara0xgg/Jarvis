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

from dataclasses import replace
from typing import TYPE_CHECKING

from jarvis.runtime import session_compaction
from jarvis.runtime.session_compaction import CompactionSweep, run_compaction
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    SessionSettings,
    render_context,
    verbatim_stats,
)
from tests.support.rolling_summary import (
    CompactSummariser,
    fill_records,
    rolling_summaries,
    sentence_numbers,
    tick_sweep,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

N = 20


def _said(history: tuple[dict[str, str], ...]) -> list[int]:
    """The records in the history; the summary and the note name none."""
    return sentence_numbers(str(history))


def _sweep(tmp_path: Path, summariser: CompactSummariser) -> CompactionSweep:
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


def test_the_window_moving_folds_what_it_hid_once_and_the_prompt_has_no_gap(
    tmp_path: Path,
) -> None:
    """150 records at N=20 hide 120: one fold, then summary + records 120-149, no gap."""
    path = tmp_path / "memory.db"
    fill_records(path, 0, 149)
    summariser = CompactSummariser()
    sweep = _sweep(tmp_path, summariser)
    assert verbatim_stats(path, recent=N).hidden == 120
    before = render_context(path, exclude_id="", recent=N).history
    assert before[0]["content"].startswith("[Earlier conversation · 120 records")

    tick_sweep(sweep)
    tick_sweep(sweep)  # nothing hidden any more: no second call

    assert len(summariser.requests) == 1
    assert "(none, this is the first summary)" in summariser.requests[0]
    assert sentence_numbers(summariser.requests[0].split("[Records")[1]) == list(range(120))
    [(upto, summary)] = rolling_summaries(path)
    assert upto == "r0119"
    history = render_context(path, exclude_id="", recent=N).history
    assert history[0]["content"].startswith("[Conversation summary · up to ")
    assert "conversation(open=<record_id>)" in history[0]["content"]
    assert "conversation(query=...)" in history[0]["content"]
    assert summary in history[0]["content"]
    assert _said(history) == list(range(120, 150))
    assert "Earlier conversation" not in str(history)
    assert verbatim_stats(path, recent=N).hidden == 0

    # Ten more records: the window moves again, and the next fold extends the summary.
    fill_records(path, 150, 159)
    tick_sweep(sweep)
    assert len(summariser.requests) == 2
    assert summary in summariser.requests[1]
    assert sentence_numbers(summariser.requests[1].split("[Records")[1]) == list(range(120, 140))
    assert [upto for upto, _ in rolling_summaries(path)] == ["r0119", "r0139"]
    assert _said(render_context(path, exclude_id="", recent=N).history) == list(range(140, 160))


def test_a_failed_fold_keeps_the_old_summary_and_is_retried_on_a_later_turn(
    tmp_path: Path,
) -> None:
    """A raised call and a malformed summary each leave the store alone; a new record retries."""
    path = tmp_path / "memory.db"
    fill_records(path, 0, 149)
    summariser = CompactSummariser()
    sweep = _sweep(tmp_path, summariser)

    summariser.fail_next = True
    tick_sweep(sweep)
    assert rolling_summaries(path) == []
    tick_sweep(sweep)  # same newest record: not retried on every tick
    assert summariser.requests == []

    fill_records(path, 150, 150)  # a later turn
    summariser.reply = "## Conversation summary\nno headings at all"
    tick_sweep(sweep)
    assert len(summariser.requests) == 1
    assert rolling_summaries(path) == []
    # The prompt still shows everything the window leaves, with the note.
    assert render_context(path, exclude_id="", recent=N).history[0]["content"].startswith(
        "[Earlier conversation · 120 records",
    )

    fill_records(path, 151, 151)
    summariser.reply = None
    tick_sweep(sweep)
    assert [upto for upto, _ in rolling_summaries(path)] == ["r0119"]


def test_with_the_window_off_the_idle_size_and_age_gates_still_apply(tmp_path: Path) -> None:
    """N=0: 150 recent records are far under 40% of a 1.05M window, so nothing folds."""
    path = tmp_path / "memory.db"
    fill_records(path, 0, 149)
    summariser = CompactSummariser()
    sweep = _sweep(tmp_path, summariser)
    sweep._settings = replace(sweep._settings, recent_records=0)  # noqa: SLF001
    tick_sweep(sweep)
    assert summariser.requests == []
    assert rolling_summaries(path) == []


def test_the_first_fold_of_a_long_history_is_a_chain_of_bounded_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked at 1000 characters: each call extends the summary the one before wrote."""
    path = tmp_path / "memory.db"
    fill_records(path, 0, 149)
    monkeypatch.setattr(session_compaction, "_CHUNK_CHARS", 1000)
    summariser = CompactSummariser()
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
    anchors = [upto for upto, _ in rolling_summaries(path)]
    assert anchors[-1] == "r0119"
    assert len(anchors) == len(summariser.requests)
    assert _said(render_context(path, exclude_id="", recent=N).history) == list(range(120, 150))
