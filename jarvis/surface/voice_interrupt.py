"""ADR-0006 D8/D9 two-stage barge-in: candidate window, confirm, interrupt.

Both stages are *spoken* (D9 :466).  There is no VAD candidate stage: no
capture frame, energy verdict, or Silero probability reaches this module, so
without validated AEC the speaker's own audio cannot open or confirm a
barge-in.  Stage one is a wake hit while output is active; stage two is a
configured interrupt keyword found on the partial-ASR path inside the window,
or a PTT upload, which is a deliberate button press and cannot be echo.

The router is closed: it receives :class:`BargeInSignal` values, deduplicates
per candidate window, and calls one injected ``interrupt`` callable bounded by
``confirm_timeout_ms``.  It never imports ``jarvis.decision`` or
``jarvis.runtime``, never touches actions, and never writes a durable event.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jarvis.shared.realtime_trace import TraceValue, record_realtime_trace
from jarvis.surface import voice_asr

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger("jarvis.surface.voice_interrupt")

BargeInPhase = Literal["candidate", "confirmed"]
BargeInConfirmSource = Literal["keyword", "ptt"]


@dataclass(frozen=True)
class BargeInConfig:
    """ADR-0006 D8/D9 barge-in bounds; off by default.

    The values are D8/D9 calibration candidates, not promises.
    ``confirm_timeout_ms`` is the outer bound on the injected interrupt
    callable and sits above ``realtime.response.cancel_timeout_ms``.
    """

    enabled: bool = False
    candidate_window_ms: int = 1200
    confirm_timeout_ms: int = 800
    interrupt_keywords: tuple[str, ...] = ("停", "别说了", "stop")


@dataclass(frozen=True)
class BargeInSignal:
    """One stage of a barge-in, named at the boundary that observed it."""

    phase: BargeInPhase
    session_id: str
    window_id: int
    confirm_source: BargeInConfirmSource | None = None
    keyword: str | None = None
    outcome: str | None = None


class BargeInRouter:
    """Own the candidate window, the keyword matcher, and the bounded call.

    Every entry point is reachable from a different thread — the wake loop
    opens candidates, the capture loop feeds partial revisions and ticks
    expiry, and the HTTP worker confirms a PTT upload — so the window state
    is guarded by one lock.
    """

    def __init__(
        self,
        *,
        config: BargeInConfig,
        interrupt: Callable[[str], str],
        output_active: Callable[[], bool],
        session_id: str = "",
    ) -> None:
        """Freeze the normalized keyword set before any signal can arrive."""
        self._config = config
        self._interrupt = interrupt
        self._output_active = output_active
        self._session_id = session_id
        self._keywords = tuple(
            normalized
            for keyword in config.interrupt_keywords
            if (normalized := voice_asr.normalize_partial_text(keyword))
        )
        self._lock = threading.Lock()
        self._window_id = 0
        self._window_deadline_ns: int | None = None
        self._candidates = 0
        self._candidates_dropped = 0
        self._confirmations = 0

    @property
    def candidates(self) -> int:
        """Count of opened candidate windows."""
        return self._candidates

    @property
    def candidates_dropped(self) -> int:
        """Count of candidate windows that closed with no confirm."""
        return self._candidates_dropped

    @property
    def confirmations(self) -> int:
        """Count of emitted ``phase="confirmed"`` signals."""
        return self._confirmations

    def open_candidate(self, **attributes: TraceValue) -> BargeInSignal:
        """Open a bounded window for a wake hit observed during output.

        Nothing is ducked, nothing is cancelled, and no durable event is
        written: the candidate only buys the caller the right to arm capture
        so the partial-ASR lane can look for the interrupt keyword.
        """
        with self._lock:
            self._expire_locked()
            if self._window_deadline_ns is not None:
                # F11 counts every false candidate, including one a newer wake
                # hit replaces before its own deadline.
                self._drop_locked("superseded_by_candidate")
            self._window_id += 1
            self._candidates += 1
            self._window_deadline_ns = (
                time.monotonic_ns() + self._config.candidate_window_ms * 1_000_000
            )
            window_id = self._window_id
        record_realtime_trace(
            "barge_in_candidate",
            session_id=self._session_id,
            window_id=window_id,
            candidate_window_ms=self._config.candidate_window_ms,
            **attributes,
        )
        return BargeInSignal(phase="candidate", session_id=self._session_id, window_id=window_id)

    def offer_partial(
        self,
        normalized_text: str,
        *,
        utterance_id: str = "",
    ) -> BargeInSignal | None:
        """Confirm on an interrupt keyword inside an open candidate window.

        ``normalized_text`` is the value the partial path already computed
        with :func:`voice_asr.normalize_partial_text`; the configured
        keywords went through the same normalizer at construction, so the
        two sides are compared in one code-point form.
        """
        keyword = next((word for word in self._keywords if word in normalized_text), None)
        if keyword is None:
            return None
        with self._lock:
            self._expire_locked()
            if self._window_deadline_ns is None:
                return None
            window_id = self._window_id
            self._window_deadline_ns = None
            self._confirmations += 1
        return self._confirm(
            window_id,
            "keyword",
            keyword=keyword,
            utterance_id=utterance_id,
        )

    def confirm_ptt(self) -> str:
        """Confirm directly from a PTT upload, in both D9 barge modes.

        A button press is not echo, so no prior candidate is required; an
        upload that arrives while nothing is speaking is not a barge-in.
        """
        try:
            if not self._output_active():
                return "output_idle"
        except Exception:  # noqa: BLE001 - unknown output state cannot confirm
            LOGGER.warning("output activity query failed; PTT barge-in ignored", exc_info=True)
            return "output_unknown"
        with self._lock:
            self._expire_locked()
            if self._window_deadline_ns is None:
                self._window_id += 1
            self._window_deadline_ns = None
            window_id = self._window_id
            self._confirmations += 1
        return self._confirm(window_id, "ptt").outcome or ""

    def expire_due(self) -> None:
        """Drop a window that reached ``candidate_window_ms`` with no keyword."""
        with self._lock:
            self._expire_locked()

    def _expire_locked(self) -> None:
        deadline = self._window_deadline_ns
        if deadline is None or time.monotonic_ns() < deadline:
            return
        self._drop_locked("candidate_window_elapsed")

    def _drop_locked(self, reason: str) -> None:
        self._window_deadline_ns = None
        self._candidates_dropped += 1
        record_realtime_trace(
            "barge_in_candidate_dropped",
            session_id=self._session_id,
            window_id=self._window_id,
            reason=reason,
            candidate_window_ms=self._config.candidate_window_ms,
        )

    def _confirm(
        self,
        window_id: int,
        source: BargeInConfirmSource,
        *,
        keyword: str | None = None,
        utterance_id: str = "",
    ) -> BargeInSignal:
        outcome = self._bounded_interrupt(source)
        record_realtime_trace(
            "barge_in_confirmed",
            session_id=self._session_id,
            window_id=window_id,
            confirm_source=source,
            keyword=keyword or "",
            utterance_id=utterance_id,
            outcome=outcome,
        )
        return BargeInSignal(
            phase="confirmed",
            session_id=self._session_id,
            window_id=window_id,
            confirm_source=source,
            keyword=keyword,
            outcome=outcome,
        )

    def _bounded_interrupt(self, source: BargeInConfirmSource) -> str:
        # ponytail: one daemon thread per confirmed barge-in. Confirms are
        # deduplicated per candidate window so they are rare; a pool is the
        # upgrade path only if a confirm rate ever makes thread churn visible.
        result: list[str] = []

        def _run() -> None:
            try:
                result.append(self._interrupt(source))
            except Exception:  # a failed interrupt cannot kill the voice session
                LOGGER.exception("barge-in interrupt callable raised")
                result.append("interrupt_failed")

        thread = threading.Thread(target=_run, name="jarvis-barge-in-interrupt", daemon=True)
        thread.start()
        thread.join(self._config.confirm_timeout_ms / 1000)
        return result[0] if result else "timeout"


def barge_in_config_from_mapping(raw: object) -> BargeInConfig:
    """Parse the ADR-0006 D8/D9 ``barge_in`` block; absent means off.

    Unknown keys are ignored: ``device-profile-resolver`` parses
    ``detection_mode`` and ``accepted_natural_profiles`` out of the same
    YAML block through a different parser.
    """
    if raw is None:
        return BargeInConfig()
    if not isinstance(raw, Mapping):
        msg = "realtime.single_audio_ingress.barge_in must be a mapping"
        raise ValueError(msg)  # noqa: TRY004 - runtime downgrades on ValueError
    defaults = BargeInConfig()
    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        msg = "realtime.single_audio_ingress.barge_in.enabled must be a boolean"
        raise ValueError(msg)  # noqa: TRY004 - runtime downgrades on ValueError

    def _positive_int(key: str, fallback: int) -> int:
        value = raw.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.single_audio_ingress.barge_in.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    keywords = raw.get("interrupt_keywords", defaults.interrupt_keywords)
    if not isinstance(keywords, Sequence) or isinstance(keywords, str | bytes):
        msg = "realtime.single_audio_ingress.barge_in.interrupt_keywords must be a list"
        raise ValueError(msg)  # noqa: TRY004 - runtime downgrades on ValueError
    if not all(isinstance(word, str) for word in keywords):
        msg = "realtime.single_audio_ingress.barge_in.interrupt_keywords must be strings"
        raise ValueError(msg)
    return BargeInConfig(
        enabled=enabled,
        candidate_window_ms=_positive_int("candidate_window_ms", defaults.candidate_window_ms),
        confirm_timeout_ms=_positive_int("confirm_timeout_ms", defaults.confirm_timeout_ms),
        interrupt_keywords=tuple(str(word) for word in keywords),
    )


__all__ = [
    "BargeInConfig",
    "BargeInConfirmSource",
    "BargeInPhase",
    "BargeInRouter",
    "BargeInSignal",
    "barge_in_config_from_mapping",
]
