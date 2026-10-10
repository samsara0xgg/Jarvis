"""Opt-in acceptance (ADR 0214): "把它推到明天" acts on the item the phone has open.

--live-llm sends one short conversation to the configured conversation model (``llm`` in
``config/jarvis.yaml``, the shipped prompt, the default tool registry) twice, in a temp runtime
root with two waiting reminders and nothing else of Jarvis's state:

- with ``about`` naming the first reminder, the first is moved to tomorrow at the same clock and
  the second is not touched: after the turn two reminders wait, the old first one is cancelled;
- the same words without ``about``: no reminder is set or cancelled, and she asks which one.

Needs ``OPENAI_API_KEY`` in the environment (read by the client, never printed).
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.state import reminders
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.memory_db import MemorySettings
from jarvis.surface.cli import emit_surface_user_intent
from tests.integration.test_wire_routine_streaming import _drive, _payloads, _runtime

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime

pytestmark = pytest.mark.live_llm

_REPO = Path(__file__).resolve().parents[2]
_WORDS = "把它推到明天"
_ASKS = ("哪个", "哪一个", "哪条", "哪件", "which", "Which")


def _live_runtime(tmp_path: Path) -> JarvisRuntime:
    """The routine-stream runtime of the integration tests, with the shipped model and prompt."""
    shipped = yaml.safe_load((_REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    # As shipped (routine streaming off); the fixture URL is never called.
    base = _runtime(tmp_path, "http://127.0.0.1:9/v1", routine=False)
    return replace(
        base,
        llm_client=LLMClient(shipped["llm"]),
        llm_session_factory=LLMSessionFactory(shipped["llm"]),
        system_prompt=(_REPO / "prompts" / "jarvis_v1.md").read_text(encoding="utf-8"),
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
    )


def _seed(runtime: JarvisRuntime) -> tuple[str, str, datetime]:
    """Two waiting reminders today; returns the first's id, the second's id and the first's time."""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    first_due = now + timedelta(hours=2)
    if first_due.date() != now.date():
        first_due = now.replace(hour=23, minute=50)
    second_due = first_due + timedelta(minutes=30)
    first = reminders.schedule(
        runtime.conn, due=first_due, text="给妈妈打电话", action_id="A-seed-1",
    )
    second = reminders.schedule(
        runtime.conn, due=second_due, text="交房租", action_id="A-seed-2",
    )
    return first, second, first_due


def _ask(runtime: JarvisRuntime, first: str, due: datetime, *, with_about: bool) -> str:
    """The phone's typed turn; returns the answer's text."""
    about: dict[str, Any] | None = {
        "kind": "reminder", "id": first, "title": "给妈妈打电话",
        "start_ms": int(due.timestamp() * 1000),
    } if with_about else None
    intent = emit_surface_user_intent(
        runtime.conn, transcript=_WORDS, turn_id="T-live-about", channel="cli_stdin",
        ingestion_node="iphone", about=about,
    )
    _drive(runtime, intent)
    return str(_payloads(runtime.conn, "surface.response_emitted")[-1]["text"])


def _require_key() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set; the conversation model is OpenAI's")


def test_live_it_means_the_open_reminder(tmp_path: Path) -> None:
    """With ``about``: the first reminder moves to tomorrow at the same clock, the second stays."""
    _require_key()
    runtime = _live_runtime(tmp_path)
    first, second, due = _seed(runtime)
    answer = _ask(runtime, first, due, with_about=True)

    state = reminders.fold(runtime.conn)
    waiting = reminders.pending(runtime.conn)
    print(f"\nwith about: {answer!r}; waiting: {[(r.text, r.due_at_local) for r in waiting]}")  # noqa: T201 - the live run's evidence
    assert state[first].cancelled, "the old reminder is cancelled"
    assert state[second].pending, "the other reminder is untouched"
    moved = [r for r in waiting if r.reminder_id not in {first, second}]
    assert len(moved) == 1, [(r.text, r.due_at_local) for r in waiting]
    assert len(waiting) == 2
    tomorrow = (due + timedelta(days=1)).astimezone()
    got = datetime.fromisoformat(moved[0].due_at_local).astimezone()
    assert (got.date(), got.hour, got.minute) == (tomorrow.date(), due.hour, due.minute)
    assert "妈妈" in moved[0].text


def test_live_without_about_she_asks_which_one(tmp_path: Path) -> None:
    """The same words with two reminders waiting and nothing open: nothing changes, she asks."""
    _require_key()
    runtime = _live_runtime(tmp_path)
    first, second, due = _seed(runtime)
    answer = _ask(runtime, first, due, with_about=False)

    print(f"\nwithout about: {answer!r}")  # noqa: T201 - the live run's evidence
    writes = iter_events_of_types(runtime.conn, ("reminder.scheduled", "reminder.cancelled"))
    changed = [e.type for e in writes if e.payload.get("action_id") not in {"A-seed-1", "A-seed-2"}]
    assert changed == [], "no reminder was set or cancelled"
    assert {r.reminder_id for r in reminders.pending(runtime.conn)} == {first, second}
    assert any(word in answer for word in _ASKS), answer
