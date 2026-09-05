"""Attention Policy default channel — a direct answer to the user is spoken.

docs/goals/speak-ordinary-answers.md: an ordinary user utterance with no
verified Postcondition, no Limitation and no ``worker.reported`` trigger
routes ``voice_notify``. The ``worker.reported`` branches are unchanged
(``silent_log`` without evidence, ``queue_review`` on
``needs_human_review``), and the reconciliation terminals the ADR-0009 D4
supervisor sweep drives stay ``queue_review`` so a 3am system turn never
speaks (ADR-0002 Limitation-routing amendment, B-0005).

Drives :func:`jarvis.decision.gates.attention_policy` on a packet folded
from an empty event log — no LLM, no daemon.
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING

from jarvis.decision.gates import attention_policy
from jarvis.decision.packet import assemble_packet
from jarvis.shared import Event
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


def _trigger(event_type: str) -> Event:
    return Event(
        event_uid=f"uid-{event_type}",
        type=event_type,
        schema_version=1,
        ts_epoch_ms=0,
        payload={},
        source_event_id=None,
        correlation=None,
    )


def test_attention_policy_speaks_ordinary_answers_only(tmp_path: Path) -> None:
    """Plain utterance -> voice_notify; worker/system triggers keep their channels."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        packet = assemble_packet(_trigger("surface.user_intent"), conn)
    evidence = packet.task_ledger_snapshot.claim_evidence

    assert attention_policy(packet, evidence) == "voice_notify"
    utterance = dataclasses.replace(packet, trigger_event=_trigger("utterance.received"))
    assert attention_policy(utterance, evidence) == "voice_notify"

    worker = dataclasses.replace(packet, trigger_event=_trigger("worker.reported"))
    assert attention_policy(worker, evidence) == "silent_log"
    assert attention_policy(worker, evidence, needs_human_review=True) == "queue_review"
    assert attention_policy(worker, evidence, limitation_emitted=True) == "voice_notify"

    for terminal in ("action.timeout_assumed", "action.failed", "action.cancelled"):
        system = dataclasses.replace(packet, trigger_event=_trigger(terminal))
        assert attention_policy(system, evidence, limitation_emitted=True) == "queue_review"
        # A terminal without action_id emits no claim; the channel must not depend on it.
        assert attention_policy(system, evidence) == "queue_review"
