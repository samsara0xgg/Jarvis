"""The current boot's replay boundary and the dedup of response deliveries."""

from __future__ import annotations

import uuid
from collections import deque
from typing import TYPE_CHECKING

from jarvis.surface.voice_media.contract import MediaSubmitOutcome

if TYPE_CHECKING:
    from jarvis.shared import Event


class ActivePlaybackRegistry:
    """Current-boot high-water, registration, dedup, and tombstone boundary."""

    _RECENT_EVENT_LIMIT = 8192
    _RECENT_TERMINAL_LIMIT = 8192
    _ACTIVE_RESPONSE_LIMIT = 1024
    _SEQUENCES_PER_RESPONSE_LIMIT = 4096

    def __init__(self, *, boot_high_water_id: int) -> None:
        """Capture the only replay boundary accepted for this boot."""
        self.boot_id = "BOOT" + uuid.uuid4().hex
        self.boot_high_water_id = boot_high_water_id
        self._seen_events: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._registered: set[str] = set()
        self._terminal: set[str] = set()
        self._terminal_order: deque[str] = deque()
        self._sequences: set[tuple[str, int]] = set()
        self._sequence_counts: dict[str, int] = {}
        self._emitted: set[str] = set()

    def classify(  # noqa: C901, PLR0911, PLR0912 - lifecycle disposition table
        self,
        *,
        row_id: int,
        event: Event,
    ) -> MediaSubmitOutcome:
        """Reject pre-boot, duplicate, unregistered, and terminal deliveries."""
        response_raw = event.payload.get("response_id")
        response_id = response_raw if isinstance(response_raw, str) else None
        if row_id <= self.boot_high_water_id:
            return MediaSubmitOutcome(
                status="historical",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.event_uid in self._seen_events:
            return MediaSubmitOutcome(
                status="duplicate",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        self._remember_event(event.event_uid)
        if response_id is None or not response_id:
            return MediaSubmitOutcome(
                status="invalid",
                event_uid=event.event_uid,
                detail="streaming response event requires stable response_id",
            )
        if response_id in self._terminal:
            return MediaSubmitOutcome(
                status="terminal",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.type == "surface.response_open":
            if response_id in self._registered:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            if len(self._registered) >= self._ACTIVE_RESPONSE_LIMIT:
                return MediaSubmitOutcome(
                    status="overloaded",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="current-boot active response registry is full",
                )
            self._registered.add(response_id)
            return MediaSubmitOutcome("accepted", event.event_uid, response_id)
        if response_id not in self._registered:
            return MediaSubmitOutcome(
                status="unregistered",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.type == "surface.response_chunk":
            if response_id in self._emitted:
                return MediaSubmitOutcome(
                    status="terminal",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="response chunk arrived after response_emitted",
                )
            sequence_raw = event.payload.get("sequence")
            if not isinstance(sequence_raw, int) or isinstance(sequence_raw, bool):
                return MediaSubmitOutcome(
                    status="invalid",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="streaming response chunk requires integer sequence",
                )
            key = (response_id, sequence_raw)
            if key in self._sequences:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            if (
                self._sequence_counts.get(response_id, 0)
                >= self._SEQUENCES_PER_RESPONSE_LIMIT
            ):
                self.terminalize(response_id)
                return MediaSubmitOutcome(
                    status="overloaded",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="bounded response segment registry is full",
                )
            self._sequences.add(key)
            self._sequence_counts[response_id] = (
                self._sequence_counts.get(response_id, 0) + 1
            )
        elif event.type == "surface.response_emitted":
            if response_id in self._emitted:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            self._emitted.add(response_id)
        return MediaSubmitOutcome("accepted", event.event_uid, response_id)

    def has_seen(self, event_uid: str) -> bool:
        """Return whether the actor already drained this committed event."""
        return event_uid in self._seen_events

    def terminalize(self, response_id: str) -> None:
        """Prevent every late same-boot delivery after playback terminal."""
        if response_id not in self._terminal:
            self._terminal.add(response_id)
            self._terminal_order.append(response_id)
            if len(self._terminal_order) > self._RECENT_TERMINAL_LIMIT:
                self._terminal.discard(self._terminal_order.popleft())
        self._registered.discard(response_id)
        self._emitted.discard(response_id)
        self._sequences = {key for key in self._sequences if key[0] != response_id}
        self._sequence_counts.pop(response_id, None)

    def _remember_event(self, event_uid: str) -> None:
        self._seen_events.add(event_uid)
        self._seen_order.append(event_uid)
        if len(self._seen_order) > self._RECENT_EVENT_LIMIT:
            self._seen_events.discard(self._seen_order.popleft())
