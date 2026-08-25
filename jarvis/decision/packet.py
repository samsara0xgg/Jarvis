"""L3 Situation Packet assembler — frozen view of "what's true right now".

Per ADR 0001 § Stub strategy L3 row ("real, reads projection snapshots")
and spec §3.4.x.

The Situation Packet bundles the trigger event, the most recent trace
window, the live Task Ledger snapshot, and (ADR-0009 D6) the Status
Board for the L3 decision pipeline (Intent Router / Resolver / Gates).
Reading projections from disk on every L3 invocation is fine Day-1 — the
trace is short, and rebuilding per invocation is exactly what makes the
Status Board's freshness automatic: there is no cache to invalidate.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``) and no import of ``jarvis.decision.llm`` so this
module can be re-used by the resolver / gates without circular pulls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.projections import (
        CommitObservation,
        RepoObservation,
        StatusBoard,
        TaskLedgerRecord,
        TaskLedgerSnapshot,
    )


# --- SituationPacket --------------------------------------------------------


@dataclass(frozen=True)
class SituationPacket:
    """Bundle of "what's true at the moment of this L3 invocation".

    Frozen so a single packet can be passed to multiple downstream
    callers (Resolver / Gates / LLM bridge) without anyone mutating
    shared state mid-evaluation.

    Attributes:
        trigger_event: The single event that re-entered L3 (e.g.
            ``surface.user_intent``, ``worker.reported``,
            ``action.result_observed``).
        recent_trace: Frozen tuple of the most recent events (oldest
            first; same order as Recent Trace projection iteration).
        task_ledger_snapshot: Snapshot of the Task Ledger projection
            used by the Resolver and Pre-action Gate.
        open_tasks: Convenience tuple of currently-open task records
            (derived from the snapshot; cached here so downstream
            callers do not need to recompute).
        current_turn_id: Turn correlation for the trigger (when known
            from ``trigger_event.correlation`` or
            ``trigger_event.payload``).
        current_run_id: Run correlation for the trigger (similar).
        status_board: Folded Status Board (ADR-0009 D6) — watched-repo
            state, last Mac power transition, open actions. Carried on
            the packet so L3 can answer "repo X 现在什么状态" from the
            log instead of shelling out to git mid-turn.
    """

    trigger_event: Event
    recent_trace: tuple[Event, ...]
    task_ledger_snapshot: TaskLedgerSnapshot
    open_tasks: tuple[TaskLedgerRecord, ...]
    current_turn_id: str | None
    current_run_id: str | None
    status_board: StatusBoard


# --- assemble_packet --------------------------------------------------------


def _extract_correlation(trigger: Event, key: str) -> str | None:
    """Return ``trigger.correlation[key]`` or ``trigger.payload[key]`` if str."""
    if trigger.correlation is not None and key in trigger.correlation:
        value = trigger.correlation[key]
        if isinstance(value, str):
            return value
    payload_value = trigger.payload.get(key)
    if isinstance(payload_value, str):
        return payload_value
    return None


def assemble_packet(trigger: Event, conn: sqlite3.Connection) -> SituationPacket:
    """Build a :class:`SituationPacket` from the live event log.

    Reads the event log via ``jarvis.state.projections.make_snapshot``
    (which folds Task Ledger + Recent Trace + Claim/Evidence + Status
    Board in one pass), then bundles the trigger + correlations into a
    frozen packet.

    Args:
        trigger: The event that re-entered L3 (already appended to
            the log before this call). The trigger is included in the
            packet so downstream callers can inspect its payload
            without re-querying.
        conn: Open Event Log connection.

    Returns:
        Frozen :class:`SituationPacket` ready for the Intent Router,
        Resolver, Effective Policy resolver, and Gates.
    """
    projections = make_snapshot(conn)
    snapshot = projections.task_ledger.snapshot()

    return SituationPacket(
        trigger_event=trigger,
        recent_trace=projections.recent_trace.events,
        task_ledger_snapshot=snapshot,
        open_tasks=snapshot.open_tasks(),
        current_turn_id=_extract_correlation(trigger, "turn_id"),
        current_run_id=_extract_correlation(trigger, "run_id"),
        status_board=projections.status_board,
    )


# --- Status Board system note (ADR-0009 D6 · spec §3.6.9) -------------------


# Repo-observer poll cadence (ADR-0009 D5 default ``observer.poll_interval_s``).
# Passed in by callers that hold the config; this constant is the fallback,
# because L3 may not import the config loader.
_DEFAULT_POLL_INTERVAL_S: Final[int] = 60

# Stale rule v0 (ADR-0009 D6, deviation V4): an observation older than three
# poll intervals is prefixed "stale". The per-domain TTL ladder of spec
# §3.3.3 is deferred. What keeps v0 honest is that the age is printed on
# EVERY line, stale or not — the rule can misjudge staleness, but it can
# never hand the LLM a repo state without also handing it the age.
_STALE_POLL_MULTIPLIER: Final[int] = 3

# Repos rendered per note before the remainder is summarized as a count.
# The payload caps (D5: 200-char subjects) bound each line; this bounds
# the number of lines.
_NOTE_MAX_REPOS: Final[int] = 5

# HEAD sha prefix length shown per line.
_HEAD_SHA_PREFIX_LEN: Final[int] = 12

_MS_PER_SECOND: Final[int] = 1000
_SECONDS_PER_MINUTE: Final[int] = 60
_MINUTES_PER_HOUR: Final[int] = 60
_HOURS_PER_DAY: Final[int] = 24


def _format_age(age_ms: int) -> str:
    """Render a non-negative age in milliseconds as a compact string."""
    seconds = age_ms // _MS_PER_SECOND
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    minutes = seconds // _SECONDS_PER_MINUTE
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m"
    hours = minutes // _MINUTES_PER_HOUR
    if hours < _HOURS_PER_DAY:
        return f"{hours}h{minutes % _MINUTES_PER_HOUR:02d}m"
    return f"{hours // _HOURS_PER_DAY}d{hours % _HOURS_PER_DAY:02d}h"


def _freshness_phrase(
    observed_at_ms: int,
    now_ms: int,
    stale_after_ms: int,
    last_wake_ts_ms: int | None,
) -> str:
    """Render the spec §3.6.9 freshness clause for one observation.

    The age is always part of the returned string: "stale" is a prefix on
    it, never a replacement for it. An observation older than the last
    wake is reported as ``stale since wake`` — it cannot have seen
    anything that happened while the Mac was asleep — and the plain age
    still rides along behind it.
    """
    age_ms = max(0, now_ms - observed_at_ms)
    refreshed = (
        "refreshed just now"
        if age_ms < _MS_PER_SECOND
        else f"refreshed {_format_age(age_ms)} ago"
    )
    if last_wake_ts_ms is not None and observed_at_ms < last_wake_ts_ms:
        return f"stale since wake, {refreshed}"
    if age_ms > stale_after_ms:
        return f"stale, {refreshed}"
    return refreshed


def _format_repo_line(
    observation: RepoObservation,
    commit: CommitObservation | None,
    freshness: str,
) -> str:
    """Render one watched repo as a single note bullet."""
    parts = [
        f"repo={observation.repo_path!r}",
        f"branch={observation.branch!r}",
        f"head={observation.head_sha[:_HEAD_SHA_PREFIX_LEN]!r}",
        f"dirty_files={observation.dirty_file_count}",
        f"last_commit={observation.last_commit_subject!r}",
    ]
    if commit is not None:
        parts.append(f"newest_commit_seen={commit.subject!r}")
        if commit.truncated:
            parts.append("commit_history=gapped")
    return "- " + ", ".join(parts) + f" — {freshness}"


def format_status_board_note(
    packet: SituationPacket,
    *,
    now_ms: int | None = None,
    poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
    max_repos: int = _NOTE_MAX_REPOS,
) -> str | None:
    """Render the Status Board as an LLM system note, or None when empty.

    Mirrors ``jarvis.decision._format_open_tasks_note``: None when there
    is no signal worth spending context on (no repo has been observed
    yet), otherwise a bullet list plus a short directive. One bullet per
    watched repo, each ending in its own spec §3.6.9 freshness clause.

    Args:
        packet: The packet whose ``status_board`` is rendered.
        now_ms: Reference "now" in epoch milliseconds. Defaults to the
            wall clock; injected by callers that need a fixed reference.
        poll_interval_s: Repo-observer poll cadence, in seconds. The
            stale threshold is three of these (ADR-0009 D6 stale rule v0).
        max_repos: How many repos to render before summarizing the rest
            as a count. The most recently observed win.

    Returns:
        The note string, or None when the Status Board has seen no repo.
    """
    board = packet.status_board
    observations = board.repos_by_recency()
    if not observations:
        return None

    resolved_now_ms = int(time.time() * _MS_PER_SECOND) if now_ms is None else now_ms
    stale_after_ms = poll_interval_s * _STALE_POLL_MULTIPLIER * _MS_PER_SECOND
    last_wake_ts_ms = board.last_wake_ts_ms()

    shown = observations[:max_repos]
    lines = [
        _format_repo_line(
            observation,
            board.latest_commits.get(observation.repo_path),
            _freshness_phrase(
                observation.observed_at_ms,
                resolved_now_ms,
                stale_after_ms,
                last_wake_ts_ms,
            ),
        )
        for observation in shown
    ]
    hidden = len(observations) - len(shown)
    if hidden > 0:
        lines.append(
            f"- (+{hidden} more watched repo(s) not shown; this note lists the "
            f"{max_repos} most recently observed)",
        )

    bullets = "\n".join(lines)
    return (
        "[system context] Watched repo state (Status Board projection — folded "
        "from repo-observer events, NOT read live):\n"
        f"{bullets}\n"
        "Every line carries its own freshness. Repeat that wording when you "
        "tell Allen a repo's state, and never present a line prefixed `stale` "
        "as the repo's current state — it predates the latest observation "
        "window. Do not run git yourself to refresh this; the observer owns it."
    )


__all__ = [
    "SituationPacket",
    "assemble_packet",
    "format_status_board_note",
]
