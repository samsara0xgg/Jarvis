"""Inherent response-group view fold — ADR-0014 D8/D9/D16 (L2).

The v2 panel's durable truth is derived here and nowhere else: one
:class:`InherentView` folds ``surface.response_{open,chunk,emitted}`` rows in
``events.id`` order and answers two questions the runtime sequencer asks —
"what changed on this row?" (:class:`ViewTransition`) and "what does a new
client need to see?" (:class:`InherentViewCheckpoint`).  Both answers are
immutable values; the sequencer owns the scan watermark and the wake, the
presenter owns the wire shape, and neither re-derives what this fold holds
(D9: "L2 owns all fold logic and immutable projection snapshots").

Two bounds live here because they bound the *truth kept*, not the bytes
sent (D16):

- at most :data:`RECENT_TERMINAL_GROUP_LIMIT` terminal groups are retained,
  oldest evicted first, so a snapshot carries every open group plus a bounded
  recent history and the fold never grows with the log;
- a closed response whose segments exceed :data:`INLINE_DOCUMENT_BUDGET_BYTES`
  of UTF-8 keeps a whole-segment prefix as its preview plus a
  :class:`DocumentReference` naming the durable row that carries the complete
  body.  Open responses are never cut: a live client extends them by
  ``sequence`` and a trimmed prefix would only send it into a resync loop.

Only rows carrying a ``response_id`` are Inherent-relevant.  The three
production emitters (``cli_render`` and the L2 stream emission path) always
bind one; a legacy row without it advances the cursor and nothing else.

Layer rules (L2): stdlib only.  The fold takes plain row fields rather than
an ``Event`` so this module names no other package at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

RESPONSE_EVENT_TYPES: Final[tuple[str, ...]] = (
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
)
INLINE_DOCUMENT_BUDGET_BYTES: Final[int] = 16384
RECENT_TERMINAL_GROUP_LIMIT: Final[int] = 20
INPUT_CORRELATION_TYPE: Final[str] = "surface.user_intent"
PENDING_REQUEST_LIMIT: Final[int] = 64

PanelStream = Literal["open", "closed"]
ChangeKind = Literal["response.opened", "response.segment", "response.delivery"]

_PHASES: Final[frozenset[str]] = frozenset({"commentary", "final"})
_CHANNELS: Final[frozenset[str]] = frozenset({"speech", "document", "both"})
_DEFAULT_PHASE: Final[str] = "final"
_DEFAULT_CHANNEL: Final[str] = "document"


@dataclass(frozen=True)
class SegmentView:
    """One permitted panel segment of a response (D10 ``response.segment``)."""

    sequence: int
    phase: str
    channel: str
    text: str
    segment_hash: str
    truncated: bool = False


@dataclass(frozen=True)
class DocumentReference:
    """Durable pointer to a body the view does not inline (D16).

    ``event_uid`` names the ``surface.response_emitted`` row, which carries
    the complete text; a later card's fetch path resolves it.
    """

    response_id: str
    event_uid: str
    utf8_bytes: int


@dataclass(frozen=True)
class ResponseView:
    """One response's panel-stream truth (D3 ``ResponseState``, panel half)."""

    response_id: str
    response_group_id: str
    turn_id: str
    phase: str
    channel: str
    question: str | None
    created_at_ms: int
    revision: int
    panel_stream: PanelStream
    segments: tuple[SegmentView, ...] = ()
    document_reference: DocumentReference | None = None
    source_client_request_id: str | None = None


@dataclass(frozen=True)
class ResponseGroupView:
    """One response group: sibling responses sharing a turn (D3, D26)."""

    response_group_id: str
    turn_id: str
    question: str | None
    created_at_ms: int
    opened_cursor: int
    last_cursor: int
    responses: tuple[ResponseView, ...]
    source_client_request_id: str | None = None

    @property
    def terminal(self) -> bool:
        """Report whether every sibling's panel stream is closed."""
        return all(response.panel_stream == "closed" for response in self.responses)


@dataclass(frozen=True)
class InherentViewCheckpoint:
    """The bounded, immutable state a snapshot is built from (D8 step 1).

    ``pending_requests`` carries the D21 ``turn_id -> source_client_request_id``
    entries whose group has not opened yet, so a catch-up re-fold from this
    checkpoint stamps the same correlation a live client saw.
    """

    through_cursor: int
    groups: tuple[ResponseGroupView, ...]
    pending_requests: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ViewChange:
    """One typed mutation; ``segment`` is set only for ``response.segment``."""

    kind: ChangeKind
    response: ResponseView
    segment: SegmentView | None = None


@dataclass(frozen=True)
class ViewTransition:
    """What one relevant row changed: exactly one per relevant row (D6)."""

    cursor: int
    event_uid: str
    changes: tuple[ViewChange, ...]


def _string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _enum(payload: Mapping[str, Any], key: str, allowed: frozenset[str], default: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value in allowed else default


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_utf8(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` UTF-8 bytes on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def bound_document(response: ResponseView, *, event_uid: str) -> ResponseView:
    """Apply the inline budget to a closed response (D16).

    A body at or under :data:`INLINE_DOCUMENT_BUDGET_BYTES` is kept whole.
    Above it, the longest whole-segment prefix that fits is the preview; when
    even the first segment does not fit, that segment is cut on a character
    boundary and flagged ``truncated`` while keeping its durable hash.

    Args:
        response: The response whose panel stream just closed.
        event_uid: The closing ``surface.response_emitted`` row, which carries
            the complete body the reference points at.

    Returns:
        The response unchanged, or with a bounded preview plus reference.
    """
    total = sum(_utf8_len(segment.text) for segment in response.segments)
    if total <= INLINE_DOCUMENT_BUDGET_BYTES:
        return response
    kept: list[SegmentView] = []
    used = 0
    for segment in response.segments:
        size = _utf8_len(segment.text)
        if used + size > INLINE_DOCUMENT_BUDGET_BYTES:
            break
        kept.append(segment)
        used += size
    if not kept:
        first = response.segments[0]
        preview = _cut_utf8(first.text, INLINE_DOCUMENT_BUDGET_BYTES)
        kept.append(replace(first, text=preview, truncated=True))
    reference = DocumentReference(
        response_id=response.response_id,
        event_uid=event_uid,
        utf8_bytes=total,
    )
    return replace(response, segments=tuple(kept), document_reference=reference)


class InherentView:
    """The mutable fold behind the immutable views.

    One instance lives inside the runtime sequencer for the daemon's lifetime;
    :meth:`from_checkpoint` builds a second, private one for a client's
    post-ACK catch-up (D8 step 5) so the same fold rules produce the same
    deltas the client would have seen live.
    """

    def __init__(
        self,
        *,
        through_cursor: int = 0,
        groups: Iterable[ResponseGroupView] = (),
        pending_requests: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Start from ``groups`` as truth folded through ``through_cursor``."""
        self._through_cursor = through_cursor
        self._request_of_turn: dict[str, str] = dict(pending_requests)
        self._groups: dict[str, ResponseGroupView] = {
            group.response_group_id: group for group in groups
        }
        self._group_of: dict[str, str] = {
            response.response_id: group.response_group_id
            for group in self._groups.values()
            for response in group.responses
        }

    @classmethod
    def from_checkpoint(cls, checkpoint: InherentViewCheckpoint) -> InherentView:
        """Clone a checkpoint into a fold that accepts rows past its cursor."""
        return cls(
            through_cursor=checkpoint.through_cursor,
            groups=checkpoint.groups,
            pending_requests=checkpoint.pending_requests,
        )

    @property
    def through_cursor(self) -> int:
        """The highest cursor this fold has been told about."""
        return self._through_cursor

    def checkpoint(self, *, through_cursor: int) -> InherentViewCheckpoint:
        """Freeze the retained groups, oldest first, as folded through ``through_cursor``.

        Args:
            through_cursor: The scan high-water the caller drained to; it may
                exceed the last relevant row's cursor because irrelevant rows
                advance the watermark without touching the view.

        Returns:
            An immutable checkpoint at that cursor.
        """
        if through_cursor < self._through_cursor:
            msg = (
                f"checkpoint cursor {through_cursor} regresses below the fold's "
                f"{self._through_cursor}"
            )
            raise ValueError(msg)
        self._through_cursor = through_cursor
        ordered = sorted(self._groups.values(), key=lambda group: group.opened_cursor)
        return InherentViewCheckpoint(
            through_cursor=through_cursor,
            groups=tuple(ordered),
            pending_requests=tuple(self._request_of_turn.items()),
        )

    def fold(
        self,
        *,
        cursor: int,
        event_uid: str,
        event_type: str,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
    ) -> ViewTransition | None:
        """Apply one Event Log row and report what it changed.

        Rows must arrive in strictly ascending ``cursor`` order (D6: cursor
        regression is not legal).  An irrelevant row — another type, or a
        response row without a ``response_id`` — advances the cursor and
        returns None.  A relevant row always returns a transition, possibly
        with no changes (a duplicate open, a late or repeated segment), so
        every relevant row maps to exactly one durable envelope.

        Args:
            cursor: ``events.id`` of the row.
            event_uid: The row's ``event_uid``; becomes the delta's message id.
            event_type: The row's ``type``.
            ts_epoch_ms: The row's timestamp; a new response's creation time.
            payload: The decoded ``payload_json``.

        Returns:
            The transition, or None for an irrelevant row.

        Raises:
            ValueError: ``cursor`` does not exceed the fold's cursor.
        """
        if cursor <= self._through_cursor:
            msg = f"cursor {cursor} does not advance past {self._through_cursor}"
            raise ValueError(msg)
        self._through_cursor = cursor
        if event_type == INPUT_CORRELATION_TYPE:
            self._remember_request(payload)
        if event_type not in RESPONSE_EVENT_TYPES:
            return None
        response_id = _string(payload, "response_id")
        if response_id is None:
            return None

        changes: list[ViewChange] = []
        response = self._response(response_id)
        if response is None:
            response = self._open(response_id, cursor, ts_epoch_ms, payload)
            changes.append(ViewChange("response.opened", response))
            changes.append(ViewChange("response.delivery", response))
        if event_type == "surface.response_chunk":
            segment = self._segment(response, payload)
            if segment is not None and response.panel_stream == "open":
                response = self._store(
                    replace(response, segments=(*response.segments, segment), revision=cursor),
                    cursor,
                )
                changes.append(ViewChange("response.segment", response, segment))
        elif event_type == "surface.response_emitted" and response.panel_stream == "open":
            response = self._store(
                bound_document(
                    replace(response, panel_stream="closed", revision=cursor),
                    event_uid=event_uid,
                ),
                cursor,
            )
            changes.append(ViewChange("response.delivery", response))
            self._evict_terminal_groups()
        return ViewTransition(cursor=cursor, event_uid=event_uid, changes=tuple(changes))

    # --- internals ---------------------------------------------------------

    def _response(self, response_id: str) -> ResponseView | None:
        group_id = self._group_of.get(response_id)
        if group_id is None:
            return None
        return next(
            response
            for response in self._groups[group_id].responses
            if response.response_id == response_id
        )

    def _open(
        self,
        response_id: str,
        cursor: int,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
    ) -> ResponseView:
        group_id = _string(payload, "response_group_id") or response_id
        turn_id = _string(payload, "turn_id") or ""
        question = _string(payload, "query")
        group = self._groups.get(group_id)
        # D21: the correlation is consumed when the group first opens; a
        # sibling response of an already-open group inherits what the group
        # carries, exactly as it shares the group's turn.
        request_id = (
            self._request_of_turn.pop(turn_id, None)
            if group is None
            else group.source_client_request_id
        )
        response = ResponseView(
            response_id=response_id,
            response_group_id=group_id,
            turn_id=turn_id,
            phase=_enum(payload, "phase", _PHASES, _DEFAULT_PHASE),
            channel=_enum(payload, "channel", _CHANNELS, _DEFAULT_CHANNEL),
            question=question,
            created_at_ms=ts_epoch_ms,
            revision=cursor,
            panel_stream="open",
            source_client_request_id=request_id,
        )
        if group is None:
            group = ResponseGroupView(
                response_group_id=group_id,
                turn_id=turn_id,
                question=question,
                created_at_ms=ts_epoch_ms,
                opened_cursor=cursor,
                last_cursor=cursor,
                responses=(),
                source_client_request_id=request_id,
            )
        self._groups[group_id] = replace(
            group,
            question=group.question or question,
            responses=(*group.responses, response),
            last_cursor=cursor,
        )
        self._group_of[response_id] = group_id
        return response

    def _remember_request(self, payload: Mapping[str, Any]) -> None:
        """Hold one D21 ``turn_id -> request_id`` until its group opens.

        The row itself stays irrelevant: it produces no transition and no
        envelope, because the next ``response.opened`` already carries the
        fact.  The map is bounded so an input row whose turn never opens a
        response — a refused turn, a crash — cannot grow the fold with the log.
        """
        turn_id = _string(payload, "turn_id")
        request_id = _string(payload, "source_client_request_id")
        if turn_id is None or request_id is None:
            return
        self._request_of_turn[turn_id] = request_id
        while len(self._request_of_turn) > PENDING_REQUEST_LIMIT:
            self._request_of_turn.pop(next(iter(self._request_of_turn)))

    def _store(self, response: ResponseView, cursor: int) -> ResponseView:
        group = self._groups[response.response_group_id]
        self._groups[response.response_group_id] = replace(
            group,
            responses=tuple(
                response if existing.response_id == response.response_id else existing
                for existing in group.responses
            ),
            last_cursor=cursor,
        )
        return response

    @staticmethod
    def _segment(response: ResponseView, payload: Mapping[str, Any]) -> SegmentView | None:
        text = payload.get("text")
        if not isinstance(text, str):
            return None
        sequence = payload.get("sequence")
        if type(sequence) is not int or sequence < 0:
            sequence = len(response.segments)
        if any(existing.sequence == sequence for existing in response.segments):
            return None
        segment_hash = _string(payload, "segment_hash") or hashlib.sha256(text.encode()).hexdigest()
        return SegmentView(
            sequence=sequence,
            phase=_enum(payload, "phase", _PHASES, response.phase),
            channel=_enum(payload, "channel", _CHANNELS, response.channel),
            text=text,
            segment_hash=segment_hash,
        )

    def _evict_terminal_groups(self) -> None:
        terminal = sorted(
            (group for group in self._groups.values() if group.terminal),
            key=lambda group: group.last_cursor,
        )
        for group in terminal[: max(0, len(terminal) - RECENT_TERMINAL_GROUP_LIMIT)]:
            del self._groups[group.response_group_id]
            for response in group.responses:
                del self._group_of[response.response_id]


__all__ = [
    "INLINE_DOCUMENT_BUDGET_BYTES",
    "INPUT_CORRELATION_TYPE",
    "PENDING_REQUEST_LIMIT",
    "RECENT_TERMINAL_GROUP_LIMIT",
    "RESPONSE_EVENT_TYPES",
    "DocumentReference",
    "InherentView",
    "InherentViewCheckpoint",
    "ResponseGroupView",
    "ResponseView",
    "SegmentView",
    "ViewChange",
    "ViewTransition",
]
