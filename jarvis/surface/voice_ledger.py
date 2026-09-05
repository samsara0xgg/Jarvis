"""Conservative L5 output-timeline accounting for realtime speech.

The ledger deliberately keeps three different cursors:

* accepted samples entered the generation-valid software timeline;
* submitted samples crossed the PortAudio callback boundary;
* estimated-audible samples crossed a conservative presentation horizon.

None of these values is a physical loopback measurement.  A caller may label
the last cursor ``measured_dac`` only when a backend supplies a real measured
DAC/loopback mapping; the sounddevice backend used by Wave 2 reports
``estimated`` or ``unknown``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

CursorQuality = Literal["measured_dac", "estimated", "unknown"]
AudibilityClass = Literal["normal", "attenuated", "muted", "unknown"]


@dataclass(frozen=True)
class GenerationLease:
    """One L5-minted write lease for a playable response."""

    session_id: str
    response_id: str
    response_group_id: str
    turn_id: str
    playback_generation_id: int
    timeline_epoch: int


@dataclass(frozen=True)
class StalePlaybackGeneration:
    """A typed no-op returned for an old playback generation."""

    expected_playback_generation_id: int
    active_playback_generation_id: int | None
    reason: str = "stale_generation"


@dataclass(frozen=True)
class ForegroundBusy:
    """Activation was rejected because another foreground lease is live."""

    active_lease: GenerationLease


@dataclass(frozen=True)
class AcceptedSamples:
    """One generation-valid player write and its output span."""

    playback_generation_id: int
    segment_sequence: int
    output_start_cursor: int
    output_end_cursor: int

    @property
    def sample_count(self) -> int:
        """Return the accepted sample count."""
        return self.output_end_cursor - self.output_start_cursor


@dataclass
class SpeechChunk:
    """One semantic speech segment mapped onto a generation timeline."""

    response_id: str
    playback_generation_id: int
    segment_sequence: int
    text: str
    text_start: int
    text_end: int
    timeline_epoch: int
    output_start_cursor: int
    output_end_cursor: int | None
    sample_rate: int
    cursor_quality: CursorQuality
    audibility_class: AudibilityClass
    segment_hash: str
    closed: bool = False
    cursor_quality_observed: bool = False


@dataclass(frozen=True)
class OutputTimelineSnapshot:
    """Immutable conservative state for one playback generation."""

    lease: GenerationLease
    accepted_samples: int
    submitted_samples: int
    estimated_audible_samples: int
    heard_through_sequence: int | None
    heard_text: str
    cursor_quality: CursorQuality
    software_drained: bool
    final_segment_sequence: int | None
    all_segments_closed: bool

    @property
    def heard_text_hash(self) -> str:
        """Return the bounded milestone hash for the conservative prefix."""
        return hashlib.sha256(self.heard_text.encode()).hexdigest()

    @property
    def fully_presented(self) -> bool:
        """Return whether every accepted sample crossed the audible horizon."""
        return (
            self.all_segments_closed
            and self.software_drained
            and self.estimated_audible_samples >= self.accepted_samples
        )


class PlaybackLedger:
    """Mutable single-owner timeline for one playback generation."""

    def __init__(self, lease: GenerationLease, *, sample_rate: int) -> None:
        """Create an empty generation timeline owned by one media actor."""
        self.lease = lease
        self.sample_rate = sample_rate
        self._accepted_cursor = 0
        self._submitted_cursor = 0
        self._estimated_audible_cursor = 0
        self._cursor_quality: CursorQuality = "unknown"
        self._cursor_quality_observed = False
        self._chunks: dict[int, SpeechChunk] = {}
        self._text_cursor = 0
        self._software_drained = False
        self._frozen = False

    @property
    def accepted_cursor(self) -> int:
        """Return the next accepted output cursor."""
        return self._accepted_cursor

    def begin_segment(self, *, sequence: int, text: str, segment_hash: str) -> None:
        """Open one segment before its first PCM is accepted."""
        if self._frozen:
            msg = "cannot open a segment on a frozen playback ledger"
            raise RuntimeError(msg)
        existing = self._chunks.get(sequence)
        if existing is not None:
            if existing.segment_hash != segment_hash or existing.text != text:
                msg = f"segment {sequence} conflicts with its existing ledger entry"
                raise ValueError(msg)
            return
        text_start = self._text_cursor
        self._text_cursor += len(text)
        self._chunks[sequence] = SpeechChunk(
            response_id=self.lease.response_id,
            playback_generation_id=self.lease.playback_generation_id,
            segment_sequence=sequence,
            text=text,
            text_start=text_start,
            text_end=self._text_cursor,
            timeline_epoch=self.lease.timeline_epoch,
            output_start_cursor=self._accepted_cursor,
            output_end_cursor=None,
            sample_rate=self.sample_rate,
            cursor_quality="unknown",
            audibility_class="normal",
            segment_hash=segment_hash,
        )

    def accept_samples(self, *, sequence: int, sample_count: int) -> AcceptedSamples:
        """Append accepted resampled samples to one open segment."""
        if self._frozen:
            msg = "cannot accept samples on a frozen playback ledger"
            raise RuntimeError(msg)
        if sample_count <= 0:
            msg = "sample_count must be positive"
            raise ValueError(msg)
        chunk = self._chunks.get(sequence)
        if chunk is None or chunk.closed:
            msg = f"segment {sequence} is not open"
            raise RuntimeError(msg)
        start = self._accepted_cursor
        self._accepted_cursor += sample_count
        self._software_drained = False
        return AcceptedSamples(
            playback_generation_id=self.lease.playback_generation_id,
            segment_sequence=sequence,
            output_start_cursor=start,
            output_end_cursor=self._accepted_cursor,
        )

    def finish_segment(self, *, sequence: int) -> None:
        """Close one segment exactly at its accepted cursor."""
        chunk = self._chunks.get(sequence)
        if chunk is None:
            msg = f"segment {sequence} was never opened"
            raise RuntimeError(msg)
        if chunk.closed:
            return
        chunk.output_end_cursor = self._accepted_cursor
        chunk.closed = True
        # The callback may have crossed the conservative presentation horizon
        # while provider I/O still owned this segment. Preserve that already
        # observed quality when the semantic boundary becomes known; otherwise
        # a fully presented segment would remain ``unknown`` forever merely
        # because SegmentFinished arrived after its last callback report.
        if (
            self._cursor_quality_observed
            and chunk.output_end_cursor <= self._estimated_audible_cursor
        ):
            chunk.cursor_quality = self._cursor_quality

    def record_submitted(
        self,
        *,
        output_start_cursor: int,
        output_end_cursor: int,
        audibility_class: AudibilityClass,
    ) -> None:
        """Advance host submission and classify the post-gain interval."""
        if output_start_cursor < 0 or output_end_cursor < output_start_cursor:
            msg = "invalid submitted output span"
            raise ValueError(msg)
        # Reports are ordered, but callback-report overflow may create a gap.
        # Never guess through it: keep the cursor at the last contiguous end.
        if output_start_cursor != self._submitted_cursor:
            self._cursor_quality = "unknown"
            self._cursor_quality_observed = True
            for chunk in self._chunks.values():
                if (
                    chunk.output_end_cursor is None
                    or chunk.output_end_cursor > self._submitted_cursor
                ):
                    chunk.audibility_class = "unknown"
            return
        self._submitted_cursor = min(output_end_cursor, self._accepted_cursor)
        for chunk in self._chunks.values():
            chunk_end = chunk.output_end_cursor or self._accepted_cursor
            if output_start_cursor < chunk_end and output_end_cursor > chunk.output_start_cursor:
                chunk.audibility_class = _least_audible(
                    chunk.audibility_class,
                    audibility_class,
                )

    def record_audible(
        self,
        *,
        output_cursor: int,
        cursor_quality: CursorQuality,
    ) -> None:
        """Advance the conservative audible horizon, rounding backward."""
        bounded = max(
            self._estimated_audible_cursor,
            min(int(output_cursor), self._submitted_cursor, self._accepted_cursor),
        )
        self._estimated_audible_cursor = bounded
        self._cursor_quality = (
            _least_quality(self._cursor_quality, cursor_quality)
            if self._cursor_quality_observed
            else cursor_quality
        )
        self._cursor_quality_observed = True
        for chunk in self._chunks.values():
            if chunk.output_end_cursor is not None and chunk.output_end_cursor <= bounded:
                # ``_least_quality`` combines two already observed qualities,
                # so the birth sentinel must not be fed to it: ``unknown``
                # outranks everything and would pin the chunk there forever,
                # leaving the heard prefix permanently empty.
                chunk.cursor_quality = (
                    _least_quality(chunk.cursor_quality, cursor_quality)
                    if chunk.cursor_quality_observed
                    else cursor_quality
                )
                chunk.cursor_quality_observed = True

    def mark_software_drained(self) -> None:
        """Record ring exhaustion without treating it as audible completion."""
        self._software_drained = True

    def freeze(self) -> OutputTimelineSnapshot:
        """Close write eligibility and return the conservative snapshot."""
        self._frozen = True
        return self.snapshot()

    def snapshot(self) -> OutputTimelineSnapshot:
        """Return a conservative immutable view without changing ownership."""
        sequences = sorted(self._chunks)
        heard_parts: list[str] = []
        heard_through: int | None = None
        all_closed = bool(sequences)
        for sequence in sequences:
            chunk = self._chunks[sequence]
            if not chunk.closed:
                all_closed = False
                break
            end = chunk.output_end_cursor
            if (
                end is None
                or end > self._estimated_audible_cursor
                or chunk.audibility_class != "normal"
                or chunk.cursor_quality == "unknown"
            ):
                break
            heard_parts.append(chunk.text)
            heard_through = sequence
        if any(not chunk.closed for chunk in self._chunks.values()):
            all_closed = False
        return OutputTimelineSnapshot(
            lease=self.lease,
            accepted_samples=self._accepted_cursor,
            submitted_samples=self._submitted_cursor,
            estimated_audible_samples=self._estimated_audible_cursor,
            heard_through_sequence=heard_through,
            heard_text="".join(heard_parts),
            cursor_quality=self._cursor_quality,
            software_drained=self._software_drained,
            final_segment_sequence=sequences[-1] if sequences else None,
            all_segments_closed=all_closed,
        )


_AUDIBILITY_RANK: dict[AudibilityClass, int] = {
    "normal": 0,
    "attenuated": 1,
    "muted": 2,
    "unknown": 3,
}
_QUALITY_RANK: dict[CursorQuality, int] = {
    "measured_dac": 0,
    "estimated": 1,
    "unknown": 2,
}


def _least_audible(left: AudibilityClass, right: AudibilityClass) -> AudibilityClass:
    """Return the more conservative audibility classification."""
    return left if _AUDIBILITY_RANK[left] >= _AUDIBILITY_RANK[right] else right


def _least_quality(left: CursorQuality, right: CursorQuality) -> CursorQuality:
    """Return the less certain of two already-observed cursor qualities."""
    return left if _QUALITY_RANK[left] >= _QUALITY_RANK[right] else right


__all__ = [
    "AcceptedSamples",
    "AudibilityClass",
    "CursorQuality",
    "ForegroundBusy",
    "GenerationLease",
    "OutputTimelineSnapshot",
    "PlaybackLedger",
    "SpeechChunk",
    "StalePlaybackGeneration",
]
