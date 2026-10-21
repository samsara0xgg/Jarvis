"""The cancel seams: a stop, a barge-in, a newer sentence or a continuation ends open runs."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Final, Protocol

from jarvis.decision.response_run import (
    CancelAccepted,
    CancelAlreadyTerminal,
    CancelPlaybackAuthorized,
    CancelTimedOut,
    ResponseCancelRequest,
    ResponseTerminalizer,
    decide_foreground,
    request_response_cancel,
)
from jarvis.runtime import JarvisRuntime, device_voice
from jarvis.shared.realtime import RESPONSE_CANCEL_REASONS
from jarvis.state.authorized_dispatch_outbox import (
    ConfirmationRevalidationError,
    answer_confirmation_once,
)
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.turn_facts import (
    card_requested_by_turn,
    turn_dispatched_action,
    turn_has_run,
    turn_is_over,
    turns_closed_since,
    turns_dispatching_since,
    turns_of_his_words_since,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path


LOGGER = logging.getLogger(__name__)


class ResponseCancel(Protocol):
    """``(response_id, scope, reason, by_turn) -> outcome``; ``by_turn`` took the run's place."""

    def __call__(
        self, response_id: str, scope: str, reason: str, by_turn: str | None = None,
    ) -> str:
        """Cancel the response's ``scope``; the outcome word says what happened."""
        ...


def make_response_cancel_callable(  # noqa: C901 - one seam, two scopes' exact outcome words
    runtime: JarvisRuntime,
    *,
    stop_foreground_output: Callable[[str], str] | None = None,
) -> ResponseCancel:
    """Build the injectable ``(response_id, scope, reason) -> outcome`` seam.

    Returned strings for ``scope="generation"``: ``"cancelled"``,
    ``"already_terminal"``, ``"unknown_response"``, ``"unsupported_scope"``,
    ``"timeout"``. ``scope="foreground_output"`` adds L5's own words
    ``"applied"``, ``"stale"`` and ``"uncertain"``, plus
    ``"policy_hash_mismatch"`` and ``"policy_ignore"`` from L3's policy
    check. Without ``stop_foreground_output`` there is no playback actor to
    reach, so that scope answers ``"unsupported_scope"``.

    The callable opens its OWN connection per call. That is mandatory, not
    stylistic: ``open_event_log`` uses ``check_same_thread=True`` and the
    surface offloads this onto an ``asyncio.to_thread`` worker. The
    connection also carries ``PRAGMA busy_timeout = cancel_timeout_ms``,
    which is what gives ``realtime.response.cancel_timeout_ms`` a concrete
    meaning: a contended ``BEGIN IMMEDIATE`` gives up after that many
    milliseconds, no terminal is written, and the caller may retry.
    """
    cancel_timeout_ms = runtime.config.realtime.cancel_timeout_ms
    event_log_path = runtime.runtime_paths.event_log
    committed_event_bus = runtime.committed_event_bus
    registry = runtime.response_runs

    def _cancel(  # noqa: PLR0911 - policy, timeout, CAS and playback outcomes stay distinct
        response_id: str,
        scope: str,
        reason: str,
        by_turn: str | None = None,
    ) -> str:
        deadline = time.monotonic() + cancel_timeout_ms / 1000

        def _connect() -> sqlite3.Connection:
            return open_runtime_event_log(event_log_path, deadline=deadline)

        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return "unknown_response"
        if scope not in ("generation", "foreground_output"):
            return "unsupported_scope"
        normalized_reason = reason
        if normalized_reason not in RESPONSE_CANCEL_REASONS:
            LOGGER.warning(
                "cancel-response: reason %r is outside the closed vocabulary; "
                "recording it as 'operator_request'",
                reason,
            )
            normalized_reason = "operator_request"
        outcome = request_response_cancel(
            registry,
            ResponseTerminalizer(
                _connect,
                close_after=True,
                committed_event_bus=committed_event_bus,
            ),
            ResponseCancelRequest(
                request_id="CREQ" + uuid.uuid4().hex,
                response_id=response_id,
                scope="generation" if scope == "generation" else "foreground_output",
                reason=normalized_reason,
                source_turn_id=by_turn,
            ),
            deadline=deadline,
        )
        if isinstance(outcome, CancelAccepted):
            return "cancelled"
        if isinstance(outcome, CancelAlreadyTerminal):
            return "already_terminal"
        if isinstance(outcome, CancelTimedOut):
            return "timeout"
        if isinstance(outcome, CancelPlaybackAuthorized):
            if stop_foreground_output is None:
                return "unsupported_scope"
            return stop_foreground_output(response_id)
        return outcome.reason

    return _cancel


def make_foreground_decision_callable(
    *, wait_for_lane: bool = False,
) -> Callable[[str, int, str, int], str]:
    """Build the injectable foreground-lane arbitration seam.

    ``(incumbent_group, incumbent_row_id, candidate_group, candidate_row_id)
    -> outcome``, where the outcome is ``"enqueue_after_drain"``,
    ``"supersede"`` or ``"decline"``.

    The policy is a pure L3 function with no clock and no IO, so there is
    nothing to bind but ``realtime.response.slow_results``; the builder exists
    because ``jarvis.decision`` and ``jarvis.surface`` are siblings
    (``.importlinter``) and the runtime is the only layer allowed to wire them.
    """
    return partial(decide_foreground, wait_for_lane=True) if wait_for_lane else decide_foreground


def _turn_has_run(event_log_path: Path, turn_id: str) -> bool:
    """Whether the turn already opened a response run (its answer is out or over)."""
    with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
        return turn_has_run(conn, turn_id)


def make_turn_cancel_callable(runtime: JarvisRuntime) -> Callable[[str, str], str]:
    """Build the ``(turn_id, reason) -> outcome`` seam behind a turn stop.

    The surface's stop while Jarvis is still thinking: that turn's answer has
    no response id on the wire until it is whole (ADR 0108), so the surface
    names the turn and every run of it still open is cancelled as
    ``generation``. ``no_open_run`` when none is open: the answer is out, and
    the surface stops that by its response id.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _cancel_turn(turn_id: str, reason: str) -> str:
        if registry is not None and reason == "user_stop":
            # Before the scan: a run opening meanwhile checks this on its own.
            registry.mark_turn_stopped(turn_id)
        runs = [] if registry is None else [
            run for run in registry.open_runs() if run.turn_id == turn_id
        ]
        outcomes = [cancel(run.response_id, "generation", reason) for run in runs]
        if outcomes:
            return "cancelled" if "cancelled" in outcomes else outcomes[0]
        if registry is None or reason != "user_stop" or _turn_has_run(event_log_path, turn_id):
            return "no_open_run"
        return "stopped_before_start"

    return _cancel_turn


def make_barge_in_interrupt_callable(
    runtime: JarvisRuntime,
    *,
    only_turns: Callable[[str], bool] | None = None,
) -> Callable[[str], str]:
    """Build the injectable ``(confirm_source) -> outcome`` barge-in seam.

    ADR-0006 D8: the runtime is a mechanical applier of the interrupt policy
    the run was started with, never its author.  The target is resolved from
    the live-run index rather than supplied by L5 — the voice session never
    learns a response id — and a confirmed barge-in cancels only when that
    run's own ``ResponseInterruptPolicy`` permits it.

    Returned strings beyond ``make_response_cancel_callable``'s own outcomes:
    ``"no_open_run"``, ``"ambiguous_open_runs"``, ``"policy_ignore"``,
    ``"policy_generation_continue"``.

    ``only_turns`` (ADR 0209, 0219) narrows the target to the open runs of the turns it
    accepts: a device's barge-in interrupts that device's answer, whatever else is open.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs

    def _interrupt(confirm_source: str) -> str:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return "no_open_run"
        # A commentary run for the same turn is legitimately open while the
        # final run waits on its action (ADR-0006 D8): counting it would make
        # every action-dispatching turn read as `ambiguous_open_runs`.
        open_runs = tuple(
            run
            for run in registry.open_runs()
            if run.phase == "final" and (only_turns is None or only_turns(run.turn_id))
        )
        if not open_runs:
            return "no_open_run"
        if len(open_runs) > 1:
            return "ambiguous_open_runs"
        run = open_runs[0]
        policy = run.interrupt_policy
        if policy.confirmed_playback != "interrupt_expected_playback_generation":
            return "policy_ignore"
        if policy.generation_action != "cancel":
            return "policy_generation_continue"
        LOGGER.info(
            "barge-in confirmed via %s; cancelling response %s generation",
            confirm_source,
            run.response_id,
        )
        return cancel(run.response_id, "generation", "barge_in")

    return _interrupt


# How far back a voice sentence still counts as the first half of a split one.
SUPERSEDE_WINDOW_S: Final[float] = 10.0


def make_supersede_unspoken_callable(
    runtime: JarvisRuntime,
    drop_unspoken: Callable[[frozenset[str]], frozenset[str]],
    *,
    only_turns: Callable[[str], bool] | None = None,
    leave: Callable[[str], str | None] | None = None,
) -> Callable[[str], None]:
    """Build the ADR 0074 ``(accepted_turn_id) -> None`` seam.

    Called for a voice utterance just accepted, before its
    ``utterance.received`` is written. Every other turn of the last
    ``SUPERSEDE_WINDOW_S`` that answers a voice sentence or a filled-in ask
    card (``clarify`` intent) whose run is still open, whose policy lets its
    generation be cancelled, and whose answer never reached the speaker is
    dropped: L5 discards its queued audio (``drop_unspoken``), then the run is
    cancelled with reason ``superseded``. Its run is open because no run
    completes while Allen is talking, so nothing of it reaches memory.db and
    the new turn's prompt folds its words in (ADR 0044). A card it put up and
    that still waits is rejected with rule ``superseded`` (ADR 0074), so the
    new turn asks afresh.

    A turn whose run has not opened yet (a line accepted under 0.5 s before
    this one) is remembered and cancelled the same way as its run registers.

    With ``slow_results`` on, a turn that already dispatched an action is
    skipped: it is working on a lookup, not half of a split sentence, and the
    new question gets its own answer while the lookup's follows. Live test
    2026-10-01: a weekday question 7 s after a Micron search was dispatched
    cancelled it, re-ran the search for 24 s and never answered the weekday.

    ``only_turns`` (ADR 0219) narrows both the open runs and the turns marked superseded to
    the turns it accepts: a device's voice replaces its own earlier sentence, never another
    device's answer.

    ``leave`` (ADR 0278) names the one turn the sweep leaves alone for ``turn_id``: the
    earlier sentence the new one is composed into at the utterance, which takes it over
    whether or not it dispatched a tool or any of it is audible. Without it the sweep is the
    whole of how a sentence replaces the one before.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _supersede(turn_id: str) -> None:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return
        kept = leave(turn_id) if leave is not None else None
        runs = [
            run
            for run in registry.open_runs()
            if run.phase == "final"
            and run.turn_id not in {turn_id, kept}
            and run.interrupt_policy.generation_action == "cancel"
            and (only_turns is None or only_turns(run.turn_id))
        ]
        any_open = bool(runs)
        since_ms = int((time.time() - SUPERSEDE_WINDOW_S) * 1000)
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            recent = turns_of_his_words_since(conn, since_ms=since_ms)
            if only_turns is not None:
                recent = {recent_turn for recent_turn in recent if only_turns(recent_turn)}
            if kept is not None:
                recent.discard(kept)
            working: set[str] = set()
            if runtime.response_flags.slow_results:
                # The action names its turn in its correlation (L4 stamps it
                # from the ActionRequest), so this is a durable read, and it is
                # after `since_ms` because the turn's utterance was.
                working = turns_dispatching_since(conn, since_ms=since_ms)
                runs = [run for run in runs if run.turn_id not in working]
            # A line accepted within ~0.5 s of the one before it is swept before
            # that line's run opens (its request is built after its words): mark
            # every recent turn with no run yet, and `register` cancels it on open.
            registry.mark_turns_superseded(
                recent - {turn_id} - working
                - {run.turn_id for run in registry.open_runs()}
                - turns_closed_since(conn, since_ms=since_ms),
                for_s=SUPERSEDE_WINDOW_S,
                by_turn=turn_id,
            )
            # ponytail: a run that passed the completion hold just before Allen
            # started talking can complete between this drop and its cancel; its
            # queued audio is then lost while its row stays. A millisecond window.
            dropped = (
                drop_unspoken(frozenset(run.turn_id for run in runs) & recent)
                if any_open
                else frozenset()
            )
            for run in runs:
                if run.turn_id not in dropped:
                    continue
                outcome = cancel(run.response_id, "generation", "superseded", turn_id)
                LOGGER.info(
                    "unspoken answer superseded by %s: turn %s response %s -> %s",
                    turn_id, run.turn_id, run.response_id, outcome,
                )
                if outcome == "cancelled":
                    _withdraw_card(conn, run.turn_id, turn_id)

    return _supersede


def make_relation_supersede_callable(
    runtime: JarvisRuntime,
    drop_unspoken: Callable[[frozenset[str]], frozenset[str]],
    stop_playback: Callable[[], object],
) -> Callable[[str, str, str], str]:
    """Build the ADR 0139 ``(earlier_turn_id, turn_id, relation) -> outcome`` seam.

    Jev read a line as a supplement or correction of the one before it, confidently enough. The
    earlier turn's open answer run is cancelled as ``superseded`` (the new turn's prompt then
    folds the earlier words in, ADR 0044) and its card withdrawn, the way ADR 0074 does it for
    an accepted line, but aimed at that one turn and not bounded by the 10 s window. A turn
    that dispatched an action is left alone (it is working on a lookup). An answer that has
    begun playing is left to barge-in, except for a correction, which stops it and cancels the
    run. Outcomes: ``superseded``, ``stopped``, ``audible``, ``working``, ``no_open_run``,
    ``no_registry``.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _relate(earlier: str, turn_id: str, relation: str) -> str:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return "no_registry"
        runs = [
            run
            for run in registry.open_runs()
            if run.phase == "final"
            and run.turn_id == earlier
            and run.interrupt_policy.generation_action == "cancel"
        ]
        if not runs:
            return "no_open_run"
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            if turn_dispatched_action(conn, earlier):
                return "working"
            unspoken = earlier in drop_unspoken(frozenset({earlier}))
            if not unspoken:
                if relation != "correction":
                    return "audible"
                stop_playback()
            for run in runs:
                outcome = cancel(run.response_id, "generation", "superseded", turn_id)
                LOGGER.info(
                    "earlier answer %s by %s (%s): response %s -> %s",
                    "superseded" if unspoken else "stopped", turn_id, relation,
                    run.response_id, outcome,
                )
                if outcome == "cancelled":
                    _withdraw_card(conn, run.turn_id, turn_id)
        return "superseded" if unspoken else "stopped"

    return _relate


def make_compose_callable(
    runtime: JarvisRuntime,
) -> Callable[[str, str, str | None], str | None]:
    """Build the ADR 0278 ``(device, turn_id, words) -> earlier_turn_id | None`` seam.

    For a spoken sentence from any device, the earlier turn of that device it goes on from:
    its latest earlier spoken sentence when her answer to it is not over, within
    ``device_voice.COMPOSE_BOUND_S``. A stop request, a repeat of the earlier words and a
    host without run bookkeeping answer ``None``. With ``words`` ``None`` the words checks
    are left out. Blocks on SQLite; call from a worker thread.
    """
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _compose(device: str, turn_id: str, words: str | None) -> str | None:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return None
        return device_voice.compose_target(
            event_log_path, device, turn_id, words, superseded=registry.turn_superseded,
        )

    return _compose


def make_continuation_callable(runtime: JarvisRuntime) -> Callable[[str, str], str]:
    """Build the ADR 0271 ``(earlier_turn_id, turn_id) -> outcome`` seam.

    The words of ``turn_id`` go on from the sentence of ``earlier_turn_id``, the same device's
    (the brain's pick, ADR 0278: :func:`make_compose_callable`), and ``turn_id`` will answer
    both. The earlier turn is marked superseded by it, so a run that opens later is cancelled
    as it opens, and every run of it still open is cancelled as
    ``superseded`` naming ``turn_id``, spoken to the speaker or not (the sweep of ADR 0074 leaves
    an audible answer to barge-in; here the words are known to go on from it), whether or not
    it dispatched an action; a card it put up that still waits is rejected. The caller has
    already dropped the earlier turn's unspoken audio and stopped what was audible.

    Outcomes: ``superseded``; ``over`` when the earlier turn has ended, failed or had its final
    answer completed, cancelled or failed (its answer is a record that cannot be taken back,
    ADR 0053); ``no_registry`` without run bookkeeping.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _continue(earlier: str, turn_id: str) -> str:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return "no_registry"
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            if turn_is_over(conn, earlier):
                return "over"
            registry.mark_turns_superseded(
                [earlier], for_s=SUPERSEDE_WINDOW_S, by_turn=turn_id,
            )
            for run in registry.open_runs():
                if run.turn_id != earlier:
                    continue
                outcome = cancel(run.response_id, "generation", "superseded", turn_id)
                LOGGER.info(
                    "turn %s goes on from %s: response %s -> %s",
                    turn_id, earlier, run.response_id, outcome,
                )
                if outcome == "cancelled":
                    _withdraw_card(conn, earlier, turn_id)
        return "superseded"

    return _continue


def _withdraw_card(conn: sqlite3.Connection, dropped_turn_id: str, turn_id: str) -> None:
    """ADR 0074: reject the card a dropped turn put up, if it still waits.

    Through the one-answer-per-ask primitive, so a button press racing this
    either lands first (and this finds the card answered) or finds it gone.
    """
    confirmation_id = card_requested_by_turn(conn, dropped_turn_id)
    if confirmation_id is None:
        return
    try:
        answer_confirmation_once(
            conn,
            confirmation_id=confirmation_id,
            accepted=False,
            utterance_raw="",
            grammar_rule_id="superseded",
            correlation={"turn_id": turn_id},
        )
    except ConfirmationRevalidationError:
        return  # already answered, or a newer ask replaced it
    LOGGER.info("card %s of dropped turn %s withdrawn", confirmation_id, dropped_turn_id)
