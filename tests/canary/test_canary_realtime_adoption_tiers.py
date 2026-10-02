"""Canary - the shipped realtime rollout matches the 2026-09-07 tier decision.

Allen adopted "tier A" on 2026-09-07: the twelve switches that carry live-burn
evidence ship on, and the eight that do not ship off.  This canary pins both
halves against ``config/jarvis.yaml`` so neither half drifts silently.

Turning a tier-B switch on is a normal next step, not a violation - but it
needs its own live run first, and flipping it here without one is exactly the
mistake this canary exists to catch.  Tier C is different: each of those
has a recorded reason it must stay off.

    ``v2_sequencer``  ``RealtimeTransportV2.enabled`` is ``false`` in Swift and
                      nothing in the card references it, so the server would
                      build a sequencer and hub no client ever connects to.

``durable_expiry`` and ``barge_in`` (D8's spoken stages, which the
2026-09-05 VoiceProcessingIO burn measured at 15 times their false-candidate
target) were tier C until ADR 0047 retired them with their code.

The evidence for tier A is the live burns ADR 0006 records (4/4, 3/3 and
10/10).

``commentary`` was on from ADR 0040 (2026-09-24) and off from ADR 0045
(2026-09-25): Allen heard "结果回来了" out of nowhere after fast tools. ADR 0116
(2026-10-02) turned it on again in its narrow form, the owner's decision; it
is not pinned off here any more, and its live run on the Mac is still owed.

``response.spoken_streaming`` (docs/plans/speak-as-written-proposal.md,
2026-09-29) is tier B: it ships off until its live run on the Mac.
``response.prefix_warm`` (2026-09-30) is tier B too: one more request per
turn, off until a live run shows the next turn's first request cached.
``response.spoken_streaming.structured`` (ADR 0114, 2026-10-02) is tier B: off
until a live run of the schema answer on the Mac.
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
    "response.spoken_streaming.enabled",
    "response.spoken_streaming.structured",
    "response.prefix_warm.enabled",
    "streaming_output.speak_from_segments",
    "single_audio_ingress.partial_asr.enabled",
    "single_audio_ingress.route_observer.enabled",
    "inherent.v2_sequencer.enabled",
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
    """The switches with no live evidence stay off in the shipped config."""
    realtime = _shipped_realtime()
    on = [path for path in _TIER_B_AND_C_DISABLED if _lookup(realtime, path) is not False]
    assert not on, f"switches enabled without a live run: {on}"
