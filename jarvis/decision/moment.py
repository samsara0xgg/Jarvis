"""L3 moment rules (ADR 0161): when to hold alerts, and the short 现况 doc a model may be shown.

Facts come from ``jarvis.state.timesink_moment``; each may be ``unknown``. The hold rule acts
only on what is known: unknown holds nothing, so behaviour is exactly as before. The doc is built
by code in a few hundred characters, with three parts (此刻, 今天到现在, 你的情况) whose fields
config can switch off one by one. It names apps and site domains only, never a window title
or a URL.

Layer rules: stdlib only; no wiring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

# The doc never grows past this many characters; the lowest fields are dropped first.
DOC_MAX_CHARS: Final[int] = 400
FIELDS: Final[tuple[str, ...]] = (
    "front_app",
    "site",
    "since",
    "call",
    "screen_share",
    "presence",
    "job_today",
    "apps_today",
    "situation",
)
_PRESENCE: Final[dict[str, str]] = {
    "idle": "离开（空闲）",
    "locked": "离开（已锁屏）",
    "asleep": "离开（休眠）",
}
_UNKNOWN: Final[str] = "unknown"


def hold_reason(facts: dict[str, Any]) -> str | None:
    """Why alerts wait right now: ``call``, ``asleep``, ``locked`` or ``idle``; None otherwise.

    Only a known state holds. A call wins over the away states.
    """
    if facts.get("in_call") == "yes":
        return "call"
    presence = facts.get("presence")
    return presence if presence in ("asleep", "locked", "idle") else None


def _hours(seconds: float) -> str:
    return f"{round(seconds / 60)} 分钟" if seconds < 3600 else f"{seconds / 3600:.1f}h"  # noqa: PLR2004


def _clock(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def structured(
    facts: dict[str, Any], fields: dict[str, bool], situation: str = ""
) -> dict[str, Any]:
    """The enabled doc fields with their values (``unknown`` kept), exactly as the doc says them."""
    today = facts.get("today")
    values: dict[str, Any] = {
        "front_app": facts.get("front_app", _UNKNOWN),
        "site": facts.get("site_domain", _UNKNOWN),
        "since": facts.get("since", _UNKNOWN),
        "call": {"in_call": facts.get("in_call", _UNKNOWN), "app": facts.get("call_app")},
        "screen_share": facts.get("screen_share", _UNKNOWN),
        "presence": facts.get("presence", _UNKNOWN),
        "job_today": today["job_s"] if isinstance(today, dict) else _UNKNOWN,
        "apps_today": today["apps"] if isinstance(today, dict) else _UNKNOWN,
        "situation": situation,
    }
    return {name: values[name] for name in FIELDS if fields.get(name)}


def _now_part(got: dict[str, Any]) -> str:
    pieces = []
    app, site = got.get("front_app"), got.get("site")
    if app not in (None, _UNKNOWN):
        pieces.append(f"前台 {app}")
    if site not in (None, _UNKNOWN):
        pieces.append(f"站点 {site}")
    if got.get("since") not in (None, _UNKNOWN):
        pieces.append(f"自 {_clock(got['since'])} 起")
    call = got.get("call")
    if isinstance(call, dict) and call["in_call"] != _UNKNOWN:
        pieces.append(
            f"正在通话/会议（{call['app']}）" if call["in_call"] == "yes" else "不在通话/会议中"
        )
    if got.get("screen_share") == "yes":
        pieces.append("正在共享屏幕")
    presence = got.get("presence")
    if presence is not None and presence != _UNKNOWN:
        pieces.append(_PRESENCE.get(presence, "在电脑前"))
    return "此刻：" + ("，".join(pieces) if pieces else "未知") + "。"


def _today_part(got: dict[str, Any], *, apps: bool) -> str:
    items = []
    job, top = got.get("job_today"), got.get("apps_today")
    if isinstance(job, int | float) and job > 0:
        items.append(f"求职 {_hours(job)}")
    if apps and isinstance(top, list):
        items.extend(f"{name} {_hours(seconds)}" for name, seconds in top)
    unknown = _UNKNOWN in (got.get("job_today"), got.get("apps_today"))
    return (
        "今天到现在：" + ("；".join(items) if items else "未知" if unknown else "还没有记录") + "。"
    )


def _text(got: dict[str, Any], *, apps: bool) -> str:
    parts = []
    if {"front_app", "site", "since", "call", "screen_share", "presence"} & got.keys():
        parts.append(_now_part(got))
    if {"job_today", "apps_today"} & got.keys():
        parts.append(_today_part(got, apps=apps))
    if got.get("situation"):
        parts.append(f"你的情况：{got['situation']}")
    return "\n".join(parts)


def render_doc(
    facts: dict[str, Any], fields: dict[str, bool], situation: str = ""
) -> dict[str, Any]:
    """``{"text", "fields"}``: the 现况 doc and the structured values behind it.

    ``fields`` switches each field on or off; ``situation`` is Allen's watch list, goals and
    rules (empty for now, so the third part is left out). Over ``DOC_MAX_CHARS`` the per-app
    list goes first, then the text is cut.
    """
    got = structured(facts, fields, situation)
    text = _text(got, apps=True)
    if len(text) > DOC_MAX_CHARS:
        text = _text(got, apps=False)
    if len(text) > DOC_MAX_CHARS:
        text = text[: DOC_MAX_CHARS - 1] + "…"
    return {"text": text, "fields": got}
