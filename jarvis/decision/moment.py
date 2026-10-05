"""L3 moment rules (ADR 0161): when to hold alerts, and the short situation doc (现况).

Facts come from ``jarvis.state.timesink_moment``; each may be ``unknown``. The hold rule acts
only on what is known: unknown holds nothing, so behaviour is exactly as before. The doc a model
may be shown is built by code in a few hundred characters, with three parts (Now, Today so far,
Your situation) whose fields config can switch off one by one. It names apps and site domains
only, never a window title or a URL.

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
    "idle": "away (idle)",
    "locked": "away (screen locked)",
    "asleep": "away (asleep)",
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
    return f"{round(seconds / 60)} min" if seconds < 3600 else f"{seconds / 3600:.1f} h"  # noqa: PLR2004


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
        pieces.append(f"front app {app}")
    if site not in (None, _UNKNOWN):
        pieces.append(f"site {site}")
    if got.get("since") not in (None, _UNKNOWN):
        pieces.append(f"since {_clock(got['since'])}")
    call = got.get("call")
    if isinstance(call, dict) and call["in_call"] != _UNKNOWN:
        pieces.append(
            f"in a call/meeting ({call['app']})" if call["in_call"] == "yes" else "not in a call"
        )
    if got.get("screen_share") == "yes":
        pieces.append("sharing the screen")
    presence = got.get("presence")
    if presence is not None and presence != _UNKNOWN:
        pieces.append(_PRESENCE.get(presence, "at the computer"))
    return "Now: " + (", ".join(pieces) if pieces else "unknown") + "."


def _today_part(got: dict[str, Any], *, apps: bool) -> str:
    items = []
    job, top = got.get("job_today"), got.get("apps_today")
    if isinstance(job, int | float) and job > 0:
        items.append(f"job hunting {_hours(job)}")
    if apps and isinstance(top, list):
        items.extend(f"{name} {_hours(seconds)}" for name, seconds in top)
    unknown = _UNKNOWN in (got.get("job_today"), got.get("apps_today"))
    return (
        "Today so far: "
        + ("; ".join(items) if items else "unknown" if unknown else "none yet")
        + "."
    )


def _text(got: dict[str, Any], *, apps: bool) -> str:
    parts = []
    if {"front_app", "site", "since", "call", "screen_share", "presence"} & got.keys():
        parts.append(_now_part(got))
    if {"job_today", "apps_today"} & got.keys():
        parts.append(_today_part(got, apps=apps))
    if got.get("situation"):
        parts.append(f"Your situation: {got['situation']}")
    return "\n".join(parts)


def render_doc(
    facts: dict[str, Any], fields: dict[str, bool], situation: str = ""
) -> dict[str, Any]:
    """``{"text", "fields"}``: the situation doc and the structured values behind it.

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
