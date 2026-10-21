"""The wait for an action's result: the trigger poll and the checkpoint a suspended turn leaves."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.decision.response_run import ResponseCancelledError, ResponseRun
from jarvis.state.event_log import fetch_events_after

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.decision import turn_timing
    from jarvis.execution.tools import ActionLifecycle
    from jarvis.shared import Event


# Trigger event types the runtime loop expects from L4 paths.
# ``action.result_observed`` is defensive — sync tools emit it inline
# so decide() consumes it within one invocation, but we accept it here
# so a late-firing scheduled re-entry does not deadlock the poll loop.
# ``action.timeout_assumed`` / ``action.failed`` / ``action.cancelled``
# are the terminal failure events (supervisor sweep, dispatcher): the
# runtime must wake decide() so L3 can emit a canonical limitation
# response.
RUNTIME_TRIGGER_TYPES: tuple[str, ...] = (
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
    "action.cancelled",
)


# Default polling cadence for ``wait_for_next_trigger``. 10 ms balances
# CPU usage with first-byte latency once the Timer fires.
_DEFAULT_POLL_INTERVAL_S: float = 0.01


# Conversational per-trigger wait. 5 s is generous, and small enough that
# a stuck ordinary turn cannot pin one of ``max_concurrent_turns`` for
# long — see :func:`trigger_wait_budget`.
_DEFAULT_TRIGGER_TIMEOUT_S: float = 5.0


class TriggerWaitTimeout(RuntimeError):  # noqa: N818 — Day-1 vocabulary keeps `Timeout` suffix per ADR § Multi-trigger loop.
    """Raised by :func:`wait_for_next_trigger` when no trigger arrives in time."""


@dataclass(frozen=True)
class WaitingTurn:
    """Connection-free checkpoint while L4 works; live turn ownership stays held."""

    intent: Event
    run: ResponseRun | None
    after_id: int
    action_ids: frozenset[str]
    deadline: float
    iterations: int
    events: tuple[Event, ...]
    trigger: Event | None = None
    failure: Exception | None = None
    queued: bool = False
    timer: turn_timing.TurnTimer | None = None  # ADR 0276: the turn's clock runs through the wait


class TurnSuspended(Exception):  # noqa: N818 — internal scheduler handoff, not a failure.
    """Transfer a quiet turn back to the runtime scheduler."""

    def __init__(self, checkpoint: WaitingTurn) -> None:
        """Carry only connection-free state across worker invocations."""
        super().__init__(checkpoint.intent.payload["turn_id"])
        self.checkpoint = checkpoint


def _absorbed_inline(lifecycle: ActionLifecycle, action_id: str | None) -> bool:
    """True when this process already moved ``action_id`` past ``running``.

    Sync tool handlers transition the FSM inline before ``dispatch`` returns,
    so their ``action.result_observed`` row is history to the waiter, not a
    trigger. ``None`` (unknown here — another process wrote it) is not
    treated as absorbed.
    """
    if action_id is None:
        return False
    state = lifecycle.state_of(action_id)
    return state is not None and state != "running"


def event_action_id(event: Event) -> str | None:
    """Return the action_id this event belongs to, or ``None``.

    Correlation first (the canonical slot every L4 emitter fills via
    ``_action_correlation``), payload second (all four trigger types
    mirror the id there, and the supervisor sweep's
    ``action.timeout_assumed`` fills both).

    ADR-0009 D4 partitions terminal events between two consumers — the
    in-turn waiter (``action_id`` IS one of the turn's own) and
    ``_system_trigger_watcher`` (``action_id`` is in NO live turn's set).
    Both predicates read the key through THIS function, so the two
    filters are complements of one another by construction and no event
    can be claimed twice or dropped by both.
    """
    for source in (event.correlation, event.payload):
        if source is None:
            continue
        value = source.get("action_id")
        if isinstance(value, str):
            return value
    return None


def wait_for_next_trigger(  # noqa: PLR0913 — one defaulted cancel predicate on top of the existing waiter contract.
    conn: sqlite3.Connection,
    *,
    after_id: int,
    action_ids: frozenset[str],
    lifecycle: ActionLifecycle,
    timeout: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Event, int]:
    """Poll the event log for the next L3 trigger event after ``after_id``.

    Trigger types: ``action.result_observed`` (defensive — sync
    tools emit this inline so decide() already absorbed it, but a
    late re-entry is accepted to keep the poll loop drainable), and
    ``action.timeout_assumed`` / ``action.failed`` / ``action.cancelled``
    (terminal failures emitted by the dispatcher or the supervisor
    sweep when the
    subprocess crashes; the runtime waiter must wake decide() so L3
    can fold a Limitation Claim).

    Scoping (ADR-0009 D4 / F9). Type + ``after_id`` alone is NOT a
    correlation: a supervisor sweep or wake reconciliation closing some
    *foreign* orphan writes an ``action.timeout_assumed`` into the same
    log, and the pre-D4 waiter would hand it to whichever turn happened
    to be in flight — which then adopts the orphan's correlation and
    ends the wrong turn with the wrong limitation. Every candidate row
    is therefore matched against ``action_ids``, the set of actions THIS
    turn dispatched; foreign rows are skipped over (the local cursor
    still advances past them, so they are examined once, not once per
    poll). This is a filter, not a new trigger type — the
    ``RUNTIME_TRIGGER_TYPES`` tuple is untouched.

    The runtime composition root explicitly polls across thread
    boundaries; ``time.sleep`` is the cleanest primitive here. The
    "no time.sleep" rule is for L3/L4 gate machinery that must not
    block on wall-clock; the orchestration loop is a different
    concern (spec §3.4.1).

    Args:
        conn: Open Event Log connection. NOTE: this is the same
            connection :func:`bootstrap_runtime_app` opened — the
            ``threading.Timer`` callback opens its OWN connection per
            ``jarvis.execution.tools`` so ``check_same_thread=True``
            stays honored.
        after_id: The most recent SQLite row id the caller has
            already consumed. Returned events all have ``id >
            after_id``.
        action_ids: The action_ids this turn owns — read fresh per call
            from :func:`jarvis.execution.tools.turn_action_ids` so an
            action dispatched during the previous decide() iteration is
            already in scope. A row whose ``action_id`` is outside this
            set belongs to somebody else and is never returned.
        lifecycle: The process-local action FSM. An
            ``action.result_observed`` row is only a wake-up for an action
            this process still holds at ``running``; a sync tool writes
            the same row inline and moves its action past ``running``
            before ``dispatch`` returns, so decide() has already absorbed
            it. An action unknown to this process (crash recovery) is not
            filtered.
        timeout: Hard wall-clock cap in seconds; raise
            :class:`TriggerWaitTimeout` if exceeded.
        poll_interval_s: Sleep between polls (default 10 ms).
        cancelled: ADR-0008 Wave 4A — optional predicate checked once
            per poll tick. When it returns True the wait aborts with
            :class:`~jarvis.decision.response_run.ResponseCancelledError`.
            This is the single line that makes a response cancel land
            within one poll interval during a 600 s action wait instead
            of after it. ``None`` (the default) polls without a cancel check.

    Returns:
        Tuple of ``(event, new_after_id)`` — the freshly-folded
        :class:`Event` plus the SQLite row id to use for the next
        wait call.

    Raises:
        TriggerWaitTimeout: No matching trigger arrived within
            ``timeout`` seconds.
        ResponseCancelledError: ``cancelled`` reported that this turn's
            ResponseRun already reached a cancelled terminal.
    """
    deadline = time.monotonic() + timeout
    cursor_id = after_id
    while True:
        for row_id, event in fetch_events_after(
            conn, after_id=cursor_id, event_types=RUNTIME_TRIGGER_TYPES,
        ):
            cursor_id = row_id
            if event_action_id(event) in action_ids:
                if event.type == "action.result_observed" and _absorbed_inline(
                    lifecycle, event_action_id(event),
                ):
                    continue
                return event, cursor_id
        if cancelled is not None and cancelled():
            msg = (
                "runtime: response run cancelled while waiting for a trigger of "
                f"types {RUNTIME_TRIGGER_TYPES!r} (after_id={after_id})."
            )
            raise ResponseCancelledError(msg)
        if time.monotonic() >= deadline:
            msg = (
                f"runtime: no trigger event of types {RUNTIME_TRIGGER_TYPES!r} "
                f"for action_ids={sorted(action_ids)!r} arrived within "
                f"{timeout!r}s (after_id={after_id})."
            )
            raise TriggerWaitTimeout(msg)
        time.sleep(poll_interval_s)


def trigger_wait_budget(override: float | None) -> float:
    """Return this iteration's per-trigger wait budget, in seconds.

    An explicit caller value always wins. Scenarios and any surface that
    wants its own budget keep it; otherwise the conversational default, so
    an ordinary turn cannot pin one of ``max_concurrent_turns`` for long.
    """
    if override is not None:
        return override
    return _DEFAULT_TRIGGER_TIMEOUT_S
