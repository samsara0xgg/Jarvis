"""ADR 0241: the tool exam sends a logged request in the spoken reply shape its preset gets.

No provider is called: ``scripts.tool_exam`` rewrites the request's note and text format from the
real ``config/jarvis.yaml`` (through the factory production uses), and prints how many it changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.decision import _SPOKEN_REPLY_NOTE, _SPOKEN_REPLY_NOTE_STRUCTURED, SPOKEN_REPLY_FORMAT
from scripts import tool_exam

CONFIG = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"
INPUT = [{"role": "user", "content": "今天有什么日程"}]


def _request(note: str, *, schema: bool) -> dict[str, Any]:
    request: dict[str, Any] = {"instructions": f"You are Jarvis.\n\n{note}\n\nEnd.", "input": INPUT}
    if schema:
        request["text"] = {"format": SPOKEN_REPLY_FORMAT}
    return request


STRUCTURED = _request(_SPOKEN_REPLY_NOTE_STRUCTURED, schema=True)
UNSTRUCTURED = _request(_SPOKEN_REPLY_NOTE, schema=False)


def test_the_claude_presets_are_unstructured_and_the_openai_ones_are_not() -> None:
    """The field is read through the factory production uses; no preset that says nothing flips."""
    names = ["haiku", "haiku-think", "sonnet", "luna", "luna-think"]
    got = tool_exam.structured_presets(CONFIG, names)
    assert got == {"haiku": False, "haiku-think": False, "sonnet": False, "luna": True,
                   "luna-think": True}


def test_a_request_is_rewritten_both_ways_and_only_when_it_is_the_other_shape() -> None:
    """The note is swapped and the schema comes or goes; a request without the note is as logged."""
    assert tool_exam.spoken_reply_for(STRUCTURED, structured=False) == UNSTRUCTURED
    assert tool_exam.spoken_reply_for(UNSTRUCTURED, structured=True) == STRUCTURED
    assert tool_exam.spoken_reply_for(STRUCTURED, structured=True) == STRUCTURED
    assert tool_exam.spoken_reply_for(UNSTRUCTURED, structured=False) == UNSTRUCTURED
    plain = {"instructions": "no spoken note here", "input": INPUT}
    assert tool_exam.spoken_reply_for(plain, structured=False) == plain
    assert tool_exam.spoken_reply_for(plain, structured=True) == plain


def test_a_request_with_an_older_note_is_left_as_logged_and_counted() -> None:
    """A note of another text is not found verbatim: nothing changes, and the report says so."""
    older = {**STRUCTURED, "instructions": "Your reply is a JSON object with spoken, written."}
    assert tool_exam.spoken_reply_for(older, structured=False) == older
    cases = [{"request": STRUCTURED}, {"request": older}, {"request": UNSTRUCTURED}]
    assert tool_exam.shape_report(cases, {"haiku": False, "luna": True}) == (
        "spoken reply shape: haiku unstructured: 1/3 rewritten, 1 still carry the schema "
        "(their note is an older text); luna structured: 1/3 rewritten"
    )


def test_an_older_wording_of_the_json_note_is_rewritten_too() -> None:
    """Requests logged before the note's last edit carry another JSON wording; it goes too."""
    older = _request(
        'Your reply is a JSON object with "spoken" and "written"; an older wording.', schema=True,
    )
    assert tool_exam.spoken_reply_for(older, structured=False) == UNSTRUCTURED
    assert tool_exam.spoken_reply_for(older, structured=True) == STRUCTURED
