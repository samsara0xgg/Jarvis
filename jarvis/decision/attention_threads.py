"""L3 attention threads — what each key of his attention served, told by the writer (ADR 0331).

The writer of the doing lane is shown, besides the day, the day's keys that nobody has told yet
(:mod:`jarvis.state.attention`), each with how long it held him, how long he typed in it, where
and when, the keys told before to serve different things at different times that held him today,
and the threads already told, each with a few of its keys. Its answer names a thread and its
category for each new key, or says the key serves different things at different times; then, in
each span it writes, what each such key served there (:func:`read_serves`, ADR 9902). Keys are
named by short labels (``k1``, ``v1``) the program gave, so an answer can only tell a key that was
asked about. The runtime owns when it runs and the client; L2 owns the pieces and the write.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from jarvis.decision.day_prose import read_object
from jarvis.state.doing import CATEGORIES

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.state.attention import Piece

THREAD_MAX_CHARS: Final = 24
"""The longest name of a thread: a project, a person, a show."""
MIN_KEY_MS: Final = 60_000
"""A key that held him less than this in the day is not asked about: it stays itself."""
MAX_KEYS: Final = 150
"""The most keys one ask holds, those that held him longest; the rest wait for the next ask."""
MAX_EXAMPLES: Final = 4
"""The most keys shown for each known thread."""
THREAD_CATEGORIES: Final = tuple(c for c in CATEGORIES if c != "unknown")
"""What a told thread can be: what he was doing is known once a thread is named."""
_TYPED_MIN_MS: Final = 5_000

type Known = tuple[str, str | None, list[str]]
"""A thread told so far: its name, its category and the names of some of its keys."""


@dataclass(frozen=True)
class Asked:
    """One key asked about, with what it held of the day."""

    key: str
    name: str
    where: str
    ms: float
    typed_ms: float
    first_ms: int
    last_ms: int


def untold(pieces: Iterable[Piece], told: Mapping[str, object]) -> list[Asked]:
    """The keys of ``pieces`` nobody told, that held him at least :data:`MIN_KEY_MS`, longest first.

    Time alongside counts: a key seen only beside the Mac still needs a thread. At most
    :data:`MAX_KEYS`.
    """
    return _ranked(p for p in pieces if p.key not in told)


def varying(
    pieces: Iterable[Piece], told: Mapping[str, tuple[str | None, str | None]],
) -> list[Asked]:
    """The keys of ``pieces`` told to serve different things at different times (no thread).

    Those that held him at least :data:`MIN_KEY_MS`, longest first, at most :data:`MAX_KEYS`;
    time alongside counts, as in :func:`untold`.
    """
    return _ranked(p for p in pieces if p.key in told and told[p.key][0] is None)


def _ranked(pieces: Iterable[Piece]) -> list[Asked]:
    ms: dict[str, float] = defaultdict(float)
    typed: dict[str, float] = defaultdict(float)
    where: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: dict[str, str] = {}
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for p in pieces:
        ms[p.key] += p.ms
        typed[p.key] += p.typed_ms
        where[p.key][p.where] += p.ms
        names.setdefault(p.key, p.name)
        first[p.key] = min(first.get(p.key, p.start_ms), p.start_ms)
        last[p.key] = max(last.get(p.key, p.end_ms), p.end_ms)
    ranked = sorted((k for k in ms if ms[k] >= MIN_KEY_MS), key=lambda k: (-ms[k], k))
    return [
        Asked(
            k, names[k], max(where[k], key=lambda w: (where[k][w], w)), ms[k], typed[k],
            first[k], last[k],
        )
        for k in ranked[:MAX_KEYS]
    ]


def known_threads(
    told: Mapping[str, tuple[str | None, str | None]], names: Mapping[str, str],
) -> list[Known]:
    """The threads ``told`` names, each with its category and up to :data:`MAX_EXAMPLES` keys.

    ``names`` says how a key is shown; a key with no name there is shown as the key itself.
    """
    keys: dict[str, list[str]] = defaultdict(list)
    category: dict[str, str | None] = {}
    for key, (thread, cat) in told.items():
        if thread is None:
            continue
        keys[thread].append(names.get(key, key))
        category[thread] = cat
    return [(t, category[t], sorted(keys[t])[:MAX_EXAMPLES]) for t in sorted(keys)]


def _clock(ms: int, zone: tzinfo) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def labels(asked: Sequence[Asked], varies: Sequence[Asked]) -> dict[str, str]:
    """The labels keys are shown by: ``k1``, ... for new keys, ``v1``, ... for keys that vary."""
    return {
        **{f"k{n}": one.key for n, one in enumerate(asked, 1)},
        **{f"v{n}": one.key for n, one in enumerate(varies, 1)},
    }


def _key_lines(prefix: str, keys: Sequence[Asked], zone: tzinfo) -> list[str]:
    lines = []
    for n, one in enumerate(keys, 1):
        place = "Mac" if one.where == "mac" else one.where
        typed = f", typed {round(one.typed_ms / 1000)}s" if one.typed_ms >= _TYPED_MIN_MS else ""
        lines.append(
            f"{prefix}{n} {one.name} | {place}, {max(1, round(one.ms / 60_000))}m{typed}, "
            f"{_clock(one.first_ms, zone)}-{_clock(one.last_ms, zone)}",
        )
    return lines


def brief_lines(
    asked: Sequence[Asked], varies: Sequence[Asked], known: Sequence[Known], zone: tzinfo,
) -> list[str]:
    """The writer's sections on keys: the threads told so far, the keys to tell, those that vary.

    Empty when no key is asked about and none that varies held him; each list only when it has a
    key.
    """
    if not asked and not varies:
        return []
    threads = [
        f"- {name}" + (f" ({cat})" if cat else "") + (f": {', '.join(keys)}" if keys else "")
        for name, cat, keys in known
    ] or ["(none yet)"]
    head = "(id name | where, minutes that day, seconds typed, first-last time)"
    return [
        "KNOWN THREADS (name (category): some of its keys):\n" + "\n".join(threads),
        *([f"NEW KEYS {head}:\n" + "\n".join(_key_lines("k", asked, zone))] if asked else []),
        *(
            [f"VARYING KEYS {head}:\n" + "\n".join(_key_lines("v", varies, zone))]
            if varies else []
        ),
    ]


class _Rejected(Exception):  # noqa: N818 — a gate's verdict, caught where the answer is read
    """Why an answer may not be stored."""


def _entry(
    one: object, labels: Mapping[str, str], found: Mapping[str, object], *, where: str = "",
) -> tuple[str, tuple[str | None, str | None]]:
    """One entry of the answer as (key, (thread, category)); raises :class:`_Rejected`.

    ``where`` names the span an entry of its ``serves`` is in, for the refusal.
    """
    if not isinstance(one, dict) or one.get("key") not in labels:
        msg = f"{where}an entry names no key that may be told there: {one!r}"[:200]
        raise _Rejected(msg)
    label = str(one["key"])
    if labels[label] in found:
        msg = f"{where}{label} is told twice"
        raise _Rejected(msg)
    thread, category = one.get("thread"), one.get("category")
    if thread is None:
        if category is not None:
            msg = f"{where}{label} has no thread, so it has no category"
            raise _Rejected(msg)
        return labels[label], (None, None)
    if not isinstance(thread, str) or not thread.strip():
        msg = f"{where}{label}'s thread is not a name"
        raise _Rejected(msg)
    if len(thread.strip()) > THREAD_MAX_CHARS:
        msg = f"{where}{label}'s thread is longer than {THREAD_MAX_CHARS} characters"
        raise _Rejected(msg)
    if category not in THREAD_CATEGORIES:
        msg = f"{where}{label}'s category must be one of {', '.join(THREAD_CATEGORIES)}"
        raise _Rejected(msg)
    return labels[label], (thread.strip(), str(category))


def read_told(
    text: str | None, finish_reason: str | None, asked: Sequence[Asked],
) -> dict[str, tuple[str | None, str | None]] | str:
    """What each asked key served, as (thread, category) by key, or why none may be stored.

    The writer's answer holds ``"threads": [{"key", "thread", "category"}, ...]`` that tells
    every key asked about exactly once, by its label. A thread is a name of at most
    :data:`THREAD_MAX_CHARS` characters with a category of :data:`THREAD_CATEGORIES`, or null with
    a null category: the key serves different things at different times, told span by span
    (:func:`read_serves`). One bad entry refuses all of it. With no key asked there is nothing to
    tell, whatever the answer holds.
    """
    if not asked:
        return {}
    data = read_object(text, finish_reason)
    if isinstance(data, str):
        return data
    raw = data.get("threads")
    if not isinstance(raw, list):
        return 'no "threads" list for the new keys'
    labels = {f"k{n}": one.key for n, one in enumerate(asked, 1)}
    found: dict[str, tuple[str | None, str | None]] = {}
    try:
        for one in raw:
            key, told = _entry(one, labels, found)
            found[key] = told
    except _Rejected as rejected:
        return str(rejected)
    if missing := [label for label, key in labels.items() if key not in found]:
        return f"keys not told: {', '.join(missing[:10])}"
    return found


def held_in(pieces: Iterable[Piece], start_ms: int, end_ms: int) -> dict[str, float]:
    """How long each key of ``pieces`` held him inside ``[start_ms, end_ms)``, alongside too."""
    ms: dict[str, float] = defaultdict(float)
    for p in pieces:
        lo, hi = max(p.start_ms, start_ms), min(p.end_ms, end_ms)
        if hi > lo:
            ms[p.key] += (hi - lo) * p.share
    return dict(ms)


def read_serves(
    raw: object, varies: Mapping[str, str], held: Mapping[str, float], where: str,
) -> list[dict[str, str | None]] | str:
    """What a span says each key that varies served in it, as a run stores it, or why it may not.

    ``raw`` is the span's ``"serves"`` (absent or null: none); ``varies`` the keys a span may tell
    there, by label; ``held`` how long each key held him inside the span; ``where`` names the span
    in a refusal. Each entry is ``{"key", "thread", "category"}`` by a label of ``varies``, told
    at most once, with a thread as in :func:`read_told`, or both null: what it served there cannot
    be told. Every key of ``varies`` that held him at least :data:`MIN_KEY_MS` inside the span is
    told. Stored by key, not label.
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return f'{where}"serves" is not a list'
    found: dict[str, tuple[str | None, str | None]] = {}
    try:
        for one in raw:
            key, told = _entry(one, varies, found, where=where)
            found[key] = told
    except _Rejected as rejected:
        return str(rejected)
    missing = [
        label for label, key in varies.items()
        if key not in found and held.get(key, 0.0) >= MIN_KEY_MS
    ]
    if missing:
        return f"{where}keys that vary and held him there are not told: {', '.join(missing[:10])}"
    return [
        {"key": key, "thread": thread, "category": category}
        for key, (thread, category) in found.items()
    ]


__all__ = [
    "MAX_EXAMPLES",
    "MAX_KEYS",
    "MIN_KEY_MS",
    "THREAD_CATEGORIES",
    "THREAD_MAX_CHARS",
    "Asked",
    "Known",
    "brief_lines",
    "held_in",
    "known_threads",
    "labels",
    "read_serves",
    "read_told",
    "untold",
    "varying",
]
