"""An answer sent to the screen in sentence chunks reads back as written: no glued sentences."""

# ruff: noqa: RUF001 — the story uses the typographic quotes a model writes.
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from jarvis.shared.realtime import LegacyPresentationBinding
from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface import cli_render
from jarvis.surface.sentence_splitter import split_into_sentences

if TYPE_CHECKING:
    from pathlib import Path

_STORY = (
    "Mara found a brass compass in her grandfather’s attic. Its needle pointed not north, "
    "but toward whatever someone had lost. She followed it through town.\n\n"
    "At sunset, the compass led her to the harbor. “I lost my first adventure,” he said. "
    "Pi is 3.14, said Dr. Who. 你好。 再见。"
)


@dataclass(frozen=True)
class _Plan:
    text: str
    required_gate_mode: str = "sentence"


def test_sentence_chunks_join_back_to_the_text() -> None:
    """Chunks carry the whitespace between sentences; only the ends of the whole are trimmed."""
    chunks = split_into_sentences(f"  {_STORY}\n")
    assert len(chunks) > 5
    assert "".join(chunks) == _STORY
    assert chunks[1].startswith(" Its needle")  # the space stays with the sentence after it


def test_streamed_chunks_on_the_wire_keep_the_space(tmp_path: Path) -> None:
    """The companion appends each chunk's text as it comes: that is what it shows."""
    conn = open_event_log(tmp_path / "chunks.db")
    try:
        cli_render._emit_response_chunks(  # noqa: SLF001 - the one emitter of non-streamed chunks
            conn, turn_id="T", response_plan=cast("cli_render.ResponsePlanLike", _Plan(_STORY)),
            binding=LegacyPresentationBinding(response_id="R", response_group_id="G"),
            channel="both", phase="final",
        )
        shown = "".join(
            str(event.payload["text"])
            for event in iter_events(conn)
            if event.type == "surface.response_chunk"
        )
    finally:
        conn.close()
    assert shown == _STORY
    assert "attic. Its" in shown
    assert "lost. She" in shown
