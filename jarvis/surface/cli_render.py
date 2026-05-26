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

import logging
import sys
from typing import IO, TYPE_CHECKING

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


def _emit_response_open(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    query: str,
) -> None:
    """Emit the ADR-0003 Step 2 ``surface.response_open`` event.

    Single emission per turn. Payload carries ``turn_id``, ``query``
    (the user transcript that triggered the turn — empty string allowed),
    and ``kind`` (always ``"text"`` for A1).
    """
    emit_event(
        conn,
        type="surface.response_open",
        payload={"turn_id": turn_id, "query": query, "kind": "text"},
        correlation={"turn_id": turn_id},
    )


def _emit_response_chunks(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    response_plan: ResponsePlanLike,
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

    for chunk_text in chunks:
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={"turn_id": turn_id, "text": chunk_text},
            correlation={"turn_id": turn_id},
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
       one ``surface.response_open`` (carrying ``query`` + ``kind="text"``),
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
    if streaming_enabled:
        _emit_response_open(conn, turn_id=turn_id, query=query)
        _emit_response_chunks(conn, turn_id=turn_id, response_plan=response_plan)

    # 6. Audit event. The payload preserves the Day-1 ``text`` field +
    #    adds Day-2 channel + delivery fields. ``response_hash`` is the
    #    plan's hash (the Pre-emit token) so downstream auditors can
    #    correlate this surface emission with the gate that approved it.
    event = emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            "turn_id": turn_id,
            "text": response_plan.text,
            "voice_text": voice_text,
            "document_text": document_text,
            "delivered_via": list(delivered_via),
            "attention_channel": attention_channel,
            "response_hash": response_plan.response_hash,
        },
        correlation={"turn_id": turn_id},
    )

    return SurfaceState(last_gate_response_hash=None), event


__all__ = [
    "render_response",
]
