"""The composition root's boot: :func:`bootstrap_runtime_app`, its stages run under one stack."""

from __future__ import annotations

from contextlib import ExitStack
from functools import partial
from typing import TYPE_CHECKING

from jarvis.decision.llm_anthropic import set_stale_files_handler
from jarvis.execution.tools import ActionLifecycle
from jarvis.execution.transit_tool import build_leg_planner
from jarvis.runtime import JarvisRuntime
from jarvis.runtime.bootstrap.catalog import load_tables, serve_tools
from jarvis.runtime.bootstrap.core import _daemon_idle, open_core
from jarvis.runtime.bootstrap.day import (
    _about_items,
    _daily_schedule,
    _ledger,
    _places_block,
    _shares_line,
)
from jarvis.runtime.bootstrap.devices import (
    _audio_devices,
    _default_audio_device,
    _no_audio_devices,
    _no_default_audio_device,
    _sound_output,
)
from jarvis.runtime.bootstrap.features import build_features
from jarvis.runtime.bootstrap.intakes import build_intakes
from jarvis.runtime.bootstrap.jev import (
    _event_emitter,
    _jev_oneshot,
    _surrogate_route,
    _turn_end_asks,
    _voice_words,
)
from jarvis.runtime.bootstrap.llm import (
    _confirmation_words_client,
    _follow_conversation_setting,
    build_llm,
)
from jarvis.runtime.bootstrap.places import _start_locating_places
from jarvis.runtime.core_memory import checkin_line
from jarvis.runtime.decision_state import DecisionStateCache
from jarvis.runtime.departures import Departures
from jarvis.runtime.mail_page import MailPage
from jarvis.runtime.now_block import calendar_line, doing_now_line, plan_line
from jarvis.runtime.picture_files import PictureFiles
from jarvis.runtime.picture_notes import PictureNotes
from jarvis.runtime.push import Push
from jarvis.runtime.reminders import Reminders
from jarvis.runtime.settings import Settings
from jarvis.runtime.settings_owner import page_keys
from jarvis.runtime.whereabouts import Whereabouts
from jarvis.state import places as place_state
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.voiceprint import VoiceprintBook

if TYPE_CHECKING:
    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.bootstrap.catalog import Catalog, Tables
    from jarvis.runtime.bootstrap.core import Core
    from jarvis.runtime.bootstrap.features import Features
    from jarvis.runtime.bootstrap.intakes import Intakes
    from jarvis.runtime.bootstrap.llm import Llm
    from jarvis.runtime.config import RuntimeConfig


def bootstrap_runtime_app(config: RuntimeConfig, paths: RuntimePaths) -> JarvisRuntime:
    """Assemble a frozen :class:`JarvisRuntime` ready for :func:`~.turn.drive.drive_turn`.

    The caller has laid out the runtime root (:func:`jarvis.deployment.bootstrap_runtime`),
    loaded its environment (:func:`load_runtime_env_and_trace`) and parsed the config
    (:func:`~jarvis.runtime.config.load_runtime_config`): a bad value has stopped it before
    anything here opens a file.

    Stages, bottom-up across the layer DAG, each in its own module:

    1. :func:`~jarvis.runtime.bootstrap.core.open_core`: the L2 Event Log and the memory store.
    2. :func:`~jarvis.runtime.bootstrap.features.build_features`: the services built from the
       config, the stores and the paired devices.
    3. :func:`~jarvis.runtime.bootstrap.catalog.serve_tools`: the L4 registry, the tools and the
       plugin connections.
    4. :func:`~jarvis.runtime.bootstrap.catalog.load_tables`: the L3 Tier 0, grammar and cue
       tables, checked against the registry.
    5. :func:`~jarvis.runtime.bootstrap.llm.build_llm`: the L3 LLM client and the prompt. Lazy
       SDK construction: no network call until a turn invokes ``chat``.
    6. :func:`~jarvis.runtime.bootstrap.intakes.build_intakes`: the mail and record intakes.

    The stages run under one :class:`~contextlib.ExitStack`. A stage that opens something the
    runtime would own (the Event Log connection, the plugin connections) hands the stack the call
    that closes it; if any later stage fails, the stack closes them and the failure is raised as
    it was. When the runtime is built the stack is released: the runtime owns them (ADR 0372).
    What cannot be undone is done after the last stage that can fail.

    L5 :class:`SurfaceState` is allocated per turn inside
    :func:`~jarvis.runtime.turn.drive.drive_turn` (spec §3.6.7 — local surface state owns no
    truth and doesn't survive across turns), so the composition root does not wire it here.

    Args:
        config: The parsed config, with the role's overrides applied.
        paths: The runtime root's layout.

    Returns:
        Frozen :class:`JarvisRuntime`.

    Raises:
        RuntimeBootstrapError: When the prompt file, the Tier 0, grammar or cue tables, or the
            ``llm`` section is missing or malformed, or the zone database has no rule for now.
    """
    with ExitStack() as stack:
        core = open_core(config, paths, stack)
        features = build_features(config, paths, core)
        served = serve_tools(config, paths, core, features, stack)
        tables = load_tables(config, served.registry)
        llm = build_llm(config)
        served.plugins.publish_event = (
            llm.committed_event_bus.publish if llm.committed_event_bus is not None else None
        )
        intakes = build_intakes(config, paths, core, served.plugins)
        runtime = _assemble(config, paths, core, features, served, tables, llm, intakes)
        stack.pop_all()
    _start_locating_places(core.memory.db_path, features.maps_key)
    return runtime


def _assemble(  # noqa: PLR0913 - one argument per stage; the runtime is every stage's result
    config: RuntimeConfig, paths: RuntimePaths, core: Core, features: Features, served: Catalog,
    tables: Tables, llm: Llm, intakes: Intakes,
) -> JarvisRuntime:
    """The :class:`JarvisRuntime` over the stages' results, with the last features built for it.

    The late Jev routes, the Settings page and the reminders are built here, in the order the
    runtime lists them; the stale-files handler (ADR 0255) is the last thing set, once nothing
    after it can fail.
    """
    memory, clock, terminal_hub = core.memory, core.clock, core.terminal_hub
    role = config.role
    reminders = Reminders(
        paths.event_log,
        moment=features.moment,
        departures=Departures(paths.event_log, features.bus_live, features.transit_offers),
    )
    output = _sound_output(terminal_hub, served.job_mail, reminders)
    attachments = Attachments(paths.artifacts_root / ATTACHMENTS_DIRNAME)
    picture_files = PictureFiles(attachments, memory_db=memory.db_path)
    voiceprint_gate = config.voiceprint_gate  # ADR 0286, read once at boot
    mail_page = (  # ADR 0366: the draft under the page's open letter is a Gmail draft
        None if features.mail_view is None or served.home is None
        else MailPage(features.mail_view, served.home, paths.event_log, clock)
    )
    runtime = JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=core.conn,
        tool_registry=served.registry,
        lifecycle=ActionLifecycle(),
        llm_client=llm.client,
        system_prompt=llm.system_prompt,
        clock=clock,
        terminal_hub=terminal_hub,
        settings_drift=core.settings_drift,
        phone_events=core.phone_events,
        locator=features.locator,
        attachments=attachments,
        picture_files=picture_files,
        picture_notes=(
            None if features.vision_client is None else PictureNotes(
                memory_db=memory.db_path,
                store=attachments,
                vision=features.vision_client,
                event_log=paths.event_log,
                pinned_language=config.reply_language,
            )
        ),
        tier0_table=tables.tier0,
        confirm_grammar_table=tables.confirm_grammar,
        confirmation_words=_confirmation_words_client(config),
        wave1_features=config.realtime.wave1,
        response_flags=llm.response_flags,
        llm_session_factory=llm.session_factory,
        response_runs=llm.response_runs,
        committed_event_bus=llm.committed_event_bus,
        decision_state=DecisionStateCache(
            partial(open_runtime_event_log, paths.event_log),
            is_idle=partial(_daemon_idle, paths.event_log),
        ),
        surrogate_route=_surrogate_route(config.jev.surrogate_route, clock, features.jev_log),
        memory=memory,
        workers=served.workers,
        plugin_connections=served.plugins,
        voiceprints=VoiceprintBook(memory.db_path, gate=lambda: voiceprint_gate),
        tool_cues=tables.tool_cues,
        think_mode=llm.think_mode,
        work_state=features.work_state,
        work_state_auto=config.work_state.auto,
        projects=features.projects,
        home=served.home,
        view=features.view,
        doing=features.doing,
        mail_page=mail_page,
        ambient=features.ambient,
        live_context=(
            features.rides.line,
            *(() if features.view is None else (features.view.line,)),
            *(() if mail_page is None else (mail_page.line,)),
            features.voice_cues.line,
            *(() if features.ambient is None else (features.ambient.line,)),
            partial(_shares_line, paths.event_log),
            # ADR 0249: what he is doing now, with its age, then his next things, read from the
            # ledger of his things (ADR 0299).
            partial(doing_now_line, paths.event_log, config.zone, clock.now),
            partial(calendar_line, paths.event_log, config.zone, clock.now),
            # ADR 0291: today's plan, settled or not, with its next item.
            partial(
                plan_line, paths.event_log, config.zone, clock.now,
                config.plan_day_ends,
            ),
            # ADR 0389: once a day, a thread of his life that has gone quiet, to ask about.
            partial(checkin_line, memory.db_path, config.zone, clock.now),
        ),
        voice_cues=features.voice_cues,
        turn_end_asks=_turn_end_asks(config.jev.turn_end_asks, clock, features.jev_log),
        job_mail=served.job_mail,
        mail_codes=intakes.mail_codes,
        commitments=intakes.commitments,
        claude_daily=intakes.claude_daily,
        things=intakes.things_ledger,
        things_mirror=intakes.things_mirror,
        turn_things=intakes.turn_things,
        moment=features.moment,
        push=Push.from_settings(config.push, paths.root, paths.event_log),
        whereabouts=Whereabouts(
            paths.root, terminal_hub, features.moment, memory_db=memory.db_path, zone=config.zone,
            around=features.surroundings, here=features.here,
        ),
        about_items=_about_items(config, memory, paths),
        reminders=reminders,
        rides=features.rides,
        leg_planner=None if not features.maps_key else partial(
            build_leg_planner, features.maps_key, features.kept_places,
            kept=partial(place_state.read_kept, memory.db_path),
            cap_usd=config.maps_monthly_usd, bus_live=features.bus_live,
        ),
        voice_words=_voice_words(config.jev.voice_words, clock, features.jev_log),
        oneshot=_jev_oneshot(
            config.jev.oneshot, clock, features.jev_log, tables.tier0,
            _event_emitter(paths.event_log),
        ),
        settings=Settings(
            paths.root,
            config.raw,
            _no_audio_devices if role == "brain" else _audio_devices,
            _no_default_audio_device if role == "brain" else _default_audio_device,
            owned=page_keys("brain", "both") if role == "brain" else None,
        ),
        night=features.night,
        voice_settings=features.voice_settings,
        daily_schedule=_daily_schedule(
            features.daily_report, paths.event_log, config, terminal_hub,
        ),
        ledger=_ledger(config, memory, paths.event_log, core.device, attachments.directory),
        places_block=partial(_places_block, memory.db_path, paths.event_log, config.zone),
        sound_output=output,
    )
    _follow_conversation_setting(runtime.settings, llm.session_factory)
    set_stale_files_handler(picture_files.stale)  # ADR 0255
    return runtime
