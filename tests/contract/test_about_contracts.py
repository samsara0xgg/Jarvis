"""``about``: the item a paired phone has open when its owner speaks (ADR 0214).

The phone puts ``about`` on a say or a submit so a question like "when is this?" has something to
point at. It carries a third party's words (an invite's subject), so the brain checks it: closed
keys, an id of a fixed shape, a title flattened to one line, a start in range. The file holds what
the phone may send; the test posts each as ``POST /inherent/submit`` from a paired phone and
checks what the brain starts the turn with. A ``say`` on the socket runs the same check
(``contracts/phone_ws/say.json``).
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from jarvis.shared.about import ABOUT_KINDS, MAX_ID_CHARS, MAX_START_MS, MAX_TITLE_CHARS
from jarvis.state.device_tokens import device_name_for_token
from tests.support.timeline import peers_with

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.contracts import Contracts

ROUTE = "POST /inherent/submit"
WORDS = "when is this"


def test_the_brain_takes_the_item_a_phone_has_open_as_it_is_checked_and_refuses_the_rest(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """A valid ``about`` reaches the turn cleaned (``becomes``); an invalid one is a 422."""
    inputs = contracts.inputs("about", route=ROUTE)
    started: list[tuple[str, str, Any]] = []

    def start(
        device: str, text: str, _ids: object, about: Any, _utterance: object, _via: object,  # noqa: ANN401
    ) -> str:
        started.append((device, text, about))
        return "T1"

    peers = peers_with(
        tmp_path, submit_as_device=start,
        device_name=functools.partial(device_name_for_token, tmp_path),
    )

    def post(about: Any) -> int:  # noqa: ANN401 - whatever the phone sent
        body = {"text": WORDS, "about": about}
        return peers.paired.post("/inherent/submit", json=body).status_code

    # a stretch of the doing lane is named by its window (ADR 0410)
    window = {"start_ms": 0, "end_ms": 1}
    kinds = {
        kind: post({"kind": kind, "id": "x", **(window if kind == "doing" else {})})
        for kind in inputs.doc["kinds"]
    }
    taken = {case.name: post(case.value) for case in inputs.valid}
    refused = {case.name: post(case.value) for case in inputs.invalid}

    assert tuple(inputs.doc["kinds"]) == ABOUT_KINDS, "the kinds the brain knows changed"
    assert set(kinds.values()) == {200}
    assert taken == {case.name: 200 for case in inputs.valid}
    assert refused == {case.name: int(case.refusal or 0) for case in inputs.invalid}
    # what the turn starts with: the item as checked, or as the case says it becomes
    after_kinds = started[len(kinds) :]
    assert [(device, text) for device, text, _ in after_kinds] == [("phone", WORDS)] * len(
        inputs.valid,
    )
    raw = inputs.doc["valid"]
    assert [about for _, _, about in after_kinds] == [
        row["becomes"] if "becomes" in row else row["value"] for row in raw
    ]
    assert inputs.doc["limits"] == {
        "id_chars": MAX_ID_CHARS, "title_chars": MAX_TITLE_CHARS, "start_ms": MAX_START_MS,
    }
