"""Jev's routes: the decision log, the surrogate route and the one-shot readers."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jarvis.decision.jev_oneshot import TOOL_GROUPS, JevOneShot
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute, offered
from jarvis.decision.turn_end_asks import TurnEndAsks
from jarvis.decision.voice_words import VoiceWords
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.state.event_log import emit_event, open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.decision.tier0 import Tier0Table
    from jarvis.shared.clock import Clock


def _jev_log(config: Mapping[str, Any], root: Path) -> JevLog | None:
    """``jev_log`` (ADR 0128): on unless ``enabled: false``; the file is made at the first line."""
    block = config.get("jev_log")
    if isinstance(block, Mapping) and block.get("enabled") is False:
        return None
    return JevLog(root / "jev" / "decisions.jsonl")


def _surrogate_route(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
) -> SurrogateRoute | None:
    """``realtime.surrogate_route`` (ADR 0122): off unless enabled; bad values stop boot."""
    realtime = config.get("realtime")
    block = realtime.get("surrogate_route") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("min_confidence"), block.get("timeout_ms")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.surrogate_route needs model (text),"
            " min_confidence (0-1] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    return SurrogateRoute(
        model=model.strip(), min_confidence=float(bar), timeout_ms=timeout, clock=clock,
        parallel=block.get("parallel") is True, zdr=block.get("zdr") is not False,
        log=log,
    )


def _turn_end_asks(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
) -> TurnEndAsks | None:
    """``agents.turn_end_asks`` (ADR 0125): off unless enabled; bad values stop boot."""
    agents = config.get("agents")
    block = agents.get("turn_end_asks") if isinstance(agents, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("at"), block.get("timeout_ms")
    question_bar = block.get("question_at")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(question_bar, bool) or not isinstance(question_bar, int | float)
        or not 0 < question_bar <= bar
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} agents.turn_end_asks needs model (text), at in (0, 1],"
            " question_at in (0, at] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    return TurnEndAsks(route, float(bar), float(question_bar))


def _voice_words(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
) -> VoiceWords | None:
    """``realtime.jev_words`` (ADR 0130): off unless enabled; bad values stop boot."""
    realtime = config.get("realtime")
    block = realtime.get("jev_words") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar = block.get("model"), block.get("at")
    timeout, chars = block.get("timeout_ms"), block.get("max_chars")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.jev_words needs model (text), at in (0, 1],"
            " timeout_ms (positive int) and max_chars (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    return VoiceWords(route, float(bar), chars)


def _bar(value: object) -> float | None:
    """A confidence bar in (0, 1], or None when it is anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        return None
    return float(value)


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


def _jev_oneshot(  # noqa: PLR0913 - the config, the clock, the dataset, the table, the emitter
    config: Mapping[str, Any],
    config_path: Path,
    clock: Clock,
    log: JevLog | None,
    tier0_table: Tier0Table,
    emit: Callable[[str, dict[str, Any], str], None] | None = None,
) -> JevOneShot | None:
    """``realtime.jev_oneshot`` (ADR 0139, 0140): off unless enabled; bad values stop boot.

    It needs ``realtime.surrogate_route`` on (its instant functions and transport bar); the
    control words need ``realtime.jev_words`` too, and without it the surface asks nothing.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return None
    block = realtime.get("jev_oneshot")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    prefix = f"runtime: {config_path} realtime.jev_oneshot"
    model, timeout = block.get("model"), block.get("timeout_ms")
    relation, tool_line = block.get("relation"), block.get("tool_line")
    words = realtime.get("jev_words")
    surrogate = realtime.get("surrogate_route")
    if not isinstance(surrogate, Mapping) or surrogate.get("enabled") is not True:
        msg = f"{prefix} needs realtime.surrogate_route enabled"
        raise RuntimeBootstrapError(msg)
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or not isinstance(relation, Mapping) or not isinstance(tool_line, Mapping)
    ):
        msg = f"{prefix} needs model (text), timeout_ms (positive int), relation and tool_line"
        raise RuntimeBootstrapError(msg)
    relation_at = _bar(relation.get("at")) if relation.get("enabled") is True else None
    window = relation.get("window_s")
    tool_at = _bar(tool_line.get("at")) if tool_line.get("enabled") is True else None
    groups = tool_line.get("groups")
    if (
        (relation.get("enabled") is True and relation_at is None)
        or isinstance(window, bool) or not isinstance(window, int | float) or window <= 0
        or (tool_line.get("enabled") is True and tool_at is None)
        or not isinstance(groups, list) or not all(g in TOOL_GROUPS and g != "none" for g in groups)
    ):
        msg = (
            f"{prefix}.relation needs at in (0, 1] and window_s > 0; .tool_line needs at in"
            " (0, 1] and groups from the tool group names"
        )
        raise RuntimeBootstrapError(msg)
    words_at = words_timeout = words_chars = None
    if isinstance(words, Mapping) and words.get("enabled") is True:
        words_at, words_timeout = _bar(words.get("at")), words.get("timeout_ms")
        words_chars = words.get("max_chars")
        if (
            words_at is None
            or isinstance(words_timeout, bool) or not isinstance(words_timeout, int)
            or isinstance(words_chars, bool) or not isinstance(words_chars, int)
        ):
            msg = f"{prefix} reads realtime.jev_words at, timeout_ms and max_chars: not valid"
            raise RuntimeBootstrapError(msg)
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock,
        zdr=block.get("zdr") is not False, log=log,
    )
    return JevOneShot(
        route, offered(tier0_table),
        words_at=words_at, words_timeout_ms=words_timeout or 800, max_chars=words_chars or 24,
        relation_at=relation_at, window_s=float(window),
        tool_at=tool_at, tool_groups=frozenset(groups), emit=emit,
    )
