"""L2 acceptance for the ADR-0014 D6 ``log_epoch`` (lane C slice 1).

The epoch names one Event Log lineage.  A realtime client compares the
epoch it last saw against the one in ``server.hello`` to decide whether it
may resume from its cursor or must take a fresh snapshot, so the value has
to be assigned exactly once per file and then survive every operation that
preserves the log's contents: restart, ``VACUUM``, and a byte copy.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import (
    open_event_log,
    open_runtime_event_log,
    read_log_epoch,
)

if TYPE_CHECKING:
    from pathlib import Path


def _epoch_rows(path: Path) -> list[str]:
    """Read every metadata row from a closed log file, bypassing the API."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute("SELECT log_epoch FROM event_log_metadata")]


def _make_pre_upgrade_log(path: Path) -> None:
    """Produce a schema-v1 file: bootstrapped, then stripped of the epoch."""
    with contextlib.closing(open_event_log(path)):
        pass
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TABLE event_log_metadata")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()


def test_fresh_log_gets_exactly_one_epoch_that_survives_reopen(tmp_path: Path) -> None:
    """A new database is stamped once; reopening never re-mints."""
    path = tmp_path / "mac_events.db"

    with contextlib.closing(open_event_log(path)) as conn:
        first = read_log_epoch(conn)

    assert first.startswith("L")
    assert _epoch_rows(path) == [first]

    with contextlib.closing(open_event_log(path)) as conn:
        assert read_log_epoch(conn) == first
    rows = _epoch_rows(path)
    assert len(rows) == 1
    assert rows == [first]


def test_pre_upgrade_log_is_stamped_once_and_stays_stable(tmp_path: Path) -> None:
    """A v1 file gains one epoch on upgrade; copy and VACUUM keep it."""
    path = tmp_path / "mac_events.db"
    _make_pre_upgrade_log(path)

    with contextlib.closing(open_event_log(path)) as conn:
        assigned = read_log_epoch(conn)
    assert assigned.startswith("L")
    assert _epoch_rows(path) == [assigned]

    with contextlib.closing(open_event_log(path)) as conn:
        assert read_log_epoch(conn) == assigned
        conn.execute("VACUUM")
        assert read_log_epoch(conn) == assigned

    copied = tmp_path / "copy_of_mac_events.db"
    shutil.copy2(path, copied)
    assert _epoch_rows(copied) == [assigned]
    with contextlib.closing(open_event_log(copied)) as conn:
        assert read_log_epoch(conn) == assigned
    assert _epoch_rows(copied) == [assigned]


def test_runtime_open_fails_closed_until_bootstrap_migrates(tmp_path: Path) -> None:
    """A v1 file is unusable at runtime until ``open_event_log`` upgrades it."""
    path = tmp_path / "mac_events.db"
    _make_pre_upgrade_log(path)

    with pytest.raises(sqlite3.OperationalError, match="requires bootstrap migration"):
        open_runtime_event_log(path)

    with contextlib.closing(open_event_log(path)) as conn:
        expected = read_log_epoch(conn)
    with contextlib.closing(open_runtime_event_log(path)) as conn:
        assert read_log_epoch(conn) == expected


def test_read_log_epoch_raises_when_the_row_is_missing(tmp_path: Path) -> None:
    """Serving a hello without an epoch would let a client resume blind."""
    path = tmp_path / "mac_events.db"
    with contextlib.closing(open_event_log(path)) as conn:
        conn.execute("DELETE FROM event_log_metadata")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="no log_epoch row"):
            read_log_epoch(conn)
