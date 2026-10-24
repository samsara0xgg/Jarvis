"""L3 topics night (ADR 0390): the model's input and the gate on its answer.

One past day goes in with the topics kept so far and the talk of the 14 days before that no topic
holds; the topics that day touched come out, with the lines filed under each, or the reason to
store nothing. Lines are cited by short labels (``r1`` for the day's records, ``e1`` for earlier
talk) that this module maps back to record ids; topics by their own ids (``t3``). The runtime owns
when it runs and the client; L2 owns the topics and the write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.topics import LINE_MAX, NAME_MAX, RECAP_MAX, NightPlan, NightTopic

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.state.topics import Topic

NEW_TOPIC_DAYS: Final = 2
"""A new topic's matter was talked about on at least this many days..."""
NEW_TOPIC_LINES: Final = 6
"""...or in at least this many lines of the one day."""
EARLIER_DAYS: Final = 14
"""How far back the night looks for talk no topic holds yet."""
HER_LINE_CHARS: Final = 600
"""Her answers are cut to this in the input: what she said is in his next words if it mattered."""
HIS_LINE_CHARS: Final = 2000
EARLIER_CHARS: Final = 200
_FENCE: Final = "```"
_RECORD_ID: Final = re.compile(r"\s*\[record_id=([^\]\s]+)\]")
_STATES: Final = frozenset({"open", "done"})


@dataclass(frozen=True)
class KeptTopic:
    """A listed topic as the night reads it: the topic, and the day it was last talked about."""

    topic: Topic
    last_day: str


@dataclass(frozen=True)
class Mention:
    """Earlier talk no topic holds: its day, what was talked about, the record it rests on."""

    day: str
    text: str
    record_id: str


@dataclass(frozen=True)
class DayLine:
    """One message of the day, with the topics it was said in."""

    record_id: str
    ts: str
    source: str
    text: str
    topics: tuple[str, ...]


@dataclass(frozen=True)
class NightAnswer:
    """The checked plan, and the new topics dropped for not reaching the bar, by name."""

    plan: NightPlan
    dropped: tuple[str, ...]


def summary_mentions(day: str, summary: str) -> list[Mention]:
    """The ``### Topics`` lines of a day summary that name the record they rest on.

    A day kept word for word (a short day, ADR 0142) has no such lines; the caller reads its
    records instead.
    """
    found: list[Mention] = []
    inside = False
    for raw in summary.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            inside = line[4:].strip() == "Topics"
            continue
        if not (inside and line.startswith("- ")):
            continue
        cited = _RECORD_ID.search(line)
        text = _RECORD_ID.sub("", line[2:]).strip()
        if cited is not None and text and text.casefold() != "none":
            found.append(Mention(day, text, cited.group(1)))
    return found


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit].rstrip()}… ({len(flat) - limit} more characters)"


def labels(
    day: str, earlier: Sequence[Mention], lines: Sequence[DayLine],
) -> dict[str, tuple[str, str]]:
    """``{label: (record id, its day)}``: ``e1``.. for earlier talk, ``r1``.. for ``day``'s lines.

    A 32-hex record id is easy for a model to mistype; a short label is not.
    """
    found = {f"e{n}": (one.record_id, one.day) for n, one in enumerate(earlier, start=1)}
    found.update({f"r{n}": (one.record_id, day) for n, one in enumerate(lines, start=1)})
    return found


def build_night_messages(
    *, day: str, kept: Sequence[KeptTopic], earlier: Sequence[Mention], lines: Sequence[DayLine],
) -> list[dict[str, Any]]:
    """The one user message the night sees: the topics, the earlier talk, the day's records."""
    rows = ["[Topics; each line: id | state | name | last talked | where it stands | recap]"]
    for one in kept:
        topic = one.topic
        recap = "" if topic.state == "done" else f" | {' '.join(topic.recap.split())}"
        rows.append(
            f"{topic.id} | {topic.state} | {topic.name} | {one.last_day} | {topic.line}{recap}",
        )
    if not kept:
        rows.append("(none yet)")
    rows += [
        "",
        f"[Earlier talk not under any topic, the {EARLIER_DAYS} days before {day}; each line: "
        "label | day | what was talked about]",
    ]
    rows.extend(
        f"e{n} | {talk.day} | {_cut(talk.text, EARLIER_CHARS)}"
        for n, talk in enumerate(earlier, start=1)
    )
    if not earlier:
        rows.append("(none)")
    rows += [
        "",
        f"[Records of {day}, oldest first; each line: label | time | speaker | words | topics it "
        "was said in]",
    ]
    for n, said in enumerate(lines, start=1):
        limit = HIS_LINE_CHARS if said.source == "allen" else HER_LINE_CHARS
        said_in = ", ".join(said.topics) or "-"
        rows.append(
            f"r{n} | {said.ts[11:16]} | {said.source} | {_cut(said.text, limit)} | {said_in}",
        )
    return [{"role": "user", "content": "\n".join(rows)}]


def _parse(text: str) -> dict[str, Any] | str:
    body = text.strip()
    if body.startswith(_FENCE):  # a fenced answer is still the one JSON object
        body = body.removeprefix(_FENCE).removeprefix("json").removesuffix(_FENCE).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "not valid JSON"
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        return 'no "topics" list'
    if not isinstance(data.get("merges", []), list):
        return '"merges" is not a list'
    return data


def _words(raw: Mapping[str, Any], key: str, limit: int, what: str) -> str:
    """``raw[key]`` collapsed to one line of 1 to ``limit`` characters; ``ValueError`` if not."""
    value = raw.get(key)
    flat = " ".join(value.split()) if isinstance(value, str) else ""
    if not flat or len(flat) > limit:
        msg = f"{what}'s {key} is not 1 to {limit} characters"
        raise ValueError(msg)
    return flat


def _recap(raw: Mapping[str, Any], what: str) -> str:
    """The recap, its line breaks kept: the phone shows it as written."""
    value = raw.get("recap")
    text = "\n".join(" ".join(line.split()) for line in value.splitlines()).strip() if isinstance(
        value, str,
    ) else ""
    if not text or len(text) > RECAP_MAX:
        msg = f"{what}'s recap is not 1 to {RECAP_MAX} characters"
        raise ValueError(msg)
    return text


def _merges(raw: Sequence[Any], listed: Mapping[str, Topic]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for one in raw:
        if not isinstance(one, dict):
            msg = "a merge is not an object"
            raise ValueError(msg)  # noqa: TRY004 - one exit for every malformed answer
        source, into = one.get("from"), one.get("into")
        if source not in listed or into not in listed or source == into:
            msg = f"merge {source!r} into {into!r} does not name two listed topics"
            raise ValueError(msg)
        pairs.append((str(source), str(into)))
    sources = [source for source, _ in pairs]
    if len(sources) != len(set(sources)):
        msg = "a topic is merged twice"
        raise ValueError(msg)
    if {into for _, into in pairs} & set(sources):
        msg = "a topic is merged into one that is merged away"
        raise ValueError(msg)
    return pairs


def _entry(
    raw: object, listed: Mapping[str, Topic], known: Mapping[str, tuple[str, str]],
) -> tuple[str, NightTopic, set[str], int]:
    """One entry checked: its label, the topic it leaves, its lines' days, its lines of the day."""
    if not isinstance(raw, dict):
        msg = "a topic entry is not an object"
        raise ValueError(msg)  # noqa: TRY004 - one exit for every malformed answer
    label = raw.get("topic")
    if label != "new" and label not in listed:
        msg = f"topic {label!r} is not a listed topic or new"
        raise ValueError(msg)
    what = f"topic {label}"
    cited = raw.get("lines", [])
    if not (isinstance(cited, list) and all(isinstance(one, str) for one in cited)):
        msg = f"{what}'s lines are not a list of labels"
        raise ValueError(msg)
    if foreign := sorted(set(cited) - set(known)):
        msg = f"{what} cites labels that are not given: {', '.join(foreign[:5])}"
        raise ValueError(msg)
    state = raw.get("state", "open")
    if state not in _STATES:
        msg = f"{what}'s state is not open or done"
        raise ValueError(msg)
    # The night never renames a topic: its name is his to change.
    name = _words(raw, "name", NAME_MAX, what) if label == "new" else listed[str(label)].name
    unique = list(dict.fromkeys(cited))
    topic = NightTopic(
        None if label == "new" else str(label), name, state,
        _words(raw, "line", LINE_MAX, what), _recap(raw, what),
        tuple(known[one][0] for one in unique),
    )
    days = {known[one][1] for one in unique}
    return str(label), topic, days, sum(one.startswith("r") for one in unique)


def _sorted_entries(
    entries: Sequence[tuple[str, NightTopic, set[str], int]],
    merges: Sequence[tuple[str, str]],
    listed: Mapping[str, Topic],
) -> tuple[list[NightTopic], list[str]]:
    """The topics the entries leave, and the new ones dropped for being below the bar.

    Raises:
        ValueError: the entries do not fit together or with the merges.
    """
    existing = [label for label, *_ in entries if label != "new"]
    targets = {into for _, into in merges}
    if len(existing) != len(set(existing)):
        msg = "a topic has two entries"
        raise ValueError(msg)
    if gone := sorted({source for source, _ in merges} & set(existing)):
        msg = f"topic {gone[0]} is merged away and also listed"
        raise ValueError(msg)
    if missing := sorted(targets - set(existing)):
        msg = f"topic {missing[0]} takes a merge but has no entry with its new recap"
        raise ValueError(msg)
    names = {topic.name.casefold() for topic in listed.values()}
    kept: list[NightTopic] = []
    dropped: list[str] = []
    for label, topic, days, day_lines in entries:
        if label != "new":
            if not topic.record_ids and label not in targets:
                msg = f"topic {label} has an entry but cites no line"
                raise ValueError(msg)
            kept.append(topic)
        elif topic.name.casefold() in names:
            msg = f"new topic {topic.name!r} has the name of a topic already kept"
            raise ValueError(msg)
        else:
            names.add(topic.name.casefold())
            if len(days) < NEW_TOPIC_DAYS and day_lines < NEW_TOPIC_LINES:
                dropped.append(topic.name)
            else:
                kept.append(topic)
    return kept, dropped


def check_night(
    text: str | None, finish_reason: str | None, *, day: str, kept: Sequence[KeptTopic],
    known: Mapping[str, tuple[str, str]],
) -> NightAnswer | str:
    """The plan the answer makes, or why it must not be stored.

    Mechanical, and the whole answer is refused on any failure but one: a new topic below the
    bar (two days, or six lines of the day) is left out and named in ``dropped``, so one topic
    opened too early does not cost the day's filing.
    """
    if finish_reason in ("length", "max_tokens"):
        return "cut off by the output limit"
    data = _parse(text or "")
    if isinstance(data, str):
        return data
    listed = {one.topic.id: one.topic for one in kept}
    try:
        merges = _merges(data.get("merges", []), listed)
        entries = [_entry(raw, listed, known) for raw in data["topics"]]
        topics, dropped = _sorted_entries(entries, merges, listed)
    except ValueError as exc:
        return str(exc)
    return NightAnswer(NightPlan(day, tuple(topics), tuple(merges)), tuple(dropped))


__all__ = [
    "EARLIER_DAYS",
    "NEW_TOPIC_DAYS",
    "NEW_TOPIC_LINES",
    "DayLine",
    "KeptTopic",
    "Mention",
    "NightAnswer",
    "build_night_messages",
    "check_night",
    "labels",
    "summary_mentions",
]
