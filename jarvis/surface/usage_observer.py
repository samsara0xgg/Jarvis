"""Usage observer — AI plan quota and API balance perception.

An L5 input adapter under the ``observer`` principal, shaped like
:mod:`jarvis.surface.repo_observer`: one poll produces a *total* snapshot
per service, and ``usage.state_observed`` is emitted only when a service's
snapshot changed (spec §3.6.1 emit-on-change). The change-baseline is
recovered from the event log, never from process memory.

Services and their sources (all verified live 2026-09-13):

- ``claude``   — claude.ai OAuth usage (``/api/oauth/usage``); token from the
  macOS Keychain item Claude Code writes, file fallback.
- ``codex``    — ChatGPT ``wham/usage``; token from ``~/.codex/auth.json``.
- ``openai``   — official org Costs + Usage APIs; needs ``OPENAI_ADMIN_KEY``.
- ``deepseek`` — official ``/user/balance``.
- ``minimax``  — no balance API exists; the balance is *estimated* from a
  configured top-up anchor minus the TTS characters ``tts.usage_observed``
  has recorded since that anchor (folded from the event log on the loop
  thread, no network).

Secrets never enter a payload: only percentages, dollars, timestamps and
plan labels are stored. Every remote failure collapses to a snapshot with
``status`` ``error`` (or ``unconfigured`` when a credential is absent), so
the dashboard can show *why* a row is stale instead of silently freezing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from jarvis.shared import Event

LOGGER = logging.getLogger("jarvis.surface.usage_observer")

OBSERVER_ACTOR: Final[str] = "observer"
EVENT_TYPE: Final[str] = "usage.state_observed"
TTS_USAGE_EVENT_TYPE: Final[str] = "tts.usage_observed"
SERVICES: Final[tuple[str, ...]] = ("claude", "codex", "openai", "deepseek", "minimax")

DEFAULT_HTTP_TIMEOUT_S: Final[float] = 15.0
MAX_ERROR_CHARS: Final[int] = 200
MAX_ROWS: Final[int] = 20

_SELECT_BY_TYPE_SQL: Final[str] = "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC"
_SELECT_TTS_CHARS_SQL: Final[str] = (
    "SELECT COALESCE(SUM(CAST(json_extract(payload_json, '$.characters') AS INTEGER)), 0) "
    "FROM events WHERE type = ? AND ts_epoch_ms >= ?"
)


@dataclass(frozen=True)
class UsageConfig:
    """Poller settings; the MiniMax anchor is the one human-supplied fact."""

    minimax_anchor_usd: float | None = None
    minimax_anchor_at_ms: int | None = None
    minimax_usd_per_million_chars: float = 60.0
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S


@dataclass(frozen=True)
class UsageSnapshot:
    """One service's observable state. ``data`` is bounded JSON, no secrets."""

    service: str
    status: str  # ok | error | unconfigured
    data: dict[str, Any]
    error: str | None = None

    def same_state(self, other: UsageSnapshot | None) -> bool:
        """State equality; the observation timestamp lives outside the snapshot."""
        return other is not None and (self.status, self.error, self.data) == (
            other.status,
            other.error,
            other.data,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(epoch_s: float | None) -> str | None:
    if epoch_s is None:
        return None
    return dt.datetime.fromtimestamp(float(epoch_s), tz=dt.UTC).isoformat()


def _iso_seconds(value: object) -> str | None:
    """Drop sub-second noise so an unchanged reset time compares equal poll to poll."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).replace(microsecond=0).isoformat()
    except ValueError:
        return value


def _error(service: str, exc: BaseException | str) -> UsageSnapshot:
    text = str(exc)[:MAX_ERROR_CHARS]
    LOGGER.warning("usage_observer: %s failed: %s", service, text)
    return UsageSnapshot(service=service, status="error", data={}, error=text)


def _get_json(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
    if not url.startswith("https://"):
        msg = f"refusing non-https URL: {url}"
        raise ValueError(msg)
    request = urllib.request.Request(  # noqa: S310 — https enforced above.
        url, headers={"Accept": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        body = json.loads(response.read())
    return body if isinstance(body, dict) else {}


# --- Claude --------------------------------------------------------------


def _read_claude_credentials() -> dict[str, Any] | None:
    """Keychain first (what Claude Code writes on macOS), file fallback."""
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            from_keychain = json.loads(completed.stdout)
            if isinstance(from_keychain, dict):
                return from_keychain
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        from_file = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return from_file if isinstance(from_file, dict) else None


def _claude_plan_label(oauth: Mapping[str, Any]) -> str:
    tier = str(oauth.get("rateLimitTier") or "")
    match = re.search(r"(\d+)x", tier, re.IGNORECASE)
    if match:
        return f"{match.group(1)}X"
    return str(oauth.get("subscriptionType") or "").title() or "?"


def collect_claude(*, timeout_s: float) -> UsageSnapshot:
    """claude.ai subscription windows: 5h, 7d total, 7d per model."""
    creds = _read_claude_credentials()
    oauth = (creds or {}).get("claudeAiOauth") if creds else None
    if not oauth or not oauth.get("accessToken"):
        return UsageSnapshot("claude", "unconfigured", {}, "no Claude Code login")
    try:
        body = _get_json(
            "https://api.anthropic.com/api/oauth/usage",
            {
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout_s=timeout_s,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("claude", exc)
    windows: list[dict[str, Any]] = []
    for key, label in (("five_hour", "5 小时"), ("seven_day", "7 天 · 总")):
        window = body.get(key) or {}
        if window.get("utilization") is not None:
            windows.append(
                {
                    "key": key,
                    "label": label,
                    "percent": float(window["utilization"]),
                    "resets_at": _iso_seconds(window.get("resets_at")),
                }
            )
    for limit in body.get("limits") or []:
        scope = limit.get("scope") or {}
        if limit.get("kind") != "weekly_scoped" or scope.get("surface") is not None:
            continue
        name = str((scope.get("model") or {}).get("display_name") or "").strip()
        if not name or limit.get("percent") is None:
            continue
        windows.append(
            {
                "key": f"seven_day_{name.lower()}",
                "label": f"7 天 · {name}",
                "percent": float(limit["percent"]),
                "resets_at": _iso_seconds(limit.get("resets_at")),
            }
        )
    return UsageSnapshot(
        "claude", "ok", {"plan": _claude_plan_label(oauth), "windows": windows[:MAX_ROWS]}
    )


# --- Codex ---------------------------------------------------------------

_CODEX_PLAN_LABELS: Final[dict[str, str]] = {
    "prolite": "Pro Lite",
    "pro": "Pro",
    "plus": "Plus",
    "team": "Team",
    "free": "Free",
}


_FIVE_HOURS_S: Final[int] = 5 * 3600
_SEVEN_DAYS_S: Final[int] = 7 * 86400


def _codex_window_label(seconds: int) -> str:
    if seconds == _FIVE_HOURS_S:
        return "5 小时"
    if seconds == _SEVEN_DAYS_S:
        return "7 天"
    return f"{seconds // 3600} 小时"


def collect_codex(*, timeout_s: float) -> UsageSnapshot:
    """ChatGPT/Codex rate-limit windows plus the reset-credit counter."""
    try:
        auth = json.loads((Path.home() / ".codex" / "auth.json").read_text())
        tokens = auth["tokens"]
        access, account = tokens["access_token"], tokens.get("account_id", "")
    except (OSError, ValueError, KeyError, TypeError):
        return UsageSnapshot("codex", "unconfigured", {}, "no Codex login")
    try:
        body = _get_json(
            "https://chatgpt.com/backend-api/wham/usage",
            {
                "Authorization": f"Bearer {access}",
                "ChatGPT-Account-Id": account,
                "User-Agent": "codex-cli",
            },
            timeout_s=timeout_s,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("codex", exc)
    windows: list[dict[str, Any]] = []
    rate = body.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        window = rate.get(key)
        if not window:
            continue
        windows.append(
            {
                "key": key,
                "label": _codex_window_label(int(window.get("limit_window_seconds") or 0)),
                "percent": float(window.get("used_percent") or 0),
                "resets_at": _iso(window.get("reset_at")),
            }
        )
    plan = str(body.get("plan_type") or "")
    reset_credits = body.get("rate_limit_reset_credits") or {}
    return UsageSnapshot(
        "codex",
        "ok",
        {
            "plan": _CODEX_PLAN_LABELS.get(plan, plan.title() or "?"),
            "windows": windows,
            "reset_credits": int(reset_credits.get("available_count") or 0),
        },
    )


# --- OpenAI API spend ------------------------------------------------------


def _local_month_start_s(now: dt.datetime) -> int:
    return int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


_OPENAI_ORG: Final[str] = "https://api.openai.com/v1/organization/"


def _openai_buckets(
    key: str, path: str, *, start_s: int, group_by: str, timeout_s: float
) -> list[dict[str, Any]]:
    """Daily buckets since ``start_s`` (the Costs API only buckets by day), paged."""
    buckets: list[dict[str, Any]] = []
    page: str | None = None
    for _ in range(4):  # ponytail: page cap; one page already holds 31 days.
        query: dict[str, Any] = {
            "start_time": start_s,
            "bucket_width": "1d",
            "limit": 31,
            "group_by": group_by,
        }
        if page:
            query["page"] = page
        body = _get_json(
            _OPENAI_ORG + path + "?" + urllib.parse.urlencode(query),
            {"Authorization": f"Bearer {key}"},
            timeout_s=timeout_s,
        )
        buckets.extend(b for b in body.get("data") or [] if isinstance(b, dict))
        page = body.get("next_page") if body.get("has_more") else None
        if not page:
            break
    return buckets


def _fold_buckets(
    buckets: list[dict[str, Any]], fold: Callable[[dict[str, Any]], tuple[str, float]]
) -> tuple[dict[str, float], dict[str, float]]:
    """Return ``(month totals, latest-day totals)`` keyed by ``fold``'s label.

    "Today" is the newest daily bucket, which the API aligns to UTC days —
    a few hours of skew for a Pacific user, accepted (ADR-0018 §3).
    """
    latest = max((int(b.get("start_time") or 0) for b in buckets), default=0)
    month: dict[str, float] = {}
    today: dict[str, float] = {}
    for bucket in buckets:
        for row in bucket.get("results") or []:
            label, value = fold(row)
            month[label] = month.get(label, 0.0) + value
            if int(bucket.get("start_time") or 0) == latest:
                today[label] = today.get(label, 0.0) + value
    return month, today


def _cost_row(row: dict[str, Any]) -> tuple[str, float]:
    """USD per model: line_item stripped of its ', input' / ', output' suffix."""
    model = str(row.get("line_item") or "other").split(",")[0].strip()
    return model, float((row.get("amount") or {}).get("value") or 0)


def _token_row(row: dict[str, Any]) -> tuple[str, float]:
    """Input + output tokens per API key id."""
    tokens = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    return str(row.get("api_key_id") or "unknown"), float(tokens)


def _openai_key_names(key: str, *, timeout_s: float) -> dict[str, str]:
    names: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        projects = _get_json(
            "https://api.openai.com/v1/organization/projects?limit=20", headers, timeout_s=timeout_s
        )
        for project in projects.get("data") or []:
            keys = _get_json(
                f"https://api.openai.com/v1/organization/projects/{project['id']}/api_keys?limit=20",
                headers,
                timeout_s=timeout_s,
            )
            for row in keys.get("data") or []:
                names[str(row.get("id"))] = str(
                    row.get("name") or row.get("redacted_value") or row.get("id")
                )
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        LOGGER.info("usage_observer: OpenAI key names unavailable; showing ids")
    return names


def collect_openai(*, timeout_s: float, now: dt.datetime | None = None) -> UsageSnapshot:
    """Today / month-to-date USD by model, plus tokens by API key."""
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return UsageSnapshot("openai", "unconfigured", {}, "需要 Admin key")
    now = now or dt.datetime.now().astimezone()
    month_s = _local_month_start_s(now)
    try:
        month, today = _fold_buckets(
            _openai_buckets(
                key, "costs", start_s=month_s, group_by="line_item", timeout_s=timeout_s
            ),
            _cost_row,
        )
        month_keys, today_keys = _fold_buckets(
            _openai_buckets(
                key,
                "usage/completions",
                start_s=month_s,
                group_by="api_key_id",
                timeout_s=timeout_s,
            ),
            _token_row,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("openai", exc)
    names = _openai_key_names(key, timeout_s=timeout_s) if month_keys else {}
    models = sorted(set(today) | set(month), key=lambda m: -(month.get(m, 0.0)))
    keys = sorted(set(today_keys) | set(month_keys), key=lambda k: -month_keys.get(k, 0.0))
    return UsageSnapshot(
        "openai",
        "ok",
        {
            "today_usd": round(sum(today.values()), 4),
            "month_usd": round(sum(month.values()), 4),
            "by_model": [
                {
                    "model": m,
                    "today_usd": round(today.get(m, 0.0), 4),
                    "month_usd": round(month.get(m, 0.0), 4),
                }
                for m in models[:MAX_ROWS]
            ],
            "by_key": [
                {
                    "key_id": k,
                    "name": names.get(k, k),
                    "today_tokens": int(today_keys.get(k, 0.0)),
                    "month_tokens": int(month_keys.get(k, 0.0)),
                }
                for k in keys[:MAX_ROWS]
            ],
        },
    )


# --- DeepSeek --------------------------------------------------------------


def collect_deepseek(*, timeout_s: float) -> UsageSnapshot:
    """Official account balance."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return UsageSnapshot("deepseek", "unconfigured", {}, "no DEEPSEEK_API_KEY")
    try:
        body = _get_json(
            "https://api.deepseek.com/user/balance",
            {"Authorization": f"Bearer {key}"},
            timeout_s=timeout_s,
        )
        info = (body.get("balance_infos") or [{}])[0]
    except (urllib.error.URLError, OSError, ValueError, IndexError) as exc:
        return _error("deepseek", exc)
    return UsageSnapshot(
        "deepseek",
        "ok",
        {
            "balance": float(info.get("total_balance") or 0),
            "currency": str(info.get("currency") or "USD"),
            "is_available": bool(body.get("is_available")),
        },
    )


# --- MiniMax (estimate, loop thread) -----------------------------------------


def tts_characters_since(event_log: sqlite3.Connection, since_ms: int) -> int:
    """Sum ``tts.usage_observed`` characters at or after ``since_ms``."""
    row = event_log.execute(_SELECT_TTS_CHARS_SQL, (TTS_USAGE_EVENT_TYPE, since_ms)).fetchone()
    return int(row[0]) if row else 0


def estimate_minimax(event_log: sqlite3.Connection, config: UsageConfig) -> UsageSnapshot:
    """Anchor minus characters times unit price; ``unconfigured`` without an anchor."""
    if config.minimax_anchor_usd is None or config.minimax_anchor_at_ms is None:
        return UsageSnapshot("minimax", "unconfigured", {}, "no balance anchor")
    characters = tts_characters_since(event_log, config.minimax_anchor_at_ms)
    spent = characters * config.minimax_usd_per_million_chars / 1_000_000
    return UsageSnapshot(
        "minimax",
        "ok",
        {
            "anchor_usd": config.minimax_anchor_usd,
            "anchor_at": _iso(config.minimax_anchor_at_ms / 1000),
            "characters_since_anchor": characters,
            "usd_per_million_chars": config.minimax_usd_per_million_chars,
            "estimate_usd": round(config.minimax_anchor_usd - spent, 4),
        },
    )


# --- Baseline + emit ----------------------------------------------------------


def _snapshot_from_payload(payload: dict[str, Any]) -> UsageSnapshot | None:
    try:
        return UsageSnapshot(
            service=str(payload["service"]),
            status=str(payload["status"]),
            data=dict(payload.get("data") or {}),
            error=payload.get("error"),
        )
    except (KeyError, TypeError):
        LOGGER.warning("usage_observer: unreadable %s payload; ignoring row", EVENT_TYPE)
        return None


def recover_baselines(event_log: sqlite3.Connection) -> dict[str, UsageSnapshot]:
    """Latest ``usage.state_observed`` per service, folded from the log."""
    baselines: dict[str, UsageSnapshot] = {}
    for (payload_json,) in event_log.execute(_SELECT_BY_TYPE_SQL, (EVENT_TYPE,)):
        snapshot = _snapshot_from_payload(json.loads(payload_json))
        if snapshot is not None:
            baselines[snapshot.service] = snapshot
    return baselines


def latest_usage(event_log: sqlite3.Connection) -> dict[str, Any]:
    """Read model for the dashboard: latest snapshot per service + when."""
    services: dict[str, Any] = {}
    for (payload_json,) in event_log.execute(_SELECT_BY_TYPE_SQL, (EVENT_TYPE,)):
        payload = json.loads(payload_json)
        service = payload.get("service")
        if service in SERVICES:
            services[service] = {
                "status": payload.get("status"),
                "error": payload.get("error"),
                "observed_at_ms": payload.get("observed_at_ms"),
                "data": payload.get("data") or {},
            }
    return {"services": services}


class UsageObserver:
    """Emit-on-change usage perception over a log-recovered baseline.

    :meth:`collect` is the ``asyncio.to_thread`` half (network only);
    :meth:`emit` runs on the connection's owning thread, adds the
    log-derived MiniMax estimate, and appends events.
    """

    def __init__(self, event_log: sqlite3.Connection, config: UsageConfig | None = None) -> None:
        """Bind the observer to a log connection and its poller settings."""
        self._event_log = event_log
        self._config = config or UsageConfig()
        self._baselines: dict[str, UsageSnapshot] = {}

    def recover_baselines(self) -> dict[str, UsageSnapshot]:
        """Seed the baseline cache from the event log; call at startup."""
        self._baselines = recover_baselines(self._event_log)
        return dict(self._baselines)

    def collect(self) -> list[UsageSnapshot]:
        """Every remote service, sequentially; a failure is a snapshot, never a raise."""
        timeout_s = self._config.http_timeout_s
        return [
            collect_claude(timeout_s=timeout_s),
            collect_codex(timeout_s=timeout_s),
            collect_openai(timeout_s=timeout_s),
            collect_deepseek(timeout_s=timeout_s),
        ]

    def emit(self, snapshots: list[UsageSnapshot]) -> list[Event]:
        """Append one ``usage.state_observed`` per *changed* service."""
        emitted: list[Event] = []
        observed_at_ms = _now_ms()
        for snapshot in [*snapshots, estimate_minimax(self._event_log, self._config)]:
            if snapshot.same_state(self._baselines.get(snapshot.service)):
                continue
            emitted.append(
                emit_event(
                    self._event_log,
                    type="usage.state_observed",  # literal: the registry canaries scan it.
                    payload={
                        "service": snapshot.service,
                        "status": snapshot.status,
                        "error": snapshot.error,
                        "data": snapshot.data,
                        "observed_at_ms": observed_at_ms,
                        "actor": OBSERVER_ACTOR,
                    },
                )
            )
            self._baselines[snapshot.service] = snapshot
        return emitted

    def poll_once(self) -> list[Event]:
        """Collect + emit synchronously; the runtime task splits the halves."""
        return self.emit(self.collect())


__all__ = [
    "EVENT_TYPE",
    "OBSERVER_ACTOR",
    "SERVICES",
    "TTS_USAGE_EVENT_TYPE",
    "UsageConfig",
    "UsageObserver",
    "UsageSnapshot",
    "collect_claude",
    "collect_codex",
    "collect_deepseek",
    "collect_openai",
    "estimate_minimax",
    "latest_usage",
    "recover_baselines",
    "tts_characters_since",
]
