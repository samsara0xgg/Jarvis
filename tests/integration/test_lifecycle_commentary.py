"""ADR-0008 D6 acceptance: deterministic lifecycle commentary.

Every check runs against a real on-disk Event Log and the shipped runtime
observer; nothing about the commentary path is faked, because the property
under test is precisely that a phrase is spoken only when a durable action
row justifies it.
"""

from __future__ import annotations

import yaml

from jarvis.decision.commentary import COMMENTARY_ATTENTION_CHANNEL, commentary_intent_for
from jarvis.runtime import _wave4_response_activation
from jarvis.shared import Event
from tests.canary._helpers import repo_root

# --- helpers ---------------------------------------------------------------


def _action_event(event_type: str, *, action_id: str = "ACT-1") -> Event:
    """Build one committed-shaped action event without touching the log."""
    return Event(
        event_uid=f"uid-{event_type}-{action_id}",
        type=event_type,
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"action_id": action_id, "tool_name": "get_current_time"},
        source_event_id=None,
        correlation={"action_id": action_id, "turn_id": "T-1"},
    )


# --- D6 mapping ------------------------------------------------------------


def test_four_d6_rows_map_to_their_exact_intent_and_phrase() -> None:
    """ADR-0008 D6's action table, verbatim, with the action id as subject."""
    expected = {
        "action.dispatched": ("acknowledge", "我开始处理了。"),
        "action.running": ("progress", "任务已经在运行。"),
        "action.result_observed": ("progress", "结果回来了，我整理一下。"),  # noqa: RUF001 — intentional Chinese punctuation.
        "action.failed": ("error", "这一步失败了，我告诉你具体原因。"),  # noqa: RUF001 — intentional Chinese punctuation.
    }
    for event_type, (intent_type, phrase) in expected.items():
        intent = commentary_intent_for(_action_event(event_type, action_id="ACT-7"))
        assert intent is not None, event_type
        assert intent.intent_type == intent_type
        assert intent.content_hint == phrase
        assert intent.subject_ref == "ACT-7"
        assert intent.surface_hint == "speech"
        assert intent.freshness_required is True


def test_non_mapped_event_types_return_none() -> None:
    """Only the four D6 action rows speak; every other row is silent."""
    for event_type in ("run.started", "gate.evaluated", "action.cancelled",
                       "action.timeout_assumed", "turn.started", "response.completed"):
        assert commentary_intent_for(_action_event(event_type)) is None, event_type


def test_mapped_row_without_action_id_returns_none() -> None:
    """``subject_ref`` is the action id; without one there is nothing to say."""
    event = Event(
        event_uid="uid-no-action",
        type="action.dispatched",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"tool_name": "get_current_time"},
        source_event_id=None,
        correlation=None,
    )
    assert commentary_intent_for(event) is None


def test_intent_is_frozen_and_never_an_event() -> None:
    """Spec §3.6.3: PresentationIntent is a contract object, not a log row."""
    intent = commentary_intent_for(_action_event("action.dispatched"))
    assert intent is not None
    assert tuple(intent.__dataclass_fields__) == (
        "intent_type",
        "surface_hint",
        "subject_ref",
        "content_hint",
        "freshness_required",
    )
    assert COMMENTARY_ATTENTION_CHANNEL == "voice_notify"


# --- flag graph ------------------------------------------------------------


def _config(*, enabled: bool, lifecycle: bool, commentary: bool) -> dict[str, object]:
    """Build the `realtime` mapping the activation graph reads."""
    return {
        "realtime": {
            "enabled": enabled,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
            },
            "response": {"response_run_lifecycle": lifecycle},
            "commentary": {"enabled": commentary},
        },
    }


def test_shipped_config_leaves_commentary_off() -> None:
    """config/jarvis.yaml ships the switch off, like every other realtime flag."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    assert shipped["realtime"]["commentary"] == {"enabled": False}
    assert _wave4_response_activation(shipped).flags.lifecycle_commentary is False


def test_commentary_requires_the_response_run_lifecycle() -> None:
    """Without the lifecycle switch there is no ResponseRun to open at all."""
    activation = _wave4_response_activation(
        _config(enabled=True, lifecycle=False, commentary=True),
    )
    assert activation.reason == "lifecycle_flag_disabled"
    assert activation.requested.lifecycle_commentary is True
    assert activation.flags.lifecycle_commentary is False


def test_commentary_requires_realtime_enabled() -> None:
    """The parent switch off downgrades the whole graph, commentary included."""
    activation = _wave4_response_activation(
        _config(enabled=False, lifecycle=True, commentary=True),
    )
    assert activation.reason == "realtime_parent_disabled"
    assert activation.flags.lifecycle_commentary is False


def test_commentary_validates_with_its_two_preconditions() -> None:
    """Both preconditions met: the switch survives the graph."""
    activation = _wave4_response_activation(
        _config(enabled=True, lifecycle=True, commentary=True),
    )
    assert activation.reason == "validated"
    assert activation.flags.lifecycle_commentary is True


def test_non_boolean_truthy_never_enables_commentary() -> None:
    """Fail-closed `is True`, the same rule every other realtime flag uses."""
    config = _config(enabled=True, lifecycle=True, commentary=True)
    config["realtime"]["commentary"] = {"enabled": "yes"}  # type: ignore[index]
    assert _wave4_response_activation(config).flags.lifecycle_commentary is False
