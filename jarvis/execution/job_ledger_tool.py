"""The conversation's way to read the job ledger (ADR 0155) and to record an application he told.

``job_ledger`` reads what the Dashboard Jobs page shows; ``record_application`` (ADR 0213) adds
the row of an application he says he submitted somewhere Jarvis has no record of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

MAX_APPLICATIONS: Final = 15
SUBJECT_CHARS: Final = 80
NAME_CHARS: Final = 60
_MAX_RESULT_CHARS: Final = 8_000

_DESCRIPTION: Final = (
    "Read Allen's job-hunt ledger, the same one the Dashboard Jobs page shows: one row per "
    "company and role, newest first, with the latest stage (offer, interview, rejection, "
    "receipt, other), the last mail's date and subject, the next interview or event if one is "
    "known, and the time spent on that company's site. Use this when the user asks about job "
    "applications, interviews, offers or how the job hunt is going. Read-only."
)
_RECORD_DESCRIPTION: Final = (
    "Record one job application Allen says he submitted somewhere Jarvis has no record of, for "
    "example 'I applied to Acme'. Do not use it for a company that is already "
    "in the job ledger with that role (job_ledger shows what is there). One call per "
    "application. The role and the date (YYYY-MM-DD, default today) are optional."
)
_RECORD_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "The company he applied to."},
        "role": {"type": "string", "description": "The role, if he said it."},
        "applied_at": {"type": "string", "description": "YYYY-MM-DD; today when he did not say."},
    },
    "required": ["company"],
    "additionalProperties": False,
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _row(group: Mapping[str, Any]) -> dict[str, Any]:
    mails = group.get("mails") or []
    newest = mails[0] if mails else {}
    due = group.get("next_event_at")
    row: dict[str, Any] = {
        "company": _clip(str(group.get("company") or ""), NAME_CHARS),
        "role": _clip(str(group.get("role") or ""), NAME_CHARS),
        "stage": group.get("kind") or "",
        "mails": group.get("count") or len(mails),
        "last_mail_at": group.get("last_at"),
        "last_subject": _clip(str(newest.get("subject") or ""), SUBJECT_CHARS),
        "spent_s": group.get("time_total_s") or 0,
    }
    if due:
        text = next((m.get("event_text") for m in mails if m.get("event_at") == due), None)
        row["next_event_at"] = due
        if text:
            row["next_event"] = _clip(str(text), SUBJECT_CHARS)
    return row


def build_job_ledger_tool(read: Callable[[], Mapping[str, Any]] | None) -> tuple[Tool, ...]:
    """``job_ledger`` over the runtime's ledger read; none while job mail is off."""
    if read is None:
        return ()

    def job_ledger(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        view = read()
        groups = list(view.get("ledger") or [])
        return {
            "applications": [_row(g) for g in groups[:MAX_APPLICATIONS]],
            "total": len(groups),
            "truncated": len(groups) > MAX_APPLICATIONS,
            "held_back_mails": len(view.get("skipped") or []),
        }

    return (
        Tool(
            name="job_ledger",
            description=_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=job_ledger,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            max_result_chars=_MAX_RESULT_CHARS,
        ),
    )


def build_record_application_tool(
    add: Callable[[Mapping[str, Any]], str] | None,
) -> tuple[Tool, ...]:
    """``record_application`` over the runtime's ``add_application``; none while job mail is off."""
    if add is None:
        return ()

    def record_application(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "company": str(args.get("company") or ""),
            "role": str(args.get("role") or ""),
            "source": "said",
        }
        if args.get("applied_at"):
            fields["applied_at"] = str(args["applied_at"])
        try:
            return {"recorded": True, "id": add(fields)}
        except ValueError as exc:
            msg = f"record_application: {exc}"
            raise ToolError(msg, code="invalid_argument") from exc

    return (
        Tool(
            name="record_application",
            description=_RECORD_DESCRIPTION,
            input_schema=_RECORD_SCHEMA,
            handler=record_application,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["MAX_APPLICATIONS", "build_job_ledger_tool", "build_record_application_tool"]
