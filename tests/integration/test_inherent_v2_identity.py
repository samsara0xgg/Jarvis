"""Identity acceptance for the Inherent v2 handshake (ADR-0014 D5/D6).

The epoch names one Event Log lineage.  A realtime client compares the
epoch it last saw against the one in ``server.hello`` to decide whether it
may resume from its cursor or must take a fresh snapshot, so the value has
to be assigned exactly once per file and then survive every operation that
preserves the log's contents: restart, ``VACUUM``, and a byte copy.

The D5 bearer token is the connection's other half of that identity, with
the opposite lifetime: a fresh secret every boot, readable by nobody but its
owner.  Both live here so the handshake's inputs stay in one place.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment import (
    InherentTokenError,
    inherent_v2_token_matches,
    rotate_inherent_v2_token,
)
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


def test_inherent_v2_token_rotation_is_private_and_fresh(tmp_path: Path) -> None:
    """Each boot writes a new 256-bit secret readable only by its owner."""
    path = tmp_path / "inherent-v2.token"

    first = rotate_inherent_v2_token(path)
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == first + "\n"

    second = rotate_inherent_v2_token(path)
    assert second != first
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == second + "\n"


def test_inherent_v2_token_refuses_a_symlink_at_the_path(tmp_path: Path) -> None:
    """A planted link must not redirect the secret to someone else's file."""
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    path = tmp_path / "inherent-v2.token"
    path.symlink_to(victim)

    with pytest.raises(InherentTokenError, match="not a regular file"):
        rotate_inherent_v2_token(path)

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert path.is_symlink()


def test_inherent_v2_token_refuses_a_directory_at_the_path(tmp_path: Path) -> None:
    """A directory is not something rotation may replace."""
    path = tmp_path / "inherent-v2.token"
    path.mkdir()

    with pytest.raises(InherentTokenError, match="not a regular file"):
        rotate_inherent_v2_token(path)

    assert path.is_dir()


def test_inherent_v2_token_refuses_a_world_readable_predecessor(tmp_path: Path) -> None:
    """A 0644 token file already leaked; do not write a new secret into it."""
    path = tmp_path / "inherent-v2.token"
    path.write_text("stale\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(InherentTokenError, match="beyond its owner"):
        rotate_inherent_v2_token(path)

    assert path.read_text(encoding="utf-8") == "stale\n"


def test_inherent_v2_token_refuses_a_writable_parent(tmp_path: Path) -> None:
    """A group-writable directory lets anyone swap the file after we write it."""
    parent = tmp_path / "runtime"
    parent.mkdir()
    parent.chmod(0o775)

    with pytest.raises(InherentTokenError, match="group- or world-writable"):
        rotate_inherent_v2_token(parent / "inherent-v2.token")

    assert not (parent / "inherent-v2.token").exists()


def test_inherent_v2_token_matches_only_the_exact_token(tmp_path: Path) -> None:
    """Comparison accepts the real token and rejects everything else.

    The wrong-owner branch of `rotate_inherent_v2_token` is not exercised
    here: giving a file or directory a uid other than `os.geteuid()`
    requires root, which the hermetic suite never has.
    """
    token = rotate_inherent_v2_token(tmp_path / "inherent-v2.token")

    assert inherent_v2_token_matches(token, token) is True
    wrong = ("0" if token[0] != "0" else "1") + token[1:]
    assert inherent_v2_token_matches(token, wrong) is False
    assert inherent_v2_token_matches(token, "") is False
    assert inherent_v2_token_matches(token, "not-a-token") is False
    assert inherent_v2_token_matches(token, token + "\n") is False
    assert inherent_v2_token_matches(token, "你好") is False
