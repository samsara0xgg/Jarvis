"""A closed event log, clean or spoiled, and the secrets it is checked for leaking."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_780_000_000_000  # 2026-05-28T20:26:40Z, the instant the check "runs".


SEC = 1000


MIN = 60 * SEC


# One sample per shape, built from pieces so this file holds no key-looking literal.
SECRET_SAMPLES: dict[str, str] = {
    "pem_private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "sk_api_key": "sk-" + "Ab1" * 10,
    "aws_access_key_id": "AKIA" + "0123456789ABCDEF",
    "github_token": "ghp_" + "a1" * 20,
    "slack_token": "xoxb-" + "1234567890-abcdef",
    "google_api_key": "AIza" + "Sy" + "0123456789abcdefghijklmnopqrstuvw",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0In0" + ".sig" + "nature12345",
    "bearer_token": "Authorization: Bearer " + "abcdefghij0123456789klmnop",
}


MARKER = "PRIVATE-WORDS-OF-THE-OWNER"


SECRET = SECRET_SAMPLES["github_token"]


WEIRD_TYPE = f"weird type {MARKER}"


class LogClock:
    now = NOW - 30 * MIN


def say_to_log(conn: Any, clock: LogClock, words: str) -> None:  # noqa: ANN401 - a sqlite3 connection.
    clock.now += MIN
    emit_event(
        conn,
        type="surface.conversation_words",
        payload={"turn_id": "T", "reason": "r", "transcript": words},
    )


def make_log_root(root: Path, clock: LogClock, *, spoiled: bool) -> None:
    """A closed WAL log (its WAL checkpointed away on close), clean or with three faults."""
    (root / "memory" / "audio").mkdir(parents=True)
    conn = open_event_log(root / "mac_events.db")
    try:
        for _ in range(3):
            say_to_log(conn, clock, "nothing private")
        if spoiled:
            say_to_log(conn, clock, f"{MARKER} {SECRET}")
            conn.execute(
                "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, "
                "actor, ingestion_node) VALUES ('w', ?, 1, ?, ?, 'x', 'mac')",
                (WEIRD_TYPE, clock.now, json.dumps({"transcript": MARKER})),
            )
            conn.commit()
    finally:
        conn.close()
