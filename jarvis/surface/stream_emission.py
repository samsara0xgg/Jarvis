"""L5 admission of complete permitted text; semantics remain owned by L3."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.state.stream_emission import append_permitted_segment

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared.stream_emission import EmissionPermit
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.lifecycle_terminal import FailureInjector
    from jarvis.state.stream_emission import SegmentCommit


def emit_permitted_segment(  # noqa: PLR0913 - output data plus transaction collaborators
    conn: sqlite3.Connection,
    permit: EmissionPermit,
    text: str,
    *,
    query: str,
    attention_channel: str,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> SegmentCommit:
    """Revalidate receipt/text/source binding, then expose the committed chunk.

    There is no stdout, socket, or TTS side effect before the L2 append. Existing
    committed-event subscribers and durable watchers consume the same row.
    """
    return append_permitted_segment(
        conn,
        permit,
        text,
        query=query,
        attention_channel=attention_channel,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
    )
