"""The capture-side pipeline and answer readers for the tests of the answer hold (ADR 0053)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_pipeline
from tests.support.arbitration import emit_whole_response
from tests.support.media import terminal_rows
from tests.support.voice_input import RecordingPipeline

if TYPE_CHECKING:
    from pathlib import Path


class AcceptingPipeline(RecordingPipeline):
    """ASR that accepts every utterance, or finds nothing in it."""

    def __init__(
        self,
        record: list[str],
        *,
        empty: bool = False,
        said: str = "明天上午十点提醒我",
    ) -> None:
        super().__init__()
        self._record = record
        self._empty = empty
        self._said = said

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        if self._empty:
            msg = "silence"
            raise voice_pipeline.VoicePipelineEmptyError(msg)
        kwargs["before_emit"](self._said)
        self._record.append("utterance.received")
        return super().run_turn(**kwargs)


def played_texts(db_path: Path) -> list[str]:
    return [
        str(payload["response_id"])
        for kind, payload in terminal_rows(open_event_log(db_path))
        if kind == "surface.playback_completed"
    ]


def answer_of(conn: Any, response_id: str, turn_id: str) -> Any:  # noqa: ANN401
    return emit_whole_response(
        conn,
        response_id=response_id,
        group_id=f"G{turn_id}",
        turn_id=turn_id,
        text="好的",
    )
