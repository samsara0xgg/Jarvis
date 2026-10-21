"""Jev's routes: the decision log, the surrogate route and the one-shot readers."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from jarvis.decision.jev_oneshot import JevOneShot
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute, offered
from jarvis.decision.turn_end_asks import TurnEndAsks
from jarvis.decision.voice_words import VoiceWords
from jarvis.state.event_log import emit_event, open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.decision.tier0 import Tier0Table
    from jarvis.runtime.config import (
        JevOneShotSettings,
        JevSettings,
        SurrogateRouteSettings,
        TurnEndAsksSettings,
        VoiceWordsSettings,
    )
    from jarvis.shared.clock import Clock


def build_jev_log(settings: JevSettings, root: Path) -> JevLog | None:
    """``jev_log`` (ADR 0128): on unless ``enabled: false``; the file is made at the first line."""
    return JevLog(root / "jev" / "decisions.jsonl") if settings.log else None


def _surrogate_route(
    settings: SurrogateRouteSettings | None, clock: Clock, log: JevLog | None = None,
) -> SurrogateRoute | None:
    """``realtime.surrogate_route`` (ADR 0122): the route, or None while it is off."""
    if settings is None:
        return None
    return SurrogateRoute(
        model=settings.model, min_confidence=settings.min_confidence,
        timeout_ms=settings.timeout_ms, clock=clock, parallel=settings.parallel,
        zdr=settings.zdr, log=log,
    )


def _turn_end_asks(
    settings: TurnEndAsksSettings | None, clock: Clock, log: JevLog | None = None,
) -> TurnEndAsks | None:
    """``agents.turn_end_asks`` (ADR 0125): the asks, or None while they are off."""
    if settings is None:
        return None
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=settings.model, min_confidence=1.0, timeout_ms=settings.timeout_ms, clock=clock,
        log=log,
    )
    return TurnEndAsks(route, settings.at, settings.question_at)


def _voice_words(
    settings: VoiceWordsSettings | None, clock: Clock, log: JevLog | None = None,
) -> VoiceWords | None:
    """``realtime.jev_words`` (ADR 0130): the reader, or None while it is off."""
    if settings is None:
        return None
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=settings.model, min_confidence=1.0, timeout_ms=settings.timeout_ms, clock=clock,
        log=log,
    )
    return VoiceWords(route, settings.at, settings.max_chars)


def _event_emitter(event_log_path: Path) -> Callable[[str, dict[str, Any], str], None]:
    """``(type, payload, turn_id) -> None``: one event on a turn, on the caller's own connection."""

    def emit(event_type: str, payload: dict[str, Any], turn_id: str) -> None:
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            emit_event(
                conn, type=event_type, payload=payload, correlation={"turn_id": turn_id},
            )

    return emit


def _jev_oneshot(
    settings: JevOneShotSettings | None,
    clock: Clock,
    log: JevLog | None,
    tier0_table: Tier0Table,
    emit: Callable[[str, dict[str, Any], str], None] | None = None,
) -> JevOneShot | None:
    """``realtime.jev_oneshot`` (ADR 0139, 0140): the one request per voice line, or None."""
    if settings is None:
        return None
    route = SurrogateRoute(
        model=settings.model, min_confidence=1.0, timeout_ms=settings.timeout_ms, clock=clock,
        zdr=settings.zdr, log=log,
    )
    return JevOneShot(
        route, offered(tier0_table),
        words_at=settings.words_at, words_timeout_ms=settings.words_timeout_ms,
        max_chars=settings.words_max_chars,
        relation_at=settings.relation_at, window_s=settings.window_s,
        tool_at=settings.tool_at, tool_groups=settings.tool_groups, emit=emit,
    )
