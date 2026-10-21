"""The fixed lines the daemon says with no model and no turn (ADR 0102, 0155, 0179, 0291)."""

from __future__ import annotations

import functools
import logging
import secrets
from typing import TYPE_CHECKING

from jarvis.decision.commentary import COMMENTARY_ATTENTION_CHANNEL
from jarvis.decision.gates import pre_emit_gate
from jarvis.decision.response_run import (
    deterministic_commentary_policy,
    evidence_snapshot_hash,
    start_response_run,
)
from jarvis.runtime.daemon.commentary import commentary_terminalizer, emit_pre_emit_verdict
from jarvis.runtime.plan_run import Hooks
from jarvis.runtime.turn.start import new_turn_id
from jarvis.shared import lang
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import emit_event, open_runtime_event_log
from jarvis.surface.cli import record_pre_emit_token
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.surface import voice_controls

LOGGER = logging.getLogger(__name__)


def say_conversation_line(
    runtime: JarvisRuntime, turn_id: str, reason: str, text: str, phrase: str | None = None,
) -> None:
    """ADR 0102: one fixed line back to 「等我一下」 or a dismissal, no model and no turn.

    ``phrase`` is the exact line when the caller has built it (a reminder, ADR 0179); otherwise
    one wording of ``conversation.<reason>`` in the language of ``text``.

    The words make no turn, so a ``surface.conversation_words`` row is the
    run's trigger. Generation is done when the run opens, so it completes
    before render like a final answer (ADR-0008 D1). Runs on its own thread:
    the capture thread that heard the words must not wait on SQLite.
    """
    factory = runtime.llm_session_factory
    try:
        conn = open_runtime_event_log(runtime.runtime_paths.event_log)
        try:
            trigger = emit_event(
                conn,
                type="surface.conversation_words",
                payload={"turn_id": turn_id, "reason": reason, "transcript": text},
                committed_event_bus=runtime.committed_event_bus,
            )
            if phrase is None:
                said_in = lang.reply_language(
                    text, runtime.config.reply_language,
                )
                phrase = secrets.choice(lang.variants(f"conversation.{reason}", said_in))
            plan = pre_emit_gate(phrase)
            response_id = new_response_id()
            snapshot = factory.snapshot(None)
            run = start_response_run(
                conn,
                turn_id=turn_id,
                trigger_event_uid=trigger.event_uid,
                request_client=factory.create(snapshot, response_id=response_id),
                policy=deterministic_commentary_policy(
                    active_subject_ref=trigger.event_uid,
                    evidence_snapshot_hash=evidence_snapshot_hash(conn),
                    preset_snapshot_hash=snapshot.snapshot_hash,
                ),
                response_id=response_id,
                phase="commentary",
                channel="speech",
                committed_event_bus=runtime.committed_event_bus,
            )
            emit_pre_emit_verdict(conn, plan=plan, turn_id=turn_id)
            run.mark("finalizing")
            commentary_terminalizer(runtime).complete(run.facts, response_hash=plan.response_hash)
            run.mark("completed")
            render_response(
                record_pre_emit_token(plan.response_hash),
                plan,
                conn=conn,
                turn_id=turn_id,
                attention_channel=COMMENTARY_ATTENTION_CHANNEL,
                available_surfaces=frozenset(),
                streaming_enabled=True,
                response_id=run.response_id,
                response_group_id=run.response_group_id,
                phase="commentary",
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a courtesy line never breaks capture
        LOGGER.warning("conversation line failed (%s) turn_id=%s", reason, turn_id, exc_info=True)


def say_job_line(runtime: JarvisRuntime) -> None:
    """ADR 0155: the one fixed line for an interview or offer email, said as a conversation line."""
    say_conversation_line(runtime, new_turn_id(), "job_speak", "")


def plan_hooks(runtime: JarvisRuntime, controls: voice_controls.VoiceControls) -> Hooks:
    """ADR 0291: how the plan watcher tells him: the quiet level, the speakers, the phone.

    It speaks as a reminder does (speech on, no live conversation, not on a call) and pushes only
    while he is away from the Mac. All of it runs on the watcher's thread.
    """
    moment, push, whereabouts = runtime.moment, runtime.push, runtime.whereabouts
    return Hooks(
        quiet=lambda: controls.quiet,
        may_speak=lambda: (
            not controls.speech_muted and not controls.conversation
            and (moment is None or moment.hold() != "call")
        ),
        private_output=lambda: bool(runtime.sound_output()["private"]),
        at_mac=lambda: whereabouts is not None and whereabouts.at_mac(),
        say=functools.partial(_say_plan_line, runtime),
        push=push.send if push is not None and push.enabled else None,
    )


def _say_plan_line(runtime: JarvisRuntime, said: str) -> None:
    """ADR 0291: one line of the plan watcher, said as a conversation line (no model, no turn)."""
    say_conversation_line(runtime, new_turn_id(), "plan", "", phrase=said)


def say_reminder(runtime: JarvisRuntime, said: str) -> None:
    """ADR 0179: one reminder line, said as a conversation line (no model, no turn)."""
    say_conversation_line(runtime, new_turn_id(), "reminder", "", phrase=said)
