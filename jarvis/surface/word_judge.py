"""The judge of words said over her voice or inside a conversation (ADR 0053, 0102, 0130, 0216).

One judge for every ear: the Mac's :class:`~jarvis.surface.voice_session.DuplexVoiceSession` and a
paired phone's ``say`` (:mod:`jarvis.surface.phone_link`) both ask :func:`words_verdict`. The
verdict is one of ``turn``, ``backchannel``, ``unclear``, ``echo``, ``stop``, ``wait``,
``dismissed`` or ``quiet:<level>``; what to do about it belongs to the caller.

The regexes (:mod:`jarvis.surface.voice_asr`) judge first. Where the hooks carry ``ask`` (Jev,
ADR 0130), a line they call a turn, said over her or in conversation mode, is put to it.

Layer rules: stdlib and ``jarvis.surface`` only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.surface import voice_asr

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger("jarvis.surface.word_judge")


@dataclass(frozen=True)
class WordHooks:
    """What the judge reaches in the host; every hook is optional.

    ``ask(turn_id, text, recent, over_her, confirm)`` returns Jev's choice, or ``None`` for a
    turn, and blocks up to its own timeout. ``note(turn_id, verdict, text, over_her,
    conversation)`` records a line the regexes settled alone. ``begin(turn_id, text, recent,
    over_her, conversation)`` is called for every line the regexes call a turn and sends its
    one Jev request without waiting. ``recent()`` is what she said lately. ``quiet`` is whether
    the host can set a quiet level, which is what makes a quiet command recognizable (ADR 0153).
    """

    ask: Callable[[str, str, str, bool, bool], str | None] | None = None
    note: Callable[[str, str, str, bool, bool], None] | None = None
    begin: Callable[[str, str, str, bool, bool], None] | None = None
    recent: Callable[[], str] | None = None
    quiet: bool = False


def words_verdict(
    hooks: WordHooks, turn_id: str, text: str, *, conversation: bool, over_her: bool,
) -> str:
    """dismissed, wait, backchannel, unclear, stop, echo or turn; Jev settles what is left."""
    ask = hooks.ask
    # With Jev, a dismissal found only inside a sentence must be confirmed by it.
    loose = (
        ask is not None and conversation
        and voice_asr.is_dismissal(text) and not voice_asr.is_whole_dismissal(text)
    )
    verdict = regex_words(
        hooks, text, conversation=conversation, over_her=over_her, whole_only=ask is not None,
    )
    if verdict == "turn" and hooks.begin is not None:
        try:
            hooks.begin(turn_id, text, recent_speech(hooks), over_her, conversation)
        except Exception:  # noqa: BLE001 - Jev cannot break capture; the line stays a turn
            LOGGER.warning("begin_line failed turn_id=%s", turn_id, exc_info=True)
    if ask is None:
        return verdict
    if verdict != "turn":
        if hooks.note is not None:
            hooks.note(turn_id, verdict, text, over_her, conversation)
        return verdict
    if not (over_her or conversation):
        return verdict
    try:
        choice = ask(turn_id, text, recent_speech(hooks), over_her, loose)
    except Exception:  # noqa: BLE001 - Jev cannot break capture; the line stays a turn
        LOGGER.warning("ask_words failed turn_id=%s", turn_id, exc_info=True)
        return verdict
    return jev_verdict(choice, conversation=conversation, over_her=over_her)


def regex_words(
    hooks: WordHooks, text: str, *, conversation: bool, over_her: bool, whole_only: bool,
) -> str:
    """What the regexes make of ``text``; ``whole_only`` leaves out the loose dismissal."""
    checks: list[tuple[str, Callable[[str], bool]]] = []
    # ADR 0153: said in any state, ahead of every other verdict.
    if hooks.quiet and (level := voice_asr.quiet_command(text)) is not None:
        return f"quiet:{level}"
    if conversation:
        dismissal = voice_asr.is_whole_dismissal if whole_only else voice_asr.is_dismissal
        checks += [("dismissed", dismissal), ("wait", voice_asr.is_wait_request)]
        if not over_her:
            checks += [
                ("backchannel", voice_asr.is_backchannel),
                ("unclear", voice_asr.is_unclear_sound),
            ]
    if over_her:
        checks += [
            ("backchannel", voice_asr.is_backchannel),
            ("stop", voice_asr.is_stop_request),
            ("unclear", voice_asr.is_unclear_sound),
        ]
    for verdict, test in checks:
        if test(text):
            return verdict
    return "echo" if over_her and _is_own_echo(hooks, text) else "turn"


def jev_verdict(choice: str | None, *, conversation: bool, over_her: bool) -> str:
    """Jev's choice as the verdict the regex path would have given (ADR 0130).

    Dismiss and wait only mean something in conversation mode; over her outside it they
    stop her, and a stop with her silent has nothing to stop. Everything else is a turn.
    """
    if choice == "keep_going":
        return "backchannel"
    if choice == "dismiss" and conversation:
        return "dismissed"
    if choice == "wait" and conversation:
        return "wait"
    if choice in {"stop", "wait", "dismiss"} and over_her:
        return "stop"
    return "turn"


def recent_speech(hooks: WordHooks) -> str:
    """What she said lately, for Jev's context; none when it cannot be read."""
    if hooks.recent is None:
        return ""
    try:
        return hooks.recent()
    except Exception:  # noqa: BLE001 - her recent words cannot break capture
        LOGGER.debug("recent_speech failed", exc_info=True)
        return ""


def _is_own_echo(hooks: WordHooks, text: str) -> bool:
    """Whether ``text`` copies what she said lately; no answer means no."""
    if hooks.recent is None:
        return False
    try:
        return voice_asr.is_own_echo(text, hooks.recent())
    except Exception:  # noqa: BLE001 - her recent words cannot break capture
        LOGGER.debug("recent_speech failed", exc_info=True)
        return False
