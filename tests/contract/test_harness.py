"""The contract harness itself (ADR 0325): what it lets through, what it stops, how it rewrites.

The files of ``contracts/`` are only worth what the comparison is worth, so the comparison has a
table: a field the brain adds passes; a field it renames, drops or retypes fails and is named.
Then the two modes over a throwaway root, and a check over the shipped files: each is well formed,
is written as the one writer writes it, and has a test that drives it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.support.contracts import (
    CONTRACTS_DIR,
    FROM_BRAIN,
    TO_BRAIN,
    WORLD_NOW_MS,
    Contracts,
    brain_at,
    drift,
    dump,
    json_type,
)

SAID = {"type": "said", "utterance_id": "u1", "turn_id": "T1", "verdict": "turn"}

# (what changed, the contract's example, what the brain sends now, the exact keys, the problems)
DRIFT: dict[str, tuple[Any, Any, tuple[str, ...], list[str]]] = {
    "the same": (SAID, SAID, (), []),
    "a field added": (SAID, {**SAID, "record_id": "r"}, (), []),
    "a field renamed": (
        SAID, {**{k: v for k, v in SAID.items() if k != "turn_id"}, "turnId": "T1"}, (),
        ["$.turn_id: the contract has it, the brain no longer sends it"],
    ),
    "a field dropped": (
        SAID, {k: v for k, v in SAID.items() if k != "verdict"}, (),
        ["$.verdict: the contract has it, the brain no longer sends it"],
    ),
    "a string that became a number": (
        SAID, {**SAID, "turn_id": 7}, (),
        ["$.turn_id: the contract has string, the brain sent number"],
    ),
    "a string that became null": (
        SAID, {**SAID, "turn_id": None}, (),
        ["$.turn_id: the contract has string, the brain sent null"],
    ),
    "a null in the contract fixes no type": (
        {**SAID, "turn_id": None}, {**SAID, "turn_id": "T1"}, (), [],
    ),
    "an integer and a float are both numbers": ({"n": 1}, {"n": 1.5}, (), []),
    "a bool is not a number": (
        {"n": 1}, {"n": True}, (), ["$.n: the contract has number, the brain sent boolean"],
    ),
    "a value of an exact key": (
        SAID, {**SAID, "verdict": "elsewhere"}, ("verdict",),
        ["$.verdict: the contract has 'turn', the brain sent 'elsewhere'"],
    ),
    "a value of a key that is not exact": (SAID, {**SAID, "verdict": "elsewhere"}, ("type",), []),
    "a nested field renamed": (
        {"items": [{"id": "a", "kind": "stay"}]}, {"items": [{"id": "a", "type": "stay"}]}, (),
        ["$.items[0].kind: the contract has it, the brain no longer sends it"],
    ),
    "a list with more items": ({"rows": [1]}, {"rows": [1, 2]}, (), []),
    "a list with fewer items": (
        {"rows": [1, 2]}, {"rows": [1]}, (),
        ["$.rows: the contract has 2 items, the brain sent 1"],
    ),
    "an empty list in the contract": ({"rows": []}, {"rows": ["x"]}, (), []),
    "an object that became a list": (
        {"card": {"id": "C1"}}, {"card": []}, (),
        ["$.card: the contract has object, the brain sent array"],
    ),
}


@pytest.mark.parametrize("name", list(DRIFT))
def test_a_field_added_passes_and_one_renamed_dropped_or_retyped_is_named(name: str) -> None:
    """The subset match on keys and types, row by row."""
    example, actual, exact, expected = DRIFT[name]
    assert drift(example, actual, exact=exact) == expected


def test_the_world_clock_is_stopped_in_the_owners_zone_whatever_zone_the_machine_is_in() -> None:
    """A time worded in local time reads the same wherever the tests run."""
    before = datetime.now().astimezone().utcoffset()
    with brain_at(WORLD_NOW_MS):
        inside = datetime.now().astimezone()
        assert (inside.isoformat(timespec="seconds"), time.time()) == (
            "2026-10-14T09:00:00-07:00", WORLD_NOW_MS / 1000,
        )
    assert datetime.now().astimezone().utcoffset() == before


def test_json_types_keep_a_bool_apart_from_a_number() -> None:
    """``True`` is a Python int; on the wire it is not a number."""
    values: list[object] = [None, True, 1, 1.5, "s", [], {}]
    assert [json_type(v) for v in values] == [
        "null", "boolean", "number", "number", "string", "array", "object",
    ]


def test_the_writer_puts_an_object_a_key_a_line_and_a_list_of_plain_values_on_one() -> None:
    """The layout every file has, so an update's diff holds only what changed."""
    assert dump({"a": [1, "x", None], "b": {"c": [[0, 1], [2, 3]]}, "d": {}, "e": []}) == (
        '{\n  "a": [1, "x", null],\n  "b": {\n    "c": [\n      [0, 1],\n      [2, 3]\n    ]\n  },'
        '\n  "d": {},\n  "e": []\n}'
    )


# --- the two modes, over a root of their own ------------------------------------------------


def _outputs(root: Path, *, update: bool) -> Contracts:
    return Contracts(update=update, root=root)


def _say(contracts: Contracts, sent: dict[str, Any]) -> None:
    contracts.output(
        "phone_ws/said", example=sent, exact=("type",), frame="said", socket="/phone/ws",
        description="The answer to a say.",
    )


def test_an_update_writes_the_example_and_a_later_run_compares_against_it(tmp_path: Path) -> None:
    """``--update-contracts`` makes the file; without it a changed shape fails and says how."""
    _say(_outputs(tmp_path, update=True), SAID)
    file = tmp_path / "phone_ws" / "said.json"
    assert json.loads(file.read_text(encoding="utf-8")) == {
        "frame": "said", "socket": "/phone/ws", "direction": FROM_BRAIN,
        "description": "The answer to a say.", "exact": ["type"], "example": SAID,
    }

    _say(_outputs(tmp_path, update=False), {**SAID, "record_id": "r"})  # a field added: still holds
    renamed = {k: v for k, v in SAID.items() if k != "turn_id"}
    with pytest.raises(pytest.fail.Exception, match=r"\$\.turn_id.*no longer sends it"):
        _say(_outputs(tmp_path, update=False), renamed)

    _say(_outputs(tmp_path, update=True), renamed)  # the rename is meant: the file follows it
    _say(_outputs(tmp_path, update=False), renamed)
    assert "turn_id" not in json.loads(file.read_text(encoding="utf-8"))["example"]


def test_a_contract_that_does_not_exist_is_a_failure_naming_the_command(tmp_path: Path) -> None:
    """Nothing is written unless asked to."""
    with pytest.raises(pytest.fail.Exception, match="update-contracts"):
        _say(_outputs(tmp_path, update=False), SAID)
    assert list(tmp_path.iterdir()) == []


def test_the_files_name_the_route_the_test_drives_and_the_direction(tmp_path: Path) -> None:
    """A test driving another route than its file names is a failure, not a pass."""
    _say(_outputs(tmp_path, update=True), SAID)
    with pytest.raises(pytest.fail.Exception, match="`frame` is 'said', the test drives 'ready'"):
        _outputs(tmp_path, update=False).output(
            "phone_ws/said", example=SAID, exact=("type",), frame="ready", socket="/phone/ws",
        )
    with pytest.raises(pytest.fail.Exception, match="`direction`"):
        _outputs(tmp_path, update=False).inputs("phone_ws/said", frame="said", socket="/phone/ws")


def test_variants_are_compared_by_name_and_a_missing_one_is_named(tmp_path: Path) -> None:
    """A message with a variant per verdict: all must still be produced, each must still fit."""
    kinds = {"turn": SAID, "elsewhere": {**SAID, "turn_id": None, "verdict": "elsewhere"}}
    base: dict[str, Any] = {"exact": ("type",), "frame": "said", "socket": "/phone/ws"}
    _outputs(tmp_path, update=True).output("phone_ws/said", examples=kinds, **base)
    _outputs(tmp_path, update=False).output("phone_ws/said", examples=kinds, **base)
    with pytest.raises(pytest.fail.Exception, match="variant 'elsewhere' is no longer produced"):
        _outputs(tmp_path, update=False).output(
            "phone_ws/said", examples={"turn": SAID}, **base,
        )
    with pytest.raises(pytest.fail.Exception, match=r"\$\.turn\.turn_id"):
        _outputs(tmp_path, update=False).output(
            "phone_ws/said", examples={**kinds, "turn": {**SAID, "turn_id": 1}}, **base,
        )


def test_an_update_never_edits_the_lists_a_client_is_held_to(tmp_path: Path) -> None:
    """``valid`` and ``invalid`` are the specification; an update only fixes their layout."""
    (tmp_path / "phone_ws").mkdir()
    file = tmp_path / "phone_ws" / "hello.json"
    doc = {
        "frame": "hello", "socket": "/phone/ws", "direction": TO_BRAIN,
        "valid": [{"name": "voice", "value": {"type": "hello", "voice": True}}],
        "invalid": [
            {"name": "text", "value": {"type": "hello", "voice": "yes"}, "refusal": "closed"},
        ],
    }
    file.write_text(json.dumps(doc), encoding="utf-8")
    inputs = _outputs(tmp_path, update=True).inputs(
        "phone_ws/hello", frame="hello", socket="/phone/ws",
    )
    assert [c.name for c in inputs.valid] == ["voice"]
    assert inputs.invalid[0].refusal == "closed"
    assert json.loads(file.read_text(encoding="utf-8")) == doc
    assert file.read_text(encoding="utf-8") == dump(doc) + "\n"


def test_an_invalid_case_without_a_refusal_or_a_repeated_name_is_a_failure(tmp_path: Path) -> None:
    """The lists must be usable as they are: every refusal named, every case told apart."""
    (tmp_path / "x").mkdir()
    doc = {
        "route": "POST /x", "direction": TO_BRAIN,
        "valid": [{"name": "a", "value": 1}, {"name": "a", "value": 2}],
        "invalid": [{"name": "b", "value": 3}],
    }
    (tmp_path / "x" / "y.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(pytest.fail.Exception) as raised:
        _outputs(tmp_path, update=False).inputs("x/y", route="POST /x")
    assert "`valid` names 'a' twice" in str(raised.value)
    assert "invalid case 'b' names no refusal" in str(raised.value)


# --- the shipped files -----------------------------------------------------------------------


_SHIPPED = Contracts(root=CONTRACTS_DIR)
_TESTS = Path(__file__).parent


def test_every_shipped_contract_is_well_formed_and_written_as_the_writer_writes_it() -> None:
    """One identity, one direction, its examples or its lists; as an update writes it."""
    for name in _SHIPPED.names():
        text = _SHIPPED.path(name).read_text(encoding="utf-8")
        doc = json.loads(text)
        assert text == dump(doc) + "\n", f"{name}: run --update-contracts to lay it out"
        assert doc["direction"] in {TO_BRAIN, FROM_BRAIN}, name
        assert ("route" in doc) != ("frame" in doc), f"{name}: name a route or a frame"
        if doc["direction"] == TO_BRAIN:
            assert doc["valid"], f"{name}: no valid case"
            assert doc["invalid"], f"{name}: no invalid case"
            assert "example" not in doc, name
        else:
            assert ("example" in doc) != ("examples" in doc), f"{name}: one example or named ones"
            assert not set(doc).issuperset({"valid", "invalid"}), name


def test_every_shipped_contract_has_a_test_that_drives_it() -> None:
    """A file no test reads is a promise nobody keeps."""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(_TESTS.glob("test_*.py"))
        if path.name != Path(__file__).name
    )
    assert [name for name in _SHIPPED.names() if f'"{name}"' not in sources] == []
