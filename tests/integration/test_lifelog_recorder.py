"""ADR 0239: the Mac terminal's microphone goes into the brain's day archive, piece by piece.

Acceptance checks against the real recorder, the real brain route and archive, and a fake lane
and encoder (no PortAudio, no afconvert):

- frames are cut into pieces at each ten-minute mark of the wall clock, at a gap in the stream
  (a discontinuity, a new stream epoch, a skipped cursor) and while paused, with exact lengths;
  a piece the stream runs on into after a mark ends at the next mark, never a sliver later;
- each piece reaches the brain's archive with its start, length and bytes, and leaves the outbox
  only once the brain's SHA-256 matches;
- a refused piece is held, never deleted; a full brain and an unreachable one leave it waiting;
- WAVs a stopped run left half-written are recovered from the samples on disk.
"""

from __future__ import annotations

import functools
import hashlib
import wave
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.lifelog import LIFELOG_DIRNAME, Lifelog
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import lifelog_recorder
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.lifelog_recorder import (
    FULL_RETRY_S,
    HELD_DIRNAME,
    RECORDING_DIRNAME,
    LifelogRecorder,
)
from jarvis.surface.voice_audio import CanonicalAudioFrame

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

REMOTE = "100.87.250.92"
RATE = 16_000
FRAME = 512
BOUNDARY_MS = 1_791_756_000_000  # 2026-10-11T22:00:00Z, a ten-minute mark
FRAME_MS = FRAME * 1000 // RATE


def fake_encode(wav: Path, stem: Path) -> Path:
    """A CAF whose body is the WAV's samples, so the brain takes it and the bytes can be read."""
    with wave.open(str(wav), "rb") as src:
        pcm = src.readframes(src.getnframes())
    out = stem.with_suffix(".caf")
    out.write_bytes(b"caff" + pcm)
    return out


class Lane:
    """The frames a test hands over; closing it is all the recorder asks of it."""

    def __init__(self) -> None:
        """Open."""
        self.closed = False

    def read(self, *, timeout_s: float = 0.0) -> CanonicalAudioFrame | None:
        """Nothing arrives by itself: the tests call ``feed``."""
        del timeout_s
        return None

    def close(self) -> None:
        """Mark it gone."""
        self.closed = True


class Frames:
    """Consecutive canonical frames of one stream, each sample a distinct value."""

    def __init__(self) -> None:
        """At the stream's first sample."""
        self.cursor = 0
        self.epoch = 1
        self.sequence = 0

    def next(self, *, gap: bool = False, epoch: int | None = None) -> CanonicalAudioFrame:
        """The next frame; ``gap`` skips ahead in the stream, ``epoch`` starts a new one."""
        if gap:
            self.cursor += FRAME * 3
        if epoch is not None:
            self.epoch = epoch
        pcm = bytes((self.sequence % 251,)) * (FRAME * 2)
        frame = CanonicalAudioFrame(
            stream_epoch=self.epoch, sequence=self.sequence, sample_cursor=self.cursor,
            sample_rate_hz=RATE, frame_count=FRAME, adc_time_s=None,
            captured_monotonic_ns=0, discontinuity_before=False, pcm16_mono=pcm,
        )
        self.cursor += FRAME
        self.sequence += 1
        return frame


@pytest.fixture
def brain(tmp_path: Path) -> Iterator[tuple[TestClient, str, Lifelog]]:
    """The brain's app with the archive, and the Mac's paired token."""
    root = tmp_path / "brain"
    root.mkdir()
    store = Lifelog(root / LIFELOG_DIRNAME, free_bytes=lambda _p: 100 * 1024**3)
    app = create_app(InherentDeps(
        submit_callable=lambda _t: "T", broadcaster=InherentBroadcaster(),
        device_name=functools.partial(device_name_for_token, root), lifelog=store,
    ))
    require_local_key(
        app, functools.partial(local_key_matches, local_key(root)), extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "macbook")
    with TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)) as client:
        yield client, token, store


def _bridge(client: TestClient) -> httpx.MockTransport:
    """The recorder's requests, answered by the brain's real app through its test client."""

    def handle(request: httpx.Request) -> httpx.Response:
        answer = client.request(
            request.method, request.url.raw_path.decode(), content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        )
        return httpx.Response(answer.status_code, content=answer.content)

    return httpx.MockTransport(handle)


def _recorder(
    tmp_path: Path, client: TestClient | None, token: str, *, paused: list[bool] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LifelogRecorder:
    flag = paused if paused is not None else [False]
    recorder = LifelogRecorder(
        Lane(), tmp_path / "mac" / "lifelog-outbox",
        brain_url=f"http://{REMOTE}:8006", token=token, paused=lambda: flag[0],
        encode=fake_encode,
        transport=transport or (None if client is None else _bridge(client)),
    )
    for sub in ("", RECORDING_DIRNAME, HELD_DIRNAME):
        (tmp_path / "mac" / "lifelog-outbox" / sub).mkdir(parents=True, exist_ok=True)
    return recorder


def _ready(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "mac" / "lifelog-outbox").glob("*.caf"))


def test_pieces_break_at_the_ten_minute_mark_at_gaps_and_while_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two frames before the mark: a piece ends there; a gap, an epoch and a pause cut too."""
    starts = iter([
        BOUNDARY_MS - 2 * FRAME_MS, BOUNDARY_MS, BOUNDARY_MS + 10_000, BOUNDARY_MS + 20_000,
        BOUNDARY_MS + 30_000,
    ])
    monkeypatch.setattr(lifelog_recorder, "_wall_ms_of", lambda _frame: next(starts))
    paused = [False]
    recorder = _recorder(tmp_path, None, "t", paused=paused)
    frames = Frames()

    for _ in range(3):  # two before the mark, the third opens a new piece
        recorder.feed(frames.next())
    recorder.feed(frames.next())
    recorder.feed(frames.next(gap=True))  # a skipped cursor
    recorder.feed(frames.next(epoch=2))  # the device reopened
    paused[0] = True
    recorder.feed(frames.next())  # dropped, and the piece in hand closes
    paused[0] = False
    recorder.feed(frames.next())
    recorder._finish()  # noqa: SLF001

    names = _ready(tmp_path)
    assert [(n.split("_")[0], n.split("_")[1]) for n in names] == [
        (str(BOUNDARY_MS - 2 * FRAME_MS), f"{2 * FRAME_MS}ms"),
        (str(BOUNDARY_MS), f"{2 * FRAME_MS}ms"),
        (str(BOUNDARY_MS + 10_000), f"{FRAME_MS}ms"),
        (str(BOUNDARY_MS + 20_000), f"{FRAME_MS}ms"),
        (str(BOUNDARY_MS + 30_000), f"{FRAME_MS}ms"),
    ]
    assert list((tmp_path / "mac" / "lifelog-outbox" / RECORDING_DIRNAME).iterdir()) == []


def test_a_cut_at_the_mark_runs_on_to_the_next_mark_however_the_wall_clock_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frame after a mark's cut read 38 ms short of it: its piece still runs ten minutes."""
    starts = iter([BOUNDARY_MS - 2 * FRAME_MS, BOUNDARY_MS - 38, BOUNDARY_MS + 1])
    monkeypatch.setattr(lifelog_recorder, "_wall_ms_of", lambda _frame: next(starts))
    recorder = _recorder(tmp_path, None, "t")
    frames = Frames()

    for _ in range(2 + 6):  # two before the mark, then six that cross it on the wall clock
        recorder.feed(frames.next())
    recorder._finish()  # noqa: SLF001

    assert [n.split("_")[:2] for n in _ready(tmp_path)] == [
        [str(BOUNDARY_MS - 2 * FRAME_MS), f"{2 * FRAME_MS}ms"],
        [str(BOUNDARY_MS - 38), f"{6 * FRAME_MS}ms"],
    ]


def test_a_piece_reaches_the_archive_and_leaves_the_outbox_once_the_digest_matches(
    tmp_path: Path, brain: tuple[TestClient, str, Lifelog], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brain keeps the bytes under the Mac's name, start and length; the Mac's copy goes."""
    client, token, store = brain
    monkeypatch.setattr(lifelog_recorder, "_wall_ms_of", lambda _frame: BOUNDARY_MS + 5_000)
    recorder = _recorder(tmp_path, client, token)
    frames = Frames()
    for _ in range(4):
        recorder.feed(frames.next())
    recorder._finish()  # noqa: SLF001
    (sent,) = (tmp_path / "mac" / "lifelog-outbox").glob("*.caf")
    body = sent.read_bytes()

    recorder.send_ready()

    (kept,) = (p for p in store.directory.rglob("*") if p.is_file())
    assert kept.relative_to(store.directory).parts[:3] == ("audio", "macbook", "2026-10-11")
    assert kept.name.startswith(f"20261011T220005.000Z_{4 * FRAME_MS}ms_")
    assert hashlib.sha256(kept.read_bytes()).digest() == hashlib.sha256(body).digest()
    assert _ready(tmp_path) == []


def test_refused_pieces_are_held_and_a_full_or_absent_brain_leaves_them_waiting(
    tmp_path: Path, brain: tuple[TestClient, str, Lifelog],
) -> None:
    """415 moves the piece to held; 507 and a dead link keep it in the outbox for later."""
    client, token, store = brain
    outbox = tmp_path / "mac" / "lifelog-outbox"
    recorder = _recorder(tmp_path, client, token)
    bad = outbox / f"{BOUNDARY_MS}_1000ms_{'a' * 32}.caf"
    bad.write_bytes(b"not audio")

    recorder.send_ready()

    assert not bad.exists()
    assert (outbox / HELD_DIRNAME / bad.name).read_bytes() == b"not audio"

    piece = outbox / f"{BOUNDARY_MS}_1000ms_{'b' * 32}.caf"
    piece.write_bytes(b"caff" + b"\x00" * 100)
    store.min_free_bytes = 10**18
    assert recorder.send_ready() == FULL_RETRY_S
    assert piece.exists()

    def down(_request: httpx.Request) -> httpx.Response:
        message = "no route to the brain"
        raise httpx.ConnectError(message)

    offline = _recorder(tmp_path, None, token, transport=httpx.MockTransport(down))
    offline.send_ready()
    assert piece.exists()


def test_a_half_written_wav_is_recovered_from_its_samples(
    tmp_path: Path, brain: tuple[TestClient, str, Lifelog],
) -> None:
    """A WAV whose header never got its lengths becomes a piece of the samples on disk."""
    client, token, _store = brain
    recorder = _recorder(tmp_path, client, token)
    recording = tmp_path / "mac" / "lifelog-outbox" / RECORDING_DIRNAME
    wav = recording / f"{BOUNDARY_MS}_{'c' * 32}.wav"
    writer = wave.open(str(wav), "wb")  # noqa: SIM115 — left open on purpose, as a crash would
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(RATE)
    writer.writeframesraw(b"\x01\x00" * RATE)  # one second; the header still says zero
    writer._file.flush()  # type: ignore[attr-defined]  # noqa: SLF001

    recorder.recover()

    assert _ready(tmp_path) == [f"{BOUNDARY_MS}_1000ms_{'c' * 32}.caf"]
    assert not wav.exists()


def test_the_terminal_records_only_when_its_own_config_says_so(tmp_path: Path) -> None:
    """``lifelog_capture.enabled`` off or no brain: nothing subscribes. On: a diagnostic lane."""
    from types import SimpleNamespace  # noqa: PLC0415

    from jarvis.runtime import terminal  # noqa: PLC0415
    from jarvis.surface.voice_audio import SubscriberPurpose  # noqa: PLC0415

    asked: list[tuple[str, SubscriberPurpose]] = []

    def subscribe(*, name: str, purpose: SubscriberPurpose, capacity: int) -> Lane:
        del capacity
        asked.append((name, purpose))
        return Lane()

    session = SimpleNamespace(ingress=SimpleNamespace(subscribe=subscribe))

    def speaking(config: dict[str, object], url: str = "http://jarvis:8006") -> object:
        return terminal._Speaking(  # noqa: SLF001
            config, tmp_path, brain_url=url, brain_token="tok",  # noqa: S106
        )

    off = terminal._start_lifelog(speaking({}), session)  # type: ignore[arg-type]  # noqa: SLF001
    no_brain = terminal._start_lifelog(  # noqa: SLF001
        speaking({"lifelog_capture": {"enabled": True}}, url=""), session,  # type: ignore[arg-type]
    )
    on = terminal._start_lifelog(  # noqa: SLF001
        speaking({"lifelog_capture": {"enabled": True}}), session,  # type: ignore[arg-type]
    )

    assert (off, no_brain) == (None, None)
    assert on is not None
    assert asked == [("lifelog", SubscriberPurpose.DIAGNOSTIC)]
    on.close()
    assert (tmp_path / "lifelog-outbox" / RECORDING_DIRNAME).is_dir()
