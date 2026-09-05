"""Inherent v2 presenter — derived truth to wire DTOs (ADR-0014 D8/D9/D10, L5).

Everything here is a pure mapping from :mod:`jarvis.state.inherent_view`
values to the JSON-ready shapes the v2 socket carries:

- :func:`delta_payload` turns one :class:`ViewTransition` into the
  ``view.delta`` payload (``source_event_uid`` plus the ordered ``changes``);
- :func:`build_snapshot_plan` turns one :class:`InherentViewCheckpoint` into
  the complete ``snapshot.begin`` / ``snapshot.page``* / ``snapshot.end``
  frames, each page at most :data:`SNAPSHOT_PAGE_MAX_BYTES`, with
  ``content_hash`` computed over the exact UTF-8 bytes of the page frames it
  hands back — the bytes the hub sends are the bytes that were hashed.

The presenter never derives canonical action state, confirmation validity
or cancellability (D9); it does not even build the envelope — the runtime
injects a :data:`FrameEncoder` bound to the connection's identities, so
this module needs nothing but the L2 view types.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.state.inherent_view import (
        InherentViewCheckpoint,
        ResponseGroupView,
        ResponseView,
        SegmentView,
        ViewTransition,
    )

SNAPSHOT_PAGE_MAX_BYTES: Final[int] = 64 * 1024
RESPONSE_GROUPS_SECTION: Final[str] = "response_groups"
# The L3 lifecycle fold (D13 card) is what can report a terminal; until it
# lands, every response this fold projects is at most known to be underway.
_LIFECYCLE_PENDING: Final[str] = "generating"

FrameEncoder = Callable[[str, str, "Mapping[str, Any]"], str]
"""``(message_type, message_id, payload) -> frame text`` for one connection."""


def _opened_change(response: ResponseView) -> dict[str, Any]:
    return {
        "kind": "response.opened",
        "response_id": response.response_id,
        "response_group_id": response.response_group_id,
        "turn_id": response.turn_id,
        "phase": response.phase,
        "channel": response.channel,
        "lifecycle": _LIFECYCLE_PENDING,
        "question": response.question,
        "summary": None,
        "created_at_ms": response.created_at_ms,
        "revision": response.revision,
    }


def _segment_item(response_id: str, segment: SegmentView) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "response.segment",
        "response_id": response_id,
        "sequence": segment.sequence,
        "phase": segment.phase,
        "channel": segment.channel,
        "text": segment.text,
        "segment_hash": segment.segment_hash,
    }
    if segment.truncated:
        item["truncated"] = True
    return item


def _delivery_change(response: ResponseView) -> dict[str, Any]:
    return {
        "kind": "response.delivery",
        "response_id": response.response_id,
        "panel_stream": response.panel_stream,
        "reason": None,
    }


def delta_payload(transition: ViewTransition) -> dict[str, Any]:
    """Map one fold transition to the ``view.delta`` payload (D6/D10).

    Args:
        transition: What one relevant Event Log row changed.

    Returns:
        ``{"source_event_uid": ..., "changes": [...]}`` in the fold's order.
    """
    changes: list[dict[str, Any]] = []
    for change in transition.changes:
        if change.kind == "response.opened":
            changes.append(_opened_change(change.response))
        elif change.kind == "response.segment" and change.segment is not None:
            changes.append(_segment_item(change.response.response_id, change.segment))
        else:
            changes.append(_delivery_change(change.response))
    return {"source_event_uid": transition.event_uid, "changes": changes}


def response_group_item(group: ResponseGroupView) -> dict[str, Any]:
    """Map one group to its ``response_groups`` snapshot item (D8)."""
    responses: list[dict[str, Any]] = []
    for response in group.responses:
        item: dict[str, Any] = {
            "response_id": response.response_id,
            "phase": response.phase,
            "channel": response.channel,
            "lifecycle": _LIFECYCLE_PENDING,
            "revision": response.revision,
            "panel_stream": response.panel_stream,
            "segments": [
                _segment_item(response.response_id, segment) for segment in response.segments
            ],
            "playbacks": [],
        }
        if response.document_reference is not None:
            item["document_reference"] = {
                "response_id": response.document_reference.response_id,
                "event_uid": response.document_reference.event_uid,
                "utf8_bytes": response.document_reference.utf8_bytes,
            }
        responses.append(item)
    return {
        "response_group_id": group.response_group_id,
        "turn_id": group.turn_id,
        "question": group.question,
        "created_at_ms": group.created_at_ms,
        "responses": responses,
    }


@dataclass(frozen=True)
class SnapshotPlan:
    """The complete, hashed snapshot for one client (D8 step 2).

    ``page_frames`` are the exact strings to send; ``content_hash`` is the
    SHA-256 over their UTF-8 bytes in send order.
    """

    snapshot_id: str
    through_cursor: int
    begin_frame: str
    page_frames: tuple[str, ...]
    end_frame: str
    content_hash: str

    @property
    def frames(self) -> tuple[str, ...]:
        """Every frame in transmission order."""
        return (self.begin_frame, *self.page_frames, self.end_frame)


def _page_frame(
    encode: FrameEncoder,
    snapshot_id: str,
    page_index: int,
    items: list[dict[str, Any]],
) -> str:
    return encode(
        "snapshot.page",
        f"{snapshot_id}:page:{RESPONSE_GROUPS_SECTION}:{page_index}",
        {
            "snapshot_id": snapshot_id,
            "section": RESPONSE_GROUPS_SECTION,
            "page_index": page_index,
            "items": items,
        },
    )


def _utf8_len(frame: str) -> int:
    return len(frame.encode("utf-8"))


def _pack_pages(
    encode: FrameEncoder,
    snapshot_id: str,
    items: list[dict[str, Any]],
) -> list[str]:
    """Fill pages greedily so every page frame stays within the byte cap.

    A group is never split across pages: the Swift adopter keys groups by
    id, so a repeated group item would replace rather than extend the first.
    """
    # ponytail: each candidate page is re-encoded whole (quadratic in items
    # per page); size the measurement incrementally if snapshots ever hold
    # hundreds of groups.  A single group above the cap gets a page of its own
    # rather than being cut: trimming a live prefix would loop the client
    # through gap -> resync.
    pages: list[str] = []
    current: list[dict[str, Any]] = []
    current_frame = _page_frame(encode, snapshot_id, 0, current)
    for item in items:
        candidate = _page_frame(encode, snapshot_id, len(pages), [*current, item])
        if current and _utf8_len(candidate) > SNAPSHOT_PAGE_MAX_BYTES:
            pages.append(current_frame)
            current = [item]
            candidate = _page_frame(encode, snapshot_id, len(pages), current)
        else:
            current.append(item)
        current_frame = candidate
    if current:
        pages.append(current_frame)
    return pages


def build_snapshot_plan(
    checkpoint: InherentViewCheckpoint,
    *,
    snapshot_id: str,
    view_schema_version: int,
    encode: FrameEncoder,
) -> SnapshotPlan:
    """Encode one checkpoint as begin / pages / end and hash the page bytes.

    Args:
        checkpoint: The immutable view at the snapshot's high-water ``H``.
        snapshot_id: The sequencer's unique snapshot-attempt token.
        view_schema_version: The schema advertised in ``snapshot.begin``.
        encode: Builds one complete protocol-class frame for the connection.

    Returns:
        The plan whose ``through_cursor`` is the checkpoint's cursor.
    """
    items = [response_group_item(group) for group in checkpoint.groups]
    page_frames = _pack_pages(encode, snapshot_id, items)
    digest = hashlib.sha256()
    for frame in page_frames:
        digest.update(frame.encode("utf-8"))
    content_hash = digest.hexdigest()
    begin_frame = encode(
        "snapshot.begin",
        f"{snapshot_id}:begin",
        {
            "snapshot_id": snapshot_id,
            "through_cursor": checkpoint.through_cursor,
            "view_schema_version": view_schema_version,
            "section_order": [RESPONSE_GROUPS_SECTION],
            "counts": {RESPONSE_GROUPS_SECTION: len(page_frames)},
        },
    )
    end_frame = encode(
        "snapshot.end",
        f"{snapshot_id}:end",
        {
            "snapshot_id": snapshot_id,
            "through_cursor": checkpoint.through_cursor,
            "content_hash": content_hash,
        },
    )
    return SnapshotPlan(
        snapshot_id=snapshot_id,
        through_cursor=checkpoint.through_cursor,
        begin_frame=begin_frame,
        page_frames=tuple(page_frames),
        end_frame=end_frame,
        content_hash=content_hash,
    )


__all__ = [
    "RESPONSE_GROUPS_SECTION",
    "SNAPSHOT_PAGE_MAX_BYTES",
    "FrameEncoder",
    "SnapshotPlan",
    "build_snapshot_plan",
    "delta_payload",
    "response_group_item",
]
