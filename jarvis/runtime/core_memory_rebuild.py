"""Runtime wiring for the monthly core memory rebuild (ADR 0389), and its hand-run command.

On the first run of the nightly job after a month ends, once the nightly pass has consolidated
that month's last day, one call on a client pinned to ``core_memory.rebuild.preset`` writes the
note anew from the current version and the month's day summaries; L3 gates the answer and L2
appends it as one version of origin ``rebuild``. The schedule runs only after a first rebuild
exists: the first one is run by hand and reviewed before it lands.

Hand run (prints the proposed note and what changes; core memory is not written, only the
call's cost is recorded in the event log)::

    uv run python -m jarvis.runtime.core_memory_rebuild --month 2026-10

``--out FILE`` saves the gated changes; ``--apply FILE`` lands exactly those, if the note has not
changed since.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.decision.core_memory_rebuild import (
    build_rebuild_messages,
    check_rebuild,
    summary_labels,
)
from jarvis.decision.cost_guard import CostRecorder
from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL
from jarvis.state import core_memory, places
from jarvis.state.cost_accounting import call_recorded_since
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    INFERRED,
    MemorySettings,
    append_rebuild_core_memory,
    core_memory_notes,
    current_core_memory,
    last_rebuild_month,
    latest_day_summaries,
    open_memory_db,
    record_sources,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.decision.llm import LLMClient
    from jarvis.runtime.core_memory import CoreMemorySettings

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "core_memory_rebuild"
CALL_TIMEOUT_S: Final[float] = 1800.0  # a whole note at effort high takes longer than a day


class Proposal(NamedTuple):
    """A gated rebuild of ``month`` against version ``base_id``, or why it failed."""

    base_id: str
    month: str
    day: str
    changes: list[dict[str, Any]] | None
    why: str | None


def month_days(month: str) -> tuple[str, str]:
    """The first and last day of ``YYYY-MM``."""
    first = date.fromisoformat(f"{month}-01")
    after = (first + timedelta(days=32)).replace(day=1)
    return first.isoformat(), (after - timedelta(days=1)).isoformat()


def _previous_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _tried_this_month(event_log_path: Path, today: date) -> bool:
    """Whether a rebuild call was recorded since the first of ``today``'s month (his Mac's zone)."""
    first = datetime.combine(today.replace(day=1), time(0)).astimezone()
    with closing(open_runtime_event_log(event_log_path)) as conn:
        return call_recorded_since(conn, kind=KIND, since_ms=int(first.timestamp() * 1000))


def rebuild_due(memory: MemorySettings, today: date) -> str | None:
    """The month to rebuild now, or None.

    The month before ``today``, once the nightly pass consolidated its last day and no rebuild
    covers it. None before the first rebuild: that one is run by hand.
    """
    last = last_rebuild_month(memory.db_path)
    month = _previous_month(today)
    if last is None or last >= month:
        return None
    upto = current_core_memory(memory.db_path).upto_day or ""
    return month if upto >= month_days(month)[1] else None


def propose_rebuild(  # noqa: PLR0913 — the month, the store, the knobs, the client, the accounting.
    month: str,
    *,
    memory: MemorySettings,
    settings: CoreMemorySettings,
    client: LLMClient,
    cost_recorder: CostRecorder,
    day: str,
) -> Proposal:
    """One call for ``month``; the gated changes, or why the answer cannot land."""
    rebuild = settings.rebuild
    if rebuild is None:
        msg = "core_memory.rebuild is not configured"
        raise ValueError(msg)
    base = current_core_memory(memory.db_path)
    kinds = {
        item_id: note.split(", ")[1]
        for item_id, note in core_memory_notes(memory.db_path, base.doc).items()
    }
    with closing(open_memory_db(memory.db_path)) as conn:
        month_summaries = latest_day_summaries(conn, *month_days(month))
        summaries, labels, label_days = summary_labels(month_summaries)
        spoken = record_sources(conn, set(labels.values()))
    his = {label for label, rid in labels.items() if spoken.get(rid, ("",))[0] == "allen"}
    result = cost_recorder.chat(
        client,
        messages=build_rebuild_messages(
            month=month,
            doc=base.doc,
            kinds=kinds,
            summaries=summaries,
            places=places.names_block(memory.db_path),
            section_caps=settings.section_caps,
            max_chars=settings.max_chars,
        ),
        system=rebuild.prompt,
        tools=None,
        tool_choice=None,
        kind=KIND,
        turn_id=None,
    )
    changes, why = check_rebuild(
        result.text,
        result.finish_reason,
        doc=base.doc,
        labels=labels,
        label_days=label_days,
        his_labels=his,
        kinds=kinds,
        max_chars=settings.max_chars,
        section_caps=settings.section_caps,
        day=day,
    )
    return Proposal(base.id, month, day, changes, why)


def land(memory: MemorySettings, proposal: Proposal) -> str | None:
    """Append the proposal as one version; None when it failed or the note changed meanwhile."""
    if proposal.changes is None:
        return None
    return append_rebuild_core_memory(
        memory.db_path,
        base_id=proposal.base_id,
        day=proposal.day,
        month=proposal.month,
        changes=proposal.changes,
    )


def run_rebuild(  # noqa: PLR0913 — the store, the knobs, the client and the accounting seam.
    memory: MemorySettings,
    settings: CoreMemorySettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    today: date,
) -> str:
    """The scheduled rebuild: one month when due, asked once more on a rejected answer.

    One run a month: once a rebuild call is recorded this month, a month that was rejected waits
    for a hand run instead of paying again every night.
    """
    month = rebuild_due(memory, today)
    if month is None:
        return "not due"
    if _tried_this_month(event_log_path, today):
        return f"{month} tried this month already; run it by hand"
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        outcome = "rejected"
        for _attempt in range(2):
            proposal = propose_rebuild(
                month, memory=memory, settings=settings, client=client,
                cost_recorder=cost_recorder, day=today.isoformat(),
            )
            if proposal.why is not None:
                LOGGER.warning("core_memory_rebuild: %s rejected (%s)", month, proposal.why)
                outcome = f"rejected ({proposal.why})"
                continue
            version = land(memory, proposal)
            return f"landed {month} ({version})" if version else "rejected (the note changed)"
    return outcome


def diff_lines(base: core_memory.Doc, changes: Sequence[dict[str, Any]]) -> list[str]:
    """What ``changes`` do to ``base``, one line each: rewritten, added, dropped with the reason."""
    items = core_memory.numbered_items(base)
    out: list[str] = []
    for change in changes:
        if change["op"] == "add":
            out.append(f"+ [{change['section']}] {change['text']}")
            continue
        section, item, _id = items[change["item"] - 1]
        if change["op"] == "stale":
            out.append(f"- [{section}] {item['text']}  (why: {change.get('reason')})")
        elif change["text"] != item["text"] or change["section"] != section:
            out.append(f"~ [{section}] {item['text']}\n  -> [{change['section']}] {change['text']}")
    kept = len(items) - sum(1 for c in changes if c["op"] == "stale")
    out.append(f"= {kept} of {len(items)} items kept")
    return out


def _root(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser()
    return Path(os.environ.get("JARVIS_RUNTIME_ROOT") or DEFAULT_RUNTIME_ROOT_LITERAL).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    """Propose a rebuild and print it, save it with ``--out``, or ``--apply`` a saved one."""
    from jarvis.deployment import load_env_file  # noqa: PLC0415 — only the hand run needs keys
    from jarvis.runtime.config import load_runtime_config  # noqa: PLC0415 — nor the config
    from jarvis.runtime.core_memory import CoreMemorySettings  # noqa: PLC0415
    from jarvis.runtime.session_compaction import build_compact_client  # noqa: PLC0415
    from jarvis.shared.pricing import load_pricing_table  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="python -m jarvis.runtime.core_memory_rebuild", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--month", help="YYYY-MM (default: last month)")
    parser.add_argument("--runtime-root", type=Path, help="override JARVIS_RUNTIME_ROOT")
    parser.add_argument("--out", type=Path, help="save the gated changes to this file")
    parser.add_argument("--apply", type=Path, help="land the changes saved in this file")
    args = parser.parse_args(argv)
    root = _root(args.runtime_root)
    config = load_runtime_config(None, root)
    memory = config.memory
    if args.apply is not None:
        saved = json.loads(args.apply.read_text())
        version = land(memory, Proposal(**saved))
        print(f"landed {version}" if version else "not landed: the note changed since; run again")  # noqa: T201 - this is the tool's output
        return 0 if version else 1
    settings = CoreMemorySettings.from_config(config.raw.get("core_memory"))
    if settings is None or settings.rebuild is None:
        print("core_memory.rebuild is not configured", file=sys.stderr)  # noqa: T201 - this is the tool's output
        return 1
    load_env_file(root)
    llm = config.raw.get("llm")
    if not isinstance(llm, Mapping):
        print("no llm block in the config", file=sys.stderr)  # noqa: T201 - this is the tool's output
        return 1
    client = build_compact_client(llm, settings.rebuild.preset, timeout_s=CALL_TIMEOUT_S)
    month = args.month or _previous_month(date.today())  # noqa: DTZ011 — his own Mac's day
    with closing(open_runtime_event_log(root / "mac_events.db")) as conn:
        proposal = propose_rebuild(
            month, memory=memory, settings=settings, client=client,
            cost_recorder=CostRecorder(conn, pricing_table=load_pricing_table(config.pricing_path)),
            day=date.today().isoformat(),  # noqa: DTZ011 — his own Mac's day
        )
    if proposal.changes is None:
        print(f"rejected: {proposal.why}", file=sys.stderr)  # noqa: T201 - this is the tool's output
        return 1
    base = current_core_memory(memory.db_path).doc
    new = core_memory.apply_changes(base, proposal.changes, proposal.day)
    lines = [core_memory.render(new), "", *diff_lines(base, proposal.changes)]
    notes = core_memory_notes(memory.db_path, core_memory.live(new)).values()
    inferred = sum(1 for note in notes if INFERRED in note)
    lines.append(f"= {inferred} inferred items left, {len(core_memory.render(new))} characters")
    print(*lines, sep="\n")  # noqa: T201 - this is the tool's output
    if args.out is not None:
        args.out.write_text(json.dumps(proposal._asdict(), ensure_ascii=False, indent=1))
        print(f"saved to {args.out}; land it with --apply {args.out}")  # noqa: T201 - this is the tool's output
    return 0


__all__ = [
    "Proposal", "diff_lines", "land", "month_days", "propose_rebuild", "rebuild_due", "run_rebuild",
]


if __name__ == "__main__":
    sys.exit(main())
