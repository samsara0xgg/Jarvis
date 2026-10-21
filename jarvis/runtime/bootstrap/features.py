"""The services the boot builds from the config and the core, before any tool exists."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from jarvis.deployment.launchd import logs_dir
from jarvis.deployment.night_power import MacPower
from jarvis.execution.around import Surroundings
from jarvis.execution.transit_tool import TransitOffers
from jarvis.runtime.bootstrap.day import (
    _DAILY_REPORT_TIMEOUT_S,
    _codex_sessions_path,
    _doing_schedule,
)
from jarvis.runtime.bootstrap.jev import build_jev_log
from jarvis.runtime.bootstrap.places import _here_location, _locator
from jarvis.runtime.bootstrap.vision import _build_vision_client
from jarvis.runtime.bus_live import BusLive
from jarvis.runtime.daily_report import DailyReportService
from jarvis.runtime.dashboard import MailDrafts, ViewState
from jarvis.runtime.deep_search import build_deep_search
from jarvis.runtime.locate import HerePolicy
from jarvis.runtime.moment import Moment
from jarvis.runtime.night_run import NightRun
from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.rides import Rides
from jarvis.runtime.weatherkit import WeatherKit
from jarvis.runtime.work_state import WorkStateService, build_analyst
from jarvis.state import places as place_state
from jarvis.state.projects import parse_catalog
from jarvis.state.voice_settings import VoiceSettings
from jarvis.surface.ambient_sounds import AmbientSounds
from jarvis.surface.voice_cues import VoiceCues

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.decision.surrogate_route import JevLog
    from jarvis.deployment import RuntimePaths
    from jarvis.execution.tools import DeepSearch, VisionClient
    from jarvis.runtime.bootstrap.core import Core
    from jarvis.runtime.config import DashboardSettings, RuntimeConfig
    from jarvis.runtime.doing_run import DoingSchedule
    from jarvis.runtime.locate import Locator


@dataclass(frozen=True)
class Features:
    """What the later stages read of the services built from the config and the core.

    Nothing here holds a connection or runs a thread: a failure after this stage leaves nothing
    to undo.
    """

    maps_key: str
    kept_places: Callable[[], dict[str, str]]
    moment: Moment | None
    work_state: WorkStateService
    projects: ProjectsService | None
    daily_report: DailyReportService
    night: NightRun | None
    voice_settings: VoiceSettings
    view: ViewState | None
    mail_view: ViewState | None
    mail_drafts: MailDrafts | None
    voice_cues: VoiceCues
    ambient: AmbientSounds | None
    bus_live: BusLive
    transit_offers: TransitOffers
    vision_client: VisionClient | None
    deep_search: DeepSearch | None
    rides: Rides
    locator: Locator | None
    here: HerePolicy
    weatherkit: WeatherKit | None
    surroundings: Surroundings
    doing: DoingSchedule | None
    jev_log: JevLog | None


def _dashboard_state(
    settings: DashboardSettings,
) -> tuple[ViewState | None, ViewState | None, MailDrafts | None]:
    """The Dashboard's view, the view the mail page reads, and the mail drafts (None = off).

    The mail page needs the view (ADR 0176: its open letter is the view's open item); with
    ``dashboard.mail.enabled`` alone it stays off.
    """
    view = ViewState() if settings.view else None
    mail_view = view if settings.mail else None
    return view, mail_view, None if mail_view is None else MailDrafts(mail_view)


def build_features(config: RuntimeConfig, paths: RuntimePaths, core: Core) -> Features:
    """Build the services that read the config, the stores and the paired devices."""
    memory, device, clock = core.memory, core.device, core.clock
    moment = (
        None if config.moment is None
        else Moment(config.moment, config.observer.timesink_db_path, memory.db_path, device)
    )
    maps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    kept_places = partial(place_state.home_school, memory.db_path)
    work_state = WorkStateService(
        event_log_path=paths.event_log,
        memory_path=memory.db_path,
        timesink_path=config.observer.timesink_db_path,
        repos=config.observer.repos,
        analyst=build_analyst(
            config.raw,
            config.work_state.preset,
            pricing_path=config.pricing_path,
            account_cost=True,
        ),
        model=config.work_state.preset,
        tz=config.zone,
        device=device,
    )
    catalog = parse_catalog(config.raw.get("projects"))
    projects = (
        None
        if not catalog
        else ProjectsService(
            event_log_path=paths.event_log,
            timesink_path=config.observer.timesink_db_path,
            projects=catalog,
            sorter=build_analyst(
                config.raw,
                config.work_state.preset,
                pricing_path=config.pricing_path,
                account_cost=True,
                kind="projects",
            ),
            model=config.work_state.preset,
            tz=config.zone,
            device=device,
        )
    )
    daily_report = DailyReportService(
        memory_path=memory.db_path,
        timesink_path=config.observer.timesink_db_path,
        repos=config.observer.repos,
        reporter=build_analyst(
            config.raw,
            config.daily_report.preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="daily_report",
            timeout_s=_DAILY_REPORT_TIMEOUT_S,
        ),
        model=config.daily_report.preset,
        tz=config.zone,
        codex_sessions_path=_codex_sessions_path(config.daily_report),
        device=device,
    )
    night = (
        None
        if config.role == "brain"  # ADR 0170: holding a Mac awake and dark is the terminal's job
        else NightRun(
            paths.event_log,
            config.night,
            MacPower(),
            zone=config.zone,
        )
    )
    voice_settings = VoiceSettings(paths.root / "voice-settings.json")
    view, mail_view, mail_drafts = _dashboard_state(config.dashboard)
    voice_cues = VoiceCues()
    ambient = (
        AmbientSounds(log_path=logs_dir(paths.root) / "ambient-sounds.jsonl")
        if config.realtime.ambient_sounds else None
    )
    bus_live = BusLive(paths.root / "cache")
    transit_offers = TransitOffers()  # the list `bus` keeps, and its chat card rows (ADR 0205)
    vision_client = _build_vision_client(
        config.raw,
        config.tools.vision_preset,
        event_log_path=paths.event_log,
        pricing_path=config.pricing_path,
    )
    deep_search = build_deep_search(
        config.raw,
        event_log_path=paths.event_log,
        pricing_path=config.pricing_path,
        account_cost=True,
    )
    rides = Rides(paths.event_log, bus_live, places=kept_places, offers=transit_offers)  # ADR 0224
    locator = (  # ADR 0281: only where a phone's answer can arrive
        None if core.phone_events is None
        else _locator(config.locate, core.locate_waits, clock)
    )
    here = HerePolicy(  # ADR 0322: the one Here every tool call reads where he is from
        paths.event_log, clock, locator=locator, mac=_here_location(config.role),
        memory_db=memory.db_path, phone=config.locate.phone, max_age_s=config.locate.max_age_s,
    )
    weatherkit = WeatherKit.from_settings(config.weatherkit)  # ADR 0285: logs which source is used
    surroundings = Surroundings(  # ADR 0284: held by position; the tool and the state line share it
        maps_key, config.maps_monthly_usd, memory.db_path, config.zone,
    )
    return Features(
        maps_key=maps_key,
        kept_places=kept_places,
        moment=moment,
        work_state=work_state,
        projects=projects,
        daily_report=daily_report,
        night=night,
        voice_settings=voice_settings,
        view=view,
        mail_view=mail_view,
        mail_drafts=mail_drafts,
        voice_cues=voice_cues,
        ambient=ambient,
        bus_live=bus_live,
        transit_offers=transit_offers,
        vision_client=vision_client,
        deep_search=deep_search,
        rides=rides,
        locator=locator,
        here=here,
        weatherkit=weatherkit,
        surroundings=surroundings,
        doing=_doing_schedule(config, memory, paths, device),
        jev_log=build_jev_log(config.jev, paths.root),
    )
