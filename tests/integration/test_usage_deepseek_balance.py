"""ADR-0018 DeepSeek collector — one balance per currency, whatever order the API lists them.

``/user/balance`` returns a USD and a CNY row once both were topped up, in no
fixed order; taking the first row flipped the Usage card between them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.surface import usage_observer

if TYPE_CHECKING:
    import pytest

ROWS: list[dict[str, Any]] = [
    {"currency": "USD", "total_balance": "-0.10"},
    {"currency": "CNY", "total_balance": "19.97"},
]


def test_both_currencies_in_either_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Either order gives the same snapshot, so the card neither flips nor re-emits."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    snapshots = []
    for rows in (ROWS, ROWS[::-1]):
        body = {"is_available": True, "balance_infos": rows}
        monkeypatch.setattr(usage_observer, "_get_json", lambda *_a, body=body, **_k: body)
        snapshots.append(usage_observer.collect_deepseek(timeout_s=1))
    assert snapshots[0].data == {"balances": {"CNY": 19.97, "USD": -0.1}, "is_available": True}
    assert snapshots[0].same_state(snapshots[1])
