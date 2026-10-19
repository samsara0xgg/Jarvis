"""ADR 0304: his iPhone and iPad app use, from the Mac's Screen Time sync to the day line.

Acceptance checks against the real shipper step, the real intake and the real fold, on synthetic
events (no helper, no network):

- the Mac step pairs per device and ships one entry per closed stretch with a stable key; system
  overlays, stretches under five seconds and Macs are dropped; a stretch still open is not shipped
  and holds the cursor, and a send again adds nothing;
- names come from Apple's table, then the cache, then the (mocked) store, each bundle once, with
  its genre; a store that cannot be asked holds the whole pass;
- the day line joins one device's stretches within five minutes into a ``screen`` block of at
  least five active minutes, keeps two devices apart, tops the apps, and says how far each device
  is synced; ``screen`` is missing only when a shipped store holds no device record.
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.state import imports
from jarvis.state.day_line import DaySources, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.state.shipments import Shipments
from jarvis.surface import mac_ship_devices as devices
from jarvis.surface.mac_screen_time import ScreenTimeUnavailable
from jarvis.surface.mac_ship import ship_config
from jarvis.surface.mac_ship_core import SET_META, Report, State

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
SEC = 1000
MIN = 60 * SEC
PHONE = "AAAAAAAA-1111-2222-3333-444444444444"
PAD = "BBBBBBBB-1111-2222-3333-444444444444"
MAC = "CCCCCCCC-1111-2222-3333-444444444444"
DEVICES = [
    {"id": PHONE, "name": None, "model": "iPhone17,2"},
    {"id": PAD, "name": None, "model": "iPad16,3"},
    {"id": MAC, "name": None, "model": "Mac17,2"},
    {"id": "DDDDDDDD", "name": None},
]
DAY0, _ = day_window(parse_day("2026-10-10"), ZONE)
H = 60 * MIN


def at(minutes: float) -> int:
    """Milliseconds ``minutes`` after local midnight of the test day."""
    return DAY0 + int(minutes * MIN)


def stretch(device: str, bundle: str, start: int, end: int) -> list[dict[str, Any]]:
    """One app in the foreground on one device, from ``start`` to ``end``."""
    return [
        {"device": device, "bundle_id": bundle, "focus": "in", "at_ms": start},
        {"device": device, "bundle_id": bundle, "focus": "out", "at_ms": end},
    ]


class _Intake:
    """The brain's intake, called in place of HTTP; keeps what each send added."""

    def __init__(self, shipments: Shipments) -> None:
        """Empty stores."""
        self.shipments = shipments
        self.added: list[int] = []

    def send_entries(self, source: str, body: bytes) -> None:
        """Keep what the brain would keep."""
        self.added.append(self.shipments.receive_entries(source, body).added)

    def send_file(self, source: str, relpath: str, data: bytes) -> str:  # pragma: no cover
        raise AssertionError((source, relpath, len(data)))


class Mac:
    """One Mac terminal's shipper state, with the helper's answer settable."""

    def __init__(self, tmp_path: Path) -> None:
        """Empty stores."""
        self.directory = tmp_path / "brain" / imports.IMPORTS_DIRNAME
        self.intake = _Intake(Shipments(self.directory, min_free_bytes=0))
        self.state = State(tmp_path / "mac" / "state.db")
        self.names = tmp_path / "mac" / "app-names.json"
        self.events: list[dict[str, Any]] = []
        self.reads: list[int | None] = []
        self.looked_up: list[str] = []
        self.store: dict[str, tuple[str | None, str | None] | None] = {}

    def read(self, since_ms: int | None) -> dict[str, Any]:
        """The helper's answer: the events at or after ``since_ms``."""
        self.reads.append(since_ms)
        kept = [e for e in self.events if since_ms is None or e["at_ms"] >= since_ms]
        return {"devices": DEVICES, "events": kept}

    def lookup(self, bundle: str) -> tuple[str | None, str | None] | None:
        """The store's answer for a bundle, as set in ``store``."""
        self.looked_up.append(bundle)
        return self.store.get(bundle, (None, None))

    def pass_(
        self, now_ms: int, read: Callable[[int | None], dict[str, Any]] | None = None
    ) -> None:
        """One pass of the step at ``now_ms``."""
        devices.ship(
            self.names,
            self.state,
            self.intake,
            Report(),
            now_ms,
            read=read or self.read,
            lookup=self.lookup,
        )

    def rows(self, kind: str) -> list[imports.Entry]:
        """The newest revision of each shipped row of one kind."""
        with closing(imports.open_shipped(self.directory)) as conn:
            return imports.newest(conn, imports.DEVICE_SOURCE, kinds=(kind,), by_prefix=True)


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    """A fresh Mac terminal."""
    return Mac(tmp_path)


def test_pairs_per_device_ships_closed_stretches_and_holds_the_open_one(mac: Mac) -> None:
    """Pairs per device ships closed stretches and holds the open one."""
    mac.store = {"com.ss.iphone.ugc.Aweme": ("抖音", "Entertainment")}
    mac.events = [
        *stretch(PHONE, "com.ss.iphone.ugc.Aweme", at(600), at(610)),
        *stretch(PHONE, "com.apple.control-center", at(610), at(611)),  # an overlay
        *stretch(PHONE, "com.apple.springboard.app-library", at(611), at(612)),
        *stretch(PHONE, "com.apple.camera", at(612), at(612) + 3 * SEC),  # under 5 s
        *stretch(PAD, "com.apple.mobilesafari", at(620), at(625)),
        *stretch(MAC, "com.apple.mobilesafari", at(620), at(625)),  # a Mac: TimeSink's
        {"device": PHONE, "bundle_id": "com.tencent.xin", "focus": "in", "at_ms": at(630)},
    ]
    mac.pass_(at(700))

    apps = mac.rows(imports.DEVICE_APP)
    assert [(r.fields["device"], r.fields["app"], r.fields["category"]) for r in apps] == [
        ("iPhone", "抖音", "Entertainment"),
        ("iPad", "Safari", "Utilities"),
    ]
    first = apps[0]
    assert first.fields == {
        "device_id": PHONE,
        "device": "iPhone",
        "bundle_id": "com.ss.iphone.ugc.Aweme",
        "app": "抖音",
        "category": "Entertainment",
        "start_ms": at(600),
        "end_ms": at(610),
        "unpaired": False,
    }
    assert first.at_ms == at(600)
    assert first.zone == "UTC"
    assert imports.entry_id(imports.DEVICE_SOURCE, "x")  # the same id scheme as the others
    # the open WeChat stretch is not shipped; the iPhone's cursor is its start, the iPad's is
    # just past its newest record, and each device's horizon is its newest record
    assert mac.state.meta_int(f"{devices.CURSOR_PREFIX}{PHONE}") == at(630)
    assert mac.state.meta_int(f"{devices.CURSOR_PREFIX}{PAD}") == at(625) + 1
    assert {
        r.fields["device"]: r.fields["synced_ms"] for r in mac.rows(imports.DEVICE_HORIZON)
    } == {
        "iPhone": at(630),
        "iPad": at(625),
    }

    # the "out" arrives: the next pass, a quarter hour on, ships it from the held cursor
    mac.events.append(
        {"device": PHONE, "bundle_id": "com.tencent.xin", "focus": "out", "at_ms": at(640)}
    )
    mac.pass_(at(700) + 5 * MIN)
    assert len(mac.rows(imports.DEVICE_APP)) == 2  # under 15 minutes since the last: no read
    assert len(mac.reads) == 1
    mac.pass_(at(700) + 16 * MIN)
    assert mac.reads[1] == at(625) + 1 - devices.OVERLAP_MS  # the oldest cursor, less the overlap
    apps = mac.rows(imports.DEVICE_APP)
    assert len(apps) == 3
    assert sorted(r.fields["bundle_id"] for r in apps)[-1] == "com.tencent.xin"
    assert mac.state.meta_int(f"{devices.CURSOR_PREFIX}{PHONE}") == at(640) + 1


def test_a_send_again_adds_nothing_and_a_stretch_never_ships_twice(mac: Mac) -> None:
    """A send again adds nothing and a stretch never ships twice."""
    mac.events = stretch(PHONE, "com.apple.camera", at(600), at(610))
    mac.pass_(at(700))
    mac.state.apply(
        [
            (SET_META, (devices.CURSOR_PREFIX + PHONE, "0")),
            (SET_META, (devices.HORIZON_PREFIX + PHONE, "0")),
        ]
    )  # as if never kept
    mac.pass_(at(700) + 16 * MIN)
    assert mac.intake.added == [2, 0]  # the stretch and the horizon, then the same keys: held


def test_an_open_stretch_older_than_a_day_is_given_up_cut_and_unpaired(mac: Mac) -> None:
    """An open stretch older than a day is given up cut and unpaired."""
    mac.events = [
        {"device": PAD, "bundle_id": "com.apple.mobilesafari", "focus": "in", "at_ms": at(600)}
    ]
    mac.pass_(at(600) + 23 * H)
    assert mac.rows(imports.DEVICE_APP) == []
    mac.pass_(at(600) + 25 * H)
    (row,) = mac.rows(imports.DEVICE_APP)
    assert (row.fields["end_ms"] - row.fields["start_ms"], row.fields["unpaired"]) == (
        30 * MIN,
        True,
    )


def test_names_come_from_the_table_then_the_cache_then_the_store_once_each(mac: Mac) -> None:
    """Names come from the table then the cache then the store once each."""
    mac.store = {"com.new.app": ("New App", "Games"), "com.unlisted.app": (None, None)}
    mac.events = [
        *stretch(PHONE, "com.apple.Preferences", at(600), at(601)),
        *stretch(PHONE, "com.new.app", at(602), at(603)),
        *stretch(PHONE, "com.unlisted.app", at(604), at(605)),
        *stretch(PHONE, "com.new.app", at(606), at(607)),
    ]
    mac.pass_(at(700))
    assert mac.looked_up == ["com.new.app", "com.unlisted.app"]  # the table's Apple app: no lookup
    assert [(r.fields["app"], r.fields["category"]) for r in mac.rows(imports.DEVICE_APP)] == [
        ("设置", "Utilities"),
        ("New App", "Games"),
        (None, None),
        ("New App", "Games"),
    ]
    mac.events += stretch(PHONE, "com.unlisted.app", at(608), at(609))
    mac.pass_(at(700) + 16 * MIN)
    assert mac.looked_up == ["com.new.app", "com.unlisted.app"]  # misses are cached too


def test_a_store_that_cannot_be_asked_holds_the_whole_pass_until_it_can(mac: Mac) -> None:
    """A store that cannot be asked holds the whole pass until it can."""
    mac.store = {"com.new.app": None}  # the store did not answer
    mac.events = stretch(PHONE, "com.new.app", at(600), at(610))
    mac.pass_(at(700))
    assert mac.rows(imports.DEVICE_APP) == []
    assert mac.rows(imports.DEVICE_HORIZON) == []
    assert mac.state.meta(f"{devices.CURSOR_PREFIX}{PHONE}") is None
    mac.store = {"com.new.app": ("New App", None)}
    mac.pass_(at(700) + 16 * MIN)
    assert [r.fields["app"] for r in mac.rows(imports.DEVICE_APP)] == ["New App"]


def test_a_helper_with_no_access_logs_once_and_does_nothing(
    mac: Mac,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A helper with no access logs once and does nothing."""

    def refused(_since: int | None) -> dict[str, Any]:
        msg = "no_access: grant Full Disk Access to the test helper"
        raise ScreenTimeUnavailable(msg)

    devices.WARNED.clear()
    with caplog.at_level(logging.WARNING):
        mac.pass_(at(700), refused)
        mac.pass_(at(700) + 16 * MIN, refused)
    assert [r.message for r in caplog.records if "device apps" in r.message].__len__() == 1
    assert mac.rows(imports.DEVICE_APP) == []


def test_a_device_met_late_is_read_from_the_start(mac: Mac) -> None:
    """A device met late is read from the start."""
    mac.events = stretch(PHONE, "com.apple.camera", at(600), at(610))
    mac.pass_(at(700))
    mac.events += (
        stretch(PAD, "com.apple.camera", at(300), at(310))  # older than the held cursors
        + stretch(PAD, "com.apple.camera", at(720), at(725))
    )
    mac.pass_(at(700) + 16 * MIN)
    assert mac.reads[-1] is None
    assert sorted(r.fields["device"] for r in mac.rows(imports.DEVICE_APP)) == [
        "iPad",
        "iPad",
        "iPhone",
    ]


def test_shipping_is_on_with_mac_ship_and_off_when_asked(tmp_path: Path) -> None:
    """Shipping is on with mac ship and off when asked."""
    on = ship_config({"mac_ship": {"enabled": True}}, tmp_path)
    off = ship_config({"mac_ship": {"enabled": True, "device_apps": {"enabled": False}}}, tmp_path)
    assert on is not None
    assert on.device_names == tmp_path / "mac-ship" / devices.NAMES_FILE
    assert off is not None
    assert off.device_names is None


# --- the day line ----------------------------------------------------------------------------


def _shipped(mac: Mac, events: list[dict[str, Any]], now_ms: int) -> DaySources:
    mac.events = events
    mac.store = {"com.ss.iphone.ugc.Aweme": ("抖音", "Entertainment")}
    mac.pass_(now_ms)
    return DaySources(ZONE, shipped=mac.directory)


def _fold(sources: DaySources, tmp_path: Path, now_ms: int) -> Any:  # noqa: ANN401
    start, end = day_window(parse_day("2026-10-10"), ZONE)
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        return fold_day(conn, start, end, now_ms, sources)


def _screens(line: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    return [i for i in line.items if i["kind"] == "screen"]


def test_the_day_line_joins_stretches_within_five_minutes_into_one_block_per_device(
    mac: Mac,
    tmp_path: Path,
) -> None:
    """One block per device, stretches within five minutes joined, short blocks dropped."""
    events = [
        # the iPad: Douyin 20 min, a 5 min gap (joined), Safari 4 min, then a 6 min gap, then a
        # 2 min stretch (a block of its own, under five minutes: dropped)
        *stretch(PAD, "com.ss.iphone.ugc.Aweme", at(1330), at(1350)),
        *stretch(PAD, "com.apple.mobilesafari", at(1355), at(1359)),
        *stretch(PAD, "com.apple.camera", at(1365), at(1367)),
        # the iPhone at the same time: its own block
        *stretch(PHONE, "com.tencent.xin", at(1332), at(1342)),
        # a 4 minute block alone: dropped
        *stretch(PHONE, "com.apple.camera", at(600), at(604)),
    ]
    now = at(1500)
    line = _fold(_shipped(mac, events, now), tmp_path, now)
    shown = _screens(line)
    assert [(i["device"], i["start_ms"], i["end_ms"], i["active_s"]) for i in shown] == [
        ("iPad", at(1330), at(1359), 24 * 60),
        ("iPhone", at(1332), at(1342), 10 * 60),
    ]
    pad = shown[0]
    assert pad["id"] == f"screen-iPad-{at(1330)}"
    assert (pad["lane"], pad["state"]) == ("you", "done")
    assert pad["apps"] == [
        {
            "name": "抖音",
            "bundle": "com.ss.iphone.ugc.Aweme",
            "minutes": 20,
            "category": "Entertainment",
        },
        {
            "name": "Safari",
            "bundle": "com.apple.mobilesafari",
            "minutes": 4,
            "category": "Utilities",
        },
    ]
    assert "screen" not in line.missing


def test_a_block_names_at_most_five_apps_and_is_still_going_until_it_ends(
    mac: Mac,
    tmp_path: Path,
) -> None:
    """A block names at most five apps and is still going until it ends."""
    bundles = [f"com.app.n{n}" for n in range(7)]
    events = [
        e
        for n, b in enumerate(bundles)
        for e in stretch(PHONE, b, at(600 + 3 * n), at(602.5 + 3 * n))
    ]
    now = at(610)
    (block,) = _screens(_fold(_shipped(mac, events, at(1500)), tmp_path, now))
    assert len(block["apps"]) == 5
    assert block["state"] == "now"  # the block ends at 620, after the clock


def test_each_device_is_known_only_up_to_its_newest_record(mac: Mac, tmp_path: Path) -> None:
    """Each device is known only up to its newest record."""
    events = [
        *stretch(PHONE, "com.apple.camera", at(600), at(610)),
        *stretch(PAD, "com.apple.camera", at(600), at(620)),
    ]
    sources = _shipped(mac, events, at(700))
    # a past day: both are behind midnight's end
    assert _fold(sources, tmp_path, at(1500) + 24 * H).synced == {
        "iPad": at(620),
        "iPhone": at(610),
    }
    # more of the iPhone arrives, to past the clock: it is no longer behind
    mac.events += stretch(PHONE, "com.apple.camera", at(1000), at(1010))
    mac.pass_(at(700) + 16 * MIN)
    assert _fold(sources, tmp_path, at(1005)).synced == {"iPad": at(620)}


def test_screen_is_missing_when_the_store_holds_no_device_record_and_silent_without_a_store(
    mac: Mac,
    tmp_path: Path,
) -> None:
    """Screen is missing when the store holds no device record and silent without a store."""
    # a shipped store with something else in it
    mac.intake.shipments.receive_entries(
        "timesink",
        b'{"key":"x:1:aaaaaaaaaaaa","kind":"x","at_ms":1,"text":"","zone":"UTC","fields":{}}',
    )
    sources = DaySources(ZONE, shipped=mac.directory)
    line = _fold(sources, tmp_path, at(700))
    assert "screen" in line.missing
    assert line.synced == {}
    assert "screen" not in _fold(DaySources(ZONE), tmp_path, at(700)).missing
