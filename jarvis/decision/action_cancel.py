"""ADR-0008 D10 — resolve, request and report one exact action cancellation.

An utterance asking to stop a running action never trusts the LLM's raw
``target_action_id`` on its own. :func:`resolve_cancellable_action` reads
the L2 fold (``SituationPacket.action_admissions``, the non-terminal set
the ``action:`` EntityRegistry kind mirrors) and answers with exactly one
of three results; :func:`build_cancel_action_request` freezes the target's
recorded admission gate uid into a :class:`CancelActionRequest` so the
Pre-action Gate's ``cancel_action`` arm has one truth to match against;
:func:`cancel_outcome_text` turns the L4 ack into the words Allen hears.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``; no sibling
layer imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.decision.packet import SituationPacket
    from jarvis.shared import AuthorizationLease, RiskLevel

CANCEL_ACTION_TOOL_NAME: Final[str] = "cancel_action"
"""The L4 tool name; the gate's admission arm and L3's cancel branch key on it."""

ACTION_ENTITY_PREFIX: Final[str] = "action:"
"""EntityRegistry id prefix of the ``action:`` kind (ADR-0008 D10)."""

CANCEL_ACTION_RISK_LEVEL: Final[RiskLevel] = "L2"
"""Fixed risk of the cancellation command; the target's risk never transfers."""


@dataclass(frozen=True)
class CancelActionRequest:
    """The frozen cancel request, in ADR-0008 D10's exact shape.

    ``authorization_gate_event_uid`` is filled from the L2 admission lookup
    for the target, so a well-formed request always matches its target's
    recorded admission gate; a mismatch can only come from a forged or
    stale request.
    """

    request_id: str
    target_action_id: str
    caller_principal: CallerPrincipal
    risk_level: RiskLevel
    reason: str
    requested_by_turn_id: str | None
    source_event_uid: str
    authorization_gate_event_uid: str | None
    authorization_lease: AuthorizationLease | None = None

    def as_action_payload(self) -> dict[str, Any]:
        """Flatten onto ``ActionRequest.payload`` for the gate arm to read."""
        return {
            "request_id": self.request_id,
            "target_action_id": self.target_action_id,
            "caller_principal": self.caller_principal.value,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "requested_by_turn_id": self.requested_by_turn_id,
            "source_event_uid": self.source_event_uid,
            "authorization_gate_event_uid": self.authorization_gate_event_uid,
            "authorization_lease": self.authorization_lease,
        }


CancelResolutionKind = Literal["resolved", "ambiguous", "none"]


@dataclass(frozen=True)
class CancelResolution:
    """Outcome of :func:`resolve_cancellable_action`.

    ``resolved`` carries ``action_id``; ``ambiguous`` carries the open
    ``candidates``; ``none`` carries neither.
    """

    kind: CancelResolutionKind
    action_id: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def action_ref(self) -> str | None:
        """The ``action:<id>`` entity ref, or None unless resolved."""
        return None if self.action_id is None else f"{ACTION_ENTITY_PREFIX}{self.action_id}"


def resolve_cancellable_action(
    packet: SituationPacket,
    *,
    requested: object = None,
) -> CancelResolution:
    """Resolve the one non-terminal action a cancel utterance names.

    ``requested`` is the LLM's raw ``target_action_id`` (or None). It is
    accepted only when it names a current non-terminal action; otherwise
    the open set decides: exactly one → resolved, several → ambiguous,
    none → none.
    """
    open_ids = tuple(packet.action_admissions.by_action_id)
    if isinstance(requested, str) and requested:
        wanted = requested.removeprefix(ACTION_ENTITY_PREFIX)
        if wanted in open_ids:
            return CancelResolution(kind="resolved", action_id=wanted)
    if len(open_ids) == 1:
        return CancelResolution(kind="resolved", action_id=open_ids[0])
    if not open_ids:
        return CancelResolution(kind="none")
    return CancelResolution(kind="ambiguous", candidates=open_ids)


def cancel_resolution_answer(resolution: CancelResolution) -> str:
    """The spoken answer for an unresolved cancel: a question, or a no-op."""
    if resolution.kind == "ambiguous":
        listed = "、".join(resolution.candidates)
        return f"哪一个？现在有 {len(resolution.candidates)} 个动作在跑：{listed}。"  # noqa: RUF001 — Chinese punctuation.
    return "现在没有正在跑的动作，没有可以取消的。"  # noqa: RUF001 — Chinese punctuation.


def build_cancel_action_request(
    packet: SituationPacket,
    *,
    request_id: str,
    target_action_id: str,
    reason: str,
    requested_by_turn_id: str | None,
) -> CancelActionRequest:
    """Freeze the target's recorded admission gate into a cancel request."""
    admission = packet.action_admissions.get(target_action_id)
    return CancelActionRequest(
        request_id=request_id,
        target_action_id=target_action_id,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level=CANCEL_ACTION_RISK_LEVEL,
        reason=reason,
        requested_by_turn_id=requested_by_turn_id,
        source_event_uid=packet.trigger_event.event_uid,
        authorization_gate_event_uid=None if admission is None else admission.admission_gate_uid,
    )


def cancel_outcome_text(payload: Mapping[str, Any]) -> str:
    """Words for one serialized ``CancelOutcome`` (the L4 ``cancel_action`` ack)."""
    status = payload.get("status")
    if status == "accepted":
        return "已经停下了。"
    if status == "already_terminal":
        return "它已经结束了，不需要取消。"  # noqa: RUF001 — Chinese punctuation.
    if status == "unsupported":
        return "这个动作不支持中途取消，它会继续跑。"  # noqa: RUF001 — Chinese punctuation.
    if payload.get("reason") == "no_live_context":
        return "还没真正开始，没能停下。"  # noqa: RUF001 — Chinese punctuation.
    return "没能确认它停下，它可能还在跑。"  # noqa: RUF001 — Chinese punctuation.


def render_cancel_result_for_llm(payload: Mapping[str, Any]) -> str:
    """The tool-result JSON the LLM sees: the ack plus its spoken meaning."""
    return json.dumps({**payload, "message": cancel_outcome_text(payload)}, ensure_ascii=False)


__all__ = [
    "ACTION_ENTITY_PREFIX",
    "CANCEL_ACTION_RISK_LEVEL",
    "CANCEL_ACTION_TOOL_NAME",
    "CancelActionRequest",
    "CancelResolution",
    "CancelResolutionKind",
    "build_cancel_action_request",
    "cancel_outcome_text",
    "cancel_resolution_answer",
    "render_cancel_result_for_llm",
    "resolve_cancellable_action",
]
