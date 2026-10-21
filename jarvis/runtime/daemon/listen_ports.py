"""The ports a capture session calls to reach the turn when the daemon holds the turn itself."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Final

from jarvis.runtime import device_voice
from jarvis.runtime.daemon.conversation_lines import say_conversation_line
from jarvis.runtime.daemon.output_watchers import TTS_SILENT_CHANNELS
from jarvis.runtime.daemon.voice_wiring import ListenPorts
from jarvis.runtime.turn.cancel import (
    make_barge_in_interrupt_callable,
    make_relation_supersede_callable,
    make_response_cancel_callable,
    make_supersede_unspoken_callable,
)
from jarvis.state.event_log import MAC_NODE, open_runtime_event_log
from jarvis.state.turn_facts import answer_started, input_channels, spoken_since
from jarvis.state.turn_overlap import any_turn_in_flight

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.runtime import JarvisRuntime
    from jarvis.surface import voice_media

LOGGER = logging.getLogger(__name__)

_TURN_WORKING_WINDOW_MS: Final[int] = 5 * 60 * 1000
_RECENT_SPEECH_WINDOW_MS: Final[int] = 60 * 1000
"""A turn of his words still open this long after it began is the supervisor's, not
conversation mode's to wait for (the window ADR 0107 gives a turn in flight)."""


def make_cancel_voice_runs(
    runtime: JarvisRuntime, *, only_turns: Callable[[str], bool] | None = None,
) -> Callable[[], None]:
    """ADR 0138: the ``() -> None`` that ends every answer still being written for the speaker.

    An exit from the surface must leave nothing that can speak after it,
    whatever its age or number. Every open run of a turn that came from Allen's
    words, that is not for a document alone, and whose policy lets its generation
    be cancelled, is cancelled as ``user_stop``; its turn is marked stopped so a run
    of it opening later is cancelled at its open, and the media owner drops what
    it had of a cancelled run. A turn submitted on a channel the speaker is silent
    for (``TTS_SILENT_CHANNELS``) and background turns are left alone.

    A turn he spoke whose answer has not started (ADR 0269) is not cancelled: it answers in
    text only, and only a wait line or an answer already under way is ended.

    ``only_turns`` (ADR 0219) narrows it to the turns it accepts: a device's exit ends that
    device's answers, not another's.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _from_his_words(conn: sqlite3.Connection, turn_id: str) -> bool:
        channels = input_channels(conn, turn_id)
        return bool(channels) and not any(channel in TTS_SILENT_CHANNELS for channel in channels)

    def _cancel_voice_runs() -> None:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return
        runs = [
            run
            for run in registry.open_runs()
            if run.channel != "document" and run.interrupt_policy.generation_action == "cancel"
        ]
        if not runs:
            return
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            runs = [run for run in runs if _from_his_words(conn, run.turn_id)]
            if only_turns is not None:
                runs = [run for run in runs if only_turns(run.turn_id)]
            unanswered = {
                run.turn_id
                for run in runs
                if run.phase == "final"
                and not answer_started(conn, run.turn_id)
            }
        for turn_id in unanswered:
            registry.mark_turn_text_only(turn_id)
        runs = [run for run in runs if run.turn_id not in unanswered or run.phase != "final"]
        for run in runs:
            if run.turn_id not in unanswered:
                registry.mark_turn_stopped(run.turn_id)
        for run in runs:
            outcome = cancel(run.response_id, "generation", "user_stop")
            LOGGER.info(
                "exit: run %s of turn %s cancelled -> %s", run.response_id, run.turn_id, outcome,
            )

    return _cancel_voice_runs


def local_listen_ports(
    runtime: JarvisRuntime,
    streaming: voice_media.StreamingTTSPipeline | None,
) -> ListenPorts:
    """The ports of a daemon that holds the turn itself: direct calls into ``runtime``.

    Its voice acts on the host's own turns only (ADR 0219); ``hold_runs`` stays global.
    """
    oneshot = runtime.oneshot
    if oneshot is not None and streaming is not None and runtime.response_runs is not None:
        # ADR 0139: a supplement or correction of the line before it acts on that turn.
        oneshot.relate = make_relation_supersede_callable(
            runtime,
            streaming.drop_unspoken,
            lambda: streaming.stop_foreground_output(None, reason="correction"),
        )
    registry = runtime.response_runs
    # ADR 0219: the Mac's mic acts on the host's own turns; a paired phone's are never touched.
    host_turns = device_voice.host_turns(runtime.runtime_paths.event_log)

    def _hold_runs(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        # ADR 0053: while Allen's words are coming in, no run completes.
        if registry is not None:
            registry.hold_completion(held=held)

    def _turn_working() -> bool:
        # ADR 0102: conversation mode's quiet clock does not run while a turn works.
        # Runs on the session's capture worker, never the audio callback; the
        # deadline bounds the open so a locked log cannot stall capture.
        try:
            conn = open_runtime_event_log(
                runtime.runtime_paths.event_log, deadline=time.monotonic() + 0.25,
            )
        except sqlite3.Error:
            return False
        try:
            return any_turn_in_flight(
                conn, since_ms=int(time.time() * 1000) - _TURN_WORKING_WINDOW_MS,
            )
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _recent_speech() -> str:
        # What she said in the last minute, to tell her own voice in the mic
        # from Allen's words over her. Same worker and open as _turn_working.
        conn = open_runtime_event_log(
            runtime.runtime_paths.event_log, deadline=time.monotonic() + 0.25,
        )
        try:
            return " ".join(
                spoken_since(conn, since_ms=int(time.time() * 1000) - _RECENT_SPEECH_WINDOW_MS),
            )
        finally:
            conn.close()

    return ListenPorts(
        answer_words=lambda turn_id, reason, text: threading.Thread(
            target=say_conversation_line,
            args=(runtime, turn_id, reason, text),
            name="conversation-line",
            daemon=True,
        ).start(),
        turn_working=_turn_working,
        recent_speech=_recent_speech,
        ask_words=(
            oneshot.ask
            if oneshot is not None and oneshot.words_enabled
            else runtime.voice_words.ask if runtime.voice_words is not None else None
        ),
        note_words=runtime.voice_words.note if runtime.voice_words is not None else None,
        begin_line=oneshot.begin if oneshot is not None else None,
        interrupt=make_barge_in_interrupt_callable(runtime, only_turns=host_turns),
        hold_runs=_hold_runs,
        warm=lambda: runtime.llm_client.warm_stream(),  # noqa: PLW0108 — looked up per call.
        supersede_unspoken=(
            make_supersede_unspoken_callable(
                runtime, streaming.drop_unspoken, only_turns=host_turns,
            )
            if streaming is not None and registry is not None
            else None
        ),
        cancel_voice_runs=(
            make_cancel_voice_runs(runtime, only_turns=host_turns)
            if registry is not None
            else None
        ),
        heard_elsewhere=lambda _turn_id, text: device_voice.heard_elsewhere(
            runtime.runtime_paths.event_log, MAC_NODE, text,
        ),
        voiceprints=runtime.voiceprints,
        cues=runtime.voice_cues,
        ambient=runtime.ambient,
    )
