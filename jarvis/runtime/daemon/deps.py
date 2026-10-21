"""What the Inherent routes read: the daemon's side of ``InherentDeps``, and the Setup page's."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

from jarvis.deployment import inherent_v2_token_matches, models
from jarvis.deployment.launchd import supervised
from jarvis.runtime.card_feedback import CardFeedback
from jarvis.runtime.core_memory import section_caps
from jarvis.runtime.daemon import submissions
from jarvis.runtime.daemon.cards import answer_question, decide_card, read_card, read_question
from jarvis.runtime.daemon.live_backend import ask_outcome
from jarvis.runtime.daemon.observers import claude_sessions_served, refresh_usage_now
from jarvis.runtime.daemon.restart import restart_soon
from jarvis.runtime.daemon.retention import clear_media, erase_data, export_data, start_upload
from jarvis.runtime.daemon.routes import (
    act_commitment,
    alert_deps,
    answer_doing,
    archive_mail,
    card_act,
    day_sources,
    draft_deps,
    lifelog_coverage,
    mail_letter,
    mail_summary,
    mail_trash,
    mark_mail_read,
    model_takes_images,
    moment_hold,
    notice_deps,
    open_lifelog,
    read_conversation,
    refresh_work_state_now,
    ride_act,
    save_settings,
    serve_day,
    serve_footprint,
    serve_today,
    serve_track,
    serve_turn_timings,
    set_highlight,
    set_todo,
    settings_notice_deps,
    shipments,
    turn_end_asks,
)
from jarvis.runtime.doing_answers import DoingAnswers
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.runtime.memory_page import MemoryPage
from jarvis.runtime.setup import Setup, save_language
from jarvis.runtime.turn.cancel import make_response_cancel_callable, make_turn_cancel_callable
from jarvis.state.device_tokens import PairingCodes, device_name_for_token, device_token_matches
from jarvis.state.imports import IMPORTS_DIRNAME
from jarvis.state.ledger import TURN_WAIT_S, TerminalScreen
from jarvis.state.push_tokens import register_body
from jarvis.surface import respeaker_board
from jarvis.surface.device_pairing import DevicePairing, brain_urls
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import manager_authorize
from jarvis.surface.inherent_server.v2 import InherentV2Deps
from jarvis.surface.usage_observer import (
    UsageObserver,
    latest_usage,
    redeem_codex_reset,
    usage_history,
)

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.daemon.voice_boot import Voice
    from jarvis.runtime.daemon.voice_wiring import VoiceKnobs
    from jarvis.runtime.spend_cap import SpendCapSettings
    from jarvis.surface.codex_sessions import CodexSession
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.phone_link import PhoneHub


def build_setup(
    runtime: JarvisRuntime, knobs: VoiceKnobs, model_fetch: models.Progress,
) -> Setup | None:
    """What ``GET /inherent/setup`` reads, or ``None`` without a memory database."""
    window_memory = runtime.memory
    if window_memory is None:
        return None
    return Setup(
        root=runtime.runtime_paths.root,
        settings_path=runtime.runtime_paths.settings,
        memory_path=window_memory.db_path,
        config=runtime.config.raw,
        tts_endpoint=knobs.tts_primary_endpoint,
        tts_model=knobs.tts_model,
        restart=restart_soon if supervised() else None,
        voice_models=model_fetch,
    )


def build_deps(  # noqa: PLR0913 - one declarative table: each route's reads, bound to what boot built
    runtime: JarvisRuntime,
    *,
    broadcaster: InherentBroadcaster,
    voice: Voice,
    setup: Setup | None,
    usage_observer: UsageObserver | None,
    codex_board: dict[str, CodexSession],
    phone_hub: PhoneHub | None,
    spend: SpendCapSettings | None,
    v2_token: str,
    port: int,
) -> InherentDeps:
    """Bind every route of the Inherent app to the stores and the voice this daemon built."""
    controls = voice.controls
    window_memory = runtime.memory
    event_log_path = runtime.runtime_paths.event_log
    submit_callable = functools.partial(submissions.submit_text, event_log_path)
    images_ok = model_takes_images(runtime.config.raw)
    submit_attachments = functools.partial(
        submissions.submit_with_attachments, event_log_path,
    )
    submit_as_device = functools.partial(submissions.submit_as_device, event_log_path)
    share_callable = functools.partial(
        submissions.keep_share, event_log_path, picture_notes=runtime.picture_notes,
    )
    cancel_response_callable = (
        make_response_cancel_callable(
            runtime,
            # ADR-0006 D8: only a live playback actor can stop the tail
            # of a run the registry has already released.
            stop_foreground_output=(
                voice.tts.stop_foreground_output
                if voice.tts is not None
                else None
            ),
        )
        if runtime.response_runs is not None
        else None
    )
    # ADR 0147: the Dashboard's mail page exists only with ``dashboard.mail.enabled``.
    mail_home = None if runtime.mail_drafts is None else runtime.home
    local_zone = runtime.config.zone
    return InherentDeps(
        submit_callable=submit_callable,
        attachments=runtime.attachments,
        attachment_saved=functools.partial(start_upload, runtime),
        images_ok=images_ok,
        submit_attachments=submit_attachments,
        submit_as_device=submit_as_device,
        share_callable=share_callable,
        reaction_set=(
            None if runtime.memory is None
            else functools.partial(
                submissions.set_reaction, event_log_path, runtime.memory.db_path,
            )
        ),
        broadcaster=broadcaster,
        usage_read=(
            None if usage_observer is None else functools.partial(latest_usage, runtime.conn)
        ),
        usage_refresh=(
            None
            if usage_observer is None
            else functools.partial(refresh_usage_now, usage_observer, runtime.conn)
        ),
        usage_codex_reset=None if usage_observer is None else redeem_codex_reset,
        usage_record_balance=None if usage_observer is None else usage_observer.record_balance,
        usage_history=(
            None if usage_observer is None else functools.partial(usage_history, runtime.conn)
        ),
        work_state_read=(
            None
            if runtime.work_state is None
            else functools.partial(runtime.work_state.read, runtime.conn)
        ),
        work_state_refresh=(
            None
            if runtime.work_state is None
            else functools.partial(refresh_work_state_now, runtime.work_state)
        ),
        projects_read=(
            None
            if runtime.projects is None
            else functools.partial(asyncio.to_thread, runtime.projects.read)
        ),
        projects_refresh=(
            None
            if runtime.projects is None
            else functools.partial(asyncio.to_thread, runtime.projects.refresh)
        ),
        conversation_read=(
            None if window_memory is None else functools.partial(read_conversation, runtime)
        ),
        ask_outcome=functools.partial(ask_outcome, runtime.runtime_paths.event_log),
        turn_timings_read=functools.partial(
            serve_turn_timings, runtime.runtime_paths.event_log,
        ),
        track_read=functools.partial(serve_track, runtime.runtime_paths.event_log),
        footprint_read=functools.partial(
            serve_footprint,
            runtime.runtime_paths.event_log,
            runtime.config.zone,
            None if runtime.memory is None else runtime.memory.db_path,
        ),
        card_read=functools.partial(read_card, runtime),
        card_decide=functools.partial(decide_card, runtime),
        question_read=functools.partial(read_question, runtime),
        question_answer=functools.partial(answer_question, runtime),
        think_read=(
            None if runtime.think_mode is None
            else functools.partial(runtime.think_mode.status, runtime.conn)
        ),
        day_read=functools.partial(
            serve_day,
            runtime.runtime_paths.event_log,
            runtime.config.zone,
            None if runtime.memory is None else runtime.memory.db_path,
            runtime.config.observer.timesink_db_path,
            terminal=(
                None if runtime.terminal_hub is None
                else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S)
            ),
            attachments=None if runtime.attachments is None else runtime.attachments.directory,
            shipped=runtime.runtime_paths.root / IMPORTS_DIRNAME,
            weather=None if runtime.home is None else runtime.home.day_weather_card,
        ),
        highlight_set=(
            None if runtime.memory is None
            else functools.partial(
                set_highlight,
                HighlightMarks(
                    runtime.runtime_paths.event_log,
                    day_sources(runtime, runtime.memory.db_path),
                ),
            )
        ),
        doing_answer=(
            None if runtime.memory is None
            else functools.partial(
                answer_doing,
                DoingAnswers(
                    runtime.runtime_paths.event_log,
                    day_sources(runtime, runtime.memory.db_path),
                ),
            )
        ),
        doing_tidy=(
            None if runtime.doing is None
            else functools.partial(asyncio.to_thread, runtime.doing.tidy)
        ),
        commitment_act=functools.partial(
            act_commitment, runtime.runtime_paths.event_log, local_zone,
        ),
        today_read=(
            None if runtime.home is None
            else functools.partial(
                serve_today, runtime.runtime_paths.event_log, local_zone,
                runtime.home.weather,
            )
        ),
        todo_set=(
            None if runtime.home is None
            else functools.partial(
                set_todo, runtime.home, runtime.things_mirror,
                runtime.runtime_paths.event_log, local_zone,
            )
        ),
        mail_read=(
            None if runtime.home is None
            else functools.partial(asyncio.to_thread, runtime.home.mail)
        ),
        mail_archive=(
            None if runtime.home is None else functools.partial(archive_mail, runtime.home)
        ),
        mail_letter=None if mail_home is None else functools.partial(mail_letter, mail_home),
        mail_summary=(
            None if mail_home is None else functools.partial(mail_summary, mail_home)
        ),
        mail_mark_read=None if mail_home is None else functools.partial(mark_mail_read, mail_home),
        mail_trash=None if mail_home is None else functools.partial(mail_trash, mail_home),
        view_set=None if runtime.view is None else runtime.view.set,
        **draft_deps(runtime, mail_home),
        **settings_notice_deps(
            notice_deps(
                runtime.job_mail, runtime.reminders, runtime.moment,
                alerts=(
                    alert_deps(
                        runtime.memory.db_path, lambda: controls.quiet, runtime.moment,
                        runtime.sound_output,
                    )
                    if spend is not None and runtime.memory is not None else None
                ),
            ),
            runtime.settings_drift,
            lambda: not controls.conversation and not voice.ducker.outputting,
        ),
        moment_read=(
            None if runtime.moment is None else functools.partial(moment_hold, runtime.moment)
        ),
        card_act=(
            None if window_memory is None
            else functools.partial(
                card_act,
                CardFeedback(window_memory.db_path, lambda: controls.quiet, runtime.moment),
            )
        ),
        memory_page=(
            None if window_memory is None
            else MemoryPage(
                window_memory.db_path,
                runtime.settings,
                section_caps=section_caps(runtime.config.raw.get("core_memory")),
                history_since=runtime.config.session.history_since,
                event_log=runtime.runtime_paths.event_log,
                zone=runtime.config.zone,
            )
        ),
        brief_read=(
            None if runtime.home is None
            else functools.partial(runtime.home.brief, runtime.conn)
        ),
        turn_end_asks=(
            None if runtime.turn_end_asks is None
            else functools.partial(turn_end_asks, runtime.turn_end_asks)
        ),
        turn_end_peek=None if runtime.turn_end_asks is None else runtime.turn_end_asks.peek,
        turn_end_answered=(
            None if runtime.turn_end_asks is None else runtime.turn_end_asks.answered
        ),
        settings_read=(
            None if runtime.settings is None
            else functools.partial(asyncio.to_thread, runtime.settings.read)
        ),
        settings_update=(
            None if runtime.settings is None
            else functools.partial(save_settings, runtime.settings)
        ),
        board_status=(
            None if runtime.settings is None or runtime.config.role == "brain"
            else functools.partial(asyncio.to_thread, respeaker_board.status)
        ),
        restart=restart_soon if supervised() else None,
        data_export=functools.partial(asyncio.to_thread, export_data, runtime),
        data_clear=functools.partial(asyncio.to_thread, clear_media, runtime),
        data_erase=(
            functools.partial(erase_data, runtime)
            if supervised() else None
        ),
        claude_sessions_read=claude_sessions_served(runtime),
        agent_marks_path=runtime.runtime_paths.root / "agent-marks.json",
        plugin_read=runtime.plugin_connections.read if runtime.plugin_connections else None,
        plugin_action=runtime.plugin_connections.action if runtime.plugin_connections else None,
        plugin_authorize=(
            manager_authorize(
                runtime.plugin_connections.settings.matches,
                functools.partial(device_token_matches, runtime.runtime_paths.root)
                if runtime.config.listen_addresses
                else None,
            )
            if runtime.plugin_connections
            else None
        ),
        plugin_icon=runtime.plugin_connections.icon if runtime.plugin_connections else None,
        language_save=functools.partial(save_language, runtime.runtime_paths.settings),
        setup=setup,
        dictation=voice.dictation,
        night=runtime.night,
        codex_board=codex_board,
        cancel_response_callable=cancel_response_callable,
        cancel_turn_callable=(
            make_turn_cancel_callable(runtime) if cancel_response_callable is not None else None
        ),
        controls=controls,
        dismiss_callable=(
            None if voice.owners.duplex_session is None else voice.owners.duplex_session.dismiss
        ),
        live=voice.live,
        terminals=runtime.terminal_hub if runtime.config.listen_addresses else None,
        phone_events=runtime.phone_events if runtime.config.listen_addresses else None,
        lifelog=(
            lifelog_archive := open_lifelog(runtime) if runtime.config.listen_addresses else None
        ),
        lifelog_coverage=(
            None if lifelog_archive is None else lifelog_coverage(runtime, lifelog_archive)
        ),
        imports_intake=shipments(runtime) if runtime.config.listen_addresses else None,
        phone=phone_hub,
        push_register=(
            functools.partial(register_body, runtime.runtime_paths.root)
            if runtime.config.listen_addresses
            else None
        ),
        ride_act=None if runtime.rides is None else functools.partial(ride_act, runtime.rides),
        ride_read=None if runtime.rides is None else functools.partial(
            asyncio.to_thread, runtime.rides.view,
        ),
        claude_request=None if runtime.push is None else runtime.push.claude_request,
        claude_prompt_log=runtime.runtime_paths.root / "claude-prompts.jsonl",
        device_name=(
            functools.partial(device_name_for_token, runtime.runtime_paths.root)
            if runtime.config.listen_addresses
            else None
        ),
        # ADR 0196: routes on every daemon, but only a brain that listens can mint a code.
        pairing=DevicePairing(
            root=runtime.runtime_paths.root,
            codes=PairingCodes(runtime.runtime_paths.root),
            listens=bool(runtime.config.listen_addresses),
            brain_urls=brain_urls(
                runtime.config.listen_addresses, runtime.config.listen_hosts, port,
            ),
        ),
        v2=InherentV2Deps(
            token_matches=functools.partial(inherent_v2_token_matches, v2_token),
            device_token_matches=(
                functools.partial(device_token_matches, runtime.runtime_paths.root)
                if runtime.config.listen_addresses
                else None
            ),
            submit_text=functools.partial(
                submissions.submit_text_v2,
                runtime.runtime_paths.event_log,
            ),
            submit_asr=(
                None
                if voice.pipeline_callable is None
                else functools.partial(
                    submissions.submit_asr_v2,
                    runtime.runtime_paths.event_log,
                    voice.pipeline_callable,
                )
            ),
        ),
    )
