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
   returns a final plan and BEFORE handing the plan to ``write_output``.
3. ``write_output`` — render the document side of a channel-split
   ``ResponsePlan`` to stdout. Refuses if the token in
   :class:`SurfaceState` does not match
   ``response_plan.response_hash`` (canary H3 runtime check — "the
   final output must match the latest valid gate token").
4. ``parse_response_channels`` — verbatim port of legacy
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
import sys
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Protocol

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3

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
    """Raised when ``write_output`` is called without a matching Pre-emit token.

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
    :func:`write_output` once the plan has been rendered.

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


def emit_surface_user_intent(
    conn: sqlite3.Connection,
    *,
    transcript: str,
    turn_id: str,
    channel: str = "cli_stdin",
    language: str = "zh-CN",
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
        },
        correlation={"turn_id": turn_id},
    )


def record_pre_emit_token(
    state: SurfaceState,  # noqa: ARG001 — kept for API symmetry; SurfaceState is immutable so this is effectively a constructor wrapper that documents the lifecycle.
    response_hash: str,
) -> SurfaceState:
    """Record the Pre-emit Gate token on the surface state.

    Returns a fresh frozen :class:`SurfaceState` (no in-place
    mutation). The composition root calls this with the
    ``ResponsePlan.response_hash`` returned by
    :func:`jarvis.decision.decide` immediately before passing the
    plan to :func:`write_output`.

    The ``state`` argument is accepted for API symmetry with the
    ``state -> state`` style used across :class:`SurfaceState`
    mutators; the previous token (if any) is intentionally discarded
    because a new approved plan supersedes whatever was on the
    surface.
    """
    return SurfaceState(last_gate_response_hash=response_hash)


def write_output(
    state: SurfaceState,
    response_plan: ResponsePlanLike,
    *,
    stream: IO[str] | None = None,
) -> SurfaceState:
    """Render an approved ``ResponsePlan`` to the user.

    Behavior per ADR § Gate contracts (Pre-emit token) and
    spec §18 (voice / document split):

    1. Refuse if ``state.last_gate_response_hash != response_plan.response_hash``
       (raise :class:`PreEmitTokenError`). Refuse also when no token has been
       recorded (``state.last_gate_response_hash is None``).
    2. Parse ``response_plan.text`` via :func:`parse_response_channels`.
    3. Write the document side of the channel split to ``stream``
       (default ``sys.stdout``). When the document channel is empty,
       fall back to the voice channel so a voice-only response still
       surfaces. Day-1 voice would route to TTS in Stage 2; the
       surface renders the document side to the CLI.
    4. Return a fresh :class:`SurfaceState` with the token consumed
       (``last_gate_response_hash=None``) so a stale token cannot be
       reused for a second emission.

    Args:
        state: Current surface state (must hold a matching token).
        response_plan: The approved plan from :func:`jarvis.decision.decide`.
        stream: Output stream; default ``sys.stdout``.

    Returns:
        Updated frozen :class:`SurfaceState` with the token cleared.

    Raises:
        PreEmitTokenError: Token missing or mismatched.
    """
    if state.last_gate_response_hash is None:
        msg = (
            "write_output: no Pre-emit Gate token recorded on surface state; "
            "composition root must call record_pre_emit_token(...) before write_output(...)."
        )
        raise PreEmitTokenError(msg)
    if state.last_gate_response_hash != response_plan.response_hash:
        msg = (
            f"write_output: Pre-emit token mismatch — recorded "
            f"{state.last_gate_response_hash!r} but plan carries "
            f"{response_plan.response_hash!r}."
        )
        raise PreEmitTokenError(msg)

    target_stream: IO[str] = sys.stdout if stream is None else stream
    channels = parse_response_channels(response_plan.text)

    rendered = channels.document or channels.voice
    target_stream.write(rendered)
    if not rendered.endswith("\n"):
        target_stream.write("\n")
    target_stream.flush()

    return SurfaceState(last_gate_response_hash=None)


__all__ = [
    "PreEmitTokenError",
    "ResponseChannels",
    "ResponsePlanLike",
    "SurfaceState",
    "emit_surface_user_intent",
    "parse_response_channels",
    "record_pre_emit_token",
    "write_output",
]
