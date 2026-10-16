r"""Replay mails through the commitment reader of ADR 0274, offline.

    .venv/bin/python -m scripts.commitment_replay MAILS.jsonl OUT.jsonl [--live] [--preset haiku-bg]
        [--config config/jarvis.yaml] [--zone America/Vancouver] [--today 2026-10-15]

``MAILS.jsonl`` has one mail per line: ``{message_id, sender_name, sender_domain, subject,
received_at, body}``. ``OUT.jsonl`` gets one line per mail, in received order:
``{message_id, subject, filter, asked, answer, actions}``. ``filter`` is whether the local
filter lets the mail through; without ``--live`` that is all that is computed (no model call, no
cost, ``asked`` false, ``actions`` null). With ``--live`` each mail that passes is sent to the
preset's real model (the key comes from the environment) and its answer is parsed exactly as the
poller parses it. The commitments earlier mails made are carried forward in memory, so a
reminder or a cancellation is read against them as it would be; nothing is written anywhere
but ``OUT.jsonl``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision import commitment_mail as triage
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.work_state import build_analyst

REPO = Path(__file__).resolve().parents[1]


def _asker(config_path: Path, preset: str) -> Any:  # noqa: ANN401 - a callable of two strings
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    analyst = build_analyst(config, preset, pricing_path=None, account_cost=False, kind="replay")
    if analyst is None:
        msg = f"llm.presets.{preset} in {config_path} is missing or malformed"
        raise SystemExit(msg)

    def ask(system: str, user: str) -> str:
        with closing(sqlite3.connect(":memory:")) as conn:
            result = analyst.analyze(
                conn, system=system, messages=[{"role": "user", "content": user}],
                tools=[], tool_choice="none",
            )
        return (result.text or "").strip()

    return ask


def _carry(known: dict[str, triage.Known], action: triage.Action, domain: str, n: int) -> None:
    """Fold one action into the commitments later mails are read against."""
    fields = action.fields
    if action.action == "new":
        cid = f"replay-{n}"
        known[cid] = triage.Known(
            cid, fields["title"], fields["start_at"], fields.get("weekly_until", ""), domain,
        )
    elif action.action == "cancel":
        known.pop(action.commitment_id, None)
    else:
        old = known[action.commitment_id]
        known[old.commitment_id] = dataclasses.replace(
            old,
            **{k: v for k, v in fields.items() if k in {"title", "start_at", "weekly_until"}},
        )


def replay(
    mails: list[dict[str, Any]], zone: str, today: date, ask: Any | None,  # noqa: ANN401
) -> list[dict[str, Any]]:
    """The output line of each mail, oldest first."""
    known: dict[str, triage.Known] = {}
    out = []
    for n, raw in enumerate(sorted(mails, key=lambda m: str(m["received_at"]))):
        mail = triage.Mail(
            str(raw["sender_name"]), str(raw["sender_domain"]).lower(), str(raw["subject"]),
            str(raw["received_at"]), str(raw["body"]),
        )
        line: dict[str, Any] = {
            "message_id": raw["message_id"], "subject": mail.subject,
            "filter": triage.may_hold_time(mail.subject, mail.body),
            "asked": False, "answer": None, "actions": None,
        }
        if line["filter"] and ask is not None:
            shown = triage.related(
                list(known.values()), mail.sender_domain, datetime.fromisoformat(mail.received_at),
            )
            system, user = triage.build_prompt(mail, shown, zone=zone, today=today)
            answer = ask(system, user)
            actions = triage.parse_actions(
                answer, {k.commitment_id for k in shown}, zone=ZoneInfo(zone), today=today,
            )
            for action in actions:
                _carry(known, action, mail.sender_domain, n)
            listed = [dataclasses.asdict(a) for a in actions]
            line |= {"asked": True, "answer": answer, "actions": listed}
        out.append(line)
    return out


def main(argv: list[str] | None = None) -> int:
    """Read the mails, replay them, write the lines."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("mails", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--live", action="store_true", help="call the preset's real model")
    parser.add_argument("--preset", default="haiku-bg")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    mails = [
        json.loads(line)
        for line in args.mails.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    today = args.today or datetime.now(ZoneInfo(args.zone)).date()
    ask = _asker(args.config, args.preset) if args.live else None
    lines = replay(mails, args.zone, today, ask)
    args.out.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8",
    )
    passed = sum(bool(line["filter"]) for line in lines)
    sys.stdout.write(
        f"{len(lines)} mails, {passed} pass the filter, "
        f"{sum(bool(line['asked']) for line in lines)} sent to the model -> {args.out}\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
