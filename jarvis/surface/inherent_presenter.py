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
  Sections page independently in :data:`SECTION_ORDER`, ``page_index``
  restarts at 0 in each, and a section the checkpoint left empty is omitted
  from both ``section_order`` and ``counts``.

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
        ActionView,
        ConfirmationCleared,
        ConfirmationView,
        InherentViewCheckpoint,
        ResponseGroupView,
        ResponseView,
        SegmentView,
        ViewChange,
        ViewTransition,
    )

SNAPSHOT_PAGE_MAX_BYTES: Final[int] = 64 * 1024
RESPONSE_GROUPS_SECTION: Final[str] = "response_groups"
ACTIONS_SECTION: Final[str] = "actions"
PENDING_CONFIRMATION_SECTION: Final[str] = "pending_confirmation"
SECTION_ORDER: Final[tuple[str, ...]] = (
    RESPONSE_GROUPS_SECTION,
    ACTIONS_SECTION,
    PENDING_CONFIRMATION_SECTION,
)
"""The D8 sections this presenter produces, in the order it advertises them."""

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
        "lifecycle": response.lifecycle,
        "question": response.question,
        "summary": None,
        "created_at_ms": response.created_at_ms,
        "revision": response.revision,
        "source_client_request_id": response.source_client_request_id,
    }


def _lifecycle_change(response: ResponseView) -> dict[str, Any]:
    return {
        "kind": "response.lifecycle",
        "response_id": response.response_id,
        "lifecycle": response.lifecycle,
        "terminal_reason": response.terminal_reason,
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


def action_item(action: ActionView) -> dict[str, Any]:
    """Map one action to its ``ActionUpsert`` wire shape (D13).

    ``freshness_ms`` is omitted: no clock reaches a pure fold, and the shipped
    decoder reads its absence as fresh.  ``cleanup_state`` and
    ``cancel_request`` have no key on the shipped ``ActionUpsert`` and stay
    inside the fold.
    """
    return {
        "action_id": action.action_id,
        "response_group_id": action.response_group_id,
        "task_id": action.task_id,
        "state": action.canonical_state,
        "label": action.action_type,
        "target": action.safe_target_ref,
        "revision": action.state_revision_cursor,
        "cancellable": action.cancellable,
    }


def confirmation_item(confirmation: ConfirmationView) -> dict[str, Any]:
    """Map the live slot to its ``ConfirmationUpsert`` wire shape (D14)."""
    return {
        "confirmation_id": confirmation.confirmation_id,
        "response_group_id": confirmation.response_group_id,
        "action_id": confirmation.action_id,
        "summary": confirmation.summary,
        "target": confirmation.target,
        "risk": confirmation.risk,
        "options": list(confirmation.options),
        "expires_at_ms": confirmation.expires_at_ms,
        "revision": confirmation.revision,
    }


def _cleared_change(cleared: ConfirmationCleared) -> dict[str, Any]:
    return {
        "kind": "confirmation.cleared",
        "confirmation_id": cleared.confirmation_id,
        "reason": cleared.reason,
        "revision": cleared.revision,
    }


def _response_change(change: ViewChange) -> dict[str, Any] | None:
    response = change.response
    if response is None:
        return None
    if change.kind == "response.opened":
        return _opened_change(response)
    if change.kind == "response.lifecycle":
        return _lifecycle_change(response)
    if change.kind == "response.segment" and change.segment is not None:
        return _segment_item(response.response_id, change.segment)
    return _delivery_change(response)


def _change_payload(change: ViewChange) -> dict[str, Any] | None:
    """Map one typed fold mutation to its D10 wire shape."""
    if change.kind == "action.upsert" and change.action is not None:
        return {"kind": "action.upsert", **action_item(change.action)}
    if change.kind == "confirmation.upsert" and change.confirmation is not None:
        return {"kind": "confirmation.upsert", **confirmation_item(change.confirmation)}
    if change.kind == "confirmation.cleared" and change.cleared is not None:
        return _cleared_change(change.cleared)
    return _response_change(change)


def delta_payload(transition: ViewTransition) -> dict[str, Any]:
    """Map one fold transition to the ``view.delta`` payload (D6/D10).

    Args:
        transition: What one relevant Event Log row changed.

    Returns:
        ``{"source_event_uid": ..., "changes": [...]}`` in the fold's order.
    """
    changes = [_change_payload(change) for change in transition.changes]
    return {
        "source_event_uid": transition.event_uid,
        "changes": [change for change in changes if change is not None],
    }


def response_group_item(group: ResponseGroupView) -> dict[str, Any]:
    """Map one group to its ``response_groups`` snapshot item (D8)."""
    responses: list[dict[str, Any]] = []
    for response in group.responses:
        item: dict[str, Any] = {
            "response_id": response.response_id,
            "phase": response.phase,
            "channel": response.channel,
            "lifecycle": response.lifecycle,
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
        "source_client_request_id": group.source_client_request_id,
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
    section: str,
    page_index: int,
    items: list[dict[str, Any]],
) -> str:
    return encode(
        "snapshot.page",
        f"{snapshot_id}:page:{section}:{page_index}",
        {
            "snapshot_id": snapshot_id,
            "section": section,
            "page_index": page_index,
            "items": items,
        },
    )


def _utf8_len(frame: str) -> int:
    return len(frame.encode("utf-8"))


def _pack_pages(
    encode: FrameEncoder,
    snapshot_id: str,
    section: str,
    items: list[dict[str, Any]],
) -> list[str]:
    """Fill one section's pages greedily so every frame stays within the byte cap.

    ``page_index`` restarts at 0 per section — the adopter keys staged pages
    by ``(section, page_index)``.  An item is never split across pages: the
    Swift adopter keys groups and actions by id, so a repeated item would
    replace rather than extend the first.
    """
    # ponytail: each candidate page is re-encoded whole (quadratic in items
    # per page); size the measurement incrementally if snapshots ever hold
    # hundreds of groups.  A single group above the cap gets a page of its own
    # rather than being cut: trimming a live prefix would loop the client
    # through gap -> resync.
    pages: list[str] = []
    current: list[dict[str, Any]] = []
    current_frame = _page_frame(encode, snapshot_id, section, 0, current)
    for item in items:
        candidate = _page_frame(encode, snapshot_id, section, len(pages), [*current, item])
        if current and _utf8_len(candidate) > SNAPSHOT_PAGE_MAX_BYTES:
            pages.append(current_frame)
            current = [item]
            candidate = _page_frame(encode, snapshot_id, section, len(pages), current)
        else:
            current.append(item)
        current_frame = candidate
    if current:
        pages.append(current_frame)
    return pages


def _section_items(checkpoint: InherentViewCheckpoint) -> dict[str, list[dict[str, Any]]]:
    """The items of each D8 section this presenter produces, empties included."""
    confirmation = checkpoint.pending_confirmation
    return {
        RESPONSE_GROUPS_SECTION: [response_group_item(group) for group in checkpoint.groups],
        ACTIONS_SECTION: [action_item(action) for action in checkpoint.actions],
        PENDING_CONFIRMATION_SECTION: (
            [] if confirmation is None else [confirmation_item(confirmation)]
        ),
    }


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
    pages_of_section = {
        section: _pack_pages(encode, snapshot_id, section, items)
        for section, items in _section_items(checkpoint).items()
        if items
    }
    page_frames = [frame for pages in pages_of_section.values() for frame in pages]
    digest = hashlib.sha256()
    for frame in page_frames:
        digest.update(frame.encode("utf-8"))
    content_hash = digest.hexdigest()
    section_order = [section for section in SECTION_ORDER if section in pages_of_section]
    begin_frame = encode(
        "snapshot.begin",
        f"{snapshot_id}:begin",
        {
            "snapshot_id": snapshot_id,
            "through_cursor": checkpoint.through_cursor,
            "view_schema_version": view_schema_version,
            "section_order": section_order,
            "counts": {section: len(pages_of_section[section]) for section in section_order},
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
    "ACTIONS_SECTION",
    "PENDING_CONFIRMATION_SECTION",
    "RESPONSE_GROUPS_SECTION",
    "SECTION_ORDER",
    "SNAPSHOT_PAGE_MAX_BYTES",
    "FrameEncoder",
    "SnapshotPlan",
    "action_item",
    "build_snapshot_plan",
    "confirmation_item",
    "delta_payload",
    "response_group_item",
]
