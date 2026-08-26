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
    from collections.abc import Sequence

    from jarvis.shared import Event
    from jarvis.state.projections import (
        CommitObservation,
        EntityRegistry,
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
        entity_registry: Folded EntityRegistry (ADR-0011 D4) — `file:` /
            `repo:` / `task:` entries the trusted resolver (or config
            bookmarks) has produced. Consulted by the Pre-action Gate's
            widened entity-trust arm (ADR-0011 §12.2 Reconciliation J).
    """

    trigger_event: Event
    recent_trace: tuple[Event, ...]
    task_ledger_snapshot: TaskLedgerSnapshot
    open_tasks: tuple[TaskLedgerRecord, ...]
    current_turn_id: str | None
    current_run_id: str | None
    status_board: StatusBoard
    entity_registry: EntityRegistry


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


def assemble_packet(
    trigger: Event,
    conn: sqlite3.Connection,
    *,
    entity_bookmarks: Sequence[tuple[str, str]] = (),
) -> SituationPacket:
    """Build a :class:`SituationPacket` from the live event log.

    Reads the event log via ``jarvis.state.projections.make_snapshot``
    (which reads the event log once and folds Task Ledger, Recent
    Trace, Claim/Evidence, Status Board, and EntityRegistry via five
    separate in-memory passes over that one materialized read), then
    bundles the trigger + correlations into a frozen packet.

    Args:
        trigger: The event that re-entered L3 (already appended to
            the log before this call). The trigger is included in the
            packet so downstream callers can inspect its payload
            without re-querying.
        conn: Open Event Log connection.
        entity_bookmarks: `(alias, absolute-path)` seed pairs forwarded
            to the EntityRegistry's config route (ADR-0011 D4). L3 may
            not import `jarvis.execution.path_resolver`; the runtime
            composition root loads `config/file_targets.yaml` and
            threads the pairs down as plain data. Default `()`.

    Returns:
        Frozen :class:`SituationPacket` ready for the Intent Router,
        Resolver, Effective Policy resolver, and Gates.
    """
    projections = make_snapshot(conn, entity_bookmarks=entity_bookmarks)
    snapshot = projections.task_ledger.snapshot()

    return SituationPacket(
        trigger_event=trigger,
        recent_trace=projections.recent_trace.events,
        task_ledger_snapshot=snapshot,
        open_tasks=snapshot.open_tasks(),
        current_turn_id=_extract_correlation(trigger, "turn_id"),
        current_run_id=_extract_correlation(trigger, "run_id"),
        status_board=projections.status_board,
        entity_registry=projections.entity_registry,
    )


# --- Status Board system note (ADR-0009 D6 · spec §3.6.9) -------------------


# Repo-observer poll cadence (ADR-0009 D5 default ``observer.poll_interval_s``).
# Public because it is also :class:`jarvis.decision.DecideContext`'s default —
# one constant for the whole layer, so a composition root that does not pass
# the configured value still lands on the shipped cadence rather than on a
# second, independently-drifting literal. L3 may not import the config loader;
# the runtime reads ``observer.poll_interval_s`` and hands it down.
DEFAULT_OBSERVER_POLL_INTERVAL_S: Final[int] = 60

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
    poll_interval_s: int = DEFAULT_OBSERVER_POLL_INTERVAL_S,
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


# --- Evidence context note (spec §3.4.4 evidence_summary, minimal) ----------

# Claims rendered per note before the remainder is summarized as a count.
_NOTE_MAX_CLAIMS: Final[int] = 6

# Statement text cap per line (claim statements are producer-capped at
# 200 chars; this is the tighter render budget).
_NOTE_STATEMENT_CHARS: Final[int] = 90

# Stable header prefix — `jarvis.decision` uses it to find and REPLACE
# the note when the packet is re-assembled mid-turn (sync tool results
# change evidence exactly when it matters).
EVIDENCE_NOTE_PREFIX: Final[str] = "[system context] Evidence state for"


def format_evidence_context_note(
    packet: SituationPacket,
    *,
    subject_ref: str | None,
    max_claims: int = _NOTE_MAX_CLAIMS,
) -> str | None:
    """Render the subject's Claim/Evidence state as an LLM system note.

    The projection has ridden the packet since Day-1
    (``task_ledger_snapshot.claim_evidence``) but nothing rendered it,
    so the LLM drafted completion answers blind, got refused by the
    Pre-emit Gate, and burned a retry round-trip. This note hands it
    the per-turn facts the gate will judge it on, in the same evidence
    vocabulary the system prompt already teaches.

    Correction-aware (spec §3.8 invariant 2): refuted / superseded
    claims are excluded from the bullets and summarized as a count.
    ``stale_warnings`` / ``missing`` from the full §3.4.4 shape stay
    deferred with their contract fields (typed freshness,
    ``required_for_completion``).

    Returns None when there is no subject or the subject has no claims —
    no signal is worth no context spend.
    """
    if subject_ref is None:
        return None
    claim_evidence = packet.task_ledger_snapshot.claim_evidence
    all_claims = claim_evidence.claims_for(subject_ref)
    if not all_claims:
        return None
    active = claim_evidence.active_claims_for(subject_ref)
    inactive_count = len(all_claims) - len(active)

    shown = active[-max_claims:]
    lines = []
    for claim in shown:
        rows = claim_evidence.evidence_for(claim.claim_id)
        supporting = [
            ev.level for ev in rows if ev.payload.get("relation", "supports") == "supports"
        ]
        strongest = (
            max(supporting, key=lambda lv: _EVIDENCE_NOTE_RANK[lv]) if supporting else "none"
        )
        refuting = sum(1 for ev in rows if ev.payload.get("relation") == "refutes")
        statement = claim.statement[:_NOTE_STATEMENT_CHARS]
        lines.append(
            f"- [{claim.type}] {statement} — status={claim.status}, "
            f"strongest_support={strongest}, refuting_evidence={refuting}",
        )
    hidden = len(active) - len(shown)
    if hidden > 0:
        lines.append(f"- (+{hidden} older active claim(s) not shown)")
    if inactive_count > 0:
        lines.append(
            f"- ({inactive_count} claim(s) refuted or superseded — corrections "
            f"applied, no longer count as support)",
        )

    bullets = "\n".join(lines)
    return (
        f"{EVIDENCE_NOTE_PREFIX} {subject_ref!r} (Claim/Evidence projection — "
        "the Pre-emit Gate judges completion language against exactly this):\n"
        f"{bullets}\n"
        "`reported` is the worker's own words, not proof. Only an active "
        "Postcondition claim with `verified`/`accepted` SUPPORTING evidence "
        "justifies completion language; anything less, use limitation "
        "language and say what is unverified."
    )


_EVIDENCE_NOTE_RANK: Final[dict[str, int]] = {
    "reported": 0,
    "observed": 1,
    "executed": 2,
    "verified": 3,
    "accepted": 4,
}


__all__ = [
    "DEFAULT_OBSERVER_POLL_INTERVAL_S",
    "EVIDENCE_NOTE_PREFIX",
    "SituationPacket",
    "assemble_packet",
    "format_evidence_context_note",
    "format_status_board_note",
]
