"""L3 reading of his words against a waiting card (ADR 0321).

Words the exact-sentence grammar does not match, said in the card's answer window, go to one
closed-choice request on a fast preset. It sees his words and the card's question lines and
nothing else, and answers through a forced tool call whose argument is an enum: nothing is
parsed out of free text. Only ``accept`` lets code mint the leases, and code mints them from
the frozen card, so these words never change an argument.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.decision.llm import ChatResult

Verdict = Literal["accept", "reject", "change", "unrelated", "not_sure"]
VERDICTS: Final[tuple[Verdict, ...]] = get_args(Verdict)

ANSWER_TOOL: Final[str] = "answer"

_ANSWER_TOOL_SCHEMA: Final[dict[str, object]] = {
    "name": ANSWER_TOOL,
    "description": "Report how the user's words relate to the card.",
    "input_schema": {
        "type": "object",
        "properties": {"verdict": {"type": "string", "enum": list(VERDICTS)}},
        "required": ["verdict"],
    },
}

SYSTEM: Final[str] = (
    "The user was shown a confirmation card and then spoke. Decide how their words relate to "
    "the card: accept (they agree to everything on it), reject (they refuse it), change (they "
    "want it done differently), unrelated (they are saying something else), or not_sure. "
    "Answer by calling the answer tool once."
)


@dataclass(frozen=True)
class WordsRequest:
    """The one request: what the model is sent."""

    system: str
    messages: list[dict[str, object]]
    tools: list[dict[str, object]]
    tool_choice: str


def build_request(words: str, questions: Sequence[str]) -> WordsRequest:
    """His words and the card's question lines, and nothing of the conversation."""
    card = "\n".join(f"- {line}" for line in questions)
    return WordsRequest(
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Card:\n{card}\n\nUser said: {words}"}],
        tools=[dict(_ANSWER_TOOL_SCHEMA)],
        tool_choice="required",
    )


def verdict_of(result: ChatResult) -> Verdict:
    """The enum the forced call carried; anything else is ``not_sure``."""
    for call in result.tool_calls:
        if call.name != ANSWER_TOOL:
            continue
        try:
            arguments = json.loads(call.arguments_json)
        except ValueError:
            return "not_sure"
        said = arguments.get("verdict") if isinstance(arguments, dict) else None
        return next((one for one in VERDICTS if one == said), "not_sure")
    return "not_sure"
