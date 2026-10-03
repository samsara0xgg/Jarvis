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
- ``minimax``  — ``/account/query_balance``, undocumented; MiniMax's own
  ``mmx-cli`` reads the balance of an ``sk-api-`` key there (ADR 0065).

Secrets never enter a payload: only percentages, dollars, timestamps and
plan labels are stored. Every remote failure collapses to a snapshot with
``status`` ``error`` (or ``unconfigured`` when a credential is absent), so
the dashboard can show *why* a row is stale instead of silently freezing.
The one exception is Claude's 429 (polled too soon), which keeps the last
reading. :func:`redeem_codex_reset` is the one write: it spends a Codex
limit reset when Allen confirms it on the Usage page (ADR 0048).

OpenAI reports no balance. Allen records one on the Usage page
(``usage.balance_recorded``, ADR 0065); the balance shown is the latest
recording minus the spend observed since.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared.lang import t
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from jarvis.shared import Event

LOGGER = logging.getLogger("jarvis.surface.usage_observer")

OBSERVER_ACTOR: Final[str] = "observer"
EVENT_TYPE: Final[str] = "usage.state_observed"
SERVICES: Final[tuple[str, ...]] = ("claude", "codex", "openai", "deepseek", "minimax")
BALANCE_EVENT_TYPE: Final[str] = "usage.balance_recorded"
# The services with no balance API, whose balance Allen records by hand.
BALANCE_SERVICES: Final[tuple[str, ...]] = ("openai",)
MAX_BALANCE_USD: Final[float] = 1_000_000.0

DEFAULT_HTTP_TIMEOUT_S: Final[float] = 15.0
MAX_ERROR_CHARS: Final[int] = 200
MAX_ROWS: Final[int] = 20

_SELECT_BY_TYPE_SQL: Final[str] = "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC"
_SELECT_BALANCES_SQL: Final[str] = (
    "SELECT payload_json, ts_epoch_ms FROM events WHERE type = ? ORDER BY id ASC"
)


@dataclass(frozen=True)
class UsageConfig:
    """Poller settings."""

    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S


@dataclass(frozen=True)
class RecordedBalance:
    """A balance Allen read off the provider's billing page, and when he recorded it."""

    usd: float
    at_ms: int


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


def _post_json(
    url: str, headers: Mapping[str, str], body: Mapping[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    if not url.startswith("https://"):
        msg = f"refusing non-https URL: {url}"
        raise ValueError(msg)
    request = urllib.request.Request(  # noqa: S310 — https enforced above.
        url,
        data=json.dumps(body).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        answer = json.loads(response.read())
    return answer if isinstance(answer, dict) else {}


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


def _refresh_claude_login() -> None:
    """Have Claude Code renew its own 8-hour access token, which it does only when it runs.

    The Claude desktop app keeps its own login, so the Keychain token goes stale whenever
    the terminal CLI and the Agents window sit unused. ``/usage`` is a local command: it
    makes no model request, but it calls the API, so the CLI refreshes and stores the token
    first. Jarvis never writes the Keychain itself.
    """
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603
            [claude, "-p", "/usage"], capture_output=True, check=False, timeout=60, env=env,
            cwd=Path.home(),
        )


def _claude_oauth() -> dict[str, Any] | None:
    """Claude Code's OAuth block, renewed through the CLI first when its token has expired."""
    oauth = (_read_claude_credentials() or {}).get("claudeAiOauth")
    if oauth and (oauth.get("expiresAt") or math.inf) <= _now_ms():
        _refresh_claude_login()
        oauth = (_read_claude_credentials() or {}).get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def _claude_plan_label(oauth: Mapping[str, Any]) -> str:
    tier = str(oauth.get("rateLimitTier") or "")
    match = re.search(r"(\d+)x", tier, re.IGNORECASE)
    if match:
        return f"{match.group(1)}X"
    return str(oauth.get("subscriptionType") or "").title() or "?"


# The route reports limit resets (its `cedar_ember` block) only to the claude_code_cli surface,
# which it reads from Claude Code's own client headers; without them the block says
# `ineligible_reason: "surface"`. ponytail: the version is pinned; read it from the installed
# Claude Code if the route ever starts gating on it.
CLAUDE_CLI_HEADERS: Final[dict[str, str]] = {
    "User-Agent": "claude-cli/2.1.283 (external, cli)",
    "x-app": "cli",
}


def _claude_resets(block: object) -> dict[str, Any]:
    """Limit resets left and the last day to use them; nothing when none are on offer."""
    if not isinstance(block, dict) or not block.get("eligible"):
        return {}
    grants = [
        grant
        for grant in block.get("grants") or []
        if isinstance(grant, dict) and int(grant.get("resets_left") or 0) > 0
    ]
    ends = [end for grant in grants if (end := _iso_seconds(grant.get("ends_at")))]
    return {
        "reset_credits": sum(int(grant["resets_left"]) for grant in grants),
        "reset_ends_at": min(ends, default=None),
    }


def collect_claude(*, timeout_s: float) -> UsageSnapshot | None:
    """claude.ai subscription windows (5h, 7d total, 7d per model) and the limit resets left.

    ``None`` when the route answers 429: it refuses polls closer than about five minutes
    apart, which says nothing about the account, so the last reading stands.
    """
    oauth = _claude_oauth()
    if not oauth or not oauth.get("accessToken"):
        return UsageSnapshot("claude", "unconfigured", {}, "no Claude Code login")
    try:
        body = _get_json(
            "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1",
            {
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
                **CLAUDE_CLI_HEADERS,
            },
            timeout_s=timeout_s,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.TOO_MANY_REQUESTS:
            LOGGER.info("usage_observer: claude polled too soon (429); keeping the last reading")
            return None
        return _error("claude", exc)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("claude", exc)
    windows: list[dict[str, Any]] = []
    for key, label in (
        ("five_hour", t("usage.window_hours", hours=5)),
        ("seven_day", t("usage.window_week_total")),
    ):
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
                "label": t("usage.window_week_model", name=name),
                "percent": float(limit["percent"]),
                "resets_at": _iso_seconds(limit.get("resets_at")),
            }
        )
    return UsageSnapshot(
        "claude",
        "ok",
        {
            "plan": _claude_plan_label(oauth),
            "windows": windows[:MAX_ROWS],
            **_claude_resets(body.get("cedar_ember")),
        },
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
    if seconds == _SEVEN_DAYS_S:
        return t("usage.window_days", days=7)
    return t("usage.window_hours", hours=seconds // 3600)


def _codex_headers() -> dict[str, str] | None:
    """The Codex login's request headers; ``None`` without a login."""
    try:
        auth = json.loads((Path.home() / ".codex" / "auth.json").read_text())
        tokens = auth["tokens"]
        access, account = tokens["access_token"], tokens.get("account_id", "")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {
        "Authorization": f"Bearer {access}",
        "ChatGPT-Account-Id": account,
        "User-Agent": "codex-cli",
    }


def collect_codex(*, timeout_s: float) -> UsageSnapshot:
    """ChatGPT/Codex rate-limit windows plus the reset-credit counter."""
    headers = _codex_headers()
    if headers is None:
        return UsageSnapshot("codex", "unconfigured", {}, "no Codex login")
    try:
        body = _get_json("https://chatgpt.com/backend-api/wham/usage", headers, timeout_s=timeout_s)
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


# Where Codex's own client spends a reset (openai/codex, codex-rs/backend-client
# rate_limit_resets.rs). The request id is its idempotency key: a retried request answers
# already_redeemed instead of spending a second reset.
_CODEX_RESET_URL: Final[str] = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
)


def redeem_codex_reset(
    request_id: str, *, timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
) -> dict[str, Any]:
    """Spend one Codex limit reset (ADR 0048).

    Returns ``{code, windows_reset}``; code is reset, nothing_to_reset, no_credit or
    already_redeemed. Raises ``ValueError`` without a login and lets network errors out.
    """
    headers = _codex_headers()
    if headers is None:
        msg = "no Codex login"
        raise ValueError(msg)
    body = _post_json(
        _CODEX_RESET_URL, headers, {"redeem_request_id": request_id}, timeout_s=timeout_s
    )
    LOGGER.info("usage_observer: codex reset %s answered %s", request_id, body.get("code"))
    return {
        "code": str(body.get("code") or "unknown"),
        "windows_reset": int(body.get("windows_reset") or 0),
    }


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


def _openai_spent_since(key: str, balance: RecordedBalance, *, timeout_s: float) -> float:
    """USD spent from the start of the recording's UTC day, the Costs API's grain.

    Spend earlier that day is counted too, so the balance errs low by at most a day.
    ponytail: four pages of 31 days; a recording older than that undercounts.
    """
    day_s = balance.at_ms // 1000 // 86_400 * 86_400
    buckets = _openai_buckets(
        key, "costs", start_s=day_s, group_by="line_item", timeout_s=timeout_s
    )
    return sum(_cost_row(row)[1] for bucket in buckets for row in bucket.get("results") or [])


def collect_openai(
    *, timeout_s: float, now: dt.datetime | None = None, balance: RecordedBalance | None = None
) -> UsageSnapshot:
    """Today / month-to-date USD by model, tokens by API key, and the balance left."""
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return UsageSnapshot("openai", "unconfigured", {}, t("usage.needs_admin_key"))
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
        spent = None if balance is None else _openai_spent_since(key, balance, timeout_s=timeout_s)
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
            **(
                {}
                if balance is None or spent is None
                else {
                    "balance_usd": round(balance.usd - spent, 4),
                    "balance_recorded_usd": balance.usd,
                    "balance_recorded_at": _iso(balance.at_ms / 1000),
                }
            ),
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
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("deepseek", exc)
    infos = body.get("balance_infos") or []
    if not infos:
        # A 200 with no balance row is missing data, not a zero balance: an "ok"
        # 0.0 would look like a spent account and rewrite the baseline.
        return _error("deepseek", "no balance_infos in response")
    info = infos[0]
    return UsageSnapshot(
        "deepseek",
        "ok",
        {
            "balance": float(info.get("total_balance") or 0),
            "currency": str(info.get("currency") or "USD"),
            "is_available": bool(body.get("is_available")),
        },
    )


# --- MiniMax ------------------------------------------------------------------


def collect_minimax(*, timeout_s: float) -> UsageSnapshot:
    """Account balance, less anything owed."""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        return UsageSnapshot("minimax", "unconfigured", {}, "no MINIMAX_API_KEY")
    try:
        body = _get_json(
            "https://api.minimax.io/account/query_balance",
            {"Authorization": f"Bearer {key}"},
            timeout_s=timeout_s,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("minimax", exc)
    # MiniMax answers a refused key with a 200 whose base_resp carries the error.
    base = body.get("base_resp") or {}
    if base.get("status_code") != 0 or body.get("available_amount") is None:
        return _error("minimax", str(base.get("status_msg") or "no available_amount in response"))
    balance = float(body["available_amount"]) - float(body.get("owed_amount") or 0)
    return UsageSnapshot("minimax", "ok", {"balance": round(balance, 4)})


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


def recorded_balances(event_log: sqlite3.Connection) -> dict[str, RecordedBalance]:
    """Latest ``usage.balance_recorded`` per service, folded from the log."""
    balances: dict[str, RecordedBalance] = {}
    for payload_json, ts_ms in event_log.execute(_SELECT_BALANCES_SQL, (BALANCE_EVENT_TYPE,)):
        payload = json.loads(payload_json)
        if payload.get("service") in BALANCE_SERVICES:
            balances[payload["service"]] = RecordedBalance(float(payload["usd"]), int(ts_ms))
    return balances


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
    :meth:`emit` runs on the connection's owning thread and appends events.
    """

    def __init__(self, event_log: sqlite3.Connection, config: UsageConfig | None = None) -> None:
        """Bind the observer to a log connection and its poller settings."""
        self._event_log = event_log
        self._config = config or UsageConfig()
        self._baselines: dict[str, UsageSnapshot] = {}
        self._balances: dict[str, RecordedBalance] = {}

    def recover_baselines(self) -> dict[str, UsageSnapshot]:
        """Seed the baselines and recorded balances from the event log; call at startup."""
        self._baselines = recover_baselines(self._event_log)
        self._balances = recorded_balances(self._event_log)
        return dict(self._baselines)

    def record_balance(self, service: str, usd: float) -> Event:
        """Record a balance Allen read off the provider's page (ADR 0065); loop thread.

        The next poll subtracts the spend since. Raises ``ValueError`` for a service
        that reports its own balance or an amount that is not a plausible balance.
        """
        plausible = math.isfinite(usd) and 0 <= usd < MAX_BALANCE_USD
        if service not in BALANCE_SERVICES or not plausible:
            msg = f"cannot record {usd!r} as the {service!r} balance"
            raise ValueError(msg)
        event = emit_event(
            self._event_log,
            type="usage.balance_recorded",  # literal: the registry canaries scan it.
            payload={"service": service, "usd": usd},
        )
        self._balances[service] = RecordedBalance(usd, event.ts_epoch_ms)
        return event

    def collect(self) -> list[UsageSnapshot]:
        """Every remote service, sequentially; a failure is a snapshot, never a raise.

        The guard is here rather than inside each collector because this is
        where the promise is made: a body that parses badly (a string where a
        percentage belongs, a missing key) costs that one service its row, not
        the whole cycle's.
        """
        timeout_s = self._config.http_timeout_s
        collectors: tuple[tuple[str, Callable[..., UsageSnapshot | None]], ...] = (
            ("claude", collect_claude),
            ("codex", collect_codex),
            ("openai", functools.partial(collect_openai, balance=self._balances.get("openai"))),
            ("deepseek", collect_deepseek),
            ("minimax", collect_minimax),
        )
        snapshots: list[UsageSnapshot] = []
        for service, collector in collectors:
            try:
                snapshot = collector(timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 - the docstring's promise.
                snapshot = _error(service, exc)
            if snapshot is not None:  # None: nothing new observed, the last reading stands.
                snapshots.append(snapshot)
        return snapshots

    def emit(self, snapshots: list[UsageSnapshot]) -> list[Event]:
        """Append one ``usage.state_observed`` per *changed* service."""
        emitted: list[Event] = []
        observed_at_ms = _now_ms()
        for snapshot in snapshots:
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
    "BALANCE_EVENT_TYPE",
    "BALANCE_SERVICES",
    "EVENT_TYPE",
    "OBSERVER_ACTOR",
    "SERVICES",
    "RecordedBalance",
    "UsageConfig",
    "UsageObserver",
    "UsageSnapshot",
    "collect_claude",
    "collect_codex",
    "collect_deepseek",
    "collect_minimax",
    "collect_openai",
    "latest_usage",
    "recorded_balances",
    "recover_baselines",
    "redeem_codex_reset",
]
