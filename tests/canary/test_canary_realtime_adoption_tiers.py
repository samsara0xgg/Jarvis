"""Canary - the shipped realtime rollout matches the 2026-09-07 tier decision.

Allen adopted "tier A" on 2026-09-07: the twelve switches that carry live-burn
evidence ship on, and the eight that do not ship off.  This canary pins both
halves against ``config/jarvis.yaml`` so neither half drifts silently.

Turning a tier-B switch on is a normal next step, not a violation - but it
needs its own live run first, and flipping it here without one is exactly the
mistake this canary exists to catch.  Tier C is different: each of those
has a recorded reason it must stay off.

    ``barge_in``      the 2026-09-05 VoiceProcessingIO burn measured 7.53
                      false candidates/min against ADR-0006 D9's 0.5/min
                      target, a 15x miss that hardware AEC did not close.
    ``v2_sequencer``  ``RealtimeTransportV2.enabled`` is ``false`` in Swift and
                      nothing in the card references it, so the server would
                      build a sequencer and hub no client ever connects to.

``durable_expiry`` was tier C until ADR 0047 retired it with its code.

The evidence for tier A is docs/live-burn-2026-09-03-realtime-wave4.md (4/4),
-wave5.md (3/3) and docs/live-burn-2026-09-04-realtime-post6ed7280.md (10/10).

``commentary`` was on from ADR 0040 (2026-09-24) and is off again since
ADR 0045 (2026-09-25): Allen heard "结果回来了" out of nowhere after fast tools.
"""

from __future__ import annotations

import yaml

from tests.canary._helpers import repo_root

# (dotted path under ``realtime:``, expected shipped value)
_TIER_A_ENABLED = (
    "enabled",
    "concurrency_safety.transactional_event_append",
    "concurrency_safety.lifecycle_terminal_cas",
    "concurrency_safety.confirmation_dispatch_outbox",
    "concurrency_safety.exactly_once_cost_accounting",
    "response.response_run_lifecycle",
    "response.independent_response_cancel",
    "input.intent_pump",
    "streaming_output.enabled",
    "single_audio_ingress.enabled",
)

_TIER_B_AND_C_DISABLED = (
    "response.routine_streaming.enabled",
    "streaming_output.speak_from_segments",
    "single_audio_ingress.partial_asr.enabled",
    "single_audio_ingress.route_observer.enabled",
    "single_audio_ingress.barge_in.enabled",
    "inherent.v2_sequencer.enabled",
    "commentary.enabled",
)


def _shipped_realtime() -> dict[str, object]:
    path = repo_root() / "config" / "jarvis.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["realtime"])


def _lookup(block: dict[str, object], dotted: str) -> object:
    node: object = block
    for segment in dotted.split("."):
        assert isinstance(node, dict), f"realtime.{dotted}: {segment} has no parent map"
        assert segment in node, f"realtime.{dotted}: key {segment} is missing"
        node = node[segment]
    return node


def test_canary_tier_a_switches_ship_enabled() -> None:
    """The ten live-burned switches are on in the shipped config."""
    realtime = _shipped_realtime()
    off = [path for path in _TIER_A_ENABLED if _lookup(realtime, path) is not True]
    assert not off, f"tier-A switches unexpectedly off: {off}"


def test_canary_tier_b_and_c_switches_ship_disabled() -> None:
    """The seven switches with no live evidence, and commentary, stay off in the shipped config."""
    realtime = _shipped_realtime()
    on = [path for path in _TIER_B_AND_C_DISABLED if _lookup(realtime, path) is not False]
    assert not on, f"switches enabled without a live run: {on}"
