"""The topics she keeps (ADR 0390): ``GET /inherent/topics`` and the page around one message.

The chat page's topic list reads ``topics/list.json``; tapping a place opens the conversation at
its first line with ``GET /inherent/conversation?around=<record_id>`` (``topics/around.json``),
where a row he said inside a topic carries ``topic``. The world is the store's own writes: the
pottery class (opened by a night, talked in on its third day, with a topic merged into it) and
the move (done). A row's steps, places and reactions come from the event log as on every page
of the conversation and are not shown here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.runtime.daemon import submissions
from jarvis.state import topics
from jarvis.state.memory_db import conversation_around
from tests.support.contracts import WORLD_NOW_MS, Contracts, brain_at
from tests.support.timeline import peers_with
from tests.support.topics import ZONE, pottery_world

if TYPE_CHECKING:
    from pathlib import Path


def _peers(tmp_path: Path) -> Any:  # noqa: ANN401 - the app's four peers
    db = tmp_path / "memory.db"
    pottery_world(db)

    def around(record_id: str, limit: int) -> dict[str, Any] | None:
        found = conversation_around(db, record_id, limit=limit)
        if found is None:
            return None
        rows, older, newer = found
        topics.tag_rows(db, rows)
        return {"since": "", "rows": rows, "has_more": older, "has_newer": newer}

    keys = tmp_path / "keys"
    keys.mkdir()
    return peers_with(
        keys,
        conversation_read=lambda *_args: {"since": "", "rows": [], "has_more": False},
        conversation_around=around,
        topics_read=lambda: topics.listed(db, ZONE),
        topic_resolve=lambda tid: submissions.resolve_topic(db, tid),
    )


def test_the_topic_list_the_chat_page_draws(tmp_path: Path, contracts: Contracts) -> None:
    """Open first, newest talk first; one position per day with the lines to light up."""
    peers = _peers(tmp_path)
    with brain_at():
        listed = peers.paired.get("/inherent/topics")
    assert listed.status_code == 200
    contracts.output(
        "topics/list", example=listed.json(), exact=("state", "by"), now_ms=WORLD_NOW_MS,
        route="GET /inherent/topics",
        description=(
            "The topics she keeps over the one conversation (ADR 0390), open ones first, each "
            "side newest talk first. `state` is open or done; `line` is one sentence on where it "
            "stands and `recap` what she wrote of it from his words (rewritten each night; a "
            "turn sent from the topic starts from it). `count` is how many days it was talked "
            "about and `last_ms` when last. `positions` are those days, oldest first: the first "
            "line of the day (`record_id`, `seq`, `ts_ms`; open the chat there with GET "
            "/inherent/conversation?around=<record_id>), `by` him when he said any of it inside "
            "the topic and night when only the nightly pass filed it, and `lines` the record ids "
            "of that day's lines under the topic, oldest first. Send `topic` with POST "
            "/inherent/submit or a say to talk inside one; a merged topic's id still works."
        ),
    )


def test_the_conversation_opened_at_a_topics_place(tmp_path: Path, contracts: Contracts) -> None:
    """The message in the middle of the page, both directions flagged, his topic rows tagged."""
    peers = _peers(tmp_path)
    with brain_at():
        page = peers.paired.get("/inherent/conversation", params={"around": "c1", "limit": 4})
    assert page.status_code == 200
    assert [row["id"] for row in page.json()["rows"]] == ["b3", "c1", "c2", "c3"]
    contracts.output(
        "topics/around", example=page.json(), now_ms=WORLD_NOW_MS,
        route="GET /inherent/conversation?around=",
        description=(
            "A page of the conversation with one message in its middle (ADR 0390): `around` is "
            "its record id, half of `limit` rows end at it and the rest follow, oldest first, "
            "past the history floor. `has_more` says older rows exist (page back with `before`) "
            "and `has_newer` newer ones (page on with `after`). A row he said inside a topic "
            "carries `topic` {id, name}. An id that names no message is a 404; `around` with "
            "`after` or `before` is a 400."
        ),
    )
