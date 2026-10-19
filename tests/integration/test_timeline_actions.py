"""ADR 0296 — the day timeline's three buttons are routes a paired device can press.

Through the real FastAPI app, guarded as the daemon guards it (local key on loopback, a paired
device's token from any other peer), over a real event log and memory.db in ``tmp_path``:

- ``POST /inherent/day/highlight``: his mark, written as the ``mark_highlight`` tool writes it; his
  marks win over hers; the cap of five is a 409 that drops nothing; idempotent;
- ``POST /inherent/commitments/{hex}``: ``keep(confirm=)`` and ``keep(cancel=)``, same events;
- ``POST /inherent/memory/places``: ``places.save``, as ``save_place`` writes it, origin ``page``.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.inherent_loop import _act_commitment, _set_highlight
from jarvis.runtime.memory_page import MemoryPage
from jarvis.shared import lang
from jarvis.state import commitments, places
from jarvis.state.device_tokens import device_token_matches, pair_device
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.memory_db import append_day_prose, open_memory_db
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from tests.integration.test_commitments import NOW as COMMITMENT_NOW
from tests.integration.test_commitments import _at as commitment_at
from tests.integration.test_commitments import _Keep, _Log, _record
from tests.integration.test_day_highlights import (
    DAY_KEY,
    Dispatcher,
    _ids,
    _marks,
    _ms,
    _picture,
    _served,
    _visit,
    _work,
)
from tests.integration.test_ledger import _at, _world
from tests.integration.test_terminal_voice import REMOTE

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.state.ledger import LedgerSources

HIGHLIGHT = "/inherent/day/highlight"
PLACES = "/inherent/memory/places"


class Peers(NamedTuple):
    """One app, reached the three ways the guard tells apart."""

    local: TestClient  # loopback with the local key
    paired: TestClient  # another peer with a paired device's token
    bare_local: TestClient  # loopback, no key
    bare_remote: TestClient  # another peer, no token


def _peers(tmp_path: Path, **deps: Any) -> Peers:  # noqa: ANN401 — the deps under test
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1", broadcaster=InherentBroadcaster(), **deps,
    ))
    key = local_key(tmp_path)
    require_local_key(
        app, functools.partial(local_key_matches, key),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    token = pair_device(tmp_path, "phone")
    host = "http://127.0.0.1:8006"
    loopback = ("127.0.0.1", 50000)
    return Peers(
        TestClient(app, base_url=host, client=loopback, headers={"Authorization": f"Bearer {key}"}),
        TestClient(
            app, base_url=host, client=(REMOTE, 50000),
            headers={"Authorization": f"Bearer {token}"},
        ),
        TestClient(app, base_url=host, client=loopback),
        TestClient(app, base_url=host, client=(REMOTE, 50000)),
    )


def _refused_without_credentials(peers: Peers, path: str, body: dict[str, Any]) -> None:
    """No key on loopback, no token elsewhere, and a wrong token: 401, and nothing is written."""
    assert peers.bare_local.post(path, json=body).status_code == 401
    assert peers.bare_remote.post(path, json=body).status_code == 401
    wrong = {"Authorization": "Bearer not-a-token"}
    assert peers.bare_remote.post(path, json=body, headers=wrong).status_code == 401
    assert peers.bare_local.post(path, json=body, headers=wrong).status_code == 401


# --- highlight -------------------------------------------------------------------------------


class Day(NamedTuple):
    """10-07 of the ledger world with seven stays 07:00-13:30 and its work block at 13:00."""

    src: LedgerSources
    peers: Peers
    tmp_path: Path


def _stays(src: LedgerSources) -> None:
    for n in range(1, 8):
        _visit(src, f"v{n}", _at(7, 6 + n), _at(7, 6 + n, 30), f"Place {n}")


@pytest.fixture
def day(tmp_path: Path) -> Day:
    """The stays, and the route wired as the runtime wires it, to the world's stores."""
    src = _world(tmp_path)
    _stays(src)
    peers = _peers(tmp_path, highlight_set=functools.partial(_set_highlight, _marks(src)))
    return Day(src, peers, tmp_path)


def _rows(src: LedgerSources) -> list[tuple[Any, ...]]:
    """Every mark ever written, as the columns the day line reads."""
    with closing(open_memory_db(src.memory_db)) as conn:
        return conn.execute(
            'SELECT day, item_id, "on", label, kind, start_ms FROM day_highlight_marks '
            "ORDER BY rowid",
        ).fetchall()


def _mark(
    peers: Peers, item_id: str, *, on: bool = True, label: str | None = None, on_day: str = DAY_KEY,
) -> Any:  # noqa: ANN401 — a response
    body: dict[str, Any] = {"day": on_day, "id": item_id, "on": on}
    if label is not None:
        body["label"] = label
    return peers.local.post(HIGHLIGHT, json=body)


def test_his_mark_is_the_row_the_tool_writes_and_the_day_serves_it(
    day: Day, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The same mark by the tool in another world leaves the same row; the answer is the day's."""
    reply = _mark(day.peers, "v3", label="Long lunch")
    assert reply.status_code == 200
    assert reply.json() == {
        "ok": True, "highlights": [{"id": "v3", "label": "Long lunch", "by": "him"}],
    }
    assert reply.json()["highlights"] == _served(day.src)["highlights"]

    other = _world(tmp_path_factory.mktemp("tool"))
    _stays(other)
    Dispatcher(tmp_path_factory.mktemp("tool-log"), other)(
        {"day": DAY_KEY, "time": "09:10", "label": "Long lunch"},
    )
    assert _rows(day.src) == _rows(other)
    assert _rows(day.src)[0][1:] == ("v3", 1, "Long lunch", "stay", _ms(_at(7, 9)))


def test_marking_is_idempotent_and_a_mark_without_a_label_keeps_the_label(day: Day) -> None:
    """The second press changes nothing and writes nothing; the label stands until a new one."""
    first = _mark(day.peers, "v3", label="Long lunch").json()
    assert _mark(day.peers, "v3", label="Long lunch").json() == first
    assert _mark(day.peers, "v3").json() == first
    assert len(_rows(day.src)) == 1
    renamed = _mark(day.peers, "v3", label="  Brunch  ").json()
    assert renamed["highlights"] == [{"id": "v3", "label": "Brunch", "by": "him"}]
    assert len(_rows(day.src)) == 2


def test_unmarking_takes_off_only_that_mark_and_unmarking_what_is_not_marked_writes_nothing(
    day: Day,
) -> None:
    """His other marks stay; an item that was never highlighted leaves no row."""
    _mark(day.peers, "v1")
    _mark(day.peers, "v2")
    reply = _mark(day.peers, "v1", on=False)
    assert reply.status_code == 200
    assert _ids(reply.json()) == ["v2"]
    rows = len(_rows(day.src))
    assert _ids(_mark(day.peers, "v1", on=False).json()) == ["v2"]
    assert _ids(_mark(day.peers, "v5", on=False).json()) == ["v2"]
    assert len(_rows(day.src)) == rows
    assert _ids(_mark(day.peers, "v1").json()) == ["v1", "v2"]  # and back on


def test_his_mark_wins_over_hers_and_unmarking_her_pick_hides_it(day: Day) -> None:
    """Five of hers, one of his: his shows, the last of hers gives way; hers can be hidden."""
    append_day_prose(
        day.src.memory_db, day=DAY_KEY, text="Night prose.", model="m", input_chars=1,
        output_chars=1, line="Night line.", highlights=[
            {"id": f"v{n}", "label": f"Hers {n}", "start_ms": _ms(_at(7, 6 + n))}
            for n in range(1, 6)
        ],
    )
    assert _ids(_served(day.src)) == ["v1", "v2", "v3", "v4", "v5"]
    reply = _mark(day.peers, "v7", label="Mine")
    assert [(h["id"], h["by"]) for h in reply.json()["highlights"]] == [
        ("v1", "her"), ("v2", "her"), ("v3", "her"), ("v4", "her"), ("v7", "him"),
    ]
    hidden = _mark(day.peers, "v2", on=False)
    assert [(h["id"], h["by"]) for h in hidden.json()["highlights"]] == [
        ("v1", "her"), ("v3", "her"), ("v4", "her"), ("v5", "her"), ("v7", "him"),
    ]
    assert hidden.json()["highlights"] == _served(day.src)["highlights"]


def test_a_sixth_mark_of_his_is_a_409_that_drops_nothing_and_a_standing_one_still_answers(
    day: Day,
) -> None:
    """At five of his marks another is refused; unmarking one makes room."""
    for n in range(1, 6):
        assert _mark(day.peers, f"v{n}").status_code == 200
    before = _rows(day.src)
    refused = _mark(day.peers, "v6")
    assert refused.status_code == 409
    assert "unmark one first" in refused.json()["detail"]
    assert _rows(day.src) == before
    assert _ids(_served(day.src)) == ["v1", "v2", "v3", "v4", "v5"]
    assert _mark(day.peers, "v5", label="Again").status_code == 200  # already his: relabelled
    assert _mark(day.peers, "v1", on=False).status_code == 200
    assert _ids(_mark(day.peers, "v6").json()) == ["v2", "v3", "v4", "v5", "v6"]


def test_an_item_that_is_not_on_that_day_is_a_404_and_a_label_is_not_an_item(day: Day) -> None:
    """No such id, the right id on the wrong day, and a day with nothing: nothing is written."""
    assert _mark(day.peers, "v-nope").status_code == 404
    assert _mark(day.peers, "v3", on_day="2026-10-08").status_code == 404
    assert _mark(day.peers, _work(7, 13), on_day="2026-10-06").status_code == 404
    assert _mark(day.peers, _work(7, 13)).status_code == 200  # the work block is an item too
    assert len(_rows(day.src)) == 1


def test_a_picture_inside_a_stay_can_be_marked_by_its_own_id(day: Day) -> None:
    """The ids the day line shows are the ones the route takes."""
    _picture(day.src, "a" * 32, _at(7, 9, 10), "A bowl of pho.")
    reply = _mark(day.peers, f"picture-{'a' * 32}")
    assert reply.status_code == 200
    assert reply.json()["highlights"][0]["label"] == "picture 09:10"


@pytest.mark.parametrize(
    "body",
    [
        {"id": "v3", "on": True},
        {"day": DAY_KEY, "on": True},
        {"day": DAY_KEY, "id": "v3"},
        {"day": "today", "id": "v3", "on": True},
        {"day": "2026-13-01", "id": "v3", "on": True},
        {"day": "2026-10-7", "id": "v3", "on": True},
        {"day": "9999-12-31", "id": "v3", "on": True},
        {"day": DAY_KEY, "id": "", "on": True},
        {"day": DAY_KEY, "id": "v 3", "on": True},
        {"day": DAY_KEY, "id": "v3\n", "on": True},
        {"day": DAY_KEY + "\n", "id": "v3", "on": True},
        {"day": DAY_KEY, "id": "v" * 257, "on": True},
        {"day": DAY_KEY, "id": 3, "on": True},
        {"day": DAY_KEY, "id": "v3", "on": "yes"},
        {"day": DAY_KEY, "id": "v3", "on": 1},
        {"day": DAY_KEY, "id": "v3", "on": True, "label": "l" * 25},
        {"day": DAY_KEY, "id": "v3", "on": True, "label": 7},
    ],
)
def test_a_body_of_the_wrong_shape_is_a_422_and_writes_nothing(
    day: Day, body: dict[str, Any],
) -> None:
    """Day, id, on and label are checked before anything is read or written."""
    assert day.peers.local.post(HIGHLIGHT, json=body).status_code == 422
    assert _rows(day.src) == []


def test_a_blank_label_is_no_label(day: Day) -> None:
    """Whitespace only is the same as leaving it out: the label is hers, else kind and time."""
    reply = _mark(day.peers, "v3", label="   ")
    assert reply.json()["highlights"] == [{"id": "v3", "label": "stay 09:00", "by": "him"}]


def test_the_highlight_route_is_for_the_local_key_and_paired_devices_alone(day: Day) -> None:
    """Loopback with the key, and a paired device's token from elsewhere; nobody else."""
    body = {"day": DAY_KEY, "id": "v3", "on": True}
    _refused_without_credentials(day.peers, HIGHLIGHT, body)
    assert _rows(day.src) == []
    assert day.peers.paired.post(HIGHLIGHT, json=body).status_code == 200
    assert day.peers.local.post(HIGHLIGHT, json={**body, "id": "v4"}).status_code == 200
    assert _ids(_served(day.src)) == ["v3", "v4"]


def test_a_brain_without_a_memory_store_has_no_highlight_route(tmp_path: Path) -> None:
    """Not wired: the route is not there, as with every optional route."""
    peers = _peers(tmp_path)
    body = {"day": DAY_KEY, "id": "v3", "on": True}
    assert peers.local.post(HIGHLIGHT, json=body).status_code == 404


# --- commitments -----------------------------------------------------------------------------


class Appointments(NamedTuple):
    """A runtime root with a mail-made appointment and the route wired to its log."""

    log: _Log
    peers: Peers
    hex: str


@pytest.fixture
def appointments(tmp_path: Path) -> Iterator[Appointments]:
    """One unconfirmed appointment from a mail, in a real event log."""
    before = lang.language()
    lang.set_language("en")
    log = _Log(tmp_path)
    one = _record(commitment_at(10, 20, 19), commitment_id="commitment-" + "ab" * 16)
    commitments.note(log.conn, one)
    peers = _peers(tmp_path, commitment_act=functools.partial(_act_commitment, log.paths.event_log))
    yield Appointments(log, peers, "ab" * 16)
    log.conn.close()
    lang.set_language(before)


def _events(log: _Log) -> list[tuple[str, dict[str, Any], str]]:
    """Every commitment event as (type, payload, actor)."""
    with closing(sqlite3.connect(log.paths.event_log)) as conn:
        found = conn.execute(
            "SELECT type, payload_json, actor FROM events WHERE type LIKE 'commitment.%' "
            "ORDER BY id",
        ).fetchall()
    return [(t, json.loads(p), a) for t, p, a in found]


def test_confirm_writes_the_noted_the_keep_tool_writes_and_a_second_press_is_a_409(
    appointments: Appointments, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Same ``commitment.noted`` payload as ``keep(confirm=)``; the actor is the one who pressed."""
    log, peers, hex_id = appointments
    reply = peers.local.post(f"/inherent/commitments/{hex_id}", json={"action": "confirm"})
    assert (reply.status_code, reply.json()) == (200, {"ok": True})
    assert commitments.fold(log.conn)[f"commitment-{hex_id}"].confirmed is True
    events = _events(log)
    assert [e[0] for e in events] == ["commitment.noted", "commitment.noted"]
    assert events[-1][2] == "user"

    again = peers.local.post(f"/inherent/commitments/{hex_id}", json={"action": "confirm"})
    assert again.status_code == 409
    assert len(_events(log)) == 2

    other = _Log(tmp_path_factory.mktemp("tool"))
    commitments.note(other.conn, _record(
        commitment_at(10, 20, 19), commitment_id="commitment-" + "ab" * 16,
    ))
    _Keep(other).call("keep", confirm="commitment-" + "ab" * 16)
    assert [e[:2] for e in _events(other)] == [e[:2] for e in events]
    other.conn.close()


def test_cancel_writes_the_drop_the_keep_tool_writes_and_it_leaves_the_day(
    appointments: Appointments, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``commitment.dropped`` with ``he_dropped``; a cancelled one is not found again."""
    log, peers, hex_id = appointments
    reply = peers.paired.post(f"/inherent/commitments/{hex_id}", json={"action": "cancel"})
    assert (reply.status_code, reply.json()) == (200, {"ok": True})
    assert commitments.live(log.conn) == []
    dropped = {"commitment_id": f"commitment-{hex_id}", "why": "he_dropped"}
    assert _events(log)[-1] == ("commitment.dropped", dropped, "user")
    for action in ("cancel", "confirm"):
        gone = peers.local.post(f"/inherent/commitments/{hex_id}", json={"action": action})
        assert gone.status_code == 404
    assert len(_events(log)) == 2

    other = _Log(tmp_path_factory.mktemp("tool"))
    commitments.note(other.conn, _record(
        commitment_at(10, 20, 19), commitment_id="commitment-" + "ab" * 16,
    ))
    _Keep(other).call("keep", cancel="commitment-" + "ab" * 16)
    assert _events(other)[-1][:2] == _events(log)[-1][:2]
    other.conn.close()


def test_a_cancel_of_a_confirmed_appointment_and_a_confirm_of_a_confirmed_one(
    appointments: Appointments,
) -> None:
    """Cancel works whatever its state; confirm of one he made himself is a 409."""
    log, peers, _hex = appointments
    said = _record(commitment_at(10, 21, 10), source="said", confirmed=True)
    commitments.note(log.conn, said)
    hex_said = said.commitment_id.removeprefix("commitment-")
    path = f"/inherent/commitments/{hex_said}"
    assert peers.local.post(path, json={"action": "confirm"}).status_code == 409
    assert peers.local.post(path, json={"action": "cancel"}).status_code == 200


@pytest.mark.parametrize(
    ("hex_id", "body", "status"),
    [
        ("cd" * 16, {"action": "confirm"}, 404),
        ("cd" * 16, {"action": "cancel"}, 404),
        ("not-hex", {"action": "cancel"}, 404),
        ("ab" * 16, {"action": "drop"}, 422),
        ("ab" * 16, {"action": ""}, 422),
        ("ab" * 16, {}, 422),
        ("ab" * 16, {"action": ["confirm"]}, 422),
    ],
)
def test_an_unknown_commitment_is_a_404_and_an_action_that_is_not_one_is_a_422(
    appointments: Appointments, hex_id: str, body: dict[str, Any], status: int,
) -> None:
    """Nothing is written for a refusal."""
    log, peers, _hex = appointments
    assert peers.local.post(f"/inherent/commitments/{hex_id}", json=body).status_code == status
    kept = iter_events_of_types(log.conn, ("commitment.noted", "commitment.dropped"))
    assert len(list(kept)) == 1


def test_the_commitment_route_is_for_the_local_key_and_paired_devices_alone(
    appointments: Appointments,
) -> None:
    """No credentials, no write."""
    log, peers, hex_id = appointments
    _refused_without_credentials(peers, f"/inherent/commitments/{hex_id}", {"action": "cancel"})
    assert len(_events(log)) == 1
    assert (
        peers.paired.post(f"/inherent/commitments/{hex_id}", json={"action": "cancel"})
        .status_code == 200
    )


def test_the_keep_tool_still_says_what_it_did_with_the_shared_confirm(
    appointments: Appointments,
) -> None:
    """``keep(confirm=)`` is the same call: confirmed once, then 'already confirmed'."""
    log, _peers, hex_id = appointments
    keep = _Keep(log)
    assert "Confirmed" in keep.call("keep", confirm=f"commitment-{hex_id}")["summary"]
    assert "Already confirmed" in keep.call("keep", confirm=f"commitment-{hex_id}")["summary"]
    assert COMMITMENT_NOW.year == 2026  # the tool's clock is the test module's


# --- places ----------------------------------------------------------------------------------


class PlacesWorld(NamedTuple):
    """A memory.db with the memory page wired to it, and the app."""

    db: Path
    peers: Peers


@pytest.fixture
def places_world(tmp_path: Path) -> PlacesWorld:
    """The memory page over an empty memory.db."""
    db = tmp_path / "memory.db"
    open_memory_db(db).close()
    page = MemoryPage(db, None, history_since="2026-01-01T00:00:00")
    return PlacesWorld(db, _peers(tmp_path, memory_page=page))


def _changes(db: Path) -> list[tuple[str, str, str]]:
    with closing(open_memory_db(db)) as conn:
        return conn.execute(
            "SELECT origin, op, place_id FROM place_changes ORDER BY rowid",
        ).fetchall()


def test_a_place_is_kept_at_a_point_as_save_place_keeps_it(
    places_world: PlacesWorld, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The place and its change are ``places.save``'s; the memory page lists it afterwards."""
    db, peers = places_world
    reply = peers.local.post(
        PLACES, json={"name": "Gym", "lat": 48.4, "lng": -123.3, "role": "other"},
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["ok"] is True
    assert body["place"]["name"] == "Gym"
    assert (body["place"]["role"], body["place"]["has_centre"]) == ("other", True)
    assert body["place"]["radius_m"] == places.DEFAULT_RADIUS_M
    assert body["change"].startswith("place-change:")

    other = tmp_path_factory.mktemp("tool") / "memory.db"
    open_memory_db(other).close()
    written = places.save(other, name="Gym", lat=48.4, lng=-123.3, role="other", origin="model")
    with closing(open_memory_db(db)) as one, closing(open_memory_db(other)) as two:
        query = "SELECT name, role, lat, lng, radius_m, address FROM places"
        mine, theirs = one.execute(query).fetchall(), two.execute(query).fetchall()
    assert mine == theirs
    assert [c[1:2] for c in _changes(db)] == [("save",)]
    assert _changes(db)[0][0] == "page"
    assert written.change_id is not None

    listed = peers.paired.get(PLACES).json()
    assert [p["name"] for p in listed["places"]] == ["Gym"]
    assert (listed["changes"][0]["op"], listed["changes"][0]["origin"]) == ("save", "page")


def test_keeping_the_same_place_again_changes_nothing(places_world: PlacesWorld) -> None:
    """The same name at the same point: 200 with the place and no new change."""
    db, peers = places_world
    body = {"name": "Gym", "lat": 48.4, "lng": -123.3}
    first = peers.local.post(PLACES, json=body).json()
    again = peers.local.post(PLACES, json=body)
    assert again.status_code == 200
    assert again.json()["place"] == first["place"]
    assert again.json()["change"] is None
    assert len(_changes(db)) == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"lat": 48.4, "lng": -123.3}, 422),
        ({"name": "", "lat": 48.4, "lng": -123.3}, 422),
        ({"name": "Gym", "lng": -123.3}, 422),
        ({"name": "Gym", "lat": "north", "lng": -123.3}, 422),
        ({"name": "Gym", "lat": 48.4, "lng": None}, 422),
        ({"name": "Gym", "lat": 91, "lng": -123.3}, 400),
        ({"name": "Gym", "lat": 48.4, "lng": 181}, 400),
        ({"name": "  ", "lat": 48.4, "lng": -123.3}, 400),
        ({"name": "x" * 41, "lat": 48.4, "lng": -123.3}, 400),
        ({"name": "Gym", "lat": 48.4, "lng": -123.3, "role": "castle"}, 400),
    ],
)
def test_a_refused_place_writes_nothing(
    places_world: PlacesWorld, body: dict[str, Any], status: int,
) -> None:
    """Shape errors are 422; the store's own refusals are the memory page's 400."""
    db, peers = places_world
    assert peers.local.post(PLACES, json=body).status_code == status
    assert _changes(db) == []


def test_a_name_kept_elsewhere_and_a_point_inside_a_kept_circle_are_refused(
    places_world: PlacesWorld,
) -> None:
    """The rules of ``save_place``: no second place for a name, none inside another's circle."""
    db, peers = places_world
    gym = {"name": "Gym", "lat": 48.4, "lng": -123.3}
    assert peers.local.post(PLACES, json=gym).status_code == 200
    far = peers.local.post(PLACES, json={"name": "Gym", "lat": 49.0, "lng": -123.3})
    assert (far.status_code, "already kept somewhere else" in far.json()["detail"]) == (400, True)
    inside = peers.local.post(PLACES, json={"name": "Weights", "lat": 48.4001, "lng": -123.3})
    assert (inside.status_code, "already kept as" in inside.json()["detail"]) == (400, True)
    assert len(_changes(db)) == 1


def test_the_place_route_is_for_the_local_key_and_paired_devices_alone(
    places_world: PlacesWorld,
) -> None:
    """No credentials, no place."""
    db, peers = places_world
    body = {"name": "Gym", "lat": 48.4, "lng": -123.3}
    _refused_without_credentials(peers, PLACES, body)
    assert _changes(db) == []
    assert peers.paired.post(PLACES, json=body).status_code == 200


def test_the_place_a_page_edit_can_still_rename_is_the_one_the_button_made(
    places_world: PlacesWorld,
) -> None:
    """The new route sits beside ``/places/{id}`` and ``/places/undo`` without taking them over."""
    _db, peers = places_world
    made = peers.local.post(PLACES, json={"name": "Gym", "lat": 48.4, "lng": -123.3}).json()
    renamed = peers.local.post(
        f"/inherent/memory/places/{made['place']['id']}", json={"name": "Iron Works"},
    )
    assert renamed.status_code == 200
    undone = peers.local.post(
        "/inherent/memory/places/undo", json={"change_id": renamed.json()["change"]},
    )
    assert undone.status_code == 200
    assert undone.json()["place"]["name"] == "Gym"

