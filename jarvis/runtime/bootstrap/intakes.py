"""The pollers and passes that read his mail and records into the ledgers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.attention import rule_judge_v1
from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.runtime.claude_daily import ClaudeDailyIntake, ClaudeDailySettings
from jarvis.runtime.commitment_intake import CommitmentIntake, CommitmentSettings
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.runtime.home import mail_summarizer
from jarvis.runtime.interview_reminders import InterviewSettings
from jarvis.runtime.job_mail import LINKEDIN_ALERTS, JobMail, JobMailSettings
from jarvis.runtime.mail_codes import MailCodes, MailCodesSettings
from jarvis.runtime.things_ledger import ThingsLedger
from jarvis.runtime.turn_things import TurnThings, TurnThingsSettings
from jarvis.state import imports

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.runtime.moment import Moment
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.runtime.work_state import LLMAnalyst
    from jarvis.shared.clock import Clock
    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)


def _mail_reply(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
) -> MailReply | None:
    """``home.mail_reply`` (ADR 0123): off unless enabled; bad values stop boot."""
    home = config.get("home")
    block = home.get("mail_reply") if isinstance(home, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, timeout = block.get("model"), block.get("timeout_ms")
    bars = [  # fyi_at, yes_at, junk_at
        v for v in (block.get("fyi_at"), block.get("yes_at"), block.get("junk_at"))
        if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or len(bars) != 3 or not 0 <= bars[0] < bars[1] <= 1  # noqa: PLR2004 — fyi_at, yes_at
        or not 0 < bars[2] <= 1
    ):
        msg = (
            f"runtime: {config_path} home.mail_reply needs model (text), timeout_ms (positive"
            " int), fyi_at < yes_at, both in [0, 1], and junk_at in (0, 1]"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    rating = block.get("importance")  # ADR 0141
    return MailReply(
        route, float(bars[1]), float(bars[0]), float(bars[2]),
        importance=isinstance(rating, Mapping) and rating.get("enabled") is True,
    )


def _job_mail(  # noqa: PLR0913 - the config, its collaborators, the clock and the moment
    config: Mapping[str, Any], config_path: Path, log: JevLog | None,
    connections: PluginConnections, db_path: Path, clock: Clock,
    moment: Moment | None = None, device: DeviceLink | None = None,
    event_log: Path | None = None, timesink_path: Path | None = None,
) -> JobMail | None:
    """``job_mail`` (ADR 0155): off unless enabled; bad values stop boot.

    ``clock`` is the runtime's; its zone is the owner's: a time a letter gives with no zone of
    its own is his clock there.
    """
    block = config.get("job_mail")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None

    def number(key: str, *, low: float, high: float | None = None, whole: bool = False) -> Any:  # noqa: ANN401 - int or float by key
        value = block.get(key)
        kind = int if whole else int | float
        if isinstance(value, bool) or not isinstance(value, kind) or value < low or (
            high is not None and value > high
        ):
            msg = (
                f"runtime: {config_path} job_mail.{key} must be "
                f"{'an integer' if whole else 'a number'} of at least {low}"
                + ("" if high is None else f" and at most {high}")
            )
            raise RuntimeBootstrapError(msg)
        return value

    model = block.get("model")
    if not isinstance(model, str) or not model.strip():
        msg = f"runtime: {config_path} job_mail.model must be text"
        raise RuntimeBootstrapError(msg)
    if not isinstance(block.get("speak"), bool):
        msg = f"runtime: {config_path} job_mail.speak must be true or false"
        raise RuntimeBootstrapError(msg)
    if block.get("linkedin_alerts") not in LINKEDIN_ALERTS:
        msg = f"runtime: {config_path} job_mail.linkedin_alerts must be one of {LINKEDIN_ALERTS}"
        raise RuntimeBootstrapError(msg)
    excluded = block.get("exclude_domains")
    if not isinstance(excluded, list) or not all(
        isinstance(one, str) and one.strip() for one in excluded
    ):
        msg = f"runtime: {config_path} job_mail.exclude_domains must be a list of domain names"
        raise RuntimeBootstrapError(msg)
    since = block.get("backfill_since")
    if since is not None and (not isinstance(since, date) or isinstance(since, datetime)):
        msg = f"runtime: {config_path} job_mail.backfill_since must be a date (YYYY-MM-DD) or null"
        raise RuntimeBootstrapError(msg)
    settings = JobMailSettings(
        interview_reminders=_interview_reminders(block.get("interview_reminders"), config_path),
        poll_s=float(number("poll_s", low=1)),
        backfill_days=number("backfill_days", low=1, high=60, whole=True),
        max_messages_per_cycle=number("max_messages_per_cycle", low=1, high=100, whole=True),
        header_skip_at=float(number("header_skip_at", low=0.0001, high=1)),
        body_min=float(number("body_min", low=0.0001, high=1)),
        max_body_chars=number("max_body_chars", low=100, whole=True),
        max_calls_per_day=number("max_calls_per_day", low=1, whole=True),
        speak=block["speak"],
        speak_gap_s=float(number("speak_gap_s", low=0)),
        linkedin_alerts=block["linkedin_alerts"],
        exclude_domains=tuple(one.strip().lower() for one in excluded),
        backfill_since=since,
    )
    timeout = number("timeout_ms", low=1, whole=True)
    # min_confidence is the choice question's bar; these questions read probabilities instead.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    return JobMail(
        settings, route, connections, db_path, rule_judge_v1, clock=clock,
        moment=moment, timesink_path=timesink_path, device=device, event_log=event_log,
    )


def _mail_codes(
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections,
    device: DeviceLink | None,
) -> MailCodes | None:
    """``mail_codes`` (ADR 0266): off unless enabled; bad values stop boot."""
    block = config.get("mail_codes")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    values = {}
    for key, low, high in (("poll_s", 1, None), ("window_min", 1, 60)):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"runtime: {config_path} mail_codes.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        values[key] = value
    return MailCodes(
        MailCodesSettings(float(values["poll_s"]), int(values["window_min"])), connections, device,
    )


def _commitments(  # noqa: PLR0913 - the config and the poller's collaborators
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections, db_path: Path,
    event_log: Path, analyst_for: Callable[[str], LLMAnalyst | None],
) -> CommitmentIntake | None:
    """``commitments`` (ADR 0274): off unless enabled; bad values stop boot."""
    block = config.get("commitments")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    where = f"runtime: {config_path} commitments"
    numbers = {}
    for key, low, high in (
        ("poll_s", 1, None), ("max_mails_per_cycle", 1, 200), ("remind_before_min", 1, 1440),
    ):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"{where}.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        numbers[key] = value
    since = block.get("backfill_since")
    if not isinstance(since, date) or isinstance(since, datetime):
        msg = f"{where}.backfill_since must be a date (YYYY-MM-DD)"
        raise RuntimeBootstrapError(msg)
    preset = block.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        msg = f"{where}.preset must be the name of an llm preset"
        raise RuntimeBootstrapError(msg)
    settings = CommitmentSettings(
        float(numbers["poll_s"]), since, int(numbers["max_mails_per_cycle"]), preset.strip(),
        int(numbers["remind_before_min"]),
    )
    return CommitmentIntake(
        settings, connections, db_path, event_log,
        mail_summarizer(analyst_for(settings.preset), event_log),
    )


_BACKFILL_MAX_DAYS: Final[int] = 60


def _turn_things(  # noqa: PLR0913 - the config and the pass's collaborators
    config: Mapping[str, Any], config_path: Path, db_path: Path, event_log: Path, zone: tzinfo,
    analyst_for: Callable[[str], LLMAnalyst | None],
) -> TurnThings | None:
    """``turn_things`` (ADR 0300): off unless enabled; bad values stop boot."""
    block = config.get("turn_things")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    where = f"runtime: {config_path} turn_things"
    days = block.get("backfill_days")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= _BACKFILL_MAX_DAYS:
        msg = f"{where}.backfill_days must be a whole number of days from 1 to {_BACKFILL_MAX_DAYS}"
        raise RuntimeBootstrapError(msg)
    preset = block.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        msg = f"{where}.preset must be the name of an llm preset"
        raise RuntimeBootstrapError(msg)
    settings = TurnThingsSettings(days, preset.strip())
    return TurnThings(
        settings, db_path, event_log, mail_summarizer(analyst_for(settings.preset), event_log),
        zone,
    )


def _things_ledger(
    event_log: Path, db_path: Path, zone: tzinfo, remind_before_min: int,
) -> ThingsLedger:
    """The ledger of his things (ADR 0299), carried in once now; it runs with the poller off too.

    A carried-in commitment rings ``commitments.remind_before_min`` ahead, else 60 minutes. A
    carry-in that fails is logged and tried again by the daemon's rings pass.
    """
    ledger = ThingsLedger(event_log, db_path, zone, remind_before_min)
    try:
        ledger.carry_in()
    except Exception:
        LOGGER.exception("things: the carry-in failed; the daemon tries it again")
    return ledger


def _claude_daily(
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections, db_path: Path,
    root: Path,
) -> ClaudeDailyIntake:
    """``claude_daily`` (ADR 0295): Gmail letters off unless enabled; bad values stop boot.

    Always built (ADR 0303): the Mac's day report is read whether or not Gmail letters are on.
    """
    block = config.get("claude_daily")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        # Gmail letters are off, but the Mac's day report of his Claude Code work (ADR 0303) is
        # still read from the imports archive: an intake that never touches Gmail.
        return ClaudeDailyIntake.code_reports_only(
            connections, db_path, root / imports.IMPORTS_DIRNAME,
        )
    where = f"runtime: {config_path} claude_daily"
    numbers = {}
    for key, low, high in (("poll_s", 60, None), ("max_letters_per_cycle", 1, 200)):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"{where}.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        numbers[key] = value
    since = block.get("backfill_since")
    if not isinstance(since, date) or isinstance(since, datetime):
        msg = f"{where}.backfill_since must be a date (YYYY-MM-DD)"
        raise RuntimeBootstrapError(msg)
    settings = ClaudeDailySettings(
        float(numbers["poll_s"]), since, int(numbers["max_letters_per_cycle"]),
    )
    return ClaudeDailyIntake(settings, connections, db_path, root / imports.IMPORTS_DIRNAME)


def _interview_reminders(block: object, config_path: Path) -> InterviewSettings | None:
    """``job_mail.interview_reminders`` (ADR 0186): None while off; bad values stop boot."""
    where = f"runtime: {config_path} job_mail.interview_reminders"
    if not isinstance(block, Mapping) or not isinstance(block.get("enabled"), bool):
        msg = f"{where}.enabled must be true or false"
        raise RuntimeBootstrapError(msg)
    if not block["enabled"]:
        return None
    raw = block.get("evening_at")
    try:
        evening = clock.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        evening = None
    if evening is None or evening.tzinfo:
        msg = f'{where}.evening_at must be a local time such as "20:00"'
        raise RuntimeBootstrapError(msg)
    minutes = block.get("before_min")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:  # noqa: PLR2004 - a day
        msg = f"{where}.before_min must be a whole number of minutes from 1 to 1440"
        raise RuntimeBootstrapError(msg)
    return InterviewSettings(evening, minutes)
