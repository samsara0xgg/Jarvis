"""ADR-0018 Claude collector — limit resets, from the route's recorded answers.

The two bodies are the route's real ``cedar_ember`` blocks of 2026-09-25:
without Claude Code's client headers it answers ``ineligible_reason:
"surface"``; with them it lists the Opus 5.5 launch grant. Each check
asserts the snapshot data ``GET /inherent/usage`` serves (what the Usage
page reads) or the request the collector sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.surface import usage_observer

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest

Sent = list[tuple[str, dict[str, str]]]

WINDOWS: dict[str, Any] = {
    "five_hour": {"utilization": 27.0, "resets_at": "2026-09-26T03:40:00.534247+00:00"},
    "seven_day": {"utilization": 50.0, "resets_at": "2026-09-29T12:00:00.534276+00:00"},
}
INELIGIBLE: dict[str, Any] = {
    "eligible": False, "ineligible_reason": "surface", "at_limit": False, "exhausted": [],
    "grants": [], "next_grant_id": None, "weekly_resets_at": None,
}
ELIGIBLE: dict[str, Any] = {
    "eligible": True, "ineligible_reason": None, "at_limit": False, "exhausted": [],
    "grants": [{
        "id": "opus55-launch-promax-20260921", "resets_total": 1, "resets_left": 1,
        "starts_at": "2026-09-22T16:00:00+00:00", "ends_at": "2026-10-22T16:00:00+00:00",
        "paused": False, "usable_now": True,
    }],
    "next_grant_id": "opus55-launch-promax-20260921",
    "weekly_resets_at": "2026-09-29T12:00:00+00:00",
}
CREDENTIALS: dict[str, Any] = {
    "claudeAiOauth": {"accessToken": "t", "rateLimitTier": "default_claude_max_20x"},
}


def _collect(
    monkeypatch: pytest.MonkeyPatch, cedar_ember: dict[str, Any],
) -> tuple[dict[str, Any], Sent]:
    sent: Sent = []

    def answer(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del timeout_s
        sent.append((url, dict(headers)))
        return {**WINDOWS, "cedar_ember": cedar_ember}

    monkeypatch.setattr(usage_observer, "_read_claude_credentials", lambda: CREDENTIALS)
    monkeypatch.setattr(usage_observer, "_get_json", answer)
    snapshot = usage_observer.collect_claude(timeout_s=1)
    assert snapshot.status == "ok"
    return snapshot.data, sent


def test_asks_for_resets_as_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request carries the resets query and Claude Code's headers, which set the surface."""
    _, sent = _collect(monkeypatch, ELIGIBLE)
    url, headers = sent[0]
    assert url.endswith("/api/oauth/usage?cedar_ember=1&skip_spend=1")
    assert headers["User-Agent"].startswith("claude-cli/")
    assert headers["x-app"] == "cli"


def test_eligible_grant_gives_resets_left_and_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """One grant, one reset left: reset_credits 1 until its ends_at; the windows unchanged."""
    data, _ = _collect(monkeypatch, ELIGIBLE)
    assert data["reset_credits"] == 1
    assert data["reset_ends_at"] == "2026-10-22T16:00:00+00:00"
    assert [w["key"] for w in data["windows"]] == ["five_hour", "seven_day"]


def test_ineligible_surface_shows_no_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A surface-ineligible answer adds no reset fields, so the Usage page shows none."""
    data, _ = _collect(monkeypatch, INELIGIBLE)
    assert "reset_credits" not in data
    assert "reset_ends_at" not in data
