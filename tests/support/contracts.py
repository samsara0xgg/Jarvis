"""The cross-device contract fixtures of ``contracts/``: load them, compare them, rewrite them.

One JSON file per message the brain and a client (the iPhone, the desktop) exchange. The files are
the one place both ends read, so a field the brain renames or drops shows up as a red test here
instead of as a blank screen on the phone.

A message the brain sends carries ``example`` (or, for a message with named variants such as
``said`` per verdict, ``examples``), which a test produces from the real route or serializer and
this module holds the brain's answer to. The comparison is a subset match on keys and types: a
field the brain adds passes, a field it renames, drops or changes the type of fails. ``exact`` names
the keys whose value is the contract too (a frame's ``type``, a state, a kind): those must also
be equal. An ``example`` of ``null`` fixes no type. A list is compared item by item over the
contract's length; the brain may send more items than the contract shows, not fewer.

A message a client sends carries ``valid`` and ``invalid`` lists of ``{name, value}`` (an invalid
one also names the ``refusal`` the brain answers). Those are the specification, written by hand:
the test feeds each to the real route and checks the brain takes every valid one and refuses every
invalid one. ``pytest --update-contracts`` rewrites only the examples, from what the routes answer
now, so a change of shape is a diff in review; it never edits the lists.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
import time_machine

from tests.support.clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping

CONTRACTS_DIR: Final = Path(__file__).parents[2] / "contracts"
"""Where the files live: the repository's ``contracts/``, read by iOS and the desktop too."""

WORLD_NOW_MS: Final = 1_791_993_600_000
"""2026-10-14 09:00 in Vancouver: the instant :class:`FakeClock` starts at, and the brain's clock
in every contract world."""

TO_BRAIN: Final = "client -> brain"
FROM_BRAIN: Final = "brain -> client"
_IDENTITY: Final = ("route", "frame", "socket")
_UPDATE_HINT: Final = (
    "If the change is meant, run `pytest tests/contract --update-contracts`, review the diff of "
    "contracts/ and commit it with the change; the iOS and desktop tests read the same files."
)
_MISSING: Final = object()


@contextlib.contextmanager
def brain_at(now_ms: int = WORLD_NOW_MS) -> Iterator[FakeClock]:
    """The brain's clock stopped at ``now_ms``: what its routes read as the time, and a clock.

    The routes ask ``time.time()`` for the moment, so it is frozen; the returned
    :class:`FakeClock` is the same instant for code that takes a clock.
    """
    clock = FakeClock(datetime.fromtimestamp(now_ms / 1000, tz=UTC))
    with time_machine.travel(clock.utc_now(), tick=False):
        yield clock


def json_type(value: object) -> str:
    """The JSON type of a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    msg = f"{type(value).__name__} is not JSON"
    raise TypeError(msg)


def drift(
    example: object, actual: object, *, exact: Collection[str] = (), path: str = "$",
) -> list[str]:
    """Where ``actual`` no longer fits ``example``; empty when the contract still holds.

    Every key of an example object must be there with the example's type; extra keys are fine.
    A key named in ``exact`` must also hold the example's value, wherever it appears.
    """
    if example is None:
        return []  # the contract fixes no type here: another variant carries it
    want, seen = json_type(example), json_type(actual)
    if want != seen:
        return [f"{path}: the contract has {want}, the brain sent {seen}"]
    problems: list[str] = []
    if isinstance(example, dict) and isinstance(actual, dict):
        for key, value in example.items():
            if key not in actual:
                problems.append(f"{path}.{key}: the contract has it, the brain no longer sends it")
                continue
            problems += drift(value, actual[key], exact=exact, path=f"{path}.{key}")
            if key in exact and actual[key] != value:
                problems.append(
                    f"{path}.{key}: the contract has {value!r}, the brain sent {actual[key]!r}",
                )
    elif isinstance(example, list) and isinstance(actual, list):
        if len(actual) < len(example):
            problems.append(
                f"{path}: the contract has {len(example)} items, the brain sent {len(actual)}",
            )
        for index, (item, sent) in enumerate(zip(example, actual, strict=False)):
            problems += drift(item, sent, exact=exact, path=f"{path}[{index}]")
    return problems


def _inline(value: object) -> bool:
    return not isinstance(value, dict | list)


def dump(value: object, depth: int = 0) -> str:
    """The text a contract file holds: objects a key a line, a list of plain values on one line.

    One writer for every file keeps the diff of an update to the lines that really changed.
    """
    pad, close = "  " * (depth + 1), "  " * depth
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = ",\n".join(
            f"{pad}{json.dumps(key, ensure_ascii=False)}: {dump(item, depth + 1)}"
            for key, item in value.items()
        )
        return f"{{\n{rows}\n{close}}}"
    if isinstance(value, list):
        if all(_inline(item) for item in value):
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        rows = ",\n".join(f"{pad}{dump(item, depth + 1)}" for item in value)
        return f"[\n{rows}\n{close}]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class Case:
    """One thing a client may send: what it is called, the value, and (invalid) the refusal."""

    name: str
    value: Any
    refusal: str | None = None


@dataclass(frozen=True)
class Inputs:
    """The ``valid`` and ``invalid`` lists of a message a client sends."""

    doc: Mapping[str, Any]
    valid: list[Case]
    invalid: list[Case]

    @property
    def now_ms(self) -> int:
        """The brain's clock these cases were judged at (a time in a case is read against it)."""
        return int(self.doc.get("now_ms", WORLD_NOW_MS))


class Contracts:
    """The contract files of one root, compared as they are or (``update``) rewritten."""

    def __init__(self, *, update: bool = False, root: Path = CONTRACTS_DIR) -> None:
        """Read ``root``; with ``update`` the brain's answers replace the examples there."""
        self.update = update
        self.root = root

    def path(self, name: str) -> Path:
        """The file of ``name`` (``group/message``)."""
        return self.root / f"{name}.json"

    def names(self) -> list[str]:
        """Every contract under the root, as ``group/message``, sorted."""
        return sorted(
            str(path.relative_to(self.root).with_suffix("")) for path in self.root.rglob("*.json")
        )

    def load(self, name: str) -> dict[str, Any]:
        """The decoded file of ``name``; a missing one fails the test with what to run."""
        path = self.path(name)
        if not path.exists():
            pytest.fail(f"contracts/{name}.json does not exist. {_UPDATE_HINT}", pytrace=False)
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def inputs(self, name: str, **identity: str) -> Inputs:
        """The cases of a message a client sends; ``identity`` is the message the test drives."""
        doc = self.load(name)
        problems = self._identity_problems(doc, identity, TO_BRAIN)
        cases: dict[str, list[Case]] = {"valid": [], "invalid": []}
        for kind, found in cases.items():
            for row in doc.get(kind, []):
                found.append(Case(row["name"], row["value"], row.get("refusal")))
            names = [case.name for case in found]
            if not names:
                problems.append(f"`{kind}` is empty")
            problems += [
                f"`{kind}` names {one!r} twice"
                for one in sorted(set(names))
                if names.count(one) > 1
            ]
        problems += [
            f"invalid case {case.name!r} names no refusal"
            for case in cases["invalid"]
            if not case.refusal
        ]
        self._fail_on(name, problems)
        if self.update:
            self._write(name, doc)  # only the layout: the lists are the specification
        return Inputs(doc, cases["valid"], cases["invalid"])

    def output(  # noqa: PLR0913 - the message's identity, its one or many examples and how to read them
        self,
        name: str,
        *,
        example: object = _MISSING,
        examples: Mapping[str, object] | None = None,
        exact: Collection[str] = (),
        now_ms: int | None = None,
        description: str = "",
        **identity: str,
    ) -> None:
        """Hold what the brain sent to ``name``'s contract, or (``update``) write it there.

        ``example`` is one answer, ``examples`` named variants of it; both come from the real
        route or serializer, never typed. ``identity`` is ``route=...`` for an HTTP route or
        ``frame=...`` and ``socket=...`` for a frame, checked against the file.
        """
        if (example is _MISSING) == (examples is None):
            msg = "give example or examples, not both"
            raise TypeError(msg)
        produced: dict[str, Any] = (
            {"example": example} if examples is None else {"examples": dict(examples)}
        )
        if self.update:
            self._rewrite(name, produced, exact, now_ms, description, identity)
            return
        doc = self.load(name)
        problems = self._identity_problems(doc, identity, FROM_BRAIN)
        if sorted(doc.get("exact", [])) != sorted(exact):
            problems.append(f"`exact` is {doc.get('exact', [])}, the test checks {sorted(exact)}")
        if now_ms is not None and doc.get("now_ms") != now_ms:
            problems.append(f"`now_ms` is {doc.get('now_ms')}, the world is at {now_ms}")
        key = next(iter(produced))
        if key not in doc:
            problems.append(f"`{key}` is missing from the file")
        elif examples is None:
            problems += drift(doc["example"], example, exact=exact, path="$")
        else:
            held = doc["examples"]
            problems += [
                f"variant {one!r} is no longer produced" for one in held if one not in examples
            ]
            problems += [
                f"variant {one!r} has no example in the file" for one in examples if one not in held
            ]
            for one in held.keys() & examples.keys():
                problems += drift(held[one], examples[one], exact=exact, path=f"$.{one}")
        self._fail_on(name, problems)

    def _identity_problems(
        self, doc: Mapping[str, Any], identity: Mapping[str, str], direction: str,
    ) -> list[str]:
        problems = [] if doc.get("direction") == direction else [
            f"`direction` is {doc.get('direction')!r}, this message goes {direction!r}",
        ]
        problems += [
            f"`{key}` is {doc.get(key)!r}, the test drives {identity.get(key)!r}"
            for key in _IDENTITY
            if doc.get(key) != identity.get(key)
        ]
        return problems

    def _rewrite(  # noqa: PLR0913 - everything a file's header and its answer hold
        self,
        name: str,
        produced: Mapping[str, Any],
        exact: Collection[str],
        now_ms: int | None,
        description: str,
        identity: Mapping[str, str],
    ) -> None:
        path = self.path(name)
        old: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        doc: dict[str, Any] = {key: identity[key] for key in _IDENTITY if key in identity}
        doc["direction"] = FROM_BRAIN
        if description or old.get("description"):
            doc["description"] = description or old["description"]
        if now_ms is not None:
            doc["now_ms"] = now_ms
        if exact:
            doc["exact"] = sorted(exact)
        doc.update(produced)
        self._write(name, doc)

    def _write(self, name: str, doc: Mapping[str, Any]) -> None:
        path = self.path(name)
        text = dump(doc) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def _fail_on(self, name: str, problems: list[str]) -> None:
        if problems:
            lines = "\n".join(f"  {one}" for one in problems)
            pytest.fail(
                f"contracts/{name}.json no longer fits:\n{lines}\n{_UPDATE_HINT}", pytrace=False,
            )
