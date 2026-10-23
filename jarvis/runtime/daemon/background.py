"""The tasks the daemon runs beside the server: its watchers, schedules and the power observer."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Mapping
from typing import TYPE_CHECKING

from jarvis.deployment.launchd import logs_dir
from jarvis.runtime.bootstrap.jev import build_jev_log
from jarvis.runtime.core_memory import CoreMemorySettings
from jarvis.runtime.daemon.commentary import commentary_watcher
from jarvis.runtime.daemon.conversation_lines import plan_hooks, say_job_line, say_reminder
from jarvis.runtime.daemon.devices import start_audio_device_watch, watch_board
from jarvis.runtime.daemon.intent_pump import start_intent_pump
from jarvis.runtime.daemon.observers import (
    agents_port,
    start_meaning_index,
    start_repo_observer,
    start_timesink_observer,
    start_usage_observer,
)
from jarvis.runtime.daemon.output_watchers import (
    log_watcher_death,
    phone_ends_watcher,
    response_watcher,
    tool_status_watcher,
    tts_watcher,
)
from jarvis.runtime.daemon.retention import (
    data_sweep_task,
    link_earlier_pictures,
    media_dirs,
    release_uploaded_pictures,
)
from jarvis.runtime.daemon.routes import her_state, open_lifelog
from jarvis.runtime.daemon.sweep import start_sweep_control_plane
from jarvis.runtime.daemon.voice_power import (
    VoicePowerCoordinator,
    install_power_observer_or_degrade,
)
from jarvis.runtime.day_experience import DayExperienceSchedule, DayExperienceSettings
from jarvis.runtime.day_plan import DayPlanSettings, DayPlanSources, build_schedule
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.runtime.night_watch import NightWatch
from jarvis.runtime.plan_run import build_watch, leg_session, make_route_legs
from jarvis.runtime.session_compaction import CompactionSweep, preset_context_length
from jarvis.runtime.source_silence import SourceSilenceWatch
from jarvis.runtime.spend_cap import SpendCap
from jarvis.runtime.work_state import auto_refresh
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.day_line import DaySources
from jarvis.state.ledger import TURN_WAIT_S, TerminalScreen
from jarvis.state.plugin_settings import local_key
from jarvis.surface.claude_sessions import ClaudeSessions

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.daemon.teardown import Running
    from jarvis.runtime.daemon.voice_boot import Voice
    from jarvis.runtime.setup import Setup
    from jarvis.runtime.spend_cap import SpendCapSettings
    from jarvis.surface.codex_sessions import CodexSession
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.phone_link import PhoneHub
    from jarvis.surface.usage_observer import UsageObserver
    from jarvis.surface.voice_controls import VoiceControls


async def start_background(  # noqa: PLR0913 - what boot built that the tasks read; each is read once
    runtime: JarvisRuntime,
    running: Running,
    *,
    broadcaster: InherentBroadcaster,
    setup: Setup | None,
    usage_observer: UsageObserver | None,
    codex_board: dict[str, CodexSession],
    phone_hub: PhoneHub | None,
    phone_ends: Callable[[str], int | None] | None,
    spend: SpendCapSettings | None,
    poll_interval_s: float,
) -> None:
    """Start every watcher and schedule onto ``running.watchers``, in the order boot needs.

    The list is extended in place as each task starts, so a step that fails leaves the ones
    already running where :func:`jarvis.runtime.daemon.teardown.shutdown` cancels them.
    """
    watchers = running.watchers
    controls = running.voice.controls
    await _start_delivery(
        runtime, watchers,
        broadcaster=broadcaster, voice=running.voice, setup=setup, phone_ends=phone_ends,
        poll_interval_s=poll_interval_s,
    )
    await _start_sweep(runtime, watchers, voice=running.voice, poll_interval_s=poll_interval_s)
    # ADR-0009 D5 — repo observer: baselines recovered from the log
    # first, then one more watchers-list task polling `observer.repos`
    # every `observer.poll_interval_s`. Nothing it emits is a trigger,
    # so it is wired after the sweep control plane without disturbing
    # that anchor-then-bootstrap ordering.
    watchers.extend(start_repo_observer(runtime))
    watchers.extend(start_usage_observer(usage_observer, runtime.config))
    watchers.extend(start_timesink_observer(runtime))
    watchers.extend(start_meaning_index(runtime))
    _start_feeds(runtime, watchers, controls, spend)
    _start_reminders(runtime, watchers, controls, phone_hub)
    _start_night(runtime, watchers, running.voice, codex_board)
    _start_day_schedules(runtime, watchers, controls)
    _start_housekeeping(runtime, watchers)
    for watcher in watchers:
        watcher.add_done_callback(log_watcher_death)


async def _start_delivery(  # noqa: PLR0913 - the pump and the watchers that carry events out
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    *,
    broadcaster: InherentBroadcaster,
    voice: Voice,
    setup: Setup | None,
    phone_ends: Callable[[str], int | None] | None,
    poll_interval_s: float,
) -> None:
    """The intent pump and the watchers that carry events out to the surfaces and the speakers."""
    # ADR-0008 D8 (Step 4). Adoption and the recovery scan happen
    # inside the startup barrier, before any task exists that could
    # race them.
    watchers.extend(
        await start_intent_pump(
            runtime,
            poll_interval_s=poll_interval_s,
            queue_capacity=runtime.config.realtime.input_queue_capacity,
            max_concurrent_turns=runtime.config.realtime.input_max_concurrent_turns,
        ),
    )
    watchers.append(
        asyncio.create_task(
            response_watcher(
                runtime,
                broadcaster,
                poll_interval_s=poll_interval_s,
                voiced=voice.tts is not None,
            ),
            name="response_watcher",
        ),
    )
    watchers.append(
        asyncio.create_task(
            tool_status_watcher(runtime, broadcaster, poll_interval_s=poll_interval_s),
            name="tool_status_watcher",
        ),
    )
    if setup is not None:
        # The keys' boot check: a model list and one word of TTS, off the loop;
        # failures are logged and served by GET /inherent/setup.
        watchers.append(
            asyncio.create_task(asyncio.to_thread(setup.check_at_boot), name="setup_key_check"),
        )
    if voice.tts is not None:
        watchers.append(
            asyncio.create_task(
                tts_watcher(
                    conn=runtime.conn,
                    pipeline=voice.tts,
                    poll_interval_s=poll_interval_s,
                    broadcaster=broadcaster,
                    text_only=(
                        None if runtime.response_runs is None
                        else runtime.response_runs.turn_text_only
                    ),
                ),
                name="tts_watcher",
            ),
        )
    if phone_ends is not None:
        # ADR 0222: a phone voice answer that ends with no actor for its device still ends.
        watchers.append(
            asyncio.create_task(
                phone_ends_watcher(
                    runtime.conn, phone_ends, poll_interval_s=poll_interval_s,
                ),
                name="phone_ends_watcher",
            ),
        )
    if runtime.response_flags.lifecycle_commentary:
        # ADR-0008 D6 (Step 5). Flag off, no task exists and the event log
        # is byte-identical to a build without this observer.
        watchers.append(
            asyncio.create_task(
                commentary_watcher(runtime, poll_interval_s=poll_interval_s),
                name="commentary_watcher",
            ),
        )


async def _start_sweep(
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    *,
    voice: Voice,
    poll_interval_s: float,
) -> None:
    """ADR-0009 D4: the sweep control plane, with the session compaction on its tick."""
    # ADR-0009 D4 — the sweep control plane: anchor the system-trigger
    # watcher, THEN run the bootstrap sweep, THEN start the periodic
    # task. The ordering is load-bearing and lives inside the helper.
    # The session compaction rides the same tick: eligible only while
    # nothing is being written and no Live connection is open.
    compaction = (
        CompactionSweep(
            memory=runtime.memory,
            settings=runtime.config.session,
            llm_config=(
                runtime.config.raw["llm"]
                if isinstance(runtime.config.raw.get("llm"), Mapping)
                else {}
            ),
            event_log_path=runtime.runtime_paths.event_log,
            pricing_table=load_pricing_table(runtime.config.pricing_path),
            context_length=lambda: preset_context_length(runtime.llm_client),
            live_open=lambda: (
                voice.live is not None and voice.live.status().get("state") != "idle"
            ),
        )
        if runtime.memory is not None
        else None
    )
    watchers.extend(
        await start_sweep_control_plane(
            runtime, poll_interval_s=poll_interval_s, compaction=compaction,
        ),
    )


def _start_source_silence(runtime: JarvisRuntime, watchers: list[asyncio.Task[None]]) -> None:
    """ADR 0382: a card when a source the Mac sends stops arriving; served with the alert rows."""
    if runtime.memory is None:
        return
    watch = SourceSilenceWatch(
        runtime.runtime_paths.root, open_lifelog(runtime).directory,
        runtime.memory.db_path, runtime.config.zone,
    )
    watchers.append(asyncio.create_task(watch.run(), name="source_silence"))


def _start_feeds(
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    controls: VoiceControls,
    spend: SpendCapSettings | None,
) -> None:
    """The mail, commitment, things and work-state tasks, and the spend cap."""
    if runtime.job_mail is not None:
        # ADR 0155: she speaks only at quiet off, unmuted and outside a live conversation.
        job_mail = runtime.job_mail
        job_mail.quiet = lambda: controls.quiet
        job_mail.may_speak = lambda: not controls.speech_muted and not controls.conversation
        job_mail.say = functools.partial(say_job_line, runtime)
        watchers.append(asyncio.create_task(job_mail.run(), name="job_mail"))
    if runtime.mail_codes is not None:
        watchers.append(asyncio.create_task(runtime.mail_codes.run(), name="mail_codes"))
    if runtime.commitments is not None:
        watchers.append(asyncio.create_task(runtime.commitments.run(), name="commitments"))
    if runtime.things is not None:
        # ADR 0299: the rings of his things for the next seven days, now and every 15 minutes.
        watchers.append(asyncio.create_task(runtime.things.run(), name="things"))
    if runtime.things_mirror is not None:
        # ADR 0299: Outlook and To Do read into the ledger, now and every 15 minutes.
        watchers.append(asyncio.create_task(runtime.things_mirror.run(), name="things_mirror"))
    if runtime.turn_things is not None:
        # ADR 0300: each record of his, once answered, read into the ledger.
        watchers.append(asyncio.create_task(runtime.turn_things.run(), name="turn_things"))
    if runtime.claude_daily is not None:
        watchers.append(asyncio.create_task(runtime.claude_daily.run(), name="claude_daily"))
    if spend is not None and runtime.memory is not None:
        # ADR 0185: the card is a job-alert row; _alert_deps serves it with job mail off too.
        watchers.append(asyncio.create_task(
            SpendCap(spend, runtime.runtime_paths.event_log, runtime.memory.db_path).run(),
            name="spend_cap",
        ))
    _start_source_silence(runtime, watchers)
    if runtime.work_state_auto and runtime.work_state and runtime.moment:
        # ADR 0180: a changed front app or site re-analyses the work state, silently.
        watchers.append(asyncio.create_task(
            auto_refresh(runtime.work_state, runtime.moment), name="work_state_auto",
        ))


def _start_reminders(
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    controls: VoiceControls,
    phone_hub: PhoneHub | None,
) -> None:
    """Reminders, the bus rides, and the push that speaks for both."""
    if runtime.reminders is not None:
        # ADR 0179: reminders speak when unmuted and out of a conversation, at any quiet level.
        reminders = runtime.reminders
        reminders.may_speak = lambda: not controls.speech_muted and not controls.conversation
        reminders.say = functools.partial(say_reminder, runtime)
        watchers.append(asyncio.create_task(reminders.run(), name="reminders"))
    if runtime.rides is not None:
        # ADR 0224: the bus ride follower; it fetches a feed only while there is a reason.
        watchers.append(asyncio.create_task(runtime.rides.run(), name="rides"))
    if runtime.push is not None and runtime.push.enabled:
        # ADR 0210: a fired reminder pushes at any quiet level; waiting cards follow the level.
        push = runtime.push
        push.quiet = lambda: controls.quiet
        push.her_state = lambda: her_state(controls)
        if phone_hub is not None:
            # ADR 0209: a device with a live conversation socket needs no push.
            push.phone_socket_open = phone_hub.connected
        if runtime.locator is not None:
            # ADR 0281: a phone with no socket is woken for a fix by a silent push.
            runtime.locator.ask_push = push.locate
        if runtime.whereabouts is not None:
            # ADR 0218: a request, or a card no turn asked, goes to the phone only away from
            # the Mac.
            push.at_mac = runtime.whereabouts.at_mac
        if runtime.plugin_connections is not None:
            # ADR 0232: an app's login link goes to the phone that asked for it.
            push.plugin_login = runtime.plugin_connections.login_waiting
        if runtime.reminders is not None:
            runtime.reminders.push = push.reminder
            push.next_reminder = runtime.reminders.next_due
        if runtime.rides is not None:
            runtime.rides.push = push
        watchers.append(asyncio.create_task(push.run(), name="push"))


def _start_night(
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    voice: Voice,
    codex_board: dict[str, CodexSession],
) -> None:
    """The night run, with what it waits on, and the daily report schedule."""
    if runtime.night is not None:
        # ADR 0093: the night run mutes after the goodnight line, never under a spoken line.
        runtime.night.busy = lambda: voice.ducker.outputting
        # It holds the Mac past the deadline while a session it can see still works.
        runtime.night.watch = NightWatch(
            port=agents_port(),
            key=functools.partial(local_key, runtime.runtime_paths.root),
            claude=ClaudeSessions() if runtime.config.observer.claude_sessions else None,
            codex=codex_board,
        )
        watchers.append(asyncio.create_task(runtime.night.run(), name="night_run"))
    if runtime.daily_schedule is not None:
        watchers.append(
            asyncio.create_task(runtime.daily_schedule.run(), name="daily_report_schedule"),
        )


def _start_day_schedules(
    runtime: JarvisRuntime,
    watchers: list[asyncio.Task[None]],
    controls: VoiceControls,
) -> None:
    """The once-a-day writers: summaries, what he was doing, the experience table, the plan."""
    day_summary = DaySummarySettings.from_config(runtime.config.raw.get("day_summary"))
    if day_summary is not None and runtime.memory is not None:
        # ADR 0142: past days' conversation summaries, written once a day.
        llm_config = runtime.config.raw.get("llm")
        watchers.append(
            asyncio.create_task(
                DaySummarySchedule(
                    memory=runtime.memory,
                    settings=day_summary,
                    llm_config=llm_config if isinstance(llm_config, Mapping) else {},
                    event_log_path=runtime.runtime_paths.event_log,
                    pricing_table=load_pricing_table(runtime.config.pricing_path),
                    clock=runtime.clock,
                    core_memory=CoreMemorySettings.from_config(
                        runtime.config.raw.get("core_memory"),
                    ),
                    jev_log=build_jev_log(runtime.config.jev, runtime.runtime_paths.root),
                    ledger=runtime.ledger,
                ).run(),
                name="day_summary_schedule",
            ),
        )
    if runtime.doing is not None:
        # ADR 0302, 0310: what he was doing, written for the days under way and just past.
        watchers.append(asyncio.create_task(runtime.doing.run(), name="doing_schedule"))
    day_experience = DayExperienceSettings.from_config(runtime.config.raw.get("day_experience"))
    if day_experience is not None and runtime.memory is not None:
        # ADR 0280: past days' experience table, folded from the event log once a day.
        watchers.append(
            asyncio.create_task(
                DayExperienceSchedule(
                    memory=runtime.memory,
                    settings=day_experience,
                    event_log_path=runtime.runtime_paths.event_log,
                ).run(),
                name="day_experience_schedule",
            ),
        )
    day_plan = DayPlanSettings.from_config(runtime.config.raw.get("day_plan"))
    if day_plan is not None and runtime.memory is not None:
        # ADR 0288: the day's plan, drafted at `day_plan.at` and on a start with none for today.
        plan_zone = runtime.config.zone
        home = runtime.home
        plan_sources = DayPlanSources(
            event_log_path=runtime.runtime_paths.event_log,
            memory_db=runtime.memory.db_path,
            zone=plan_zone,
            day_sources=DaySources(
                plan_zone, runtime.config.observer.timesink_db_path,
                runtime.memory.db_path,
                None if runtime.terminal_hub is None
                else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S),
                None if runtime.attachments is None
                else runtime.attachments.directory,
            ),
            weather=None if home is None else home.plan_weather,
            outing_weather=None if home is None else home.outing_weather,
        )
        open_leg = (
            None if runtime.leg_planner is None else leg_session(
                runtime.leg_planner, runtime.runtime_paths.event_log, runtime.runtime_paths,
                runtime.clock,
            )
        )
        watchers.append(
            asyncio.create_task(
                build_schedule(
                    day_plan,
                    config=runtime.config.raw,
                    pricing_path=runtime.config.pricing_path,
                    sources=plan_sources,
                    route_legs=(
                        None if open_leg is None
                        else make_route_legs(open_leg, runtime.memory.db_path)
                    ),
                ).run(),
                name="day_plan_schedule",
            ),
        )
        if day_plan.watch:
            # ADR 0383: her four kinds of unprompted line, each due by the log, never followed up.
            watchers.append(
                asyncio.create_task(
                    build_watch(
                        day_plan,
                        config=runtime.config.raw,
                        pricing_path=runtime.config.pricing_path,
                        sources=plan_sources,
                        open_leg=open_leg,
                        hooks=plan_hooks(runtime, controls),
                    ).run(),
                    name="plan_watch",
                ),
            )


def _start_housekeeping(runtime: JarvisRuntime, watchers: list[asyncio.Task[None]]) -> None:
    """What tidies and watches the hardware: picture notes, the data sweep, the board."""
    if runtime.picture_notes is not None:
        runtime.picture_notes.start()  # recovers the notes still owed, then runs the queue
    watchers.append(asyncio.create_task(
        data_sweep_task(
            media_dirs(runtime), logs_dir(runtime.runtime_paths.root),
            functools.partial(link_earlier_pictures, runtime),
            functools.partial(release_uploaded_pictures, runtime),
        ),
        name="data_sweep",
    ))
    if runtime.settings is not None and runtime.config.role != "brain":
        watchers.append(asyncio.create_task(watch_board(runtime.settings), name="board_watch"))


def start_power(runtime: JarvisRuntime, running: Running) -> None:
    """Start the voice's audio-device watch and install the power observer, onto ``running``."""
    # ADR-0009 D3 — power observer, installed after the lock (which
    # stays the outermost scope) and immediately before serving, so its
    # registered CFRunLoop thread is torn down first by the same
    # ``shutdown`` that tears the watchers down; no path between install
    # and serve can leak a live registration.
    voice = running.voice
    session = voice.owners.duplex_session
    coordinator = (
        VoicePowerCoordinator(
            session=session,
            media=voice.tts,
        )
        if session is not None and voice.tts is not None
        else None
    )
    running.power_coordinator = coordinator
    if coordinator is not None and session is not None:
        device_watch = start_audio_device_watch(
            runtime.config.raw, runtime.settings, coordinator,
            session.ingress, voice.live,
        )
        device_watch.add_done_callback(log_watcher_death)
        running.watchers.append(device_watch)
    before_sleep_hook = coordinator.before_sleep if coordinator is not None else None
    on_wake_hook = coordinator.on_wake if coordinator is not None else None
    # ADR 0170: a brain has no lid or sleep of its own to report.
    running.power_observer = (
        None
        if runtime.config.role == "brain"
        else install_power_observer_or_degrade(
            runtime.conn,
            asyncio.get_running_loop(),
            before_sleep_hook=before_sleep_hook,
            on_wake_hook=on_wake_hook,
        )
    )
