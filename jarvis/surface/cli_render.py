r"""L5 channel-aware multi-surface render — Step 18 of ADR-0002.

Per ADR-0002 § Attention channel → physical surface mapping
(lines 1017-1044) and § build-order Step 18 row (line 1876).

The Day-1 surface render was a single ``write_output`` call into a
stdout-like stream. Day-2 the same approved :class:`ResponsePlan`
must be routed to ALL physical surfaces declared for the L3
Attention Policy's chosen channel:

- ``cli_stdout`` -> write the document text to the supplied stream
  (typically stdout when attached as a TTY).
- ``say`` -> :func:`jarvis.surface.notify.deliver_voice` (verbatim
  voice text).
- ``say_bell`` -> ``deliver_voice`` with a leading BEL char (``\\a``)
  prepended to mark the interrupt-class tone. Same TTS voice as
  ``say``; the differentiator is the leading marker.
- ``osascript_banner`` -> :func:`jarvis.surface.notify.deliver_banner`
  with ``title="Jarvis"`` + ``body=document_text``.
- ``osascript_banner_title_only`` -> ``deliver_banner`` with
  ``title=document_text`` + ``body=""`` (badge surrogate; Day-2 has
  no Inherent panel).

Voice / document split convention (Day-2 minimum):

    The L3 ResponsePlan's ``text`` may already carry
    ``<voice>...</voice><document>...</document>`` markup parsed by
    :func:`jarvis.surface.cli.parse_response_channels`. When the
    response is one undifferentiated block, both channels receive
    the same (stripped) text. When only one tag is present, the
    other channel is empty and the missing-side surfaces silently
    skip (deliver_voice/deliver_banner already short-circuit on
    empty input).

For each turn the function emits exactly one
``surface.response_emitted`` event whose payload carries:

- ``turn_id`` (required)
- ``text`` (full response text; preserved Day-1 contract)
- ``voice_text`` / ``document_text`` (the channel-split slices)
- ``delivered_via`` — list of PHYSICAL surface names actually
  written this turn (e.g. ``["voice", "banner", "stdout"]``;
  subset if partial — stdout dropped when the parent process has
  detached and the supplied stream is not a TTY).
- ``attention_channel`` — the L3 logical channel (one of the 9
  channels in
  :data:`jarvis.surface.notify.ATTENTION_CHANNEL_TO_SURFACES`).
- ``response_hash`` — SHA-256 hex of the ResponsePlan text, taken
  from ``ResponsePlan.response_hash`` (the Pre-emit Gate token).

Pre-emit token enforcement: the Day-1 canary H3 (``surface.cli``
runtime check) is preserved — :func:`render_response` performs
the same token check as :func:`jarvis.surface.cli.write_output`
and raises :class:`jarvis.surface.cli.PreEmitTokenError` on
mismatch / absence. The returned :class:`SurfaceState` has the
token cleared.

Layer rules: this module sits in L5 alongside
:mod:`jarvis.surface.cli` and :mod:`jarvis.surface.notify`. It
MUST NOT import L3 / L4 / L6 / L1 siblings. The
:class:`ResponsePlanLike` Protocol is re-used from
``jarvis.surface.cli`` so we never name ``jarvis.decision``.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from typing import IO, TYPE_CHECKING

from jarvis.shared.realtime import (
    LegacyPresentationBinding,
    stable_legacy_presentation_binding,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import emit_event
from jarvis.surface.cli import (
    PreEmitTokenError,
    ResponsePlanLike,
    SurfaceState,
    parse_response_channels,
)
from jarvis.surface.notify import (
    ATTENTION_CHANNEL_TO_SURFACES,
    deliver_banner,
    deliver_voice,
)
from jarvis.surface.sentence_splitter import split_into_sentences

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event


LOGGER = logging.getLogger("jarvis.surface.cli_render")


# --- Physical surface names (what lands in ``delivered_via``) --------------
#
# The L5 physical surface vocabulary is intentionally narrower than the L3
# mapping (e.g. ``say`` and ``say_bell`` both yield ``"voice"``); the L3
# logical channel is captured separately in ``attention_channel``.
_PHYSICAL_VOICE = "voice"
_PHYSICAL_BANNER = "banner"
_PHYSICAL_STDOUT = "stdout"

# L3-channel-label -> physical surface name. Keys are the entries that
# appear inside ATTENTION_CHANNEL_TO_SURFACES tuples.
_L3_SURFACE_TO_PHYSICAL: dict[str, str] = {
    "say": _PHYSICAL_VOICE,
    "say_bell": _PHYSICAL_VOICE,
    "osascript_banner": _PHYSICAL_BANNER,
    "osascript_banner_title_only": _PHYSICAL_BANNER,
    "cli_stdout": _PHYSICAL_STDOUT,
}

# Default Day-2 banner title for ``osascript_banner`` (the document-text
# banner). ``osascript_banner_title_only`` uses the document text itself
# as the title.
_DEFAULT_BANNER_TITLE = "Jarvis"

# Leading marker prepended to the voice text for the ``say_bell``
# variant — the Day-2 minimum interrupt cue. The BEL char (``\\a``) is
# the conservative pick: it survives copy/paste, it's a single byte, and
# downstream TTS treats it as silence on macOS.
_BELL_MARKER = "\a"

# Default Day-1 physical surfaces fired by the CLI path. The new Inherent
# daemon (ADR-0003) overrides this with frozenset() so the daemon's
# physical delivery channel (WebSocket push from Step 6 InherentBroadcaster)
# is the sole observable side-effect of a turn. Match the literal surface
# IDs in ATTENTION_CHANNEL_TO_SURFACES' codomain (jarvis/surface/notify.py).
_CLI_DEFAULT_SURFACES: frozenset[str] = frozenset(
    {
        "say",
        "say_bell",
        "osascript_banner",
        "osascript_banner_title_only",
        "cli_stdout",
    }
)


def _emit_response_open(  # noqa: PLR0913 - explicit committed event shape
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    query: str,
    response_plan: ResponsePlanLike,
    attention_channel: str,
    binding: LegacyPresentationBinding,
    channel: str,
) -> None:
    """Emit the ADR-0003 Step 2 ``surface.response_open`` event.

    Single emission per turn. Payload carries ``turn_id``, ``query``
    (the user transcript that triggered the turn — empty string allowed),
    ``kind`` (always ``"text"`` for A1), ``required_gate_mode``
    (ADR-0005 §7: L5 TTS consumers read this off the open header to
    route between sentence-streaming and full-text TTS playback per
    spec §3.6.6 — the same plan field already drives chunk-splitting
    in :func:`_emit_response_chunks`), and ``attention_channel``.

    ``attention_channel`` (ADR-0009 §4 registry amendment, D4) is the
    L3 verdict this render is executing. It already rides the terminal
    ``surface.response_emitted`` audit event, but the streaming
    consumers (``_tts_watcher`` / the WS broadcaster) key off the OPEN
    header — they must know the channel BEFORE the first chunk arrives
    to drop a turn that must not speak. The label is written verbatim,
    including an unknown value: the audit trail records the L3 anomaly
    rather than the ``silent_log`` surface fallback applied downstream.
    """
    emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": turn_id,
            "query": query,
            "kind": "text",
            "required_gate_mode": response_plan.required_gate_mode,
            "attention_channel": attention_channel,
            "response_id": binding.response_id,
            "response_group_id": binding.response_group_id,
            "phase": "final",
            "channel": channel,
        },
        correlation={"turn_id": turn_id},
    )


def _emit_response_chunks(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    response_plan: ResponsePlanLike,
    binding: LegacyPresentationBinding,
    channel: str,
) -> None:
    """Emit one or more ADR-0003 Step 2 ``surface.response_chunk`` events.

    Chunking gated by ``response_plan.required_gate_mode`` per spec §3.4.13:

    - ``"sentence"`` -> :func:`split_into_sentences` on the plan text;
      one event per non-empty chunk in source order.
    - any other value (``"full_text"`` / ``"structured"``) -> a single
      event carrying the full plan text verbatim (no speculative splitting).
    """
    if response_plan.required_gate_mode == "sentence":
        chunks = split_into_sentences(response_plan.text)
    else:
        chunks = [response_plan.text]

    for sequence, chunk_text in enumerate(chunks):
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={
                "turn_id": turn_id,
                "text": chunk_text,
                "response_id": binding.response_id,
                "response_group_id": binding.response_group_id,
                "sequence": sequence,
                "phase": "final",
                "channel": channel,
                "segment_hash": hashlib.sha256(chunk_text.encode()).hexdigest(),
            },
            correlation={"turn_id": turn_id},
        )
        record_realtime_trace(
            "surface_segment_committed",
            turn_id=turn_id,
            segment_sequence=sequence,
            gate_mode=response_plan.required_gate_mode,
            text_characters=len(chunk_text),
        )


def render_response(  # noqa: C901, PLR0912, PLR0913, PLR0915 — closed dispatch over 5 fixed surfaces; argument set is the L5 boundary contract and intentionally explicit.
    state: SurfaceState,
    response_plan: ResponsePlanLike,
    *,
    conn: sqlite3.Connection,
    turn_id: str,
    attention_channel: str,
    stream: IO[str] | None = None,
    available_surfaces: frozenset[str] | None = None,
    streaming_enabled: bool = False,
    query: str = "",
    response_id: str | None = None,
    response_group_id: str | None = None,
    delivery_terminal_only: bool = False,
) -> tuple[SurfaceState, Event]:
    """Render an approved ResponsePlan across all surfaces for ``attention_channel``.

    Per ADR-0002 Step 18. The renderer:

    1. Enforces the Pre-emit Gate token (canary H3 contract — same
       check as :func:`jarvis.surface.cli.write_output`).
    2. Parses ``response_plan.text`` into voice / document channels
       via :func:`parse_response_channels`. When the text is not
       channelized, both slots receive the (stripped) full text.
    3. Looks up ``ATTENTION_CHANNEL_TO_SURFACES[attention_channel]``;
       unknown channels fall back to ``silent_log`` (no surface call)
       AND log a warning. The unknown channel still appears in the
       emitted event's ``attention_channel`` so the audit trail
       records the L3 anomaly.
    4. Dispatches to ``deliver_voice`` / ``deliver_banner`` / ``print``
       per the channel's surface tuple. The supplied ``stream`` (or
       ``sys.stdout`` by default) is used for the ``cli_stdout``
       surface; when the stream is not a TTY the stdout write is
       skipped and the ``stdout`` entry is omitted from
       ``delivered_via`` (partial delivery on a detached parent).
       Before dispatching any individual surface, the loop applies the
       ``available_surfaces`` filter (ADR-0003 D3): a surface ID not
       in the effective set is skipped silently so the audit event
       still emits with the remaining (possibly empty) ``delivered_via``.
    5. When ``streaming_enabled=True`` (ADR-0003 Step 2, A1 daemon path),
       emits the 3-event Inherent taxonomy before the audit event:
       one ``surface.response_open`` (carrying ``query`` + ``kind="text"``
       + ``attention_channel`` — ADR-0009 D4: the streaming consumers
       read the channel off the open header so a non-speaking turn is
       dropped before its first chunk),
       then one-or-more ``surface.response_chunk`` events gated by
       ``response_plan.required_gate_mode`` (``"sentence"`` -> one chunk
       per :func:`split_into_sentences` output; ``"full_text"`` /
       ``"structured"`` / anything else -> a single chunk carrying the
       full plan text). Default ``streaming_enabled=False`` (CLI path)
       preserves Step-1 single-emit semantics byte-for-byte. All three
       Inherent event types share ``correlation={"turn_id": turn_id}``
       with the audit event below.
    6. Emits exactly one ``surface.response_emitted`` event carrying
       ``text`` + ``voice_text`` + ``document_text`` +
       ``delivered_via`` (PHYSICAL surface list) +
       ``attention_channel`` (L3 logical channel) + ``response_hash``.

    Args:
        state: Current :class:`SurfaceState`. MUST hold a token
            matching ``response_plan.response_hash``.
        response_plan: Approved plan from
            :func:`jarvis.decision.decide` (consumed structurally via
            :class:`ResponsePlanLike`).
        conn: Open Event Log connection (L2) — the renderer is the
            sole surface site that emits ``surface.response_emitted``.
        turn_id: Composition-root turn id; lands on the event payload
            + correlation.
        attention_channel: L3 Attention Policy verdict (one of the 9
            channels). Unknown values fall back to ``silent_log``.
        stream: Optional override for the ``cli_stdout`` write. The
            TTY check only runs when ``stream is None`` (default
            ``sys.stdout``); explicit streams are always written to
            (tests pass an ``io.StringIO``).
        available_surfaces: When provided, only surfaces whose ID appears
            in this set are fired. Default ``None`` resolves to
            ``_CLI_DEFAULT_SURFACES`` (the full 5-surface CLI set, day-1
            behaviour). Daemon callers pass ``frozenset()`` to suppress
            all physical surfaces; the audit ``surface.response_emitted``
            event STILL emits with ``delivered_via=[]``.
        streaming_enabled: When ``True`` (ADR-0003 Step 2 daemon path),
            emit the 3-event Inherent taxonomy (``surface.response_open``
            + ``surface.response_chunk`` * N) before the audit event.
            Default ``False`` (CLI path) emits only the audit event.
        query: User transcript that triggered this turn. Lands on the
            ``surface.response_open`` payload; only consulted when
            ``streaming_enabled=True``. Empty string allowed.
        response_id: ADR-0008 Wave 4A — the L3 ResponseRun's own id.
            When both this and ``response_group_id`` are supplied the
            three ``surface.response_*`` events carry them verbatim, so
            L5 delivery names the same response as ``response.started``.
            ``None`` (every legacy caller) keeps the uuid5 derivation.
        response_group_id: The L3 ResponseRun's group id; see
            ``response_id``. Both must be supplied together.
        delivery_terminal_only: ADR-0008 Step 8 — the run already exposed
            its ``surface.response_open`` and permitted chunks while
            streaming, so this call performs physical delivery and emits
            only ``surface.response_emitted`` for that same ``response_id``;
            never a second open or a duplicate chunk. Requires
            ``response_id``/``response_group_id``.

    Returns:
        ``(next_state, event)`` — the :class:`SurfaceState` with the
        token cleared (``last_gate_response_hash=None``) and the
        :class:`Event` row appended to the log.

    Raises:
        PreEmitTokenError: When the surface state lacks the matching
            Pre-emit Gate token (canary H3 runtime check).
    """
    # 1. Pre-emit token check (canary H3 — same contract as write_output).
    if state.last_gate_response_hash is None:
        msg = (
            "render_response: no Pre-emit Gate token recorded on surface state; "
            "composition root must call record_pre_emit_token(...) before render_response(...)."
        )
        raise PreEmitTokenError(msg)
    if state.last_gate_response_hash != response_plan.response_hash:
        msg = (
            f"render_response: Pre-emit token mismatch — recorded "
            f"{state.last_gate_response_hash!r} but plan carries "
            f"{response_plan.response_hash!r}."
        )
        raise PreEmitTokenError(msg)

    # 2. Channel split.
    channels = parse_response_channels(response_plan.text)
    voice_text = channels.voice
    document_text = channels.document
    presentation_channel = (
        "both"
        if voice_text.strip() and document_text.strip()
        else "speech"
        if voice_text.strip()
        else "document"
    )

    # 3. Surface lookup. Unknown channel -> silent_log fallback (no
    #    surface call) plus a warning. The unknown label still lands
    #    in the emitted event's attention_channel.
    surfaces = ATTENTION_CHANNEL_TO_SURFACES.get(attention_channel)
    if surfaces is None:
        LOGGER.warning(
            "render_response: unknown attention_channel %r — falling back to silent_log",
            attention_channel,
        )
        surfaces = ATTENTION_CHANNEL_TO_SURFACES["silent_log"]

    # 4. Dispatch. Track delivered_via in declaration order (per the
    #    ADR table) but de-dup since ``say`` + ``say_bell`` both fold
    #    into a single ``"voice"`` physical surface. The ADR-0003 D3
    #    filter only gates physical fires below — the audit event STILL
    #    emits because the L5 channel pick already happened (§3.6.4).
    effective_surfaces = (
        available_surfaces if available_surfaces is not None else _CLI_DEFAULT_SURFACES
    )
    delivered_via: list[str] = []

    def _record_physical(name: str) -> None:
        if name not in delivered_via:
            delivered_via.append(name)

    target_stream: IO[str] = sys.stdout if stream is None else stream
    # TTY check only applies when the caller relies on the default
    # stdout — explicit streams (StringIO in tests, captured pipes in
    # programmatic callers) are always considered attached.
    stdout_attached: bool = (
        True if stream is not None else bool(getattr(sys.stdout, "isatty", lambda: False)())
    )

    for surface in surfaces:
        if surface not in effective_surfaces:
            continue
        if surface == "say":
            deliver_voice(voice_text)
            if voice_text.strip():
                _record_physical(_PHYSICAL_VOICE)
        elif surface == "say_bell":
            bell_text = f"{_BELL_MARKER}{voice_text}" if voice_text.strip() else ""
            deliver_voice(bell_text)
            if voice_text.strip():
                _record_physical(_PHYSICAL_VOICE)
        elif surface == "osascript_banner":
            deliver_banner(title=_DEFAULT_BANNER_TITLE, body=document_text)
            if document_text.strip():
                _record_physical(_PHYSICAL_BANNER)
        elif surface == "osascript_banner_title_only":
            deliver_banner(title=document_text, body="")
            if document_text.strip():
                _record_physical(_PHYSICAL_BANNER)
        elif surface == "cli_stdout":
            if stdout_attached:
                rendered = document_text or voice_text
                target_stream.write(rendered)
                if not rendered.endswith("\n"):
                    target_stream.write("\n")
                target_stream.flush()
                if rendered:
                    _record_physical(_PHYSICAL_STDOUT)
        else:  # pragma: no cover — defensive guard; the mapping is closed.
            LOGGER.warning(
                "render_response: unknown surface %r for channel %r — skipped",
                surface,
                attention_channel,
            )

    # 5. ADR-0003 Step 2 Inherent taxonomy (daemon path only). Order is
    #    open -> chunk(s) -> emitted so a downstream watcher with a single
    #    cursor over the three types sees the sequence per turn.
    binding: LegacyPresentationBinding | None = None
    if streaming_enabled:
        # ADR-0008 Wave 4A: when L3 opened an explicit ResponseRun it owns
        # the identity, and these L5 events must name the SAME response as
        # `response.started` (ADR-0014's never-reused rule). With no run —
        # every legacy caller — this falls back to the uuid5 derivation,
        # byte-for-byte as before.
        binding = (
            LegacyPresentationBinding(
                response_id=response_id,
                response_group_id=response_group_id,
            )
            if response_id is not None and response_group_id is not None
            else stable_legacy_presentation_binding(
                turn_id=turn_id,
                response_hash=response_plan.response_hash,
            )
        )
        if delivery_terminal_only and response_id is None:
            msg = "render_response: delivery_terminal_only requires the run's response_id"
            raise ValueError(msg)
        if not delivery_terminal_only:
            _emit_response_open(
                conn,
                turn_id=turn_id,
                query=query,
                response_plan=response_plan,
                attention_channel=attention_channel,
                binding=binding,
                channel=presentation_channel,
            )
            _emit_response_chunks(
                conn,
                turn_id=turn_id,
                response_plan=response_plan,
                binding=binding,
                channel=presentation_channel,
            )

    # 6. Audit event. The payload preserves the Day-1 ``text`` field +
    #    adds Day-2 channel + delivery fields. ``response_hash`` is the
    #    plan's hash (the Pre-emit token) so downstream auditors can
    #    correlate this surface emission with the gate that approved it.
    audit_payload: dict[str, object] = {
        "turn_id": turn_id,
        "text": response_plan.text,
        "voice_text": voice_text,
        "document_text": document_text,
        "delivered_via": list(delivered_via),
        "attention_channel": attention_channel,
        "response_hash": response_plan.response_hash,
    }
    if binding is not None:
        audit_payload.update(
            {
                "response_id": binding.response_id,
                "response_group_id": binding.response_group_id,
                "phase": "final",
                "channel": presentation_channel,
            },
        )
    event = emit_event(
        conn,
        type="surface.response_emitted",
        payload=audit_payload,
        correlation={"turn_id": turn_id},
    )

    return SurfaceState(last_gate_response_hash=None), event


__all__ = [
    "render_response",
]
