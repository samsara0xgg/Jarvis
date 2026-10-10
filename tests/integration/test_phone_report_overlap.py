"""A phone's REPORTs may overlap by one sample; the heard-prefix ledger reads that as contiguous.

Live 2026-10-10 (22:23-22:43Z): each REPORT of the phone started one sample before the last one
ended. ``PlaybackLedger.record_submitted`` read the mismatch as a gap, kept its submitted cursor
at the last contiguous end, and so the audible horizon, which is bounded by that cursor, never
reached the accepted samples. ``fully_presented`` never held, the media actor never wrote
``surface.playback_completed``, and the answer stayed the active one until Allen's next turn
interrupted it: every phone answer logged ``surface.playback_interrupted``.

Shown: (a) the ledger, on a table of report sequences: contiguous and a one-sample overlap
present the whole answer, a real gap (forward, or an overlap of more than one) is still a gap and
leaves it unpresented; (b) a phone whose reports overlap by one sample plays an answer through
the real route and actor, and the answer ends in ``surface.playback_completed``, with no
``surface.playback_interrupted``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.surface import voice_ledger
from tests.integration.test_phone_voice import _answer, _Host
from tests.integration.test_terminal_voice import _wait_for

if TYPE_CHECKING:
    from pathlib import Path

_SAMPLES = 1_000
_RATE = 32_000


@pytest.mark.parametrize(
    ("reports", "submitted", "presented"),
    [
        pytest.param([(0, 400), (400, 1_000)], 1_000, True, id="contiguous"),
        pytest.param([(0, 400), (399, 1_000)], 1_000, True, id="one sample overlap"),
        pytest.param([(0, 400), (399, 700), (699, 1_000)], 1_000, True, id="overlap each report"),
        pytest.param([(0, 400), (401, 1_000)], 400, False, id="one sample forward is a gap"),
        pytest.param([(0, 400), (500, 1_000)], 400, False, id="forward gap"),
        pytest.param([(0, 400), (398, 1_000)], 400, False, id="two samples back is a gap"),
        pytest.param([(0, 400), (100, 1_000)], 400, False, id="far back is a gap"),
        pytest.param([(0, 400), (399, 600), (700, 1_000)], 600, False, id="gap after an overlap"),
    ],
)
def test_the_ledger_reads_a_one_sample_overlap_as_contiguous_and_a_real_gap_as_a_gap(
    reports: list[tuple[int, int]], submitted: int, *, presented: bool,
) -> None:
    """(a): the table's reports in order; the submitted cursor and whether the answer finished."""
    lease = voice_ledger.GenerationLease("S", "R", "G", "T", 1, 1)
    ledger = voice_ledger.PlaybackLedger(lease, sample_rate=_RATE)
    ledger.begin_segment(sequence=0, text="Hello there.", segment_hash="a")
    ledger.accept_samples(sequence=0, sample_count=_SAMPLES)
    ledger.finish_segment(sequence=0)
    ledger.mark_software_drained()
    for start, end in reports:
        ledger.record_submitted(
            output_start_cursor=start, output_end_cursor=end, audibility_class="normal",
        )
    ledger.record_audible(output_cursor=_SAMPLES, cursor_quality="estimated")
    snapshot = ledger.snapshot()
    assert snapshot.submitted_samples == submitted
    assert snapshot.fully_presented is presented
    assert (snapshot.heard_text == "Hello there.") is presented


def test_a_phone_whose_reports_overlap_by_one_sample_completes_its_answers(
    tmp_path: Path,
) -> None:
    """(b): the answer is heard whole, ended by its completion, and nothing interrupts it."""
    text = "The first sentence."
    with _Host(tmp_path) as host:
        phone = host.phone(report_overlap=1)
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-1", "what is the weather", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            _answer(host.log, turn, "R-1", text)
            _wait_for(
                lambda: "surface.playback_completed" in host.playback("R-1"),
                "the answer never completed",
                timeout_s=20,
            )
            [done] = [
                p for p in host.types("surface.playback_completed") if p["response_id"] == "R-1"
            ]
            assert done["heard_text"] == text
            # his next turn, and the host's barge-in for it, find nothing left to interrupt
            phone.say("u-2", "and tomorrow", spoken=True)
            phone.wait_until(
                lambda: sum(1 for t in phone.texts if t.get("type") == "said") == 2,
                "the second say was never answered",
            )
            assert "surface.playback_interrupted" not in host.playback("R-1")
            assert not host.types("surface.playback_interrupted")
        finally:
            phone.close()
