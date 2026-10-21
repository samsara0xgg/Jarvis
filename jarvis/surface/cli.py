"""L5 Surface adapter — CLI input capture + channel-split output rendering.

Per spec.html §3.4 / §18 (voice + document channels) and ADR 0001
§ Stub strategy L5 row + § Gate contracts (Pre-emit token check) +
§ Six-layer boundary contract.

Day-1 responsibilities:

1. ``emit_surface_user_intent`` — canonical L5 -> L2 event emission for a
   raw stdin transcript. This is the entry point for one conversational
   turn. Per spec §3.4.1 trigger taxonomy, the CLI surface emits
   ``surface.user_intent``; the legacy voice-surface event type stays
   reserved in the registry (see ``jarvis.state.event_log``).
2. ``record_pre_emit_token`` — the surface holds the latest
   ``ResponsePlan.response_hash`` the Pre-emit Gate stamped. The runtime
   composition root calls this after :func:`jarvis.decision.decide`
   returns a final plan and BEFORE handing the plan to
   :func:`jarvis.surface.cli_render.render_response`, which refuses when the
   token in :class:`SurfaceState` does not match
   ``response_plan.response_hash`` (canary H3 runtime check — "the
   final output must match the latest valid gate token").
3. ``parse_response_channels`` — verbatim port of legacy
   ``jarvis-legacy/core/response_channels.py`` (ADR § Reference sources).
   The implementation of ``parse_response_channels`` is byte-equivalent
   to the legacy 52-LOC module.

Layer rules (``.importlinter`` + canary H13): this module imports stdlib
plus ``jarvis.shared`` and ``jarvis.state.event_log``. It MUST NOT import
``jarvis.decision`` / ``jarvis.execution`` / ``jarvis.deployment`` /
``jarvis.runtime`` / ``jarvis.cli`` / ``jarvis.constitution``. The
``ResponsePlan`` type is consumed via a structural :class:`ResponsePlanLike`
Protocol so the surface never imports the L3 sibling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

    from jarvis.shared import Event


# --- Verbatim port: legacy `core/response_channels.py` ---------------------
#
# The block below is byte-equivalent in implementation to
# /Users/alllllenshi/Projects/jarvis-legacy/core/response_channels.py.
# Only structural decoration (module-private dataclass, module placement)
# differs; the regex pattern, parsing loop, dataclass field set, and
# return shapes are identical. No Legacy-bypass annotation needed.

_CHANNEL_RE = re.compile(
    r"<(?P<tag>voice|document)>\s*(?P<body>.*?)\s*</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ResponseChannels:
    """Parsed assistant response channels (verbatim from legacy)."""

    raw: str
    voice: str
    document: str
    has_channels: bool


def parse_response_channels(text: str) -> ResponseChannels:
    """Parse ``<voice>`` and ``<document>`` sections from an assistant response.

    Verbatim from ``jarvis-legacy/core/response_channels.py`` per ADR
    § Reference sources. If the response is not channelized, return it
    as both voice and document so legacy callers keep working. When
    only one channel is present, the missing channel is an empty
    string.
    """
    raw = text or ""
    values: dict[str, str] = {}
    for match in _CHANNEL_RE.finditer(raw):
        tag = match.group("tag").lower()
        if tag not in values:
            values[tag] = match.group("body").strip()

    if not values:
        stripped = raw.strip()
        return ResponseChannels(
            raw=raw,
            voice=stripped,
            document=stripped,
            has_channels=False,
        )

    return ResponseChannels(
        raw=raw,
        voice=values.get("voice", ""),
        document=values.get("document", ""),
        has_channels=True,
    )


# --- Pre-emit token surface state ------------------------------------------


class PreEmitTokenError(RuntimeError):
    """Raised when ``render_response`` is called without a matching Pre-emit token.

    Canary H3 runtime check — surfaces the violation as a hard runtime
    failure so a bug in the composition root (forgetting to record the
    token, or writing a different plan than the one the gate evaluated)
    cannot silently leak unverified output to the user.
    """


@dataclass(frozen=True)
class SurfaceState:
    """Immutable surface state carrying the latest Pre-emit Gate token.

    Held by the composition root; refreshed via
    :func:`record_pre_emit_token` after the Pre-emit Gate stamps a
    ``ResponsePlan.response_hash``, and cleared (set to ``None``) by
    :func:`jarvis.surface.cli_render.render_response` once the plan has been rendered.

    Attributes:
        last_gate_response_hash: The most recent
            ``ResponsePlan.response_hash`` value the gate emitted, or
            ``None`` if no plan is currently approved for emission.
    """

    last_gate_response_hash: str | None


class ResponsePlanLike(Protocol):
    """Structural view of :class:`jarvis.decision.ResponsePlan` (L5 cannot import L3)."""

    @property
    def text(self) -> str:
        """Final response text the surface should render."""
        ...

    @property
    def response_hash(self) -> str:
        """SHA-256 hex of ``text`` stamped on ``gate.evaluated(pre_emit)``."""
        ...

    @property
    def required_gate_mode(self) -> str:
        """Spec §3.4.13 required gate mode (``"sentence"`` / ``"full_text"`` / ``"structured"``).

        Drives ADR-0003 Step 2 chunked-emission policy in
        :func:`jarvis.surface.cli_render.render_response`: ``"sentence"``
        splits the response into sentence-sized chunks before emit;
        ``"full_text"`` / ``"structured"`` emit a single chunk with the
        full text (no speculative splitting).
        """
        ...


# --- Public API -------------------------------------------------------------


def emit_surface_user_intent(  # noqa: PLR0913 — one keyword per payload field.
    conn: sqlite3.Connection,
    *,
    transcript: str,
    turn_id: str,
    channel: str = "cli_stdin",
    language: str = "zh-CN",
    attachments: Sequence[str] = (),
    ingestion_node: str = "mac",
    about: Mapping[str, Any] | None = None,
) -> Event:
    """Emit the canonical ``surface.user_intent`` event for a CLI utterance.

    Per spec §3.4.1 trigger taxonomy: the CLI surface emits
    ``surface.user_intent`` as the L5 -> L3 trigger for one conversation
    turn — the composition root then feeds the returned :class:`Event`
    into :func:`jarvis.decision.decide` as the first trigger. The
    legacy voice-surface event type stays reserved in the registry
    (Day-2 ADR-0002 Step 2 rename).

    Args:
        conn: Open Event Log connection.
        transcript: Raw user-typed text (stripped or unstripped — the
            event payload carries it verbatim).
        turn_id: Caller-generated turn identifier (composition root
            mints ``"T" + uuid.uuid4().hex[:8]``).
        channel: Source channel label; Day-1 default ``"cli_stdin"``.
        language: BCP-47 language tag; Day-1 default ``"zh-CN"``.
        attachments: Ids of stored files a phone sent with the words (ADR 0211); the key is
            written only when there are some.
        ingestion_node: The paired device the words came from (ADR 0212), else this Mac.
        about: The item that device's phone has open, already checked by
            :func:`jarvis.shared.about.clean_about` (ADR 0214); the key is written only when given.

    Returns:
        The frozen Event row appended to the log (carrying the
        ``event_uid`` used as ``source_event_id`` by downstream events).
    """
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": channel,
            "language": language,
            **({"attachments": list(attachments)} if attachments else {}),
            **({"about": dict(about)} if about else {}),
        },
        correlation={"turn_id": turn_id},
        ingestion_node=ingestion_node,
    )


def record_pre_emit_token(response_hash: str) -> SurfaceState:
    """Record the Pre-emit Gate token on a fresh surface state.

    Returns a new frozen :class:`SurfaceState`. The composition root calls
    this with the ``ResponsePlan.response_hash`` returned by
    :func:`jarvis.decision.decide` immediately before passing the
    plan to :func:`jarvis.surface.cli_render.render_response`.

    A new approved plan supersedes whatever token was on the surface, so
    there is no previous state to carry over.
    """
    return SurfaceState(last_gate_response_hash=response_hash)


__all__ = [
    "PreEmitTokenError",
    "ResponseChannels",
    "ResponsePlanLike",
    "SurfaceState",
    "emit_surface_user_intent",
    "parse_response_channels",
    "record_pre_emit_token",
]
