"""The dependencies :func:`jarvis.surface.inherent_server.app.create_app` is built from.

The runtime builds one :class:`InherentDeps` in ``serve_inherent`` and hands it to the app. Each
route area reads the fields it serves and registers its routes only when they are wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import date
    from pathlib import Path

    from jarvis.state.attachments import AttachmentRef, Attachments
    from jarvis.state.lifelog import Lifelog
    from jarvis.state.shipments import Shipments
    from jarvis.surface.codex_sessions import CodexSession
    from jarvis.surface.device_pairing import DevicePairing
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.inherent_server.ask import AskOutcome
    from jarvis.surface.inherent_server.dictation import DictationRoutes
    from jarvis.surface.inherent_server.memory import MemoryRoutes
    from jarvis.surface.inherent_server.night import NightRoutes
    from jarvis.surface.inherent_server.setup import SetupRoutes
    from jarvis.surface.inherent_server.v2 import InherentV2Deps
    from jarvis.surface.phone_link import PhoneHub
    from jarvis.surface.terminal_events import BrainEvents
    from jarvis.surface.terminal_link import TerminalHub
    from jarvis.surface.voice_controls import VoiceControls
    from jarvis.surface.voice_live import LiveVoice


@dataclass(frozen=True)
class InherentDeps:
    """Injectable dependencies for the FastAPI app.

    Frozen so the runtime's wiring stays explicit — once
    ``serve_inherent`` builds the deps, neither the app factory nor
    the handlers mutate them.

    Attributes:
        submit_callable: Synchronous side effect of the ``/submit``
            handler. The daemon binds this to a partial of
            ``emit_surface_user_intent(runtime.conn, transcript=text,
            turn_id=<mint>)`` per spec §3.6.1. Sync (not async)
            because the underlying SQLite write is sync; the handler
            offloads the call via ``asyncio.to_thread`` so the event
            loop stays unblocked.

            ADR-0009 D2: the return value is the ``turn_id`` the
            callable minted, echoed back on the wire so a forwarding
            client can correlate the response stream (and print the id
            when it gives up waiting). ``None`` is accepted for
            bindings that do not mint one — the response then carries
            an empty ``turn_id`` and the client falls back to matching
            the response header's query text.
        broadcaster: Shared :class:`InherentBroadcaster` instance.
            The runtime's ``_response_watcher`` task pushes envelopes
            into it from the background (one cursor over the three
            ``surface.response_{open,chunk,emitted}`` types, dispatched
            by ``event.type``); the WS endpoint here registers /
            unregisters client sockets on connect / disconnect.
        cancel_response_callable: ADR-0008 D10 — bound to the runtime's
            ``make_response_cancel_callable``.
            Takes ``(response_id, scope, reason)`` and returns one of
            ``cancelled`` / ``already_terminal`` / ``unknown_response``
            / ``unsupported_scope`` / ``timeout``, or, for
            ``scope="foreground_output"``, ``applied`` / ``stale`` /
            ``uncertain`` / ``policy_ignore`` /
            ``policy_hash_mismatch`` -- where ``uncertain`` means the
            durable terminal is owed OR the playback actor did not answer
            in time, never that the audio is confirmed stopped. The
            injected-callable
            shape is what keeps ``jarvis/surface`` free of any
            ``jarvis.decision`` import. ``None`` (the default) means the
            ``/inherent/cancel-response`` route is never registered, so
            the route table and OpenAPI schema stay exactly as they are
            today.
        v2: ADR-0014 D21 — the input routes' dependencies. ``None`` (the
            default) leaves ``/inherent/submit/v2`` and ``/inherent/asr-submit/v2``
            unregistered.
    """

    submit_callable: Callable[[str], str | None]
    broadcaster: InherentBroadcaster
    # ADR 0211: the phone's files. ``attachments`` is the store behind
    # ``POST /inherent/attachments`` (``None`` leaves the route unregistered, and a submit
    # carrying ids answers 501); ``images_ok`` is whether the conversation model takes pictures.
    # ``submit_attachments(text, ids)`` starts a turn that carries the files and returns its id;
    # ``share_callable(fields)`` keeps a share that is not a turn and returns its id (``None``
    # leaves ``POST /inherent/share`` unregistered).
    attachments: Attachments | None = None
    # ADR 0255: called with each picture the store has just taken, to start its upload.
    attachment_saved: Callable[[AttachmentRef], None] | None = None
    # ADR 0238: the archive a paired phone's recordings of his day go to, at
    # ``PUT /inherent/lifelog/audio/{id}``. Needs ``device_name``; ``None`` leaves the route
    # unregistered (404).
    lifelog: Lifelog | None = None
    # ADR 0292: ``() -> coverage facts`` behind ``GET /inherent/lifelog/coverage``; a small file
    # and SQLite read, called off the loop thread. Needs ``lifelog`` and ``device_name``;
    # ``None`` leaves the route unregistered (404).
    lifelog_coverage: Callable[[], dict[str, Any]] | None = None
    # ADR 0294: where a paired terminal's scheduled copy of TimeSink and Claude Code sessions
    # goes, at ``POST /inherent/imports/{source}`` and ``PUT /inherent/imports/{source}/files/..``.
    # Needs ``device_name``; ``None`` leaves the routes unregistered (404).
    imports_intake: Shipments | None = None
    images_ok: bool = False
    submit_attachments: Callable[[str, Sequence[str]], str] | None = None
    # ADR 0212: ``submit_as_device(device, text, ids, about)`` starts the same turn with the
    # opening row written under the paired ``device``'s name, for words a paired device sent over
    # ``submit``, ``ask`` or ``share`` (``ids`` may be empty). ``None``: the Mac's own name.
    # ADR 0214: ``about`` is the checked item the phone has open, or ``None`` (``submit`` only).
    # ADR 0262: ``utterance_id`` (or ``None``) is the phone's id for the message, deduplicated
    # exactly as a ``say`` is. ADR 0401: ``via`` is ``"watch"`` when the phone asked for its watch.
    submit_as_device: (
        Callable[[str, str, Sequence[str], dict[str, Any] | None, str | None, str | None], str]
        | None
    ) = None
    share_callable: Callable[[dict[str, Any]], str] | None = None
    # ADR 0257: ``reaction_set(target, emoji)`` puts his reaction on a message record (``None``
    # takes it off), writes the line the model reads and the event, and raises ``LookupError``
    # when the target is not a message of his or hers. It blocks and runs on a worker thread;
    # ``None`` leaves ``POST /inherent/reactions`` unregistered.
    reaction_set: Callable[[str, str | None], bool] | None = None
    cancel_response_callable: Callable[[str, str, str], str] | None = None
    # ``(turn_id, reason) -> outcome``: the same route's stop for a turn still
    # being thought about; wired with ``cancel_response_callable``.
    cancel_turn_callable: Callable[[str, str], str] | None = None
    v2: InherentV2Deps | None = None
    # ADR-0015: the mute switches behind ``POST /inherent/controls``. ``None``
    # (the default) leaves the route unregistered.
    controls: VoiceControls | None = None
    # Called once when ``POST /inherent/controls`` turns conversation mode from
    # on to off: the surface's exit, which is a dismissal said without words.
    dismiss_callable: Callable[[], None] | None = None
    # GPT-Live phase A controller; ``None`` means ``live`` requests are refused.
    live: LiveVoice | None = None
    # ADR 0170: the brain's terminals. ``terminals`` holds the connected ones and
    # ``device_name`` maps a device token to the paired name it belongs to; the route
    # ``/terminal/ws`` exists only with both, i.e. only where device tokens are wired.
    # ``POST /inherent/device/events`` (ADR 0197) needs ``device_name`` and ``phone_events``,
    # the log it appends to; a Mac running alone has that without a hub (ADR 0207).
    terminals: TerminalHub | None = None
    device_name: Callable[[str], str | None] | None = None
    phone_events: BrainEvents | None = None
    # ADR 0209: the phone's conversation socket ``/phone/ws``, on every host that has
    # ``device_name``; it needs no terminal hub, so a Mac running alone has it too.
    phone: PhoneHub | None = None
    # ADR 0210: ``(device name, body) -> None`` keeps the push tokens a paired device registers at
    # ``POST /inherent/device/push``; it raises ``RegistrationError`` for a body it refuses.
    # Needs ``device_name``. ``None`` leaves the route unregistered (404).
    push_register: Callable[[str, object], None] | None = None
    # ADR 0210: ``(tool, session, request_id, waiting) -> None``, called on the loop when a Claude
    # Code permission prompt is held for him, so the host can push that it waits; ``waiting()``
    # says from any thread whether it still does (ADR 0218).
    claude_request: Callable[[str, str, str, Callable[[], bool]], None] | None = None
    # Where each Claude Code permission prompt is counted (tool, folder, offered rule; never the
    # command), so the owner can choose which kinds to allow for good. ``None`` counts nothing.
    claude_prompt_log: Path | None = None
    # ADR 0196: the four device-pairing routes. ``None`` leaves them unregistered (404).
    pairing: DevicePairing | None = None
    # ADR-0018: the quota dashboard's read model and its on-demand poll.
    # ``usage_read`` runs a small SQLite fold on the loop thread; ``usage_refresh``
    # awaits one full poll (network off-thread, emit on-thread) and returns
    # the same read model. ``None`` leaves both routes unregistered.
    usage_read: Callable[[], dict[str, Any]] | None = None
    usage_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0048: spend one Codex limit reset, ``request_id -> {code, windows_reset}``
    # (blocking network, run off the loop). Registered only with ``plugin_authorize``:
    # it spends account credit, so only the desktop's private credential may call it.
    usage_codex_reset: Callable[[str], dict[str, Any]] | None = None
    # ADR 0065: record a balance a provider will not report, ``(service, usd)``;
    # raises ``ValueError`` for a bad pair. Loop thread (it emits). Desktop
    # credential only, like the reset.
    usage_record_balance: Callable[[str, float], object] | None = None
    # The day pane's quota bar: ``(from_ms, to_ms)`` -> how Claude's and Codex's windows stood over
    # that span, a small SQLite fold on the loop thread. The route checks the span; ``None``
    # leaves ``GET /inherent/usage/history`` unregistered.
    usage_history: Callable[[int, int], dict[str, Any]] | None = None
    # ADR 0023: the current-work-state record. ``work_state_read`` is a small
    # SQLite fold on the loop thread; ``work_state_refresh`` awaits the
    # runtime's single-flight analysis (off-thread) and answers the same
    # shape plus ``outcome``. ``None`` leaves both routes unregistered.
    work_state_read: Callable[[], dict[str, Any]] | None = None
    work_state_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0037: the project view (never a model call) and the sorting job that
    # answers the same view plus ``outcome``; both run off the loop thread.
    # ``None`` leaves both routes unregistered.
    projects_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    projects_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # Spec §18.3: the conversation window reads the memory.db rows the
    # backend's own history starts from, oldest first, past a ``seq``
    # cursor or before one. ``(after, limit, before) -> {"since", "rows",
    # "has_more"}``; ``None`` leaves the route unregistered.
    conversation_read: Callable[[int, int, int], dict[str, Any]] | None = None
    # ADR 0062: the card waiting for Allen's button (``{"card": ... | None}``, a
    # small fold off the loop thread) and his answer to it, which starts a turn
    # under the device that answered and returns its id. ``None`` leaves both
    # routes unregistered. A card's ``device`` is the one whose turn asked it
    # (ADR 0218): only that device's reads are served it.
    card_read: Callable[[], dict[str, Any]] | None = None
    card_decide: Callable[[str, str, dict[str, str], str], str] | None = None
    # ADR 0066: the ask card waiting to be filled in (``{"card": ... | None}``)
    # and Allen's answers to it, which start a turn (its id) under the device
    # that answered or, dismissed, nothing (``None``). ``None`` leaves both
    # routes unregistered. Its ``device`` is served as the confirmation's is.
    question_read: Callable[[], dict[str, Any]] | None = None
    question_answer: Callable[[str, dict[str, str] | None, str], str | None] | None = None
    # ADR 0108: whether a turn that thinks is under way, which one, and the
    # words that make one. A small SQLite read on the loop thread; ``None``
    # leaves the route unregistered.
    think_read: Callable[[], dict[str, Any]] | None = None
    # ``POST /inherent/ask``: how the turn ended (its final answer or its failure), ``None`` while
    # it is still running. A small SQLite read, called off the loop thread; ``None`` leaves the
    # route unregistered.
    ask_outcome: Callable[[str], AskOutcome | None] | None = None
    # ADR 0276: ``(since_ms, limit)`` -> the newest ``limit`` per-turn timing facts at or after
    # ``since_ms``, read from the log off the loop thread; ``None`` leaves the route unregistered.
    turn_timings_read: Callable[[int, int], Awaitable[dict[str, Any]]] | None = None
    # ADR 0199: one day's timeline, folded from the log when asked: the day asked for, or ``None``
    # for today in the owner's zone, to the response document. Off the loop thread. A ValueError
    # is a 400; ``None`` leaves the route unregistered.
    day_read: Callable[[date | None], Awaitable[dict[str, Any]]] | None = None
    # ADR 0312: ``(from_ms, to_ms)`` -> every fix the phones kept in that window, per device, read
    # from the log off the loop thread. A ValueError (a window backwards or over 12 hours) is a
    # 400; ``None`` leaves ``GET /inherent/track`` unregistered.
    track_read: Callable[[int, int], Awaitable[dict[str, Any]]] | None = None
    # ADR 0323: his footprint of one day, folded from the log when asked: the day asked for, or
    # ``None`` for today in the owner's zone. Off the loop thread. A ValueError is a 400; ``None``
    # leaves ``GET /inherent/footprint`` unregistered.
    footprint_read: Callable[[date | None], Awaitable[dict[str, Any]]] | None = None
    # ADR 0296: ``(day, item id, on, label)`` -> the day's highlights after his mark, off the loop
    # thread. LookupError (no such item on that day) is 404, ``HighlightCapReached`` 409, a
    # ValueError 422; ``None`` leaves ``POST /inherent/day/highlight`` unregistered.
    highlight_set: (
        Callable[[date, str, bool, str | None], Awaitable[list[dict[str, str]]]] | None
    ) = None
    # ADR 0302: ``(day, span id, choice, text, (start, end))`` -> the span as now served, off the
    # loop thread; a span id or a stretch, not both. LookupError (no such span on that day) is
    # 404, a ValueError (a choice the span's question does not offer, a stretch outside the day or
    # touching none of the lane) 422; ``None`` leaves ``POST /inherent/day/answer`` unregistered.
    doing_answer: (
        Callable[
            [date, str | None, str | None, str | None, tuple[int, int] | None],
            Awaitable[dict[str, Any]],
        ] | None
    ) = None
    # ADR 0310: he asked to have today's timeline tidied -> what the writer did, off the loop
    # thread, after the model writes; ``None`` leaves ``POST /inherent/day/tidy`` unregistered.
    doing_tidy: Callable[[], Awaitable[str]] | None = None
    # ADR 0296, 0299: ``(thing hex, "confirm" | "cancel")`` -> whether anything changed, off the
    # loop thread. LookupError (none such, or closed) is 404 and a False is 409; ``None`` leaves
    # ``POST /inherent/commitments/{hex}`` unregistered.
    commitment_act: Callable[[str, str], Awaitable[bool]] | None = None
    # ADR 0051: the companion home's reads and its one write, all off the loop
    # thread. A LookupError is "not connected" (404, the home's fallback), any
    # other failure 502. ``None`` leaves the routes unregistered.
    today_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    todo_set: Callable[[str, bool], Awaitable[None]] | None = None
    mail_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0124: archive junk letters (ids, archive) or put them back (archive False).
    mail_archive: Callable[[list[str], bool], Awaitable[None]] | None = None
    # ADR 0147, the Dashboard's mail page: one letter whole, its read and trash taps and the
    # reply draft under the open letter. ``None`` leaves a route unregistered (404).
    mail_letter: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_summary: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_mark_read: Callable[[list[str], bool], Awaitable[None]] | None = None
    mail_trash: Callable[[list[str], bool], Awaitable[None]] | None = None
    # ADR 0176, ``dashboard.view.enabled``: what the Dashboard shows (page, tab, open item
    # ``(kind, id, title)``, rows ``(id, title, mail_id)``; page None closes). ``None`` = 404.
    view_set: (
        Callable[
            [str | None, str, tuple[str, str, str] | None, list[tuple[str, str, str]]], None,
        ] | None
    ) = None
    mail_draft_read: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_draft_save: Callable[[str, str, str], Awaitable[dict[str, Any]]] | None = None
    mail_draft_send: Callable[[str, str, str], Awaitable[None]] | None = None
    mail_draft_discard: Callable[[str], Awaitable[None]] | None = None
    # ADR 0154: the Dashboard's memory page (core memory, day summaries, search, versions).
    # ``None`` leaves every ``/inherent/memory`` route unregistered (404).
    memory_page: MemoryRoutes | None = None
    # ADR 0155: the job-mail notices the notch shows, Allen's tap on one (id, action, reaction)
    # and the job ledger with its delete. ``None`` leaves the routes unregistered (404): the
    # daemon wires them only while ``job_mail.enabled``. A LookupError is 404, a ValueError 400.
    notices_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    notice_act: Callable[[str, str, str | None], Awaitable[str | None]] | None = None
    # ADR 0203: the notch's trip cards (pin an offered option, next bus, undo an unpin). ``None``
    # leaves the route unregistered (404); a LookupError is 404.
    departure_act: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # ADR 0224: the followed bus ride. ``ride_act`` takes his answer to the boarding question (or
    # ends the ride) and ``ride_read`` is the ride now. ``None`` leaves the routes unregistered
    # (404); a LookupError from ``ride_act`` is 404.
    ride_act: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    ride_read: Callable[[], Awaitable[dict[str, Any] | None]] | None = None
    # ADR 0160: the same feedback for every other proactive card the client raises, by card id.
    # ``None`` leaves the route unregistered (404); a LookupError is 404, a ValueError 400.
    card_act: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None
    # ADR 0163: ``{hold}`` (``call``, ``away`` or None), for a client whose daemon runs no job mail.
    # ``None`` leaves the route unregistered (404): the daemon wires it while ``moment.enabled``.
    moment_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    jobs_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    job_delete: Callable[[str], Awaitable[None]] | None = None
    job_flag: Callable[[str, str], Awaitable[None]] | None = None
    # ADR 0177: the tracker's own rows: add one (fields, returns its id) and edit one by id
    # (the fields given); a ValueError is 400, an unknown id a LookupError (404).
    application_add: Callable[[dict[str, Any]], Awaitable[str]] | None = None
    application_edit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    # ADR 0186: Allen's undo of an interview's reminders and Outlook event, by application id; an
    # application with none armed is a LookupError (404).
    application_cancel_reminders: Callable[[str], Awaitable[None]] | None = None
    brief_read: Callable[[], dict[str, Any] | None] | None = None
    # ADR 0125: does a finished agent turn's ending ask Allen something? ``asks`` waits for Jev
    # (off the loop thread); ``peek`` never waits, for the terminal sessions' board. None = off.
    turn_end_asks: Callable[[str, str], Awaitable[bool | None]] | None = None
    turn_end_peek: Callable[[str, str, bool], bool | None] | None = None
    # ADR 0128: Allen's messages (epoch seconds) in a session; the first after a scored finish
    # is logged.
    turn_end_answered: Callable[[str, list[float]], None] | None = None
    # ADR 0052: the Settings page's file, read and saved off the loop thread;
    # a ValueError from saving is a 400. ``None`` leaves the routes unregistered.
    settings_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    settings_update: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # ADR 0147: the reSpeaker board's {present, firmware, direction, speech}, read off the loop.
    board_status: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # Settings > Restart: answer, then TERM this process; registered only when
    # launchd's KeepAlive is there to bring the daemon back.
    restart: Callable[[], None] | None = None
    # ADR 0067, Settings > Privacy & data: the export builds a zip and hands
    # back its temp path (deleted once sent); the clear deletes every
    # recording and screenshot now and answers how many; the erase marks
    # everything for the next boot and restarts, so it is wired only with
    # ``restart``. ``None`` leaves a route unregistered.
    data_export: Callable[[], Awaitable[Path]] | None = None
    data_clear: Callable[[], Awaitable[int]] | None = None
    data_erase: Callable[[], None] | None = None
    # ADR 0046: whether the Agents page may read Claude Code's own session
    # files (``observer.claude_sessions.enabled``); off, it shows only what
    # the user's installed hooks push.
    claude_sessions_read: bool = False
    # ADR 0069: the file Allen's marks on agent sessions live in (unread,
    # parked, archived). ``None`` leaves the marks routes unregistered.
    agent_marks_path: Path | None = None
    # ADR 0038, 0202: desktop management takes the local key or a paired device's
    # token (``manager_authorize``), unlike ordinary text submission. Secrets never
    # travel on the public websocket.
    plugin_read: Callable[[], dict[str, Any]] | None = None
    plugin_action: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    plugin_authorize: Callable[[str | None], bool] | None = None
    plugin_icon: Callable[[str], str | None] | None = None
    # The desktop settings switch: switch the fixed-text language now and keep
    # it in settings.yaml (``code -> language``, raises ValueError on anything
    # but zh / en). Registered only with ``plugin_authorize``.
    language_save: Callable[[str], str] | None = None
    # First-run setup (``jarvis.runtime.setup.Setup``): read, save_names,
    # check_key and preview block (run off the loop thread; ValueError is a
    # 400), done runs on the loop because it schedules the restart.
    setup: SetupRoutes | None = None
    # ADR 0058: dictation from the live mic, heard and polished for the text
    # caret. ``None`` (no voice stack) leaves both routes unregistered.
    dictation: DictationRoutes | None = None
    # ADR 0093: the night run the companion's cards show and its buttons
    # drive. ``None`` leaves both routes unregistered.
    night: NightRoutes | None = None
    # ADR 0019: the Codex session board the hooks fill; the runtime shares it
    # with the night run's watch.
    codex_board: dict[str, CodexSession] = field(default_factory=dict)
