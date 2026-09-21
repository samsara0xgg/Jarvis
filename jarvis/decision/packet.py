"""L3 Situation Packet assembler — frozen view of "what's true right now".

Per ADR 0001 § Stub strategy L3 row ("real, reads projection snapshots")
and spec §3.4.x.

The Situation Packet bundles the trigger event, the most recent trace
window, and (ADR-0009 D6) the Status Board for the L3 decision pipeline
(Intent Router / Gates). Reading projections from disk on every L3
invocation is fine Day-1 — the trace is short, and rebuilding per
invocation is exactly what makes the Status Board's freshness automatic:
there is no cache to invalidate.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``) and no import of ``jarvis.decision.llm`` so this
module can be re-used by the gates without circular pulls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.decision_snapshot import read_decision_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.authorization_snapshot import AuthorizationSnapshot
    from jarvis.state.conversation import ConversationHistory
    from jarvis.state.projections import (
        ActionAdmissions,
        CommitObservation,
        PendingConfirmations,
        RepoObservation,
        StatusBoard,
    )


# --- SituationPacket --------------------------------------------------------


@dataclass(frozen=True)
class SituationPacket:
    """Bundle of "what's true at the moment of this L3 invocation".

    Frozen so a single packet can be passed to multiple downstream
    callers (Gates / LLM bridge) without anyone mutating shared state
    mid-evaluation.

    Attributes:
        trigger_event: The single event that re-entered L3 (e.g.
            ``surface.user_intent``, ``action.result_observed``).
        recent_trace: Frozen tuple of the most recent events (oldest
            first; same order as Recent Trace projection iteration).
        current_turn_id: Turn correlation for the trigger (when known
            from ``trigger_event.correlation`` or
            ``trigger_event.payload``).
        status_board: Folded Status Board (ADR-0009 D6) — watched-repo
            state, last Mac power transition, open actions. Carried on
            the packet so L3 can answer "repo X 现在什么状态" from the
            log instead of shelling out to git mid-turn.
        pending_confirmation: Folded PendingConfirmations (ADR-0012 §3
            D4, packet block 8) — the single live-or-recent
            confirmation ask, plus every `lease_id` a passing gate
            evaluation has consumed. The answer-path grammar hook
            (Step 6) and `format_pending_confirmation_note` both read
            this field.
        action_admissions: Folded ActionAdmissions (ADR-0008 D10) — the
            admitting `gate.evaluated` uid and dispatched uid of every
            non-terminal action.
    """

    trigger_event: Event
    recent_trace: tuple[Event, ...]
    current_turn_id: str | None
    status_board: StatusBoard
    pending_confirmation: PendingConfirmations
    action_admissions: ActionAdmissions
    conversation_history: ConversationHistory | None = None
    authorization_snapshot: AuthorizationSnapshot | None = None
    event_cursor: int | None = None


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

    Reads the Event Log and confirmation/dispatch operational tables under one
    SQLite read transaction, folds the materialized events into projections,
    then bundles the exact cursor, trigger and correlations into a frozen packet.

    Args:
        trigger: The event that re-entered L3 (already appended to
            the log before this call). The trigger is included in the
            packet so downstream callers can inspect its payload
            without re-querying.
        conn: Open Event Log connection.

    Returns:
        Frozen :class:`SituationPacket` ready for the Intent Router,
        Effective Policy resolver, and Gates.
    """
    state = read_decision_snapshot(conn)
    projections = state.projections

    return SituationPacket(
        trigger_event=trigger,
        recent_trace=projections.recent_trace.events,
        current_turn_id=_extract_correlation(trigger, "turn_id"),
        status_board=projections.status_board,
        pending_confirmation=projections.pending_confirmations,
        action_admissions=projections.action_admissions,
        conversation_history=projections.conversation_history,
        authorization_snapshot=state.authorizations,
        event_cursor=state.event_cursor,
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

    None when there is no signal worth spending context on (no repo has
    been observed yet), otherwise a bullet list plus a short directive. One bullet per
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


# --- Pending confirmation note (ADR-0012 §3 D4, packet block 8) -------------


def format_pending_confirmation_note(
    packet: SituationPacket,
    *,
    now_ms: int | None = None,
) -> str | None:
    """Render the live pending confirmation ask as an LLM system note.

    Id-free by design (ADR-0012 §3 D4): `confirmation_id` never appears
    in the rendered text, so an LLM reading this note — on an unrelated
    turn (C4), or one that only paraphrases consent (C6) — has no
    handle it could try to use to act on the pending ask itself. Only
    the answer-path grammar hook (Step 6, exact-sentence match against
    `confirm_grammar.yaml`) can move the slot; this note exists so the
    LLM can *talk about* the ask without being structurally able to
    authorize it.

    Returns None when there is no live slot: no `confirmation.requested`
    has fired, the slot has moved past `pending` (accepted / rejected /
    consumed / superseded), or the recorded `expires_at_ms` has lapsed
    at `now_ms` — a merely-expired ask renders no note, matching D6's
    "expired pending -> ordinary turn, packet note shows no pending"
    rule (:meth:`PendingConfirmationSlot.is_live`).

    Args:
        packet: The packet whose ``pending_confirmation`` is rendered.
        now_ms: Reference "now" in epoch milliseconds. Defaults to the
            wall clock; injected by callers that need a fixed reference
            (mirrors ``format_status_board_note``).
    """
    slot = packet.pending_confirmation.slot
    resolved_now_ms = int(time.time() * _MS_PER_SECOND) if now_ms is None else now_ms
    if slot is None or not slot.is_live(resolved_now_ms):
        return None

    tool_name = slot.snapshot.get("tool_name", "?")
    target = slot.snapshot.get("canonical_target", "?")
    remaining_s = max(0, (slot.expires_at_ms - resolved_now_ms) // _MS_PER_SECOND)
    return (
        "[system context] A confirmation is pending Allen's answer — "
        f"tool={tool_name!r}, target={target!r}, expires in {remaining_s}s. "
        "You cannot execute or authorize this yourself; only Allen's exact "
        "yes/no answer to the runtime's own question can move it. If Allen "
        "asks about it, describe the pending action; do not claim you can "
        "act on it, and do not re-propose the same action unless Allen "
        "asks you to."
    )


__all__ = [
    "DEFAULT_OBSERVER_POLL_INTERVAL_S",
    "SituationPacket",
    "assemble_packet",
    "format_pending_confirmation_note",
    "format_status_board_note",
]
