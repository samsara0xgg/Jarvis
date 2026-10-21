"""The composition root's boot: :func:`bootstrap_runtime_app`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING

from jarvis.decision.confirm_grammar import ConfirmGrammarConfigError, load_confirm_grammar
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_anthropic import set_stale_files_handler
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.policy import (
    PolicyConsistencyError,
    effective_policy,
    validate_requires_confirmation,
)
from jarvis.decision.pre_route import ToolCueConfigError, load_tool_cues
from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.deployment.launchd import logs_dir
from jarvis.deployment.night_power import MacPower
from jarvis.execution.around import Surroundings
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.execution.transit_tool import TransitOffers, build_leg_planner
from jarvis.runtime import JarvisRuntime, device_tools
from jarvis.runtime.bootstrap.catalog import (
    _dashboard_state,
    _install_open_path,
    _on_menu,
    _regex_router_tools,
    _web_search_credentials,
    _work_state_tool_refresh,
    register_catalog,
)
from jarvis.runtime.bootstrap.core import _daemon_idle
from jarvis.runtime.bootstrap.day import (
    _DAILY_REPORT_TIMEOUT_S,
    _about_items,
    _codex_sessions_path,
    _daily_schedule,
    _doing_schedule,
    _highlight_marks,
    _ledger,
    _moment,
    _places_block,
    _shares_line,
    _wire_plan_reader,
)
from jarvis.runtime.bootstrap.devices import (
    _audio_devices,
    _default_audio_device,
    _no_audio_devices,
    _no_default_audio_device,
    _settings_drift,
    _sound_output,
)
from jarvis.runtime.bootstrap.intakes import (
    _claude_daily,
    _commitments,
    _job_mail,
    _mail_codes,
    _mail_reply,
    _things_ledger,
    _turn_things,
)
from jarvis.runtime.bootstrap.jev import (
    _event_emitter,
    _jev_log,
    _jev_oneshot,
    _surrogate_route,
    _turn_end_asks,
    _voice_words,
)
from jarvis.runtime.bootstrap.llm import (
    _confirmation_words_client,
    _follow_conversation_setting,
    _think_mode,
)
from jarvis.runtime.bootstrap.places import (
    _here_location,
    _live_departure,
    _locator,
    _start_locating_places,
)
from jarvis.runtime.bootstrap.vision import _build_vision_client
from jarvis.runtime.bus_live import BusLive
from jarvis.runtime.config import RuntimeBootstrapError, RuntimeConfig
from jarvis.runtime.daily_report import DailyReportService
from jarvis.runtime.decision_state import DecisionStateCache
from jarvis.runtime.deep_search import build_deep_search
from jarvis.runtime.departures import Departures
from jarvis.runtime.home import Home, mail_summarizer, weather_lookup
from jarvis.runtime.locate import HerePolicy
from jarvis.runtime.night_run import NightRun, night_settings
from jarvis.runtime.now_block import calendar_line, doing_now_line, plan_line
from jarvis.runtime.picture_files import PictureFiles
from jarvis.runtime.picture_notes import PictureNotes
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.push import Push
from jarvis.runtime.reminders import Reminders
from jarvis.runtime.rides import Rides
from jarvis.runtime.settings import REPLY_LINES, Settings
from jarvis.runtime.settings_owner import page_keys
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.runtime.weatherkit import WeatherKit
from jarvis.runtime.whereabouts import Whereabouts
from jarvis.runtime.work_state import WorkStateService, build_analyst
from jarvis.shared import lang, llm_io_log
from jarvis.shared.clock import SystemClock, ZoneError
from jarvis.state import places as place_state
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.ledger import TURN_WAIT_S, LedgerSources, TerminalScreen
from jarvis.state.locate_wait import LocateWaits
from jarvis.state.memory_db import open_memory_db
from jarvis.state.projects import parse_catalog
from jarvis.state.voice_settings import VoiceSettings
from jarvis.state.voiceprint import VoiceprintBook
from jarvis.surface.ambient_sounds import AmbientSounds
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.voice_cues import VoiceCues

if TYPE_CHECKING:

    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.job_mail import JobMail


def bootstrap_runtime_app(  # noqa: PLR0915 - composition root wiring stays explicit
    config: RuntimeConfig, paths: RuntimePaths,
) -> JarvisRuntime:
    """Assemble a frozen :class:`JarvisRuntime` ready for :func:`run_turn`.

    The caller has laid out the runtime root (:func:`jarvis.deployment.bootstrap_runtime`),
    loaded its environment (:func:`load_runtime_env_and_trace`) and parsed the config
    (:func:`~jarvis.runtime.config.load_runtime_config`): a bad value has stopped it before
    anything here opens a file.

    Wiring (bottom-up across the layer DAG):

    1. L2 event log — :func:`jarvis.state.event_log.open_event_log`
       opens (or creates) the SQLite spine, idempotently installing
       the schema / indexes / append-only triggers.
    2. L4 registry + lifecycle — :func:`jarvis.execution.tools.build_default_registry`
       registers the tools; the :class:`ActionLifecycle` is per-process FSM.
    2b. L3 Tier 0 whitelist — ``config/tier0_patterns.yaml`` parsed and
       cross-checked against the registry's ``regex_router`` surface.
    3. L3 LLM client — :class:`jarvis.decision.llm.LLMClient` reads the
       ``llm:`` block from the YAML config. Lazy SDK construction —
       no network call until ``run_turn`` actually invokes ``chat``.

    L5 :class:`SurfaceState` is allocated per turn inside :func:`run_turn`
    (spec §3.6.7 — local surface state owns no truth and doesn't survive
    across turns), so the composition root does not wire it here.

    Args:
        config: The parsed config, with the role's overrides applied.
        paths: The runtime root's layout.

    Returns:
        Frozen :class:`JarvisRuntime`.

    Raises:
        RuntimeBootstrapError: When the prompt file, the Tier 0, grammar or cue tables, or the
            ``llm`` section is missing or malformed, or the zone database has no rule for now.
    """
    config_path, repo_root = config.config_path, config.repo_root
    if not config.prompt_path.is_file():
        msg = f"runtime: prompt file {config.prompt_path} does not exist"
        raise RuntimeBootstrapError(msg)
    system_clock = SystemClock(config.zone)
    try:
        system_clock.check_rule()
    except ZoneError as exc:
        msg = f"runtime: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 1. L2 event log.
    conn = open_event_log(paths.event_log)

    # 2. L4 registry + lifecycle. `search_notes`/`web_search`/`web_fetch`
    #    need `tools.obsidian.vault_root` / `tools.web.*` threaded into
    #    the registry at build time (ADR-0011 D7) — L4 handlers do not
    #    load YAML themselves.
    role = config.role
    listen_addresses = config.listen_addresses
    lang.set_language(config.language)
    log_llm_io = config.diagnostics.log_llm_io
    llm_io_log.configure(logs_dir(paths.root) / "llm-io.jsonl" if log_llm_io else None)
    wave1_features = config.realtime.wave1
    tools = config.tools
    web_search_provider, web_search_api_key = _web_search_credentials(tools.web)
    _install_open_path(tools.open_path)
    memory = config.memory
    voiceprint_gate = config.voiceprint_gate  # ADR 0286, read once at boot
    # ADR 0170: a brain's TimeSink and git are its terminal's, so what reads them asks the hub.
    locate_waits = LocateWaits()
    phone_events = (
        BrainEvents(conn, locate_waits) if role == "brain" or listen_addresses else None
    )
    terminal_hub = TerminalHub(events=phone_events) if role == "brain" else None
    device = None if terminal_hub is None else terminal_hub.call
    settings_drift = _settings_drift(terminal_hub, paths, config.raw)
    moment = _moment(
        config.raw, config_path, memory.db_path, device, config.observer.timesink_db_path,
    )
    # ADR 0068: refuse a memory.db a newer Jarvis wrote before anything writes to it.
    open_memory_db(memory.db_path).close()
    local_zone = config.zone
    # ADR 0234: home and school move out of the settings into the places table, once.
    place_state.migrate_settings(memory.db_path, config.transit_places)
    maps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    _start_locating_places(memory.db_path, maps_key)
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
        if role == "brain"  # ADR 0170: holding a Mac awake and dark is the terminal's job
        else NightRun(
            paths.event_log,
            night_settings(config.raw),
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
        tools.vision_preset,
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
        None if phone_events is None else _locator(config.locate, locate_waits, system_clock)
    )
    here = HerePolicy(  # ADR 0322: the one Here every tool call reads where he is from
        paths.event_log, system_clock, locator=locator, mac=_here_location(role),
        memory_db=memory.db_path, phone=config.locate.phone, max_age_s=config.locate.max_age_s,
    )
    weatherkit = WeatherKit.from_config(config.raw)  # ADR 0285: logs which source is used
    surroundings = Surroundings(  # ADR 0284: held by position; the tool and the state line share it
        maps_key, config.maps_monthly_usd, memory.db_path, local_zone,
    )
    doing = _doing_schedule(config, memory, paths, device)
    jev_log = _jev_log(config.raw, paths.root)

    def make_late(connections: PluginConnections) -> tuple[JobMail | None, Home]:
        _wire_plan_reader(daily_report, connections)
        job_mail = _job_mail(
            config.raw, config_path, jev_log, connections, memory.db_path, system_clock,
            moment, device, paths.event_log, config.observer.timesink_db_path,
        )
        home = Home(
            connections,
            (config.zone_name, config.zone),
            config.home_weather,
            _mail_reply(config.raw, config_path, system_clock, jev_log),
            mail_view,
            None if mail_view is None else mail_summarizer(
                # ADR 0148: the cheapest preset (gpt-6-luna), never Jev: a body goes here.
                build_analyst(
                    config.raw,
                    config.work_state.preset,
                    pricing_path=config.pricing_path,
                    account_cost=True,
                    kind="mail_summary",
                ),
                paths.event_log,
            ),
            weatherkit,
        )
        return job_mail, home

    build_registry = partial(
        build_default_registry,
        mail_drafts=mail_drafts,
        memory_db_path=memory.db_path,
        observed_repos=config.observer.repos,
        timesink_db_path=config.observer.timesink_db_path,
        work_state_refresh=_work_state_tool_refresh(work_state, daily_report),
        night=night,
        voice_settings=voice_settings,
        weather_lookup=weather_lookup(
            config.home_weather, weatherkit,
            config.zone_name,
        ),
        transit_api_key=maps_key,
        maps_monthly_usd=config.maps_monthly_usd,
        surroundings=surroundings,
        transit_places=kept_places,
        transit_offers=transit_offers,
        clock=system_clock,
        here=here,
        bus_live=_live_departure(bus_live),
        ride_control=rides.control(),
        highlight_marker=_highlight_marks(config, memory, paths, device).mark,
        plan_day_ends=config.plan_day_ends,
        ring_before_min=config.remind_before_min,
        confirmation_dispatch_outbox=wave1_features.confirmation_dispatch_outbox,
        obsidian_vault_root=tools.obsidian_vault_root,
        web_search_max_results=tools.web.search_max_results,
        web_search_provider=web_search_provider,
        web_search_api_key=web_search_api_key,
        web_fetch_max_bytes=tools.web.fetch_max_bytes,
        web_fetch_max_text_bytes=tools.web.fetch_max_text_bytes,
        web_timeout_s=tools.web.timeout_s,
        web_deep_search=deep_search,
        vision_client=vision_client,
        screen_max_width_px=tools.screen_max_width_px,
        device_link=device,
    )
    served = register_catalog(
        build_registry=build_registry,
        offer_devices=partial(device_tools.follow_terminals, terminal_hub, paths.root),
        paths=paths,
        workers=tools.workers,
        view=view,
        make_plugins=lambda reg: PluginConnections(
            repo_root=repo_root, runtime_root=paths.root, event_log=paths.event_log,
            registry=reg, config=config.raw,
        ),
        make_late=make_late,
        memory_db=memory.db_path,
        doing=doing,
        ledger=LedgerSources(
            memory.db_path, paths.event_log, config.observer.timesink_db_path, local_zone,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
        ),
    )
    registry, workers, plugin_connections = served.registry, served.workers, served.plugins
    job_mail, home = served.job_mail, served.home
    lifecycle = ActionLifecycle()

    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = _on_menu(load_tier0_table(tier0_path), registry, role)
        regex_router_tools = _regex_router_tools(registry)
        validate_tier0_table(
            tier0_table,
            allowed_tool_names=frozenset(t.name for t in regex_router_tools),
            entity_required_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_entity
            ),
            # ADR-0012 §3 D5 — a Tier 0 row must never target a
            # `requires_confirmation` tool (Tier 0 has no LLM to
            # receive Allen's confirmation answer).
            requires_confirmation_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_confirmation
            ),
        )
    except Tier0ConfigError as exc:
        msg = f"runtime: {tier0_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3c. ADR-0011 D2 — boot invariant: every registered tool's
    #     `requires_confirmation` must equal `risk_rank(risk_level) >=
    #     risk_rank(confirmation_threshold)`, so the two fields can never
    #     drift (spec §14.2 / ADR-0011 V6). Loud failure, same shape as
    #     the Tier 0 block above.
    try:
        validate_requires_confirmation(
            registry.get_definitions(),
            effective_policy().confirmation_threshold,
        )
    except PolicyConsistencyError as exc:
        msg = f"runtime: tool registry requires_confirmation invariant violated: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3e. ADR-0012 §3 D6 — answer-path grammar table. Same posture as
    #     3b above: sits next to jarvis.yaml, missing file = the
    #     grammar hook disabled (inert, not broken), malformed content
    #     fails the boot loudly.
    confirm_grammar_path = config_path.parent / "confirm_grammar.yaml"
    try:
        confirm_grammar_table = load_confirm_grammar(confirm_grammar_path)
    except ConfirmGrammarConfigError as exc:
        msg = f"runtime: {confirm_grammar_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3d. ADR-0008 Step 8 tool cues — same posture as the grammar table.
    tool_cues_path = config_path.parent / "tool_cues.yaml"
    try:
        tool_cues = load_tool_cues(tool_cues_path)
    except ToolCueConfigError as exc:
        msg = f"runtime: {tool_cues_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 4. L3 LLM client. `config.raw` was already loaded at step 3 above.
    llm_config = config.raw.get("llm")
    if not isinstance(llm_config, Mapping):
        msg = f"runtime: {config_path} has no 'llm' section"
        raise RuntimeBootstrapError(msg)
    llm_client = LLMClient(llm_config)
    think_mode = _think_mode(llm_config, config_path)

    # 4b. ADR-0008 Step 2 (Wave 4A). The whole flag graph was resolved once, when the
    #     config was parsed; every downstream site reads the validated result. Every
    #     runtime gets the session factory and the cancel registry (ADR 0332).
    response_flags = config.realtime.response.flags
    # The bus is gated on `realtime.enabled` rather than a Wave-4A switch so
    # Waves 4B/4C reuse this same line. It is inert while nobody subscribes.
    committed_event_bus = CommittedEventBus() if config.realtime.enabled else None
    llm_session_factory = LLMSessionFactory(llm_config)
    response_runs = ResponseRunRegistry()

    # Plugin skills are sampled per turn so connections need no daemon restart.
    system_prompt = config.prompt_path.read_text(encoding="utf-8").replace(
        "{assistant}", config.assistant_name
    )
    if config.reply_language in REPLY_LINES:
        reply_line = REPLY_LINES[config.reply_language]
        system_prompt = f"{system_prompt.rstrip()}\n\n{reply_line}\n"
    plugin_connections.publish_event = (
        committed_event_bus.publish if committed_event_bus is not None else None
    )

    mail_codes = _mail_codes(config.raw, config_path, plugin_connections, device)
    commitments = _commitments(
        config.raw, config_path, plugin_connections, memory.db_path, paths.event_log,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="commitments",
        ),
    )
    claude_daily = _claude_daily(
        config.raw, config_path, plugin_connections, memory.db_path, paths.root,
    )
    things_ledger = _things_ledger(
        paths.event_log, memory.db_path, local_zone, config.remind_before_min,
    )
    things_mirror = ThingsMirror(plugin_connections, paths.event_log, local_zone)
    turn_things = _turn_things(
        config.raw, config_path, memory.db_path, paths.event_log, local_zone,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="turn_things",
        ),
    )
    reminders = Reminders(
        paths.event_log,
        moment=moment,
        departures=Departures(paths.event_log, bus_live, transit_offers),
    )
    output = _sound_output(terminal_hub, job_mail, reminders)
    attachments = Attachments(paths.artifacts_root / ATTACHMENTS_DIRNAME)
    picture_files = PictureFiles(attachments, memory_db=memory.db_path)
    set_stale_files_handler(picture_files.stale)  # ADR 0255
    runtime = JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=registry,
        lifecycle=lifecycle,
        llm_client=llm_client,
        system_prompt=system_prompt,
        clock=system_clock,
        terminal_hub=terminal_hub,
        settings_drift=settings_drift,
        phone_events=phone_events,
        locator=locator,
        attachments=attachments,
        picture_files=picture_files,
        picture_notes=(
            None if vision_client is None else PictureNotes(
                memory_db=memory.db_path,
                store=attachments,
                vision=vision_client,
                event_log=paths.event_log,
                pinned_language=config.reply_language,
            )
        ),
        tier0_table=tier0_table,
        confirm_grammar_table=confirm_grammar_table,
        confirmation_words=_confirmation_words_client(config),
        wave1_features=wave1_features,
        response_flags=response_flags,
        llm_session_factory=llm_session_factory,
        response_runs=response_runs,
        committed_event_bus=committed_event_bus,
        decision_state=DecisionStateCache(
            partial(open_runtime_event_log, paths.event_log),
            is_idle=partial(_daemon_idle, paths.event_log),
        ),
        surrogate_route=_surrogate_route(config.raw, config_path, system_clock, jev_log),
        memory=memory,
        workers=workers,
        plugin_connections=plugin_connections,
        voiceprints=VoiceprintBook(memory.db_path, gate=lambda: voiceprint_gate),
        tool_cues=tool_cues,
        think_mode=think_mode,
        work_state=work_state,
        work_state_auto=config.work_state.auto,
        projects=projects,
        home=home,
        view=view,
        doing=doing,
        mail_drafts=mail_drafts,
        ambient=ambient,
        live_context=(
            rides.line,
            *(() if view is None else (view.line,)),
            *(() if mail_drafts is None else (mail_drafts.line,)),
            voice_cues.line,
            *(() if ambient is None else (ambient.line,)),
            partial(_shares_line, paths.event_log),
            # ADR 0249: what he is doing now, with its age, then his next things, read from the
            # ledger of his things (ADR 0299).
            partial(doing_now_line, paths.event_log, local_zone, system_clock.now),
            partial(calendar_line, paths.event_log, local_zone, system_clock.now),
            # ADR 0291: today's plan, settled or not, with its next item.
            partial(
                plan_line, paths.event_log, local_zone, system_clock.now,
                config.plan_day_ends,
            ),
        ),
        voice_cues=voice_cues,
        turn_end_asks=_turn_end_asks(config.raw, config_path, system_clock, jev_log),
        job_mail=job_mail,
        mail_codes=mail_codes,
        commitments=commitments,
        claude_daily=claude_daily,
        things=things_ledger,
        things_mirror=things_mirror,
        turn_things=turn_things,
        moment=moment,
        push=Push.from_config(config.raw, paths.root, paths.event_log),
        whereabouts=Whereabouts(
            paths.root, terminal_hub, moment, memory_db=memory.db_path, zone=local_zone,
            around=surroundings, here=here,
        ),
        about_items=_about_items(config, memory, paths),
        reminders=reminders,
        rides=rides,
        leg_planner=None if not maps_key else partial(
            build_leg_planner, maps_key, kept_places,
            kept=partial(place_state.read_kept, memory.db_path),
            cap_usd=config.maps_monthly_usd,
        ),
        voice_words=_voice_words(config.raw, config_path, system_clock, jev_log),
        oneshot=_jev_oneshot(
            config.raw, config_path, system_clock, jev_log, tier0_table,
            _event_emitter(paths.event_log),
        ),
        settings=Settings(
            paths.root,
            config.raw,
            _no_audio_devices if role == "brain" else _audio_devices,
            _no_default_audio_device if role == "brain" else _default_audio_device,
            owned=page_keys("brain", "both") if role == "brain" else None,
        ),
        night=night,
        voice_settings=voice_settings,
        daily_schedule=_daily_schedule(daily_report, paths.event_log, config, terminal_hub),
        ledger=_ledger(config, memory, paths.event_log, device, attachments.directory),
        places_block=partial(_places_block, memory.db_path, paths.event_log, local_zone),
        sound_output=output,
    )
    _follow_conversation_setting(runtime.settings, llm_session_factory)
    return runtime
