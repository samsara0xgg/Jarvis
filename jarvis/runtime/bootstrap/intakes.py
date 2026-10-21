"""The pollers and passes that read his mail and records into the ledgers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.decision.attention import rule_judge_v1
from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.runtime.claude_daily import ClaudeDailyIntake, ClaudeDailySettings
from jarvis.runtime.commitment_intake import CommitmentIntake, CommitmentSettings
from jarvis.runtime.home import mail_summarizer
from jarvis.runtime.job_mail import JobMail
from jarvis.runtime.mail_codes import MailCodes, MailCodesSettings
from jarvis.runtime.things_ledger import ThingsLedger
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.runtime.turn_things import TurnThings, TurnThingsSettings
from jarvis.runtime.work_state import build_analyst
from jarvis.state import imports

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.bootstrap.core import Core
    from jarvis.runtime.config import JobMailConfig, MailReplySettings, RuntimeConfig
    from jarvis.runtime.moment import Moment
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.runtime.work_state import LLMAnalyst
    from jarvis.shared.clock import Clock
    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)


def _mail_reply(
    settings: MailReplySettings | None, clock: Clock, log: JevLog | None = None,
) -> MailReply | None:
    """``home.mail_reply`` (ADR 0123): Jev's reply marks over the inbox, or None while off."""
    if settings is None:
        return None
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=settings.model, min_confidence=1.0, timeout_ms=settings.timeout_ms, clock=clock,
        log=log,
    )
    return MailReply(
        route, settings.yes_at, settings.fyi_at, settings.junk_at, importance=settings.importance,
    )


def _job_mail(  # noqa: PLR0913 - the settings, its collaborators, the clock and the moment
    config: JobMailConfig | None, log: JevLog | None,
    connections: PluginConnections, db_path: Path, clock: Clock,
    moment: Moment | None = None, device: DeviceLink | None = None,
    event_log: Path | None = None, timesink_path: Path | None = None,
) -> JobMail | None:
    """``job_mail`` (ADR 0155): the poller, or None while off.

    ``clock`` is the runtime's; its zone is the owner's: a time a letter gives with no zone of
    its own is his clock there.
    """
    if config is None:
        return None
    # min_confidence is the choice question's bar; these questions read probabilities instead.
    route = SurrogateRoute(
        model=config.model, min_confidence=1.0, timeout_ms=config.timeout_ms, clock=clock,
        log=log,
    )
    return JobMail(
        config.settings, route, connections, db_path, rule_judge_v1, clock=clock,
        moment=moment, timesink_path=timesink_path, device=device, event_log=event_log,
    )


def _mail_codes(
    settings: MailCodesSettings | None, connections: PluginConnections, device: DeviceLink | None,
) -> MailCodes | None:
    """``mail_codes`` (ADR 0266): the poller, or None while off."""
    return None if settings is None else MailCodes(settings, connections, device)


def _commitments(
    settings: CommitmentSettings | None, connections: PluginConnections, db_path: Path,
    event_log: Path, analyst_for: Callable[[str], LLMAnalyst | None],
) -> CommitmentIntake | None:
    """``commitments`` (ADR 0274): the poller, or None while off."""
    if settings is None:
        return None
    return CommitmentIntake(
        settings, connections, db_path, event_log,
        mail_summarizer(analyst_for(settings.preset), event_log),
    )


def _turn_things(
    settings: TurnThingsSettings | None, db_path: Path, event_log: Path, zone: tzinfo,
    analyst_for: Callable[[str], LLMAnalyst | None],
) -> TurnThings | None:
    """``turn_things`` (ADR 0300): the pass, or None while off."""
    if settings is None:
        return None
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
    settings: ClaudeDailySettings | None, connections: PluginConnections, db_path: Path,
    root: Path,
) -> ClaudeDailyIntake:
    """``claude_daily`` (ADR 0295): the intake, with Gmail letters off while the settings are None.

    Always built (ADR 0303): the Mac's day report is read whether or not Gmail letters are on.
    """
    if settings is None:
        # Gmail letters are off, but the Mac's day report of his Claude Code work (ADR 0303) is
        # still read from the imports archive: an intake that never touches Gmail.
        return ClaudeDailyIntake.code_reports_only(
            connections, db_path, root / imports.IMPORTS_DIRNAME,
        )
    return ClaudeDailyIntake(settings, connections, db_path, root / imports.IMPORTS_DIRNAME)


@dataclass(frozen=True)
class Intakes:
    """The pollers and passes that read his mail and records into the ledgers.

    Built, not started: the daemon starts them, so a failure after this stage leaves nothing
    running.
    """

    mail_codes: MailCodes | None
    commitments: CommitmentIntake | None
    claude_daily: ClaudeDailyIntake
    things_ledger: ThingsLedger
    things_mirror: ThingsMirror
    turn_things: TurnThings | None


def build_intakes(
    config: RuntimeConfig, paths: RuntimePaths, core: Core, connections: PluginConnections,
) -> Intakes:
    """Build the mail and record intakes over the started plugin connections."""
    memory = core.memory
    mail_codes = _mail_codes(config.mail_codes, connections, core.device)
    commitments = _commitments(
        config.commitments, connections, memory.db_path, paths.event_log,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="commitments",
        ),
    )
    claude_daily = _claude_daily(config.claude_daily, connections, memory.db_path, paths.root)
    things_ledger = _things_ledger(
        paths.event_log, memory.db_path, config.zone, config.remind_before_min,
    )
    things_mirror = ThingsMirror(connections, paths.event_log, config.zone)
    turn_things = _turn_things(
        config.turn_things, memory.db_path, paths.event_log, config.zone,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="turn_things",
        ),
    )
    return Intakes(
        mail_codes, commitments, claude_daily, things_ledger, things_mirror, turn_things,
    )
