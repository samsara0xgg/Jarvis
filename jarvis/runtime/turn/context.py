"""What a turn reads to build its request, and the next turn's prefix warm-up."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from jarvis.decision import (
    EntityResolverLike,
    ResolvedEntityLike,
    ToolRegistryLike,
    open_prefix_warm,
)
from jarvis.decision.pre_route import spoken_turn
from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.runtime.core_memory import CHECKIN_LINE_CHARS, CHECKIN_LINE_PREFIX
from jarvis.runtime.dashboard import (
    DRAFT_LINE_CHARS,
    DRAFT_LINE_PREFIX,
    VIEW_LINE_CHARS,
    VIEW_LINE_PREFIX,
)
from jarvis.runtime.rides import RIDE_LINE_CHARS, RIDE_LINE_PREFIX
from jarvis.shared.about import clean_about
from jarvis.shared.device_link import DeviceCallError
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import MemorySettings, PictureFileId, ReceiptsOf, render_context
from jarvis.state.shares import SHARES_LINE_CHARS, SHARES_LINE_PREFIX
from jarvis.state.turn_steps import answer_receipts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from jarvis.decision.llm import LLMClient
    from jarvis.decision.llm_stream import LLMStreamHandle
    from jarvis.decision.packet import SnapshotReader
    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.config import RuntimeConfig
    from jarvis.shared import Event
    from jarvis.surface.terminal_link import TerminalHub


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedFileEntity:
    """Adapts `path_resolver.ResolvedTarget` to L3's `ResolvedEntityLike`.

    `jarvis.runtime` is the only module allowed to know both the L3
    Protocol shape and the L4 `ResolvedTarget` dataclass it adapts —
    that is the whole point of the resolve-on-propose seam (ADR-0011
    D4): L3 never imports `jarvis.execution.path_resolver` directly.
    """

    entity_id: str
    canonical: str
    confidence: str
    match_basis: str


def make_entity_resolver(
    conn: sqlite3.Connection, terminals: TerminalHub | None = None,
) -> EntityResolverLike:
    """Build the resolve-on-propose callable (ADR-0011 D4), closing over `conn`.

    Wired into `DecideContext.entity_resolver`; `_dispatch_one_tool_call`
    calls it for a `requires_entity=True` tool whose `target_entity_ref`
    is still unset. `path_resolver.resolve` never raises for a bad or
    unresolvable query (its own docstring's contract) — a miss is the
    normal `None` return, not an exception, so this wrapper adds no
    try/except of its own; a real bug inside `resolve` should surface,
    not be swallowed here.

    On a brain (ADR 0170) the file is on the terminal's disk, so the terminal resolves
    the name; no terminal connected is a miss.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        path: str
        source: str
        if terminals is None:
            target = resolve_file_entity(query, "file", conn)
            if target is None:
                return None
            path, source = str(target.path), target.source
        else:
            try:
                found = terminals.call("resolve_file", {"query": query}, None)
            except DeviceCallError:
                return None
            path, source = str(found.get("path", "")), str(found.get("source", ""))
            if not path:
                return None
        return _ResolvedFileEntity(
            entity_id=f"file:{path}",
            canonical=path,
            confidence="bookmark" if source == "bookmark" else "fuzzy",
            match_basis=source,
        )

    return _resolve


def snapshot_reader(runtime: JarvisRuntime) -> SnapshotReader | None:
    """The shared incremental reader every read of a turn goes through (ADR 0164)."""
    return runtime.decision_state.read if runtime.decision_state is not None else None


def voice_service_tier(config: RuntimeConfig, trigger: Event) -> str | None:
    """``realtime.response.voice_service_tier``, for a turn Allen spoke; else ``None``.

    The tier rides every model request that turn makes through ``decide()`` and
    nothing else: the client still sends it to OpenAI's own host only.
    """
    return config.realtime.voice_service_tier if spoken_turn(trigger) else None


def max_tool_iterations(config: RuntimeConfig, trigger: Event) -> int:
    """``llm.max_tool_iterations`` (ADR 0060); a spoken turn reads ``max_tool_iterations_voice``."""
    return config.tool_iterations.for_turn(spoken=spoken_turn(trigger))


def about_record(runtime: JarvisRuntime, trigger: Event) -> str | None:
    """ADR 0298: the record of the item this turn's phone has open; never fails the turn."""
    raw = trigger.payload.get("about")
    if runtime.about_items is None or raw is None:
        return None
    try:
        return runtime.about_items.record(clean_about(raw))
    except Exception:  # a producer must never fail a turn.
        LOGGER.exception("live context: the about record failed, skipped")
        return None


_LIVE_LINE_CHARS: Final = 200


def turn_device(runtime: JarvisRuntime, turn_id: str) -> str | None:
    """ADR 0226: the device that opened this turn, for its channel line; never fails it."""
    if runtime.whereabouts is None:
        return None
    try:
        return runtime.whereabouts.origin(runtime.conn, turn_id)
    except Exception:  # a producer must never fail a turn.
        LOGGER.exception("live context: the turn's device could not be read, skipped")
        return None


def whereabouts_lines(runtime: JarvisRuntime, turn_id: str | None) -> tuple[str, ...]:
    """ADR 0217: the devices there are and the phone's place; never fails the turn."""
    if runtime.whereabouts is None:
        return ()
    try:
        return runtime.whereabouts.lines(runtime.conn, turn_id)
    except Exception:  # a producer must never fail a turn.
        LOGGER.exception("live context: the whereabouts lines failed, skipped")
        return ()


def live_lines(producers: tuple[Callable[[], str | None], ...]) -> tuple[str, ...]:
    """ADR 0148: what each live-context producer says now; a raising one is skipped, logged."""
    lines: list[str] = []
    for produce in producers:
        try:
            line = produce()
        except Exception:  # a producer must never fail a turn.
            LOGGER.exception("live context: a producer failed, skipped")
            continue
        if line:
            # The open letter's draft and the Dashboard's view are the lines that may run long.
            limit = (
                DRAFT_LINE_CHARS if line.startswith(DRAFT_LINE_PREFIX)
                else VIEW_LINE_CHARS if line.startswith(VIEW_LINE_PREFIX)
                else SHARES_LINE_CHARS if line.startswith(SHARES_LINE_PREFIX)
                else RIDE_LINE_CHARS if line.startswith(RIDE_LINE_PREFIX)
                else CHECKIN_LINE_CHARS if line.startswith(CHECKIN_LINE_PREFIX)
                else _LIVE_LINE_CHARS
            )
            lines.append(line[:limit])
    return tuple(lines)


def history_pictures(runtime: JarvisRuntime, client: LLMClient) -> PictureFileId | None:
    """ADR 0255: how history gets a picture's file id on ``client``; none off a Claude preset."""
    if runtime.picture_files is None:
        return None
    return runtime.picture_files.resolver(client)


def history_receipts(runtime: JarvisRuntime) -> ReceiptsOf:
    """ADR 0319: how history learns the receipts of the turn behind an answer, from the log."""

    def receipts(answer_ids: Sequence[str]) -> Mapping[str, str]:
        path = runtime.runtime_paths.event_log
        try:
            with contextlib.closing(open_runtime_event_log(path)) as conn:
                return answer_receipts(conn, answer_ids)
        except sqlite3.Error:
            LOGGER.warning("history: the receipts of its answers could not be read", exc_info=True)
            return {}

    return receipts


def warm_next_prefix(  # noqa: PLR0913 — the next turn's request: client, prompt, shape, format
    runtime: JarvisRuntime,
    memory: MemorySettings,
    *,
    llm_client: LLMClient,
    system_prompt: str,
    responses: bool,
    text_format: Mapping[str, Any] | None = None,
) -> None:
    """Send the next turn's prompt prefix in the background (``open_prefix_warm``).

    The history is read the way the next turn will read it, now that this
    turn's rows are in. Its own thread and Event Log connection: the answer is
    already on its way and nothing waits for this.
    """

    async def _drain(handle: LLMStreamHandle) -> None:
        async for _event in handle.events():
            pass

    def _run() -> None:
        try:
            history = render_context(
                memory.db_path, exclude_id="", since=runtime.config.session.history_since,
                recent=runtime.config.session.recent_records,
                context=runtime.config.session.context,
                raw_max_chars=runtime.config.session.context_raw_max_chars,
                ledger=runtime.ledger,
                places=runtime.places_block,
                pictures=history_pictures(runtime, llm_client),
                receipts=history_receipts(runtime),
            ).history
            with contextlib.closing(
                open_runtime_event_log(runtime.runtime_paths.event_log),
            ) as conn:
                handle = open_prefix_warm(
                    conn,
                    llm_client=llm_client,
                    system_prompt=system_prompt,
                    history=history,
                    tool_registry=cast("ToolRegistryLike", runtime.tool_registry),
                    responses=responses,
                    committed_event_bus=runtime.committed_event_bus,
                    text_format=text_format,
                )
                if handle is not None:
                    asyncio.run(_drain(handle))
        except Exception:  # noqa: BLE001 — a failed warm-up costs only the cache it tried to fill
            LOGGER.warning("prefix warm failed", exc_info=True)

    threading.Thread(target=_run, name="jarvis-prefix-warm", daemon=True).start()
