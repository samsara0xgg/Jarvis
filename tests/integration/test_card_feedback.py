"""ADR 0160 — the 合适吗 feedback of every proactive card that is not job mail.

A real ``CardFeedback`` behind the real route: each card kind the client raises stores one snapshot
when it is shown, a reaction writes one ``notice_feedback`` row with the level shown, the rule's id
and the pack as stored, and the ``attention_log`` row carries the same reaction.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.decision import attention
from jarvis.decision.attention import ContextPack, replay, rule_judge_v1
from jarvis.runtime.card_feedback import KINDS, CardFeedback
from jarvis.runtime.inherent_loop import _card_act
from jarvis.shared import lang
from jarvis.state import job_ledger
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# One card of each kind as the client reports it: title and counts, never what an agent wrote.
CARDS: dict[str, dict[str, Any]] = {
    "pop": {
        "level": "card_sound",
        "facts": {"count": 2, "states": ["done", "err"], "title": "api"},
    },
    "wait": {"level": "card_sound", "facts": {"title": "Fix login", "agent": "claude"}},
    "req": {"level": "card", "facts": {"title": "Fix login", "agent": "codex", "tool": "Bash"}},
    "digest": {"level": "card", "facts": {"count": 3, "needs": 1, "errors": 0, "done": 2}},
    "night": {"level": "card", "facts": {"phase": "glance", "busy": 1, "sessions": 2}},
    "morning": {"level": "card", "facts": {"reason": "returned", "sessions": 2}},
}


@pytest.fixture(autouse=True)
def _zh() -> Iterator[None]:
    before = lang.language()
    lang.set_language("zh")
    yield
    lang.set_language(before)


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "memory.db"
        self.quiet = "off"
        self.cards = CardFeedback(self.db, lambda: self.quiet)
        self.client = TestClient(
            create_app(
                InherentDeps(
                    submit_callable=lambda _text: None,
                    broadcaster=InherentBroadcaster(),
                    card_act=functools.partial(_card_act, self.cards),
                )
            )
        )

    def post(self, card_id: str, **body: Any) -> int:  # noqa: ANN401 - the route's JSON
        return self.client.post(f"/inherent/cards/{card_id}", json=body).status_code

    def shown(self, kind: str) -> int:
        card = CARDS[kind]
        return self.post(
            f"{kind}-1", action="seen", kind=kind, level=card["level"], facts=card["facts"]
        )

    def sql(self, query: str, *args: object) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                return conn.execute(query, args).fetchall()
        finally:
            conn.close()


def test_the_kinds_the_route_takes_are_the_ones_the_client_reports() -> None:
    """Every kind of the inventory is accepted, and the test below covers each."""
    assert set(CARDS) == KINDS


@pytest.mark.parametrize("kind", sorted(CARDS))
def test_a_shown_card_is_stored_once_and_its_reaction_is_one_row(tmp_path: Path, kind: str) -> None:
    """Shown: one snapshot with the pack; answered: one feedback row with level, judge, context."""
    h = _Harness(tmp_path)
    h.quiet = "quiet"
    assert h.shown(kind) == 200
    assert h.shown(kind) == 200  # a card shown twice (a reminder) is still one snapshot
    (row,) = job_ledger.list_attention(h.db, f"card:{kind}")
    assert (row["event_id"], row["level"], row["judge_id"], row["judge_version"]) == (
        f"{kind}-1",
        CARDS[kind]["level"],
        "card_rule",
        "1",
    )
    pack = ContextPack.from_json(row["pack_json"])
    assert pack.source == f"card:{kind}"
    assert {k: pack.facts[k] for k in CARDS[kind]["facts"]} == CARDS[kind]["facts"]
    # The daemon's own clock and quiet level are in the situation, whatever the client said.
    assert pack.situation["quiet"] == "quiet"
    assert {"hour", "weekday"} <= pack.situation.keys()
    assert row["delivery"].keys() == {"shown"}
    assert h.sql("SELECT count(*) FROM notice_feedback") == [(0,)]

    level = lang.JOB_LEVEL_NAMES[4]
    assert h.post(f"{kind}-1", action="feedback", reaction=f"level:{level}") == 200
    ((notice_id, got_kind, shown, reaction, judge, context),) = h.sql(
        "SELECT notice_id, kind, level_shown, reaction, judge, context_json FROM notice_feedback"
    )
    assert (notice_id, got_kind, shown, reaction, judge) == (
        f"{kind}-1",
        kind,
        CARDS[kind]["level"],
        f"level:{level}",
        "card_rule/1",
    )
    assert json.loads(context) == json.loads(row["pack_json"])
    (logged,) = job_ledger.list_attention(h.db, f"card:{kind}")
    assert [f["reaction"] for f in logged["feedback"]] == [f"level:{level}"]

    # Right, acted and dismissed are reactions too; dismissed by action or by reaction.
    assert h.post(f"{kind}-1", action="feedback", reaction="right") == 200
    assert h.post(f"{kind}-1", action="feedback", reaction="acted") == 200
    assert h.post(f"{kind}-1", action="dismissed") == 200
    assert [r for (r,) in h.sql("SELECT reaction FROM notice_feedback ORDER BY id")][1:] == [
        "right",
        "acted",
        "dismissed",
    ]


def test_every_level_a_judge_can_say_is_a_level_a_card_is_rated_at(tmp_path: Path) -> None:
    """ADR 0187 made glow a level job mail delivers: a card shown at it is stored like any."""
    assert set(attention.LEVELS) <= set(attention.CARD_LEVELS)
    h = _Harness(tmp_path)
    for level in attention.LEVELS:
        assert h.post(f"lvl-{level}", action="seen", kind="pop", level=level, facts={}) == 200
    stored = {row["level"] for row in job_ledger.list_attention(h.db, "card:pop")}
    assert stored == set(attention.LEVELS)


def test_unknown_cards_are_404_and_bad_posts_are_400(tmp_path: Path) -> None:
    """Feedback for a card never shown is 404; a made-up reaction, kind, level or body is 400."""
    h = _Harness(tmp_path)
    assert h.post("never-shown", action="feedback", reaction="right") == 404
    assert h.post("never-shown", action="dismissed") == 404
    assert h.shown("pop") == 200
    # "ignored" is the daemon's word, not one a client may send.
    for reaction in ("loved-it", "level:nonsense", "ignored", None):
        assert h.post("pop-1", action="feedback", reaction=reaction) == 400
    # The mail path's own notice ids are not cards.
    assert h.post("digest-a-b", action="feedback", reaction="right") == 404
    seen = {"action": "seen", "facts": {}}
    assert h.post("x", **seen, kind="job_mail", level="card") == 400
    assert h.post("x", **seen, kind="pop", level="loud") == 400
    assert h.post("x", **seen, kind="pop") == 400
    assert h.post("x", action="nonsense") == 422
    assert h.sql("SELECT count(*) FROM notice_feedback") == [(0,)]


def test_a_card_carries_typed_values_and_never_a_body(tmp_path: Path) -> None:
    """Nested values, long text and big packs are refused before anything is stored."""
    h = _Harness(tmp_path)
    base: dict[str, Any] = {"action": "seen", "kind": "wait", "level": "card"}
    assert h.post("a", **base, facts={"title": "x" * 201}) == 400
    assert h.post("a", **base, facts={"nested": {"line": "hello"}}) == 400
    assert h.post("a", **base, facts={"k" * 41: 1}) == 400
    assert h.post("a", **base, facts={"titles": [f"title {i} " * 20 for i in range(10)]}) == 400
    assert h.post("a", **base, facts={"title": "ok"}, situation={"in_claude": {"a": 1}}) == 400
    assert job_ledger.list_attention(h.db) == []
    assert h.post("a", **base, facts={"title": "ok"}, situation={"in_claude": True}) == 200
    (row,) = job_ledger.list_attention(h.db)
    assert ContextPack.from_json(row["pack_json"]).situation["in_claude"] is True


def test_a_card_nobody_answers_ends_ignored_at_the_next_card(tmp_path: Path) -> None:
    """Shown 31 minutes ago with no reaction: ignored; one with a reaction is left alone."""
    h = _Harness(tmp_path)
    later = datetime.now(UTC) + timedelta(minutes=31)
    assert h.shown("pop") == 200
    assert h.shown("wait") == 200
    assert h.post("wait-1", action="feedback", reaction="right") == 200
    h.cards.now = lambda: later
    assert h.post("req-1", action="seen", kind="req", level="card", facts={}) == 200
    assert h.sql("SELECT notice_id, level_shown, reaction FROM notice_feedback ORDER BY id") == [
        ("wait-1", "card_sound", "right"),
        ("pop-1", "card_sound", "ignored"),
    ]
    assert h.post("pop-1", action="seen", kind="pop", level="card_sound", facts={}) == 200
    assert h.sql("SELECT count(*) FROM notice_feedback WHERE reaction = 'ignored'") == [(1,)]


def test_a_replay_reads_a_card_pack_like_any_other(tmp_path: Path) -> None:
    """The card packs go through the same replay seam; the mail rule table has no rule for them."""
    h = _Harness(tmp_path)
    assert h.shown("req") == 200
    rows = job_ledger.list_attention(h.db, "card:req")
    (judgement,) = replay(rule_judge_v1, rows)
    assert judgement.level == "ledger"
    assert "no rule for kind" in judgement.reason


def test_without_a_memory_db_the_route_does_not_exist() -> None:
    """The daemon wires it only with memory.db; a bare server answers 404."""
    client = TestClient(
        create_app(
            InherentDeps(submit_callable=lambda _text: None, broadcaster=InherentBroadcaster())
        )
    )
    assert client.post("/inherent/cards/x", json={"action": "seen"}).status_code == 404
