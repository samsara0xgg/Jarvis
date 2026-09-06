"""Acceptance for the Inherent action and confirmation projections (ADR-0014 D13/D14).

The fold is exercised directly with plain rows, so every cursor asserted here
is the ``events.id`` the wire carries as ``revision``; the catch-up case runs
the real sequencer over a real Event Log.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from jarvis.runtime.inherent_view_sequencer import (
    _SELECT_RESPONSE_ROWS_SQL,
    ClientLane,
    InherentViewSequencer,
)
from jarvis.shared.realtime import stable_response_group_id
from jarvis.state.event_log import emit_event, open_event_log, read_log_epoch
from jarvis.state.inherent_view import (
    FOLD_EVENT_TYPES,
    RECENT_TERMINAL_ACTION_LIMIT,
    ActionView,
    InherentView,
    ViewTransition,
)
from jarvis.surface.inherent_presenter import (
    SnapshotPlan,
    build_snapshot_plan,
    delta_payload,
)
from jarvis.surface.inherent_protocol import (
    ServerEnvelope,
    SnapshotBeginPayload,
    SnapshotPagePayload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

BASE_MS = 1_788_200_000_000
FAR_FUTURE_MS = 2_000_000_000_000  # a deadline no wall clock in a test run reaches
ACTION_WIRE_KEYS = {
    "action_id",
    "response_group_id",
    "task_id",
    "state",
    "label",
    "target",
    "revision",
    "cancellable",
    "cancel_request",
}
CONFIRMATION_WIRE_KEYS = {
    "confirmation_id",
    "response_group_id",
    "action_id",
    "summary",
    "target",
    "risk",
    "options",
    "expires_at_ms",
    "revision",
}
TERMINAL_TYPES = ("action.result_observed", "action.failed", "action.timeout_assumed")
CLEANUP_TRIO = {
    "worker.quiesced": "quiesced",
    "action.cleanup_completed": "completed",
    "action.cleanup_failed": "quarantined",
}


class _Fold:
    """Feed rows to one fold with ascending cursors and readable timestamps."""

    def __init__(self) -> None:
        self.view = InherentView()
        self.cursor = 0

    def row(  # noqa: PLR0913 — one keyword per row column the fold reads.
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        ts_epoch_ms: int | None = None,
        uid: str | None = None,
        source_event_id: str | None = None,
        correlation: dict[str, Any] | None = None,
    ) -> ViewTransition | None:
        self.cursor += 1
        return self.view.fold(
            cursor=self.cursor,
            event_uid=uid or f"uid{self.cursor}",
            event_type=event_type,
            ts_epoch_ms=BASE_MS + self.cursor if ts_epoch_ms is None else ts_epoch_ms,
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def changes(self, event_type: str, payload: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ANN401, E501
        """Fold one row and return the wire changes the presenter makes of it."""
        transition = self.row(event_type, payload, **kwargs)
        return [] if transition is None else delta_payload(transition)["changes"]

    def action(self, action_id: str) -> ActionView:
        checkpoint = self.view.checkpoint(through_cursor=self.cursor)
        return next(
            action for action in checkpoint.actions if action.action_id == action_id
        )


def _proposed(
    action_id: str,
    tool_name: str = "read_file",
    *,
    target: str | None = None,
    turn_id: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_id": action_id,
        "tool_name": tool_name,
        "caller_principal": "jarvis_llm",
        "risk_level": "low",
        "arguments": arguments or {},
    }
    if target is not None:
        payload["target_entity_ref"] = target
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return payload


def _ack(target_action_id: str, status: str) -> dict[str, Any]:
    return {
        "action_id": "ACANCEL",
        "semantics": "ack",
        "tool_output": json.dumps({"target_action_id": target_action_id, "status": status}),
    }


def _requested(
    confirmation_id: str, *, expires_at_ms: int = BASE_MS + 60_000, risk: str = "high",
) -> dict[str, Any]:
    return {
        "confirmation_id": confirmation_id,
        "action_snapshot": {
            "tool_name": "write_file",
            "caller": "jarvis_llm",
            "canonical_target": "file:/tmp/note.md",
            "target_entity_ref": "file:/tmp/note.md",
            "risk_level": risk,
            "args_meta": {"content_sha256": "abc", "content_bytes": 3},
        },
        "template_line": "要我写入 /tmp/note.md 吗?",
        "expires_at_ms": expires_at_ms,
    }


def _answer(confirmation_id: str) -> dict[str, Any]:
    return {
        "confirmation_id": confirmation_id,
        "utterance_raw": "是",
        "grammar_rule_id": "confirm_yes_v1",
    }


def _open_payload(response: str, group: str, turn: str) -> dict[str, Any]:
    return {
        "turn_id": turn,
        "query": "q",
        "kind": "text",
        "response_id": response,
        "response_group_id": group,
        "phase": "final",
        "channel": "document",
    }


def _cancel_proposal(fold: _Fold) -> None:
    """The A5 request row itself, as L3 writes it (target frozen into arguments)."""
    proposal = fold.row(
        "action.proposed",
        _proposed(
            "ACANCEL",
            "cancel_action",
            target="action:ATARGET",
            arguments={"target_action_id": "ATARGET", "reason": "停下"},
        ),
        uid="uid_proposal",
    )
    assert proposal is not None


def _cancel_verdict(fold: _Fold, outcome: str = "pass") -> None:
    """Its Pre-action Gate verdict, carrying both join keys the emitter sets."""
    fold.row(
        "gate.evaluated",
        {
            "gate": "pre_action",
            "outcome": outcome,
            "reasons": [] if outcome == "pass" else ["target_already_terminal"],
            "action_id": "ACANCEL",
        },
        source_event_id="uid_proposal",
    )


def _cancel_trail(fold: _Fold, target: str, *, outcome: str = "pass") -> None:
    """Emit the A5 rows up to the request's verdict, exactly as L3 writes them."""
    assert target == "ATARGET"
    _cancel_proposal(fold)
    _cancel_verdict(fold, outcome)


def _encoder(connection_id: str) -> Any:  # noqa: ANN401 — the presenter's FrameEncoder alias.
    def encode(message_type: str, message_id: str, payload: Mapping[str, Any]) -> str:
        return ServerEnvelope(
            protocol_version=2,
            message_type=message_type,
            message_id=message_id,
            delivery_class="protocol",
            connection_id=connection_id,
            log_epoch="Lfixture0001",
            boot_id="Bfixture0001",
            sent_at_ms=BASE_MS,
            payload=dict(payload),
        ).model_dump_json()

    return encode


def _decode(plan: SnapshotPlan) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return json.loads(plan.begin_frame), [json.loads(f) for f in plan.page_frames]


# --- actions ------------------------------------------------------------------


def test_every_reachable_action_state_upserts_once_at_its_own_cursor() -> None:
    """D13: the row's type is the canonical state and its events.id is the revision."""
    fold = _Fold()
    changes = [
        fold.changes(
            "action.proposed", _proposed("A1", "write_file", target="file:/tmp/x", turn_id="T1"),
        ),
        fold.changes("action.authorized", {"action_id": "A1"}),
        fold.changes("action.dispatched", {"action_id": "A1"}),
        fold.changes("action.running", {"action_id": "A1"}),
        fold.changes("action.result_observed", {"action_id": "A1", "semantics": "sync"}),
    ]

    assert [[c["kind"] for c in delta] for delta in changes] == [["action.upsert"]] * 5
    upserts = [delta[0] for delta in changes]
    assert [u["state"] for u in upserts] == [
        "proposed", "authorized", "dispatched", "running", "result_observed",
    ]
    assert [u["revision"] for u in upserts] == [1, 2, 3, 4, 5]
    assert {u["label"] for u in upserts} == {"write_file"}
    assert {u["target"] for u in upserts} == {"file:/tmp/x"}
    assert {u["response_group_id"] for u in upserts} == {stable_response_group_id("T1")}
    assert set(upserts[0]) == {"kind", *ACTION_WIRE_KEYS}
    assert "freshness_ms" not in upserts[0]
    assert all(u["cancel_request"] is None for u in upserts)
    assert fold.action("A1").result_available is True


def test_each_terminal_type_carries_its_reason_code_and_never_free_text() -> None:
    """D13: failure_code is payload.reason; payload.error never reaches the view."""
    states = {}
    for index, event_type in enumerate(("action.failed", "action.timeout_assumed",
                                        "action.cancelled")):
        fold = _Fold()
        action_id = f"A{index}"
        fold.row("action.proposed", _proposed(action_id, "spawn_worker"))
        fold.row("action.dispatched", {"action_id": action_id})
        upsert = fold.changes(
            event_type,
            {
                "action_id": action_id,
                "reason": "budget_exhausted",
                "error": "Traceback (most recent call last): secrets",
                "task_id": "TASK7",
            },
        )[0]
        view = fold.action(action_id)
        states[event_type] = (upsert["state"], view.failure_code, view.task_id)

    assert states == {
        "action.failed": ("failed", "budget_exhausted", None),
        "action.timeout_assumed": ("timeout_assumed", "budget_exhausted", "TASK7"),
        "action.cancelled": ("cancelled", "budget_exhausted", None),
    }


def test_cancellable_is_true_exactly_between_dispatch_and_the_first_terminal() -> None:
    """D13: the open-set rule the L3 cancel resolver reads, computed in the fold."""
    for terminal in (*TERMINAL_TYPES, "action.cancelled"):
        fold = _Fold()
        before = [
            fold.changes("action.proposed", _proposed("A9", "spawn_worker"))[0]["cancellable"],
            fold.changes("action.authorized", {"action_id": "A9"})[0]["cancellable"],
        ]
        during = [
            fold.changes("action.dispatched", {"action_id": "A9"})[0]["cancellable"],
            fold.changes("action.running", {"action_id": "A9"})[0]["cancellable"],
        ]
        payload = {"action_id": "A9", "semantics": "sync"} if terminal == (
            "action.result_observed"
        ) else {"action_id": "A9"}
        after = fold.changes(terminal, payload)[0]["cancellable"]

        assert before == [False, False]
        assert during == [True, True]
        assert after is False


def test_cleanup_state_folds_the_trio_and_defaults_to_none() -> None:
    """D9's cleanup trio is fold-internal truth: no wire key, so no envelope."""
    seen = {}
    for event_type in CLEANUP_TRIO:
        fold = _Fold()
        fold.row("action.proposed", _proposed("A5", "spawn_worker"))
        fold.row("action.dispatched", {"action_id": "A5"})
        default = fold.action("A5").cleanup_state
        transition = fold.row(
            event_type,
            {
                "action_id": "A5",
                "worker_epoch": 1,
                "verification_outcome": "clean",
                "reason": "dirty_tree",
            },
        )
        seen[event_type] = (default, fold.action("A5").cleanup_state, transition)

    assert seen == {
        event_type: ("none", expected, None) for event_type, expected in CLEANUP_TRIO.items()
    }


def test_a_refused_proposal_is_retired_at_its_verdict() -> None:
    """L3 dispatches nothing after a non-pass outcome, so `proposed` never resolves."""
    fold = _Fold()
    refused = []
    for index, outcome in enumerate(("refuse", "confirm_required")):
        refused.append(fold.changes("action.proposed", _proposed(f"AR{index}", "write_file"))[0])
        fold.row(
            "gate.evaluated",
            {
                "gate": "pre_action",
                "outcome": outcome,
                "reasons": [f"{outcome}_reason"],
                "action_id": f"AR{index}",
            },
        )
    fold.row("action.proposed", _proposed("AKEEP", "read_file"))
    fold.row(
        "gate.evaluated",
        {"gate": "pre_action", "outcome": "pass", "reasons": [], "action_id": "AKEEP"},
    )
    fold.row("action.authorized", {"action_id": "AKEEP"})
    kept = [action.action_id for action in fold.view.checkpoint(through_cursor=fold.cursor).actions]
    # An accepted confirmation re-proposes; the fold rebuilds from that row.
    reproposed = fold.changes("action.proposed", _proposed("AR1", "write_file"))[0]

    assert [change["state"] for change in refused] == ["proposed", "proposed"]
    assert kept == ["AKEEP"]
    assert (reproposed["state"], reproposed["label"]) == ("proposed", "write_file")
    assert [
        action.action_id for action in fold.view.checkpoint(through_cursor=fold.cursor).actions
    ] == ["AKEEP", "AR1"]


def test_terminal_actions_are_bounded_and_open_ones_are_never_evicted() -> None:
    """D13's bounded projection: the fold never grows with the log."""
    fold = _Fold()
    fold.row("action.proposed", _proposed("AOPEN", "spawn_worker"))
    fold.row("action.dispatched", {"action_id": "AOPEN"})
    for index in range(RECENT_TERMINAL_ACTION_LIMIT + 5):
        fold.row("action.proposed", _proposed(f"AT{index}"))
        fold.row("action.result_observed", {"action_id": f"AT{index}", "semantics": "sync"})

    kept = [action.action_id for action in fold.view.checkpoint(through_cursor=fold.cursor).actions]

    assert "AOPEN" in kept
    assert len(kept) == RECENT_TERMINAL_ACTION_LIMIT + 1
    assert kept[-1] == f"AT{RECENT_TERMINAL_ACTION_LIMIT + 4}"
    assert "AT0" not in kept


# --- the A5 cancel trail ------------------------------------------------------


def test_the_cancel_request_walks_received_authorized_quiescing_resolved() -> None:
    """D13: only action.cancelled moves the target; the request has its own trail."""
    fold = _Fold()
    fold.row("action.proposed", _proposed("ATARGET", "spawn_worker"))
    fold.row("action.dispatched", {"action_id": "ATARGET"})
    fold.row("action.running", {"action_id": "ATARGET"})

    walk = []
    _cancel_proposal(fold)
    walk.append(fold.action("ATARGET").cancel_request)
    _cancel_verdict(fold)
    walk.append(fold.action("ATARGET").cancel_request)
    fold.row("action.authorized", {"action_id": "ACANCEL"})
    fold.row("action.dispatched", {"action_id": "ACANCEL"})
    walk.append(fold.action("ATARGET").cancel_request)
    fold.row("action.result_observed", _ack("ATARGET", "accepted"))
    walk.append(fold.action("ATARGET").cancel_request)
    cancelled = fold.changes("action.cancelled", {"action_id": "ATARGET", "reason": "requested"})
    walk.append(fold.action("ATARGET").cancel_request)

    assert [step.state for step in walk if step is not None] == [
        "received", "authorized", "quiescing", "quiescing", "resolved",
    ]
    assert [step.revision_cursor for step in walk if step is not None] == [4, 5, 7, 7, 9]
    assert walk[-1] is not None
    assert (walk[-1].reason_code, walk[-1].request_id) == ("cancelled", "ACANCEL")
    assert [c["kind"] for c in cancelled] == ["action.upsert"]
    assert (cancelled[0]["state"], cancelled[0]["action_id"]) == ("cancelled", "ATARGET")
    assert cancelled[0]["cancel_request"] == {
        "request_id": "ACANCEL",
        "state": "resolved",
        "revision_cursor": 9,
        "reason_code": "cancelled",
    }
    cancel_action = fold.action("ACANCEL")
    assert (cancel_action.action_type, cancel_action.safe_target_ref) == (
        "cancel_action", "action:ATARGET",
    )


def test_a_refusing_gate_rejects_the_request_and_never_reaches_l4() -> None:
    """The verdict is joined by its source row, which every gate.evaluated carries."""
    fold = _Fold()
    fold.row("action.proposed", _proposed("ATARGET", "spawn_worker"))
    fold.row("action.dispatched", {"action_id": "ATARGET"})
    fold.row(
        "action.proposed",
        _proposed(
            "ACANCEL", "cancel_action", target="action:ATARGET",
            arguments={"target_action_id": "ATARGET"},
        ),
        uid="uid_proposal",
    )
    # No `action_id` payload key: the optional convenience key is absent and
    # `events.source_event_id` is the durable join.
    transition = fold.row(
        "gate.evaluated",
        {"gate": "pre_action", "outcome": "refuse", "reasons": ["target_already_terminal"]},
        source_event_id="uid_proposal",
    )
    cancel = fold.action("ATARGET").cancel_request

    assert transition is None
    assert cancel is not None
    assert (cancel.state, cancel.reason_code) == ("rejected", "target_already_terminal")


def test_the_two_unhappy_ack_statuses_and_already_terminal_settle_the_request() -> None:
    """ADR-0008 F9: only the ack's `status` key is read, never its other fields."""
    settled = {}
    for status in ("unsupported", "unconfirmed", "already_terminal"):
        fold = _Fold()
        fold.row("action.proposed", _proposed("ATARGET", "spawn_worker"))
        fold.row("action.dispatched", {"action_id": "ATARGET"})
        _cancel_trail(fold, "ATARGET")
        fold.row("action.dispatched", {"action_id": "ACANCEL"})
        fold.row("action.result_observed", _ack("ATARGET", status))
        cancel = fold.action("ATARGET").cancel_request
        assert cancel is not None
        settled[status] = (cancel.state, cancel.reason_code)

    assert settled == {
        "unsupported": ("rejected", "unsupported"),
        "unconfirmed": ("failed", "unconfirmed"),
        "already_terminal": ("resolved", "already_terminal"),
    }


def test_the_requests_own_terminal_fails_it_with_its_reason_code() -> None:
    """`failed` is a cancel handler or dispatch-debt failure with a safe reason (D13)."""
    fold = _Fold()
    fold.row("action.proposed", _proposed("ATARGET", "spawn_worker"))
    fold.row("action.dispatched", {"action_id": "ATARGET"})
    _cancel_trail(fold, "ATARGET")
    fold.row("action.dispatched", {"action_id": "ACANCEL"})
    fold.row(
        "action.failed",
        {"action_id": "ACANCEL", "reason": "handler_raised", "error": "Traceback ..."},
    )
    cancel = fold.action("ATARGET").cancel_request

    assert cancel is not None
    assert (cancel.state, cancel.reason_code) == ("failed", "handler_raised")


def test_a_target_that_finishes_first_resolves_the_request_as_completed_before_cancel() -> None:
    """D13: a normal completion winning the race still resolves the request."""
    fold = _Fold()
    fold.row("action.proposed", _proposed("ATARGET", "spawn_worker"))
    fold.row("action.dispatched", {"action_id": "ATARGET"})
    _cancel_trail(fold, "ATARGET")
    fold.row("action.dispatched", {"action_id": "ACANCEL"})
    fold.row("action.result_observed", {"action_id": "ATARGET", "semantics": "sync"})
    cancel = fold.action("ATARGET").cancel_request

    assert cancel is not None
    assert (cancel.state, cancel.reason_code) == ("resolved", "completed_before_cancel")


# --- the confirmation slot ----------------------------------------------------


def test_a_confirmation_upserts_the_nine_wire_keys_and_no_snapshot_internals() -> None:
    """D14: template_line is the summary; args_meta and the lease never travel."""
    fold = _Fold()
    changes = fold.changes(
        "confirmation.requested",
        _requested("CONF1"),
        correlation={"action_id": "AWRITE", "turn_id": "T9", "run_id": "R1"},
    )

    assert [c["kind"] for c in changes] == ["confirmation.upsert"]
    upsert = changes[0]
    assert set(upsert) == {"kind", *CONFIRMATION_WIRE_KEYS}
    assert upsert["summary"] == "要我写入 /tmp/note.md 吗?"
    assert upsert["target"] == "file:/tmp/note.md"
    assert (upsert["risk"], upsert["options"]) == ("high", ["accept", "reject"])
    assert (upsert["action_id"], upsert["revision"]) == ("AWRITE", 1)
    assert upsert["response_group_id"] == stable_response_group_id("T9")
    assert upsert["expires_at_ms"] == BASE_MS + 60_000


def test_an_answer_clears_the_slot_and_a_stale_id_changes_nothing() -> None:
    """D14 mirrors PendingConfirmations: only the live id moves the slot."""
    cleared = {}
    for event_type, reason in (
        ("confirmation.accepted", "accepted"),
        ("confirmation.rejected", "rejected"),
    ):
        fold = _Fold()
        fold.row("confirmation.requested", _requested("CONF1"))
        stale = fold.row(event_type, _answer("CONF_OTHER"))
        changes = fold.changes(event_type, _answer("CONF1"))
        checkpoint = fold.view.checkpoint(through_cursor=fold.cursor)
        cleared[reason] = (stale, changes, checkpoint.pending_confirmation)

    for reason, (stale, changes, slot) in cleared.items():
        assert stale is None
        assert changes == [
            {"kind": "confirmation.cleared", "confirmation_id": "CONF1",
             "reason": reason, "revision": 3},
        ]
        assert slot is None


def test_a_supersede_is_one_delta_clearing_a_before_upserting_b() -> None:
    """D14: no confirmation.superseded event exists; the order is the contract."""
    fold = _Fold()
    fold.row("confirmation.requested", _requested("CONF_A"))
    changes = fold.changes("confirmation.requested", _requested("CONF_B"))

    assert [c["kind"] for c in changes] == ["confirmation.cleared", "confirmation.upsert"]
    assert (changes[0]["confirmation_id"], changes[0]["reason"]) == ("CONF_A", "superseded")
    assert changes[1]["confirmation_id"] == "CONF_B"
    # Both carry B's request cursor: the clear satisfies the adopter's
    # `revision >= pending.revision`, and B outranks the slot it replaced.
    assert changes[0]["revision"] == changes[1]["revision"] == 2
    assert changes[1]["revision"] > 1


def test_lazy_expiry_clears_the_slot_on_the_first_row_past_the_deadline() -> None:
    """D14: the fold has no clock, so the folded row's own timestamp judges it."""
    fold = _Fold()
    deadline = BASE_MS + 5_000
    fold.row("confirmation.requested", _requested("CONF1", expires_at_ms=deadline))
    before = fold.row("action.proposed", _proposed("A1"), ts_epoch_ms=deadline - 1)
    changes = fold.changes(
        "action.proposed", _proposed("A2"), ts_epoch_ms=deadline,
    )
    late = fold.changes("confirmation.accepted", _answer("CONF1"), ts_epoch_ms=deadline + 1)

    assert before is not None
    assert [c["kind"] for c in delta_payload(before)["changes"]] == ["action.upsert"]
    assert [c["kind"] for c in changes] == ["confirmation.cleared", "action.upsert"]
    assert (changes[0]["reason"], changes[0]["revision"]) == ("expired", 3)
    assert late == []


# --- response lifecycle -------------------------------------------------------


def test_the_lifecycle_is_reported_from_the_response_rows_with_its_reason() -> None:
    """ADR-0014 §16.9: surface.response_emitted never stands in for a terminal."""
    reported = {}
    for event_type, lifecycle in (
        ("response.completed", "completed"),
        ("response.cancelled", "cancelled"),
        ("response.failed", "failed"),
    ):
        fold = _Fold()
        opened = fold.changes("surface.response_open", _open_payload("RESP1", "RGRP1", "T1"))
        started = fold.changes(
            "response.started", {"response_id": "RESP1", "response_group_id": "RGRP1"},
        )
        emitted = fold.changes(
            "surface.response_emitted",
            {**_open_payload("RESP1", "RGRP1", "T1"), "text": "hi"},
        )
        after_emitted = fold.view.checkpoint(
            through_cursor=fold.cursor,
        ).groups[0].responses[0].lifecycle
        terminal = fold.changes(
            event_type,
            {"response_id": "RESP1", "response_group_id": "RGRP1", "turn_id": "T1",
             "reason": "interrupted_by_user", "response_hash": "h"},
        )
        reported[lifecycle] = (opened[0]["lifecycle"], started, emitted, after_emitted, terminal)

    for lifecycle, row in reported.items():
        opened_lifecycle, started, emitted, after_emitted, terminal = row
        assert opened_lifecycle == "generating"
        assert started == []  # already generating: nothing the wire does not have
        assert [c["kind"] for c in emitted] == ["response.delivery"]
        assert after_emitted == "generating"
        assert terminal == [
            {"kind": "response.lifecycle", "response_id": "RESP1", "lifecycle": lifecycle,
             "terminal_reason": "interrupted_by_user", "revision": 4},
        ]


def test_a_legacy_bound_response_with_no_lifecycle_row_stays_generating() -> None:
    """cli_render's uuid5 binding emits no response.* row; the fold invents none."""
    fold = _Fold()
    fold.row("surface.response_open", _open_payload("RESP_LEGACY", "RGRP1", "T1"))
    fold.row("surface.response_emitted", {**_open_payload("RESP_LEGACY", "RGRP1", "T1"),
                                          "text": "hi"})
    orphan = fold.row(
        "response.completed",
        {"response_id": "RESP_OTHER", "response_group_id": "RGRP2", "turn_id": "T2",
         "response_hash": "h"},
    )
    checkpoint = fold.view.checkpoint(through_cursor=fold.cursor)

    assert orphan is None
    assert checkpoint.groups[0].responses[0].lifecycle == "generating"
    assert checkpoint.groups[0].responses[0].panel_stream == "closed"


# --- the snapshot -------------------------------------------------------------


def _three_section_checkpoint() -> _Fold:
    fold = _Fold()
    fold.row("surface.response_open", _open_payload("RESP1", "RGRP1", "T1"))
    fold.row("action.proposed", _proposed("A1", "write_file", target="file:/tmp/x"))
    fold.row("action.dispatched", {"action_id": "A1"})
    fold.row(
        "confirmation.requested", _requested("CONF1"), correlation={"action_id": "A1"},
    )
    return fold


def test_the_snapshot_advertises_the_three_sections_it_produced() -> None:
    """D8: per-section page counts, page_index restarting at 0 in each section."""
    fold = _three_section_checkpoint()
    checkpoint = fold.view.checkpoint(through_cursor=fold.cursor)

    plan = build_snapshot_plan(
        checkpoint, snapshot_id="Ssections", view_schema_version=1, encode=_encoder("C1"),
    )
    begin, pages = _decode(plan)

    assert begin["payload"]["section_order"] == [
        "response_groups", "actions", "pending_confirmation",
    ]
    assert begin["payload"]["counts"] == {
        "response_groups": 1, "actions": 1, "pending_confirmation": 1,
    }
    assert [page["payload"]["section"] for page in pages] == [
        "response_groups", "actions", "pending_confirmation",
    ]
    assert [page["payload"]["page_index"] for page in pages] == [0, 0, 0]
    assert [page["message_id"] for page in pages] == [
        "Ssections:page:response_groups:0",
        "Ssections:page:actions:0",
        "Ssections:page:pending_confirmation:0",
    ]
    SnapshotBeginPayload.model_validate(begin["payload"])
    items = [SnapshotPagePayload.model_validate(page["payload"]).items for page in pages]
    assert set(items[1][0]) == ACTION_WIRE_KEYS
    assert set(items[2][0]) == CONFIRMATION_WIRE_KEYS
    assert items[1][0]["cancellable"] is True


def test_page_index_restarts_at_zero_in_every_section() -> None:
    """D8: the adopter keys staged pages by (section, page_index)."""
    fold = _Fold()
    for index in range(12):
        fold.row(
            "surface.response_open", _open_payload(f"RESP{index}", f"RGRP{index}", f"T{index}"),
        )
        fold.row(
            "surface.response_chunk",
            {**_open_payload(f"RESP{index}", f"RGRP{index}", f"T{index}"),
             "sequence": 0, "text": "x" * 12_000},
        )
    fold.row("action.proposed", _proposed("A1", "write_file"))

    plan = build_snapshot_plan(
        fold.view.checkpoint(through_cursor=fold.cursor),
        snapshot_id="Smulti", view_schema_version=1, encode=_encoder("C1"),
    )
    begin, pages = _decode(plan)
    indices = [(page["payload"]["section"], page["payload"]["page_index"]) for page in pages]

    assert begin["payload"]["counts"]["response_groups"] > 1
    assert begin["payload"]["counts"]["actions"] == 1
    assert indices[-1] == ("actions", 0)
    assert [index for section, index in indices if section == "response_groups"] == list(
        range(begin["payload"]["counts"]["response_groups"]),
    )
    assert len(indices) == sum(begin["payload"]["counts"].values())


def test_the_content_hash_covers_every_section_page_in_send_order() -> None:
    """D8: SHA-256 over the exact page frames, section then page."""
    fold = _three_section_checkpoint()
    plan = build_snapshot_plan(
        fold.view.checkpoint(through_cursor=fold.cursor),
        snapshot_id="Shash",
        view_schema_version=1,
        encode=_encoder("C1"),
    )

    digest = hashlib.sha256()
    for frame in plan.page_frames:
        digest.update(frame.encode("utf-8"))

    assert len(plan.page_frames) == 3
    assert plan.content_hash == digest.hexdigest()
    assert json.loads(plan.end_frame)["payload"]["content_hash"] == plan.content_hash


def test_an_empty_section_is_omitted_from_section_order_and_counts() -> None:
    """The Swift verifier iterates section_order only; an idle daemon sends neither."""
    fold = _Fold()
    fold.row("surface.response_open", _open_payload("RESP1", "RGRP1", "T1"))
    with_groups = build_snapshot_plan(
        fold.view.checkpoint(through_cursor=fold.cursor),
        snapshot_id="Sgroups", view_schema_version=1, encode=_encoder("C1"),
    )
    idle = build_snapshot_plan(
        InherentView().checkpoint(through_cursor=0),
        snapshot_id="Sidle", view_schema_version=1, encode=_encoder("C1"),
    )

    assert json.loads(with_groups.begin_frame)["payload"]["section_order"] == ["response_groups"]
    assert json.loads(with_groups.begin_frame)["payload"]["counts"] == {"response_groups": 1}
    assert json.loads(idle.begin_frame)["payload"]["section_order"] == []
    assert json.loads(idle.begin_frame)["payload"]["counts"] == {}
    assert idle.page_frames == ()


# --- the sequencer ------------------------------------------------------------


def test_the_row_query_selects_exactly_the_types_the_fold_reads() -> None:
    """The fold is fed by that query and sees nothing else (D9 step 2)."""
    selected = set(re.findall(r"'([a-z_]+\.[a-z_]+)'", _SELECT_RESPONSE_ROWS_SQL))

    assert selected == set(FOLD_EVENT_TYPES)
    assert "source_event_id, correlation_json" in _SELECT_RESPONSE_ROWS_SQL


def test_the_catch_up_replays_action_and_confirmation_deltas_in_cursor_order(
    tmp_path: Path,
) -> None:
    """D8 steps 5-6: the same fold rules produce the deltas the client would have seen."""
    conn = open_event_log(tmp_path / "mac_events.db")
    emit_event(conn, type="action.proposed", payload=_proposed("A0", "read_file"))
    sequencer = InherentViewSequencer(
        conn, log_epoch=read_log_epoch(conn), boot_id="Bfixture0001",
    )
    frames: list[tuple[str, int]] = []
    lane = ClientLane(
        connection_id="C1",
        enqueue=lambda frame, cursor: frames.append((frame, cursor)),
    )
    staging = sequencer.begin_snapshot(lane)

    emit_event(conn, type="action.dispatched", payload={"action_id": "A0"})
    request = emit_event(
        conn,
        type="confirmation.requested",
        payload=_requested("CONF1", expires_at_ms=FAR_FUTURE_MS),
        correlation={"action_id": "A0", "turn_id": "T1"},
    )
    emit_event(
        conn,
        type="confirmation.accepted",
        payload=_answer("CONF1"),
        source_event_id=request.event_uid,
    )
    replayed = sequencer.complete_snapshot(lane, staging)
    conn.close()

    payloads = [json.loads(frame)["payload"]["changes"] for frame, _ in frames]
    assert replayed == 3
    assert [cursor for _, cursor in frames] == [2, 3, 4]
    assert [[change["kind"] for change in changes] for changes in payloads] == [
        ["action.upsert"], ["confirmation.upsert"], ["confirmation.cleared"],
    ]
    assert payloads[0][0]["state"] == "dispatched"
    assert payloads[0][0]["revision"] == 2  # the row's own events.id, not a fixture counter
    assert payloads[1][0]["action_id"] == "A0"
    assert payloads[2][0]["reason"] == "accepted"
    assert staging.checkpoint.actions[0].canonical_state == "proposed"
